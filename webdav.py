"""Minimal async WebDAV client for storing the bot's data on Nextcloud.

Nextcloud exposes every user's files over WebDAV at:
    https://<host>/remote.php/dav/files/<username>/<path>

We only need: download a file, upload a file, and ensure the target folder
(possibly several levels deep, possibly with spaces) exists. Auth uses an
*app password* (Nextcloud → Settings → Security), never your account password.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

import aiohttp
from yarl import URL

log = logging.getLogger("furbot.webdav")

# Reasonable timeout so a hung Nextcloud doesn't stall the bot.
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class WebDAVClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        # Normalize encoding: unquote anything already-encoded (e.g. "%20"),
        # then let yarl encode exactly once. This works whether the user
        # pasted "NYFurs Private", "NYFurs%20Private", etc.
        self.base = URL(unquote(base_url.rstrip("/")), encoded=False)
        self._auth = aiohttp.BasicAuth(username, password)

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(auth=self._auth, timeout=_TIMEOUT)

    def _folders_to_create(self) -> list[URL]:
        """The folder URLs below the user's root that may need creating,
        ordered shallowest-first. e.g. for .../files/<user>/A/B -> [.../A, .../A/B]."""
        parts = self.base.parts  # ('/', 'remote.php', 'dav', 'files', '<user>', 'A', 'B')
        try:
            user_idx = parts.index("files") + 1
        except ValueError:
            return [self.base]
        depth_below_user = len(parts) - (user_idx + 1)
        if depth_below_user <= 0:
            return []
        dirs: list[URL] = []
        url = self.base
        for _ in range(depth_below_user):
            dirs.append(url)
            url = url.parent
        dirs.reverse()
        return dirs

    async def ensure_base(self) -> None:
        """Create the target folder and any missing parent folders."""
        async with self._session() as s:
            for folder in self._folders_to_create():
                async with s.request("MKCOL", folder) as r:
                    # 201 created, 405 already exists. Others are worth seeing.
                    if r.status not in (201, 405):
                        log.warning("MKCOL %s returned HTTP %s", folder, r.status)

    async def download(self, name: str) -> bytes | None:
        """Return the file's bytes, or None if it doesn't exist."""
        async with self._session() as s:
            async with s.get(self.base / name, allow_redirects=False) as r:
                if r.status in (404, 401, 403) or 300 <= r.status < 400:
                    if r.status != 404:
                        log.warning("GET %s returned HTTP %s", name, r.status)
                    return None
                r.raise_for_status()
                return await r.read()

    async def upload(self, name: str, data: bytes) -> None:
        async with self._session() as s:
            async with s.put(self.base / name, data=data) as r:
                r.raise_for_status()

    async def delete(self, name: str) -> None:
        async with self._session() as s:
            async with s.delete(self.base / name) as r:
                if r.status not in (200, 204, 404):
                    r.raise_for_status()

    async def list_json(self) -> list[str]:
        """List the `.json` file names directly inside the base folder."""
        async with self._session() as s:
            async with s.request("PROPFIND", self.base, headers={"Depth": "1"}) as r:
                if r.status >= 400:
                    log.warning("PROPFIND %s returned HTTP %s", self.base, r.status)
                    return []
                text = await r.text()
        names: set[str] = set()
        for href in re.findall(r"<[^>]*?href[^>]*?>([^<]+)</[^>]*?href>", text, re.I):
            base_name = unquote(href.rstrip("/").rsplit("/", 1)[-1])
            if base_name.endswith(".json"):
                names.add(base_name)
        return sorted(names)

    async def check(self) -> bool:
        """Quick connectivity/credentials test. Returns True if reachable."""
        try:
            async with self._session() as s:
                async with s.request("PROPFIND", self.base, headers={"Depth": "0"}) as r:
                    if r.status >= 400:
                        log.warning("Nextcloud PROPFIND %s returned HTTP %s", self.base, r.status)
                    return r.status < 400
        except (aiohttp.ClientError, OSError):
            log.exception("WebDAV connectivity check failed")
            return False
