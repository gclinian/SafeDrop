"""Multi-agent mesh: cross-device messaging over SafeDrop.

This is the "two agents on different machines talk to each other" layer.

Each ``safedrop-mcp`` process:

* picks (or generates) a stable :class:`AgentIdentity` (see
  :mod:`safedrop_mcp.agent_identity`);
* registers two SafeDrop *peer tools* on its HeadlessSafeDrop:

  - ``agent_bus_whoami`` — returns ``{agent_id, label}`` for this device;
  - ``agent_bus_recv``    — accepts ``{from_agent_id, from_label, content}``
                            and appends it to the local mailbox;

* exposes three MCP tools to its local agent:

  - ``list_agents``       — discover other agents on the LAN by polling
                            every peer's ``agent_bus_whoami``;
  - ``send_message``      — call the target peer's ``agent_bus_recv``;
  - ``recv_messages``     — drain this agent's local inbox.

So agent A says::

    send_message(to_agent="agent-abc123", content="hi from claude")

→ MCP resolves "agent-abc123" to the peer hosting it, calls
``agent_bus_recv`` over SafeDrop's encrypted TCP channel, and the
receiving agent B sees the message next time it calls ``recv_messages``.

**Mailbox format** — ``~/.safedrop/agent_bus/inbox.jsonl`` is JSON Lines,
append-only, one record per line::

    {"ts": ..., "message_id": "...", "from_agent_id": "...",
     "from_label": "...", "content": "..."}
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from safedrop.tools import ToolRegistry, ToolSpec

from .agent_identity import AgentIdentity


def default_inbox_path() -> Path:
    return Path.home() / ".safedrop" / "agent_bus" / "inbox.jsonl"


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- mailbox ---


@dataclass
class Mailbox:
    """Append-only JSON-Lines store for inbound messages."""

    path: Path

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def read(self, since_ts: float = 0.0, limit: int = 100) -> list[dict]:
        """Return messages with ``ts > since_ts``, capped to the most-recent ``limit``."""
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if float(entry.get("ts", 0)) > since_ts:
                    out.append(entry)
        if limit > 0 and len(out) > limit:
            out = out[-limit:]
        return out


# --------------------------------------------------------------------------- agent bus ---


class AgentBus:
    """One per safedrop-mcp process. Owns local identity + mailbox + peer-tool handlers."""

    def __init__(
        self,
        identity: AgentIdentity,
        mailbox: Optional[Mailbox] = None,
    ) -> None:
        self.identity = identity
        self.mailbox = mailbox or Mailbox(default_inbox_path())

    # ---- registration ---------------------------------------------------

    def register_peer_tools(self, registry: ToolRegistry) -> None:
        """Register agent_bus_whoami + agent_bus_recv on the local SafeDrop ToolRegistry.

        These are *peer* tools — visible to every other SafeDrop device on
        the LAN, callable via the existing encrypted CALL_TOOL channel.
        """

        registry.register(ToolSpec(
            name="agent_bus_whoami",
            description=(
                "Return the AI agent identity running on this SafeDrop device. "
                "Returns {agent_id, label}. agent_id is stable across MCP restarts."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: self._handle_whoami(args),
        ))

        registry.register(ToolSpec(
            name="agent_bus_recv",
            description=(
                "Deliver an inbound message to the agent on this device. "
                "Used by send_message in the agent_bus on a remote peer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_agent_id": {"type": "string"},
                    "from_label":    {"type": "string"},
                    "content":       {"type": "string"},
                },
                "required": ["from_agent_id", "content"],
            },
            handler=lambda args: self._handle_recv(args),
        ))

    # ---- peer-tool handlers --------------------------------------------

    def _handle_whoami(self, _args: dict) -> dict[str, Any]:
        return {"agent_id": self.identity.agent_id, "label": self.identity.label}

    def _handle_recv(self, args: dict) -> dict[str, Any]:
        from_agent = str(args.get("from_agent_id") or "").strip()
        content = str(args.get("content") or "")
        if not from_agent:
            return {"status": "error", "error": "from_agent_id is required"}
        if not content:
            return {"status": "error", "error": "content is required"}
        entry = {
            "ts": _now(),
            "message_id": uuid.uuid4().hex,
            "from_agent_id": from_agent,
            "from_label": str(args.get("from_label") or ""),
            "content": content,
        }
        self.mailbox.append(entry)
        return {"status": "delivered", "message_id": entry["message_id"]}

    # ---- public API used by MCP server handlers ------------------------

    def whoami(self) -> dict[str, str]:
        return {"agent_id": self.identity.agent_id, "label": self.identity.label}

    def read_inbox(self, since_ts: float = 0.0, limit: int = 100) -> list[dict]:
        return self.mailbox.read(since_ts=since_ts, limit=limit)
