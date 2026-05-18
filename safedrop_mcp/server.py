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

import argparse
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

from .policy import Policy, resolve as resolve_policy


_SERVER_VERSION = "0.3.0"


# Default policy (resolved at startup). Module-level so list_tools /
# call_tool handlers can consult it without an extra wiring layer.
_policy: Policy = Policy()


def _active_policy() -> Policy:
    """Per-call effective policy.

    The HTTP transport's auth middleware installs the token-bound policy
    in a context-var so each HTTP request gets its own scope; stdio
    falls back to the global ``_policy`` set at process startup.
    """
    try:
        from .http_server import current_request_policy
        per_req = current_request_policy()
        if per_req is not None:
            return per_req
    except Exception:
        pass
    return _policy


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


# ----------------------------------------------------- dynamic + bridge ----
#
# These two registries are populated by `register_local_tool` (Phase X.3)
# and the MCP-bridge subsystem (Phase X.4) respectively. They are kept as
# module-level globals so the list_tools / call_tool handlers can consult
# them without ceremony.

_dynamic_tools: dict[str, dict] = {}   # name → {description, inputSchema, handler_url, secret}


def _dynamic_tool_defs() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=spec.get("description") or "",
            inputSchema=spec.get("inputSchema") or {"type": "object", "properties": {}},
        )
        for name, spec in _dynamic_tools.items()
    ]


def _dynamic_has(name: str) -> bool:
    return name in _dynamic_tools


async def _dynamic_call(name: str, args: dict) -> dict:
    import httpx
    spec = _dynamic_tools.get(name)
    if spec is None:
        return {"error": f"unknown dynamic tool: {name!r}"}
    url = spec["handler_url"]
    headers: dict[str, str] = {}
    if spec.get("secret"):
        headers["Authorization"] = f"Bearer {spec['secret']}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json={"name": name, "arguments": args}, headers=headers)
            resp.raise_for_status()
            return resp.json() if resp.headers.get("content-type", "").startswith("application/json") \
                else {"result": resp.text}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# Filled in by safedrop_mcp.bridge when bridges are configured.
_bridge_callable: Any = None


def set_bridge(callable_: Any) -> None:
    """Called by safedrop_mcp.bridge.BridgeManager to install hooks."""
    global _bridge_callable
    _bridge_callable = callable_


def _bridge_tool_defs() -> list[types.Tool]:
    if _bridge_callable is None:
        return []
    out: list[types.Tool] = []
    for spec in _bridge_callable.list_tool_specs():
        out.append(types.Tool(
            name=spec["name"],
            description=spec.get("description") or "",
            inputSchema=spec.get("inputSchema") or {"type": "object", "properties": {}},
        ))
    return out


async def _bridge_call(name: str, args: dict) -> dict:
    if _bridge_callable is None:
        return {"error": "no bridges configured"}
    return await _bridge_callable.call(name, args)


