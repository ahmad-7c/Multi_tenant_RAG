# DocuMind deployment runbook (Lightsail)

You run every command here. I cannot reach the server and will not SSH
anywhere. Paste the output of anything that looks wrong.

Order matters: **§1–§6 must be done once before CI/CD can deploy
anything.** The pipeline in `.github/workflows/cd.yml` assumes a server
that already exists, with Docker installed, the repo at
`/opt/documind`, and `.env.production` in place.

**§7 and §8 are the hardening sections and come last on purpose.** SSH
config, `ufw` and `fail2ban` can each lock you out of the box, and you do
not want to be debugging a firewall and a half-installed Docker at the
same time. Get the stack running first, then harden.

> **Keep a second SSH session open** for the whole of §7 and §8. Every
> step in those two sections can lock you out, and an open session is the
> only cheap way back in.

---

## 1. Create the instance

AWS Lightsail → Create instance:

- **Region** `ap-southeast-2` (Sydney). **Fixed by this AWS account and
  not changeable** — the console reports the account region as fixed, so
  this is a constraint, not a choice. Gemini and Cohere are US-hosted, so
  every embedding, rerank and model call pays a trans-Pacific round trip;
  that latency is accepted. Mitigation, if your Vercel plan allows a
  function region: place the BFF near Sydney (`syd1`), so the BFF→API hop
  stays local and only the provider calls cross the ocean. Recorded in
  `docs/adr/0007`.
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

   Delete anything else. This mirrors the `ufw` rules in §7; both exist
   because either one alone is a single point of failure.

Expected: the instance shows *Running* with a static IP attached.

**Stay on 24.04 LTS.** Do not run `do-release-upgrade`. A release upgrade
mid-build changes the Docker repo codename, the Python in the base image
and the kernel, all at once, on the only box there is.

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
`/swapfile`. Check `/etc/fstab` holds exactly **one** `/swapfile` line —
a duplicate makes the next boot fail its `swap.target`.

---

## 3. Docker, from Docker's official apt repository

Ubuntu's own `docker.io` package lags badly and does **not** ship Compose
v2, so `docker compose` — the subcommand every file in this repo uses —
would not exist. Use Docker's repository and the
`docker-compose-plugin` package.

Run everything in this section as the **`ubuntu`** user over SSH. One
command per block, in order; compare each output before moving on.

> **Do not add any user to the `docker` group here.** Membership of that
> group is equivalent to root on this box, so it is granted once, to one
> account, in **§4 (A non-root deploy user)** — which is also where you
> log out and back in so it takes effect. Every `docker` command in this
> section therefore uses `sudo`, deliberately.
>
> Nothing here touches SSH config, `ufw` or `fail2ban`. Those are §7 and
> §8.

### 3.1 Install the two things the repo setup needs

```bash
sudo apt-get install -y ca-certificates curl
```

`ca-certificates` is what lets `curl` verify Docker's TLS certificate;
without it the key download below either fails or, worse, succeeds
unverified.

Expected — on a freshly upgraded 24.04 both are already present:

```
Reading package lists... Done
Building dependency tree... Done
Reading state information... Done
ca-certificates is already the newest version (20240203).
curl is already the newest version (8.5.0-2ubuntu10.6).
0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
```

Version numbers will differ. If either was missing you will see
`Setting up ...` lines instead; that is equally fine.

### 3.2 Create the keyring directory

```bash
sudo install -m 0755 -d /etc/apt/keyrings
```

This is where apt looks for repository signing keys. `0755` so apt, which
does not run as root for the download step, can read it.

Expected: **no output at all.** Unix tools print nothing on success.

### 3.3 Download Docker's signing key

```bash
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
```

This key is what apt uses to prove the packages came from Docker. `-f`
makes curl fail on an HTTP error instead of writing the error page to the
file, which is the failure mode that produces a confusing apt error ten
minutes later.

Expected: **no output.** Then check you got a key and not a web page:

```bash
ls -l /etc/apt/keyrings/docker.asc
```

Expected — roughly 3–4 KB, owned by root:

```
-rw-r--r-- 1 root root 3337 Nov  7 09:14 /etc/apt/keyrings/docker.asc
```

The date and the exact byte count will differ. **If the size is only a
few hundred bytes**, you downloaded something that is not a key:
`sudo rm /etc/apt/keyrings/docker.asc` and repeat 3.3.

### 3.4 Make the key readable

```bash
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

`curl` run under `sudo` can create the file mode 600; apt then cannot
read it and reports the repository as unsigned. This line is cheap
insurance, and harmless if the mode was already correct.

Expected: **no output.**

### 3.5 Add the repository

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
```

The two `$( )` parts fill in your architecture and your Ubuntu codename,
so the line is correct for this box rather than copied from a blog post.
`signed-by=` ties the repository to that one key, so this key cannot sign
for any other repo.

Expected: **no output** (`>/dev/null` swallows what `tee` would echo).
Now read the file back — this is the step most worth checking:

```bash
cat /etc/apt/sources.list.d/docker.list
```

Expected — exactly one line:

```
deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable
```

`amd64` is the Lightsail 2 GB plan's architecture and `noble` is Ubuntu
24.04's codename. **If you see `$(dpkg ...)` or an empty codename**, the
substitution did not run (usually from pasting into a shell that is not
bash): `sudo rm /etc/apt/sources.list.d/docker.list` and repeat 3.5.

### 3.6 Refresh the package lists

```bash
sudo apt-get update
```

Until this runs, apt has never heard of the packages in 3.7.

Expected: among the lines, Docker's repository appears and the run ends
cleanly:

```
Get:5 https://download.docker.com/linux/ubuntu noble InRelease [48.8 kB]
Get:6 https://download.docker.com/linux/ubuntu noble/stable amd64 Packages [36.6 kB]
...
Reading package lists... Done
```

There must be **no `NO_PUBKEY`** (the key in 3.3/3.4 is wrong or
unreadable) and **no `404`** (the codename in 3.5 is wrong).

### 3.7 Install Docker and the Compose plugin

```bash
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

`docker-ce` is the daemon, `docker-ce-cli` the `docker` command,
`containerd.io` the runtime underneath, `docker-buildx-plugin` the
builder used by `docker compose build`, and `docker-compose-plugin` the
`docker compose` subcommand this repo's deploy script calls.

Expected: a long log — roughly 120 MB downloaded, one to two minutes —
ending with lines like:

```
Setting up docker-ce (5:28.3.3-1~ubuntu.24.04~noble) ...
Created symlink /etc/systemd/system/multi-user.target.wants/docker.service → /usr/lib/systemd/system/docker.service.
Created symlink /etc/systemd/system/sockets.target.wants/docker.socket → /usr/lib/systemd/system/docker.socket.
Processing triggers for man-db (2.12.0-4build2) ...
```

### 3.8 Make sure it starts at boot

```bash
sudo systemctl enable --now docker
```

The package already enabled it; running this makes it explicit, and it is
safe to repeat. A Lightsail instance will be rebooted, and the whole
stack coming back by itself depends on this.

Expected: **no output** (if it was not yet enabled you will see one
`Created symlink` line).

### 3.9 Is the daemon actually running

```bash
systemctl is-active docker
```

Expected — exactly one word:

```
active
```

Anything else (`failed`, `inactive`): run
`sudo journalctl -u docker -n 30 --no-pager` and paste the output.

### 3.10 Check the Docker version

```bash
sudo docker --version
```

Expected — the exact numbers will differ, 28.x or newer is right for
24.04 today:

```
Docker version 28.3.3, build 980b856
```

### 3.11 Check the Compose version

```bash
sudo docker compose version
```

Expected:

```
Docker Compose version v2.39.1
```

Note the **space**: `docker compose`, not `docker-compose`. The hyphen is
Compose v1, which is end-of-life and which this repo never uses. If this
prints `'compose' is not a docker command`, then
`docker-compose-plugin` did not install — go back to 3.7.

### 3.12 Run hello-world

```bash
sudo docker run --rm hello-world
```

This is the end-to-end check: it pulls an image from Docker Hub and runs
a container. `--rm` deletes the container when it exits, so this leaves
nothing behind but the image.

Expected:

```
Unable to find image 'hello-world:latest' locally
latest: Pulling from library/hello-world
17eec7bbc9d7: Pull complete
Digest: sha256:...
Status: Downloaded newer image for hello-world:latest

Hello from Docker!
This message shows that your installation appears to be working correctly.
...
```

Four things just passed at once: the daemon is reachable, outbound 443
works through the Lightsail firewall, an image can be pulled, and a
container can run. `Unable to find image ... locally` is **normal** — it
is the line before the pull, not an error.

### 3.13 Cap container logs

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
```

By default `json-file` logs grow without any bound. The 60 GB disk is
shared with Postgres and every uploaded document, and one chatty
container filling it takes the database down with it. This caps each
container at 3 files of 10 MB.

Expected: **no output.** Read it back before restarting anything — a
malformed `daemon.json` makes the daemon refuse to start:

```bash
cat /etc/docker/daemon.json
```

Expected — exactly the one line you wrote:

```
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
```

### 3.14 Restart Docker to pick up the config

```bash
sudo systemctl restart docker
```

`daemon.json` is read only at daemon start.

Expected: **no output.** Then confirm it came back:

```bash
systemctl is-active docker
```

Expected:

```
active
```

**If this says `failed`**, the JSON is the cause nine times in ten. Run
`sudo journalctl -u docker -n 20 --no-pager`, compare the file against
3.13 character for character, fix it, and restart again.

### 3.15 Confirm the logging driver

```bash
sudo docker info --format '{{.LoggingDriver}}'
```

Expected:

```
json-file
```

Honest limit: `docker info` reports the driver but **not** `max-size` /
`max-file`. Those are visible per container, so the real confirmation
comes at the first deploy in §6:

```bash
# in §6, once containers are up:
sudo docker inspect documind-api --format '{{json .HostConfig.LogConfig}}'
```

### 3.16 Delete the hello-world image

```bash
sudo docker image rm hello-world
```

It has served its purpose and the disk is shared with Postgres.

Expected:

```
Untagged: hello-world:latest
Untagged: hello-world@sha256:...
Deleted: sha256:...
```

**Docker is done.** Next: §4 creates the `deploy` user and adds it to the
`docker` group.

---

## 4. A non-root deploy user

CI/CD needs an account on this box. It must not be `root`, and it must
not be your own `ubuntu` login — a key that GitHub holds should reach
exactly as far as a deploy needs and no further.

> **Keep your current `ubuntu` SSH session open for this whole section.**
> You will open a *second* window to test the new account. If the new key
> does not work, the still-open `ubuntu` session is how you fix it.

Steps marked **[server]** run in your `ubuntu` SSH session. Steps marked
**[Windows]** run in **cmd.exe** on your own machine (the prompt that
looks like `C:\Users\SiliCon>`). These are not interchangeable.

Nothing in this section touches SSH config, `ufw` or `fail2ban` — those
are §7 and §8, after the app is running.

### 4.1 [server] Create the account

```bash
sudo adduser --disabled-password --gecos "" deploy
```

`--disabled-password` means no password is ever set, so this account
cannot be logged into by password at all — only by SSH key.
`--gecos ""` skips the interactive "Full Name / Room Number" questions.

Expected — exact wording varies by Ubuntu version, the UID may differ:

