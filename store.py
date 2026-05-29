"""Persistent store backed by Nextcloud (WebDAV), with a local cache.

Each top-level dataset is saved as its **own JSON file** inside the configured
folder — e.g. `settings.json`, `stats.json`, `audit.json`, `pending_unbans.json`,
`onboarding.reminded.json` — both locally (in DATA_DIR) and on Nextcloud. This
makes the data inspectable in your Bot Data folder rather than one opaque blob.

Behaviour:
  * On startup, load every `*.json` from Nextcloud (falling back to the local
    copies if Nextcloud is unreachable).
  * After each change, upload only the file(s) that actually changed.
  * Migrates the previous single `furbot-state.json` into per-key files once.
  * If the local dir isn't writable, fall back to a temp dir instead of crashing.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from webdav import WebDAVClient

log = logging.getLogger("furbot.store")

# The old combined file we migrate away from.
LEGACY_KEY = "furbot-state"


def _ensure_writable_dir(path: Path) -> Path:
    """Make sure `path` exists and is writable; otherwise fall back to a temp
    directory instead of crashing (e.g. a non-root container can't write /app)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "furbot"
        fallback.mkdir(parents=True, exist_ok=True)
        log.warning(
            "Data dir %s is not writable; using temporary %s instead. "
            "Set DATA_DIR to a writable path (or configure Nextcloud) for durability.",
            path, fallback,
        )
        return fallback


class Store:
    def __init__(self, *, local_dir: str | Path, webdav: WebDAVClient | None = None) -> None:
        self.local_dir = _ensure_writable_dir(Path(local_dir))
        self.webdav = webdav
        self._data: dict[str, Any] = {}

    # ---- paths -----------------------------------------------------------

    def _fname(self, key: str) -> str:
        return f"{key}.json"

    def _local(self, key: str) -> Path:
        return self.local_dir / self._fname(key)

    @staticmethod
    def _dump(value: Any) -> bytes:
        return json.dumps(value, indent=2).encode("utf-8")

    # ---- loading ---------------------------------------------------------

    async def load(self) -> None:
        data: dict[str, Any] = {}
        loaded_remote = False
        if self.webdav:
            try:
                for name in await self.webdav.list_json():
                    key = name[:-5]  # strip ".json"
                    if key == LEGACY_KEY:
                        continue
                    raw = await self.webdav.download(name)
                    if raw:
                        try:
                            data[key] = json.loads(raw.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            log.warning("Skipping non-JSON Nextcloud file %s", name)
                loaded_remote = True
                log.info("Loaded %d data file(s) from Nextcloud.", len(data))
            except Exception:
                log.exception("Nextcloud load failed; falling back to local copies.")
        if not loaded_remote:
            for p in self.local_dir.glob("*.json"):
                if p.stem == LEGACY_KEY:
                    continue
                try:
                    data[p.stem] = json.loads(p.read_text("utf-8"))
                except (json.JSONDecodeError, OSError):
                    log.warning("Skipping unreadable local file %s", p)

        self._data = data
        await self._migrate_legacy()
        self._write_all_local()

    async def _migrate_legacy(self) -> None:
        """Fold a previous single furbot-state.json into per-key files, once."""
        legacy: dict | None = None
        if self.webdav:
            try:
                raw = await self.webdav.download(self._fname(LEGACY_KEY))
                if raw:
                    legacy = json.loads(raw.decode("utf-8"))
            except Exception:
                legacy = None
        if legacy is None:
            p = self._local(LEGACY_KEY)
            if p.exists():
                try:
                    legacy = json.loads(p.read_text("utf-8"))
                except (json.JSONDecodeError, OSError):
                    legacy = None
        if not legacy:
            return

        # Existing per-key files win over the old combined values.
        added = False
        for k, v in legacy.items():
            if k not in self._data:
                self._data[k] = v
                added = True
        if added:
            log.info("Migrated legacy furbot-state.json into per-key files.")
        for k in list(self._data.keys()):
            await self._persist_key(k)
        # Remove the legacy file now that per-key files are written.
        if self.webdav:
            try:
                await self.webdav.delete(self._fname(LEGACY_KEY))
            except Exception:
                log.exception("Could not delete legacy file on Nextcloud")
        try:
            self._local(LEGACY_KEY).unlink()
        except OSError:
            pass

    # ---- local writes ----------------------------------------------------

    def _write_local(self, key: str) -> None:
        path = self._local(key)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data[key], indent=2), "utf-8")
        tmp.replace(path)

    def _write_all_local(self) -> None:
        for key in self._data:
            self._write_local(key)

    # ---- persistence (local + remote) -----------------------------------

    async def _persist_key(self, key: str) -> None:
        self._write_local(key)
        if self.webdav:
            try:
                await self.webdav.upload(self._fname(key), self._dump(self._data[key]))
            except Exception:
                log.exception("Nextcloud upload failed for %s; kept local copy.", self._fname(key))

    async def _delete_key_files(self, key: str) -> None:
        try:
            self._local(key).unlink()
        except OSError:
            pass
        if self.webdav:
            try:
                await self.webdav.delete(self._fname(key))
            except Exception:
                log.exception("Nextcloud delete failed for %s", self._fname(key))

    # ---- reads (sync, from memory) --------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    # ---- writes (async, persisted) --------------------------------------

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        await self._persist_key(key)

    async def delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            await self._delete_key_files(key)

    async def ensure_defaults(self, defaults: dict[str, Any]) -> None:
        """Create files for any missing datasets so the storage folder is
        populated up-front (idempotent — only writes keys that don't exist)."""
        for key, value in defaults.items():
            if key not in self._data:
                self._data[key] = value
                await self._persist_key(key)

    async def update(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        """Apply several changes, then persist only the files that changed."""
        before = {k: json.dumps(v, sort_keys=True) for k, v in self._data.items()}
        mutator(self._data)
        after_keys = set(self._data.keys())
        for key in set(before) - after_keys:        # removed
            await self._delete_key_files(key)
        for key in after_keys:                       # new or changed
            if key not in before or before[key] != json.dumps(self._data[key], sort_keys=True):
                await self._persist_key(key)
