"""Repository-scoped GitHub App tokens; private material never reaches a work copy."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from pathlib import Path

import httpx

REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class Refused(Exception):
    """A fixed failure code safe for metadata logs."""


def repository_name(value):
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value) or ".." in value:
        raise Refused("repository_refused")
    return value


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


class GitHubApp:
    def __init__(
        self,
        key=Path("/run/secrets/runs-app.pem"),
        config=Path("/run/secrets/runs-app.json"),
        *,
        client=None,
        clock=time.time,
    ):
        self.key, self.config, self.client, self.clock = Path(key), Path(config), client, clock

    def token(self, repository):
        repository_name(repository)
        try:
            settings = json.loads(self.config.read_bytes())
            if set(settings) != {"app_id", "installation_id"} or any(
                type(value) is not int or value <= 0 for value in settings.values()
            ):
                raise ValueError()
            now = int(self.clock())
            header = encoded(b'{"alg":"RS256","typ":"JWT"}')
            payload = encoded(
                json.dumps(
                    {"iat": now - 60, "exp": now + 540, "iss": str(settings["app_id"])},
                    separators=(",", ":"),
                ).encode()
            )
            message = header + b"." + payload
            signature = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", str(self.key)],
                input=message,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=True,
            ).stdout
            jwt = (message + b"." + encoded(signature)).decode("ascii")
            if self.client is not None:
                return self._mint(self.client, settings["installation_id"], repository, jwt)
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
                return self._mint(client, settings["installation_id"], repository, jwt)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            httpx.HTTPError,
            subprocess.SubprocessError,
        ):
            raise Refused("app_token_failed") from None

    @staticmethod
    def _mint(client, installation, repository, jwt):
        response = client.post(
            f"https://api.github.com/app/installations/{installation}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "homelab-runs",
            },
            json={
                "repositories": [repository.split("/")[1]],
                "permissions": {"contents": "write", "metadata": "read"},
            },
        )
        response.raise_for_status()
        token = response.json()["token"]
        if not isinstance(token, str) or not token or any(ord(c) < 33 for c in token):
            raise ValueError()
        return token
