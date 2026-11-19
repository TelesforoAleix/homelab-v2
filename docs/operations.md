# Operations

This runbook covers the node as a whole: unlocking and locking the volumes, deploying the
platform, checking its health, rolling it back, database dumps, rebuilding the node from
sources, the bulk disk, and the weekly originals copy and its restore. Read it before any step
on the node; the steps that belong to one part (bot, Mac, launcher, runs service, deployer) live
in that part's document, listed in [Steps for each part](#steps-for-each-part). Steps that cannot
be undone or touch a secret need the owner's go first, as `AGENTS.md` says.

## Run it

For a fresh node, follow [Rebuilding the node](#rebuilding-the-node). On the running node,
connect with `ssh homelab` and unlock with `sudo data-volume.sh unlock` after each boot.
The main passphrase opens `/srv/homelab`; its root-only key then opens `/srv/homelab-bulk`.
Docker and containerd start next, then `homelab-data.target` and its three client services.
`lock` stops the clients, Docker/socket and containerd before unmounting and closing bulk,
then main. `status` reports both mounts and the runtime units. A partial unlock can be retried.
The Telegram bot, watchdog and alerts remain available while locked.

All clients put fetchable/rebuildable files (models, downloads, generated images) under
`/srv/homelab-bulk/<client>/`. Databases, originals, corpus and the bot inbox stay on `/srv/homelab`;
the weekly copy includes each `*-data/originals/` directory and, when present, the bot inbox at
`frontline/inbox/`. Other frontline state is excluded. Models default to
`/srv/homelab-bulk/models`, including both embedding servers. Docker uses
`/srv/homelab-bulk/docker`, containerd uses `/srv/homelab-bulk/containerd`. The default Docker
builder enables periodic GC with a 50 GB target; this is not a filesystem quota and active
builds can temporarily exceed it. See [Docker GC](https://docs.docker.com/build/cache/garbage-collection/)
and [the separate image store](https://docs.docker.com/engine/storage/containerd/).

## Deploy the platform

The agent uses `ssh homelab-agent`, which has root through sudo. Proceed within the owner's
approved task; the exceptions requiring approval are in `AGENTS.md`. Before a restart, check
that no med-ask ingest or continuous Mac model calls are running, as described in
[Mac bulk embedding serving](mac.md#mac-bulk-embedding-serving). Deploy by running
`git pull --ff-only` by hand in `/srv/homelab/homelab-v2`, then
`sudo systemctl restart homelab.service`, which rebuilds the images from that checkout and
starts the stack; the unit never pulls. Host-file installation is explicit and separate: see
[Installing host files](host-files.md). The gateway's configuration and hooks are mounted from
the checkout, so a restart applies them.

Postgres has a fixed address on the default network and host services may rely on it; every
network in `compose.yaml` has a fixed subnet. The shared `homelab-models` network sits outside
Docker's default address pools so that another project starting in parallel cannot take the
default network's range and stop the stack from starting.

## Check health

`curl -fsS http://127.0.0.1:8000/health` must return `"status":"ok"` with the version, the
purposes and the models base URL. `sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml ps`
shows each service and its healthcheck. The [management reads](#management-reads) report routes
and provider state without calling a model. The services log to journald, by container name.

## Roll back the platform

Check out the preceding commit in `/srv/homelab/homelab-v2` and restart `homelab.service`.
Stored records stay as they are: startup schema steps are idempotent and nothing deletes data.
Return to `main` with `git switch main` and `git pull --ff-only` once the fix is merged.
A part with its own rollback (bot, launcher, deployer, runs service) follows its document.

## Develop locally

On the Mac, development needs no services:

```bash
uv sync
uv run pytest
```

On a Docker host, provision `.env` and both gateway secrets as described in step 10 of
[Rebuilding the node](#rebuilding-the-node), then run:

```bash
docker compose up -d --build
docker compose exec api homelab routes
docker compose exec api homelab eval
```

## Steps for each part

| When you | Follow |
|---|---|
| change or install a file under `node/` | [Installing host files](host-files.md) |
| update the Telegram bot, its inbox helper, order endpoint or `/backup` | [Operating the bot](telegram-bot.md#operating-the-bot) |
| install or update the Mac tools, router, profiles or menu bar app | [Mac worker](mac.md#install-and-update-the-mac-tools) |
| install the CLI fence, the launcher or its `task` profile | [Launcher](launcher.md) |
| provision, switch or roll back the `runs` service | [Runs service](runs.md#runs-service) |
| use or roll back the stopped host task runner | [Coding tasks in a container](runs.md#coding-tasks-in-a-container) |
| provision run keys or change a key's repository policy | [Key file and repository policy](run-api.md#key-file-and-repository-policy) |
| install or roll back the application deployer | [Install host files](deployer.md#install-host-files) |
| change the code context server's repositories | [Code context server](node.md#code-context-server) |
| operate video jobs or check media accounting | [Operating video jobs](gateway.md#operating-video-jobs) |
| check image cost alerts | [Using the gateway](gateway.md#using-the-gateway) |

## Database dumps and restore checks

The daily `homelab-dump.timer` dumps every non-template database in
`homelab-postgres-1` as user `homelab` to
`/srv/homelab/homelab-v2-data/originals/postgres/<database>-<date>.dump`.
This includes `homelab` and `factory`; `homelab-<date>.dump` keeps its existing name.
Each custom-format dump is written to a temporary file with mode 0640, fsynced and
atomically replaced under the shared lock. Seven dated dumps are retained per database;
one database's failure fails the run, so the deployer stops before migration.
Earlier databases' completed dumps remain if a later database fails.
The weekly encrypted originals copy includes the whole folder.

Run `sudo homelab-dump` for an immediate dump, or `sudo homelab-restore-check` to take
fresh dumps and check today's dump of every non-template database, each in its own
network-isolated temporary container. It compares table names and row counts, stopping
at the first difference and naming the database. Each temporary container is removed
after its check, including on failure. Both helpers print `ok` only on overall success.
The existing `sudo homelab-restore-check <dump-path>` remains a single-dump check against
the live `homelab` database. Changes to live databases while the check runs can cause
a count difference; repeat during a quiet period.
A dump is not a substitute for testing a full application recovery.

Install these helpers from the merged tree after showing both diffs (exit 1 means differences):

```sh
sudo diff -uN /usr/local/sbin/homelab-dump node/usr/local/sbin/homelab-dump
sudo diff -uN /usr/local/sbin/homelab-restore-check node/usr/local/sbin/homelab-restore-check
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-dump node/usr/local/sbin/homelab-restore-check /usr/local/sbin/
```

## Rebuilding the node

Recovery is a rebuild from sources. No node backup exists or will be made. Host files come
from `node/`, code from this public GitHub repository, and Brain notes from their own GitHub
repository through the owner's Mac clone. Brain is copied from the Mac; originals can be restored from the encrypted weekly copy
(see [Restoring originals from a copy](#restoring-originals-from-a-copy)). The corpus can
also be copied from the owner’s Mac;
Postgres is derived by re-ingesting, models are fetched upstream, and secrets are re-issued.
The rebuilt node never clones Brain. Optional planning reads use a separate read-only GitHub
deploy key; the public application checkout needs no GitHub credential. The stack unit builds
the checked-out code at start; it does not pull updates.

The weekly archive also carries `/srv/homelab/frontline/inbox` at `frontline/inbox` when that
directory exists. Restore it using the ownership and modes in the restore procedure; the
planning clone, orders and markers are rebuilt or transient and are not in the archive.

This procedure describes the node verified on 2026-10-05: Ubuntu 26.04.1 LTS, kernel 7.0,
systemd 259, Docker 29.8 with Compose v5.5, and Tailscale 1.102. Vendor apt repositories supply
Docker and Tailscale; check the versions available when rebuilding. These commands have not
been rehearsed. Run them only during an owner-authorized rebuild, stopping on any error.
Commands run on the node as `aleix` unless marked **on the Mac**. Replace angle-bracket
placeholders locally; never commit their values. Keep the local console available until both
SSH accounts work through Tailscale.

1. **Install Ubuntu and choose the system disk.** In the Ubuntu Server 26.04.1 LTS installer,
   select the **Samsung MZ7TY256, 238.5 GiB**, by model and size. Never identify the target by
   `sda` or `sdb`: kernel names can swap between boots. Leave an existing encrypted **Micron MTFDDAV512TBN,
   476.9 GiB** untouched; recover it using its independent passphrase as described below. **Choosing the wrong disk in the installer
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
   sudo apt-get install ca-certificates curl git rsync openssh-server lvm2 cryptsetup ufw iw chrony polkitd python3 openssl age parted acl
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

   Add these managed entries, leaving Ubuntu's existing entries intact. Bulk stays closed
   until step 8 provisions or recovers it; its key path need not exist yet:

   ```text
   # /etc/crypttab
   homelab-data  /dev/ubuntu-vg/data  none  luks,noauto,discard
   homelab-bulk /dev/disk/by-id/wwn-0x500a07511802f8bd-part1 /srv/homelab/.keys/homelab-bulk.key luks,noauto
   # /etc/fstab
   /dev/mapper/homelab-data  /srv/homelab  ext4  noauto,nofail,x-systemd.device-timeout=10s  0  2
   /dev/mapper/homelab-bulk /srv/homelab-bulk ext4 noauto,nofail,x-systemd.device-timeout=10s 0 2
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
   sudo systemctl disable --now docker.service docker.socket containerd.service
   sudo docker --version
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
   sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin homelab-task
   sudo apt-get install -y acl
   sudo setfacl -m u:homelab-task:--x /srv/homelab
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

   Before applying SSH hardening, create `dictation-backup` and its root-controlled, restricted `authorized_keys` using the [mac-dictation repository's documentation](https://github.com/TelesforoAleix/mac-dictation); account and key provisioning belong there.

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

   For an existing node updating only the bot order endpoint rule, use the merged clone
   at `/srv/homelab/homelab-v2`. Before copying, show the installed-file diff
   with `sudo diff -u /etc/ufw/user.rules node/etc/ufw/user.rules`
   (status 1 means differences). After reviewing it, run:

   ```bash
   sudo install -m 0640 -o root -g root node/etc/ufw/user.rules /etc/ufw/user.rules
   sudo ufw reload
   sudo ufw status
   ```

   Confirm TCP 8110 is allowed from `172.18.0.0/16`, then run the
   [container and Mac checks](telegram-bot.md#bot-order-endpoint). This update installs only `user.rules`;
   the other firewall files and IPv6 rules stay unchanged.

   Run the following only after reviewing all diffs. The commands use the modes recorded in
   [Installing host files](host-files.md#installed-modes). Preserve the vendor's
   `root:polkitd` ownership and `0750` mode on `/etc/polkit-1/rules.d`.

   ```bash
   sudo install -d -m 0755 -o root -g root /etc/ssh /etc/ssh/sshd_config.d /etc/ufw /etc/docker /etc/sysctl.d /etc/profile.d /etc/sudoers.d /usr/local/sbin /opt/homelab-telegram-bot /opt/homelab-context /opt/homelab-task /opt/homelab-package-proxy /etc/homelab-task /etc/systemd/system
   sudo install -d -m 0755 -o root -g root /etc/systemd/system/homelab-notify@.service.d /etc/systemd/system/homelab-watchdog.service.d /etc/systemd/system/homelab-telegram-bot.service.d
   sudo install -d -m 0750 -o root -g polkitd /etc/polkit-1/rules.d
   sudo install -m 0600 -o root -g root node/etc/ssh/sshd_config.d/10-homelab-hardening.conf /etc/ssh/sshd_config.d/10-homelab-hardening.conf
   # user.rules accepts TCP 8110 from homelab_default (172.18.0.0/16).
   sudo install -m 0640 -o root -g root node/etc/ufw/after.rules node/etc/ufw/after6.rules node/etc/ufw/user.rules node/etc/ufw/user6.rules /etc/ufw/
   sudo install -m 0644 -o root -g root node/etc/docker/daemon.json /etc/docker/daemon.json
   sudo install -d -m 0755 -o root -g root /etc/containerd
   sudo install -m 0644 -o root -g root node/etc/containerd/config.toml /etc/containerd/config.toml
   sudo install -m 0644 -o root -g root node/etc/sysctl.d/99-homelab-swappiness.conf /etc/sysctl.d/99-homelab-swappiness.conf
   sudo install -m 0644 -o root -g root node/etc/profile.d/homelab-console-timeout.sh /etc/profile.d/homelab-console-timeout.sh
   sudo install -d -m 0755 -o root -g root /etc/tmpfiles.d
   sudo install -m 0644 -o root -g root node/etc/tmpfiles.d/homelab-task-run.conf /etc/tmpfiles.d/homelab-task-run.conf
   sudo systemd-tmpfiles --create /etc/tmpfiles.d/homelab-task-run.conf
   sudo visudo -cf node/etc/sudoers.d/homelab-agent-v2
   sudo install -m 0440 -o root -g root node/etc/sudoers.d/homelab-agent-v2 /etc/sudoers.d/homelab-agent-v2
   sudo install -m 0644 -o root -g root node/etc/homelab-copy.recipient /etc/homelab-copy.recipient
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-dump node/usr/local/sbin/homelab-restore-check /usr/local/sbin/
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-copy /usr/local/sbin/homelab-copy
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-inbox /usr/local/sbin/homelab-inbox
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-context /usr/local/sbin/homelab-context
   sudo install -m 0644 -o root -g root node/opt/homelab-context/* /opt/homelab-context/
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-task-run /usr/local/sbin/homelab-task-run
   sudo install -m 0644 -o root -g root node/opt/homelab-task/Dockerfile /opt/homelab-task/Dockerfile
   # Inspect the runner diff in the loop above before this host-file install.
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-task-runner /usr/local/sbin/homelab-task-runner
   # Create the diff-only bind source before starting the API; leave the run store root-only.
   sudo install -d -m 0750 -o root -g 10001 /srv/homelab/task-runs/eval-diffs
   # After updating the runner and Compose: restart both, then submit one small eval
   # and read it back through GET /v1/runs/<id> with its delivered commits.
   # sudo systemctl restart homelab-task-runner.service
   # sudo systemctl restart homelab.service
   # Both scripts must be updated together for CLI worker routes. The task image is unchanged.
   # CLI routes need the installed homelab-fence command and its Codex login; do not
   # copy that login or credentials into this tree or the task image. Its existing work/runs
   # parents must be available under /srv/factory; turn folders are created 21000:21000, 0700.
   # The worker default remains LiteLLM; a reviewer route must remain LiteLLM too.
   sudo install -m 0644 -o root -g root node/etc/homelab-task/github_known_hosts /etc/homelab-task/github_known_hosts
   sudo install -m 0644 -o root -g root node/opt/homelab-package-proxy/* /opt/homelab-package-proxy/
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-queue-notice /usr/local/sbin/homelab-queue-notice
   sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-image-cost-notice /usr/local/sbin/homelab-image-cost-notice
   sudo install -d -m 0700 -o root -g root /var/lib/homelab-copy
   sudo install -m 0755 -o root -g root node/usr/local/sbin/*.sh /usr/local/sbin/
   sudo install -m 0644 -o root -g root node/opt/homelab-telegram-bot/*.py /opt/homelab-telegram-bot/
   sudo install -m 0644 -o root -g root node/opt/homelab-telegram-bot/requirements.txt /opt/homelab-telegram-bot/
   sudo apt-get install -y python3-venv
   sudo python3 -m venv /opt/homelab-telegram-bot/.venv
   sudo /opt/homelab-telegram-bot/.venv/bin/python -m pip install --require-hashes -r /opt/homelab-telegram-bot/requirements.txt
   sudo chown -R root:root /opt/homelab-telegram-bot/.venv
   sudo chmod -R u=rwX,go=rX /opt/homelab-telegram-bot/.venv
   sudo /opt/homelab-telegram-bot/.venv/bin/python -c 'import telegram; print(telegram.__version__)'
   sudo install -m 0644 -o root -g root node/etc/polkit-1/rules.d/50-homelab-bot.rules /etc/polkit-1/rules.d/50-homelab-bot.rules
   sudo install -m 0644 -o root -g root node/etc/systemd/system/*.service node/etc/systemd/system/*.timer node/etc/systemd/system/*.target node/etc/systemd/system/*.socket /etc/systemd/system/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-notify@.service.d/*.conf /etc/systemd/system/homelab-notify@.service.d/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-watchdog.service.d/*.conf /etc/systemd/system/homelab-watchdog.service.d/
   sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-telegram-bot.service.d/*.conf /etc/systemd/system/homelab-telegram-bot.service.d/
   sudo install -d -m 0755 -o root -g root /etc/systemd/system/docker.service.d
   sudo install -m 0644 -o root -g root node/etc/systemd/system/docker.service.d/data-volume.conf /etc/systemd/system/docker.service.d/data-volume.conf
   sudo install -d -m 0755 -o root -g root /etc/systemd/system/docker.socket.d
   sudo install -m 0644 -o root -g root node/etc/systemd/system/docker.socket.d/data-volume.conf /etc/systemd/system/docker.socket.d/data-volume.conf
   sudo install -d -m 0755 -o root -g root /etc/systemd/system/containerd.service.d
   sudo install -m 0644 -o root -g root node/etc/systemd/system/containerd.service.d/data-volume.conf /etc/systemd/system/containerd.service.d/data-volume.conf
   sudo dockerd --validate --config-file=/etc/docker/daemon.json
   sudo containerd --config /etc/containerd/config.toml config dump >/dev/null
   sudo visudo -c
   sudo sshd -t
   sudo systemd-analyze --generators=yes verify /etc/systemd/system/homelab.service /etc/systemd/system/homelab-data.target /etc/systemd/system/homelab-notify@.service /etc/systemd/system/homelab-watchdog.service /etc/systemd/system/homelab-watchdog.timer /etc/systemd/system/homelab-telegram-bot.service /etc/systemd/system/homelab-bot-data-ready.service /etc/systemd/system/homelab-bot-data-released.service /etc/systemd/system/wifi-powersave-off.service /etc/systemd/system/homelab-copy-reminder.service /etc/systemd/system/homelab-copy-reminder.timer /etc/systemd/system/homelab-context-refresh.service /etc/systemd/system/homelab-context-refresh.timer
   sudo systemd-analyze verify /etc/systemd/system/homelab-image-cost-notice.service /etc/systemd/system/homelab-image-cost-notice.timer /etc/systemd/system/homelab-task-runner.service
   ```

   The context image and its private repository list are set up after Docker and the data
   volume are available, following [Code context server](node.md#code-context-server). Create
   `/etc/homelab-context/repositories` as `root:root` mode `0600` with the desired names.
   The task image pins the same uv version as CI through `pyproject.toml`; update both pins
   together. Then build the task image with `sudo docker build -t homelab-task:3 /opt/homelab-task`
   and approve its immutable id in `/etc/homelab-launcher/images` as described under
   [Launcher task profile](launcher.md#task-launcher-profile).
   Compose creates `/srv/homelab/task-tokens` and `/srv/homelab/task-costs` on first start and the
   first run tightens both (`root:10001`, `0750` and `0770`), so LiteLLM can append costs; restart
   `homelab.service` after merging, since LiteLLM needs the new mount and `task_cost_dir`. The
   task runner creates `/srv/homelab/task-work`; see
   [Coding tasks in a container](runs.md#coding-tasks-in-a-container). Compose builds `package-proxy`
   from the application clone; the copy in `/opt` only records what is installed. Push keys are
   not part of a rebuild: each is a new deploy key, issued on the owner's go.

   The bot environment stays on the root filesystem so it works before unlock. Venv
   directories are `0755`, data/Python files `0644`, and executable entry points `0755`,
   all `root:root`; the service can read but cannot modify it. For an update, first save
   the installed bot `.py` files and unit in a root-only rollback directory and show their
   diffs against the committed tree. Restore those files, run `systemctl daemon-reload`
   and restart `homelab-telegram-bot.service` to roll back. When reverting the planning feature,
   first disable and stop `homelab-bot-data-ready.service`, stop its release helper, then
   restore the prior bot files and unit, remove the two added hook unit files, reload units
   and start the bot. Keep the encrypted planning directory, key and config for recovery;
   deleting them is a separate owner decision. Restarting the Docker stack
   does not update this environment. Install every committed `.py` module, including
   `conversation.py`, `orders.py`, `order_listener.py`, `inbox.py`, `keeping.py` and
   `links.py`, together; the requirements remain unchanged.
   The inbox needs no new provisioning: the existing frontline writable directory holds
   its atomic item directories and opaque files. Follow [Bot inbox](telegram-bot.md#bot-inbox) for layout,
   caps, rollback and the owner's phone checks. Keep inbox data when reverting bot code.
   Install the root-owned inbox helper above from merged main after showing its installed-file
   diff and compiling it with the host Python; follow [Inbox helper over SSH](telegram-bot.md#inbox-helper-over-ssh)
   for Mac command checks and helper-only rollback. No bot restart is needed for helper changes.
   Install `homelab-queue-notice` from merged main after showing its installed-file diff
   (`sudo diff -u /usr/local/sbin/homelab-queue-notice node/usr/local/sbin/homelab-queue-notice`,
   using `/dev/null` if absent). Compile it with `/usr/bin/python3 -I` before installation.
   It is `root:root` `0755` in `/usr/local/sbin` (`root:root` `0755`); no static unit,
   daemon reload or bot restart is needed. The command starts a collected transient service
   with the existing bot account and TPM-sealed `LoadCredentialEncrypted` credential
   (`bot-token:/etc/homelab-telegram-bot/token.cred`), an eight-second runtime limit and no retry.
   `--task` changes the prefix to "Task: " and passes it to the `--send` step; the task runner uses
   it. Stdin is bounded to 16,384 characters; line breaks become spaces, controls are stripped,
   and the prefixed notice is capped at 1,000 characters. The shared sender's output is
   discarded so notice text cannot enter the journal. Validate the transient service properties
   with `systemd-analyze verify` before installing and test from the Mac:
   `printf '%s' 'Test notice; Mac queue notifications installed' | ssh homelab-agent sudo -n homelab-queue-notice`.
   Confirm arrival in the owner's private chat. To roll back, restore the prior helper
   (or remove it if newly installed); Mac sends fail closed and are dropped. Keep the bot,
   token and allowlist unchanged. The orchestrator installs the Mac tools and tests a failing job.
   The order listener uses the existing AF_INET permission and writable frontline directory;
   the unit changes only its networking comment. Before copying, show the installed-file diffs
   and save a dated root-only rollback copy of all installed bot modules and the unit. Verify
   the unit with `systemd-analyze verify`, reload and restart only the bot. Follow the
   [order endpoint contract](telegram-bot.md#bot-order-endpoint) for the owner's Tailscale policy step,
   port/address proof, curl checks and rollback; do not install a Mac runner here. After stack restart,
   verify `curl -fsS http://127.0.0.1:4000/v1/models` includes `frontline` and `ss -ltn`
   shows port 4000 only on `127.0.0.1`. Record the bot's active/restart state and unchanged
   security score (1.3), then check med-ask and image studio health. The owner checks natural
   questions, a follow-up, a changed topic, an explicit reply to an older answer, and a long
   `/slice` from their phone after the private `context_files` list is configured.

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
   sudo systemctl disable docker.service docker.socket containerd.service
   sudo systemctl enable --now tailscaled.service chrony.service wifi-powersave-off.service
   sudo systemctl enable homelab-telegram-bot.service homelab-bot-data-ready.service homelab-watchdog.timer homelab-copy-reminder.timer homelab-context-refresh.service homelab-context-refresh.timer homelab-image-cost-notice.timer homelab-dump.timer homelab-task-runner.service
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

8. **Provision or recover bulk, then unlock both volumes.** Follow the disk procedure
   below. On a fresh rebuild, manually open and mount main first (its key directory does not
   exist yet), then provision blank bulk. On an existing bulk disk, never format it: use the
   owner's independent passphrase to open it, mount it, generate a replacement key on the new
   main filesystem and add that key with `cryptsetup luksAddKey`. Keep its passphrase slot.
   On this rebuild only, enter a root Bash shell and open main manually first:

   ```bash
   sudo -i
   bash
   set -euo pipefail
   cryptsetup open --allow-discards /dev/ubuntu-vg/data homelab-data
   mount /srv/homelab
   install -d -m 0755 -o root -g root /srv/homelab-bulk
   ```

   For **blank bulk only**, run the disk inspection and partition blocks in the bulk section
   below: set `DISK`, `PART`, `KEY` as shown there, run from `lsblk` through the `blkid` guard
   and `test ! -e "$KEY"`, then the whole partition/encrypt block. Skip the migration backup,
   copy, rename and cleanup blocks on a fresh rebuild. For **existing bulk**, use the recovery
   block instead; the owner types its independent passphrase. Never run blank-disk formatting
   on it. Both table entries were added in step 2. Exit the root shell before continuing.
   Once both mounts and the key are present, the usual command is resumable.

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
   sudo install -d -m 0755 -o root -g root /srv/homelab-bulk/models /srv/homelab/postgres
   sudo mkdir -p /srv/homelab-bulk/models/embed-large
   sudo chown 10001:10001 /srv/homelab-bulk/models/embed-large
   sudo chmod 0755 /srv/homelab-bulk/models/embed-large
   ```

   **On the Mac**, copy notes, including hidden note files but excluding Git metadata:

   ```bash
   rsync -a --chmod=D755,F644 --exclude='.git' '<MAC-BRAIN-CLONE>/' homelab:/srv/homelab/brain/
   ```

10. **Re-issue the secrets and provision the private allowlists.** Create a fresh gateway
    key with the owner's gateway account and budget, a random hexadecimal database password
    in the password manager, and a new Telegram bot token through BotFather. Enter keys and
    the password only at hidden prompts; never put them in shell arguments or history.
    `.env` holds the database password and four volume paths and is `root:root` `0600`.
    Optional `HOMELAB_MAC_VISION_URL` and `HOMELAB_MAC_EMBED_URL` both point to the Mac
    router on port 8090 and live only in this private `.env`, each ending in `/v1`; leave
    them unset for closed defaults.
    Restore them from the owner’s Mac serving configuration, never from Git.
    Create a separate gateway key used only for images and set its $20 monthly budget in
    Vercel. Both gateway keys are `root:10001` `0440`, mounted only into LiteLLM and read
    at startup. Never place either key in `.env`, Compose environment or configuration.

    ```bash
    sudo install -d -m 0700 -o root -g root /srv/homelab/homelab-v2/secrets
    sudo bash -c 'umask 077; read -r -s -p "new gateway key: " K </dev/tty; echo; test -n "$K" || exit 1; printf "%s" "$K" > /srv/homelab/homelab-v2/secrets/gateway_api_key'
    sudo chown root:10001 /srv/homelab/homelab-v2/secrets/gateway_api_key
    sudo chmod 0440 /srv/homelab/homelab-v2/secrets/gateway_api_key
    sudo bash -c 'umask 077; read -r -s -p "new image gateway key: " K </dev/tty; echo; test -n "$K" || exit 1; printf "%s" "$K" > /srv/homelab/homelab-v2/secrets/image_gateway_api_key'
    sudo chown root:10001 /srv/homelab/homelab-v2/secrets/image_gateway_api_key
    sudo chmod 0440 /srv/homelab/homelab-v2/secrets/image_gateway_api_key
    sudo bash -c 'umask 077; read -r -s -p "new hexadecimal database password: " P </dev/tty; echo; [[ "$P" =~ ^[[:xdigit:]]{32,}$ ]] || exit 1; printf "%s\n" "HOMELAB_DB_PASSWORD=$P" "HOMELAB_BRAIN_PATH=/srv/homelab/brain" "HOMELAB_CORPUS_PATH=/srv/homelab/homelab-v2-data/originals/corpus" "HOMELAB_POSTGRES_PATH=/srv/homelab/postgres" "HOMELAB_MODELS_PATH=/srv/homelab-bulk/models" > /srv/homelab/homelab-v2/.env'
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

    For optional planning reads, unlock the data volume first. Give the bot only traverse
    access to its parent and ownership of its own private directory; no membership in the
    owner's group is needed. Preserve all existing ACL entries:

    ```bash
    mountpoint -q /srv/homelab || exit 1
    sudo setfacl -m u:homelab-bot:--x /srv/homelab
    sudo install -d -m 0700 -o homelab-bot -g homelab-bot /srv/homelab/frontline
    sudo getfacl -p /srv/homelab /srv/homelab/frontline
    ```

    Generate a new ed25519 deploy key only if none exists. Never overwrite an existing key.
    Its private half remains root-owned and readable only by root and the bot's group.
    Register the public half on the private planning repository with **Allow write access
    unchecked**. Never copy the private half off the node:

    ```bash
    sudo test ! -e /etc/homelab-telegram-bot/planning_ed25519 || exit 1
    sudo ssh-keygen -q -t ed25519 -N '' -C frontline-readonly -f /etc/homelab-telegram-bot/planning_ed25519
    sudo chown root:homelab-bot /etc/homelab-telegram-bot/planning_ed25519
    sudo chmod 0640 /etc/homelab-telegram-bot/planning_ed25519
    sudo cat /etc/homelab-telegram-bot/planning_ed25519.pub
    ```

    Pin GitHub's published ed25519 host key in
    `/etc/homelab-telegram-bot/known_hosts`, owned `root:homelab-bot`, mode `0640`.
    Obtain it from [GitHub's SSH key documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
    over verified HTTPS and compare `ssh-keygen -lf` with the fingerprint published there;
    do not trust an unverified `ssh-keyscan` result. Git uses only this known-hosts file,
    strict host checking, this identity, no agent and no interactive authentication.

    Create `planning.json` as `root:homelab-bot` `0640`, and fill it privately using `sudoedit`.
    This is its schema with neutral placeholders, not deployable values:

    ```json
    {
      "repo_url": "git@github.com:<OWNER>/<REPOSITORY>.git",
      "branch": "<BRANCH>",
      "state_path": "<RELATIVE-STATE-PATH>",
      "outbox_path": "<RELATIVE-OUTBOX-PATH>",
      "export_dir": "<ABSOLUTE-EXPORT-DIRECTORY>",
      "poll_seconds": 60,
      "context_files": ["<RELATIVE-SUMMARY-PATH>"]
    }
    ```

    The four string keys are required. `repo_url` uses GitHub's SCP-style SSH syntax;
    `state_path` and `outbox_path` are relative to the optional absolute `export_dir`;
    without it they are relative to the clone. Reads cannot escape their root. Context files
    remain relative to the clone. The bot user needs read access to the export directory and
    its files. Missing or invalid state leaves inbox items and outbox notices available; slice
    status and orders report that scheduler data is unavailable. `poll_seconds` is optional,
    defaults to 60 and accepts integer values from 10 through 3600. `context_files` is an
    optional list of up to 16 relative text-file paths (default `[]`); the orchestrator adds
    the owner’s two summary files privately. Each must fit the 128 KiB file cap and together
    with pending work fit the 256 KiB context cap. Keep the actual repository
    identity, branch and both paths out of Git, tests, PRs, docs and logs.
    Optional `github_owner` is a GitHub user/organization name for approval PR links;
    omit it to show PR numbers without links. The data directory,
    key and known-hosts paths are fixed above. The config and SSH files stay on the root
    filesystem so missing or locked-volume planning never prevents boot-time status.

    After installing the committed bot modules and all three bot units, enable
    `homelab-bot-data-ready.service`, run `systemctl daemon-reload` and restart
    `homelab-telegram-bot.service`. The hook only refreshes access after future unlocks;
    the bot itself stays enabled at boot. Do not enable the release helper: the hook starts it
    only when the target stops. Record `systemd-analyze security
    homelab-telegram-bot.service` before and after (the reviewed sandbox scores 1.3), and run
    `systemd-analyze --generators=yes verify` on all three units. With no config, confirm the
    single not-configured journal line and working `/status`. With config, confirm
    `planning clone ready`, successful pulls and `planning outbox initialized lines=N;
    history skipped`; inspect only the numeric marker fields, never planning content in logs.
    Keep the marker with the clone on the encrypted volume. Have the owner check `/pending`,
    `/slice <id>` and a new event arriving once. After a reboot, `/status` must answer while
    locked; after unlock these read commands and notices must resume without a manual restart.
    Only this bot's files and units need deployment; the Docker stack is unaffected.

    For Telegram `/backup`, provision `/etc/homelab-telegram-bot/mac-listener-url` with the
    owner-confirmed `http://<MAC-TAILSCALE-IP>:8091` base URL (no action path). The address is
    private deployment data; never put it in Git, a PR, or logs. With the existing `homelab-bot`
    account/group provisioned, create the file, enter the URL using `sudoedit`, and set its mode:

    ```bash
    sudo touch /etc/homelab-telegram-bot/mac-listener-url
    sudo chown root:homelab-bot /etc/homelab-telegram-bot/mac-listener-url
    sudo chmod 0640 /etc/homelab-telegram-bot/mac-listener-url
    sudoedit /etc/homelab-telegram-bot/mac-listener-url
    ```

    The bot reads the file on each `/backup`; an absent file means not configured. Include the
    owner's ID in both bot allowlists to permit this privileged command. It makes one direct
    HTTP call with a 210-second timeout, without proxy, redirect or retry. No sudo/polkit grant
    or copy-file access is added. Install the Mac agent from the Mac section, after allowing
    only the node to reach TCP 8091 in the tailnet policy. Restart only the bot after updating
    its installed Python files; `homelab.service` needs no restart for this channel.

11. **Copy the corpus from the owner's Mac.** Create the destination on the unlocked volume:

    ```bash
    sudo mkdir -p /srv/homelab/homelab-v2-data/originals
    sudo chown 0:0 /srv/homelab/homelab-v2-data /srv/homelab/homelab-v2-data/originals
    sudo chmod 0755 /srv/homelab/homelab-v2-data /srv/homelab/homelab-v2-data/originals
    sudo install -d -m 0750 -o root -g root /srv/homelab/homelab-v2-data/originals/postgres
    sudo install -d -m 0755 -o root -g root /srv/homelab/homelab-v2-data/originals/corpus
    sudo install -d -m 0755 -o aleix -g aleix /srv/homelab/homelab-v2-data/originals/corpus/about-aleix
    ```

    **On the Mac**, copy `corpus.json` and `eval.json` directly onto the encrypted volume:

    ```bash
    scp '<MAC-CORPUS-DIRECTORY>/corpus.json' '<MAC-CORPUS-DIRECTORY>/eval.json' homelab:/srv/homelab/homelab-v2-data/originals/corpus/about-aleix/
    ```

    Back **on the node**, make the installed corpus root-owned and readable by the app.
    Compose mounts it read-only. Set `HOMELAB_CORPUS_PATH` in the private `.env` to
    `/srv/homelab/homelab-v2-data/originals/corpus`. On an existing node, copy with
    `sudo cp -a /srv/homelab/corpus /srv/homelab/homelab-v2-data/originals/`,
    verify contents and modes, then set the path. Check no med-ask ingest is running
    before restarting the stack. Verify the mount and eval before removing the old copy.

    ```bash
    sudo chown root:root /srv/homelab/homelab-v2-data/originals/corpus/about-aleix /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/corpus.json /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/eval.json
    sudo chmod 0644 /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/corpus.json /srv/homelab/homelab-v2-data/originals/corpus/about-aleix/eval.json
    ```

12. **Start the stack and bot.** Enable the stack under the data target now that its inputs
    exist. First startup downloads Nemotron into `models` and the official Qwen3-Embedding-4B
    Q8_0 into `models/embed-large` (owned by `10001:10001` for its non-root server), and builds
    the app image. Both embedding servers are always on; wait for both model downloads and
    Postgres initialization. On an existing node, prepare that same subdirectory before deploy;
    if `HOMELAB_MODELS_PATH` is overridden, create it beneath that path. The worker
    creates Procrastinate's schema through the library's schema manager. The bot registers
    its command menu with Telegram; start its private chat from the owner's Telegram client.

    ```bash
    sudo docker compose -f /srv/homelab/homelab-v2/compose.yaml config --quiet
    sudo systemctl enable --now homelab.service
    sudo systemctl start homelab-telegram-bot.service
    sudo systemctl start homelab-copy-reminder.timer
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
    locked to prove that neither runtime can start:

    ```bash
    data-volume.sh status
    sudo systemctl start docker.socket containerd.service
    systemctl is-active docker.service docker.socket containerd.service
    systemctl show docker.socket containerd.service -p ActiveState -p Result -p ConditionResult
    sudo journalctl -b -u homelab.service -u homelab-watchdog.service --no-pager
    ```

    Expect all three runtime units inactive (is-active exits nonzero),
    `Result=success`, `ConditionResult=no` on the two explicitly started units, and a journal entry
    showing the condition skip. Confirm `/status` still works and `/ask` reports the knowledge
    service down in Telegram. Finish by unlocking and checking health again:

    ```bash
    sudo data-volume.sh unlock
    curl -fsS http://127.0.0.1:8000/health
    ```

### After the numbered steps

With the stack running, restore the execution path in this order. Each part's document holds
the commands, diffs, probes and rollback.

1. Install the CLI fence and the fixed launcher with its `task` profile, following
   [launcher.md](launcher.md). Task and CLI create/remove operations require root-owned
   work/run roots: group or other write is accepted only with the sticky bit (including
   Factory's existing 1733 roots); other writable or non-root-owned roots are refused. Run `sudo python3 tests/launcher_task_probe.py` (14 checks),
   and add `--private` with the private-repository credentials for the fifteenth check.
2. Install `/etc/tmpfiles.d/homelab-task-run.conf` (step 6 already does) and run
   `systemd-tmpfiles --create /etc/tmpfiles.d/homelab-task-run.conf` to create the boot lock.
3. Provision the `runs` service, the launcher sandbox write allowance and the per-service
   secret copies using [Runs service](runs.md#runs-service), then restore the enabled
   configuration with [Switch to runs](runs.md#switch-to-runs) steps 4 and 5. Restart every
   launcher socket client after the launcher socket. The node starts `runs` with the platform
   and enables `HOMELAB_RUN_ROLES=slice,cut,review`; the old host runner stays installed but
   stopped and disabled as rollback.
4. Provision the run API's root-owned digest file with each key's repository policy using
   [Key file and repository policy](run-api.md#key-file-and-repository-policy). Preserve
   Factory's existing bearer key; the API alone receives the read-only key-file mount.
5. Install the application deployer and its manifests using
   [deployer.md](deployer.md#install-host-files).

### Bulk disk and storage migration

These commands are **not rehearsed**. Run only after this change is merged and the owner
approves partitioning/encryption. No node changes are performed by the implementation agent.
Formatting destroys the selected disk; there is no rollback for formatting the wrong disk.
The inspection refuses signatures, partitions, mounts or holders. Stop on any error.
Copying retains both old roots until the owner proves reboot/unlock. Root space is not
reclaimed by renaming: only the later cleanup frees it. No database or originals move.

The runtime configuration and drop-in modes are in
[Installing host files](host-files.md#installed-modes).

`node/etc/crypttab` and `node/etc/fstab` are **managed entries**, not whole host files.
Keep the existing OS entries. Rebuild step 2 registers both volumes before the new unlock
script is used. Never install these files over the
host tables. On a rebuild, perform the blank-disk part below only if bulk really is blank;
an existing LUKS filesystem is recovered, not formatted. If just the Micron fails, main and
its originals survive; provision its replacement as blank and copy/rebuild bulk's contents.
If main fails, the owner's bulk passphrase recovers Micron independently. After main is
rebuilt, open/mount bulk with that passphrase and add a newly generated on-main key:

```bash
sudo cryptsetup open /dev/disk/by-id/wwn-0x500a07511802f8bd-part1 homelab-bulk
sudo mount /srv/homelab-bulk
sudo install -d -m 0700 -o root -g root /srv/homelab/.keys
sudo bash -c 'set -e; umask 077; set -o noclobber; head -c 4096 /dev/urandom > /srv/homelab/.keys/homelab-bulk.key; chmod 0400 /srv/homelab/.keys/homelab-bulk.key'
sudo cryptsetup luksAddKey /dev/disk/by-id/wwn-0x500a07511802f8bd-part1 /srv/homelab/.keys/homelab-bulk.key
```

The last command asks the owner for the existing passphrase. Do not remove that independent
slot. The generated key stays on main and is never printed, exported or committed.

**Rollback, prepared first.** These commands are for the same root Bash session used below,
with main still open. Use only if migration fails, before cleanup. They preserve the bulk
copy for inspection. Stop applications before rolling back; writes made after switching
storage may be absent in the retained old Docker state. Database bind mounts stay on main.

```bash
systemctl stop homelab-data.target
systemctl stop homelab.service med-ask.service image-studio.service
systemctl stop docker.service docker.socket
systemctl stop containerd.service
for area in docker containerd; do
  if test -d "/var/lib/$area.pre-bulk"; then
    test ! -e "/var/lib/$area"
    mv "/var/lib/$area.pre-bulk" "/var/lib/$area"
  fi
done
for file in etc/docker/daemon.json etc/containerd/config.toml usr/local/sbin/data-volume.sh usr/local/sbin/homelab-watchdog.sh etc/systemd/system/homelab-data.target etc/crypttab etc/fstab; do
  install -o root -g root -m "$(stat -c %a "/root/homelab-bulk-rollback/$file")" "/root/homelab-bulk-rollback/$file" "/$file"
done
for unit in docker.service docker.socket containerd.service; do
  rm -f "/etc/systemd/system/$unit.d/data-volume.conf"
done
install -o root -g root -m 0600 /root/homelab-bulk-rollback/env /srv/homelab/homelab-v2/.env
# Override the new Compose default even if the old env had no models setting.
sed -i '/^HOMELAB_MODELS_PATH=/d' /srv/homelab/homelab-v2/.env
printf '%s\n' 'HOMELAB_MODELS_PATH=/srv/homelab/models' >> /srv/homelab/homelab-v2/.env
systemctl daemon-reload
systemctl enable containerd.service docker.service docker.socket
systemctl start docker.service
systemctl start homelab-data.target
curl -fsS http://127.0.0.1:8000/health
```

The original models stay at `/srv/homelab/models` until cleanup. Rollback restores the old
single-volume unlock script and boot enablement. It does not delete bulk, its key or LUKS
slots. If `/var/lib/docker` or `/var/lib/containerd` unexpectedly exists as well as its
`.pre-bulk` copy, stop and inspect; never overwrite it. This rollback assumes the preflight
below found no existing `data-volume.conf` drop-ins and the three runtimes enabled at boot.

**Inspect and prepare.** After owner's approval and merge, enter the node with
`ssh homelab-agent`, then `sudo -i` and `bash`. Run these blocks in that root shell; do not
paste rollback as part of normal execution. `set -e` stops the shell on failures.

```bash
set -euo pipefail
cd /srv/homelab/homelab-v2
git pull --ff-only origin main
test -z "$(git status --porcelain)"
DISK=/dev/disk/by-id/wwn-0x500a07511802f8bd
PART=${DISK}-part1
KEY=/srv/homelab/.keys/homelab-bulk.key
findmnt --mountpoint /srv/homelab
lsblk -o NAME,MODEL,SIZE,TYPE,MOUNTPOINTS "$DISK"
test -b "$DISK"
DISK_LAYOUT=$(lsblk -nr -o TYPE "$DISK")
test "$DISK_LAYOUT" = disk
DISK_MOUNTS=$(lsblk -nr -o MOUNTPOINTS "$DISK")
test -z "$(printf '%s' "$DISK_MOUNTS" | tr -d '[:space:]')"
DISK_DEVICE=$(readlink -f "$DISK")
DISK_HOLDERS=$(ls -A "/sys/class/block/${DISK_DEVICE##*/}/holders")
test -z "$DISK_HOLDERS"
DISK_SIGNATURES=$(wipefs --no-act --noheadings --output TYPE "$DISK")
test -z "$DISK_SIGNATURES"
if blkid -p "$DISK"; then exit 1; else test "$?" -eq 2; fi
test ! -e "$PART"
test ! -e /dev/mapper/homelab-bulk
test ! -e "$KEY"
test ! -e /root/homelab-bulk-rollback
for area in docker containerd; do
  test -d "/var/lib/$area"
  test ! -e "/var/lib/$area.pre-bulk"
done
for unit in docker.service docker.socket containerd.service; do
  test "$(systemctl is-enabled "$unit")" = enabled
  test ! -e "/etc/systemd/system/$unit.d/data-volume.conf"
done
install -d -m 0700 -o root -g root /root/homelab-bulk-rollback
for file in etc/docker/daemon.json etc/containerd/config.toml usr/local/sbin/data-volume.sh usr/local/sbin/homelab-watchdog.sh etc/systemd/system/homelab-data.target etc/crypttab etc/fstab; do
  cp -a --parents "/$file" /root/homelab-bulk-rollback/
done
install -m 0600 -o root -g root .env /root/homelab-bulk-rollback/env
df -h / /srv/homelab | tee /root/homelab-bulk-rollback/space-before.txt
```

**Partition and encrypt the verified blank Micron.** This is the destructive boundary;
main is already unlocked. Do not use these commands to recover an existing bulk disk.

```bash
parted --script "$DISK" mklabel gpt mkpart homelab-bulk 1MiB 100%
udevadm settle
test -b "$PART"
install -d -m 0700 -o root -g root /srv/homelab/.keys
(umask 077; set -o noclobber; head -c 4096 /dev/urandom > "$KEY")
chown root:root "$KEY"
chmod 0400 "$KEY"
cryptsetup luksFormat --type luks2 --batch-mode --key-file "$KEY" "$PART"
cryptsetup open --key-file "$KEY" "$PART" homelab-bulk
mkfs.ext4 -L homelab-bulk /dev/mapper/homelab-bulk
install -d -m 0755 -o root -g root /srv/homelab-bulk
mount /dev/mapper/homelab-bulk /srv/homelab-bulk
findmnt --mountpoint /srv/homelab-bulk
```

**Stop, copy and install.** No rehearsal and no cache purge. `rsync -aHAXS --numeric-ids`
preserves ownership, hardlinks, ACLs, xattrs and sparse files. The dry comparison after each
copy uses checksums; expect no output. Any difference is a stop, not a reason to delete.
Neither the bind-mounted databases nor originals leave main.

```bash
systemctl stop homelab-data.target
systemctl stop homelab.service med-ask.service image-studio.service
systemctl stop docker.service docker.socket
systemctl stop containerd.service
for unit in homelab.service med-ask.service image-studio.service docker.service docker.socket containerd.service; do
  test "$(systemctl show -p ActiveState --value "$unit")" = inactive
done
# There must be no container snapshot mounts left below the source roots.
MOUNT_TARGETS=$(findmnt -rn -o TARGET)
if printf '%s\n' "$MOUNT_TARGETS" | grep -E '^/var/lib/(docker|containerd)/'; then exit 1; fi
for area in docker containerd; do
  test ! -e "/srv/homelab-bulk/$area"
  rsync -aHAXS --numeric-ids "/var/lib/$area" /srv/homelab-bulk/
  COPY_DIFFERENCES=$(rsync -aHAXSnci --numeric-ids --delete "/var/lib/$area/" "/srv/homelab-bulk/$area/")
  test -z "$COPY_DIFFERENCES"
done
test ! -e /srv/homelab-bulk/models
rsync -aHAXS --numeric-ids /srv/homelab/models /srv/homelab-bulk/
COPY_DIFFERENCES=$(rsync -aHAXSnci --numeric-ids --delete /srv/homelab/models/ /srv/homelab-bulk/models/)
test -z "$COPY_DIFFERENCES"
sync
# Show installed diffs BEFORE copying. diff exit 1 is an expected difference.
for file in etc/docker/daemon.json etc/containerd/config.toml usr/local/sbin/data-volume.sh usr/local/sbin/homelab-watchdog.sh etc/systemd/system/homelab-data.target; do
  diff -u "/$file" "node/$file" || test "$?" -eq 1
done
for unit in docker.service docker.socket containerd.service; do
  diff -u /dev/null "node/etc/systemd/system/$unit.d/data-volume.conf" || test "$?" -eq 1
done
# Preserve OS entries; replace just the two managed lines in each table.
awk '$1 != "homelab-data" && $1 != "homelab-bulk"' /etc/crypttab > /root/homelab-bulk-rollback/crypttab.new
cat node/etc/crypttab >> /root/homelab-bulk-rollback/crypttab.new
awk '$2 != "/srv/homelab" && $2 != "/srv/homelab-bulk"' /etc/fstab > /root/homelab-bulk-rollback/fstab.new
cat node/etc/fstab >> /root/homelab-bulk-rollback/fstab.new
for table in crypttab fstab; do
  diff -u "/etc/$table" "/root/homelab-bulk-rollback/$table.new" || test "$?" -eq 1
  install -m 0644 -o root -g root "/root/homelab-bulk-rollback/$table.new" "/etc/$table"
done
install -m 0644 -o root -g root node/etc/docker/daemon.json /etc/docker/daemon.json
install -m 0644 -o root -g root node/etc/containerd/config.toml /etc/containerd/config.toml
install -m 0755 -o root -g root node/usr/local/sbin/data-volume.sh node/usr/local/sbin/homelab-watchdog.sh /usr/local/sbin/
install -m 0644 -o root -g root node/etc/systemd/system/homelab-data.target /etc/systemd/system/homelab-data.target
for unit in docker.service docker.socket containerd.service; do
  install -d -m 0755 -o root -g root "/etc/systemd/system/$unit.d"
  install -m 0644 -o root -g root "node/etc/systemd/system/$unit.d/data-volume.conf" "/etc/systemd/system/$unit.d/data-volume.conf"
done
# Update only this non-secret env field; never print .env or its diff.
sed -i '/^HOMELAB_MODELS_PATH=/d' .env
printf '%s\n' 'HOMELAB_MODELS_PATH=/srv/homelab-bulk/models' >> .env
dockerd --validate --config-file=/etc/docker/daemon.json
containerd --config /etc/containerd/config.toml config dump >/dev/null
findmnt --verify --tab-file /etc/fstab
systemctl disable docker.service docker.socket containerd.service
systemctl daemon-reload
systemd-analyze --generators=yes verify homelab-data.target homelab.service docker.service docker.socket containerd.service
mv /var/lib/docker /var/lib/docker.pre-bulk
mv /var/lib/containerd /var/lib/containerd.pre-bulk
data-volume.sh unlock
```

**Checks before the owner's reboot.** All three application units and bot must be active;
all containers running and every defined healthcheck healthy. Wait for model loading and
container healthchecks, then re-run checks if they are still starting. No configuration or
service mutations are implied by checking.

```bash
data-volume.sh status
systemctl is-active homelab.service med-ask.service image-studio.service homelab-telegram-bot.service docker.service docker.socket containerd.service
systemctl is-active homelab-watchdog.timer
systemctl is-enabled docker.service docker.socket containerd.service || true
# Expect disabled for all three, not enabled or masked.
docker info --format '{{.DockerRootDir}} {{json .DriverStatus}}'
# Expect /srv/homelab-bulk/docker and io.containerd.snapshotter.v1.
containerd config dump | grep '^root ='
# Expect /srv/homelab-bulk/containerd; docker info has no containerd root field.
docker buildx inspect default
# Expect docker driver and GC policies with about 50 GB Keep/Max Used Space.
python3 -c 'import json; d=json.load(open("/etc/docker/daemon.json")); assert d["builder"]["gc"] == {"enabled": True, "defaultKeepStorage": "50GB"}'
docker ps --format '{{.Names}} {{.Status}}'
for container in $(docker ps -q); do
  test "$(docker inspect -f '{{.State.Status}}' "$container")" = running
  test "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}healthy{{end}}' "$container")" = healthy
done
docker compose -f /srv/homelab/homelab-v2/compose.yaml ps
curl -fsS http://127.0.0.1:8000/health
for service in llama-embed llama-embed-large; do
  docker compose -f /srv/homelab/homelab-v2/compose.yaml exec -T -e CHECK_SERVICE="$service" api python -c 'import os, urllib.request; base="http://"+os.environ["CHECK_SERVICE"]+":8080"; print(urllib.request.urlopen(base+"/health").read().decode()); print(urllib.request.urlopen(base+"/v1/models").read().decode())'
done
cat /root/homelab-bulk-rollback/space-before.txt
df -h / /srv/homelab /srv/homelab-bulk
```

Verify the embedding containers' mounts explicitly:

```bash
for service in llama-embed llama-embed-large; do
  docker inspect --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' "$(docker compose -f /srv/homelab/homelab-v2/compose.yaml ps -q "$service")"
done
```

Expect `/srv/homelab-bulk/models` and `/srv/homelab-bulk/models/embed-large`. The bot's `/status`
and the watchdog timer remain available before unlock. No test alert or reboot is sent by
the implementation agent. Root usage will still include `.pre-bulk` until cleanup.

**Owner's steps** (owner types the passphrase, then reboots):

```bash
sudo -i
KEY=/srv/homelab/.keys/homelab-bulk.key
PART=/dev/disk/by-id/wwn-0x500a07511802f8bd-part1
cryptsetup luksAddKey --key-file "$KEY" "$PART"
cryptsetup open --test-passphrase "$PART"
reboot
```

Enter the independent new passphrase at the first command's two prompts, then test it at
the second prompt. Keep it in the owner's password manager; it is the recovery path if
main dies. Reconnect as owner with `ssh homelab`. Before unlock, Docker/socket/containerd
must be inactive; Telegram `/status` and the watchdog's locked notice must still work.
Then `sudo data-volume.sh unlock` asks only for main's usual passphrase. Confirm both mounts,
runtimes, three application units, container health and models with the checks above.
Use `systemctl show -p ActiveEnterTimestampMonotonic` on the runtimes, target and applications
to confirm containerd → Docker → target → clients (socket precedes Docker). Mounts precede
containerd. If unlock fails, leave retained directories in place and use rollback.

**Cleanup only after that proof and the owner's go.** This destroys the retained fallback;
rollback above is no longer usable afterward. Do not remove the bulk copy or any main
originals. Run in a root shell, with the successful reboot/unlock checks recorded externally:

```bash
set -euo pipefail
findmnt --mountpoint /srv/homelab-bulk
test "$(docker info --format '{{.DockerRootDir}}')" = /srv/homelab-bulk/docker
MOUNT_TARGETS=$(findmnt -rn -o TARGET)
for path in /var/lib/docker.pre-bulk /var/lib/containerd.pre-bulk /srv/homelab/models; do
  test -d "$path"
  test ! -L "$path"
  if printf '%s\n' "$MOUNT_TARGETS" | grep -F "$path"; then exit 1; fi
done
rm -rf --one-file-system /var/lib/docker.pre-bulk /var/lib/containerd.pre-bulk /srv/homelab/models
df -h / /srv/homelab /srv/homelab-bulk
```

## Weekly originals copy

With the Mac listener installed and its private URL configured on the node, plug the volume
**SD Card** into the Mac and send `/backup` to the Telegram bot as a privileged user. No mounted
card replies “Connect the SD card and send /backup again.” Success replies
“Copy done: N KB, <file name>.” Failure replies “Copy failed: <short reason>.” If the Mac is
asleep, away or off, it replies “The Mac is not reachable; wake it and send /backup again.”
The Mac must be awake and on Tailscale; see [Mac action listener](mac.md#mac-action-listener).

The manual command remains `mac/homelab-copy '<VOLUME-NAME>'` from this checkout (or pass
`/Volumes/<NAME>`). Python 3 is required on the Mac. The command
accepts mounted removable or ejectable volumes, including cards in a built-in SD reader.
It refuses the startup volume, every APFS volume in its boot container, fixed internal disks,
and existing `homelab-originals-YYYY-MM-DD-HHMM.tar.age`
files. Names use **local time** to the minute. A collision in the **same minute** requires
waiting for another minute, choosing another card or explicitly moving the existing file.
It prints every `*-data/originals` folder and the inbox when present, with each size, and streams one tar archive through
age on the node directly onto the card, flushes the card, compares encrypted byte counts,
then records the successful copy on the node. Folder sizes are allocated bytes (`du -sk`
multiplied by 1024); the archive count is its exact encrypted length. Failed/partial files remain on the card for
inspection; they are never confirmed or overwritten. No archive is staged on either disk.
The archive contains paths relative to `/srv/homelab`, with numeric owners and modes.
Do this while originals are idle: tar is not a transactional snapshot of changing files.
Sources such as med-ask's book PDFs, databases, models and code are excluded.

The committed recipient is public; the node cannot decrypt these copies. The private age
key remains only in the owner's password manager. `sudo homelab-copy status` reports the
last successful timestamp (Unix seconds) and encrypted byte count, or `null` before the first
copy. Root-owned state lives in `/var/lib/homelab-copy/` with mode `0700` (record `0600`).
The daily persistent `homelab-copy-reminder.timer` sends one Telegram message per check
when the copy is strictly more than seven days old or missing, including while the data
volume is locked; the reminder says to plug the SD card into the Mac and send `/backup`.
Fresh copies produce no message. The service uses the existing TPM-sealed
Telegram credential and allowlist through `homelab-notify.sh "message text"`.

## Restoring originals from a copy

The owner performs the restore, first into an empty external volume directory. Obtain the
private age identity from the password manager and enter it at the hidden prompt below.
Do not put it in shell history, an argument, a file or this repository. On the Mac, with
Homebrew `age` installed, use Bash process substitution to keep the identity in memory:

```bash
bash
set -o pipefail
mkdir '/Volumes/<RESTORE-VOLUME>/restored-originals'
read -r -s -p 'age private identity: ' COPY_IDENTITY; printf '\n'
age --decrypt --identity <(printf '%s\n' "$COPY_IDENTITY") \
  '/Volumes/<COPY-VOLUME>/homelab-originals-YYYY-MM-DD-HHMM.tar.age' | \
  tar -xpf - -C '/Volumes/<RESTORE-VOLUME>/restored-originals'
COPY_RESTORE_STATUS=$?
unset COPY_IDENTITY
printf 'restore exit status: %s\n' "$COPY_RESTORE_STATUS"
```

Check the pipeline exit status and inspect the restored files. Each `<project>-data/originals/`
goes back to `/srv/homelab/<project>-data/originals/` on the unlocked rebuilt node. Restore
`frontline/inbox/` to `/srv/homelab/frontline/inbox/`, owned by `homelab-bot`. As root, apply
ownership and modes after extraction:

```bash
chown -R homelab-bot:homelab-bot /srv/homelab/frontline/inbox
find /srv/homelab/frontline/inbox -type d -exec chmod 0700 {} +
find /srv/homelab/frontline/inbox -type f -exec chmod 0600 {} +
```

Do not restore the planning clone, orders or markers.
`homelab-v2-data/originals/corpus/about-aleix/` contains `corpus.json` and `eval.json`;
use it instead of the Mac corpus copy in rebuild step 11. Copy directories without changing
their inner layout; as root on Linux restore numeric ownership and modes from the archive
(`tar --numeric-owner -xpf -`) or apply each project's known ownership after a Mac restore.
med-ask's originals directory is `root:10001` `2770`; preserve its file modes and do not
replace an active application's originals. The owner should compare the first restore with
the source before relying on the weekly copies. Rebuild code and derived stores separately.


## Management reads

From the node, these loopback reads need no key:

```sh
curl -s 127.0.0.1:8000/v1/manage/routes
```

Routes are grouped by purpose in table order, including wildcards and native media purposes.
`live` is the first configured enabled route, not an observation of traffic; `health` is its
provider's state. All current routes are enabled.

```sh
curl -s 127.0.0.1:8000/v1/manage/providers
```

Providers list their kind, state and purposes. Local states come from `/models` checks with a
two-second timeout and a 30-second cache; hosted states mean configured, not tested. Both
responses include the SHA-256 version of the route table and omit addresses and keys.
