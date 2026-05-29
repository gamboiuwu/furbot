"""A tiny JSON-file store for data that must outlive a restart.

Used for the verification cooldown list (pending unbans). It's intentionally
minimal: load the whole file into memory, mutate, and write it back
atomically. That's plenty for the small amount of data we keep.

Note on hosting: on an ephemeral filesystem (like Railway's default), this
file survives container *restarts* but not *redeploys*. To make it durable
across redeploys, attach a persistent volume and point DATA_DIR at it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("furbot.store")


class JsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read %s; starting empty.", self.path)
            return {}

    def _write(self) -> None:
        # Write to a temp file then atomically replace, so a crash mid-write
        # can't corrupt the real file.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
        tmp.replace(self.path)

    # --- dict-like helpers ------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._write()

    def delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self._write()

    def items(self) -> list[tuple[str, Any]]:
        return list(self._data.items())
