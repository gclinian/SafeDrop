"""Bridge other MCP servers into SafeDrop.

A *bridge* is another MCP server (stdio child process) whose tools we
import into our own ``tools/list`` under the ``bridge.<name>.<tool>``
namespace. When a peer calls one of those tools, we forward the call
into the child via the MCP client SDK and return its result.

This turns SafeDrop into a **cross-device MCP fabric** — any MCP server
that runs locally (filesystem, browser, github, postgres, …) becomes
addressable from every paired SafeDrop peer on the LAN.

Config lives at ``~/.safedrop/bridges.json`` by default. Schema::

    {
      "bridges": [
        {
          "name": "fs",
          "command": "uvx",
          "args": ["mcp-server-filesystem", "/Users/me/Documents"],
          "env": { "EXTRA": "x" }
        },
        {
          "name": "github",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-github"]
        }
      ]
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_BRIDGE_PATH = Path.home() / ".safedrop" / "bridges.json"


logger = logging.getLogger("safedrop_mcp.bridge")


@dataclass
class BridgeSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class _LiveBridge:
    spec: BridgeSpec
    session: ClientSession
    exit_stack: AsyncExitStack
    tools: list[dict]


class BridgeManager:
    """Owns the subprocess MCP clients for every configured bridge."""

    def __init__(self, specs: list[BridgeSpec]) -> None:
        self.specs = specs
        self._live: dict[str, _LiveBridge] = {}
        self._lock = asyncio.Lock()

    # ---- discovery ----------------------------------------------------

    @classmethod
    def from_config(cls, path_arg: str | None) -> "BridgeManager | None":
        path = Path(path_arg) if path_arg else DEFAULT_BRIDGE_PATH
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("bridges") if isinstance(raw, dict) else None
            if not isinstance(entries, list) or not entries:
                return None
            specs = [
                BridgeSpec(
                    name=str(e["name"]),
                    command=str(e["command"]),
                    args=list(e.get("args") or []),
                    env=dict(e.get("env") or {}),
                )
                for e in entries
                if isinstance(e, dict) and e.get("name") and e.get("command")
            ]
            return cls(specs) if specs else None
        except Exception as exc:
            logger.warning("[safedrop-mcp] failed to load bridges from %s: %s", path, exc)
            return None

    # ---- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        for spec in self.specs:
            try:
                await self._start_one(spec)
                logger.info("[safedrop-mcp] bridged %d tool(s) from %r",
                            len(self._live[spec.name].tools), spec.name)
            except Exception as exc:
                logger.warning("[safedrop-mcp] could not bridge %r: %s", spec.name, exc)

    async def _start_one(self, spec: BridgeSpec) -> None:
        stack = AsyncExitStack()
        params = StdioServerParameters(
            command=spec.command,
            args=spec.args,
            env={**os.environ, **spec.env},
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session: ClientSession = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_resp = await session.list_tools()
        manifests: list[dict] = []
        for t in tools_resp.tools:
            manifests.append({
                "name": f"bridge.{spec.name}.{t.name}",
                "description": f"[{spec.name}] {t.description or ''}".strip(),
                "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                "_remote_name": t.name,
            })
        self._live[spec.name] = _LiveBridge(
            spec=spec, session=session, exit_stack=stack, tools=manifests,
        )

    async def stop(self) -> None:
        for live in list(self._live.values()):
            try:
                await live.exit_stack.aclose()
            except Exception:
                pass
        self._live.clear()

    # ---- query / dispatch --------------------------------------------

    def list_tool_specs(self) -> list[dict]:
        out: list[dict] = []
        for live in self._live.values():
            out.extend({k: v for k, v in m.items() if not k.startswith("_")}
                       for m in live.tools)
        return out

    async def call(self, name: str, arguments: dict) -> dict:
        # name looks like 'bridge.<bridge_name>.<remote_tool>'
        if not name.startswith("bridge."):
            return {"error": f"not a bridge tool: {name!r}"}
        rest = name[len("bridge."):]
        bridge_name, _, remote_tool = rest.partition(".")
        live = self._live.get(bridge_name)
        if live is None:
            return {"error": f"unknown bridge {bridge_name!r}"}
        try:
            result = await live.session.call_tool(remote_tool, arguments)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

        # Normalise MCP CallToolResult into a JSON-able dict.
        if getattr(result, "isError", False):
            text = " ".join(getattr(c, "text", "") for c in (result.content or []))
            return {"error": text or "bridged tool returned isError"}
        payload: list[Any] = []
        for c in (result.content or []):
            kind = type(c).__name__
            if kind == "TextContent":
                payload.append(c.text)
            elif kind == "ImageContent":
                payload.append({"mimeType": c.mimeType, "data_b64": c.data})
            else:
                payload.append(str(c))
        if len(payload) == 1:
            return {"result": payload[0]}
        return {"result": payload}
