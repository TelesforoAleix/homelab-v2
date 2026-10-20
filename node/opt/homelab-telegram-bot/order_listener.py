"""Two HTTP routes on the runtime Tailscale IPv4; tailnet policy is the gate."""

import asyncio
import ipaddress
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

BODY_LIMIT = 4096
PORT = 8110


def tailscale_address():
    result = subprocess.run(
        ["/usr/bin/tailscale", "ip", "-4"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    address = ipaddress.IPv4Address(result.stdout.strip())
    if address not in ipaddress.IPv4Network("100.64.0.0/10"):
        raise ValueError("Tailscale IPv4 required")
    return str(address)


def handler_for(orders):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass

        def respond(self, code, body=None):
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def route(self):
            ack = re.fullmatch(r"/orders/([0-9a-f]{32})/ack", self.path)
            if not (
                (self.command == "GET" and self.path == "/orders")
                or (self.command == "POST" and ack)
            ):
                self.respond(404)
                return
            try:
                if not orders.available():
                    self.respond(503)
                    return
                if self.command == "GET":
                    self.respond(200, {"orders": orders.queued()})
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (
                    self.headers.get("Transfer-Encoding") is not None
                    or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]+", lengths[0])
                ):
                    self.respond(400)
                    return
                size = int(lengths[0])
                if size > BODY_LIMIT:
                    self.respond(413)
                    return
                raw = self.rfile.read(size)
                if len(raw) != size:
                    self.respond(400)
                    return
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    self.respond(400)
                    return
                self.respond(orders.ack(ack[1], body))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                # No exception, request path or body may reach stdlib error logging.
                self.respond(503)

        do_GET = route
        do_POST = route
        do_HEAD = route
        do_PUT = route
        do_PATCH = route
        do_DELETE = route
        do_OPTIONS = route

        def send_error(self, code, message=None, explain=None):
            self.respond(404 if code == 501 else code)

    return Handler


class Server(HTTPServer):
    def handle_error(self, request, client_address):
        # Default socketserver prints tracebacks (potentially containing content).
        pass


class Listener:
    def __init__(self, orders, log):
        self.orders = orders
        self.log = log
        self.server = None
        self.thread = None

    def bind(self, address):
        # No fallback. The CLI result is checked even though it is runtime-local data.
        self.server = Server((address, PORT), handler_for(self.orders))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def run(self):
        while self.server is None:
            try:
                address = await asyncio.to_thread(tailscale_address)
                self.bind(address)
                self.log("orders listener ready")
            except (OSError, ValueError, subprocess.SubprocessError):
                self.log("orders listener unavailable; retry pending")
                await asyncio.sleep(5)

    async def stop(self):
        if self.server is not None:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
            self.thread.join(timeout=4)
            self.server = None
