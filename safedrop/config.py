"""Tunable constants for SafeDrop."""

from __future__ import annotations

import platform
import socket
import uuid
from pathlib import Path

VERSION = "1.0"

DISCOVERY_PORT = 47890
TCP_PORT = 47891

BROADCAST_INTERVAL = 3.0
PEER_TTL = 10.0

CHUNK_SIZE = 64 * 1024
LENGTH_PREFIX = 4

DOWNLOAD_DIR = Path.home() / "Downloads" / "SafeDrop"


def default_device_name() -> str:
    try:
        return f"{socket.gethostname()} ({platform.system()})"
    except Exception:
        return f"SafeDrop-{uuid.uuid4().hex[:6]}"


def new_device_id() -> str:
    return uuid.uuid4().hex
