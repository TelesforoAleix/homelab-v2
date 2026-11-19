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

The directory and file mode of every repository directory under `node/` is in
[Installed modes](operations.md#installed-modes), inside the rebuild procedure that applies
them. Use those modes for every `sudo install`.
