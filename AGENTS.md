# Working in this repository

Read `README.md` and `ARCHITECTURE.md` first; `DECISIONS.md` when a choice looks odd.

- This repository describes what exists. Work arrives as self-contained task prompts; do not look
  for plans, roadmaps or research here or elsewhere.
- Work in slices that end with something runnable.
- `README`/`ARCHITECTURE` change when behaviour changes. Append to `DECISIONS.md` when a choice
  was not obvious. Write nothing else: no briefs, handovers, ADR files or phase numbers.
- Libraries own machinery (LlamaIndex, LangGraph, Procrastinate). Do not hand-roll a retriever,
  a graph runtime, a queue or a tool protocol.
- Applications ask for a purpose from `config/routes.yaml`; never name a provider or model in code.
- The eight rules in `ARCHITECTURE.md` are not optional. In particular: no secrets in git or
  images; loopback only; do not log content by default.
- `uv run pytest` before every commit.
- The node is reached as `ssh homelab-agent`, which has root through sudo; `ssh homelab` is the
  owner's session. Deployment is `sudo systemctl restart homelab.service`; the unit pulls
  `main` and builds images.
- Proceed without asking unless your plan includes a step that cannot be undone or carries a
  real security risk: deleting data or volumes, wiping or repartitioning a disk, touching a
  secret or the volume's encryption, a change that could leave the node unreachable, rewriting
  `main`'s history or loosening its protection, or a security concern the task prompt does not
  address. For those steps only, stop before taking them, say what each risks and how it would
  be recovered, and wait for the owner's go. Report your plan and what you did in the handover.
- Files under `node/` are installed at the same path on the node, from the committed tree. Show
  the `diff` against what is installed before copying, and run the validator a change has:
  `visudo -c`, `sshd -t`, `systemd-analyze verify`.
- A PR changing `node/` updates the ["Rebuilding the node" section](docs/operations.md#rebuilding-the-node) in the same PR.

## Host-file modes

These are installed modes, not Git's executable bit. Files are `root:root`. Directories are
`root:root` too, except the vendor-managed `polkit-1/rules.d`, which stays `root:polkitd`.
The `.d` rows cover each unit's drop-in directory.

| Repository directory | Directory mode | File mode |
|---|---|---|
| `node/etc/` | `0755` | `0644` |
| `node/etc/ssh/` | `0755` | — |
| `node/etc/ssh/sshd_config.d/` | `0755` | `0600` |
| `node/etc/ufw/` | `0755` | `0640` |
| `node/etc/docker/` | `0755` | `0644` |
| `node/etc/sysctl.d/` | `0755` | `0644` |
| `node/etc/profile.d/` | `0755` | `0644` |
| `node/etc/systemd/system/` | `0755` | `0644` |
| `node/etc/systemd/system/homelab-notify@.service.d/` | `0755` | `0644` |
| `node/etc/systemd/system/homelab-watchdog.service.d/` | `0755` | `0644` |
| `node/etc/systemd/system/homelab-telegram-bot.service.d/` | `0755` | `0644` |
| `node/etc/sudoers.d/` | `0755` | `0440` |
| `node/etc/polkit-1/rules.d/` | `0750` | `0644` |
| `node/opt/homelab-telegram-bot/` | `0755` | `0644` |
| `node/usr/local/sbin/` | `0755` | `0755` |
