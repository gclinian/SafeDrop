#!/usr/bin/env python3
"""Throughput benchmark for SafeDrop.

Three modes:

    # 1. Local loopback (one process, two managers on 127.0.0.1)
    python bench.py
    python bench.py --sizes 1KB 100KB 1MB 10MB 100MB

    # 2. Run as a receiver on this machine (auto-accept everything)
    python bench.py receive --port 47891

    # 3. Run as a sender against a receiver running elsewhere
    python bench.py send <host> --port 47891 --sizes 1MB 10MB

Each row reports:
    * size        — payload size
    * wall (s)    — from send_file() to status=done (includes handshake)
    * xfer (s)    — only the chunk-streaming portion
    * MB/s        — payload size / xfer time
    * ok          — sha256 of received file matches the source
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Tests / bench want received files in a sandbox, not the real Downloads folder.
_SCRATCH = Path(tempfile.mkdtemp(prefix="safedrop-bench-"))
import safedrop.config as _config  # noqa: E402
_config.DOWNLOAD_DIR = _SCRATCH
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _SCRATCH

from safedrop.config import TCP_PORT  # noqa: E402
from safedrop.crypto import Identity  # noqa: E402
from safedrop.discovery import Peer  # noqa: E402
from safedrop.transfer import TransferManager, TransferState  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_UNITS = {"B": 1, "KB": 1024, "K": 1024, "MB": 1024**2, "M": 1024**2, "GB": 1024**3, "G": 1024**3}


def parse_size(spec: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG]?B?)\s*", spec, re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError(f"bad size: {spec!r}")
    value = float(m.group(1))
    unit = (m.group(2) or "B").upper().rstrip("B") + "B"
    if unit == "B":
        return int(value)
    return int(value * _UNITS[unit])


def human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024.0 or unit == "GB":
            return f"{f:,.1f}{unit}" if unit != "B" else f"{int(f)}B"
        f /= 1024.0
    return f"{f:,.1f}TB"


def make_payload(path: Path, size: int) -> str:
    """Write `size` random bytes to `path`, return sha256 hex."""
    h = hashlib.sha256()
    chunk = 1 << 20  # 1 MB
    with path.open("wb") as f:
        remaining = size
        while remaining > 0:
            buf = os.urandom(min(chunk, remaining))
            f.write(buf)
            h.update(buf)
            remaining -= len(buf)
    return h.hexdigest()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Sample:
    size: int
    wall_s: float
    xfer_s: float
    ok: bool

    @property
    def mb_per_s(self) -> float:
        if self.xfer_s <= 0:
            return float("inf")
        return self.size / self.xfer_s / (1024 * 1024)


def print_table(samples: list[Sample]) -> None:
    print()
    print(f"{'size':>10}  {'wall (s)':>9}  {'xfer (s)':>9}  {'MB/s':>8}  ok")
    print("-" * 50)
    for s in samples:
        print(f"{human(s.size):>10}  {s.wall_s:>9.3f}  {s.xfer_s:>9.3f}  {s.mb_per_s:>8.2f}  {'✓' if s.ok else '✗'}")
    print()


def send_and_time(tm: TransferManager, peer: Peer, src: Path, timeout: float) -> tuple[TransferState, float, float]:
    t_call = time.perf_counter()
    state = tm.send_file(peer, src)

    t_transfer_start = [None]  # set when status first becomes "transferring"

    deadline = time.time() + timeout
    while time.time() < deadline:
        if state.status == "transferring" and t_transfer_start[0] is None:
            t_transfer_start[0] = time.perf_counter()
        if state.status in ("done", "failed", "rejected"):
            break
        time.sleep(0.01)

    t_done = time.perf_counter()
    if state.status != "done":
        raise RuntimeError(f"transfer ended with status={state.status}: {state.error}")

    wall = t_done - t_call
    xfer = t_done - (t_transfer_start[0] or t_call)
    return state, wall, xfer


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def run_local(sizes: list[int], timeout: float) -> None:
    sender = TransferManager(Identity.generate(), "sender-id", "sender", tcp_port=58801)
    receiver = TransferManager(Identity.generate(), "receiver-id", "receiver", tcp_port=58802)
    receiver.on_request = lambda req: req.accept()

    recv_states: dict[str, TransferState] = {}
    def _track_recv(s: TransferState) -> None:
        if s.direction == "recv":
            recv_states[s.transfer_id] = s
    receiver.on_state = _track_recv

    sender.start()
    receiver.start()
    time.sleep(0.2)

    peer = Peer(
        device_id="receiver-id",
        name="receiver",
        platform="local",
        ip="127.0.0.1",
        tcp_port=58802,
        pubkey=receiver.identity.public_key_b64(),
    )

    src_dir = _SCRATCH / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []
    try:
        for size in sizes:
            print(f"-> {human(size):>10} ...", end="", flush=True)
            src = src_dir / f"payload_{size}.bin"
            digest = make_payload(src, size)
            state, wall, xfer = send_and_time(sender, peer, src, timeout=timeout)
            # Wait briefly for the receiver-side state to settle.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                rs = recv_states.get(state.transfer_id)
                if rs is not None and rs.status == "done" and rs.save_path is not None:
                    break
                time.sleep(0.01)
            rs = recv_states.get(state.transfer_id)
            ok = rs is not None and rs.save_path is not None and sha256_of(rs.save_path) == digest
            print(f"  wall {wall:6.3f}s  xfer {xfer:6.3f}s  {size/xfer/1048576:7.2f} MB/s  {'OK' if ok else 'BAD'}")
            samples.append(Sample(size=size, wall_s=wall, xfer_s=xfer, ok=ok))
    finally:
        sender.stop()
        receiver.stop()
    print_table(samples)


def run_receive(port: int) -> None:
    tm = TransferManager(Identity.generate(), socket.gethostname() + "-bench", socket.gethostname(), tcp_port=port)
    tm.on_request = lambda req: req.accept()
    tm.on_state = _print_recv_state
    tm.start()
    print(f"Listening on 0.0.0.0:{port} — auto-accepting transfers.")
    print(f"Save dir: {_SCRATCH}")
    print(f"Receiver pubkey (give this to the sender):\n  {tm.identity.public_key_b64()}")
    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        tm.stop()


_recv_seen: set[str] = set()


def _print_recv_state(state: TransferState) -> None:
    if state.status == "done" and state.transfer_id not in _recv_seen:
        _recv_seen.add(state.transfer_id)
        print(f"  ← {human(state.size):>10} from {state.peer_name}: {state.name} ({state.save_path})")


def run_send(host: str, port: int, pubkey: str, sizes: list[int], timeout: float) -> None:
    if not pubkey:
        print("ERROR: --peer-pubkey is required for cross-machine sending.", file=sys.stderr)
        sys.exit(2)
    sender = TransferManager(Identity.generate(), "sender-id", socket.gethostname(), tcp_port=58901)
    sender.start()
    peer = Peer(
        device_id="bench-receiver",
        name=host,
        platform="?",
        ip=host,
        tcp_port=port,
        pubkey=pubkey,
    )
    src_dir = _SCRATCH / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []
    try:
        for size in sizes:
            print(f"-> {human(size):>10} ...", end="", flush=True)
            src = src_dir / f"payload_{size}.bin"
            make_payload(src, size)
            state, wall, xfer = send_and_time(sender, peer, src, timeout=timeout)
            ok = state.status == "done"
            print(f"  wall {wall:6.3f}s  xfer {xfer:6.3f}s  {size/xfer/1048576:7.2f} MB/s  {'OK' if ok else 'BAD'}")
            samples.append(Sample(size=size, wall_s=wall, xfer_s=xfer, ok=ok))
    finally:
        sender.stop()
    print_table(samples)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DEFAULT_SIZES = ["1KB", "100KB", "1MB", "10MB", "100MB"]


def main() -> None:
    parser = argparse.ArgumentParser(description="SafeDrop throughput benchmark")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per transfer (default 300)")
    sub = parser.add_subparsers(dest="mode")

    p_local = sub.add_parser("local", help="loopback benchmark in one process (default)")
    p_local.add_argument("--sizes", nargs="+", type=parse_size, default=[parse_size(s) for s in DEFAULT_SIZES])

    p_recv = sub.add_parser("receive", help="auto-accepting receiver")
    p_recv.add_argument("--port", type=int, default=TCP_PORT)

    p_send = sub.add_parser("send", help="send to a remote bench receiver")
    p_send.add_argument("host")
    p_send.add_argument("--port", type=int, default=TCP_PORT)
    p_send.add_argument("--peer-pubkey", required=True, help="base64 X25519 pubkey printed by the receiver")
    p_send.add_argument("--sizes", nargs="+", type=parse_size, default=[parse_size(s) for s in DEFAULT_SIZES])

    args = parser.parse_args()
    if args.mode in (None, "local"):
        sizes = getattr(args, "sizes", None) or [parse_size(s) for s in DEFAULT_SIZES]
        run_local(sizes, timeout=args.timeout)
    elif args.mode == "receive":
        run_receive(args.port)
    elif args.mode == "send":
        run_send(args.host, args.port, args.peer_pubkey, args.sizes, timeout=args.timeout)


if __name__ == "__main__":
    main()
