"""Client for the installed launcher's JSON/SCM_RIGHTS interface."""

import json
import os
import selectors
import socket
import threading
import time

SOCKET = "/run/homelab-launcher.sock"
active = set()
mutex = threading.Lock()


class Refused(Exception):
    """A fixed launcher failure; response content is never logged."""


class LostLauncher(BaseException):
    """Stop could not be confirmed; never continue to another task or attempt."""

    def __init__(self, run_id):
        super().__init__("launcher_stop_failed")
        self.run_id = run_id


def stop_run(run_id):
    try:
        request({"op": "stop", "run_id": run_id})
    except Refused as error:
        if str(error) == "unknown_run":
            return
        raise LostLauncher(run_id) from None


def request(value, descriptors=(), *, timeout=30):
    data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
    if len(data) > 65536:
        raise Refused("launcher_request_too_large")
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(timeout)
            connection.connect(SOCKET)
            socket.send_fds(connection, [data], list(descriptors))
            with connection.makefile("rb") as stream:
                answer = json.loads(stream.readline(65537))
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise Refused(
                "unknown_run"
                if value.get("op") == "stop"
                and isinstance(answer, dict)
                and answer.get("error") == "unknown_run"
                else "launcher_refused"
            )
        return answer
    except (OSError, ValueError):
        raise Refused("launcher_unavailable") from None


def healthy():
    return request({"op": "ping"}, timeout=3) == {"ok": True}


def stop_all():
    with mutex:
        ids = list(active)
    for run_id in ids:
        stop_run(run_id)


def capture(
    value,
    stdout_path,
    stderr_path,
    seconds,
    *,
    stdin=None,
    limit=256 * 1024 * 1024,
    cancelled=lambda: False,
):
    """Drain launcher output pipes with the host runner's deadline and exact byte limit."""
    run_id = value["run_id"]
    if cancelled():
        raise Refused("launcher_cancelled")
    result = []
    out_read, out_write = os.pipe()
    err_read, err_write = os.pipe()

    def execute():
        try:
            descriptors = [out_write, err_write]
            if stdin is not None:
                descriptors.append(stdin.fileno())
            result.append(
                request(value, descriptors, timeout=seconds + 60 if seconds is not None else None)
            )
        except Exception:
            result.append(None)
        finally:
            os.close(out_write)
            os.close(err_write)

    thread = threading.Thread(target=execute, daemon=True)
    with mutex:
        active.add(run_id)
    stopped, written = "exited", 0
    safe = False
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            with selectors.DefaultSelector() as selector:
                selector.register(out_read, selectors.EVENT_READ, out)
                selector.register(err_read, selectors.EVENT_READ, err)
                thread.start()
                deadline = time.monotonic() + seconds if seconds is not None else None
                while selector.get_map():
                    if cancelled():
                        stopped = "interrupted"
                        break
                    remaining = deadline - time.monotonic() if deadline is not None else 1
                    if remaining <= 0:
                        stopped = "timeout"
                        break
                    for key, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fd)
                            continue
                        written += len(chunk)
                        if written > limit:
                            stopped = "output_limit"
                            break
                        key.data.write(chunk)
                    if stopped != "exited":
                        break
        if stopped != "exited":
            stop_run(run_id)
            safe = True
        thread.join(35)
        if thread.is_alive() or not result or result[0] is None:
            # A lost answer may own a live container; stop before any next mount/claim.
            stop_run(run_id)
            safe = True
            raise Refused("launcher_run_failed")
        if result[0]["exit_code"] == 125:
            # The launcher also uses 125 for failed cleanup. Confirm removal
            # before a check or another attempt can mount the same work.
            stop_run(run_id)
        safe = True
        return (result[0]["exit_code"] if stopped == "exited" else None), stopped
    except BaseException:
        if thread.ident is not None and not safe:
            stop_run(run_id)
            safe = True
            thread.join(35)
        raise
    finally:
        os.close(out_read)
        os.close(err_read)
        if thread.ident is None:
            os.close(out_write)
            os.close(err_write)
        if safe or thread.ident is None:
            with mutex:
                active.discard(run_id)