```
info: Adding user `deploy' ...
info: Selecting UID/GID from range 1000 to 59999 ...
info: Adding new group `deploy' (1001) ...
info: Adding new user `deploy' (1001) with group `deploy (1001)' ...
info: Creating home directory `/home/deploy' ...
info: Copying files from `/etc/skel' ...
info: Adding new user `deploy' to supplemental / extra groups `users' ...
```

### 4.2 [server] Confirm the password really is disabled

```bash
sudo passwd -S deploy
```

`-S` reports the password status instead of changing it.

Expected — the second field must be **`L`**, for locked:

```
deploy L 11/07/2026 0 99999 7 -1
```

If that field is `P` (a password is set) or `NP` (no password *required*,
which would let anyone in), stop — `NP` in particular is a hole. Run
`sudo passwd -l deploy` to lock it and check again.

### 4.3 [server] Put `deploy` in the `docker` group

```bash
sudo usermod -aG docker deploy
```

`-aG` **a**ppends to a **G**roup without removing the others. Leaving out
the `a` replaces every group the user has, which is a classic way to
break an account.

Expected: **no output.**

**Read this before moving on.** Membership of the `docker` group is
**equivalent to root on this machine.** It is not a reduced form of root.
Anyone who can talk to the Docker socket can start a container that mounts
the host's whole filesystem and edit any file as root — no password, no
`sudo`, no log entry that looks like privilege escalation.

Why that is acceptable *for this one account*: deploying is, literally,
"start and stop containers". An account that cannot do that cannot do its
job, so there is no version of this account that is both useful and
unprivileged. What we control instead is how many accounts have it (one),
how it is reached (one SSH key, no password), and what else that key can
do (nothing — see 4.4 and 4.10).

### 4.4 [server] Confirm `deploy` has **no** sudo

```bash
groups deploy
```

Expected — `docker` is present, and `sudo` and `admin` are **not**:

```
deploy : deploy users docker
```

We deliberately do not grant `sudo`. Given 4.3 that is not a dramatic
reduction in power today — but it keeps root reachable by exactly **one**
route instead of two. The day this moves to rootless Docker or a socket
proxy, removing one group makes the account genuinely unprivileged. If
`sudo` were also granted, that change would quietly achieve nothing.

### 4.5 [server] Create the `.ssh` directory

```bash
sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
```

`install -d` creates the directory and sets mode and owner in one step, so
it never briefly exists with the wrong permissions. `700` means only
`deploy` can read it.

Expected: **no output.** Check it:

```bash
ls -ld /home/deploy/.ssh
```

Expected — `drwx------`, owned by `deploy deploy`:

```
drwx------ 2 deploy deploy 4096 Nov  7 10:22 /home/deploy/.ssh
```

The permissions are not cosmetic: `sshd` refuses to read `authorized_keys`
out of a directory that others can write, and it does so **silently** —
from your side it just says `Permission denied (publickey)`.

### 4.6 [Windows] Make sure your `.ssh` folder exists

```
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
```

`ssh-keygen` will not create a missing folder; it fails with
`No such file or directory`.

Expected: **no output** (whether it existed or was just created).

### 4.7 [Windows] Generate the deploy key

Generate it **here, not on the server.** A private key should never exist
on the machine it grants access to — if the box is compromised, the key to
the box should not be sitting on it.

```
ssh-keygen -t ed25519 -C "documind-deploy" -f "%USERPROFILE%\.ssh\documind_deploy" -N ""
```

`-t ed25519` is the modern key type, `-C` is just a label, `-f` is where
to save, and **`-N ""` sets an empty passphrase.**

**The empty passphrase is required, not laziness.** GitHub Actions hands
this key to `ssh` with no terminal attached and no way to type anything,
so a passphrase-protected key makes the deploy hang and then fail. The
key's protection comes from being a GitHub *environment* secret (§9) and
from the restrictions in 4.10 — not from a passphrase.

**In cmd, write `-N ""` exactly.** Do **not** write `-N '""'`. cmd does
not treat single quotes as quoting, so that form sets a real two-character
passphrase and you will not find out until the first deploy fails.

Expected:

```
Generating public/private ed25519 key pair.
Your identification has been saved as C:\Users\SiliCon\.ssh\documind_deploy
Your public key has been saved as C:\Users\SiliCon\.ssh\documind_deploy.pub
The key fingerprint is:
SHA256:9f3c... documind-deploy
The key's randomart image is:
+--[ED25519 256]--+
|      .o+        |
...
+----[SHA256]-----+
```

If it asks `Overwrite (y/n)?` a key of that name already exists. Answer
`n` and come ask, rather than overwriting something in use.

### 4.8 [Windows] Prove the passphrase is actually empty

```
ssh-keygen -y -P "" -f "%USERPROFILE%\.ssh\documind_deploy"
```

`-y` reads the private key and prints its public half; `-P ""` says "try
the empty passphrase". If the key has no passphrase this succeeds. This is
the only way to find out *now* rather than at the first deploy.

Expected — one line, starting `ssh-ed25519`:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHk3...
```

**If you instead see** `Load key ... incorrect passphrase supplied to
decrypt private key`, the key has a passphrase. Delete both files and
redo 4.7, watching the quoting:

```
del "%USERPROFILE%\.ssh\documind_deploy" "%USERPROFILE%\.ssh\documind_deploy.pub"
```

### 4.9 [Windows] Print the public key

```
type "%USERPROFILE%\.ssh\documind_deploy.pub"
```

`type` is cmd's "print a file". This is the **public** half — the one that
is safe to copy around.

Expected — one long line:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHk3... documind-deploy
```

Select it and copy it. You need it in the next step. **Never copy the
other file** (the one without `.pub`) anywhere except the GitHub secret
box in §9.

### 4.10 [server] Install the key, with restrictions

You are going to write one line made of two parts: a list of restrictions,
a space, then the key you just copied. It looks like this:

```
no-agent-forwarding,no-port-forwarding,no-X11-forwarding ssh-ed25519 AAAAC3Nza... documind-deploy
```

What each restriction does:

- `no-agent-forwarding` — stops this session reaching back through the
  connection to use *other* keys loaded on the machine that connected. A
  compromised server could otherwise hop onward using your personal keys.
- `no-port-forwarding` — stops the key being used to tunnel traffic
  through the box, e.g. reaching Postgres from the internet, or using the
  server as a relay to somewhere else.
- `no-X11-forwarding` — stops graphical forwarding. There is no GUI here,
  so it is pure attack surface with no use.

Two restrictions are deliberately **absent**:

- **No `from="1.2.3.4"` IP restriction.** GitHub Actions runners come from
  a large, changing pool of addresses, so pinning one would break the
  deploy at an unpredictable moment.
- **No `command=` or `no-pty` yet.** You need a real interactive shell for
  the test in 4.12 and for the by-hand first deploy in §6. §9 covers
  locking the key to a single command once the deploy has worked.

Now run this. **The cursor will sit on a blank line with no prompt — that
is the command waiting for input, not a freeze:**

```bash
sudo -u deploy tee /home/deploy/.ssh/authorized_keys >/dev/null
```

Then:

1. Type (or paste) `no-agent-forwarding,no-port-forwarding,no-X11-forwarding`
   followed by **one space**.
2. Paste the public key line from 4.9 straight after it, on the **same
   line**.
3. Press **Enter**.
4. Press **Ctrl+D** to finish.

`sudo -u deploy` makes the file owned by `deploy` from the moment it is
created. Plain `tee` (not `tee -a`) overwrites, so if you have to redo
this step you get one key, not two broken copies.

Expected: you see what you typed echoed back, then your normal prompt
returns after Ctrl+D.

### 4.11 [server] Lock down and check the file

```bash
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

Only `deploy` may read or write it. `sshd` ignores the file otherwise —
again, silently.

Expected: **no output.** Now check all three properties at once:

```bash
ls -l /home/deploy/.ssh/authorized_keys
```

Expected — `-rw-------`, owner `deploy deploy`, size a few hundred bytes:

```
-rw------- 1 deploy deploy 148 Nov  7 10:25 /home/deploy/.ssh/authorized_keys
```

Then confirm the paste did not break into multiple lines:

```bash
sudo wc -l /home/deploy/.ssh/authorized_keys
```

Expected — exactly **1**:

```
1 /home/deploy/.ssh/authorized_keys
```

If it says `2` or more, the key got split across lines and will not work —
redo 4.10. If it says `0`, the final Enter was missed; redo 4.10.

### 4.12 [Windows] Test the new account — before changing anything else

**Leave your `ubuntu` session open. Open a SECOND cmd window.**

```
ssh -i "%USERPROFILE%\.ssh\documind_deploy" deploy@documind-prod.duckdns.org
```

Expected: you land on a prompt like `deploy@ip-172-26-x-x:~$`.

If it asks `Are you sure you want to continue connecting (yes/no/...)?`,
that is because you previously connected by IP and this is the first time
by hostname. Compare the fingerprint with the one you saw before, then
type `yes`.

If it says `Permission denied (publickey)`, do **not** retry blindly — go
back to 4.5, 4.10 and 4.11 in your still-open `ubuntu` session.

Now run three checks **in the new `deploy` session**.

**Check 1 — who am I:**

```bash
whoami
```

Expected:

```
deploy
```

**Check 2 — Docker works without `sudo`:**

```bash
docker ps
```

Expected — the header row and nothing under it, since nothing is running
yet:

```
CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
```

If you get `permission denied while trying to connect to the Docker daemon
socket`, the group from 4.3 has not taken effect — type `exit` and
reconnect, because group membership is applied at login.

**Check 3 — `sudo` must FAIL:**

```bash
sudo -n true; echo "exit=$?"
```

`-n` means "never prompt"; `true` is a command that does nothing. We are
checking that this account *cannot* escalate.

Expected — an error, and a **non-zero** exit code:

```
sudo: a password is required
exit=1
```

(Some versions word it `sudo: no password was provided`. Either is fine.)

**If it prints `exit=0` with no error, `deploy` has passwordless sudo and
that is wrong.** Stop and fix it before going further.

Then leave:

```bash
exit
```

> Rollback: everything in this section is reversible from your open
> `ubuntu` session. `sudo deluser --remove-home deploy` undoes 4.1–4.11
> entirely, and you can start again.

### 4.13 [server] Create the application directory

```bash
sudo install -d -o deploy -g deploy -m 755 /opt/documind
```

This is where the repo lives. It is owned by `deploy` because the clone,
and every deploy after it, runs as `deploy` with no `sudo` — the account
must own the directory outright. `755` lets other users read it but only
`deploy` write.

Expected: **no output.** Check it:

```bash
ls -ld /opt/documind
```

Expected:

```
drwxr-xr-x 2 deploy deploy 4096 Nov  7 10:30 /opt/documind
```

### 4.14 [server] Capture the host fingerprint for CI/CD

CI/CD needs the server's host key so the deploy can verify it is talking
to *your* server rather than accepting whatever answers. The alternative,
`StrictHostKeyChecking=no`, accepts any key presented and makes the deploy
interceptable.

**Do not use `ssh-keyscan` from Windows here.** The OpenSSH that ships
with Windows is older than Ubuntu 24.04's and does not implement the
post-quantum key exchange the server offers, so it fails with
`unsupported KEX method sntrup761x25519-sha512`. Read the key off the
server instead — which is also the better source: `ssh-keyscan` trusts
whatever answers on the network, while an already-authenticated SSH
session does not.

In your `ubuntu` session:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

`-lf` prints the fingerprint of a key file. This is the server's own copy
of its public host key, read locally.

Expected — the fingerprint must match what your SSH client showed you on
first connect (`SHA256:2PoEb6wj...` for this server):

```
256 SHA256:2PoEb6wj... root@ip-172-26-x-x (ED25519)
```

**If it does not match, stop.** A mismatch means either you are not on the
machine you think you are, or something answered for it earlier. Do not
proceed and do not paste the value anywhere.

Now print the `known_hosts` line CI/CD needs:

```bash
awk '{print "documind-prod.duckdns.org " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

This glues the hostname onto the key type and the key itself, which is
exactly the `known_hosts` format. `ssh-keyscan -H` would have produced a
*hashed* hostname; a plain one works identically and can be checked by eye.

Expected — one line:

```
documind-prod.duckdns.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...
```

Copy that line into a scratch file on your laptop for now; it becomes
`DEPLOY_KNOWN_HOSTS` in §9. It is a **public** key — not a secret — but it
still has to be the right one.

> **Use the same hostname in both places.** `DEPLOY_HOST` below and the
> hostname in this line must name the server identically. Put the IP in one
> and the hostname in the other and verification fails, with an error that
> reads like a security alert.
>
> The ed25519 key alone is enough. Modern OpenSSH prefers it, and listing
> the RSA and ECDSA keys as well just adds entries you would also have to
> keep correct.

### What you now have for §9

Four of the seven CI/CD values exist after this section:

| Secret | Value |
|---|---|
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_HOST` | `documind-prod.duckdns.org` |
| `DEPLOY_SSH_KEY` | the whole of `%USERPROFILE%\.ssh\documind_deploy` — the file **without** `.pub`, including the `-----BEGIN` and `-----END` lines |
| `DEPLOY_KNOWN_HOSTS` | the one line printed by 4.14 |

The remaining three (`SMOKE_ORG`, `SMOKE_EMAIL`, `SMOKE_PASSWORD`) come
from §6.

**Do not put anything into GitHub yet.** §9 is where they are added, as
*environment* secrets on a `production` environment — and adding the
deploy key before the deploy works just means a live credential sitting
there longer than it has to.

---

## 5. The application directory and its config

Two jobs: get the code onto the server, and create the one file that
holds the production secrets. **Nothing starts in this section** — no
`docker compose up`. That is §6.

Every server command here runs as **`deploy`**, which has no `sudo` and
needs none: `/opt/documind` is already owned by `deploy` from §4.13. If
you find yourself reaching for `sudo`, something is wrong — stop and
check the ownership rather than working around it.