# --------------------------------------------------------- static tools ----


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
        types.Tool(
            name="register_local_tool",
            description=(
                "Register a new local tool that other SafeDrop peers can call. "
                "Provide an HTTP handler URL (typically on 127.0.0.1) — SafeDrop "
                "will POST {name, arguments} to it when a CALL_TOOL arrives, "
                "and return the JSON response back to the caller. Optional "
                "`secret` is sent as a bearer token so the handler can verify "
                "the call. Tools persist for the lifetime of this MCP process."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique tool name."},
                    "description": {"type": "string"},
                    "input_schema": {"type": "object",
                                     "description": "JSON Schema describing arguments."},
                    "handler_url": {"type": "string",
                                    "description": "HTTP(S) endpoint that will execute the tool."},
                    "secret": {"type": "string",
                               "description": "Optional bearer token sent to handler_url."},
                },
                "required": ["name", "handler_url"],
            },
        ),
        types.Tool(
            name="unregister_local_tool",
            description="Remove a tool previously added with register_local_tool.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        types.Tool(
            name="list_local_tools",
            description=(
                "Inspect the tools currently registered via register_local_tool "
                "and via configured MCP bridges (see ~/.safedrop/bridges.json)."
            ),
            inputSchema={"type": "object", "properties": {}},
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
    # Bridge tools (Phase X.4) — populated by safedrop_mcp.bridge when active.
    for bridge_tool in _bridge_tool_defs():
        tools.append(bridge_tool)
    # Dynamic tools (Phase X.3 register_local_tool) — owned by this process.
    for dyn in _dynamic_tool_defs():
        tools.append(dyn)
    # Apply per-agent policy. Empty allow list = no restriction.
    pol = _active_policy()
    return [t for t in tools if pol.allow(t.name)]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> Sequence[types.ContentBlock]:
    assert service is not None
    args = arguments or {}

    # ---- policy ----
    pol = _active_policy()
    if not pol.allow(name):
        return _text({"error": f"tool {name!r} blocked by policy (profile={pol.profile_name})"})

    # ---- dynamic register_local_tool dispatch (Phase X.3) ----
    if _dynamic_has(name):
        result = await _dynamic_call(name, args)
        return _text(result)

    # ---- bridge dispatch (Phase X.4) ----
    if name.startswith("bridge."):
        result = await _bridge_call(name, args)
        return _text(result)

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

    if name == "register_local_tool":
        tool_name = str(args.get("name") or "").strip()
        url = str(args.get("handler_url") or "").strip()
        if not tool_name or not url:
            return _text({"error": "name and handler_url are required"})
        if "__" in tool_name or tool_name.startswith("bridge.") or " " in tool_name:
            return _text({"error": "name must not contain '__', whitespace, or start with 'bridge.'"})
        _dynamic_tools[tool_name] = {
            "description": str(args.get("description") or "").strip(),
            "inputSchema": args.get("input_schema") or {"type": "object", "properties": {}},
            "handler_url": url,
            "secret": args.get("secret"),
        }
        return _text({"status": "registered", "name": tool_name})

    if name == "unregister_local_tool":
        tool_name = str(args.get("name") or "").strip()
        existed = _dynamic_tools.pop(tool_name, None) is not None
        return _text({"status": "removed" if existed else "not_found", "name": tool_name})

    if name == "list_local_tools":
        dyn = [{"name": k, **{kk: vv for kk, vv in v.items() if kk != "secret"}}
               for k, v in _dynamic_tools.items()]
        bridges = []
        if _bridge_callable is not None:
            bridges = _bridge_callable.list_tool_specs()
        return _text({"dynamic": dyn, "bridges": bridges})

    return _text({"error": f"unknown tool {name!r}"})


# ----------------------------------------------------------------- main ----


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safedrop-mcp",
                                description="SafeDrop Model Context Protocol server")
    p.add_argument("--allow", help="Comma-separated allowlist of tool-name globs "
                                     "(also reads SAFEDROP_MCP_ALLOWED_TOOLS).")
    p.add_argument("--deny", help="Comma-separated denylist of globs "
                                    "(also reads SAFEDROP_MCP_DENIED_TOOLS).")
    p.add_argument("--profile", help="Name of profile in ~/.safedrop/mcp-profiles/ "
                                      "(also reads SAFEDROP_MCP_PROFILE).")
    p.add_argument("--name-suffix", help="Override the peer-name suffix (default: 'MCP').")
    p.add_argument("--bridges", help="Path to a JSON file listing other MCP servers "
                                       "to bridge (default ~/.safedrop/bridges.json).")
    p.add_argument("--no-bridges", action="store_true",
                   help="Disable MCP bridging even if a bridges.json exists.")
    p.add_argument("--http", metavar="HOST:PORT",
                   help="Run an HTTP/Streamable-MCP server on this address instead "
                        "of stdio. Token auth required (see safedrop-mcp-tokens).")
    return p


async def _main_async(args: argparse.Namespace) -> None:
    global service, _policy
    _policy = resolve_policy(
        allow_arg=args.allow,
        deny_arg=args.deny,
        profile_arg=args.profile,
    )
    name_suffix = args.name_suffix or _policy.name_suffix or "MCP"
    service = HeadlessSafeDrop(name_suffix=name_suffix)
    service.start()
    # Configure MCP bridges (Phase X.4) if requested.
    bridge_mgr = None
    if not args.no_bridges:
        from .bridge import BridgeManager
        bridge_mgr = BridgeManager.from_config(args.bridges)
        if bridge_mgr is not None:
            set_bridge(bridge_mgr)
            await bridge_mgr.start()
    try:
        if args.http:
            from .http_server import serve_http
            host, _, port = args.http.partition(":")
            await serve_http(server, host=host or "127.0.0.1", port=int(port or 47899))
        else:
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
        if bridge_mgr is not None:
            await bridge_mgr.stop()
        service.stop()


def run(argv: list[str] | None = None) -> None:
    """Entry point used by the ``safedrop-mcp`` console script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    run()
