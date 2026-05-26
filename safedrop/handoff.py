"""Cross-device handoff store — v1.6 Continuity primitive.

The simplest piece of "state handoff" that's actually useful: a tiny
key-value store of text blobs that can be saved on one paired device
and loaded on another over the SafeDrop encrypted CALL_TOOL channel.

Typical usage::

    # On the laptop, in the middle of writing an email:
    handoff_save(key="email-draft", content="Dear Bob, ...")

    # On the phone in the elevator:
    handoff_load(key="email-draft")  # picks up where you left off

Persistence: ``~/.safedrop/handoff.json`` (atomic write, 0o600 on POSIX).

This is intentionally **not** a sync engine — there's no last-write-wins
merge, no vector clocks, no streaming. It's a primitive other features
build on (e.g. a clipboard ring, draft sync, recent-files list).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_PATH = Path.home() / ".safedrop" / "handoff.json"
MAX_CONTENT_LEN = 1_000_000  # 1 MB — anything bigger should be a file transfer


@dataclass
class HandoffEntry:
    key: str
    content: str
    mime_type: str = "text/plain"
    updated_at: float = field(default_factory=time.time)
    updated_by: str = ""   # peer label that wrote it; informational

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "mime_type": self.mime_type,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HandoffEntry":
        return cls(
            key=str(d.get("key") or "").strip() or uuid.uuid4().hex[:8],
            content=str(d.get("content") or ""),
            mime_type=str(d.get("mime_type") or "text/plain"),
            updated_at=float(d.get("updated_at") or time.time()),
            updated_by=str(d.get("updated_by") or ""),
        )

    def summary(self) -> dict[str, Any]:
        """Listing view — content collapsed to a preview to keep responses small."""
        return {
            "key": self.key,
            "preview": self.content[:120],
            "length": len(self.content),
            "mime_type": self.mime_type,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class HandoffStore:
    """Thread-safe persistent handoff store. Atomic on-disk writes."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.Lock()
        self._entries: dict[str, HandoffEntry] = {}
        self._load()

    # ---- persistence ----------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data.get("entries", []) if isinstance(data, dict) else []
            for d in entries:
                e = HandoffEntry.from_dict(d)
                self._entries[e.key] = e
        except Exception:
            self._entries = {}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "entries": [e.to_dict() for e in self._entries.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except Exception:
            pass
        os.replace(tmp, self.path)
        try:
            self.path.chmod(0o600)
        except Exception:
            pass

    # ---- public API -----------------------------------------------

    def save(self, key: str, content: str, mime_type: str = "text/plain",
             updated_by: str = "") -> HandoffEntry:
        if not key.strip():
            raise ValueError("handoff key must be non-empty")
        if len(content) > MAX_CONTENT_LEN:
            raise ValueError(
                f"handoff content too large ({len(content)} bytes); "
                f"max is {MAX_CONTENT_LEN}. Use a file transfer instead."
            )
        with self._lock:
            entry = HandoffEntry(
                key=key.strip(),
                content=content,
                mime_type=mime_type or "text/plain",
                updated_at=time.time(),
                updated_by=updated_by,
            )
            self._entries[entry.key] = entry
            self._save_locked()
            return entry

    def load(self, key: str) -> Optional[HandoffEntry]:
        with self._lock:
            return self._entries.get(key.strip())

    def list(self) -> list[HandoffEntry]:
        with self._lock:
            rows = list(self._entries.values())
        rows.sort(key=lambda e: e.updated_at, reverse=True)
        return rows

    def delete(self, key: str) -> bool:
        with self._lock:
            removed = self._entries.pop(key.strip(), None) is not None
            if removed:
                self._save_locked()
            return removed

    def clear(self) -> int:
        with self._lock:
            n = len(self._entries)
            self._entries.clear()
            if n > 0:
                self._save_locked()
            return n