> ### The secrets rule for this section
>
> **Nothing from this section gets pasted into a chat window, a ticket, an
> email, or a commit.** Not to me, not to anyone.
>
> Three of the seven values are generated *on the server* and never
> appear on your screen at all. Two you paste from your provider
> dashboards into an editor on the server — not onto a command line,
> because a command line is recorded in your shell history. Two are not
> secret (a hostname and your email).
>
> Every check in 5.11 is designed to confirm the file is complete
> **without printing a single value.**

### 5.1 Log in as `deploy`

In cmd.exe:

```
ssh -i "%USERPROFILE%\.ssh\documind_deploy" deploy@documind-prod.duckdns.org
```

Expected: a prompt like `deploy@ip-172-26-x-x:~$`.

Everything from 5.2 to 5.13 runs in this session.

### 5.2 Find out whether the repository is public or private

This decides how the code gets onto the server, and the two answers need
different setups. Do not guess — a wrong guess either hangs on a password
prompt or has you building a credential you did not need.

```bash
GIT_TERMINAL_PROMPT=0 git ls-remote https://github.com/ahmad-7c/Multi_tenant_RAG.git HEAD
```

`git ls-remote` asks "what is at the other end" without downloading
anything. `GIT_TERMINAL_PROMPT=0` stops git from prompting for a username,
so instead of hanging it fails with a message you can read.

**If the repository is PUBLIC**, expected — a commit hash and `HEAD`:

```
7ed43ae8f6c2b1d4e9a3f5c7d8b2a1e4f6c9d3b5	HEAD
```

→ go to **5.3**, skip 5.4.

**If the repository is PRIVATE**, expected:

```
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

→ skip 5.3, go to **5.4**.

(`repository not found` also means private — GitHub deliberately returns
the same answer for "private" and "does not exist", so an outsider cannot
confirm a repo exists.)

### 5.3 PUBLIC repository — clone over HTTPS

```bash
git clone https://github.com/ahmad-7c/Multi_tenant_RAG.git /opt/documind
```

Anonymous HTTPS read needs no credential at all, which is the safest
option available: there is nothing on the server to steal and nothing to
rotate. Cloning *as `deploy`* (which you are, because you logged in as
`deploy`) means the files are owned by the account that will later run
`git pull` — a root-owned repo in a `deploy`-owned directory breaks the
first automated deploy.

Expected:

```
Cloning into '/opt/documind'...
remote: Enumerating objects: 1847, done.
remote: Counting objects: 100% (1847/1847), done.
remote: Compressing objects: 100% (892/892), done.
Receiving objects: 100% (1847/1847), 1.24 MiB | 3.10 MiB/s, done.
Resolving deltas: 100% (1003/1003), done.
```

Object counts will differ. Cloning into an existing **empty** directory is
fine; that is why §4.13 only created it.

Then go to **5.5**.

> A public repository means the *code* is readable by anyone — which is
> already true and is not a problem, because no secret lives in it. That
> is precisely why `.env.production` is created here on the server and
> never committed (5.12).

### 5.4 PRIVATE repository — a read-only deploy key

Skip this if 5.2 said public.

A private repo needs a credential on the server. Use a **deploy key**: an
SSH key tied to **one repository**, which you add with write access
switched **off**.

Why not a personal access token: a classic token with the `repo` scope
grants read **and write** to **every** repository your account can reach,
so a token stolen off this box is a route into all of your code. Even a
fine-grained token expires and has to be rotated on a schedule. A
read-only deploy key can only pull one repo, and revoking it is one click
that affects nothing else.

**This key is generated ON THE SERVER**, which is the opposite of §4 —
and for the same reason. The private half belongs on whichever machine is
the *client*. In §4 your laptop connected to the server, so the key lived
on your laptop. Here the server connects to GitHub, so it lives on the
server.

```bash
ssh-keygen -t ed25519 -C "documind-server-readonly" -f /home/deploy/.ssh/github_readonly -N ""
```

Empty passphrase, because `git pull` runs unattended from CI/CD.

Expected: the same `Generating public/private ed25519 key pair.` output as
§4.7, saving to `/home/deploy/.ssh/github_readonly`.

Print the public half:

```bash
cat /home/deploy/.ssh/github_readonly.pub
```

Expected — one line ending `documind-server-readonly`:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG4k... documind-server-readonly
```

Now in your browser: the repository → **Settings** → **Deploy keys** →
**Add deploy key**. Title `documind-prod server (read-only)`, paste that
line, and **leave "Allow write access" UNCHECKED**. Checked, a compromised
server could rewrite your repository — including the CI/CD workflow files
that deploy to it.

Teach the server GitHub's host key before cloning:

```bash
ssh-keyscan github.com >> /home/deploy/.ssh/known_hosts 2>/dev/null
```

Then verify it, rather than trusting whatever answered:

```bash
ssh-keygen -lf /home/deploy/.ssh/known_hosts
```

Expected: a line per key. **Compare the `ssh-ed25519` fingerprint against
the list GitHub publishes** at
`https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints`.
If it does not match what GitHub publishes, delete the file and stop.

Test the key:

```bash
ssh -i /home/deploy/.ssh/github_readonly -T git@github.com
```

Expected — a greeting, and `exit code 1` is normal here because GitHub
does not offer a shell:

```
Hi ahmad-7c/Multi_tenant_RAG! You've successfully authenticated, but GitHub does not provide shell access.
```

Clone over SSH (note the `git@github.com:` form, not `https://`):

```bash
GIT_SSH_COMMAND="ssh -i /home/deploy/.ssh/github_readonly" git clone git@github.com:ahmad-7c/Multi_tenant_RAG.git /opt/documind
```

Expected: the same `Cloning into '/opt/documind'...` output as 5.3.

Make the key permanent for this repo, so later pulls do not need that
prefix:

```bash
cd /opt/documind && git config core.sshCommand "ssh -i /home/deploy/.ssh/github_readonly"
```

Expected: **no output.** This is written to `.git/config`, which is local
to the server and never committed.

### 5.5 Confirm the clone

```bash
cd /opt/documind
```

```bash
git log -1 --oneline
```

Expected — the newest commit on `main`:

```
7ed43ae feat(phase-5): production image, prod compose, Caddy edge, and CI/CD
```

```bash
ls -ld /opt/documind/.git
```

Expected — owned by `deploy deploy`, not root:

```
drwxrwxr-x 8 deploy deploy 4096 Nov  7 11:02 /opt/documind/.git
```

If it says `root root`, the clone was run with `sudo`. Remove the
directory contents and redo 5.3 or 5.4 as `deploy`.

### 5.6 Confirm the hostname DNS already works

You set up DuckDNS earlier; this just confirms it still points here.

```bash
getent hosts documind-prod.duckdns.org
```

`getent hosts` resolves a name using the system resolver.

Expected — your Lightsail static IP, followed by the name:

```
13.211.x.x      documind-prod.duckdns.org
```

If this is empty, Caddy will fail to get a certificate in §6. Fix DuckDNS
before going on.

### 5.7 Create `.env.production`

```bash
cp /opt/documind/.env.production.example /opt/documind/.env.production
```

The example is committed and contains no real values — every secret in it
reads `<GENERATE>` or `<PASTE>`. You are about to replace those seven.

Expected: **no output.**

```bash
chmod 600 /opt/documind/.env.production
```

`600` means only the owner can read or write it. Every other account on
the box — and every other container — is excluded.

Expected: **no output.** Check it now, not later:

```bash
ls -l /opt/documind/.env.production
```

Expected — `-rw-------`, owned by `deploy deploy`:

```
-rw------- 1 deploy deploy 8421 Nov  7 11:05 /opt/documind/.env.production
```

### 5.8 The seven values, and where each comes from

| Name | Where it comes from |
|---|---|
| `JWT_SECRET_KEY` | generated on the server (5.9) |
| `POSTGRES_PASSWORD` | generated on the server (5.9) |
| `REDIS_PASSWORD` | generated on the server (5.9) |
| `API_HOSTNAME` | a known fact: your DuckDNS name (5.10) |
| `ACME_EMAIL` | your real email address (5.10) |
| `GEMINI_API_KEY` | **you supply** — Google AI Studio (5.11) |
| `RERANKER_API_KEY` | **you supply** — your Cohere dashboard (5.11) |

Everything else in the file already has a correct production value. You
are not editing anything outside this list.

**The two provider keys must be new production keys, not the ones in your
laptop's `.env`.** If a dev key leaks it should cost you a dev project,
not the live one — and you cannot revoke a shared key without taking both
down.

### 5.9 Generate the three secrets — straight into the file

Each command generates a random value and writes it in place. **The value
is never printed**, so it cannot end up in your scroll-back, and because
you type `$(openssl ...)` rather than the value itself, your shell history
records only the command.

```bash
sed -i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$(openssl rand -hex 32)|" /opt/documind/.env.production
```

32 random bytes as 64 hex characters. The startup gate refuses anything
under 32 characters, and this is the exact command its error message
recommends.

Expected: **no output.**

```bash
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" /opt/documind/.env.production
```

The database password *is* the database. 24 bytes as 48 hex characters.

Expected: **no output.**

```bash
sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$(openssl rand -hex 24)|" /opt/documind/.env.production
```

Redis holds the rate-limit counters, the budget counters and the answer
cache — which since B1 contains document text.

Expected: **no output.**

Why hex and not base64: hex is only `0-9a-f`, so it cannot contain a
character that breaks the `sed` command above, the database connection
string compose assembles, or a shell line. A base64 password containing
`/` or `+` breaks at least one of those, usually with an error that points
somewhere else entirely.

> Honest limit: for the instant each command runs, the generated value is
> visible to anything reading the process list on this machine. The only
> accounts on this box are yours, so that is acceptable here. It is the
> reason the *pasted* keys in 5.11 go through an editor instead.

### 5.10 Set the hostname and your email

```bash
sed -i "s|^API_HOSTNAME=.*|API_HOSTNAME=documind-prod.duckdns.org|" /opt/documind/.env.production
```

`deploy/Caddyfile` reads this to decide which hostname to request a
certificate for. Neither value here is a secret.

Expected: **no output.**

Now your email. **Replace `you@example.com` with a real address you can
read** before running it:

```bash
sed -i "s|^ACME_EMAIL=.*|ACME_EMAIL=you@example.com|" /opt/documind/.env.production
```

**This must be a real address.** Let us Encrypt registers the account
against it and sends certificate-expiry and revocation warnings there —
and left blank, Caddy refuses to start, so a missing value is a failed
deploy rather than a silent gap. A fake address boots but means the one
warning that matters arrives nowhere.

Expected: **no output.**

### 5.11 Paste the two provider keys — in an editor, not on a command line

**Do not use `sed` for these.** Typing a real API key on a command line
writes it into `~/.bash_history`, where it stays in plain text long after
you have forgotten it is there. An editor writes the file directly and
records nothing.

```bash
nano /opt/documind/.env.production
```

Inside nano:

1. **Ctrl+W**, type `GEMINI_API_KEY`, **Enter** — jumps to the line.
2. Replace `<PASTE>` with your Gemini key. Keep the `GEMINI_API_KEY=`
   part, add no spaces and no quotes.
3. **Ctrl+W**, type `RERANKER_API_KEY`, **Enter**.
4. Replace `<PASTE>` with your Cohere key.
5. **Ctrl+O**, **Enter** to save. **Ctrl+X** to exit.

While you are in there, confirm these four lines already read as follows —
they come from the example file and should need no change:

```
ENV=production
RERANKER_PROVIDER=hosted
RATE_LIMIT_ENABLED=true
BUDGET_DAILY_GLOBAL_UNITS=20000
```

Expected on exit: you are back at the shell prompt. If nano says
`[ Wrote 237 lines ]` at the bottom before you exit, the save worked.

### 5.12 Check the file is complete — without printing any value

Four checks. None of them displays a secret.

**Check 1 — no placeholder left anywhere:**

```bash
grep -n '<GENERATE>\|<PASTE>' /opt/documind/.env.production
```

Expected: **no output at all.** Every line it prints is a value you still
have to fill in, named with its line number.

**Check 2 — none of the seven is empty:**

```bash
grep -nE '^(JWT_SECRET_KEY|POSTGRES_PASSWORD|REDIS_PASSWORD|API_HOSTNAME|ACME_EMAIL|GEMINI_API_KEY|RERANKER_API_KEY)=$' /opt/documind/.env.production
```

The `=$` matches a line that ends immediately after the `=`. Expected:
**no output.**

