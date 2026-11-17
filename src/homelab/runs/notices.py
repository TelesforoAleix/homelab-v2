"""The same bounded Task notice, sent directly with per-service secret copies."""

import json
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS = Path("/run/secrets")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def notify(text):
    text = "Task: " + "".join(
        c
        for c in text.replace("\n", " ").replace("\r", " ")
        if not unicodedata.category(c).startswith("C")
    )
    try:
        token = (SECRETS / "bot-token").read_text().strip()
        recipients = []
        for line in (SECRETS / "bot-allowlist").read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line.isascii() and line.isdigit():
                recipients.append(line)
        if not token or not recipients:
            return False
        success = True
        # urllib has no request logger: a bot token is part of Telegram's URL.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        for chat in recipients:
            try:
                request = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=urllib.parse.urlencode({"chat_id": chat, "text": text[:1000]}).encode(),
                )
                with opener.open(request, timeout=7) as response:
                    if json.load(response).get("ok") is not True:
                        success = False
            except (OSError, ValueError):
                success = False
        return success
    except (OSError, ValueError):
        return False
