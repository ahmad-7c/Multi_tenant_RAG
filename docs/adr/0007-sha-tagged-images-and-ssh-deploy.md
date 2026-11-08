# 0007. SHA-tagged images, SSH deploy, and automatic rollback

Date: 2026-11-06
Status: Accepted. **Written but never executed** — see "Not verified".

## Context

One Lightsail instance, Docker Compose, no orchestrator. A push to
`main` should deploy, and a bad deploy should undo itself without a
human watching. The constraints that shaped every decision below:

- the box has 2 GB of RAM and one small disk, so nothing can assume
  spare capacity;
- there is exactly one instance, so there is no blue/green and no
  rolling update — a deploy is a replacement, and the only safety net
  is the ability to go back;
- the deploy step necessarily holds a credential that reaches
  production;
- region fixed by account (ap-southeast-2); latency to US-hosted
  Gemini/Cohere accepted; mitigation: place the Vercel BFF function
  region near Sydney if the plan allows it.

## Decisions

### Images are tagged by git SHA, and nothing deploys `latest`

The SHA tag is immutable, which is the entire property that makes
rollback possible: "go back to the previous revision" is only a
meaningful instruction if that revision still resolves to the same
bytes. `latest` is also pushed, but purely as a convenience for a human
reading the registry — `deploy.sh` is passed a SHA and
`test_the_deployed_tag_is_the_git_sha_not_latest` pins that the deploy
job never references the moving tag.

### CD triggers on `workflow_run`, not on push

A deploy must be impossible unless CI passed **for that exact commit**.
Two consequences that are easy to miss and are both pinned by tests:

- `workflow_run` fires when the referenced workflow **fails** as well as
  when it succeeds, so the conclusion is checked explicitly
  (`github.event.workflow_run.conclusion == 'success'`). Without that
  line a red CI would deploy, which inverts the point of the trigger.
- the checkout uses `workflow_run.head_sha`, not the branch tip. A push
  landing during the build would otherwise deploy code no CI run ever
  saw. The race is small; the consequence is unbounded.

Splitting CI and CD into two files also keeps `packages: write` out of
the workflow that runs on pull requests, including from forks. CI holds
`contents: read` and nothing else.

**CI triggers on every branch push and drops the `pull_request`
trigger.** Every branch is verified before it reaches `main`, rather
than sitting unverified until someone opens a PR. The cost, stated
because it is real: a pull request from a FORK pushes no branch here and
therefore gets **no CI at all**. Acceptable while this is a
single-author repository — bring `pull_request:` back the day an outside
contribution arrives, and accept that a same-repo PR will then run the
whole pipeline twice for one commit.

### A deploy is never cancelled in flight

`concurrency.cancel-in-progress: false`, with a single group for the
whole environment. Cancelling mid-deploy could abort a half-applied
migration or leave the stack between two image tags. A queued deploy
waits instead. Per-ref groups would not help: two branches deploying to
one box concurrently is the same hazard.

### Raw `ssh`, not a third-party deploy action

This step holds a key that reaches production. Every candidate action
would have to be pinned to a commit SHA that could not be verified from
this repository, and an unverifiable action in that position is the
supply-chain risk the pinning rule exists to prevent. `ssh` with a key
written to mode 600 is a few lines and auditable end to end.

`StrictHostKeyChecking` is left ON and backed by a `known_hosts` from
secrets. Setting it to `no` is the classic shortcut in exactly this
step, and it makes the deploy MITM-able;
`test_the_deploy_host_key_is_verified` pins its absence.

### Secrets arrive through `env:`, never interpolated into a `run:`

`${{ secrets.X }}` inside a script body is substituted **before the
shell parses the line**, so a secret containing shell metacharacters
becomes executable text. An env var also keeps the value out of process
listings. Pinned by
`test_no_secret_is_interpolated_into_a_shell_line`.

### The deploy logic lives in a script on the server, not in YAML

`scripts/deploy.sh` is a plain script that takes an image and a SHA.
During an incident GitHub may be the thing that is down, and the
recovery path must not depend on it — the same command the pipeline runs
can be run by hand. It is also reviewable and testable, which inline
YAML is not.

### Rollback is code rollback only, never a database rollback

Migrations are expand-then-contract, so the previous image can always
read the newer schema. The rollback path therefore runs **no** migration
at all, pinned by `test_the_rollback_does_not_run_migrations`. A
down-migration executed unattended during a failed deploy is how a bad
deploy becomes data loss.

