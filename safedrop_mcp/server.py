"""SafeDrop MCP server.

Exposes SafeDrop to an MCP-aware AI agent (Claude Code, Claude Desktop,
Cursor, …). Two layers of tools:

**Static local tools** — always present, deal with our own peer:

    list_devices         — list peers on the LAN
    send_file            — push a file (receiver still must Accept)
    send_text            — push text / URL / code
    wait_for_drop        — block until something is dropped to us
    list_remote_tools    — explicit access to a peer's tool registry
    call_remote_tool     — explicit cross-device invocation
    audit_log            — local cross-device-call audit

**Dynamic remote tools** — appear as ``<peer_slug>__<tool_name>``,
generated fresh on every ``tools/list`` request by polling every peer
that advertises ``safedrop.tools``. The handler routes them via
SafeDrop's encrypted TCP channel to the peer that owns them.

So in Claude Code, after running both a Mac and a Pi on the same LAN,
the agent's tool list contains things like::

    pi_a3f2b1__system_info
    pi_a3f2b1__run_shell
    macbook_9d1c44__read_clipboard

— the agent invokes them directly without needing to remember peer names.

The server runs as a *headless* SafeDrop peer (its own X25519 identity,
UDP discovery, TCP listener on a dynamic port) so it coexists with the
GUI on the same machine.

Entry point: ``safedrop-mcp`` (installed by pyproject.toml) or
``python -m safedrop_mcp``.
"""

from __future__ import annotations

import asyncio
import json
import queue as _queue
import re
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
import mcp.types as types

from safedrop.discovery import Peer
from safedrop.headless import (
    HeadlessSafeDrop,
    peer_summary as _peer_summary,
    state_summary as _state_summary,
    wait_terminal as _wait_terminal,
)
from safedrop.transfer import TransferState


_SERVER_VERSION = "0.2.0"


# ----------------------------------------------------- peer-slug helpers ----


_SLUG_PUNCT = re.compile(r"[^a-z0-9]+")


def _peer_slug(peer: Peer) -> str:
    """Stable, MCP-safe handle for a peer.

    Format: ``<first-word-of-name>_<first-6-chars-of-device-id>``.
    Lowercased, alphanumeric + underscores only. Includes the id prefix
    so two peers with identical names don't collide.
    """
    first = peer.name.split()[0] if peer.name else "peer"
    base = _SLUG_PUNCT.sub("_", first.lower()).strip("_")
    return f"{base[:16]}_{peer.device_id[:6]}" if base else f"peer_{peer.device_id[:8]}"


def _find_peer_by_slug(target: str) -> Peer | None:
    if service is None or service.discovery is None:
        return None
    for peer in service.discovery.snapshot().values():
        if _peer_slug(peer) == target:
            return peer
    return None


# ----------------------------------------------------------- tool cache ----


_TOOLS_TTL = 20.0
_tools_cache: dict[str, tuple[list[dict], float]] = {}


async def _fetch_peer_tools(peer: Peer) -> list[dict]:
    """Return ``peer``'s tool list, with a small in-process cache."""
    cached = _tools_cache.get(peer.device_id)
    if cached and cached[1] > time.time():
        return cached[0]
    assert service is not None
    try:
        tools = await asyncio.to_thread(
            service.transfer.list_remote_tools, peer, 3.0
        )
    except Exception:
        # Don't poison the cache on transient errors; just return empty.
        return []
    _tools_cache[peer.device_id] = (tools, time.time() + _TOOLS_TTL)
    return tools


# ----------------------------------------------------------- static tools ----


def _static_tool_defs() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_devices",
            description=(
                "List SafeDrop peers visible on the local network. Returns a JSON array of "
                "{id, name, platform, ip, tcp_port, slug}. The slug is a stable handle you "
                "can use to call peer-specific tools (they appear as <slug>__<tool>)."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="send_file",
            description=(
                "Send a file from this machine to another SafeDrop device. The receiver "
                "must explicitly Accept on their device before bytes flow."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device": {"type": "string", "description": "Peer name (substring ok), id, or slug."},
                    "path": {"type": "string", "description": "Absolute path to the local file."},
                    "timeout_seconds": {"type": "integer", "default": 300},
                },
                "required": ["device", "path"],
            },
        ),
        types.Tool(
            name="send_text",
            description=(
                "Send text / URL / code snippet to another SafeDrop device. Receiver "
                "must Accept on their device."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "content": {"type": "string"},
                    "content_type": {
                        "type": "string", "enum": ["text", "url", "code"], "default": "text",
                    },
                    "timeout_seconds": {"type": "integer", "default": 60},
                },
                "required": ["device", "content"],
            },
        ),
        types.Tool(
            name="wait_for_drop",
            description=(
                "Block until another device drops something to this agent. Useful for "
                "'take a photo with your phone and send it to me, I'll wait' workflows."
            ),
            inputSchema={
                "type": "object",
                "properties": {"timeout_seconds": {"type": "integer", "default": 300}},
            },
        ),
        types.Tool(
            name="list_remote_tools",
            description=(
                "Explicit access to a peer's tool registry. Use this if you know which "
                "device you want to query rather than scanning the flattened tools list."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": 10},
                },
                "required": ["device"],
            },
        ),
        types.Tool(
            name="call_remote_tool",
            description=(
                "Explicit cross-device tool invocation. Equivalent to calling the "
                "flattened <peer>__<tool> form. Useful when the peer slug is unstable "
                "(e.g. just after discovery)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device": {"type": "string"},
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                    "timeout_seconds": {"type": "integer", "default": 60},
                },
                "required": ["device", "name"],
            },
        ),
        types.Tool(
            name="audit_log",
            description=(
                "Local audit log of cross-device tool calls (most-recent first). "
                "Shows both inbound (others called us) and outbound (we called others)."
            ),
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 50}},
            },
        ),
    ]


