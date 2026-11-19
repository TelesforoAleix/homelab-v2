# Installing host files

This procedure covers changing and installing the files under `node/`: where they go, the
order of steps, the validators and the installed modes. Follow it whenever a merged change
touches `node/`, and when a part's document tells you to install one of its host files. The
part documents add their own diffs, restarts and checks; this page holds what they share.

## The rule

`node/<path>` installs at `/<path>`, owned by `root:root`. Restarting `homelab.service` builds
containers from the checkout but never installs a host file, so every host-file change is an
explicit step on the node, taken from the node's clone of merged `main` at
`/srv/homelab/homelab-v2` after a manual `git pull --ff-only`.

Two files are exceptions to "copy the whole file": `node/etc/crypttab` and `node/etc/fstab` are
**managed entries**. Never install them over the host tables; the
[bulk disk procedure](operations.md#bulk-disk-and-storage-migration) shows how the two managed
lines are merged into the operating system's entries. Credential drop-ins name TPM-sealed
credentials; credential contents and private allowlists are never in this tree. The base
files keep their installed bytes, including historical v1 comments; those comments need no v1
script or node GitHub key.

## Steps

1. Back up each installed file you will replace in a dated, root-only directory outside any
   build context.
2. Show each difference before copying: `sudo diff -u /<path> node/<path>`, or
   `diff -u /dev/null node/<path>` for a new file. Exit status 1 means a difference; any other
   non-zero status is an inspection error and a stop.
3. Run the validator the file has: `visudo -c` (or `visudo -cf <file>` before installing) for
   sudoers, `sshd -t` for SSH configuration, `systemd-analyze --generators=yes verify` for
   units, `dockerd --validate` and `containerd config dump` for the runtime configuration.
4. Copy with `sudo install -m <mode> -o root -g root node/<path> /<path>`, using the modes below.
5. After a unit or drop-in change run `sudo systemctl daemon-reload`, then restart only the
   affected unit.
6. A PR that changes `node/` updates [Rebuilding the node](operations.md#rebuilding-the-node)
   in the same PR, so a rebuild installs the same set of files.

## Installed modes

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
| `node/etc/containerd/` | `0755` | `0644` |
| `node/etc/sysctl.d/` | `0755` | `0644` |
| `node/etc/profile.d/` | `0755` | `0644` |
| `node/etc/tmpfiles.d/` | `0755` | `0644` |
| `node/etc/systemd/system/` | `0755` | `0644` |
| `node/etc/systemd/system/homelab-notify@.service.d/` | `0755` | `0644` |
| `node/etc/systemd/system/homelab-watchdog.service.d/` | `0755` | `0644` |
| `node/etc/systemd/system/homelab-telegram-bot.service.d/` | `0755` | `0644` |
| `node/etc/systemd/system/docker.service.d/` | `0755` | `0644` |
| `node/etc/systemd/system/docker.socket.d/` | `0755` | `0644` |
| `node/etc/systemd/system/containerd.service.d/` | `0755` | `0644` |
| `node/etc/sudoers.d/` | `0755` | `0440` |
| `node/etc/polkit-1/rules.d/` | `0750` | `0644` |
| `node/etc/homelab-deployer/` | `0755` | `0644` |
| `node/etc/homelab-launcher/` | `0755` | `0644` |
| `node/etc/homelab-task/` | `0755` | `0644` |
| `node/opt/homelab-telegram-bot/` | `0755` | `0644` |
| `node/opt/homelab-context/` | `0755` | `0644` |
| `node/opt/homelab-task/` | `0755` | `0644` |
| `node/opt/homelab-package-proxy/` | `0755` | `0644` |
| `node/opt/homelab-fence/` | `0755` | `0644` |
| `node/opt/homelab-fence/cli/` | `0755` | `0644` |
| `node/opt/homelab-fence/proxy/` | `0755` | `0644` |
| `node/opt/homelab-deployer/test-app/` | `0755` | `0644` |
| `node/usr/local/sbin/` | `0755` | `0755` |

Files created on the node rather than copied from this tree keep the modes their part
documents give (for example the launcher's locally generated `images` approval list is
`0600`, and the deployer's token is `0600`).