(`CORS_ALLOW_ORIGINS=` *is* legitimately empty — the browser talks to the
BFF, not to this API — which is why it is not in that list.)

**Check 3 — all seven present, showing name and character count only:**

```bash
awk -F= '/^(JWT_SECRET_KEY|POSTGRES_PASSWORD|REDIS_PASSWORD|API_HOSTNAME|ACME_EMAIL|GEMINI_API_KEY|RERANKER_API_KEY)=/ {print $1, length(substr($0, index($0,"=")+1))}' /opt/documind/.env.production
```

This prints the name and how many characters the value has — never the
value. Expected — seven lines:

```
POSTGRES_PASSWORD 48
REDIS_PASSWORD 48
JWT_SECRET_KEY 64
API_HOSTNAME 25
ACME_EMAIL 19
GEMINI_API_KEY 53
RERANKER_API_KEY 40
```

The first four must be exactly 48, 48, 64 and 25. `ACME_EMAIL` is however
long your address is. The two provider keys are whatever length your
provider issues and the length is **not** a correctness check — Google has
shipped at least two formats (`AIza…` at 39 characters and the newer `AQ.`
at 53), and Cohere's are around 40. What matters is only that the number
is not 0 and not 7 (`<PASTE>` is 7 characters).

**Check 4 — permissions survived the editing:**

```bash
ls -l /opt/documind/.env.production
```

Expected — still `-rw-------` and still `deploy deploy`:

```
-rw------- 1 deploy deploy 8502 Nov  7 11:18 /opt/documind/.env.production
```

Some editors save by writing a new file; this confirms yours did not
widen the permissions. If the mode changed, run `chmod 600` again.

### 5.13 Prove the file can never be committed or baked into an image

Two separate leaks, two separate guards, and both are already in the
repository — this step is about *verifying* them, not adding them.

```bash
cd /opt/documind && git check-ignore -v .env.production
```

This asks git itself which rule ignores the file, which beats reading
`.gitignore` and hoping.

Expected — git names the file, line number and rule:

```
.gitignore:7:.env.*	.env.production
```

**If this prints nothing, the file is NOT ignored.** Stop and fix
`.gitignore` before you ever run `git add`.

```bash
git status --short
```

Expected: **no output** — a clean tree, with `.env.production` nowhere in
it. If you see `?? .env.production`, the previous check was wrong.

```bash
grep -n '^\.env' /opt/documind/.dockerignore
```

Expected:

```
15:.env
16:.env.*
```

Why both guards matter, and why they are different: `.gitignore` stops the
file reaching GitHub. `.dockerignore` stops it being copied into an image
layer — and an image is *pushed to a registry*, where a baked-in secret is
far harder to retract than a bad commit, because it sits in a layer digest
that anything which already pulled the image still has. The running
container gets these values through compose's `env_file` at start time
instead, so they live in the process environment and never in a layer.

### 5.14 What the startup gate will catch, and when you will see it

`app/core/startup_checks.py` runs whenever `ENV` is `production` and
**exits the process** rather than degrading. It collects *every* problem
and prints them in one message, so you fix the whole list in one restart
instead of discovering the next one each time.

It refuses to start on all of these:

- `JWT_SECRET_KEY` is a known placeholder (`change-me`, `changeme`,
  `secret`, `your-secret-key`, and anything starting `change-me`), **or**
  is shorter than 32 characters
- `DATABASE_URL` contains development credentials — `documind:documind`,
  `postgres:postgres` or `user:password`
- `RERANKER_PROVIDER=passthrough`, which leaves fused RRF rank
  reciprocals in place of relevance scores, so `CONFIDENCE_THRESHOLD`
  cannot threshold them and the gate effectively opens — shipping a
  system that answers everything
- `RERANKER_PROVIDER=hosted` with no `RERANKER_API_KEY`, because that
  degrades to exactly the same place, silently
- `GEMINI_API_KEY` missing while `EMBEDDING_PROVIDER` or `LLM_PROVIDER` is
  `gemini` — empty and whitespace-only both count as missing
- any provider set to `fake`
- both daily budgets at 0 (0 means *disabled*, not "zero allowed")
- `CELERY_TASK_ALWAYS_EAGER` set, or `RATE_LIMIT_ENABLED` false
- `CORS_ALLOW_ORIGINS` listing a loopback or non-HTTPS origin

**How you verify it in §6:** the gate runs in the FastAPI lifespan
(`app/main.py`), so it fires when the **api** container starts uvicorn —
**not** in the `migrate` job. `alembic upgrade head` imports
`app.core.config` but never `app.main`, so migrations apply cleanly even
with a bad signing key. If anything above is wrong, the api crash-loops
instead, and `docker compose logs api` shows:

```
refusing to start with ENV=production:
  - JWT_SECRET_KEY is a known placeholder. Generate one with: openssl rand -hex 32
  - ...
```

That output is safe to read and safe to share — the gate deliberately
never echoes the offending *value*, only its name, so a failed boot does
not put your signing key in a log.

§6 also includes the opposite check: deliberately breaking one value and
confirming the gate refuses, because a guard nobody has watched fire is a
claim, not a fact.

### 5.15 Last check — and stop here

```bash
cd /opt/documind && docker compose -f docker-compose.prod.yml --env-file .env.production config >/dev/null && echo "compose config OK"
```

`config` reads the compose file and your env file, substitutes everything
and validates the result. It **starts nothing** — no container, no
database, no application code, so the startup gate does not run yet.

Expected — one line:

```
compose config OK
```

An error here is a typo in the compose file or a variable it needs and
cannot find, named in the message.

> **The `>/dev/null` is not cosmetic — leave it in.** `docker compose
> config` prints the fully rendered file, with your database password and
> both API keys substituted in. Without the redirect you would put all
> three on your screen and into your terminal's scroll-back.

**Stop here.** Do not run `docker compose up`. §6 builds the image, runs
migrations as a separate job, starts the stack and verifies it — in that
order, for reasons that matter.

---

## 6. First deploy, by hand (P5)

Do this once before letting CI/CD do it, so you have seen every step
that `scripts/deploy.sh` will later run unattended.

Server steps run as **`deploy`** in `/opt/documind`. Windows steps run in
**cmd.exe** on your laptop and are marked **[Windows]**.

### 6.0 The four rules for this section

**1. Never print `.env.production`, and know which commands would.**
These leak every secret in it and must always be redirected or filtered:

| Command | Why it leaks | Safe form |
|---|---|---|
| `docker compose … config` | renders the file with your DB password and both API keys substituted in | append `>/dev/null` |
| `docker inspect <container>` | dumps the container's whole `Env` array | always pass a `--format` selecting one safe field |
| `docker compose exec api env` | prints the entire environment | do not run it |
| `cat .env.production` | the obvious one | use the name-and-length checks from §5.12 |

`docker compose logs` **is** safe: the application never logs a secret,
and the startup gate deliberately prints variable *names* only.
`docker stats` and `docker compose ps` are safe — neither reads
environment.

**2. Always `-f docker-compose.prod.yml`.** Never the bare
`docker compose` and never the dev `docker-compose.yml`. The dev file
publishes 5434, 5433, 6379 and 8000 **to the host** — correct on a
laptop, an open database on a public IPv4 — and it resolves to project
name `documind`, whose `db_data` volume is your development database.

Every command below spells the file out in full. That is deliberate: an
exported `COMPOSE_FILE` would vanish the moment you reconnect, and the
next `docker compose up -d` would silently pick the dev file.

**3. Never bring the stack down with the volume-removing flag.** It
destroys `db_data` and `uploads` — the entire database and every
uploaded document. `scripts/check_no_destructive_commands.sh` fails CI if
that command appears anywhere in the repo, which is why this runbook
describes it instead of spelling it out. The safe teardown is in 6.14.

**4. Use the smoke account only.** Never upload real documents or ask
real questions while verifying. Everything in 6.12 happens inside the
`smoke-checks` organisation.

### 6.1 Where the image comes from — decision

**Approach: build the image on this server.**

**Alternative not taken: build on your laptop, push to GHCR, pull here.**
That is the right answer *once CI exists* — it is exactly what §10
switches to, with images tagged by git SHA. It is the wrong answer for
the first deploy, for four reasons:

- it needs a **new credential on the box** — a token with
  `read:packages` — to pull a package that is not yet public. §4 and §9
  spend real effort keeping credentials off this server; adding one for
  a one-time task undoes that.
- that path has **already failed once here**: ADR 0007 records the
  laptop's Docker daemon wedging mid-build with `C:` at 100% and 0 bytes
  free. Build where you have measured free disk.
- CI has not run, so **no SHA-tagged image exists in GHCR**. There is
  nothing to pull.
- the first deploy should not depend on GitHub being up.

**Why a 2 GB box can do this safely:** nothing in `requirements.txt`
compiles. The Dockerfile records that all 22 pins have a cp314 x86_64
wheel or are pure Python, so `pip install` unpacks archives rather than
invoking a compiler — a few hundred MB of peak memory, not the 1–2 GB a
C build would want. And the build runs **before** the stack starts, so it
has the whole 2 GB plus your 2 GB swap to itself.

**If the build is OOM-killed anyway**, the fallback is the laptop path
above. 6.4 tells you how to recognise that specific failure.

### 6.2 [server] Check there is room to build

```bash
cd /opt/documind
```

```bash
df -h /
```

How much disk is free. The build pulls a base image and writes layers;
Postgres and your documents share the same 60 GB.

Expected — at least **5 GB** in `Avail`:

```
Filesystem      Size  Used Avail Use% Mounted on
/dev/nvme0n1p1   58G  3.2G   55G   6% /
```

```bash
free -h
```

Confirms the swap from §2 is still active — it is the safety net for the
build.

Expected — `Swap:` shows `2.0Gi` total:

```
               total        used        free      shared  buff/cache   available
Mem:           1.9Gi       280Mi       1.981Gi    1.0Mi       400Mi       1.5Gi
Swap:          2.0Gi          0B       2.0Gi
```

```bash
docker ps
```

Confirms nothing is already running and competing for memory.

Expected — the header row only:

```
CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
```

### 6.3 [server] Build the image

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production build
```

`--env-file` is **required even for a build.** Compose interpolates the
whole file before doing anything, and `docker-compose.prod.yml` declares
`${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}` — a *required* variable. Without
the flag this fails immediately with `set POSTGRES_PASSWORD`, which looks
like a missing value rather than a missing flag.

Expected: **3 to 8 minutes**, mostly download. Compose may report
building `api`, `worker` and `migrate` — they are one image with one
build config, so the second and third are cache hits and near-instant.
The output ends like:

```
 => [deps 2/3] COPY requirements.txt .                                    0.1s
 => [deps 3/3] RUN pip install --no-cache-dir -r requirements.txt       142.3s
 => [runtime 5/7] COPY --chown=10001:10001 app ./app                      0.3s
 => exporting to image                                                    4.1s
 => => naming to docker.io/library/documind-api:local
[+] Building 3/3 ... Built
```

**The long quiet stretch is `pip install`,** which prints almost nothing
for a minute or two. That is not a hang. Watch it in a second SSH
session with `free -h` if you want reassurance.

### 6.4 [server] Confirm the build succeeded — and what OOM looks like

```bash
docker images documind-api
```

Expected — one image, roughly 700 MB–1 GB:

```
REPOSITORY      TAG     IMAGE ID       CREATED          SIZE
documind-api    local   4f9c2a1b8e3d   32 seconds ago   842MB
```

**If the build failed, read the exit code before anything else.** An
out-of-memory kill looks like this:

```
failed to solve: process "/bin/sh -c pip install --no-cache-dir -r requirements.txt"
did not complete successfully: exit code: 137
```

**137 means killed by the kernel's OOM killer** (128 + signal 9) and
nothing else. Any other exit code is a real build error — a missing
wheel, a network failure — and the message above it says which. You
cannot read `dmesg` without root, so the exit code is your signal; `137`
is sufficient and unambiguous.

If you see 137: confirm `free -h` still shows 2.0Gi of swap, confirm
`docker ps` is empty, and retry once. If it recurs, switch to the laptop
build from 6.1.

### 6.5 [server] Apply the database migrations

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production run --rm migrate
```

A one-shot job that creates the schema. It must finish before `api` and
`worker` start, which the compose file enforces with
`service_completed_successfully` — but running it by hand first means you
read its output directly instead of meeting a failure as a dependency
error. `--rm` removes the one-off container afterwards. `run` starts `db`
first automatically and waits for it to be healthy.

Expected — Postgres starting, then every migration in order, ending at
**0009**:

