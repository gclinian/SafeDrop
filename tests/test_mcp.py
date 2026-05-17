"""Integration test for safedrop_mcp.

Spins up two HeadlessSafeDrop peers on localhost, exercises the four tools
(list_devices, send_file, send_text, wait_for_drop equivalent) directly
against the underlying service. The MCP protocol layer is just JSON-RPC
on top — we trust the MCP SDK's transport tests for that.
"""

from __future__ import annotations

import json
import os
import queue as _queue
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Patch the download dir BEFORE importing transfer so received files land
# in a sandbox.
import safedrop.config as _config  # noqa: E402
_DL = Path(tempfile.mkdtemp(prefix="safedrop-mcp-test-"))
_config.DOWNLOAD_DIR = _DL
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _DL

from safedrop.headless import HeadlessSafeDrop, wait_terminal as _wait_terminal  # noqa: E402


def _wait_for(predicate, timeout=10.0, interval=0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class MCPIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.alice = HeadlessSafeDrop(name_suffix="A")
        cls.bob = HeadlessSafeDrop(name_suffix="B")
        cls.alice.start()
        cls.bob.start()

        ok = _wait_for(
            lambda: cls.alice.discovery is not None
            and any(", B)" in p.name for p in cls.alice.discovery.snapshot().values())
            and cls.bob.discovery is not None
            and any(", A)" in p.name for p in cls.bob.discovery.snapshot().values()),
            timeout=12,
        )
        if not ok:
            cls.alice.stop()
            cls.bob.stop()
            raise AssertionError("UDP discovery did not converge in 12s")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.alice.stop()
        cls.bob.stop()

    def test_list_devices_sees_peer(self) -> None:
        peers = self.alice.discovery.snapshot()
        names = [p.name for p in peers.values()]
        self.assertTrue(any(", B)" in n for n in names), names)

    def test_dynamic_ports_differ(self) -> None:
        self.assertGreater(self.alice.transfer.tcp_port, 0)
        self.assertGreater(self.bob.transfer.tcp_port, 0)
        self.assertNotEqual(self.alice.transfer.tcp_port, self.bob.transfer.tcp_port)

    def test_send_text_e2e(self) -> None:
        peer_b = self.alice.find_peer(", B)")
        state = self.alice.transfer.send_clipboard(peer_b, "hello from A 👋", "text")
        _wait_terminal(state, timeout=5)
        self.assertEqual(state.status, "done", state.error)

        try:
            recv = self.bob._drop_queue.get(timeout=5)
        except _queue.Empty:
            self.fail("B did not receive the drop")
        self.assertEqual(recv.clipboard_content, "hello from A 👋")
        self.assertEqual(recv.clipboard_content_type, "text")

    def test_send_file_e2e(self) -> None:
        src = _DL / "src" / "mcp_payload.bin"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(os.urandom(120_000))  # ~120 KB, multi-chunk

        peer_b = self.alice.find_peer(", B)")
        state = self.alice.transfer.send_file(peer_b, src)
        _wait_terminal(state, timeout=10)
        self.assertEqual(state.status, "done", state.error)

        try:
            recv = self.bob._drop_queue.get(timeout=5)
        except _queue.Empty:
            self.fail("B did not receive the file drop")
        self.assertIsNotNone(recv.save_path)
        self.assertEqual(recv.save_path.read_bytes(), src.read_bytes())

    def test_find_peer_ambiguity(self) -> None:
        # Bring up a second 'B'-suffixed peer so the substring match is ambiguous.
        third = HeadlessSafeDrop(name_suffix="B")
        third.start()
        try:
            _wait_for(
                lambda: sum(1 for p in self.alice.discovery.snapshot().values() if ", B)" in p.name) >= 2,
                timeout=12,
            )
            with self.assertRaises(LookupError):
                self.alice.find_peer(", B)")
        finally:
            third.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
