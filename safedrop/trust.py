"""Per-(peer, tool) trust policy with on-disk persistence.

Stored as a small JSON file (default ``~/.safedrop/trust.json``). Three
possible decisions:

    "allow"  — auto-accept future CALL_TOOL from this (peer, tool) pair
    "deny"   — auto-reject ditto
    "ask"    — fall through to the authorizer callback (GUI dialog or
               whatever the host wires up); this is the default for any
               pair that's never been confirmed before, and the entry
               is *not* persisted.

The GUI's "Always allow" / "Always deny" buttons call ``set(...)``;
"Allow once" / "Deny once" don't touch the store. Users can revoke a
trusted (peer, tool) via ``clear(...)``.

A persistent audit log writer is also here for convenience — same
directory, append-only JSONL.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any


DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_ASK = "ask"

DEFAULT_DIR = Path.home() / ".safedrop"
DEFAULT_TRUST_PATH = DEFAULT_DIR / "trust.json"
DEFAULT_AUDIT_PATH = DEFAULT_DIR / "audit.jsonl"


class TrustPolicy:
    """Thread-safe per-(peer_device_id, tool_name) decision store."""

    def __init__(self, store_path: Path | None = DEFAULT_TRUST_PATH) -> None:
        self.store_path = Path(store_path) if store_path else None
        self._lock = threading.Lock()
        self._policies: dict[str, dict[str, str]] = {}
        self.load()

    def load(self) -> None:
        if self.store_path is None or not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            policies = data.get("policies") if isinstance(data, dict) else None
            if isinstance(policies, dict):
                self._policies = {
                    str(k): {str(tk): str(tv) for tk, tv in (v or {}).items()}
                    for k, v in policies.items()
                    if isinstance(v, dict)
                }
        except Exception:
            self._policies = {}

    def save(self) -> None:
        if self.store_path is None:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "policies": self._policies}
        # Write atomically.
        tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.store_path)

    def check(self, peer_device_id: str, tool_name: str) -> str:
        with self._lock:
            return self._policies.get(peer_device_id, {}).get(tool_name, DECISION_ASK)

    def set(self, peer_device_id: str, tool_name: str, decision: str) -> None:
        if decision not in (DECISION_ALLOW, DECISION_DENY, DECISION_ASK):
            raise ValueError(f"invalid decision {decision!r}")
        with self._lock:
            if decision == DECISION_ASK:
                # Treat "ask" as removal.
                self._policies.get(peer_device_id, {}).pop(tool_name, None)
                if peer_device_id in self._policies and not self._policies[peer_device_id]:
                    del self._policies[peer_device_id]
            else:
                self._policies.setdefault(peer_device_id, {})[tool_name] = decision
        self.save()

    def clear(self, peer_device_id: str, tool_name: str | None = None) -> None:
        with self._lock:
            if peer_device_id not in self._policies:
                return
            if tool_name is None:
                del self._policies[peer_device_id]
            else:
                self._policies[peer_device_id].pop(tool_name, None)
                if not self._policies[peer_device_id]:
                    del self._policies[peer_device_id]
        self.save()

    def list_for_peer(self, peer_device_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._policies.get(peer_device_id, {}))

    def snapshot(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {k: dict(v) for k, v in self._policies.items()}


class AuditWriter:
    """Append-only JSONL writer for cross-device tool calls."""

    def __init__(self, path: Path | None = DEFAULT_AUDIT_PATH) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def append(self, entry: Any) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            row = self._render(entry)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # Audit writer must never crash callers.
            pass

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            return []
        return rows[-limit:]

    @staticmethod
    def _render(entry: Any) -> dict[str, Any]:
        # Accept ToolCallAuditEntry (dataclass) or a plain dict.
        try:
            row = asdict(entry)
        except TypeError:
            row = dict(entry)
        # Standardise the timestamp + drop None/empty fields for compactness.
        if "timestamp" in row and isinstance(row["timestamp"], (int, float)):
            row["timestamp_iso"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(row["timestamp"])
            )
        return {k: v for k, v in row.items() if v not in (None, "", {}, [])}