```
[+] Creating 1/1
 ✔ Container documind-prod-db-1  Healthy
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, create organizations and users
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, create documents and document_chunks
...
INFO  [alembic.runtime.migration] Running upgrade 0008 -> 0009, add assist message kind
```

> **The startup gate does NOT run here, and it is worth knowing why.**
> The gate lives in the FastAPI lifespan (`app/main.py`), and this job
> runs `alembic upgrade head` — which imports `app.core.config` but never
> `app.main`. So migrations apply cleanly even with a bad signing key.
> The gate fires in 6.6, when the api starts uvicorn. An earlier draft of
> this runbook said the gate fired here; it does not.

### 6.6 [server] Start the services

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
```

`-d` detaches. Compose starts them in dependency order: `db` and `redis`
first, then `migrate` again (idempotent — the second run is a no-op),
then `api`, and `caddy` last because it waits for `api` to be *healthy*.

**This is where the startup gate runs.** If `.env.production` is wrong,
`api` raises in its lifespan, uvicorn exits non-zero, and Docker's
restart policy turns it into a visible crash-loop.

Expected — roughly 40–60 seconds, ending with all five started:

```
[+] Running 9/9
 ✔ Network documind-prod_internal      Created
 ✔ Network documind-prod_edge          Created
 ✔ Volume "documind-prod_db_data"      Created
 ✔ Volume "documind-prod_uploads"      Created
 ✔ Container documind-prod-db-1        Healthy
 ✔ Container documind-prod-redis-1     Healthy
 ✔ Container documind-prod-migrate-1   Exited
 ✔ Container documind-prod-api-1       Healthy
 ✔ Container documind-prod-caddy-1     Started
```

`migrate … Exited` is **correct** — it is a job that ran and finished.

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production ps
```

Expected — note that **only `caddy` has anything in PORTS**:

```
NAME                       IMAGE                   STATUS                  PORTS
documind-prod-api-1        documind-api:local      Up 2 minutes (healthy)
documind-prod-caddy-1      caddy:2.10-alpine       Up 1 minute             0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
documind-prod-db-1         pgvector/pgvector:pg16  Up 2 minutes (healthy)
documind-prod-redis-1      redis:7-alpine          Up 2 minutes (healthy)
documind-prod-worker-1     documind-api:local      Up 2 minutes (healthy)
```

The project name must read **`documind-prod`**. If it says `documind`,
you used the dev compose file — stop and re-read rule 2.

**If `api` shows `Restarting`**, the gate refused. Read why:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production logs api
```

Expected on failure — a named list, with **no values**:

```
refusing to start with ENV=production:
  - JWT_SECRET_KEY is shorter than 32 characters
  - EMBEDDING_PROVIDER=gemini but GEMINI_API_KEY is not set
```

Fix every line it names, then `up -d` again. The whole list arrives at
once by design, so you do not discover the second problem after fixing
the first.

### 6.7 [server] Prove the gate actually refuses

The gate passing is only half the evidence. A guard nobody has watched
fire is a claim, not a fact — so break one value deliberately.

**This does not touch `.env.production` and creates no copy of it.**
`docker compose run -e` overrides one variable for one throwaway
container; the real file is never read differently, never edited, and
nothing containing a secret is written anywhere.

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production run --rm --no-deps -e JWT_SECRET_KEY=short api
```

`--no-deps` so it does not touch the running stack, and `short` is
obviously not a secret, so nothing sensitive reaches your history or the
process list.

Expected — uvicorn starts, the lifespan raises, the process exits:

```
INFO:     Started server process [1]
INFO:     Waiting for application startup.
ERROR:    Traceback (most recent call last):
...
app.core.startup_checks.UnsafeProductionConfig: refusing to start with ENV=production:
  - JWT_SECRET_KEY is shorter than 32 characters
ERROR:    Application startup failed. Exiting.
```

**That is a PASS.** The gate named the problem, never echoed the value,
and the process refused to serve.

Confirm the real stack is untouched:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production ps
```

Expected: the same five rows as 6.6, `api` still `(healthy)`.

### 6.8 [server] Watch Caddy get the certificate

Caddy requests a Let's Encrypt certificate for `documind-prod.duckdns.org`
on its first start, using automatic HTTPS. It solves an ACME challenge —
TLS-ALPN-01 on **443** or HTTP-01 on **80** — which is why both ports must
be open in the Lightsail firewall (they are, from §1). **Keep 80 open
permanently**: it is not only the HTTP→HTTPS redirect, it is also a
renewal path.

The certificate is stored in the `caddy_data` volume, so restarts reuse
it. That matters: Let's Encrypt allows 5 identical certificates per week,
and a stack that re-requested on every restart would be rate-limited
within a day.

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production logs caddy
```

Caddy's logs are safe to read — hostnames, ACME URLs and your
`ACME_EMAIL`, none of which is a secret.

Expected on success:

```
{"level":"info","msg":"obtaining certificate","identifier":"documind-prod.duckdns.org"}
{"level":"info","msg":"waiting on internal rate limiter","identifiers":["documind-prod.duckdns.org"]}
{"level":"info","msg":"served key authentication","identifier":"documind-prod.duckdns.org","challenge":"tls-alpn-01"}
{"level":"info","msg":"certificate obtained successfully","identifier":"documind-prod.duckdns.org"}
{"level":"info","msg":"serving initial configuration"}
```

The line that matters is **`certificate obtained successfully`**. It
usually arrives within 30 seconds of caddy starting.

What failure looks like, and what each one means:

| Log line | Cause |
|---|---|
| `could not get certificate from issuer` + `challenge failed` | the hostname does not resolve to this server, or 80/443 are not reachable from the internet |
| `connection refused` / `timeout` during the challenge | a firewall is blocking 80 or 443 |
| `too many certificates already issued` | Let's Encrypt rate limit — **wait**, do not retry in a loop |
| `wrong argument count … after 'email'` | `ACME_EMAIL` is empty; the config is unparseable and the edge refuses to start |

If caddy never appears at all, remember it waits for `api` to be healthy —
fix `api` first.

### 6.9 [Windows] Verify from outside the server

These run on **your laptop**, so they test the real path a user takes:
public DNS, the Lightsail firewall, TLS, Caddy, then the API.

Use **`curl.exe`**, not `curl`. In PowerShell `curl` is an alias for
`Invoke-WebRequest`, which takes different arguments; `curl.exe` is
unambiguous in both shells.

**Check 1 — health over HTTPS, with certificate validation:**

```
curl.exe -sS https://documind-prod.duckdns.org/health
```

No `-k` anywhere. **Not passing `-k` is what proves the certificate is
valid** — curl validates against the Windows trust store and refuses on a
bad chain.

Expected — JSON with `"status":"ok"`:

```
{"status":"ok","degraded":[]}
```

A certificate problem instead reads `curl: (60) SSL certificate problem`.

**Check 2 — the certificate verified, in one number:**

```
curl.exe -sS -o NUL -w "%{http_code} verify=%{ssl_verify_result}\n" https://documind-prod.duckdns.org/health
```

`ssl_verify_result` is curl's own verification result: **0 means
verified.**

Expected:

```
200 verify=0
```

(If you ever paste this into a `.bat` file, double the percent signs.)

**Check 3 — who issued it, and when it expires:**

```
curl.exe -vI https://documind-prod.duckdns.org/health 2>&1 | findstr /C:"subject:" /C:"issuer:" /C:"expire date"
```

Expected — subject is your hostname, issuer is Let's Encrypt, expiry
about 90 days out:

```
*  subject: CN=documind-prod.duckdns.org
*  start date: Nov  7 00:00:00 2026 GMT
*  expire date: Feb  5 00:00:00 2027 GMT
*  issuer: C=US; O=Let's Encrypt; CN=E6
```

The issuer `CN` rotates between Let's Encrypt intermediates — `O=Let's
Encrypt` is the part to check.

**Check 4 — HTTP redirects to HTTPS:**

```
curl.exe -sS -o NUL -w "%{http_code} -> %{redirect_url}\n" http://documind-prod.duckdns.org/health
```

Note `http://`. Caddy does this automatically for a hostname site
address; no redirect is written in the Caddyfile.

Expected — a permanent redirect to the same path over HTTPS:

```
308 -> https://documind-prod.duckdns.org/health
```

**Check 5 — the API docs are disabled in production:**

```
curl.exe -sS -o NUL -w "docs=%{http_code}\n" https://documind-prod.duckdns.org/docs
```

```
curl.exe -sS -o NUL -w "openapi=%{http_code}\n" https://documind-prod.duckdns.org/openapi.json
```

`app/main.py` passes `docs_url=None`, `redoc_url=None` and
`openapi_url=None` when `ENV` is production, so these routes do not
exist. A live schema would publish every endpoint and payload shape to
anyone who asked.

Expected — **404** for both:

```
docs=404
openapi=404
```

A **200** here means `ENV` is not set to `production` in
`.env.production`, which also means the startup gate never ran.

**Check 6 — the security headers are present and the Server header is
gone:**

```
curl.exe -sSI https://documind-prod.duckdns.org/health | findstr /I "strict-transport x-frame x-content server"
```

Expected — three headers present, **no `Server:` line at all**:

```
strict-transport-security: max-age=31536000; includeSubDomains
x-content-type-options: nosniff
x-frame-options: DENY
```

### 6.10 [server] Verify nothing but 22, 80 and 443 is exposed

```bash
ss -tln
```

`ss -tln` lists TCP sockets that are **l**istening, **n**umerically,
without needing root.

Expected — `0.0.0.0` / `[::]` only on 22, 80 and 443. The `127.0.0.x:53`
entries are systemd-resolved on loopback and are not reachable from
outside:

```
State   Recv-Q  Send-Q   Local Address:Port    Peer Address:Port
LISTEN  0       4096     127.0.0.54:53         0.0.0.0:*
LISTEN  0       4096     127.0.0.53%lo:53      0.0.0.0:*
LISTEN  0       128      0.0.0.0:22            0.0.0.0:*
LISTEN  0       4096     0.0.0.0:80            0.0.0.0:*
LISTEN  0       4096     0.0.0.0:443           0.0.0.0:*
LISTEN  0       128      [::]:22               [::]:*
LISTEN  0       4096     [::]:80               [::]:*
LISTEN  0       4096     [::]:443              [::]:*
```

Now the explicit negative — the datastore ports must appear nowhere:

```bash
ss -tln | grep -E ':(5432|6379|8000)' || echo "nothing listening on 5432, 6379 or 8000"
```

Expected — exactly this sentence:

```
nothing listening on 5432, 6379 or 8000
```

And prove it by trying to connect:

```bash
timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/5432' || echo "postgres is not reachable on the host: correct"
```

This opens a raw TCP connection using a bash builtin, so it needs nothing
installed.

Expected:

```
bash: connect: Connection refused
bash: line 1: /dev/tcp/127.0.0.1/5432: Connection refused
postgres is not reachable on the host: correct
```

The errors **are** the pass. Postgres is on the `internal` network, which
compose declares `internal: true` — it has no route to the host or the
internet at all, and the api reaches it as `db:5432`.

> `scripts/verify_d1.sh` is **not** runnable here, by the way. Its step 2
> wants a development test database at `host.docker.internal:5433` and its
> step 9 invokes `./venv/Scripts/python.exe` — a Windows path. It is a
> laptop script. The checks in this section are its portable half.

### 6.11 [server] Resource check on a 2 GB box

```bash
docker stats --no-stream
```

A one-shot snapshot. Safe to run — it reads no environment.

Expected — compare `MEM USAGE / LIMIT` against the compose limits
(caddy 96M, api 512M, worker 448M, db 448M, redis 160M):

```
NAME                       CPU %   MEM USAGE / LIMIT     MEM %   PIDS
documind-prod-caddy-1       0.12%   14.2MiB / 96MiB       14.79%  10
documind-prod-api-1         0.35%   188MiB / 512MiB       36.72%  5
documind-prod-worker-1      0.21%   142MiB / 448MiB       31.70%  4
documind-prod-db-1          0.18%   96.4MiB / 448MiB      21.52%  8
documind-prod-redis-1       0.30%   9.8MiB / 160MiB        6.13%  6
```

**OK looks like:** every `MEM %` comfortably under 70% at idle. A
container sitting at 95%+ will be OOM-killed under load and comes back as
a restart you did not ask for.

```bash
free -h
```

Expected at idle — the limits total 1664M against 1.9Gi of RAM, so this
is tight by design and the swap is why it works:

```
               total        used        free      shared  buff/cache   available
Mem:           1.9Gi       1.3Gi       120Mi       8.0Mi       600Mi       480Mi
Swap:          2.0Gi        48Mi       1.9Gi
```

