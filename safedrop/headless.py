"""Headless SafeDrop peer + small formatting helpers.

Shared between the CLI (`safedrop`) and the MCP server (`safedrop-mcp`).
This module deliberately does *not* import ``mcp`` so the CLI works even
when the MCP extra hasn't been installed.
"""

from __future__ import annotations

import platform
import queue as _queue
import socket
import time
from typing import Any

from .config import new_device_id
from .crypto import Identity
from .discovery import DiscoveryService, Peer
from .tools import ToolRegistry, build_default_registry
from .transfer import TransferManager, TransferState


DEFAULT_CAPABILITIES: tuple[str, ...] = ("safedrop.transfer", "safedrop.tools")


class HeadlessSafeDrop:
    """One Identity + DiscoveryService + TransferManager + ToolRegistry. No GUI."""

    def __init__(
        self,
        name_suffix: str = "headless",
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.identity = Identity.generate()
        self.device_id = new_device_id()
        hostname = socket.gethostname()
        self.device_name = f"{hostname} ({platform.system()}, {name_suffix})"

        self.tool_registry = tool_registry if tool_registry is not None else build_default_registry()

        # tcp_port=0 → OS picks a free port; we read it back after start().
        self.transfer = TransferManager(
            identity=self.identity,
            device_id=self.device_id,
            device_name=self.device_name,
            tcp_port=0,
            tool_registry=self.tool_registry,
        )

        # No UI to click "Accept" with — inbound transfers are auto-accepted
        # on *our* side. The push side still raises a dialog on its device,
        # so the trust model is preserved at one end.
        self.transfer.on_request = lambda req: req.accept()

        # wait_for_drop pulls from this thread-safe queue.
        self._drop_queue: _queue.Queue = _queue.Queue()
        self._seen_done: set[str] = set()
        self.transfer.on_state = self._on_state

        self.discovery: DiscoveryService | None = None

    def start(self) -> None:
        self.transfer.start()
        self.discovery = DiscoveryService(
            device_id=self.device_id,
            device_name=self.device_name,
            platform_name=platform.system(),
            tcp_port=self.transfer.tcp_port,
            pubkey_b64=self.identity.public_key_b64(),
            capabilities=DEFAULT_CAPABILITIES,
        )
        self.discovery.start()

    def stop(self) -> None:
        try:
            if self.discovery is not None:
                self.discovery.stop()
        except Exception:
            pass
        try:
            self.transfer.stop()
        except Exception:
            pass

    def find_peer(self, query: str) -> Peer:
        assert self.discovery is not None
        peers = self.discovery.snapshot()
        if query in peers:
            return peers[query]
        q = query.lower()
        matches = [
            p for p in peers.values()
            if q in p.name.lower() or q in p.device_id.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(f"{m.name!r}" for m in matches)
            raise LookupError(f"Ambiguous device {query!r} — matches: {names}")
        available = ", ".join(f"{p.name!r}" for p in peers.values()) or "(none)"
        raise LookupError(f"No device matching {query!r}. Available: {available}")

    def _on_state(self, state: TransferState) -> None:
        if (
            state.direction == "recv"
            and state.status == "done"
            and state.transfer_id not in self._seen_done
        ):
            self._seen_done.add(state.transfer_id)
            self._drop_queue.put(state)


def wait_terminal(state: TransferState, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if state.status in ("done", "failed", "rejected"):
            return
        time.sleep(0.05)


def peer_summary(p: Peer) -> dict[str, Any]:
    return {
        "id": p.device_id,
        "name": p.name,
        "platform": p.platform,
        "ip": p.ip,
        "tcp_port": p.tcp_port,
    }


def state_summary(state: TransferState) -> dict[str, Any]:
    return {
        "transfer_id": state.transfer_id,
        "kind": state.kind,
        "direction": state.direction,
        "name": state.name,
        "size": state.size,
        "bytes_done": state.bytes_done,
        "pair_code": state.pair_code,
        "status": state.status,
        "peer_name": state.peer_name,
        "save_path": str(state.save_path) if state.save_path else None,
        "error": state.error,
    }
