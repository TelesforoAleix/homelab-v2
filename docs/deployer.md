# Application deployer

`homelab-deployer` is a socket-activated root helper using only Python's standard library.
It deploys an application image from a full, lowercase 40-character commit SHA on `main`.
This installation includes only `test-app`; the platform stack still uses its existing
manual Compose deployment. Every node install still needs the owner's go.

An install verifies the manifest and GitHub ancestry, downloads a bounded tarball without
checking out Git, and compares guard-file hashes and executable bits with the installed
release. Added, removed and changed guards require an approving review by `TelesforoAleix`
on the commit's merged PR in that repository. First installs require that review too.
The latest decisive owner review must be approving; a dismissal or request for changes
supersedes an earlier approval. Reviews are fetched with the deployer's own read-only token.

It builds with the **installed** Dockerfile and the downloaded source. It always uses the
**installed** Compose file, including after an approved guard change: approval does not
copy host files out of a tarball. Changed host files need the owner's separate installation
from the committed tree, with diffs and validation as below. Compose receives only the
`HOMELAB_DEPLOY_TAG` variable; callers cannot supply paths, commands or Docker options.
There is no deployment of the platform stack through this helper yet.

Before migrating or switching it atomically writes and fsyncs an intent under
`/var/lib/homelab-deployer` (root:root 0700). Nonempty migrations first run the existing
`homelab-dump.service` and then `compose run` in the new image, inheriting the installed
service's user. This dump covers the platform PostgreSQL database; manifests needing a
different database backup are not supported by this slice. Migrations must only expand
schemas, so old code remains compatible. The helper never reverses a migration.
A failed or interrupted migration's fixed-name transient container is removed before
rollback, so killing the Compose client does not leave a migration running in the background.
Build and migration failures leave the old services running. A switch or health failure
starts the previous tag and checks it again. If neither release becomes healthy it stops
the application's services, records failure, retains the intent and starts
`homelab-notify@deployer.service`. It never deletes volumes.

