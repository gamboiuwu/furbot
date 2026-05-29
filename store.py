"""Persistent JSON store with an optional Nextcloud (WebDAV) backing.

Behaviour:
  * Always keeps a local copy in DATA_DIR (fast, and a fallback).
  * If a WebDAV client is supplied, loads from Nextcloud on startup and
    uploads after every change, so data survives Railway redeploys.
  * If Nextcloud is unreachable, it logs and keeps working off the local
    copy — a reject/verify never fails because storage hiccupped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from webdav import WebDAVClient

log = logging.getLogger("furbot.store")


class Store:
    def __init__(
        self,
        *,
        local_path: str | Path,
        remote_name: str,
        webdav: WebDAVClient | None = None,
    ) -> None:
        self.local_path = Path(local_path)
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.remote_name = remote_name
        self.webdav = webdav
        self._data: dict[str, Any] = {}

    # ---- loading / saving ------------------------------------------------

    async def load(self) -> None:
        """Populate from Nextcloud if available, else from the local file."""
        data: dict[str, Any] | None = None
        if self.webdav:
            try:
                raw = await self.webdav.download(self.remote_name)
                if raw:
                    data = json.loads(raw.decode("utf-8"))
                    log.info("Loaded %s from Nextcloud.", self.remote_name)
            except Exception:
                log.exception("Nextcloud load failed for %s; falling back to local copy.", self.remote_name)
        if data is None:
            data = self._read_local()
        self._data = data
        self._write_local()  # keep local copy fresh

    def _read_local(self) -> dict[str, Any]:
        if not self.local_path.exists():
            return {}
        try:
            return json.loads(self.local_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read %s; starting empty.", self.local_path)
            return {}

    def _write_local(self) -> None:
        tmp = self.local_path.with_suffix(self.local_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
        tmp.replace(self.local_path)

    async def _persist(self) -> None:
        self._write_local()
        if self.webdav:
            try:
                await self.webdav.upload(
                    self.remote_name, json.dumps(self._data, indent=2).encode("utf-8")
                )
            except Exception:
                log.exception("Nextcloud upload failed for %s; kept local copy.", self.remote_name)

    # ---- reads (sync, from memory) --------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    # ---- writes (async, persisted) --------------------------------------

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        await self._persist()

    async def delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            await self._persist()

    async def update(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        """Apply several changes atomically, then persist once."""
        mutator(self._data)
        await self._persist()
