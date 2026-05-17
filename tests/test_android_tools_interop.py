"""Cross-language interop for Phase 2.2 — Android exposes tools.

Talks to a running Android SafeDrop instance over its TCP listener
(forwarded via ``adb forward``) and verifies the LIST_TOOLS / CALL_TOOL
protocol works between Python and Kotlin byte-for-byte.

Usage:
    adb forward tcp:48050 tcp:47891
    .venv/bin/python tests/test_android_tools_interop.py 127.0.0.1 48050
"""

from __future__ import annotations

import socket
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safedrop.crypto import Identity, derive_session
from safedrop.protocol import recv_json, send_json


def _handshake(host: str, port: int, identity: Identity):
    s = socket.create_connection((host, port), timeout=10)
    s.settimeout(10)
    send_json(s, {
        "type": "HELLO",
        "device_id": "interop-tool-test",
        "name": "interop-tool-test",
        "platform": "Python",
        "pubkey": identity.public_key_b64(),
        "version": "1.0",
    })
    ack = recv_json(s)
    assert ack.get("type") == "HELLO_ACK", f"expected HELLO_ACK, got {ack!r}"
    session = derive_session(identity, ack["pubkey"])
    return s, session, ack


def test_list_tools(host: str, port: int) -> dict:
    identity = Identity.generate()
    sock, session, ack = _handshake(host, port, identity)
    with sock:
        send_json(sock, {"type": "LIST_TOOLS", "request_id": uuid.uuid4().hex},
                  encrypt=session.encrypt)
        resp = recv_json(sock, decrypt=session.decrypt)
        assert resp.get("type") == "TOOLS_LIST", f"expected TOOLS_LIST, got {resp!r}"
        tools = resp.get("tools") or []
        names = sorted(t["name"] for t in tools)
        print(f"Android peer        : {ack.get('name')}")
        print(f"Android pair_code   : {ack.get('pair_code')}")
        print(f"Tools advertised    : {names}")
        assert "system_info" in names, names
        assert "read_clipboard" in names, names
        assert "write_clipboard" in names, names
        print("✅ LIST_TOOLS PASS")
        return {"names": names, "ack": ack}


def test_call_unknown_tool(host: str, port: int) -> None:
    identity = Identity.generate()
    sock, session, _ack = _handshake(host, port, identity)
    with sock:
        send_json(sock, {
            "type": "CALL_TOOL",
            "request_id": uuid.uuid4().hex,
            "name": "bogus_tool_that_does_not_exist",
            "arguments": {},
        }, encrypt=session.encrypt)
        resp = recv_json(sock, decrypt=session.decrypt)
        assert resp.get("type") == "CALL_TOOL_RESULT", resp
        assert "error" in resp, resp
        assert "not available" in resp["error"], resp
        print(f"Unknown-tool error  : {resp['error']}")
        print("✅ CALL_TOOL unknown-tool PASS (no UI prompt needed)")


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 48050

    test_list_tools(host, port)
    test_call_unknown_tool(host, port)
    print()
    print("All Android tool-protocol interop checks PASSED.")
    print("(Known-tool CALL_TOOL would trigger an Allow/Deny dialog on the device — skipped here.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