Health verifies the image ID and running state of every listed service, then executes the
health command in the first service, up to three times two seconds apart. Each Docker call
has a timeout; builds allow 15 minutes. The helper uses the standard
[Compose run](https://docs.docker.com/reference/cli/docker/compose/run/) and
[Compose up](https://docs.docker.com/reference/cli/docker/compose/up/) commands.
Restart recovery checks the intended new image and health before accepting the release, otherwise starts the old tag and checks health.
An unresolved recovery prevents further installs. First-install failures have no previous
release: the services stop and the owner repairs the cause before restarting recovery.
The helper's lock serializes terminal commands, socket requests and recovery across processes.

Each application's 0600 JSON record carries the requested commit, previous commit, current
and rollback tags, guard hashes and a fixed result. Successful releases retain the current
and previous image (the two most recently started releases); failed images are removed
after a successful rollback. Image cleanup failures are recorded and retried on startup,
including builds abandoned before their intent. Other applications' images and all volumes
are untouched.
Records, replies, journal messages and alerts contain only identifiers, SHAs, paths, counts
and short fixed reasons. Docker and GitHub diagnostics are suppressed. Source lives only
in root-private temporary build folders; the credential is outside every build context.

## Manifest

`/etc/homelab-deployer/<application>.json` is root:root 0644, with exactly these fields:

```json
{
  "repository": "TelesforoAleix/homelab-v2",
  "services": ["test-app"],
  "health": ["/bin/sh", "-c", "test \"$(cat /app/health)\" = healthy"],
  "migration": [],
  "guards": [".github", "node", "compose.yaml", "Dockerfile", ".dockerignore", "config"],
  "compose": "test-app/compose.yaml",
  "dockerfile": "test-app/Dockerfile"
}
```

The application and service identifiers are lowercase letters, digits and hyphens, starting
with a letter, at most 63 characters. All listed services use
`homelab-deployer-<application>:${HOMELAB_DEPLOY_TAG}` and share the built image.
The health and migration arrays are argv, run inside the first service; an empty migration
skips migration and dump. No host shell interprets these arrays. Paths are literal relative
paths without traversal, options, globs or symlinks. Compose and Dockerfile paths are relative
to the fixed `/opt/homelab-deployer` root and must end in `compose.yaml` and `Dockerfile`.
The helper rejects unknown fields and unsafe types, modes, owners, symlinks and tar members.
Archives allow only regular files and directories: at most 30,000 entries, 64 MiB compressed
and 256 MiB expanded. Application repositories containing links are refused.

Guard paths select exact repository files or directory prefixes, including absent paths.
All of `node/`, `.github/`, every `compose.yaml` and every `Dockerfile` are mandatory guards
in addition to the manifest's paths. This includes the helpers and data, host scripts and
units, proxy allow lists, fence probe, GitHub workflows and CODEOWNERS. Guard changes list
paths in the reply. The shipped manifest also protects `.dockerignore` and `config/`.

## Requests

The owner's root terminal commands authorize an install or rollback:

```sh
sudo homelab-deployer install test-app <full-sha>
sudo homelab-deployer rollback test-app
```

They require root and a terminal (use `ssh -t homelab-agent` when appropriate). Rollback
verifies the previous commit and guards, writes an intent, starts its retained tag and
checks health. It does not build or migrate. It can fail back to the release it replaced.
Both operations print one JSON result and exit nonzero on failure.

The socket `/run/homelab-deployer.sock` is root:homelab-deploy 0660. It takes one UTF-8 JSON
line of at most 4096 bytes and replies with one JSON line:

```json
{"op":"install","application":"test-app","sha":"0123456789012345678901234567890123456789"}
```

Rollback uses `{"op":"rollback","application":"test-app"}`. Linux peer credentials must
match `/etc/homelab-deployer/callers.json`, currently only uid 0 with install and rollback.
The application must also appear in `/etc/homelab-deployer/automatic`, one name per line,
for either socket operation. This file ships empty: every socket install returns
`waits for the owner`. Editing it is itself a guard change. Neither membership in the socket
group nor access to the socket grants permission to deploy. No sudoers rule is added.

## Install host files

After merge, on the owner's go, use the committed tree in
`/srv/homelab/homelab-v2` after a manual `git pull --ff-only`. Show every installed-file diff
before copying. Preserve previous helper, manifest, test-app guard files, notifier and units
in a root-only backup outside the build context first. The token is never part of the backup
or the repository. The orchestrator installs files; the owner places the token separately.

```sh
sudo diff -uN /usr/local/sbin/homelab-deployer node/usr/local/sbin/homelab-deployer
sudo diff -ruN /etc/homelab-deployer node/etc/homelab-deployer --exclude=github-token
sudo diff -ruN /opt/homelab-deployer/test-app node/opt/homelab-deployer/test-app
sudo diff -uN /usr/local/sbin/homelab-notify.sh node/usr/local/sbin/homelab-notify.sh
sudo diff -uN /etc/systemd/system/homelab-deployer.socket node/etc/systemd/system/homelab-deployer.socket
sudo diff -uN /etc/systemd/system/homelab-deployer.service node/etc/systemd/system/homelab-deployer.service

sudo groupadd --system --force homelab-deploy
sudo install -d -m 0755 -o root -g root /etc/homelab-deployer /opt/homelab-deployer /opt/homelab-deployer/test-app
sudo install -d -m 0700 -o root -g root /var/lib/homelab-deployer
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-deployer /usr/local/sbin/homelab-deployer
sudo install -m 0755 -o root -g root node/usr/local/sbin/homelab-notify.sh /usr/local/sbin/homelab-notify.sh
sudo install -m 0644 -o root -g root node/etc/homelab-deployer/callers.json node/etc/homelab-deployer/automatic node/etc/homelab-deployer/test-app.json /etc/homelab-deployer/
sudo install -m 0644 -o root -g root node/opt/homelab-deployer/test-app/* /opt/homelab-deployer/test-app/
sudo install -m 0644 -o root -g root node/etc/systemd/system/homelab-deployer.socket node/etc/systemd/system/homelab-deployer.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/homelab-deployer.socket /etc/systemd/system/homelab-deployer.service
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-deployer.socket
```

The notifier and units are also covered by the operations runbook's wildcard installation;
install them only once. These additional directory modes supplement that runbook:

| Repository directory | Directory mode | File mode |
|---|---|---|
| `node/etc/homelab-deployer/` | `0755` | `0644` |
| `node/opt/homelab-deployer/test-app/` | `0755` | `0644` |

The owner creates `/etc/homelab-deployer/github-token`, root:root **0600**, containing a
read-only fine-grained GitHub token scoped to the manifest repository with Contents and
Pull requests read permission. It is read directly from that exact path, never accepted
from the caller or environment, never sent to the archive redirect host, and never passed
to Docker. The helper does not create or change this secret. There is no credential today;
the tests fake GitHub and Docker.

The node acceptance step is: install an owner-reviewed good `test-app` commit; install an
owner-reviewed commit whose `health` source says `unhealthy` and observe automatic rollback;
then interrupt the active terminal helper after its intent appears and restart
`homelab-deployer.service` to observe recovery to a healthy release. Restarting the idle
service alone does not interrupt a separate terminal command. Both test
commits must already be on `main`; changing this fixture under `node/` requires owner review.
Check the JSON records, retained tags and notification on double failure without displaying
the token or application content. Leave `automatic` empty throughout. These are node checks,
not assertions that fake-backed local tests can establish.

## Roll back the helper

Stop the socket first to prevent activation, then stop the helper. This leaves application
containers running. Restore the backed-up helper, configuration, installed Compose/Dockerfile,
notifier and unit files with their original modes and root ownership after showing diffs;
leave the credential and state alone. Run `systemd-analyze verify` on the restored units,
`systemctl daemon-reload`, then enable/start the socket again. Any retained intent is recovered
on service startup. For a first installation with no prior helper, disable the socket and
stop the service; application state and images can remain for inspection. No data deletion
or volume cleanup is part of helper rollback. Application rollback uses the command above.
