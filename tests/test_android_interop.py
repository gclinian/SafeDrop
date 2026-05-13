"""Cross-language handshake interop check.

Connect to a running SafeDrop instance on a TCP socket (e.g. the Android
emulator's :47891 forwarded to localhost via `adb forward`) and run the
plaintext HELLO/HELLO_ACK handshake. The peer's HELLO_ACK contains the
pair code it computed; we also compute our own. If they match,
X25519 + HKDF + Fernet key derivation is byte-for-byte interoperable.

Usage:
    adb forward tcp:48050 tcp:47891
    .venv/bin/python tests/test_android_interop.py 127.0.0.1 48050
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safedrop.crypto import Identity, derive_session
from safedrop.protocol import recv_json, send_json


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 48050

    identity = Identity.generate()
    print(f"Local pubkey:  {identity.public_key_b64()}")

    with socket.create_connection((host, port), timeout=10) as s:
        s.settimeout(10)
        send_json(s, {
            "type": "HELLO",
            "device_id": "interop-test",
            "name": "interop-test",
            "platform": "Python",
            "pubkey": identity.public_key_b64(),
            "version": "1.0",
        })
        ack = recv_json(s)

    assert ack.get("type") == "HELLO_ACK", f"expected HELLO_ACK, got {ack!r}"

    peer_pubkey = ack["pubkey"]
    peer_name = ack.get("name", "?")
    peer_pair = ack.get("pair_code")
    print(f"Peer:          {peer_name}")
    print(f"Peer pubkey:   {peer_pubkey}")

    session = derive_session(identity, peer_pubkey)
    local_pair = session.pair_code

    print(f"Local pair code from ECDH:  {local_pair}")
    print(f"Peer pair code in HELLO_ACK: {peer_pair}")

    if local_pair == peer_pair:
        print("\n✅ PASS — both ends derived the same Fernet key + pair code.")
        return 0
    print("\n❌ FAIL — pair codes differ; crypto interop broken.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