# ------------------------------------------------------------- handlers ----


server: Server = Server("safedrop")
service: Optional[HeadlessSafeDrop] = None


def _text(payload: Any) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    assert service is not None
    tools = _static_tool_defs()
    peers = service.discovery.snapshot() if service.discovery else {}
    capable = [p for p in peers.values() if p.has_capability("safedrop.tools")]
    if capable:
        # Fan out in parallel so list_tools stays snappy even with several peers.
        results = await asyncio.gather(
            *(_fetch_peer_tools(p) for p in capable),
            return_exceptions=True,
        )
        for peer, peer_tools in zip(capable, results):
            if isinstance(peer_tools, Exception):
                continue
            slug = _peer_slug(peer)
            for t in peer_tools:
                name = f"{slug}__{t.get('name', '')}"
                tools.append(types.Tool(
                    name=name,
                    description=f"[{peer.name}] {t.get('description', '')}".strip(),
                    inputSchema=t.get("inputSchema") or {"type": "object", "properties": {}},
                ))
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> Sequence[types.ContentBlock]:
    assert service is not None
    args = arguments or {}

    # ---- routed: <peer_slug>__<tool> ----
    if "__" in name:
        slug, _, tool_name = name.partition("__")
        peer = _find_peer_by_slug(slug)
        if peer is None:
            return _text({"error": f"unknown peer slug {slug!r}. Try list_devices."})
        try:
            outcome = await asyncio.to_thread(
                service.transfer.call_remote_tool, peer, tool_name, args, 60.0
            )
        except Exception as exc:
            return _text({"error": f"{type(exc).__name__}: {exc}"})
        return _text({"peer": peer.name, "tool": tool_name, **outcome})

    # ---- static local tools ----
    if name == "list_devices":
        peers = service.discovery.snapshot() if service.discovery else {}
        rows = []
        for p in peers.values():
            row = _peer_summary(p)
            row["slug"] = _peer_slug(p)
            row["capabilities"] = list(p.capabilities)
            rows.append(row)
        return _text(rows)

    if name == "send_file":
        device = str(args.get("device", ""))
        path = Path(str(args.get("path", ""))).expanduser()
        timeout = float(args.get("timeout_seconds", 300))
        if not path.is_file():
            return _text({"error": f"Not a file: {path}"})
        try:
            peer = service.find_peer(device)
        except LookupError as exc:
            return _text({"error": str(exc)})
        state = service.transfer.send_file(peer, path)
        _wait_terminal(state, timeout=timeout)
        return _text(_state_summary(state))

    if name == "send_text":
        device = str(args.get("device", ""))
        content = str(args.get("content", ""))
        content_type = str(args.get("content_type", "text"))
        timeout = float(args.get("timeout_seconds", 60))
        if content_type not in ("text", "url", "code"):
            content_type = "text"
        try:
            peer = service.find_peer(device)
        except LookupError as exc:
            return _text({"error": str(exc)})
        state = service.transfer.send_clipboard(peer, content, content_type)
        _wait_terminal(state, timeout=timeout)
        return _text(_state_summary(state))

    if name == "wait_for_drop":
        timeout = float(args.get("timeout_seconds", 300))
        try:
            state: TransferState = await asyncio.to_thread(
                service._drop_queue.get, True, timeout
            )
        except _queue.Empty:
            return _text({"error": "timeout: no drop received"})
        summary = _state_summary(state)
        if state.kind == "clipboard":
            summary["clipboard_content"] = state.clipboard_content
            summary["clipboard_content_type"] = state.clipboard_content_type
        return _text(summary)

    if name == "list_remote_tools":
        device = str(args.get("device", ""))
        timeout = float(args.get("timeout_seconds", 10))
        try:
            peer = service.find_peer(device)
        except LookupError as exc:
            return _text({"error": str(exc)})
        try:
            tools = await asyncio.to_thread(service.transfer.list_remote_tools, peer, timeout)
        except Exception as exc:
            return _text({"error": f"{type(exc).__name__}: {exc}"})
        return _text({"peer": peer.name, "slug": _peer_slug(peer), "tools": tools})

    if name == "call_remote_tool":
        device = str(args.get("device", ""))
        tool_name = str(args.get("name", ""))
        tool_args = args.get("arguments") or {}
        timeout = float(args.get("timeout_seconds", 60))
        try:
            peer = service.find_peer(device)
        except LookupError as exc:
            return _text({"error": str(exc)})
        try:
            outcome = await asyncio.to_thread(
                service.transfer.call_remote_tool, peer, tool_name, tool_args, timeout
            )
        except Exception as exc:
            return _text({"error": f"{type(exc).__name__}: {exc}"})
        return _text({"peer": peer.name, "tool": tool_name, **outcome})

    if name == "audit_log":
        limit = int(args.get("limit", 50))
        rows = service.transfer.audit_log[-limit:][::-1]
        return _text([
            {
                "timestamp": r.timestamp,
                "direction": r.direction,
                "peer_name": r.peer_name,
                "peer_ip": r.peer_ip,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "decision": r.decision,
                "result_summary": r.result_summary,
                "error": r.error,
            }
            for r in rows
        ])

    return _text({"error": f"unknown tool {name!r}"})


# ----------------------------------------------------------------- main ----


async def _main_async() -> None:
    global service
    service = HeadlessSafeDrop()
    service.start()
    try:
        async with stdio_server() as (read, write):
            await server.run(
                read,
                write,
                InitializationOptions(
                    server_name="safedrop",
                    server_version=_SERVER_VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        service.stop()


def run() -> None:
    """Entry point used by the ``safedrop-mcp`` console script."""
    asyncio.run(_main_async())


if __name__ == "__main__":
    run()
