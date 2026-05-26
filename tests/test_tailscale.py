"""Tests for safedrop.tailscale — Tailscale status parsing.

We don't run a real `tailscale` binary; instead we feed a captured
JSON status blob into the parser and verify it produces the right
manual-peer stubs.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safedrop import tailscale as ts  # noqa: E402


# Synthetic but realistic shape — Tailscale's actual status JSON has many
# more fields, but the parser only cares about the ones below.
SAMPLE_STATUS = {
    "Self": {
        "HostName": "macbook",
        "DNSName": "macbook.tail-scale.ts.net.",
        "TailscaleIPs": ["100.64.1.10", "fd7a:115c:a1e0::a:1"],
        "OS": "macOS",
        "Online": True,
    },
    "Peer": {
        "abc123": {
            "HostName": "linux-box",
            "DNSName": "linux-box.tail-scale.ts.net.",
            "TailscaleIPs": ["100.64.1.20"],
            "OS": "linux",
            "Online": True,
        },
        "def456": {
            "HostName": "raspi",
            "DNSName": "raspi.tail-scale.ts.net.",
            "TailscaleIPs": ["100.64.1.30"],
            "OS": "linux",
            "Online": False,
        },
        # Edge case: empty TailscaleIPs (peer joined but never got an
        # address) — should be skipped silently.
        "empty": {
            "HostName": "ghost",
            "TailscaleIPs": [],
            "OS": "linux",
            "Online": True,
        },
    },
}


class ParseTest(unittest.TestCase):
    def test_parses_self_and_peers(self) -> None:
        peers = ts.parse_peers(SAMPLE_STATUS)
        # 3 = self + linux-box + raspi (ghost is dropped: no IP)
        self.assertEqual(len(peers), 3)
        by_host = {p.hostname: p for p in peers}
        self.assertIn("macbook", by_host)
        self.assertTrue(by_host["macbook"].is_self)
        self.assertEqual(by_host["macbook"].tailscale_ip, "100.64.1.10")
        self.assertEqual(by_host["raspi"].online, False)

    def test_to_safedrop_peer_stub(self) -> None:
        peer = ts.parse_peers(SAMPLE_STATUS)[0]
        stub = peer.to_safedrop_peer_stub(port=47891)
        self.assertEqual(stub["tcp_port"], 47891)
        self.assertEqual(stub["pubkey"], "")  # placeholder until first handshake
        self.assertTrue(stub["name"].endswith("(tailscale)"))
        self.assertEqual(stub["device_id"], f"tailscale:{peer.tailscale_ip}")

    def test_prefers_ipv4(self) -> None:
        peers = ts.parse_peers(SAMPLE_STATUS)
        macbook = next(p for p in peers if p.is_self)
        # IPv4 should be picked even though IPv6 is also present.
        self.assertEqual(macbook.tailscale_ip, "100.64.1.10")

    def test_handles_no_peer_section(self) -> None:
        peers = ts.parse_peers({"Self": SAMPLE_STATUS["Self"]})
        self.assertEqual(len(peers), 1)
        self.assertTrue(peers[0].is_self)

    def test_handles_empty_status(self) -> None:
        self.assertEqual(ts.parse_peers({}), [])

    def test_discover_excludes_self_by_default(self) -> None:
        # Monkey-patch fetch_status_json to skip subprocess.
        original = ts.fetch_status_json
        ts.fetch_status_json = lambda **kw: SAMPLE_STATUS  # type: ignore
        try:
            rows = ts.discover_peers(online_only=True, include_self=False)
            self.assertNotIn("macbook", {r.hostname for r in rows})
            self.assertIn("linux-box", {r.hostname for r in rows})
            self.assertNotIn("raspi", {r.hostname for r in rows})  # offline
        finally:
            ts.fetch_status_json = original  # type: ignore


class IsInstalledTest(unittest.TestCase):
    def test_is_installed_returns_bool(self) -> None:
        # Just exercise the code path — actual value depends on $PATH.
        self.assertIsInstance(ts.is_installed(), bool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
