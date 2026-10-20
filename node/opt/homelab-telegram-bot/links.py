"""Bounded public-page fetch with checked, pinned destinations on every redirect."""

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

READ_LIMIT = 1024 * 1024
TIMEOUT = 5


def public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return public_address(str(address.ipv4_mapped))
    if isinstance(address, ipaddress.IPv6Address) and (
        address.is_site_local or address.sixtofour is not None or address.teredo is not None
    ):
        return False
    return address.is_global and not (
        address.is_multicast or address.is_reserved or address.is_unspecified
    )


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.text = []
        self.in_title = False
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)
            if self.in_title:
                self.title.append(data)


def request(parts, address, remaining):
    port = parts.port or (443 if parts.scheme == "https" else 80)
    connection = http.client.HTTPConnection(parts.hostname, port, timeout=remaining)
    deadline = time.monotonic() + remaining
    timer = None
    try:
        # Connect numerically to precisely the checked address, with the original
        # hostname retained for TLS verification/SNI and HTTP Host.
        connection.sock = socket.create_connection((address, port), timeout=remaining)
        connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
        if parts.scheme == "https":
            connection.sock = ssl.create_default_context().wrap_socket(
                connection.sock, server_hostname=parts.hostname
            )
        checked_socket = connection.sock

        def expire():
            # Socket read timeouts alone permit indefinitely trickled headers.
            # Interrupt the underlying socket at the shared absolute deadline.
            try:
                checked_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        connection.inbox_timer = timer
        timer.start()
        connection.request(
            "GET",
            (parts.path or "/") + ("?" + parts.query if parts.query else ""),
            headers={
                "Accept": "text/html, text/plain",
                "Accept-Encoding": "identity",
                "User-Agent": "Homelab-Inbox/1",
            },
        )
        response = connection.getresponse()
        if time.monotonic() >= deadline:
            response.close()
            raise TimeoutError
        return connection, response, checked_socket
    except BaseException:
        if timer is not None:
            timer.cancel()
        connection.close()
        raise


def fetch(url, *, resolve=socket.getaddrinfo, connect=request, clock=time.monotonic):
    deadline = clock() + TIMEOUT
    for hop in range(4):
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ValueError("refused URL")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = list(
            dict.fromkeys(
                row[4][0] for row in resolve(parts.hostname, port, type=socket.SOCK_STREAM)
            )
        )
        if not addresses or not all(public_address(a) for a in addresses):
            raise ValueError("refused address")
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError
        connection, response, checked_socket = connect(parts, addresses[0], remaining)
        try:
            if response.status in {301, 302, 303, 307, 308}:
                if hop == 3 or not response.getheader("Location"):
                    raise ValueError("redirect limit")
                url = urljoin(url, response.getheader("Location"))
                continue
            if response.status != 200:
                raise ValueError("page unavailable")
            media_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if (
                media_type not in {"text/html", "text/plain"}
                or response.getheader("Content-Encoding", "identity") != "identity"
            ):
                raise ValueError("refused type")
            length = response.getheader("Content-Length")
            if length and (not length.isdecimal() or int(length) > READ_LIMIT):
                raise ValueError("page too large")
            raw = bytearray()
            while True:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise TimeoutError
                checked_socket.settimeout(remaining)
                chunk = response.read1(min(65536, READ_LIMIT + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > READ_LIMIT:
                    raise ValueError("page too large")
            text = raw.decode("utf-8", errors="replace")
            if media_type == "text/plain":
                return {"title": None, "text": text[:24000]}
            page = Page()
            page.feed(text)
            return {
                "title": " ".join(" ".join(page.title).split())[:300] or None,
                "text": " ".join(page.text)[:24000],
            }
        finally:
            timer = getattr(connection, "inbox_timer", None)
            if timer is not None:
                timer.cancel()
            response.close()
            connection.close()
    raise ValueError("redirect limit")
