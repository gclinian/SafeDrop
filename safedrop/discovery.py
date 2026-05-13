"""UDP-broadcast peer discovery.

Each running SafeDrop instance:

* periodically broadcasts a HELLO JSON datagram on UDP port DISCOVERY_PORT
  to 255.255.255.255 so peers in the same LAN can see it;
* listens on the same UDP port for HELLOs / BYEs from peers;
* maintains a peer table with a TTL — peers we haven't heard from in
  PEER_TTL seconds are dropped.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

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
    last_seen: float = field(default_factory=time.time)


PeerCallback = Callable[[dict[str, Peer]], None]


def _get_outbound_ip() -> str:
    """Best-effort local LAN IP detection.

    Opens a UDP socket "towards" a public address — the OS picks the
    routing-correct local interface but no packets are actually sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        sock.close()
    return ip


class DiscoveryService:
    """Background UDP broadcaster + listener."""

    def __init__(
        self,
        device_id: str,
        device_name: str,
        platform_name: str,
        tcp_port: int,
        pubkey_b64: str,
        on_change: PeerCallback | None = None,
    ) -> None:
        self.device_id = device_id
        self.device_name = device_name
        self.platform_name = platform_name
        self.tcp_port = tcp_port
        self.pubkey_b64 = pubkey_b64
        self.on_change = on_change

        self.local_ip = _get_outbound_ip()
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
        for target in ("255.255.255.255",):
            try:
                self._send_sock.sendto(payload, (target, DISCOVERY_PORT))
            except OSError:
                pass

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
            peer = Peer(
                device_id=device_id,
                name=str(msg.get("name", "unknown")),
                platform=str(msg.get("platform", "?")),
                ip=sender_ip,
                tcp_port=int(msg.get("tcp_port", 0)),
                pubkey=str(msg.get("pubkey", "")),
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