**OK looks like:** `available` at least ~300Mi, and `Swap used` under
roughly 200Mi. A little swap in use is healthy — that is its job.

**Not OK:** swap used climbing past ~500Mi while `available` sits near
zero. That is thrashing, and it presents as every request becoming slow
rather than as an error. Confirm with `vmstat 2 5` — sustained non-zero
`si`/`so` columns means the box is paging continuously.

This is also where §3.15's log cap becomes checkable on a real container:

```bash
docker inspect documind-prod-api-1 --format '{{json .HostConfig.LogConfig}}'
```

**The `--format` is mandatory here.** A bare `docker inspect` prints the
container's whole `Env` array — every secret in `.env.production`. The
format string selects one field.

Expected:

```
{"Type":"json-file","Config":{"max-file":"3","max-size":"10m"}}
```

### 6.12 [server] Create the smoke account

One dedicated account, created once by hand. `scripts/smoke_test.sh`
signs in as it and deliberately does **not** register — a smoke test that
registered would leave a new organisation behind on every deploy.

**Generate the password into a file, never onto your screen:**

```bash
( umask 077; openssl rand -hex 24 > /home/deploy/.smoke_password )
```

The subshell keeps the tight `umask` from leaking into your session, so
the file is created mode 600 from the start rather than being chmod'ed
after a moment of being readable. It lives in your home directory,
**not** in `/opt/documind` — nothing in the repo directory should ever
hold a secret.

Expected: **no output.** Confirm without printing it:

```bash
ls -l /home/deploy/.smoke_password
```

Expected — `-rw-------`, 49 bytes (48 hex characters plus a newline):

```
-rw------- 1 deploy deploy 49 Nov  7 12:30 /home/deploy/.smoke_password
```

48 characters is inside the register endpoint's 8–72 character range.

**Pick the email now.** It must be a domain that really accepts mail —
`.test`, `.local` and `.invalid` are reserved and the email validator
rejects them, which surfaces as a 422 that looks like a broken endpoint.
A plus-address on a mailbox you own is the easiest correct answer:
`yourname+documind-smoke@gmail.com`. Substitute yours in both commands
below.

**Register:**

```bash
curl -sS -X POST "https://documind-prod.duckdns.org/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<EOF
{"org_name":"smoke-checks","email":"yourname+documind-smoke@gmail.com","password":"$(cat /home/deploy/.smoke_password)"}
EOF
```

The password arrives through **stdin**, not an argument, so it never
appears in the process list — and your shell history records the literal
`$(cat …)`, not the value.

Expected — **201** with ids and the role. The first user of an
organisation is its admin, which is what lets 6.13 upload:

```
{"org_id":"3f2a...","user_id":"8c91...","email":"yourname+documind-smoke@gmail.com","role":"admin"}
```

A **422** mentioning `email` means the domain was rejected — use a
plus-address on a real mailbox. A **409** means the organisation name is
already taken, so this step already succeeded earlier.

**Run the smoke test:**

```bash
SMOKE_BASE_URL="https://documind-prod.duckdns.org" \
SMOKE_ORG=smoke-checks \
SMOKE_EMAIL=yourname+documind-smoke@gmail.com \
SMOKE_PASSWORD="$(cat /home/deploy/.smoke_password)" \
bash scripts/smoke_test.sh
```

Prefixed variables apply to this one command only and are not exported
into your session. The password is read from the file again rather than
typed.

Expected — **6 passed, 0 failed**:

```
smoke: https://documind-prod.duckdns.org
  PASS  /health is ok
  PASS  /health/ready is ready
  PASS  /openapi.json is 404 in production
  PASS  unauthenticated /documents is 401
  PASS  the smoke account signed in
  PASS  the assist path streamed a citations event (no provider cost)

smoke: 6 passed, 0 failed
```

Check 6 asks the question `hello`, which `app/services/assistance.py`
intercepts before the rewriter, the cache, the embedder, the reranker and
the model — **zero provider cost**, measured by a test. It therefore does
*not* prove Gemini or Cohere work. That is 6.13.

### 6.13 [server] One real end-to-end question

This is the only step that **spends provider quota**, and it is the point:
it is the first and only proof that your production Gemini and Cohere keys
work. One upload, one question.

**Sign in and keep the token in a variable:**

```bash
TOKEN=$(curl -sS -X POST "https://documind-prod.duckdns.org/api/v1/auth/login" -H 'Content-Type: application/json' --data-binary @- <<EOF | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
{"org_name":"smoke-checks","email":"yourname+documind-smoke@gmail.com","password":"$(cat /home/deploy/.smoke_password)"}
EOF
)
```

Expected: **no output.** Confirm you got a token without printing it:

```bash
echo "token length: ${#TOKEN}"
```

Expected — a JWT is a few hundred characters:

```
token length: 274
```

If it says `0`, the login failed; re-run without the `sed` pipe to read
the error.

> The token does ride on a command line in the next few steps, so it is
> briefly visible to a process listing on this box. Acceptable on a
> single-operator server, and it expires in 24 hours. 6.13 ends by
> clearing it.

**Make a tiny test document:**

```bash
printf '# Smoke Test Policy\n\nThe smoke test reimbursement limit is 42 AUD per day.\n' > /tmp/smoke.md
```

One quotable fact, so a grounded answer is unmistakable and a hallucinated
one is obvious.

Expected: **no output.**

**Upload it:**

```bash
DOC=$(curl -sS -X POST "https://documind-prod.duckdns.org/api/v1/documents" -H "Authorization: Bearer $TOKEN" -F "file=@/tmp/smoke.md" | sed -n 's/.*"document_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
```

Then:

```bash
echo "document: $DOC"
```

Expected — a UUID. A document id is not a secret:

```
document: 5b8e1c44-9a2f-4d31-b7e6-2c0a9f4d8e13
```

Upload returns **202, not 201**: the row exists but ingestion is still
queued in Celery. Claiming 201 would tell a client it was ready to query.

**Wait for the worker to finish:**

```bash
curl -sS "https://documind-prod.duckdns.org/api/v1/documents/$DOC" -H "Authorization: Bearer $TOKEN"
```

Run it again after a few seconds if needed. This is the first real
exercise of your Gemini **embedding** key.

Expected — `"status":"ready"` within about 10 seconds for a file this
small:

```
{"id":"5b8e1c44-...","filename":"smoke.md","status":"ready","chunk_count":2,...}
```

**`"status":"failed"`** means ingestion failed, and the response carries a
coarse `failure_code` rather than the raw error. `rate_limited` means
Gemini's free tier pushed back — wait and re-upload.
`provider_unavailable` or an auth-shaped failure means the key is wrong.
The raw provider message stays in the worker log:
`docker compose -f docker-compose.prod.yml --env-file .env.production logs worker`.

**Open a conversation:**

```bash
CONV=$(curl -sS -X POST "https://documind-prod.duckdns.org/api/v1/conversations" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"title":"smoke e2e"}' | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
```

```bash
echo "conversation: $CONV"
```

Expected: a UUID.

**Ask the question:**

```bash
curl -sS -N -X POST "https://documind-prod.duckdns.org/api/v1/conversations/$CONV/messages" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"content":"What is the smoke test reimbursement limit?"}'
```

`-N` disables curl's buffering so you see the stream arrive token by
token. This exercises retrieval, your Cohere rerank key, and your Gemini
answer key.

Expected — `token` events, then exactly one `citations` event, then
`done`:

```
event: token
data: {"text":"The smoke"}

event: token
data: {"text":" test reimbursement limit is 42 AUD per day [1]."}

event: citations
data: {"answer":"The smoke test reimbursement limit is 42 AUD per day [1].","citations":[{"marker":1,"document_id":"5b8e1c44-...","filename":"smoke.md","page":1,"score":0.87,...}],"cached":false,"gated":false,"kind":"answer","confidence":0.87,"scores_calibrated":true,"retrieve_ms":180,"rerank_ms":640,"first_token_ms":910}
```

**What a pass looks like, precisely:**

- the answer contains **42 AUD** and a **`[1]`** marker;
- `citations` is non-empty and names `smoke.md`;
- `"gated":false` and `"kind":"answer"`;
- `"scores_calibrated":true` — which is only true when the Cohere call
  **succeeded**. If it read `false` with `used_reranker_fallback":true`,
  reranking fell back to fused rank order and your Cohere key is not
  working, even though you still got an answer.

A refusal (`"kind":"refusal"`, low `confidence`) with the document ready
means retrieval found nothing — check `chunk_count` was not 0.

**Clean up the test document and the token:**

```bash
curl -sS -o /dev/null -w "delete=%{http_code}\n" -X DELETE "https://documind-prod.duckdns.org/api/v1/documents/$DOC" -H "Authorization: Bearer $TOKEN"
```

Expected — **204**, no content:

```
delete=204
```

```bash
unset TOKEN DOC CONV && rm -f /tmp/smoke.md
```

Expected: **no output.** Deleting the document also bumps the org's
`index_version`, which makes the cached answer unreachable — the cache
key hashes it, so the cached passage can no longer be computed by any
later request.

### 6.14 Stopping and restarting without losing data

**Stop the stack cleanly:**

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production down
```

Plain `down` removes the containers and the two networks. **It does not
touch named volumes** — `db_data`, `uploads`, `caddy_data` and
`caddy_config` all survive, so your database, your documents and your
certificate are intact.

Expected:

```
[+] Running 7/7
 ✔ Container documind-prod-caddy-1   Removed
 ✔ Container documind-prod-worker-1  Removed
 ✔ Container documind-prod-api-1     Removed
 ✔ Container documind-prod-migrate-1 Removed
 ✔ Container documind-prod-redis-1   Removed
 ✔ Container documind-prod-db-1      Removed
 ✔ Network documind-prod_internal    Removed
```

**Confirm the data survived:**

```bash
docker volume ls --filter name=documind-prod
```

Expected — all four still listed:

```
DRIVER    VOLUME NAME
local     documind-prod_caddy_config
local     documind-prod_caddy_data
local     documind-prod_db_data
local     documind-prod_uploads
```

**Start over from scratch, keeping the data:**

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
```

Same command as 6.6. The volumes are reattached, migrations re-run as a
no-op, and Caddy reuses the stored certificate instead of asking Let's
Encrypt again.

**To rebuild after changing code**, pull and rebuild, then restart:

```bash
git pull && docker compose -f docker-compose.prod.yml --env-file .env.production build && docker compose -f docker-compose.prod.yml --env-file .env.production up -d
```

> **Never pass the volume-removing flag to `down`.** It is the one
> command on this box that destroys data no backup yet covers — P6 adds
> `pg_dump`, and until then the volume *is* the only copy. If you truly
> need a clean database, remove `documind-prod_db_data` by name,
> deliberately, with the stack down.

### 6.15 Note on the branch

You are on `phase-5-deploy-cicd` at `7ed43ae`, not `main`. That is fine
for a manual deploy — you built from the working tree. But **CD triggers
on CI completing for `main`** (`workflow_run`, checking
`conclusion == 'success'`), so nothing deploys automatically until this
branch is merged. §10 is where that happens.

---

## 7. SSH hardening, the firewall, and fail2ban

**Only after §6 — the stack must already be running and verified.**

This is the section that can lock you out of the server permanently.
Everything below is ordered so you always have a way back, and each step
has a rollback you can run from a session that is already open.

> ### The three rules for this section
>
> **1. Keep your current `ubuntu` session open from 7.1 until 7.16 is
> done.** Do not close it, do not let it time out, do not reboot. An
> already-authenticated session keeps working even if you break the
> config for every *new* connection — it is the only cheap way back.
>
> **2. Never reload sshd without running `sudo sshd -t` first.** It
> validates the config and prints the file and line of any mistake. A
> reload with a broken config is how people lose a server.
>
> **3. Test every change from a NEW window before closing anything.** A
> change that works in your existing session proves nothing: that
> session authenticated under the old rules.

`ubuntu` stays your **admin recovery path** throughout. It keeps key
login, it keeps `sudo`, and nothing here restricts it beyond requiring a
key — which is already how you connect.

### 7.1 [server] Confirm both accounts can log in by key, right now

Establish the thing you are about to depend on, before changing it.

```bash
sudo grep -c . /home/ubuntu/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
```

Counts non-empty lines in each file without printing any key.

Expected — at least one key each:

```
/home/ubuntu/.ssh/authorized_keys:1
/home/deploy/.ssh/authorized_keys:1
```

**If `deploy` shows 0, stop.** You would be hardening SSH with no
working deploy key, and CI/CD would never connect again.

### 7.2 [server] Check what the config actually allows today

```bash
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication)'
```

