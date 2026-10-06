# Home Lab v2

A small applied-AI platform that applications build on: **model access** (one place that holds
provider keys and maps purposes to models), **knowledge** (ingest messy sources, retrieve with
provenance) and **durable jobs** (work that survives a reboot and can wait). Applications —
a Telegram bot, a Second Brain UI, an autonomous dev team — are clients of its API, never part of it.

It runs on one inexpensive always-on node (a used Lenovo M700 Tiny) with Docker Compose, uses
[Vercel AI Gateway](https://vercel.com/ai-gateway) for hosted models and `llama.cpp` for local
embeddings, reranking and quality-first batch work. A company could take it, point it at its own
providers, sources and infrastructure, and build its own applications against the same API.

This is the second attempt. [`homelab-v1`](https://github.com/TelesforoAleix/homelab-v1) is the
archived, learning-first build that taught the layers by hand; v2 keeps its security posture and
its server, uses mature libraries for the machinery, and optimises for building useful things.

## Status

The node runs v2. The retired v1 programs, units, configuration, state, accounts and checkouts
have been removed; Brain and v2's volume data remain. Failure notifications accept only `bot`,
`watchdog` and `homelab`.

The FastAPI service exposes health, an ingestion trigger, top-k retrieval with provenance and
answers grounded in retrieved knowledge with numbered citations.
`homelab ingest` indexes the active collection incrementally: public entries from the
about-Aleix JSON corpus by default, or included Brain Markdown. Procrastinate provides a
durable Postgres-backed worker, and Docker Compose defines the loopback-only stack.

## Run it

For a fresh node, follow [Rebuilding the node](#rebuilding-the-node). On the running node,
connect with `ssh homelab` and unlock with `sudo data-volume.sh unlock` after each boot.

Host files live under `node/`: `node/<path>` installs at `/<path>`, owned by `root:root`.
[AGENTS.md](AGENTS.md#host-file-modes) records directory and file modes. For updates, use the
node's clone of merged `main`: back up installed files, show each diff, copy changed files with
`sudo install`, and run `visudo -c`, `sshd -t` and `systemd-analyze --generators=yes verify`
where applicable. Reload systemd after unit changes. Credential drop-ins name TPM-sealed
credentials; credential contents are never committed. A PR changing `node/` updates the
rebuild section in the same PR. The base files preserve their installed bytes, including
historical v1 comments; those references do not require v1 scripts or a node GitHub key.

The agent uses `ssh homelab-agent`, which has root through sudo. Proceed within the owner's
approved task; the exceptions requiring approval are in `AGENTS.md`. Deploy with
`sudo systemctl restart homelab.service`; it pulls public `main` over HTTPS and builds images.
Host-file installation is explicit; restarting the stack does not install those files.

The Telegram bot stays a stdlib-only host service running as `homelab-bot`. Its code lives at
`node/opt/homelab-telegram-bot/`, installed at `/opt/homelab-telegram-bot/`. `/ask <question>`
calls the loopback answer endpoint and replies with the answer and numbered source titles;
refusals have no sources. Long replies are split into Telegram-sized messages. When the volume
is locked or the API is unavailable, `/ask` reports that the knowledge service is down.
`/status`, `/disk`, `/uptime`, `/restart`, `/help` and `/start` keep working independently.
`/spend` and `/model` are no longer commands. Startup registers the router's command list and
description with Telegram; registration failure is logged and polling continues.

Private bot configuration stays on the node under `/etc/homelab-telegram-bot/`: `allowlist`
and `privileged-allowlist` contain numeric Telegram user IDs, one per line; `restart-allowlist`
contains unit names, one per line (a missing `.service` suffix is added). All three ignore blank
lines and `#` comments, including trailing comments, and use owner/group `root:homelab-bot` with
mode `0640`. The main allowlist must be nonempty; privileged users must be a subset of it.
A missing or empty privileged or restart allowlist grants no capability. Paths can be overridden
by `HOMELAB_BOT_ALLOWLIST`, `HOMELAB_BOT_PRIVILEGED_ALLOWLIST` and
`HOMELAB_BOT_RESTART_ALLOWLIST`. Their contents never enter git, tests or logs. The token stays
in TPM-sealed `token.cred`; the bot reads only systemd's `$CREDENTIALS_DIRECTORY/bot-token`.

`POST /v1/knowledge/query` and `POST /v1/knowledge/answer` accept `{"question": "…", "top_k": 5}`
(nonblank question; integer `top_k` at least 1, default 5). Query returns scored chunks with
provenance. Answer retrieves the same chunks and makes one call to the configured `chat` route.
It returns `answer`, `refused` and `sources`: each cited chunk's `number`, `title`, `path` and
`score`, in first-citation order. Insufficient evidence produces a plain refusal with no sources;
malformed model replies or missing/invalid citations also fail closed. The refusal text is
“That isn't covered in what Aleix has written here — you can ask him directly.” Answers always
refer to Aleix by name in the third person. Metrics record lengths, counts, refusal and timing,
without question, chunk or answer content.

Knowledge operations share `HOMELAB_ACTIVE_COLLECTION` (`about_aleix` by default, or `brain`).
Each collection has separate chunk and document tables; switching does not rebuild Brain.
The corpus is mounted read-only from `${HOMELAB_CORPUS_PATH:-/srv/homelab/corpus}` at
`/data/corpus`. `HOMELAB_CORPUS_FILE` defaults to `/data/corpus/about-aleix/corpus.json`.
Public JSON entries become documents whose titles are their questions and whose provenance
paths are stable entry ids. Other visibility values are skipped. Invalid individual entries
are skipped with position-only warnings; invalid JSON, a non-list root or duplicate public ids
abort ingestion before writing.

`homelab eval` reads the same corpus and asks every public entry's own question against
`about_aleix`. It prints only document count, hit@5, mean reciprocal rank (MRR), retrieval miss
ids, refusal count and refusal ids. Answers use the same path as the answer endpoint; no eval
file is written. When `HOMELAB_EVAL_FILE` (default
`/data/corpus/about-aleix/eval.json`) exists in the read-only corpus mount, it also scores
`bank_paraphrases`, `refusal_checks` and `visitor`, with visitor results per group. Answer items
measure hit@5 and MRR against any listed entry, and answer correctness requires a non-refusal
with a listed source path. Refusal items measure correct refusals. Failures carry ids and
`retrieval_miss`, `refused`, `wrong_citation` or `answered` reasons; multiple reasons can apply.
The baseline, each set and each visitor group also report `first_person_count`: non-refused
answers containing `I`, `me`, `my` or `mine` as whole words, ignoring case. Each answer counts
once, including quoted phrases; no text or match details are reported.
The baseline fields stay at the JSON root, independent scores are under `sets`, and numeric
`eval_file_found` is 0 when the file was not found (baseline only), 1 otherwise. Empty subsets
have zero rates. No question or answer text is printed. Run it inside the app container on the
node after ingestion.

Models are served by LiteLLM using `config/litellm.yaml`, the single route table. The API,
worker, ingestion CLI and eval discover purposes from its `/v1/models` endpoint and use those
names through the existing LlamaIndex clients. They hold only `HOMELAB_MODELS_BASE_URL`
(default `http://litellm:4000/v1`) and `HOMELAB_MODELS_API_KEY` (default `homelab`, accepted
and ignored today). The gateway secret is mounted only into LiteLLM, which reads it at startup;
its existing `root:10001` ownership and `0440` mode stay unchanged.

On the Mac, development needs no services:

```bash
uv sync
uv run pytest
```

On a Docker host, provision `.env` and the secret as described in step 10, then run:

```bash
docker compose up -d --build
docker compose exec api homelab routes
docker compose exec api homelab eval
```

`/health` returns `status`, `version`, `routes` and `models_base_url`; the former
`gateway_key_present` field retires because the API no longer holds the gateway key. No host
port is published for LiteLLM. Its official image is pinned by digest in Compose and updated
deliberately. Telemetry, remote model-price updates, hosted callbacks and the admin UI are off;
tokenizers are bundled. Its external model calls go only to Vercel AI Gateway; embeddings go
to the existing `llama-embed` service. The journal records one line per endpoint call with
purpose, real model, HTTP status, elapsed seconds and whether an upstream call was attempted.
Content, embeddings and keys are excluded, including on errors and streaming calls.

### Use the model endpoint

Attach a client container from another Compose project to the external Docker network
`homelab-models`, created by this stack. Only LiteLLM joins it; Postgres, API and `llama-embed`
remain on the stack's private network. Give the client its base URL `http://litellm:4000/v1`
and an arbitrary API key through its environment, then use OpenAI-shaped `GET /v1/models`,
`POST /v1/chat/completions` and `POST /v1/embeddings`. There are exactly four names: `chat`
(standard), `chat:high`, `chat:xhigh` and `embed`. The client chooses a tier; no escalation
or fallback happens automatically. Chat responses report the purpose; embedding responses
report the real embedding model, which clients must record at ingest. An embedding model
change requires a new index. Unknown names, provider model ids and unavailable tiers receive
a 4xx without an upstream call. The accepted client key provides no access control today:
attach only trusted clients to this network. Provider/routing overrides and remote image URLs
are refused; configuration and management endpoints are not exposed. New purposes and optional
OpenAI fields can be added to `/v1`; breaking changes require `/v2`.

A client project's network declaration is:

```yaml
services:
  client:
    # image, environment and command belong to the client project
    networks: [models]
networks:
  models:
    external: true
    name: homelab-models
```

## Rebuilding the node

Recovery is a rebuild from sources. No node backup exists or will be made. Host files come
from `node/`, code from this public GitHub repository, and Brain notes from their own GitHub
repository through the owner's Mac clone. Brain and the corpus are copied from the Mac;
Postgres is derived by re-ingesting, models are fetched upstream, and secrets are re-issued.
The rebuilt node holds no GitHub credential and never clones Brain.

This procedure describes the node verified on 2026-10-05: Ubuntu 26.04.1 LTS, kernel 7.0,
systemd 259, Docker 29.8 with Compose v5.5, and Tailscale 1.102. Vendor apt repositories supply
Docker and Tailscale; check the versions available when rebuilding. These commands have not
been rehearsed. Run them only during an owner-authorized rebuild, stopping on any error.
Commands run on the node as `aleix` unless marked **on the Mac**. Replace angle-bracket
placeholders locally; never commit their values. Keep the local console available until both
SSH accounts work through Tailscale.

1. **Install Ubuntu and choose the system disk.** In the Ubuntu Server 26.04.1 LTS installer,
   select the **Samsung MZ7TY256, 238.5 GiB**, by model and size. Never identify the target by
   `sda` or `sdb`: kernel names can swap between boots. Leave the **Micron MTFDDAV512TBN,
   476.9 GiB**, deliberately unused, untouched. **Choosing the wrong disk in the installer
   is the one irreversible mistake in this procedure: the installer destroys that disk's
   data.** Check the model and size again before confirming the partition changes.

   Use three partitions on the Samsung: 1 GiB EFI at `/boot/efi`, 2 GiB at `/boot`, and the
   remainder as an LVM physical volume in `ubuntu-vg`. Create only `ubuntu-lv`, 64 GiB,
   mounted at `/`; leave at least 128 GiB free in the VG for step 2. Create the owner's
   `aleix` login with sudo access and select OpenSSH server. Configure Wi-Fi on `wlp1s0`
   with the Wi-Fi name and passphrase from the owner's password manager. Ubuntu's installer
   writes the private netplan; it is never copied into this repository. `eno1` is optional.
   After boot, verify the target layout and install the host utilities used below:

   ```bash
   lsblk -o NAME,MODEL,SIZE,TYPE,MOUNTPOINTS
   sudo vgs ubuntu-vg
   sudo lvs -o lv_name,lv_size ubuntu-vg
   sudo apt-get update
   sudo apt-get install ca-certificates curl git rsync openssh-server lvm2 cryptsetup ufw iw chrony polkitd python3 openssl
   ```

2. **Create, format and register the encrypted data volume.** These commands apply only to
   the new, empty `data` LV in the Samsung's `ubuntu-vg`; do not run them on an existing
   volume. Create a new data-volume passphrase in the owner's password manager and enter
   it at cryptsetup's hidden prompts. No keyfile or UUID is recorded here.

   ```bash
   sudo lvcreate -L 128G -n data ubuntu-vg
   sudo cryptsetup luksFormat --type luks2 /dev/ubuntu-vg/data
   sudo cryptsetup open --allow-discards /dev/ubuntu-vg/data homelab-data
   sudo mkfs.ext4 /dev/mapper/homelab-data
   sudo cryptsetup close homelab-data
   sudo install -d -m 0755 -o root -g root /srv/homelab
   sudoedit /etc/crypttab /etc/fstab
   ```

   Add exactly one entry to each file, leaving Ubuntu's existing entries intact:

   ```text
   # /etc/crypttab
   homelab-data  /dev/ubuntu-vg/data  none  luks,noauto,discard
   # /etc/fstab
   /dev/mapper/homelab-data  /srv/homelab  ext4  noauto,nofail,x-systemd.device-timeout=10s  0  2
   ```

3. **Install and join Tailscale.** Use the [vendor's apt repository](https://pkgs.tailscale.com/stable/)
   for Ubuntu Resolute. Authenticate through the URL printed by `tailscale up`, using the
   owner's Tailscale account. The access policy lives in the admin console: verify that it
   permits the owner's Mac to reach this replacement node. Its policy, tailnet name, email
   and addresses stay outside this repository. Update the Mac's local `homelab` and
   `homelab-agent` SSH aliases to the replacement node's Tailscale hostname or address.

   ```bash
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/resolute.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
   curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/resolute.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
   sudo apt-get update
   sudo apt-get install tailscale
   sudo systemctl enable --now tailscaled.service
   sudo tailscale up
   tailscale version
   tailscale status
   ```

4. **Install Docker and Compose.** Use [Docker's Ubuntu apt instructions](https://docs.docker.com/engine/install/ubuntu/).
   On this fresh Ubuntu install, omit distro Docker packages. The signed repository below
   is the installed node's `docker.list` form of the vendor's repository configuration.

   ```bash
   sudo install -d -m 0755 /etc/apt/keyrings
   sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   sudo chmod 0644 /etc/apt/keyrings/docker.asc
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
   sudo apt-get update
   sudo apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   sudo docker version
   sudo docker compose version
   ```

5. **Create the accounts and their restricted SSH keys.** The owner is in `sudo` and
   `docker`. The agent has an interactive shell and journal access; its root sudo grant
   is installed in step 6. The bot is a system account with no login and no Docker group.

   ```bash
   sudo usermod -aG sudo,docker aleix
   sudo useradd --system --user-group --create-home --home-dir /home/homelab-agent --shell /bin/bash homelab-agent
   sudo usermod -aG systemd-journal homelab-agent
   sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin homelab-bot
   sudo install -d -m 0700 -o aleix -g aleix /home/aleix/.ssh
   sudo install -d -m 0700 -o homelab-agent -g homelab-agent /home/homelab-agent/.ssh
   sudo install -m 0600 -o aleix -g aleix /dev/null /home/aleix/.ssh/authorized_keys
   sudo install -m 0600 -o homelab-agent -g homelab-agent /dev/null /home/homelab-agent/.ssh/authorized_keys
   sudoedit /home/aleix/.ssh/authorized_keys /home/homelab-agent/.ssh/authorized_keys
   ```

   Put the owner's public login key from the Mac in the owner's file. In the agent's file,
   use its public key from the Mac with the restriction shown below, substituting the Mac's
   Tailscale addresses locally. Keep both `authorized_keys` files and all key material out
   of git. No outgoing GitHub key or GitHub SSH config is needed on the node.

   ```text
   from="<MAC-TAILSCALE-IPV4>,<MAC-TAILSCALE-IPV6>" <AGENT-PUBLIC-KEY>
   ```

   The installer generates new SSH host keys. Check their fingerprint at the node's console:

   ```bash
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   **On the Mac**, remove only the old known-host entry for the replacement node, compare
   the new fingerprint with the console, and test both logins before applying hardening or ufw:

   ```bash
   ssh-keygen -R '<NODE-TAILSCALE-HOST-OR-ADDRESS>'
   ```

   ```bash
   ssh homelab 'id'
   ssh homelab-agent 'id'
   ```

6. **Apply `node/` with its recorded modes.** Before the encrypted-volume clone exists,
   download the public merged `main` source archive to the owner's home. This is a bootstrap
   copy of committed files, not an installer script. Inspect each difference before copying;
   for an absent destination compare against `/dev/null`. The diff loop exits on an inspection
   error; exit status 1 from `diff` means a displayed difference. Keep vendor stock
   `/etc/ufw/before.rules`. Do not install the stale SSH `.bak-2026-09-12` file.

   ```bash
   mkdir -p "$HOME/homelab-host"
   curl -fsSL https://github.com/TelesforoAleix/homelab-v2/archive/refs/heads/main.tar.gz | tar -xz -C "$HOME/homelab-host" --strip-components=1
   cd "$HOME/homelab-host"
   while IFS= read -r source; do
     destination="/${source#node/}"
     if sudo test -e "$destination"; then
       sudo diff -u "$destination" "$source" || test "$?" -eq 1 || exit 2
     else
       diff -u /dev/null "$source" || test "$?" -eq 1 || exit 2
     fi
   done < <(find node -type f | sort)
   ```

   Run the following only after reviewing all diffs. Directory and file modes are also
   recorded in `AGENTS.md`. Preserve the vendor's `root:polkitd` ownership and `0750` mode
   on `/etc/polkit-1/rules.d`; the rule itself is `root:root` `0644`. All other directories
   below are `root:root` `0755`. The firewall files are `0640`, SSH hardening is `0600`,
   sudoers is `0440`, operational scripts are `0755`, and the remaining files are `0644`.

   ```bash
   sudo install -d -m 0755 -o root -g root /etc/ssh /etc/ssh/sshd_config.d /etc/ufw /etc/docker /etc/sysctl.d /etc/profile.d /etc/sudoers.d /usr/local/sbin /opt/homelab-telegram-bot /etc/systemd/system
   sudo install -d -m 0755 -o root -g root /etc/systemd/system/homelab-notify@.service.d /etc/systemd/system/homelab-watchdog.service.d /etc/systemd/system/homelab-telegram-bot.service.d
   sudo install -d -m 0750 -o root -g polkitd /etc/polkit-1/rules.d
   sudo install -m 0600 -o root -g root node/etc/ssh/sshd_config.d/10-homelab-hardening.conf /etc/ssh/sshd_config.d/10-homelab-hardening.conf
   sudo install -m 0640 -o root -g root node/etc/ufw/after.rules node/etc/ufw/after6.rules node/etc/ufw/user.rules node/etc/ufw/user6.rules /etc/ufw/
   sudo install -m 0644 -o root -g root node/etc/docker/daemon.json /etc/docker/daemon.json
   sudo install -m 0644 -o root -g root node/etc/sysctl.d/99-homelab-swappiness.conf /etc/sysctl.d/99-homelab-swappiness.conf
   sudo install -m 0644 -o root -g root node/etc/profile.d/homelab-console-timeout.sh /etc/profile.d/homelab-console-timeout.sh
   sudo visudo -cf node/etc/sudoers.d/homelab-agent-v2
   sudo install -m 0440 -o root -g root node/etc/sudoers.d/homelab-agent-v2 /etc/sudoers.d/homelab-agent-v2
   sudo install -m 0755 -o root -g root node/usr/local/sbin/*.sh /usr/local/sbin/
   sudo install -m 0644 -o root -g root node/opt/homelab-telegram-bot/*.py /opt/homelab-telegram-bot/
   sudo install -m 0644 -o root -g root node/etc/polkit-1/rules.d/50-homelab-bot.rules /etc/polkit-1/rules.d/50-homelab-bot.rules
   sudo install -m 0644 -o root -g root node/etc/systemd/system/*.service node/etc/systemd/system/*.timer node/etc/systemd/system/*.target /etc/systemd/system/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-notify@.service.d/*.conf /etc/systemd/system/homelab-notify@.service.d/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-watchdog.service.d/*.conf /etc/systemd/system/homelab-watchdog.service.d/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-telegram-bot.service.d/*.conf /etc/systemd/system/homelab-telegram-bot.service.d/
   sudo visudo -c
   sudo sshd -t
   sudo systemd-analyze --generators=yes verify /etc/systemd/system/homelab.service /etc/systemd/system/homelab-data.target /etc/systemd/system/homelab-notify@.service /etc/systemd/system/homelab-watchdog.service /etc/systemd/system/homelab-watchdog.timer /etc/systemd/system/homelab-telegram-bot.service /etc/systemd/system/wifi-powersave-off.service
   ```

   `--generators=yes` supplies the mount unit from fstab during verification. Vendor units
   may warn about removed directives; resolve failures in the recorded units before proceeding.

7. **Enable the host units, firewall and sysctl.** Keep a console and an existing SSH session
   open while reloading SSH and enabling ufw; verify a fresh connection from the Mac afterward.
   The recorded rules allow `tailscale0` and Wi-Fi 41641/udp, and the `DOCKER-USER` chain
   drops inbound Docker traffic from `wlp1s0` and `eno1` for IPv4 and IPv6. Leave ufw's stock
   `IPV6=yes` and `MANAGE_BUILTINS=no` in place.

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl reload ssh.service
   sudo systemctl enable --now docker.service tailscaled.service chrony.service wifi-powersave-off.service
   sudo systemctl restart docker.service
   sudo systemctl enable homelab-telegram-bot.service homelab-watchdog.timer
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw default deny routed
   sudo ufw --force enable
   sudo ufw status verbose
   sudo iptables -S DOCKER-USER
   sudo ip6tables -S DOCKER-USER
   sudo sysctl -p /etc/sysctl.d/99-homelab-swappiness.conf
   iw dev wlp1s0 get power_save
   sudo mkdir -p /var/log/journal
   sudo systemd-tmpfiles --create --prefix /var/log/journal
   sudo journalctl --flush
   ```

   Persistent journaling lets the watchdog classify the previous boot. Bot and watchdog are
   enabled but not started before credentials exist; the watchdog timer first runs on the
   validation reboot. Defer enabling `homelab.service` until step 12: unlocking starts the
   data target, and the stack must not start before its clone and secrets exist. Do not enable
   `homelab-data.target` or `homelab-notify@.service`; the unlock script and failure handlers
   start them. Console logins now expire after 15 idle minutes; SSH sessions do not.
   **On the Mac**, verify the hardened logins and agent sudo access:

   ```bash
   ssh homelab 'id'
   ssh homelab-agent 'sudo -n true'
   ```

8. **Unlock the volume.** Enter its new passphrase from the owner's password manager.
   This is the same manual step required after every boot.

   ```bash
   sudo data-volume.sh unlock
   findmnt /srv/homelab
   ```

9. **Clone this repository and copy Brain from the owner's Mac.** The public code clone is
   root-owned and uses HTTPS without a GitHub credential. Brain is a copy of the Mac clone's
   working notes, excluding `.git`; keep the private repository URL and GitHub access on
   the Mac. Bring the Mac clone up to date before copying. The Brain directory stays
   owner-owned, and containers mount it read-only. Copy modes let the app UID read the notes.

   ```bash
   sudo git clone --branch main https://github.com/TelesforoAleix/homelab-v2.git /srv/homelab/homelab-v2
   sudo install -d -m 0775 -o aleix -g aleix /srv/homelab/brain
   sudo install -d -m 0755 -o root -g root /srv/homelab/models /srv/homelab/postgres
   ```

   **On the Mac**, copy notes, including hidden note files but excluding Git metadata:

   ```bash
   rsync -a --chmod=D755,F644 --exclude='.git' '<MAC-BRAIN-CLONE>/' homelab:/srv/homelab/brain/
   ```

10. **Re-issue the secrets and provision the private allowlists.** Create a fresh gateway
    key with the owner's gateway account and budget, a random hexadecimal database password
    in the password manager, and a new Telegram bot token through BotFather. Enter keys and
    the password only at hidden prompts; never put them in shell arguments or history.
    `.env` holds the database password and three volume paths and is `root:root` `0600`.
    The gateway key is `root:10001` `0440`, readable only by the non-root LiteLLM container.

    ```bash
    sudo install -d -m 0700 -o root -g root /srv/homelab/homelab-v2/secrets
    sudo bash -c 'umask 077; read -r -s -p "new gateway key: " K </dev/tty; echo; test -n "$K" || exit 1; printf "%s" "$K" > /srv/homelab/homelab-v2/secrets/gateway_api_key'
    sudo chown root:10001 /srv/homelab/homelab-v2/secrets/gateway_api_key
    sudo chmod 0440 /srv/homelab/homelab-v2/secrets/gateway_api_key
    sudo bash -c 'umask 077; read -r -s -p "new hexadecimal database password: " P </dev/tty; echo; [[ "$P" =~ ^[[:xdigit:]]{32,}$ ]] || exit 1; printf "%s\n" "HOMELAB_DB_PASSWORD=$P" "HOMELAB_BRAIN_PATH=/srv/homelab/brain" "HOMELAB_POSTGRES_PATH=/srv/homelab/postgres" "HOMELAB_MODELS_PATH=/srv/homelab/models" > /srv/homelab/homelab-v2/.env'
    sudo chown root:root /srv/homelab/homelab-v2/.env
    sudo chmod 0600 /srv/homelab/homelab-v2/.env
    sudo install -d -m 0750 -o root -g homelab-bot /etc/homelab-telegram-bot
    sudo bash -c 'umask 077; read -r -s -p "new Telegram bot token: " T </dev/tty; echo; test -n "$T" || exit 1; printf "%s" "$T" | systemd-creds encrypt --name=bot-token --with-key=tpm2 --tpm2-pcrs="" - /etc/homelab-telegram-bot/token.cred'
    sudo chown root:root /etc/homelab-telegram-bot/token.cred
    sudo chmod 0600 /etc/homelab-telegram-bot/token.cred
    sudo install -m 0640 -o root -g homelab-bot /dev/null /etc/homelab-telegram-bot/allowlist
    sudo install -m 0640 -o root -g homelab-bot /dev/null /etc/homelab-telegram-bot/privileged-allowlist
    sudo install -m 0640 -o root -g homelab-bot /dev/null /etc/homelab-telegram-bot/restart-allowlist
    sudoedit /etc/homelab-telegram-bot/allowlist /etc/homelab-telegram-bot/privileged-allowlist /etc/homelab-telegram-bot/restart-allowlist
    sudo chown root:homelab-bot /etc/homelab-telegram-bot/allowlist /etc/homelab-telegram-bot/privileged-allowlist /etc/homelab-telegram-bot/restart-allowlist
    sudo chmod 0640 /etc/homelab-telegram-bot/allowlist /etc/homelab-telegram-bot/privileged-allowlist /etc/homelab-telegram-bot/restart-allowlist
    ```

    Enter numeric Telegram user/chat IDs obtained by the owner through Telegram locally,
    one per line in `allowlist`, with at least one recipient. `privileged-allowlist` may be
    empty; any IDs entered must also be in `allowlist`. `restart-allowlist` may be empty;
    `chrony.service` is the only restart permitted by the recorded polkit rule. The three
    files are private deployment settings, not sources recovered from the node. All secret
    values, addresses, tailnet names, emails, Wi-Fi names, chat IDs and UUIDs stay outside
    the repository. Only the owner-approved login name appears in SSH hardening.
    The token is TPM2-sealed without PCR binding; no plaintext token file is created.

11. **Copy the corpus from the owner's Mac.** Create the destination on the unlocked volume:

    ```bash
    sudo install -d -m 0755 -o root -g root /srv/homelab/corpus
    sudo install -d -m 0755 -o aleix -g aleix /srv/homelab/corpus/about-aleix
    ```

    **On the Mac**, copy `corpus.json` and `eval.json` directly onto the encrypted volume:

    ```bash
    scp '<MAC-CORPUS-DIRECTORY>/corpus.json' '<MAC-CORPUS-DIRECTORY>/eval.json' homelab:/srv/homelab/corpus/about-aleix/
    ```

    Back **on the node**, make the installed corpus root-owned and readable by the app.
    Compose mounts it read-only.

    ```bash
    sudo chown root:root /srv/homelab/corpus/about-aleix /srv/homelab/corpus/about-aleix/corpus.json /srv/homelab/corpus/about-aleix/eval.json
    sudo chmod 0644 /srv/homelab/corpus/about-aleix/corpus.json /srv/homelab/corpus/about-aleix/eval.json
    ```

12. **Start the stack and bot.** Enable the stack under the data target now that its inputs
    exist. First startup downloads the embedding model upstream into `models` and builds
    the app image; wait for the model download and Postgres initialization. The worker
    creates Procrastinate's schema through the library's schema manager. The bot registers
    its command menu with Telegram; start its private chat from the owner's Telegram client.

    ```bash
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml config --quiet
    sudo systemctl enable --now homelab.service
    sudo systemctl start homelab-telegram-bot.service
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml ps
    sudo systemctl is-active homelab.service homelab-telegram-bot.service
    ```

13. **Re-ingest both collections.** Postgres starts empty and is derived from these sources;
    nothing is restored from a database backup. Wait until the local embedding server is
    ready before running ingestion. The about-Aleix corpus takes under a minute; a full Brain
    ingest took 92 minutes on 2026-09-23. Allow the Brain command to finish in the SSH session.
    The per-command environment selects Brain without changing the default serving collection.

    ```bash
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml exec -T api homelab ingest
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml exec -T -e HOMELAB_ACTIVE_COLLECTION=brain api homelab ingest
    ```

14. **Prove the rebuilt node is back.** Health must return `"status":"ok"`. In Telegram,
    send `/ask <QUESTION-COVERED-BY-THE-CORPUS>` and verify a grounded answer with numbered
    sources. Trigger one test alert through the installed notifier and confirm its receipt
    in the owner's Telegram chat; the label is deliberate and does not fail the stack.

    ```bash
    curl -fsS http://127.0.0.1:8000/health
    sudo systemctl start homelab-notify@homelab.service
    sudo systemctl is-failed homelab-notify@homelab.service
    ```

    Expect `inactive` and exit status 1 from `is-failed` after a successful oneshot alert.
    Reboot only now, with the current boot saved in the persistent journal:

    ```bash
    sudo reboot
    ```

    Reconnect with `ssh homelab`, wait for the watchdog's 90-second boot delay, and confirm
    its Telegram notice says the data volume is `LOCKED`. Explicitly start the stack while
    locked to prove that its mount condition skips it without failure or an additional alert:

    ```bash
    data-volume.sh status
    sudo systemctl start homelab.service
    systemctl show homelab.service -p ActiveState -p Result -p ConditionResult
    sudo journalctl -b -u homelab.service -u homelab-watchdog.service --no-pager
    ```

    Expect `ActiveState=inactive`, `Result=success`, `ConditionResult=no` and a journal entry
    showing the condition skip. Confirm `/status` still works and `/ask` reports the knowledge
    service down in Telegram. Finish by unlocking and checking health again:

    ```bash
    sudo data-volume.sh unlock
    curl -fsS http://127.0.0.1:8000/health
    ```

## Layout

| Path | What |
|---|---|
| `src/homelab/api/` | FastAPI — the one published port |
| `src/homelab/models/` | purpose discovery and framework clients for the LiteLLM endpoint |
| `src/homelab/knowledge/` | JSON/Markdown ingestion and retrieval with provenance through LlamaIndex |
| `src/homelab/jobs/` | Procrastinate tasks and the worker |
| `config/litellm.yaml` | LiteLLM’s purposes, models and reasoning efforts |
| `node/` | host operational files, mirroring their installed paths |

[ARCHITECTURE.md](ARCHITECTURE.md) has the boundaries and the rules. [DECISIONS.md](DECISIONS.md)
records choices that weren't obvious. MIT licence.
