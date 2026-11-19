# Node host

This document covers the node's host layer: the two encrypted volumes and their unlock order,
the host files under `node/`, recovery by rebuild, the originals copy and the code context
server. Read it before changing a volume user, a unit's mount conditions, `homelab-copy` or
`homelab-context`. Installing host files is [its own procedure](host-files.md).

## Data volumes

One `compose.yaml`: `api`, `worker`, `postgres` (pgvector image), `llama-embed` (llama.cpp
server), `llama-embed-large` (CPU Qwen3-Embedding-4B), `litellm` and `package-proxy` (tinyproxy, for
a task's prepare step). Only `api` publishes a port, on loopback.
`node/etc/systemd/system/homelab.service` wraps the stack so it starts after the encrypted
volume is unlocked (`homelab-data.target`) and
stops with it. Brain and the corpus are bind-mounted read-only; Postgres data stays on main.
Models, downloaded sources and generated images belong on the second SSD at
`/srv/homelab-bulk`. Its single LUKS filesystem holds `docker/`, `containerd/`, `models/`
and client folders. Main holds a root-only 4096-byte key (0400); bulk also has an independent
owner passphrase slot, so loss of either SSD does not prevent recovering the other.

Both volumes use noauto crypttab/fstab entries. `data-volume.sh unlock` opens main with
the owner’s passphrase, opens bulk with that key, mounts both, starts containerd and Docker
(with its socket), then starts the data target and clients. Retry after partial unlock is safe.
Lock stops clients and runtimes before closing bulk, then main. Docker, its socket and
containerd are disabled at boot and have mount conditions on both filesystems. Their drop-ins
order them after the mounts and stop them with the data target. Neither root is written while
locked. Docker’s default builder uses periodic GC with a 50 GB target, not a hard quota.

Volume users declare `ConditionPathIsMountPoint=/srv/homelab`, `After=homelab-data.target`,
`PartOf=homelab-data.target` and `WantedBy=homelab-data.target`. They do not require that target
or place `WorkingDirectory=`, `RootDirectory=` or `StateDirectory=` on the volume: those
directories create implicit mount dependencies before conditions are checked, turning a locked
volume into a failure alert. The stack instead passes Compose the absolute `-f` path; its
project name and relative paths come from `compose.yaml`.

## Host files and recovery

`node/` mirrors host paths and records the volume unlock script and target, failure notifier,
boot watchdog, stack unit and agent sudo grant. The notifier accepts only `bot`, `watchdog` and
`homelab`, mapping them to the bot, watchdog and v2 stack units. No v1 program, unit or
account remains on the node. Host files are installed from the node's clone of merged `main`
as [Installing host files](host-files.md) describes. The agent has root through sudo; the owner
approves execution plans and risks. The service restart rebuilds containers from the checkout
and never pulls, while host-file installation is explicit. Bot code, its unit, credential and
failure drop-ins, and the narrow polkit grant are recorded here. Private allowlists and secret
credential contents stay outside this tree.

Base SSH, ufw, Docker, sysctl, console timeout and Wi-Fi configuration also live in `node/`;
their installed modes are in [Installing host files](host-files.md#installed-modes). Recovery is the
[Rebuilding the node](operations.md#rebuilding-the-node) procedure: no node backup, a public code
clone, Brain from the owner's Mac, originals restored from an encrypted copy (or the
owner's Mac corpus), re-ingestion and re-issued secrets.
The optional planning mirror uses its own read-only GitHub deploy key; the public code clone
and Brain recovery need no GitHub credential on the node. Rebuild the Samsung independently; recover an
existing Micron using its owner passphrase, never reformat it. Provision a blank replacement
through the runbook.

## Originals copy

Every project's irreplaceable inputs live in `/srv/homelab/<project>-data/originals/`.
The corpus lives at `/srv/homelab/homelab-v2-data/originals/corpus/`, mounted read-only.
`homelab-copy stream` pipes all originals directories through tar and age to SSH stdout;
only a public recipient is installed. The Mac writes directly to a mounted removable or
ejectable volume, excluding the startup volume and every APFS volume in its boot container,
checks the reported encrypted byte count, then calls `confirm` to atomically record the
last-good time and bytes outside the encrypted volume. A daily persistent timer uses that
record and the existing notifier to remind the owner after seven days, or before any copy.
No archive is staged, and the private identity remains in the owner's password manager.

## Code context server

The code context server is an on-demand Serena MCP container reached through the existing
agent SSH account and stdio. It has no network or published port. A root-owned private list
selects application clones; a timer fetches their local `main` into separate copies under
`/srv/homelab/.context/`. Each container sees only its copy and its adjacent index. The source
and Serena project configuration are root-owned and unwritable to the unprivileged container
user; only the index directory is writable. A fixed six-tool allowlist permits symbol and
pattern reading, while editing, shell, onboarding and memory tools are absent. No application
clone is mounted. The image contains pinned Serena, Python and TypeScript language servers,
and needs no runtime network. Runtime logs and the copied language-server files are ephemeral.

Mac coding clients start an MCP stdio session through the existing agent account:

```bash
ssh -T homelab-agent sudo -n /usr/local/sbin/homelab-context studio
```

Replace `studio` with a configured name. The node's private root-owned list is
`/etc/homelab-context/repositories`, mode `0600`, one name per line; blank lines and `#`
comments are allowed. Names use lowercase letters, digits, underscore or hyphen and start
with a letter or digit. Each names a root-owned application clone at `/srv/homelab/<name>`.
The list is not part of Git. Add or remove names in that private file, then run
`sudo systemctl start homelab-context-refresh.service`. Removing a name stops serving it;
the previously created derived copy is retained. A missing list, unavailable copy, unknown
name or locked volume fails on stderr. The timer runs every five minutes and after unlock;
when a client is connected a refresh can change the copy while the client is reading it.

Copies and Serena's reusable symbol cache live under `/srv/homelab/.context/<name>/`,
with the parent private to root. The container has one bind mount for the selected copy and
its cache; root ownership keeps source and configuration read-only for its unprivileged user.
It has no network, capabilities or access to other host paths. The container filesystem is
read-only, with a temporary executable tmpfs for language-server binaries and logs. The six
MCP tools find symbols, file overviews, references, declarations, implementations and patterns;
there are no editing, shell, onboarding or memory tools. Source content is not logged by
default. The image build requires Internet access, but sessions and local refreshes do not.

After a merged update, install the files from the node checkout, showing diffs first as in
[Rebuilding the node](operations.md#rebuilding-the-node), then build and start only these new units:

```bash
sudo docker build -t homelab-context:cx1 /opt/homelab-context
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/homelab-context-refresh.service /etc/systemd/system/homelab-context-refresh.timer
sudo systemctl enable --now homelab-context-refresh.timer
sudo systemctl enable homelab-context-refresh.service
sudo systemctl start homelab-context-refresh.service
sudo systemctl status homelab-context-refresh.timer
```

Build again after updating the image files. Refresh and serving need both the data volume and
Docker; unknown names and locked volumes do not start a container. A new client starts its own
container; quitting the MCP session stops it.
