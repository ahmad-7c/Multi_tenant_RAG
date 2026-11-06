# DocuMind deployment runbook (Lightsail)

You run every command here. I cannot reach the server and will not SSH
anywhere. Paste the output of anything that looks wrong.

Order matters: **§1–§7 must be done once before CI/CD can deploy
anything.** The pipeline in `.github/workflows/cd.yml` assumes a server
that already exists, with Docker installed, the repo at
`/opt/documind`, and `.env.production` in place.

> **Keep a second SSH session open** from §3 until §5 is verified. Every
> step that touches SSH or the firewall can lock you out, and an open
> session is the only cheap way back in.

---

## 1. Create the instance

AWS Lightsail → Create instance:

- **Region** `us-east-1` (matches Vercel's default function region, so
  the BFF→API hop stays in one region).
- **Blueprint** OS only → **Ubuntu 24.04 LTS**.
- **Plan** the 2 GB RAM / 2 vCPU / 60 GB SSD tier.
- **Name** `documind-prod`.

Then:

1. **Networking → attach a static IP.** Without it the public address
   changes on stop/start and your DNS and `known_hosts` both break.
2. **Networking → IPv4 firewall**: allow only
   - SSH 22 — **restricted to your own IP** if it is stable,
   - HTTP 80,
   - HTTPS 443.

   Delete anything else. This mirrors the `ufw` rules in §4; both exist
   because either one alone is a single point of failure.

Expected: the instance shows *Running* with a static IP attached.

---

## 2. Swap (2 GB)

The box has 2 GB of RAM and runs five containers plus Postgres. Without
swap, an HNSW index build or one large PDF ingestion gets the worker
OOM-killed, which presents as a document stuck in `processing` with
nothing in the logs.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
# Prefer RAM; swap is a safety net, not a tier of memory.
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
```

Verify: `free -h` shows `Swap: 2.0Gi`, and `swapon --show` lists
`/swapfile`.

---

## 3. A non-root deploy user

```bash
sudo adduser --disabled-password --gecos "" deploy
sudo usermod -aG docker deploy      # after §6 installs Docker
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
```

Generate the **deploy key on your laptop**, not on the server — a
private key should never exist on the machine it grants access to:

```bash
ssh-keygen -t ed25519 -C "documind-deploy" -f ~/.ssh/documind_deploy
```

Install the public half:

```bash
# on the server, paste the CONTENTS of ~/.ssh/documind_deploy.pub
sudo -u deploy tee -a /home/deploy/.ssh/authorized_keys
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

Verify from your laptop, **in a new terminal, keeping the old one open**:

```bash
ssh -i ~/.ssh/documind_deploy deploy@<STATIC_IP> 'id'
```

Expected: `uid=1001(deploy) ... groups=...,docker`.

> Rollback: if this fails, you still have your original session. Fix
> `authorized_keys` permissions (700 on `.ssh`, 600 on the file) before
> touching §4.

---

## 4. SSH hardening and the firewall

**Only after §3 is verified.**

```bash
sudo tee /etc/ssh/sshd_config.d/99-documind.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sudo sshd -t && sudo systemctl reload ssh
```

`sshd -t` first: it validates the config and a reload with a broken
config is how people lock themselves out permanently.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp     # or: sudo ufw allow from <YOUR_IP> to any port 22
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

Expected: `Status: active`, and exactly 22, 80, 443 allowed.

> Rollback: in your still-open session, `sudo ufw disable`.

---

## 5. Unattended upgrades and fail2ban

```bash
sudo apt-get update
sudo apt-get install -y unattended-upgrades fail2ban
sudo dpkg-reconfigure -plow unattended-upgrades   # answer Yes
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
sudo timedatectl set-timezone UTC
```

Expected: `fail2ban-client status sshd` shows the jail active;
`timedatectl` reports `Time zone: UTC`.

---

## 6. Docker from the official repository

Ubuntu's own `docker.io` package lags badly. Use Docker's repo:

```bash
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Verify as **deploy** (proves the group membership from §3 took effect —
log out and back in first):

```bash
docker version --format '{{.Server.Version}}'
docker compose version
```

Also cap Docker's own logs, because the 60 GB disk is shared with
Postgres:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
sudo systemctl restart docker
```

---

## 7. The application directory and its config

```bash
sudo install -d -o deploy -g deploy /opt/documind
sudo -u deploy git clone https://github.com/ahmad-7c/Multi_tenant_RAG.git /opt/documind
cd /opt/documind
```

### DuckDNS hostname

Register a subdomain at duckdns.org, point it at the static IP, then:

```bash
export API_HOSTNAME=yourname.duckdns.org   # used by deploy/Caddyfile
```

Put it in `.env.production` (next step) so it survives a reboot.

### `.env.production` — never committed, mode 600

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

Generate the three secrets and paste them in. **Production values must
differ from your development ones.**

```bash
openssl rand -hex 32   # JWT_SECRET_KEY
openssl rand -hex 24   # POSTGRES_PASSWORD
openssl rand -hex 24   # REDIS_PASSWORD
```

Then set, by hand: `API_HOSTNAME`, `ACME_EMAIL`, `GEMINI_API_KEY`,
`RERANKER_API_KEY` (your Cohere key), and a non-zero
`BUDGET_DAILY_GLOBAL_UNITS` **or** `BUDGET_DAILY_ORG_UNITS`.

