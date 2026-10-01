"""
Model helper client — Phase 09.

This is the ONLY part of Phase 09 that runs as `homelab-bot`. It is deliberately
the dullest file in the phase: it opens a UNIX socket, writes one JSON object,
reads one JSON object, and gives up cleanly if any of that fails.

Everything interesting -- the credentials, the CLIs, the caps, the choice of
model -- is on the other side of the socket, in a process this one cannot
inspect or influence beyond the question it sends.

WHAT THIS FILE MUST NOT DO
--------------------------
It must not gain the ability to read /home/aleix, and it must not become a
general-purpose way to run things as another user. Note that the request names
no provider, no model, no binary and no path: it carries a question and the
host status, and the helper decides everything else from root-owned config. A
bug in this file cannot redirect the helper at a different program, because
there is no field in which to name one.

FAILURE IS EXPECTED AND MUST BE LEGIBLE
---------------------------------------
The helper may be missing, unreachable, or refusing new connections. None of
those is a crash: they are things the owner should be told in a sentence. Phase
07 shipped a bot whose handler had no exception guard and restart-looped on a
missing /proc file; this returns a string in every path.
"""

from __future__ import annotations

import json
import socket

SOCKET_PATH = "/run/homelab-model-helper.sock"


def request(payload: dict, *, socket_path: str = SOCKET_PATH,
            timeout: float = 150.0) -> dict:
    """
    One request, one reply. Returns a dict; never raises.

    The timeout is longer than the helper's own 120s model timeout on purpose.
    If both sides used the same number, a slow-but-successful call would be
    reported as a failure by whichever side rounded down first.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        return {"ok": False, "kind": "error", "message": f"no socket: {exc}"}

    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))

        # Half-close. The helper reads one line and would otherwise wait for
        # more; shutting down the write side is what tells it the request is
        # complete without relying on it finding the newline first.
        sock.shutdown(socket.SHUT_WR)

        chunks = []
        total = 0
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 256 * 1024:
                return {"ok": False, "kind": "error",
                        "message": "helper reply too large"}
        raw = b"".join(chunks)
    except FileNotFoundError:
        return {"ok": False, "kind": "error",
                "message": "the model helper is not installed on this host"}
    except PermissionError:
        # The socket exists and the kernel refused us. That is the access
        # control working, and it is worth a distinct message: it means the
        # socket's group or mode is wrong, not that the helper is down.
        return {"ok": False, "kind": "error",
                "message": "not permitted to reach the model helper"}
    except (ConnectionRefusedError, ConnectionResetError):
        return {"ok": False, "kind": "error",
                "message": "the model helper refused the connection"}
    except socket.timeout:
        return {"ok": False, "kind": "error",
                "message": "the model helper did not reply in time"}
    except OSError as exc:
        return {"ok": False, "kind": "error", "message": f"helper unreachable: {exc}"}
    finally:
        sock.close()

    if not raw.strip():
        return {"ok": False, "kind": "error", "message": "the model helper said nothing"}

    try:
        reply = json.loads(raw.decode("utf-8", errors="replace").splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "kind": "error",
                "message": "the model helper sent something unreadable"}

    if not isinstance(reply, dict):
        return {"ok": False, "kind": "error", "message": "malformed helper reply"}
    return reply


def ping(*, socket_path: str = SOCKET_PATH) -> dict:
    """Prove the socket works without spending any allowance."""
    return request({"v": 1, "op": "ping"}, socket_path=socket_path, timeout=10.0)


def spend(user_id: int, *, socket_path: str = SOCKET_PATH) -> dict:
    """Read the governor through the helper; the bot gets no ledger path."""
    return request({"v": 1, "op": "spend", "user_id": user_id},
                   socket_path=socket_path, timeout=10.0)


def ask(question: str, context: str, user_id: int, *,
        socket_path: str = SOCKET_PATH) -> dict:
    return request({"v": 1, "op": "ask", "user_id": user_id,
                    "question": question, "context": context},
                   socket_path=socket_path)
