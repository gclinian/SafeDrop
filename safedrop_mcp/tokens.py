"""Capability tokens for the HTTP transport.

Used to authenticate a remote agent and bound its tool surface. Each
token has:

    label        — human-readable name
    scope        — list of tool-name globs (same fnmatch syntax as Policy)
    expires_at   — Unix seconds, or None for "no expiry"

Tokens are random 32-byte URL-safe strings, persisted to
``~/.safedrop/tokens.json``. The file is created with 0o600. There's no
revocation TTL beyond ``expires_at``; ``revoke`` simply removes a row.

For the local stdio MCP server tokens are not needed (the parent process
is implicitly trusted). They are only enforced by the HTTP transport.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .policy import Policy

DEFAULT_TOKEN_PATH = Path.home() / ".safedrop" / "tokens.json"


@dataclass
class CapabilityToken:
    token: str
    label: str
    scope: tuple[str, ...]
    expires_at: float | None = None
    created_at: float = field(default_factory=time.time)

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or time.time()) >= self.expires_at

    def to_policy(self) -> Policy:
        return Policy(allow_globs=self.scope, name_suffix=f"http:{self.label}")

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "label": self.label,
            "scope": list(self.scope),
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CapabilityToken":
        return cls(
            token=str(d["token"]),
            label=str(d.get("label", "")),
            scope=tuple(str(s) for s in (d.get("scope") or [])),
            expires_at=d.get("expires_at"),
            created_at=float(d.get("created_at") or time.time()),
        )


class TokenStore:
    """Thread-safe persistent token store."""

    def __init__(self, path: Path | None = DEFAULT_TOKEN_PATH) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._tokens: dict[str, CapabilityToken] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("tokens", []) if isinstance(raw, dict) else []
            for d in entries:
                t = CapabilityToken.from_dict(d)
                self._tokens[t.token] = t
        except Exception:
            self._tokens = {}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "tokens": [t.to_dict() for t in self._tokens.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except Exception:
            pass
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except Exception:
            pass

    def mint(
        self,
        label: str,
        scope: list[str] | tuple[str, ...] = (),
        ttl_seconds: float | None = None,
    ) -> CapabilityToken:
        with self._lock:
            tok = secrets.token_urlsafe(32)
            entry = CapabilityToken(
                token=tok,
                label=label or "unnamed",
                scope=tuple(scope),
                expires_at=(time.time() + ttl_seconds) if ttl_seconds else None,
            )
            self._tokens[tok] = entry
            self._save()
            return entry

    def revoke(self, token: str) -> bool:
        with self._lock:
            removed = self._tokens.pop(token, None) is not None
            if removed:
                self._save()
            return removed

    def validate(self, token: str) -> CapabilityToken | None:
        with self._lock:
            t = self._tokens.get(token)
            if t is None or t.is_expired():
                if t is not None and t.is_expired():
                    self._tokens.pop(token, None)
                    self._save()
                return None
            return t

    def snapshot(self) -> list[CapabilityToken]:
        with self._lock:
            return list(self._tokens.values())

    def prune_expired(self) -> int:
        with self._lock:
            now = time.time()
            expired = [k for k, v in self._tokens.items() if v.is_expired(now)]
            for k in expired:
                del self._tokens[k]
            if expired:
                self._save()
            return len(expired)
