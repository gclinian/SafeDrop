"""SafeDrop MCP server.

Exposes four tools to an MCP-aware AI agent (Claude Code, Claude Desktop,
Cursor, etc.) so the agent can participate as a SafeDrop peer on the local
network:

    list_devices    — what trusted devices are visible on the LAN right now
    send_file       — push a file to a device (receiver still must Accept)
    send_text       — push text / URL / code (receiver still must Accept)
    wait_for_drop   — block until something is dropped to us, then return it

The server is *headless*: it bootstraps its own X25519 identity, UDP
discovery, and TCP listener on a dynamic port. It does NOT share state
with a running SafeDrop GUI on the same machine — both can coexist and
will appear as two separate peers on the network (e.g.
``MyMac (Darwin)`` and ``MyMac (Darwin, MCP)``).

Run via the installed entry point::

    safedrop-mcp

or equivalently::

    python -m safedrop_mcp
"""

from __future__ import annotations

import asyncio
import json
import queue as _queue
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from safedrop.headless import (
    HeadlessSafeDrop,
    peer_summary as _peer_summary,
    state_summary as _state_summary,
    wait_terminal as _wait_terminal,
)
from safedrop.transfer import TransferState


# ----------------------------------------------------------------- server ----


mcp = FastMCP("safedrop")
service: Optional[HeadlessSafeDrop] = None


@mcp.tool()
def list_devices() -> str:
    """List SafeDrop peers currently visible on the local network.

    Returns a JSON array of {id, name, platform, ip, tcp_port}.
    Each entry is a peer this agent can send_file / send_text to.
    The list may be empty for a few seconds after startup while UDP
    discovery converges.
    """
    assert service is not None
    peers = service.discovery.snapshot() if service.discovery else {}
    return json.dumps([_peer_summary(p) for p in peers.values()], ensure_ascii=False, indent=2)


@mcp.tool()
def send_file(device: str, path: str, timeout_seconds: int = 300) -> str:
    """Send a file from this machine to another SafeDrop device on the LAN.

    The receiver MUST explicitly accept the transfer on their device before
    bytes flow — this is intentional. The trust model does not change just
    because the sender is an AI agent.

    Args:
        device: Device name (case-insensitive substring match) or device id
            from list_devices.
        path: Absolute path to the local file to send.
        timeout_seconds: Max wait for the receiver to accept and the
            transfer to finish.

    Returns a JSON object with: status (done|rejected|failed|transferring),
    bytes_done, size, pair_code (for visual verification), and any error.
    """
    assert service is not None
    p = Path(path).expanduser()
    if not p.is_file():
        return json.dumps({"error": f"Not a file: {p}"})
    try:
        peer = service.find_peer(device)
    except LookupError as exc:
        return json.dumps({"error": str(exc)})
    state = service.transfer.send_file(peer, p)
    _wait_terminal(state, timeout=float(timeout_seconds))
    return json.dumps(_state_summary(state), ensure_ascii=False, indent=2)


@mcp.tool()
def send_text(device: str, content: str, content_type: str = "text", timeout_seconds: int = 60) -> str:
    """Send a text snippet, URL, or code snippet to another SafeDrop device.

    The receiver still must accept on their device.

    Args:
        device: Device name or id (see list_devices).
        content: The text to send (UTF-8). Newlines and unicode are fine.
        content_type: One of "text", "url", "code". Controls the receiver's
            preview rendering and whether they get an "Open URL" action.
        timeout_seconds: Max wait for accept and send (default 60s).

    Returns a JSON object with: status, pair_code, and any error.
    """
    assert service is not None
    if content_type not in ("text", "url", "code"):
        content_type = "text"
    try:
        peer = service.find_peer(device)
    except LookupError as exc:
        return json.dumps({"error": str(exc)})
    state = service.transfer.send_clipboard(peer, content, content_type)
    _wait_terminal(state, timeout=float(timeout_seconds))
    return json.dumps(_state_summary(state), ensure_ascii=False, indent=2)


@mcp.tool()
async def wait_for_drop(timeout_seconds: int = 300) -> str:
    """Block until another device drops something to this agent, then return it.

    Useful for human-in-the-loop workflows where the agent needs the user
    to push something from another device — e.g. "take a photo of the
    receipt with your phone and drop it to me, I'll wait."

    Args:
        timeout_seconds: Max wait. Default 5 minutes.

    Returns a JSON object describing the received item:
      - For files:     {kind: "file", name, size, peer_name, save_path}
      - For clipboard: {kind: "clipboard", peer_name, clipboard_content,
                        clipboard_content_type}
    """
    assert service is not None
    try:
        state: TransferState = await asyncio.to_thread(
            service._drop_queue.get, True, float(timeout_seconds)
        )
    except _queue.Empty:
        return json.dumps({"error": "timeout: no drop received"})

    summary = _state_summary(state)
    if state.kind == "clipboard":
        summary["clipboard_content"] = state.clipboard_content
        summary["clipboard_content_type"] = state.clipboard_content_type
    return json.dumps(summary, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ entry ----


def run() -> None:
    """Entry point used by the ``safedrop-mcp`` console script."""
    global service
    service = HeadlessSafeDrop()
    service.start()
    try:
        mcp.run()  # FastMCP defaults to stdio transport.
    finally:
        service.stop()


if __name__ == "__main__":
    run()