The startup gate **refuses to boot** if the signing key is a known
placeholder or under 32 characters, if the DSN carries development
credentials, if `RERANKER_PROVIDER` is `passthrough`, if a provider key
is missing, if both budgets are 0, if `CELERY_TASK_ALWAYS_EAGER` is
set, if rate limiting is off, or if CORS allows a loopback or non-HTTPS
origin. That is the gate working — read the list it prints and fix each
line.

Verify the config without starting anything:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production config >/dev/null \
  && echo "compose config OK"
```

---

## 8. First deploy, by hand (P5)

Do this once before letting CI/CD do it, so you have seen every step.

```bash
cd /opt/documind
export API_IMAGE=documind-api:local
docker compose -f docker-compose.prod.yml --env-file .env.production build
docker compose -f docker-compose.prod.yml --env-file .env.production run --rm migrate
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
docker compose -f docker-compose.prod.yml --env-file .env.production ps
```

Then run the local verification suite, which proves the D1 properties
(ports not published, non-root uploads volume, forged header ignored):

```bash
bash scripts/verify_d1.sh
```

Create the **smoke account** once, so `scripts/smoke_test.sh` has
something to sign in as. Register through the API, then record the
three values as GitHub secrets in §9:

```bash
curl -fsS -X POST "https://$API_HOSTNAME/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d '{"org_name":"smoke-checks","email":"smoke@yourdomain.com","password":"<a generated password>"}'
```

Finally:

```bash
SMOKE_BASE_URL="https://$API_HOSTNAME" \
SMOKE_ORG=smoke-checks \
SMOKE_EMAIL=smoke@yourdomain.com \
SMOKE_PASSWORD='<the same password>' \
bash scripts/smoke_test.sh
```

Expected: 6 passed, 0 failed.

> **Never tear this stack down with the volume-removing flag.** It
> destroys the named volumes, which here are every uploaded document
> and the entire database. `scripts/check_no_destructive_commands.sh`
> fails CI if that command appears anywhere in the repo.

---

## 9. GitHub secrets, for CI/CD

Repository → Settings → Secrets and variables → Actions. Create a
**`production` environment** first (Settings → Environments), so you can
add required reviewers later without editing `cd.yml`.

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | the static IP, or your DuckDNS hostname |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | contents of `~/.ssh/documind_deploy` (the **private** half) |
| `DEPLOY_KNOWN_HOSTS` | output of `ssh-keyscan -H <STATIC_IP>` |
| `SMOKE_ORG`, `SMOKE_EMAIL`, `SMOKE_PASSWORD` | from §8 |

`GITHUB_TOKEN` is provided automatically and is what pushes to GHCR —
you do not create it.

`DEPLOY_KNOWN_HOSTS` is not optional. Without it the deploy step would
need `StrictHostKeyChecking=no`, which accepts any host key presented
and makes the deploy MITM-able.

```bash
ssh-keyscan -H <STATIC_IP>     # run on your laptop, paste the output
```

### Restrict the deploy key (recommended)

Limit what that key can do, so a stolen key cannot become a shell:

```bash
# in /home/deploy/.ssh/authorized_keys, prefix the deploy key line:
command="cd /opt/documind && ./scripts/deploy.sh $SSH_ORIGINAL_COMMAND",no-agent-forwarding,no-port-forwarding,no-pty ssh-ed25519 AAAA...
```

If you do this, simplify the `ssh` line in `cd.yml` to pass only the
image and SHA as the original command. Verify it before relying on it:
`ssh -i ~/.ssh/documind_deploy deploy@<IP> 'whoami'` should now fail.

---

## 10. Let CI/CD take over

Push to `main`. Then:

1. **CI** runs: ruff, the full suite *inside the image* on Python 3.14,
   the frontend lint/types/build, a dependency audit, the secret scan,
   and the repo guards.
2. **CD** runs only if CI succeeded for that exact commit. It builds and
   pushes `ghcr.io/ahmad-7c/multi_tenant_rag:<sha>`, then over SSH runs
   `deploy.sh`, which pulls by SHA, runs migrations as a separate job,
   starts the stack, waits for the container healthcheck
   (`/health/ready`, which round-trips `SELECT 1`), runs the smoke test,
   and **rolls back to the previous SHA if either fails**.

### Prove the rollback (D5 requires this)

Do not trust it until you have watched it work. Deliberately ship a
broken revision — the cheapest honest break is a failing readiness
probe, because it exercises the exact path the rollback hangs off:

```bash
git switch -c prove-rollback
# in app/main.py, make the readiness probe fail:
#   db.execute(sql_text("SELECT 1"))  ->  raise RuntimeError("deliberate")
git commit -am "prove the rollback fires"
git switch main && git merge prove-rollback && git push
```

Expected: CD's deploy step fails, the log shows `ROLLING BACK to <sha>`,
and `cat /opt/documind/.deployed_sha` still names the **previous**
revision. Then revert.

---

## Not covered here

- **Backups (P6).** Nightly `pg_dump`, 7-day retention, an off-box copy,
  and a restore drill. A backup that was never restored is not a backup.
- **Observability (P8).** Structured JSON logs with a request id, an
  external uptime monitor on `/health`, an AWS Budgets alert.
- **Vercel hand-off (P9)** and the outside verification list **(P10)**.
- The **BFF client-IP problem** — see `docs/adr/0006`. Through a BFF,
  Caddy sees the BFF host, so the per-IP limiter becomes
  per-deployment. Recorded, not solved.