`.deployed_sha` is written **only after** health and smoke both pass, so
the rollback target is always a revision that actually worked. If the
file is missing (first deploy, or it was lost) the script stops with a
clear message rather than guessing a tag.

### Health is read from the container healthcheck, not a curl

The api publishes no port — only Caddy does. The healthcheck already
hits `/health/ready`, which round-trips `SELECT 1`, so `healthy` means
the database is reachable rather than merely that the process is up.
This is the whole reason ADR-adjacent work added `/health/ready`:
pointed at `/health`, which reports `ok` with Postgres down by design,
the gate would pass for a broken deploy and the rollback would never
fire.

### The smoke test asks a greeting

It must cost nothing in provider quota and must register nothing
permanent. A cached question cannot be relied on (a fresh deploy may
have a cold cache, and the key includes `index_version`), and a real
question bills Gemini and Cohere on every deploy. `app/services/
assistance.py` intercepts greetings before the rewriter, the cache, the
embedder, the reranker and the model — measured at zero calls to each —
so "hello" exercises TLS, the edge, auth, the limiter, SSE framing and a
database write for nothing.

**Stated limit:** it therefore does not prove retrieval or generation
work. That belongs in P10's verification list, run deliberately.

### The secret scan is a repo script, not gitleaks

Same reasoning as the deploy action, and more acute: the scan job reads
the entire git history. `scripts/scan_secrets.sh` scans **tracked files
only** (`git ls-files`) plus every blob in `git rev-list --all`, and it
asserts a control string is found first — a grep that silently stops
matching otherwise looks identical to a clean result, which U6 already
learned once.

Its DSN rule allows the documented development credentials
(`documind:documind` and friends) because those are correct content in
`.env.example` and `docker-compose.yml`, are published in `CLAUDE.md`,
and are already refused in production by
`startup_checks.DEV_DB_MARKERS`. Verified by construction: a
production-shaped DSN is caught, the dev forms and `${VAR}`
substitutions are not.

Revisit if the pattern list grows past a handful of rules — at that
point a maintained scanner earns its supply-chain cost.

## Action pinning, and an honest gap

The `docker/*` actions are pinned to the exact commit SHAs published by
GitHub's own packaging documentation (read 2026-11). The `actions/*`
ones are pinned to major tags (`@v7`, verified against each action's
repository) because resolving their SHAs needed a lookup that was not
available here, and **a fabricated SHA is a workflow that fails on its
first run**. A tag satisfies the "pinned to a version (or commit SHA)"
rule; a SHA is stronger because a tag can be moved.

Converting them is one command per action:

```bash
gh api repos/actions/checkout/commits/v7 --jq .sha
```

`test_no_action_is_used_unpinned` rejects `@main`, `@master`, `@HEAD`
and a bare `uses:` with no ref at all, which is the failure mode that
actually matters.

## Alternatives considered

- **`docker compose pull && up -d` with no verification.** Rejected: it
  reports success for a container that starts and then fails every
  request, which is precisely the state this deployment hit locally
  (`/health` 200, `POST /auth/login` 500).
- **Deploying `latest` and keeping a `previous` tag.** Rejected: two
  moving tags, and after a failed deploy neither reliably names the
  revision you want.
- **Watchtower or a pull-based agent on the box.** Rejected: it deploys
  whatever appears in the registry, which removes the CI gate and the
  rollback decision from the pipeline that is supposed to own them.
- **A GitHub-hosted runner with a long-lived SSH tunnel, or a
  self-hosted runner on the box.** Rejected for now: a self-hosted
  runner executing untrusted pull-request code on the production host
  is a much larger hole than a scoped deploy key.
- **Blue/green on one instance.** Rejected: 2 GB of RAM cannot hold two
  copies of the stack, and the swap file exists to survive one.

## Not verified

**None of this has run.** Written, parsed, and pinned by 18 tests, but:

- there is no Lightsail instance, no static IP, no DuckDNS hostname and
  no `deploy` user, so no deploy has been attempted;
- no GitHub secrets are set, so CD would fail at the ssh step;
- the image has never been built — the local Docker daemon wedged
  mid-build, and the cause turned out to be a **full disk** (`C:` at
  100%, 0 bytes free);
- consequently R4 (the suite running inside the image) is unproven in
  CI as well as locally;
- **the rollback has never been observed firing.** D5 requires
  deliberately deploying a broken revision and watching it undo itself;
  `deploy/runbook.md` §10 gives the exact procedure. Until that has been
  done, "a broken deploy rolls back by itself" is a claim about code
  that has never executed.
