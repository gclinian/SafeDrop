"""JSON message framing for SafeDrop TCP control channel.

Wire format: [4-byte big-endian length N][N bytes payload].
Payload is JSON bytes — plaintext during the handshake, Fernet-encrypted
ciphertext afterwards. The framing layer itself is agnostic to that.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any, Callable

from .config import LENGTH_PREFIX

MAX_FRAME = 64 * 1024 * 1024  # 64 MB — sanity cap to refuse absurd frames.


class ProtocolError(Exception):
    """Raised on malformed or oversized frames."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks: list[bytes] = []
    remaining = n
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed connection mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"frame too large ({len(payload)} bytes)")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def recv_frame(sock: socket.socket) -> bytes:
    header = _recv_exact(sock, LENGTH_PREFIX)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME:
        raise ProtocolError(f"frame too large ({length} bytes)")
    return _recv_exact(sock, length)


def send_json(sock: socket.socket, msg: dict[str, Any], encrypt: Callable[[bytes], bytes] | None = None) -> None:
    raw = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    if encrypt is not None:
        raw = encrypt(raw)
    send_frame(sock, raw)


def recv_json(sock: socket.socket, decrypt: Callable[[bytes], bytes] | None = None) -> dict[str, Any]:
    raw = recv_frame(sock)
    if decrypt is not None:
        raw = decrypt(raw)
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError("JSON frame missing 'type' field")
    return msg
