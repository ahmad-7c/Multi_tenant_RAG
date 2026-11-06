DocuMind Phase 5: Deploy to AWS Lightsail
Goal: a portfolio-grade, safe, repeatable deployment of the DocuMind backend on one Lightsail instance, with automated CI/CD. The frontend (Next.js BFF) goes to Vercel later and talks to this backend over HTTPS. The human is a learning engineer who makes the decisions and must understand each one.

Operating rules
Audit first (Step 0). Report, then STOP.
You cannot reach the server. Write files and exact commands; the human runs server-side commands and pastes the output. Do not SSH anywhere unless the human explicitly authorizes it for this session.
Never print, log, commit, or paste secrets (API keys, JWT secret, DB password, SSH keys, .env contents). Production secrets exist only on the server and in GitHub encrypted secrets. Run a secret scan on the repo and git history before the first push of deployment files.
Every checkpoint ends with a report: what was built (5 lines max), an Engineer note per feature (problem, why this solution, how it works, where it lives), checks run with results, a NOT verified list, and decisions needed with a recommendation and tradeoff.
Do not claim something works unless you ran it. Prove each safety property with a test or a command.
Verify third-party facts (Caddy directives, GitHub Actions syntax, image tags, Lightsail limits) against current official docs and cite them.
Keep scope small and boring. One instance, Docker Compose, no Kubernetes, no extra AWS services unless the human asks.
Decisions already made (do not reopen without the human)
Host: one Lightsail Linux instance, Ubuntu LTS, public IPv4, 2 GB RAM plan, static IP attached, in us-east-1 (matches Vercel's default function region; the human may change the region). Add a 2 GB swap file.
Runtime: Docker Compose. Services: api, worker (Celery), db (Postgres with pgvector), redis, caddy, plus a one-shot migrate job.
HTTPS: Caddy with automatic certificates on a free DuckDNS subdomain (no paid domain).
Database in a container on the same box (free), with nightly dumps. A managed database is an ADR alternative, not the default.
Only Caddy publishes ports (80, 443). Postgres and Redis are never published to the host or the internet.
Provider keys (Gemini, Cohere) live only in the server env file.
CI/CD: GitHub Actions, images in GHCR tagged by git SHA, deploy over SSH with a dedicated restricted key, automatic rollback on failed health check.
Step 0: Audit (report, then stop)
What exists already: Dockerfile, compose files, CI config, .dockerignore, health endpoint, migration command, worker command, Python version, test command.
Storage backend for documents (storage_key): local disk or an S3-compatible bucket? What must persist across container restarts (Postgres data, uploaded files, anything else)?
The full list of settings and environment variables, which are required in production, and which have unsafe defaults (JWT secret, DB URL, debug flags, budget settings that default to 0 meaning disabled, CORS absent by design).
Memory footprint estimate per service for a 2 GB box (api, worker concurrency, Postgres, Redis, Caddy) and proposed limits.
Whether /docs and /openapi.json are exposed, and the production default you propose.
How the app handles startup when a required secret is missing or equals a default. Propose a fail-fast check.
Redis usage: broker, rate limiter, budget counters, answer cache. State the eviction risk (broker keys must never be evicted) and the proposed Redis config.
Risks list with a proposed mitigation for each.
P1: Production image and Compose (test locally first)
Dockerfile: multi-stage, pinned slim base, non-root user, no dev dependencies, healthcheck, .dockerignore excluding .env*, tests, git data.
docker-compose.prod.yml: restart policy unless-stopped; healthchecks; depends_on with health conditions; an internal network; memory limits per service so one service dying does not take down the box; json-file log rotation (max-size 10m, max-file 3); named volumes for Postgres and any persisted files.
Postgres sized for 2 GB (small shared_buffers, limited max_connections, sane maintenance_work_mem for HNSW builds). Pin the pgvector image tag.
Redis: password required, internal only, memory cap, eviction policy noeviction so broker keys are never dropped; document what happens at the cap and how the app behaves (fail-open versus fail-closed paths from Phase 4).
Celery: concurrency 1 or 2; acks_late and visibility timeout settings preserved.
Migrations run as a one-shot job BEFORE the api starts, never inside the api container start. Migrations must be backward compatible (expand then contract) so rolling code back never needs a database rollback.
Prove it: bring the stack up locally with the prod compose file, run the test suite's smoke path against it, and show that db and redis ports are not published.
P2: Caddy, HTTPS, and SSE
Caddyfile for the DuckDNS hostname: automatic HTTPS, reverse proxy to api, security headers, request body limit slightly above the upload cap (4 MB target), no server header.
SSE: disable response buffering and compression for text/event-stream (verify the exact Caddy directive in current docs, for example flush_interval -1), generous read timeouts for streams, normal timeouts elsewhere.
HTTP redirects to HTTPS. Hide /docs in production unless the human decides otherwise.
P3: Config and secrets
.env.production exists only on the server: owner is the deploy user, mode 600. A committed .env.production.example lists every variable with no real values.
Generate secrets with a documented command (for example 64 random bytes for the JWT secret). Production keys differ from development keys.
Startup validation: the app refuses to start in production if a required secret is missing, too short, or a known default, if debug is on, or if CORS is open. Add tests for this.
Run a secret scan (gitleaks or equivalent) across history; report findings. If a key was ever committed, treat it as leaked and tell the human to rotate it.
P4: Server hardening runbook (the human runs these)
Write a numbered runbook with exact commands and an expected output for each: create a non-root deploy user; SSH key only, root login and password auth disabled; ufw allows SSH (restricted to the human's IP where possible), 80, and 443 only, and mirrors the Lightsail firewall; unattended security upgrades; fail2ban; swap file; install Docker from the official repository; timezone UTC. Include a rollback note for any step that could lock the human out (keep a second SSH session open until verified).

P5: First manual deploy (the human runs, you guide)
Deploy by hand first so the human understands every step: pull images, run the migrate job, start the stack, check health, register a first organization, upload a small public PDF, ask a question, confirm streaming works through Caddy. Produce a deploy.sh that automates exactly those steps, with set -euo pipefail.

P6: Backups and restore drill
Nightly pg_dump in custom format with 7-day retention, an off-box copy (documented free option, or the human downloads weekly), and a script to restore into a scratch database. Run a restore drill: restore, compare row counts, run one real query. A backup that was never restored is not a backup. Document RPO and RTO honestly.

P7: CI/CD with GitHub Actions
ci.yml on every push and pull request: lint, type check, tests against real Postgres with pgvector and Redis service containers using the offline fakes, dependency audit, secret scan, build the image. Least-privilege permissions, actions pinned to a version (or commit SHA), dependency caching.
cd.yml on main after CI succeeds: build and push the image to GHCR tagged with the git SHA; deploy over SSH using a dedicated deploy key stored in GitHub encrypted secrets (restricted on the server to the deploy script if possible); server pulls by SHA, runs the migrate job, up -d, waits for health, runs the smoke test. On failure: roll back to the previous image tag automatically and fail the job. A concurrency group prevents overlapping deploys. Optionally require a manual approval through a GitHub environment.
The smoke test registers nothing permanent: it checks health, a login with a dedicated smoke account, and one cached or fake-provider question path that costs no provider budget.
P8: Minimal observability
Structured JSON logs with a request id (accept X-Request-ID from the BFF or generate one) on every line; never log secrets, tokens, or document text. Docker healthchecks. An external uptime monitor on /health (a free service). Cost guardrails: AWS Budgets alert, the app's own daily budget guard configured to a non-zero value in production.

P9: Frontend hand-off (Vercel)
When the BFF exists: BACKEND_URL is set as a server-only Vercel environment variable for the Production environment only; preview deployments must not point at production data. No secret in any NEXT_PUBLIC variable. Verify the SSE streaming spike on a Vercel deployment and report the real limits found in current Vercel docs.

P10: Verification from the outside (write the script, the human runs it)
External port scan of the instance IP shows only the intended ports; HTTP redirects to HTTPS; certificate valid; security headers present; /docs hidden; rate limit returns 429 with Retry-After; budget guard returns its 503 when set to a tiny value in a test; SSE streams progressively (curl -N); a 4 MB upload succeeds and a larger one is rejected cleanly; two demo organizations cannot read each other's documents (404); killing the worker container mid-ingestion and restarting it still finishes the document; a full instance reboot brings every service back by itself; the restore drill passes.

Checkpoints
D0: audit. Stop.
D1: P1 and P2 working locally with the prod compose file. Stop.
D2: P3 plus the P4 runbook. Stop (human provisions and hardens the server).
D3: P5 first manual deploy with the human. Stop.
D4: P6 backups and restore drill. Stop.
D5: P7 CI/CD with a rollback demonstration (deliberately deploy a broken build and show the automatic rollback). Stop.
D6: P8, P9, P10, ADRs (single-instance Compose, database in a container, DuckDNS and Caddy, SHA-tagged images with SSH deploy, expand-then-contract migrations), README deployment section, CLAUDE.md update. Stop.
Definition of done
The stack comes up from nothing with one documented command; only 80 and 443 are public (SSH restricted); no secret exists in the repo, images, or logs; startup refuses unsafe config; migrations run as a separate step; backups restore successfully; CI is green; a push to main deploys automatically and a broken deploy rolls back by itself; the outside verification list passes; every item not verified is listed in the final report.

