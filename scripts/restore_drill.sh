#!/usr/bin/env bash
# Restore drill: prove a backup can actually be restored.
#
#   bash scripts/restore_drill.sh                      # newest dump
#   bash scripts/restore_drill.sh /path/to/a.dump      # a specific one
#
# A backup that has never been restored is not a backup, it is a file.
# This restores into a THROWAWAY container with its own anonymous
# volume, compares row counts against production, and destroys the
# container afterwards.
#
# WHAT IT NEVER TOUCHES, by construction rather than by care:
#   - documind-prod_db_data   (the production database volume)
#   - documind-prod_uploads   (the documents)
#   - the documind-prod compose project at all, except to READ counts
#
# The drill container is started with `docker run`, not through the
# compose file, so there is no project, no named volume and no network
# it could join by accident. Its storage is an anonymous volume that
# dies with the container.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"
BACKUP_DIR="${BACKUP_DIR:-/home/deploy/backups}"
DRILL_NAME="documind-restore-drill"
DRILL_PW="drill-only-$(date +%s)"   # throwaway, never leaves this script

log() { echo "[drill $(date -u +%H:%M:%S)] $*"; }
die() { log "FAILED: $*"; exit 1; }

# Always destroy the drill container, on every exit path. Without this a
# failed drill leaves a stray postgres holding ~100 MB on a 2 GB box.
cleanup() {
  docker rm -f "$DRILL_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
  DUMP="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'documind-db-*.dump' \
          | sort | tail -1)"
fi
[ -n "$DUMP" ] || die "no dump found in $BACKUP_DIR"
[ -f "$DUMP" ] || die "$DUMP does not exist"
log "restoring $(basename "$DUMP") ($(du -h "$DUMP" | cut -f1))"

PG_USER="$(sed -n 's/^POSTGRES_USER=//p' .env.production | head -1)"
PG_DB="$(sed -n 's/^POSTGRES_DB=//p' .env.production | head -1)"

# The tables whose counts are worth comparing. Deliberately explicit
# rather than "every table in the schema": alembic_version is expected
# to hold exactly one row, and a count over a list nobody chose is a
# number nobody reads.
TABLES="organizations users documents document_chunks conversations messages usage_events"

# ------------------------------------------------- production counts
# Read-only. Taken BEFORE the restore so the comparison below is against
# a single point in time.
log "reading production row counts"
prod_counts=""
for t in $TABLES; do
  n="$($COMPOSE exec -T db psql -U "$PG_USER" -d "$PG_DB" -tAc \
        "select count(*) from $t" 2>/dev/null | tr -d '[:space:]')" || n="?"
  prod_counts="$prod_counts $t=$n"
done

# ----------------------------------------------- the drill container
# No -p, so it publishes nothing and cannot be reached from outside.
# No -v, so its data directory is an anonymous volume removed with the
# container. A fixed name so a stray one is obvious and removable.
log "starting a throwaway postgres (publishes no port, anonymous volume)"
docker run -d --rm --name "$DRILL_NAME" \
  -e POSTGRES_PASSWORD="$DRILL_PW" \
  -e POSTGRES_USER="$PG_USER" \
  -e POSTGRES_DB="$PG_DB" \
  pgvector/pgvector:pg16 >/dev/null

log "waiting for it to accept connections"
for _ in $(seq 1 60); do
  if docker exec "$DRILL_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$DRILL_NAME" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
  || die "the drill database never became ready"

# pgvector must exist before restoring: document_chunks has a vector
# column, and pg_restore would fail on the type. The image ships the
# extension; it still has to be created in this database.
docker exec "$DRILL_NAME" psql -U "$PG_USER" -d "$PG_DB" \
  -c 'create extension if not exists vector' >/dev/null

# --------------------------------------------------------- restore
# --no-owner / --no-privileges: the dump records the production role as
# owner, and the drill database has no such role beyond POSTGRES_USER.
# Without these, every GRANT fails and the log fills with errors that
# have nothing to do with whether the DATA restored.
#
# --exit-on-error is deliberately NOT used: `create extension vector`
# above means the extension already exists, and a dump that also creates
# it would abort an otherwise perfect restore on a harmless duplicate.
# Errors are counted instead, and reported.
log "running pg_restore"
restore_log="$(docker exec -i "$DRILL_NAME" pg_restore \
  -U "$PG_USER" -d "$PG_DB" --no-owner --no-privileges < "$DUMP" 2>&1 || true)"
errors="$(printf '%s\n' "$restore_log" | grep -c '^pg_restore: error' || true)"
if [ "$errors" -gt 0 ]; then
  log "pg_restore reported $errors error line(s):"
  printf '%s\n' "$restore_log" | grep '^pg_restore: error' | head -10 | sed 's/^/    /'
fi

# --------------------------------------------------- compare counts
log "comparing row counts"
printf '\n  %-20s %12s %12s   %s\n' "table" "production" "restored" "verdict"
printf '  %-20s %12s %12s   %s\n' "--------------------" "----------" "--------" "-------"

fail=0
for t in $TABLES; do
  prod="$(printf '%s' "$prod_counts" | tr ' ' '\n' | sed -n "s/^$t=//p")"
  rest="$(docker exec "$DRILL_NAME" psql -U "$PG_USER" -d "$PG_DB" -tAc \
          "select count(*) from $t" 2>/dev/null | tr -d '[:space:]')" || rest="?"

  verdict="ok"
  if [ "$rest" = "?" ] || [ -z "$rest" ]; then
    verdict="MISSING TABLE"; fail=1
  elif [ "$prod" = "?" ] || [ -z "$prod" ]; then
    verdict="prod unreadable"
  elif [ "$rest" -gt "$prod" ]; then
    # More rows than production has cannot come from a dump of it.
    verdict="MORE THAN PROD"; fail=1
  elif [ "$prod" -gt 0 ] && [ "$rest" -eq 0 ]; then
    verdict="EMPTY, PROD IS NOT"; fail=1
  elif [ "$rest" -lt "$prod" ]; then
    # Expected and not a failure: production keeps accepting writes
    # after the dump is taken. Flagged so the gap is visible.
    verdict="older than prod"
  fi
  printf '  %-20s %12s %12s   %s\n' "$t" "$prod" "$rest" "$verdict"
done
echo

# One more check the counts cannot make: the schema version. A dump
# restored into the wrong code generation is a different kind of broken.
ver="$(docker exec "$DRILL_NAME" psql -U "$PG_USER" -d "$PG_DB" -tAc \
       'select version_num from alembic_version' 2>/dev/null | tr -d '[:space:]')" || ver=""
log "restored schema version: ${ver:-UNKNOWN}"
[ -n "$ver" ] || { log "alembic_version is missing or empty"; fail=1; }

if [ "$fail" -ne 0 ]; then
  die "the restore drill did NOT pass -- this backup cannot be relied on"
fi
log "RESTORE DRILL PASSED. Production volumes were never touched."
