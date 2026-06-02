"""UDP-broadcast peer discovery.

Each running SafeDrop instance:

* periodically broadcasts a HELLO JSON datagram on UDP port DISCOVERY_PORT
  to 255.255.255.255 so peers in the same LAN can see it;
* listens on the same UDP port for HELLOs / BYEs from peers;
* maintains a peer table with a TTL — peers we haven't heard from in
  PEER_TTL seconds are dropped.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import psutil

from .config import (
    BROADCAST_INTERVAL,
    DISCOVERY_PORT,
    PEER_TTL,
    VERSION,
)


@dataclass
class Peer:
    device_id: str
    name: str
    platform: str
    ip: str
    tcp_port: int
    pubkey: str
    capabilities: tuple[str, ...] = ()
    last_seen: float = field(default_factory=time.time)

    def has_capability(self, cap: str) -> bool:
        return cap in self.capabilities


PeerCallback = Callable[[dict[str, Peer]], None]


@dataclass(frozen=True)
class BroadcastInterface:
    name: str
    ip: str
    broadcast: str


def _is_usable_ipv4(value: str | None) -> bool:
    if not value:
        return False
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return not (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
    )


def _broadcast_for(ip: str, netmask: str | None) -> str | None:
    if not netmask:
        return None
    try:
        return str(ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False).broadcast_address)
    except ValueError:
        return None


def _broadcast_interfaces() -> tuple[BroadcastInterface, ...]:
    """Return active IPv4 interfaces with their directed-broadcast address."""
    stats = psutil.net_if_stats()
    interfaces: list[BroadcastInterface] = []
    seen: set[tuple[str, str]] = set()

    for name, addrs in psutil.net_if_addrs().items():
        stat = stats.get(name)
        if stat is not None and not stat.isup:
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET or not _is_usable_ipv4(addr.address):
                continue
            broadcast = addr.broadcast or _broadcast_for(addr.address, addr.netmask)
            if not broadcast:
                continue
            try:
                ipaddress.IPv4Address(broadcast)
            except ValueError:
                continue
            key = (addr.address, broadcast)
            if key in seen:
                continue
            seen.add(key)
            interfaces.append(BroadcastInterface(name=name, ip=addr.address, broadcast=broadcast))
    return tuple(interfaces)


def _local_ip_display() -> str:
    ips: list[str] = []
    seen: set[str] = set()
    for iface in _broadcast_interfaces():
        if iface.ip in seen:
            continue
        seen.add(iface.ip)
        ips.append(iface.ip)
    return ", ".join(ips) if ips else "127.0.0.1"


def _broadcast_targets(local_ip: str | None = None) -> tuple[str, ...]:
    """Return loopback, directed broadcasts, and global broadcast targets."""
    targets: list[str] = ["127.0.0.1"]
    for iface in _broadcast_interfaces():
        if local_ip and iface.ip != local_ip:
            continue
        targets.append(iface.broadcast)
    targets.append("255.255.255.255")

    seen: set[str] = set()
    out: list[str] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        out.append(target)
    return tuple(out)


def _broadcast_endpoints() -> tuple[tuple[str, str | None], ...]:
    """Return ``(target, source_ip)`` endpoints for broadcast sends."""
    endpoints: list[tuple[str, str | None]] = [("127.0.0.1", None)]
    for iface in _broadcast_interfaces():
        endpoints.append((iface.broadcast, iface.ip))
        endpoints.append(("255.255.255.255", iface.ip))
    if len(endpoints) == 1:
        endpoints.append(("255.255.255.255", None))

    seen: set[tuple[str, str | None]] = set()
    out: list[tuple[str, str | None]] = []
    for endpoint in endpoints:
        if endpoint in seen:
            continue
        seen.add(endpoint)
        out.append(endpoint)
    return tuple(out)


def _send_udp_broadcast(
    target: str,
    source_ip: str | None,
    payload: bytes,
    default_sock: socket.socket,
) -> None:
    if source_ip is None:
        default_sock.sendto(payload, (target, DISCOVERY_PORT))
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((source_ip, 0))
        sock.sendto(payload, (target, DISCOVERY_PORT))
    finally:
        sock.close()


class DiscoveryService:
    """Background UDP broadcaster + listener."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        platform_name: str,
        tcp_port: int,
        pubkey_b64: str,
        capabilities: tuple[str, ...] = ("safedrop.transfer",),
        on_change: PeerCallback | None = None,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.platform_name = platform_name
        self.tcp_port = tcp_port
        self.pubkey_b64 = pubkey_b64
        self.capabilities = tuple(capabilities)
        self.on_change = on_change

        self.local_ip = _local_ip_display()
        self.peers: dict[str, Peer] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self._send_sock: socket.socket | None = None
        self._recv_sock: socket.socket | None = None

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        self._recv_sock.bind(("", DISCOVERY_PORT))
        self._recv_sock.settimeout(1.0)

        for target in (self._broadcast_loop, self._listen_loop, self._reaper_loop):
            t = threading.Thread(target=target, daemon=True, name=f"Discovery-{target.__name__}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._send_bye()
        self._stop.set()
        for sock in (self._send_sock, self._recv_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # ---- broadcast ----------------------------------------------------

    def _hello_payload(self) -> bytes:
        return json.dumps(
            {
                "type": "HELLO",
                "device_id": self.device_id,
                "name": self.device_name,
                "platform": self.platform_name,
                "tcp_port": self.tcp_port,
                "pubkey": self.pubkey_b64,
                "capabilities": list(self.capabilities),
                "version": VERSION,
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def _bye_payload(self) -> bytes:
        return json.dumps(
            {"type": "BYE", "device_id": self.device_id},
            ensure_ascii=False,
        ).encode("utf-8")

    def _broadcast(self, payload: bytes) -> None:
        if self._send_sock is None:
            return
        # Recompute endpoints each tick so we adapt to a network change
        # (e.g. plugging into a different LAN). The function is cheap.
        self.local_ip = _local_ip_display()
        for target, source_ip in _broadcast_endpoints():
            try:
                _send_udp_broadcast(target, source_ip, payload, self._send_sock)
            except OSError:
                # Any single target can fail on a particular network —
                # the others still get a chance. (E.g. 255.255.255.255
                # raises "No route to host" when a VPN owns the default
                # route, but the subnet broadcast still works.)
                continue

    def _broadcast_loop(self) -> None:
        payload = self._hello_payload()
        while not self._stop.is_set():
            self._broadcast(payload)
            self._stop.wait(BROADCAST_INTERVAL)

    def _send_bye(self) -> None:
        try:
            self._broadcast(self._bye_payload())
        except OSError:
            pass

    # ---- listen -------------------------------------------------------

    def _listen_loop(self) -> None:
        assert self._recv_sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._recv_sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_datagram(data, addr[0])

    def _handle_datagram(self, data: bytes, sender_ip: str) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        device_id = msg.get("device_id")
        if not device_id or device_id == self.device_id:
            return

        changed = False
        if kind == "HELLO":
            caps_field = msg.get("capabilities", [])
            if isinstance(caps_field, list):
                caps = tuple(str(c) for c in caps_field)
            else:
                caps = ()
            peer = Peer(
                device_id=device_id,
                name=str(msg.get("name", "unknown")),
                platform=str(msg.get("platform", "?")),
                ip=sender_ip,
                tcp_port=int(msg.get("tcp_port", 0)),
                pubkey=str(msg.get("pubkey", "")),
                capabilities=caps,
            )
            if peer.tcp_port <= 0 or not peer.pubkey:
                return
            with self._lock:
                existing = self.peers.get(device_id)
                if existing is None or (
                    existing.ip != peer.ip
                    or existing.tcp_port != peer.tcp_port
                    or existing.name != peer.name
                    or existing.pubkey != peer.pubkey
                ):
                    changed = True
                self.peers[device_id] = peer
        elif kind == "BYE":
            with self._lock:
                if device_id in self.peers:
                    del self.peers[device_id]
                    changed = True

        if changed:
            self._notify()

    # ---- reaper -------------------------------------------------------

    def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(1.0)
            cutoff = time.time() - PEER_TTL
            removed = False
            with self._lock:
                for pid, peer in list(self.peers.items()):
                    if peer.last_seen < cutoff:
                        del self.peers[pid]
                        removed = True
            if removed:
                self._notify()

    # ---- helpers ------------------------------------------------------

    def snapshot(self) -> dict[str, Peer]:
        with self._lock:
            return dict(self.peers)

    def _notify(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(self.snapshot())
        except Exception:
            pass
