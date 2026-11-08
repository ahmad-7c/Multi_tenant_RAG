#!/usr/bin/env bash
# Nightly backup: the database, and the uploaded documents.
#
# Runs as the `deploy` user from its own crontab. NOT a systemd timer:
# installing a system unit needs root, and `deploy` deliberately has no
# sudo (see the runbook section 4). A systemd *user* timer would need
# `loginctl enable-linger deploy`, which also needs root. A user crontab
# needs neither and survives reboots.
#
#   crontab -l   # see it
#   bash scripts/backup.sh   # run it by hand, any time
#
# WHAT THIS IS NOT. A backup on the same disk as the thing it backs up
# is not a backup -- it survives `DROP TABLE` and a bad migration, and
# it does not survive the instance being deleted, the volume failing, or
# the account being lost. The off-box copy is a SEPARATE decision,
# recorded in the runbook; this script's job is to produce a correct
# local artefact that something else can copy away.
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"
BACKUP_DIR="${BACKUP_DIR:-/home/deploy/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
# UTC, sortable, no colons -- colons are legal on Linux and a nuisance
# the moment a file is copied to Windows or into an S3 key.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

log() { echo "[backup $(date -u +%H:%M:%S)] $*"; }
die() { log "FAILED: $*"; exit 1; }

# 0700, and created with the right mode rather than chmod'ed after, so
# it is never briefly group-readable. A dump contains every row of every
# tenant's data: it is the single most sensitive file on the box.
install -d -m 700 "$BACKUP_DIR"

# POSTGRES_USER and POSTGRES_DB are not secrets. The PASSWORD is never
# read, needed or passed: pg_dump runs INSIDE the db container over the
# local unix socket, which the postgres image trusts.
PG_USER="$(sed -n 's/^POSTGRES_USER=//p' .env.production | head -1)"
PG_DB="$(sed -n 's/^POSTGRES_DB=//p' .env.production | head -1)"
[ -n "$PG_USER" ] || die "POSTGRES_USER is not set in .env.production"
[ -n "$PG_DB" ] || die "POSTGRES_DB is not set in .env.production"

DUMP="$BACKUP_DIR/documind-db-$STAMP.dump"
UPLOADS="$BACKUP_DIR/documind-uploads-$STAMP.tar.gz"

# ------------------------------------------------------------ database
# -Fc is the custom format: compressed by default, and the only format
# pg_restore can filter -- it can restore a single table, or list the
# contents without restoring anything, which is what makes the
# verification below and the restore drill possible. A plain SQL dump
# can only be replayed whole.
#
# `exec -T` with no TTY: a TTY would corrupt the binary stream with
# carriage returns, and the symptom is a dump that only fails at restore
# time, months later.
log "dumping $PG_DB"
if ! $COMPOSE exec -T db pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$DUMP"; then
  rm -f "$DUMP"
  die "pg_dump failed (is the stack up? \`$COMPOSE ps\`)"
fi

# VERIFY, do not assume. A zero-byte or truncated dump is the classic
# silent backup failure: the cron job succeeds every night for a year
# and the first anyone knows is a restore that cannot work.
#
# pg_restore --list parses the archive's table of contents, so it fails
# on a truncated or non-archive file. It reads only the header, so it is
# fast and touches no database.
if ! pg_restore_out="$(docker run --rm -i --entrypoint pg_restore \
      pgvector/pgvector:pg16 --list < "$DUMP" 2>&1)"; then
  rm -f "$DUMP"
  die "the dump is not a readable pg_restore archive"
fi
entries="$(printf '%s\n' "$pg_restore_out" | grep -c '^[0-9]' || true)"
[ "$entries" -gt 0 ] || { rm -f "$DUMP"; die "the dump archive is empty"; }
log "dump OK: $(du -h "$DUMP" | cut -f1), $entries archive entries"

# ------------------------------------------------------------- uploads
# The named volume, not a host path: with STORAGE_BACKEND=local the
# documents live in `documind-prod_uploads`, which has no location on
# the host filesystem that is safe to assume.
#
# --entrypoint tar, explicitly. Relying on the postgres image's
# entrypoint to exec an unknown command works today and is not a
# contract. :ro so a backup can never write to the thing it is reading.
log "archiving the uploads volume"
if ! docker run --rm --entrypoint tar \
      -v documind-prod_uploads:/src:ro \
      -v "$BACKUP_DIR":/dst \
      pgvector/pgvector:pg16 -czf "/dst/$(basename "$UPLOADS")" -C /src . ; then
  rm -f "$UPLOADS"
  die "the uploads archive failed"
fi
if ! docker run --rm --entrypoint tar -v "$BACKUP_DIR":/dst:ro \
      pgvector/pgvector:pg16 -tzf "/dst/$(basename "$UPLOADS")" >/dev/null; then
  rm -f "$UPLOADS"
  die "the uploads archive is not readable"
fi
log "uploads OK: $(du -h "$UPLOADS" | cut -f1)"

# An empty uploads volume is legitimate (nothing uploaded yet) and is
# NOT an error -- but it is worth saying out loud, because "backup
# succeeded" over an empty archive is how a broken volume mount hides.
files="$(docker run --rm --entrypoint tar -v "$BACKUP_DIR":/dst:ro \
          pgvector/pgvector:pg16 -tzf "/dst/$(basename "$UPLOADS")" | grep -vc '/$' || true)"
log "uploads archive contains $files file(s)"

chmod 600 "$DUMP" "$UPLOADS"

# ----------------------------------------------------------- retention
# -mtime +N is "older than N days". Scoped by name AND to this
# directory's top level, so a mistyped BACKUP_DIR deletes nothing it did
# not create. -delete rather than `-exec rm` so there is no shell
# quoting between find and the filesystem.
log "pruning backups older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'documind-db-*.dump' \
  -mtime +"$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'documind-uploads-*.tar.gz' \
  -mtime +"$RETENTION_DAYS" -delete

kept="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'documind-db-*.dump' | wc -l)"
log "done. $kept database backup(s) retained in $BACKUP_DIR"
