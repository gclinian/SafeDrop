"""End-to-end localhost test for SafeDrop.

Spins up two TransferManagers on different TCP ports, has them send a file
and a clipboard message to each other (auto-accepting), and verifies the
received bytes / text match what was sent.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Patch the default download dir BEFORE importing transfer so received files
# land in a temp area we control.
import safedrop.config as _config  # noqa: E402

_TEST_DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="safedrop-test-"))
_config.DOWNLOAD_DIR = _TEST_DOWNLOAD_DIR
import safedrop.transfer as _transfer  # noqa: E402

_transfer.DOWNLOAD_DIR = _TEST_DOWNLOAD_DIR

from safedrop.crypto import Identity  # noqa: E402
from safedrop.discovery import Peer  # noqa: E402
from safedrop.transfer import (  # noqa: E402
    ClipboardPayload,
    IncomingRequest,
    TransferManager,
    TransferState,
)


def _wait_for(predicate, timeout=10.0, interval=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def make_manager(port: int, name: str, auto_accept: bool = True):
    identity = Identity.generate()
    tm = TransferManager(identity=identity, device_id=name + "-id", device_name=name, tcp_port=port)
    if auto_accept:
        tm.on_request = lambda req: req.accept()
    tm.start()
    return tm


class SafeDropE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.alice = make_manager(57801, "alice")
        cls.bob = make_manager(57802, "bob")
        # Give the listeners a moment to bind.
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.alice.stop()
        cls.bob.stop()

    def _peer_for_bob(self) -> Peer:
        return Peer(
            device_id="bob-id",
            name="bob",
            platform="test",
            ip="127.0.0.1",
            tcp_port=57802,
            pubkey=self.bob.identity.public_key_b64(),
        )

    def test_file_transfer(self) -> None:
        received_clip: list[ClipboardPayload] = []
        recv_states: dict[str, TransferState] = {}
        self.bob.on_clipboard = received_clip.append
        self.bob.on_state = lambda s: recv_states.__setitem__(s.transfer_id, s) if s.direction == "recv" else None

        payload = os.urandom(150_000)  # ~150KB so we exercise multiple chunks
        src = _TEST_DOWNLOAD_DIR / "src" / "payload.bin"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(payload)

        peer = self._peer_for_bob()
        state = self.alice.send_file(peer, src)

        ok = _wait_for(lambda: state.status in ("done", "failed", "rejected"), timeout=15)
        self.assertTrue(ok, f"transfer did not finish in time, status={state.status}")
        self.assertEqual(state.status, "done", state.error)

        # Look at the receiver's state directly so this is independent of which
        # DOWNLOAD_DIR is currently patched by other test modules.
        ok = _wait_for(
            lambda: recv_states.get(state.transfer_id) is not None
            and recv_states[state.transfer_id].status == "done"
            and recv_states[state.transfer_id].save_path is not None,
            timeout=5,
        )
        self.assertTrue(ok, "bob did not finish receiving")
        recv = recv_states[state.transfer_id]
        self.assertEqual(recv.save_path.read_bytes(), payload)

    def test_clipboard_transfer(self) -> None:
        received: list[ClipboardPayload] = []
        self.bob.on_clipboard = received.append

        peer = self._peer_for_bob()
        text = "hello SafeDrop ✨\nhttps://example.com"
        state = self.alice.send_clipboard(peer, text, "url")

        ok = _wait_for(lambda: state.status in ("done", "failed", "rejected"), timeout=10)
        self.assertTrue(ok, f"clipboard transfer did not finish in time, status={state.status}")
        self.assertEqual(state.status, "done", state.error)

        ok = _wait_for(lambda: bool(received), timeout=5)
        self.assertTrue(ok, "Bob never saw the clipboard payload")
        self.assertEqual(received[0].content, text)
        self.assertEqual(received[0].content_type, "url")

    def test_reject_flow(self) -> None:
        # Configure Bob to reject everything.
        self.bob.on_request = lambda req: req.reject()
        peer = self._peer_for_bob()
        state = self.alice.send_clipboard(peer, "nope", "text")
        ok = _wait_for(lambda: state.status in ("done", "failed", "rejected"), timeout=10)
        self.assertTrue(ok)
        self.assertEqual(state.status, "rejected")
        # Reset for any later tests.
        self.bob.on_request = lambda req: req.accept()

    def test_pair_codes_match(self) -> None:
        # Cross-derive sessions both ways and confirm the pair codes agree.
        from safedrop.crypto import derive_session

        a_session = derive_session(self.alice.identity, self.bob.identity.public_key_b64())
        b_session = derive_session(self.bob.identity, self.alice.identity.public_key_b64())
        self.assertEqual(a_session.pair_code, b_session.pair_code)
        self.assertEqual(len(a_session.pair_code), 4)
        self.assertTrue(a_session.pair_code.isdigit())


if __name__ == "__main__":
    unittest.main(verbosity=2)