`sshd -T` prints the **effective** configuration — defaults resolved,
includes applied. Reading `sshd_config` by eye misses what a file in
`sshd_config.d/` already overrode.

Expected on a fresh Lightsail Ubuntu 24.04. Note `passwordauthentication`
is usually **already** `no`, because the image ships it that way:

```
permitrootlogin prohibit-password
passwordauthentication no
kbdinteractiveauthentication no
pubkeyauthentication yes
```

Write down what you see. It is your rollback target, and it tells you
how much 7.3 actually changes.

### 7.3 [server] Write the hardening file

```bash
sudo tee /etc/ssh/sshd_config.d/99-documind.conf >/dev/null <<'EOF'
# Key-only login, for ubuntu and deploy. Nothing else.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
```

A drop-in under `sshd_config.d/` rather than an edit to `sshd_config`:
the main file is package-managed and an upgrade can replace it, taking
your changes with it. A drop-in survives, and deleting one file is a
complete rollback.

What each line does:

- `PermitRootLogin no` — root cannot log in at all. The default,
  `prohibit-password`, still permits a key, and Lightsail installs one
  for root that prints a notice. `no` removes the question.
- `PasswordAuthentication no` — no password login for anybody. This is
  what makes a guessed or stolen password worthless, and it is why
  fail2ban (7.12) is a second line rather than the first.
- `KbdInteractiveAuthentication no` — closes the other interactive
  route. Leaving this `yes` alongside `PasswordAuthentication no` is a
  classic half-fix, because PAM can still prompt for a password through
  it.
- `PubkeyAuthentication yes` — stated explicitly. It is the default, but
  this file is where someone will look to find out how logins work, and
  a hardening file that never mentions the method that must keep working
  is one someone will "tidy up".

Expected: **no output.**

### 7.4 [server] Validate before reloading — do not skip this

```bash
sudo sshd -t
```

Expected: **no output at all.** Silence means the config is valid.

**Any output is an error**, naming a file and line. Fix it before going
further. Do not reload.

> Rollback here costs nothing, because nothing has been applied yet:
> ```bash
> sudo rm /etc/ssh/sshd_config.d/99-documind.conf
> ```

### 7.5 [server] Confirm the effective config is what you meant

```bash
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication)'
```

`sshd -T` reads the files from disk, so this shows the result **before**
any running process adopts it.

Expected:

```
permitrootlogin no
passwordauthentication no
kbdinteractiveauthentication no
pubkeyauthentication yes
```

### 7.6 [server] Reload sshd

```bash
sudo systemctl reload ssh
```

`reload`, not `restart`: a reload re-reads the config and **leaves
established connections alone**, so your open session survives even if
something is wrong. On Ubuntu 24.04 the unit is `ssh`, not `sshd`.

Expected: **no output.**

```bash
systemctl is-active ssh
```

Expected:

```
active
```

> **Rollback — run this in your STILL-OPEN session:**
> ```bash
> sudo rm /etc/ssh/sshd_config.d/99-documind.conf && sudo sshd -t && sudo systemctl reload ssh
> ```

### 7.7 [Windows] Test both logins from a NEW window

**Do not close your existing session.** Open a new cmd window.

```
ssh -i "<path-to-LightsailDefaultKey.pem>" ubuntu@documind-prod.duckdns.org "whoami"
```

Expected:

```
ubuntu
```

```
ssh -i "%USERPROFILE%\.ssh\documind_deploy" deploy@documind-prod.duckdns.org "whoami"
```

Expected:

```
deploy
```

**Both must succeed before you continue.** If either fails, run the
rollback from 7.6 in your open session, then re-check 7.1.

### 7.8 [Windows] Confirm password login is actually refused

Proving the change did what it claims, rather than trusting it.

```
ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no ubuntu@documind-prod.duckdns.org
```

This asks the server for password authentication specifically, and
offers no key.

Expected — refused without ever prompting:

```
ubuntu@documind-prod.duckdns.org: Permission denied (publickey).
```

**A password prompt here means `PasswordAuthentication no` did not take
effect.** Re-check 7.5 and 7.6.

### 7.9 [server] The firewall — read this before running anything

The next steps are the ones most often misunderstood, so here is the
honest picture first.

**Docker-published ports BYPASS ufw.** When Docker publishes `80:80` and
`443:443` it writes its own rules into the `DOCKER` and `DOCKER-USER`
iptables chains, and traffic to a published container port is handled
there **before** ufw's `INPUT` rules are consulted. So:

- `sudo ufw deny 80/tcp` would **not** stop traffic reaching Caddy.
- `ufw status` can report "deny" for a port that is wide open in
  practice.

What follows from that:

- **The Lightsail IPv4 firewall is the real perimeter** for 80 and 443.
  It sits outside the host, in front of the kernel, and Docker cannot
  write rules there. It is already restricted to 22/80/443 from §1.
- **ufw here guards HOST services only** — sshd, and anything else
  listening directly on the host rather than inside a container. That is
  a real and worthwhile job: it is the layer that catches a service
  someone installs later and forgets to think about.
- ufw is therefore defence in depth, not the perimeter. Configuring it
  while believing otherwise is how people conclude their database is
  firewalled when it is in fact published to the world.

§6.10 already proved Postgres and Redis publish nothing, and that is
what actually keeps them unreachable — the compose file's
`internal: true` network, not any firewall.

### 7.10 [server] Configure and enable ufw

Order matters: add the allow rules **before** enabling, or
`default deny incoming` takes effect with no SSH rule and the next
connection is refused.

```bash
sudo ufw default deny incoming
```

Expected — and note this has not taken effect yet, ufw is still
inactive:

```
Default incoming policy changed to 'deny'
(be sure to update your rules accordingly)
```

```bash
sudo ufw default allow outgoing
```

Outbound stays open: the server must reach Gemini, Cohere, Let's Encrypt
and the Docker registry.

Expected:

```
Default outgoing policy changed to 'allow'
```

```bash
sudo ufw allow 22/tcp comment 'ssh'
```

**The rule that matters.** It must exist before you enable.

Expected:

```
Rule added
Rule added (v6)
```

```bash
sudo ufw allow 80/tcp comment 'http (also handled by docker)'
```

```bash
sudo ufw allow 443/tcp comment 'https (also handled by docker)'
```

Expected for each: `Rule added` and `Rule added (v6)`. These two are
declared for honesty — so `ufw status` describes the intended perimeter
instead of contradicting it — not because ufw is what enforces them
(7.9).

```bash
sudo ufw --force enable
```

`--force` skips the interactive "this may disrupt existing ssh
connections" prompt, which cannot be answered in a non-interactive
shell. Your open session is unaffected: ufw does not drop established
connections.

Expected:

```
Firewall is active and enabled on system startup
```

### 7.11 [server] Verify the firewall, then test from outside

```bash
sudo ufw status verbose
```

Expected — active, deny incoming, exactly three allowed ports:

```
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere                   # ssh
80/tcp                     ALLOW IN    Anywhere                   # http (also handled by docker)
443/tcp                    ALLOW IN    Anywhere                   # https (also handled by docker)
22/tcp (v6)                ALLOW IN    Anywhere (v6)              # ssh
80/tcp (v6)                ALLOW IN    Anywhere (v6)              # http (also handled by docker)
443/tcp (v6)               ALLOW IN    Anywhere (v6)              # https (also handled by docker)
```

Now confirm from your laptop, in a **new** window, that nothing broke:

```
curl.exe -sS -o NUL -w "health=%{http_code}\n" https://documind-prod.duckdns.org/health
```

Expected:

```
health=200
```

```
ssh -i "%USERPROFILE%\.ssh\documind_deploy" deploy@documind-prod.duckdns.org "whoami"
```

Expected: `deploy`

> **Rollback — from your still-open session:**
> ```bash
> sudo ufw disable
> ```
> Immediate and complete. To remove the rules as well:
> `sudo ufw --force reset`

### 7.12 [server] Install fail2ban

```bash
sudo apt-get update
```

```bash
sudo apt-get install -y fail2ban
```

Expected: a normal install log ending in `Setting up fail2ban ...` plus
a `Created symlink` line for the service.

With `PasswordAuthentication no` already set, fail2ban is **not** what
stops a break-in — keys are. What it stops is the noise. A public IPv4
collects thousands of authentication attempts a day, and each is a log
line, a few CPU cycles and a crypto handshake. Banning repeat offenders
keeps the journal readable, which is what makes a real anomaly visible.

### 7.13 [server] Configure the sshd jail explicitly

```bash
sudo tee /etc/fail2ban/jail.local >/dev/null <<'EOF'
[DEFAULT]
# Read from the systemd journal. Ubuntu 24.04 ships no rsyslog and so
# has no /var/log/auth.log; a jail left on the file backend loads,
# reports itself active, and silently never matches anything.
backend = systemd
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
EOF
```

**`backend = systemd` is the line that makes this work on 24.04.** The
default expects `/var/log/auth.log`, which does not exist on this image.
That failure mode looks exactly like "no attacks yet", which is why it
goes unnoticed.

`jail.local` rather than editing `jail.conf`: the `.conf` is
package-managed and replaced on upgrade.

Expected: **no output.**

```bash
sudo systemctl enable --now fail2ban
```

Expected: **no output**, or one `Created symlink` line.

### 7.14 [server] Verify fail2ban is actually watching

```bash
sudo fail2ban-client status
```

Expected — the jail list must contain `sshd`:

```
Status
|- Number of jail:	1
`- Jail list:	sshd
```

```bash
sudo fail2ban-client status sshd
```

Expected — counters will differ, and a non-zero `Total failed` within
minutes is normal on a public IPv4:

```
Status for the jail: sshd
|- Filter
|  |- Currently failed:	0
|  |- Total failed:	14
|  `- Journal matches:	_SYSTEMD_UNIT=sshd.service + _COMM=sshd
`- Actions
   |- Currently banned:	0
   |- Total banned:	2
   `- Banned IP list:
```

**`Journal matches:` is the line that proves the backend is right.** If
it instead reads `File list: /var/log/auth.log`, the backend did not
apply — re-check 7.13.

**If `Total failed` stays at 0 for an hour on a public IP**, be
suspicious rather than pleased: that is the signature of a jail running
and matching nothing.

> **Rollback:**
> ```bash
> sudo systemctl disable --now fail2ban
> ```
> To unban an address — including your own, if you fat-finger a login
> five times:
> ```bash
> sudo fail2ban-client set sshd unbanip <YOUR_IP>
> ```
> If your home IP is stable, add it to the never-ban list: put
> `ignoreip = 127.0.0.1/8 <YOUR_IP>` under `[DEFAULT]` in `jail.local`
> and `sudo systemctl reload fail2ban`.

### 7.15 Optional — restrict logins to two accounts

Stronger, with a sharper edge. Skip it if you are unsure.

```bash
echo 'AllowUsers ubuntu deploy' | sudo tee -a /etc/ssh/sshd_config.d/99-documind.conf
```

Any account not named here can no longer log in by any method. A typo in
either name locks out that account, which is why this is last, optional,
and validated the same way:

```bash
sudo sshd -t && sudo sshd -T | grep '^allowusers'
```

Expected:

```
allowusers ubuntu deploy
```

```bash
sudo systemctl reload ssh
```

Then **test both accounts from a new window again**, exactly as in 7.7.

> **Rollback:** remove the line and reload.
> ```bash
> sudo sed -i '/^AllowUsers/d' /etc/ssh/sshd_config.d/99-documind.conf && sudo sshd -t && sudo systemctl reload ssh
> ```

### 7.16 Only now, close the spare session

Once 7.7, 7.8, 7.11 and 7.14 have all passed from a **new** window, the
old session has done its job. Close it.

For one more reassurance, reboot — a reboot is the real test that
nothing here depended on state that only existed in memory:

```bash
sudo reboot
```

Wait about 40 seconds, then from your laptop:

```
ssh -i "%USERPROFILE%\.ssh\documind_deploy" deploy@documind-prod.duckdns.org "cd /opt/documind && docker compose -f docker-compose.prod.yml --env-file .env.production ps --format '{{.Name}} {{.Status}}'"
```

Expected — the stack back up by itself, because §3.8 enabled Docker at
boot and every service carries `restart: unless-stopped`:

```
documind-prod-api-1 Up 30 seconds (healthy)
documind-prod-caddy-1 Up 25 seconds
documind-prod-db-1 Up 32 seconds (healthy)
documind-prod-redis-1 Up 32 seconds (healthy)
documind-prod-worker-1 Up 30 seconds (healthy)
```

---

## 8. Unattended upgrades and the clock

```bash
sudo apt-get install -y unattended-upgrades
```

