"""Minimal async WebDAV client for storing the bot's data on Nextcloud.

Nextcloud exposes every user's files over WebDAV at:
    https://<host>/remote.php/dav/files/<username>/<path>

We only need three operations: download a file, upload a file, and make
sure the target folder exists. Auth uses an *app password* (created in
Nextcloud → Settings → Security), never your main account password.
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("furbot.webdav")

# Reasonable timeout so a hung Nextcloud doesn't stall the bot.
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class WebDAVClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base = base_url.rstrip("/")
        self._auth = aiohttp.BasicAuth(username, password)

    def _url(self, name: str) -> str:
        return f"{self.base}/{name.lstrip('/')}"

    async def ensure_base(self) -> None:
        """Create the base folder if it doesn't already exist (best-effort)."""
        async with aiohttp.ClientSession(auth=self._auth, timeout=_TIMEOUT) as s:
            async with s.request("MKCOL", self.base) as r:
                # 201 = created, 405 = already exists. Anything else we log.
                if r.status not in (201, 405):
                    log.warning("MKCOL %s returned %s", self.base, r.status)

    async def download(self, name: str) -> bytes | None:
        """Return the file's bytes, or None if it doesn't exist (404)."""
        async with aiohttp.ClientSession(auth=self._auth, timeout=_TIMEOUT) as s:
            async with s.get(self._url(name)) as r:
                if r.status == 404:
                    return None
                r.raise_for_status()
                return await r.read()

    async def upload(self, name: str, data: bytes) -> None:
        async with aiohttp.ClientSession(auth=self._auth, timeout=_TIMEOUT) as s:
            async with s.put(self._url(name), data=data) as r:
                r.raise_for_status()

    async def check(self) -> bool:
        """Quick connectivity/credentials test. Returns True if reachable."""
        try:
            async with aiohttp.ClientSession(auth=self._auth, timeout=_TIMEOUT) as s:
                async with s.request("PROPFIND", self.base, headers={"Depth": "0"}) as r:
                    return r.status < 400
        except (aiohttp.ClientError, OSError):
            log.exception("WebDAV connectivity check failed")
            return False
