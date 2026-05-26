"""Regression tests for :mod:`safedrop.discovery`.

We don't exercise the full UDP loop here — that's covered indirectly by
``test_tools``, ``test_trust``, and ``test_mcp`` which spin up two real
``HeadlessSafeDrop`` peers. These tests just lock in the broadcast-
target derivation, which is what previously broke on networks where
``255.255.255.255`` is unreachable.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safedrop.discovery import _broadcast_targets, _get_outbound_ip  # noqa: E402


class BroadcastTargetsTest(unittest.TestCase):
    def test_includes_loopback_subnet_and_global(self) -> None:
        targets = _broadcast_targets("192.168.0.101")
        self.assertIn("127.0.0.1", targets)
        self.assertIn("192.168.0.255", targets)
        self.assertIn("255.255.255.255", targets)

    def test_loopback_first(self) -> None:
        """Loopback should fire first so same-machine discovery is reliable
        even when broadcast routing is blocked by a VPN's default route."""
        self.assertEqual(_broadcast_targets("192.168.1.10")[0], "127.0.0.1")

    def test_handles_loopback_ip(self) -> None:
        # If our outbound IP is 127.0.0.1 (no real network), we should still
        # send to loopback + 255.255.255.255 but NOT synthesise a bogus
        # "127.0.0.255" subnet broadcast for the loopback interface.
        targets = _broadcast_targets("127.0.0.1")
        self.assertIn("127.0.0.1", targets)
        self.assertIn("255.255.255.255", targets)
        self.assertNotIn("127.0.0.255", targets)

    def test_handles_malformed_ip(self) -> None:
        targets = _broadcast_targets("not-an-ip")
        # Still returns the safe fallbacks.
        self.assertIn("127.0.0.1", targets)
        self.assertIn("255.255.255.255", targets)
        # And nothing wild.
        self.assertNotIn("not-an-ip", targets)

    def test_no_duplicates(self) -> None:
        targets = _broadcast_targets("192.168.0.101")
        self.assertEqual(len(targets), len(set(targets)))

    def test_handles_empty_string(self) -> None:
        targets = _broadcast_targets("")
        self.assertIn("127.0.0.1", targets)
        self.assertIn("255.255.255.255", targets)


class OutboundIPTest(unittest.TestCase):
    """Smoke test — _get_outbound_ip should never raise, always return a string."""

    def test_returns_string(self) -> None:
        ip = _get_outbound_ip()
        self.assertIsInstance(ip, str)
        # Either a real address with three dots, or "127.0.0.1" as fallback.
        self.assertTrue(ip == "127.0.0.1" or ip.count(".") == 3, ip)


if __name__ == "__main__":
    unittest.main(verbosity=2)
