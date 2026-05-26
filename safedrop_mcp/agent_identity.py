"""Persistent agent identity for the SafeDrop MCP fabric.

Each ``safedrop-mcp`` process has a stable agent_id that survives MCP
restarts. The id is the *primary key* used by the agent-bus mailbox
(see :mod:`safedrop_mcp.agent_bus`) — two agents on different machines
recognise each other by this id even across reboots and re-pairings.

On-disk location: ``~/.safedrop/agent_id.json`` (0o600 on POSIX).
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def default_path() -> Path:
    return Path.home() / ".safedrop" / "agent_id.json"


@dataclass
class AgentIdentity:
    agent_id: str
    label: str

    def to_dict(self) -> dict:
        return {"agent_id": self.agent_id, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict) -> "AgentIdentity":
        return cls(
            agent_id=str(d.get("agent_id") or "").strip(),
            label=str(d.get("label") or "").strip(),
        )


def _new_agent_id() -> str:
    """Format: ``agent-<12 hex chars>``. Short enough to read, long enough to be unique."""
    return "agent-" + uuid.uuid4().hex[:12]


def load_or_create(
    path: Optional[Path] = None,
    label: Optional[str] = None,
) -> AgentIdentity:
    """Return the persisted identity, creating one if the file is missing/empty/corrupt.

    ``label`` is only consulted on first creation (or when an existing
    identity has no label and the caller passes one). It is NOT a
    rename — the agent_id is stable across runs.
    """
    p = path or default_path()
    if p.exists():
        try:
            ident = AgentIdentity.from_dict(json.loads(p.read_text("utf-8")))
            if ident.agent_id:
                if label and not ident.label:
                    ident.label = label
                    save(ident, p)
                return ident
        except Exception:
            # Fall through to recreate.
            pass
    ident = AgentIdentity(
        agent_id=_new_agent_id(),
        label=(label or socket.gethostname()).strip() or "anonymous-agent",
    )
    save(ident, p)
    return ident


def save(ident: AgentIdentity, path: Optional[Path] = None) -> None:
    p = path or default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(ident.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        # Windows or non-POSIX filesystems — best-effort, don't fail.
        pass