Security updates applied without anyone remembering to. On a box with
one public IPv4 and one operator, an unpatched sshd is a likelier
problem than a bad automatic update.

Expected: a normal install log.

```bash
sudo dpkg-reconfigure -plow unattended-upgrades
```

Answer **Yes**. This is the one interactive prompt in the runbook.

Expected: a blue dialog. Afterwards, confirm it stuck:

```bash
grep -E 'Unattended-Upgrade|Update-Package-Lists' /etc/apt/apt.conf.d/20auto-upgrades
```

Expected — both `"1"`:

```
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
```

```bash
sudo timedatectl set-timezone UTC
```

Every timestamp in this system is UTC — `usage_events`, the daily budget
key, the `clock_timestamp()` column default, the backup filenames in
§11. A host in local time turns "which day did that happen" into a
conversion.

Expected: **no output.**

```bash
timedatectl | grep -E 'Time zone|System clock'
```

Expected:

```
                Time zone: Etc/UTC (UTC, +0000)
System clock synchronized: yes
```

> **Note:** unattended-upgrades does not reboot by default, so a kernel
> update sits applied-but-not-active until you reboot. Check with
> `ls /var/run/reboot-required` — if that file exists, reboot when
> convenient. Automatic reboots are deliberately **not** enabled: this
> host runs a database with no replica, and a 3am reboot during an
> ingestion is a worse outcome than a kernel update applied a day late.

---

## 9. GitHub secrets, for CI/CD

**All seven go in the `production` GitHub *Environment*, as Environment
secrets — not repository secrets.** Settings → Environments → **New
environment** → name it exactly `production`, then **Add environment
secret** seven times.

`cd.yml`'s deploy job declares `environment: production`, so an
environment secret is both scoped to that one job and gated by whatever
protection rules you add later (required reviewers, a wait timer, a
branch restriction) without editing the workflow. A repository secret is
readable by *every* workflow run in the repo, including one added by a
future pull request, and no protection rule applies to it.

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | the static IP, or your DuckDNS hostname |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | contents of `~/.ssh/documind_deploy` (the **private** half, no passphrase — see §4) |
| `DEPLOY_KNOWN_HOSTS` | output of `ssh-keyscan -H <STATIC_IP>` |
| `SMOKE_ORG`, `SMOKE_EMAIL`, `SMOKE_PASSWORD` | from §6 |

`GITHUB_TOKEN` is provided automatically and is what pushes to GHCR —
you do not create it.

> **Known gap:** `cd.yml` does not forward `SMOKE_*` to the server, and
> `scripts/smoke_test.sh` reads them from its own environment. Until that
> is wired, the automated deploy's login check prints
> `SKIP login: SMOKE_ORG / SMOKE_EMAIL / SMOKE_PASSWORD are not set`
> rather than failing. Set them on the server too (in `.env.production`,
> or an env file `deploy.sh` sources) if you want that check to run
> unattended.

`DEPLOY_KNOWN_HOSTS` is not optional. Without it the deploy step would
need `StrictHostKeyChecking=no`, which accepts any host key presented
and makes the deploy MITM-able.

```bash
ssh-keyscan -H <STATIC_IP>     # run on your laptop, paste the output
```

### Restrict the deploy key (recommended)

Limit what that key can do, so a stolen key cannot become a shell. This
matters more because the key has no passphrase (§4):

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

## 11. Backups, and a restore drill (P6)

Right now the named volumes are the **only** copy of every uploaded
document and the entire database. There is no replica and no snapshot.

Two scripts, run as `deploy`:

- `scripts/backup.sh` — a compressed custom-format `pg_dump` plus a tar
  of the uploads volume, into a 0700 directory, 7 days retained.
- `scripts/restore_drill.sh` — restores a dump into a **throwaway**
  container and compares row counts against production.

> **A backup on the same disk as the thing it backs up is not a
> backup.** It survives `DROP TABLE`, a bad migration and a deleted
> document. It does not survive the instance being deleted, the volume
> failing, or the account being lost. §11.6 is the decision that fixes
> that, and it needs your input.

### 11.1 [server] Run a backup by hand first

Never schedule something you have not watched work.

```bash
cd /opt/documind && bash scripts/backup.sh
```

Expected — sizes will differ; the archive entry count will not be 0:

```
[backup 02:00:01] dumping documind
[backup 02:00:04] dump OK: 148K, 212 archive entries
[backup 02:00:04] archiving the uploads volume
[backup 02:00:06] uploads OK: 12K
[backup 02:00:07] uploads archive contains 3 file(s)
[backup 02:00:07] pruning backups older than 7 days
[backup 02:00:07] done. 1 database backup(s) retained in /home/deploy/backups
```

**`uploads archive contains 0 file(s)` is legitimate** if nothing has
been uploaded yet — but it is printed on purpose, because "backup
succeeded" over an empty archive is exactly how a broken volume mount
hides.

If it says `pg_dump failed (is the stack up?)`, the database container
is not running. Check with §6.6's `ps`.

### 11.2 [server] Confirm what landed, and its permissions

```bash
ls -l /home/deploy/backups
```

Expected — two files, both `-rw-------`:

```
total 164
-rw------- 1 deploy deploy 151552 Nov  8 02:00 documind-db-20261108T020001Z.dump
-rw------- 1 deploy deploy  12408 Nov  8 02:00 documind-uploads-20261108T020001Z.tar.gz
```

```bash
stat -c '%a %U %n' /home/deploy/backups
```

Expected — `700`, owned by `deploy`:

```
700 deploy /home/deploy/backups
```

A dump contains every row of every tenant's data. It is the single most
sensitive file on this box — more than `.env.production`, which only
holds the keys to it.

### 11.3 [server] Run the restore drill

This is the step that turns a file into a backup.

```bash
cd /opt/documind && bash scripts/restore_drill.sh
```

With no argument it picks the newest dump. Pass a path to drill a
specific one.

Expected:

```
[drill 02:05:00] restoring documind-db-20261108T020001Z.dump (148K)
[drill 02:05:00] reading production row counts
[drill 02:05:01] starting a throwaway postgres (publishes no port, anonymous volume)
[drill 02:05:03] waiting for it to accept connections
[drill 02:05:09] running pg_restore
[drill 02:05:11] comparing row counts

  table                  production     restored   verdict
  --------------------   ----------     --------   -------
  organizations                   2            2   ok
  users                           2            2   ok
  documents                       3            3   ok
  document_chunks                14           14   ok
  conversations                   4            4   ok
  messages                       18           16   older than prod
  usage_events                   22           20   older than prod

[drill 02:05:11] restored schema version: 0009
[drill 02:05:11] RESTORE DRILL PASSED. Production volumes were never touched.
```

**`older than prod` is not a failure.** Production keeps accepting
writes after the dump is taken, so a live system will almost always show
fewer rows in the restore. It is printed so the gap is visible rather
than averaged away.

Three things **are** failures, and the drill exits non-zero on each:

| verdict | meaning |
|---|---|
| `MISSING TABLE` | the table did not restore at all |
| `MORE THAN PROD` | more rows than production has — impossible from a dump of it, so the dump or the comparison is wrong |
| `EMPTY, PROD IS NOT` | the table restored with no rows while production has some |

It also fails if `alembic_version` is missing: a dump restored into the
wrong code generation is a different kind of broken that row counts
cannot see.

**What the drill never touches:** it starts its container with
`docker run`, not through the compose file, so there is no project, no
named volume and no network it could join by accident. Its storage is an
anonymous volume that dies with the container, it publishes no port, and
`tests/test_backup_scripts.py::test_the_drill_never_names_a_production_volume`
asserts the script cannot even name `documind-prod_db_data`.

### 11.4 [server] Schedule it nightly

```bash
crontab -e
```

Choose `nano` if asked. Add these two lines:

```
# Nightly at 02:17 UTC. Not 02:00 -- every cron job in the world runs on
# the hour, and this box shares a hypervisor.
17 2 * * * cd /opt/documind && bash scripts/backup.sh >> /home/deploy/backup.log 2>&1
# A restore drill every Sunday. A backup nobody has restored is a file.
41 3 * * 0 cd /opt/documind && bash scripts/restore_drill.sh >> /home/deploy/backup.log 2>&1
```

Save and exit. Expected:

```
crontab: installing new crontab
```

**`deploy`'s own crontab, not a systemd timer and not root's.**
Installing a system unit needs root, and `deploy` deliberately has no
`sudo` (§4.4). A systemd *user* timer would need
`loginctl enable-linger deploy`, which also needs root. A user crontab
needs neither and survives reboots.

Confirm it is installed:

```bash
crontab -l
```

Expected: the two lines you just added.

### 11.5 [server] Confirm cron actually ran, tomorrow

Scheduling is not running. Check the day after:

```bash
tail -20 /home/deploy/backup.log
```

Expected: a `done.` line dated last night.

```bash
ls -l /home/deploy/backups | tail -3
```

Expected: a dump with yesterday's date in its name.

**If the log is empty**, cron did not fire. The usual cause is that
`cron` is not running (`systemctl is-active cron` → `active`), or the
job failed before producing output — the log captures both stdout and
stderr, so an empty file means it never started.

### 11.6 The off-box copy — your decision

Everything above lives on the same 60 GB disk as the database. Three
options, costed for a free-tier project. **Pick one and I will write
it up.**

**Option A — pull to your laptop over scp (free).**

A scheduled task on your *laptop* pulls the newest files. The laptop
pulls rather than the server pushing, because the server cannot reach a
machine behind home NAT, and because a server that can write to your
laptop is a wider blast radius than one that cannot.

```
scp -i "%USERPROFILE%\.ssh\documind_deploy" deploy@documind-prod.duckdns.org:/home/deploy/backups/* D:\documind-backups\
```

- **Cost:** nothing.
- **For:** genuinely off-AWS, so it survives losing the account. You can
  restore from it with nothing but Docker.
- **Against:** only happens when the laptop is on. No retention policy
  unless you write one. The dumps land on a machine that is probably not
  encrypted at rest.

**Option B — Lightsail automatic snapshots (~$0.25–0.50/month).**

Lightsail takes a daily snapshot of the whole instance and keeps 7.

- **Cost:** $0.05/GB-month of *used* disk, incremental after the first.
  At ~5 GB used that is roughly $0.25/month, rising as documents
  accumulate.
- **For:** zero scripting, zero maintenance, covers the OS and the
  config as well as the data. Restoring is "create instance from
  snapshot", which also rehearses a total rebuild.
- **Against:** it is in the same AWS account and the same region, so it
  does not protect against account loss or a region event. Whole-instance
  granularity — you cannot pull one table out of it. It is a *second*
  backup mechanism, not the one these scripts produce.

**Option C — Cloudflare R2 or Backblaze B2 (free tier, then cheap).**

`rclone` from the nightly cron to object storage. R2's free tier is 10 GB
and charges no egress.

- **Cost:** £0 until 10 GB.
- **For:** proper off-site, automatic, retained, and restorable from
  anywhere.
- **Against:** another credential on the box — and it must be
  **write-only scoped**, or a compromised server can delete the backups
  as well as the data. That is the whole reason this is not the default
  recommendation: it adds the exact failure mode backups exist to
  survive.

**My recommendation: A and B together.** B is the cheap insurance that
needs no discipline, A is the copy that survives losing the AWS account,
and between them a failure of either one is not a total loss. C is the
right answer once this stops being a free-tier project, with a
write-only key.

### 11.7 What is still not covered

- **No point-in-time recovery.** A nightly dump means up to 24 hours of
  loss. PITR needs WAL archiving and somewhere to put it; that is a
  different design, not a setting.
- **The restore drill does not test the uploads archive.** It verifies
  the tar is readable and counts its files (§11.1), but nothing checks
  that a restored document still ingests. A document whose bytes come
  back but whose `document_chunks` rows do not would present as a
  citation pointing at a passage that no longer exists.
- **Nothing alerts on a failed backup.** The cron job writes to a log
  nobody reads. P8's observability work is where a failed backup should
  become a notification; until then, §11.5 is a manual habit.

---

## Not covered here

- **The off-box copy (P6).** §11 covers the nightly dump, retention and
  the restore drill; where the copy goes is still an open decision --
  see §11.6.
- **Observability (P8).** Structured JSON logs with a request id, an
  external uptime monitor on `/health`, an AWS Budgets alert.
- **Vercel hand-off (P9)** and the outside verification list **(P10)**.
- The **BFF client-IP problem** — see `docs/adr/0006`. Through a BFF,
  Caddy sees the BFF host, so the per-IP limiter becomes
  per-deployment. Recorded, not solved.
