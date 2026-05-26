"""Tailscale integration — cross-LAN peer discovery (v1.7, opt-in).

SafeDrop's default UDP-broadcast discovery only works on a single LAN.
For "the laptop is at the coffee shop, the desktop is at home, they
should still see each other" cases, you can install Tailscale (or any
WireGuard-based overlay that gives each machine a stable address +
SSO auth + ACLs), then run::

    safedrop tailscale add-all     # one-shot: read `tailscale status`,
                                   # add every peer as a SafeDrop manual peer

This module is a thin parser over ``tailscale status --json`` plus a
helper that turns each tailnet peer into a SafeDrop manual-peer entry
(IP, default TCP port 47891 — SafeDrop's standard port — and a
placeholder pubkey that the user must update from the other end the
first time they pair).

Caveats and trade-offs
~~~~~~~~~~~~~~~~~~~~~~

* This is **opt-in**. The default SafeDrop stays LAN-only — Tailscale
  is an OS-level install with its own login flow. We don't ship a
  Tailscale binary or auto-install anything.
* We don't auto-discover pubkeys — they have to flow over the
  encrypted SafeDrop handshake the first time. The manual-peer entry
  is "device exists at this IP:port" stub that the user upgrades to a
  full pubkey-confirmed peer on first connection.
* No data goes through Tailscale-the-company. WireGuard is point-to-
  point; SafeDrop's Fernet encryption is layered on top. Tailscale
  is just for routing + name resolution.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class TailscalePeer:
    """One row from ``tailscale status --json``."""
    hostname: str
    dns_name: str
    tailscale_ip: str        # 100.x.x.x — the tailnet address
    os: str
    online: bool
    is_self: bool

    def to_safedrop_peer_stub(self, port: int = 47891) -> dict[str, Any]:
        """Build a manual-peer entry compatible with the GUI / mobile clients.

        Note: ``pubkey`` is an empty placeholder. The pairing UI must
        prompt the user to enter (or auto-discover) the real pubkey when
        the SafeDrop daemon on the other side first answers.
        """
        return {
            "device_id": f"tailscale:{self.tailscale_ip}",
            "name":      f"{self.hostname} (tailscale)",
            "platform":  self.os or "tailnet",
            "ip":        self.tailscale_ip,
            "tcp_port":  port,
            "pubkey":    "",  # set on first successful handshake
            "online":    self.online,
        }


def is_installed() -> bool:
    """``True`` iff a ``tailscale`` binary is on PATH."""
    return shutil.which("tailscale") is not None


def fetch_status_json(
    *,
    timeout: float = 5.0,
    tailscale_bin: str = "tailscale",
) -> dict[str, Any]:
    """Run ``tailscale status --json`` and return the parsed dict.

    Raises ``RuntimeError`` with a readable message if tailscale isn't
    installed, isn't logged in, or returns non-JSON output.
    """
    if not shutil.which(tailscale_bin):
        raise RuntimeError(
            f"{tailscale_bin!r} not on PATH. Install from https://tailscale.com/download "
            f"or pass --tailscale-bin if you've installed it elsewhere."
        )
    try:
        proc = subprocess.run(
            [tailscale_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tailscale status timed out after {timeout}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not run tailscale: {exc}") from exc

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or "non-zero exit"
        if "needs login" in msg.lower() or "logged out" in msg.lower():
            raise RuntimeError(
                "Tailscale is installed but logged out. Run `tailscale up` first."
            )
        raise RuntimeError(f"tailscale status failed: {msg}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse tailscale status JSON: {exc}") from exc


def parse_peers(status: dict[str, Any]) -> list[TailscalePeer]:
    """Extract one :class:`TailscalePeer` per tailnet machine (including self).

    The ``tailscale status --json`` layout used here:

    * ``Self``: this node;
    * ``Peer``: dict of ``{publicKey: peer_info}`` for everyone else.

    Each ``peer_info`` carries ``HostName``, ``DNSName``,
    ``TailscaleIPs``, ``OS``, ``Online``.
    """

    def _ip(info: dict[str, Any]) -> str:
        ips = info.get("TailscaleIPs") or []
        # IPv4 first; otherwise the first IP we see.
        for ip in ips:
            if isinstance(ip, str) and "." in ip:
                return ip
        return str(ips[0]) if ips else ""

    rows: list[TailscalePeer] = []

    self_info = status.get("Self") or {}
    if self_info:
        ip = _ip(self_info)
        if ip:
            rows.append(TailscalePeer(
                hostname=str(self_info.get("HostName") or ""),
                dns_name=str(self_info.get("DNSName") or "").rstrip("."),
                tailscale_ip=ip,
                os=str(self_info.get("OS") or ""),
                online=bool(self_info.get("Online", True)),
                is_self=True,
            ))

    peers = status.get("Peer") or {}
    if isinstance(peers, dict):
        for info in peers.values():
            if not isinstance(info, dict):
                continue
            ip = _ip(info)
            if not ip:
                continue
            rows.append(TailscalePeer(
                hostname=str(info.get("HostName") or ""),
                dns_name=str(info.get("DNSName") or "").rstrip("."),
                tailscale_ip=ip,
                os=str(info.get("OS") or ""),
                online=bool(info.get("Online", False)),
                is_self=False,
            ))
    return rows


def discover_peers(
    *,
    include_self: bool = False,
    online_only: bool = True,
    timeout: float = 5.0,
    tailscale_bin: str = "tailscale",
) -> list[TailscalePeer]:
    """One-call shortcut: status → parse → filter."""
    status = fetch_status_json(timeout=timeout, tailscale_bin=tailscale_bin)
    rows = parse_peers(status)
    if not include_self:
        rows = [r for r in rows if not r.is_self]
    if online_only:
        rows = [r for r in rows if r.online]
    return rows
