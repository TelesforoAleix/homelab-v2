"""Session lock plus a read-only cgroup check; neither Docker nor systemd access."""

from contextlib import contextmanager
from pathlib import Path

from homelab.tasks.store import RUNNER_LOCK as LOCK

HOST_SERVICES = Path("/run/host-services")


class Refused(Exception):
    """Fixed startup refusal."""


def legacy_inactive(root=HOST_SERVICES):
    try:
        # Mount the parent, not the service's ephemeral cgroup inode. Absence is inactive.
        if not root.is_dir():
            raise Refused("legacy_state_unavailable")
        service = root / "homelab-task-runner.service"
        try:
            events = dict(
                line.split() for line in (service / "cgroup.events").read_text().splitlines()
            )
        except FileNotFoundError:
            if service.exists():
                raise Refused("legacy_state_unavailable") from None
            return
        if events.get("populated") != "0":
            raise Refused("legacy_runner_active")
    except (OSError, ValueError):
        raise Refused("legacy_state_unavailable") from None


@contextmanager
def runner_lock(connection, root=HOST_SERVICES):
    legacy_inactive(root)
    acquired = connection.execute("SELECT pg_try_advisory_lock(%s, %s)", LOCK).fetchone()[0]
    if not acquired:
        raise Refused("runner_active")
    try:
        # Closes the startup race with the old runner's claim process acquiring the same lock.
        legacy_inactive(root)
        yield
    finally:
        connection.execute("SELECT pg_advisory_unlock(%s, %s)", LOCK)


@contextmanager
def host_run_lock(path=Path("/run/host-locks/homelab-task-run.lock")):
    """Also exclude the unchanged root script when an owner invokes a run by hand."""
    import fcntl

    try:
        stream = path.open("rb")
    except OSError:
        raise Refused("host_run_lock_unavailable") from None
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("manual_run_active") from None
        yield
