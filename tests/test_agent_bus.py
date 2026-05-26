"""Tests for v1.5 agent_bus + persistent agent identity."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safedrop.config as _config  # noqa: E402

_DL = Path(tempfile.mkdtemp(prefix="safedrop-agentbus-"))
_config.DOWNLOAD_DIR = _DL
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _DL

from safedrop.crypto import Identity  # noqa: E402
from safedrop.discovery import Peer  # noqa: E402
from safedrop.headless import HeadlessSafeDrop  # noqa: E402
from safedrop.tools import build_default_registry  # noqa: E402
from safedrop.transfer import TransferManager  # noqa: E402
from safedrop_mcp.agent_bus import AgentBus, Mailbox, default_inbox_path  # noqa: E402
from safedrop_mcp.agent_identity import (  # noqa: E402
    AgentIdentity,
    default_path as default_agent_path,
    load_or_create,
    save as save_identity,
)


def _wait_for(predicate, timeout=12.0, interval=0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class AgentIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="safedrop-agentid-"))
        self.path = self.tmp / "agent_id.json"

    def test_load_creates_when_missing(self) -> None:
        ident = load_or_create(self.path, label="test-machine")
        self.assertTrue(self.path.exists())
        self.assertTrue(ident.agent_id.startswith("agent-"))
        self.assertEqual(ident.label, "test-machine")

    def test_load_is_stable_across_calls(self) -> None:
        first = load_or_create(self.path, label="alpha")
        second = load_or_create(self.path, label="ignored")  # label ignored on second load
        self.assertEqual(first.agent_id, second.agent_id)
        self.assertEqual(second.label, "alpha")

    def test_load_recovers_from_corrupt_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("not even close to json", encoding="utf-8")
        ident = load_or_create(self.path, label="rebuilt")
        self.assertTrue(ident.agent_id.startswith("agent-"))
        self.assertEqual(ident.label, "rebuilt")

    def test_save_round_trips(self) -> None:
        ident = AgentIdentity(agent_id="agent-deadbeef0000", label="boxA")
        save_identity(ident, self.path)
        reloaded = AgentIdentity.from_dict(json.loads(self.path.read_text("utf-8")))
        self.assertEqual(reloaded, ident)


class MailboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="safedrop-mailbox-"))
        self.mailbox = Mailbox(self.tmp / "inbox.jsonl")

    def test_append_and_read_back(self) -> None:
        self.mailbox.append({"ts": time.time(), "from_agent_id": "agent-a", "content": "hi"})
        rows = self.mailbox.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "hi")

    def test_read_since_filters_old(self) -> None:
        self.mailbox.append({"ts": 100.0, "content": "old"})
        self.mailbox.append({"ts": 200.0, "content": "new"})
        rows = self.mailbox.read(since_ts=150.0)
        self.assertEqual([r["content"] for r in rows], ["new"])

    def test_read_limit_keeps_recent(self) -> None:
        for i in range(10):
            self.mailbox.append({"ts": float(i), "content": f"m{i}"})
        rows = self.mailbox.read(limit=3)
        self.assertEqual([r["content"] for r in rows], ["m7", "m8", "m9"])

    def test_read_ignores_blank_lines(self) -> None:
        path = self.tmp / "inbox.jsonl"
        self.mailbox.append({"ts": 1.0, "content": "ok"})
        # Inject a blank line + garbage line — should not raise.
        with path.open("a") as fh:
            fh.write("\n\n")
            fh.write("not json\n")
        rows = self.mailbox.read()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "ok")


class AgentBusE2ETest(unittest.TestCase):
    """Two TransferManager peers wired up directly (no UDP discovery), each
    with an AgentBus. Sender calls the receiver's ``agent_bus_recv`` and the
    message lands in the receiver's on-disk mailbox.

    We construct peers manually rather than relying on UDP broadcast so the
    test is hermetic — VPNs / firewalls that drop 255.255.255.255 don't
    affect us here (see test_e2e.py for the same pattern).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="safedrop-agentbus-e2e-"))

        # Identities + mailboxes.
        cls.alice_ident = AgentIdentity(agent_id="agent-alice0001", label="alice")
        cls.bob_ident = AgentIdentity(agent_id="agent-bob000001", label="bob")
        cls.alice_mailbox = Mailbox(cls.tmp / "alice_inbox.jsonl")
        cls.bob_mailbox = Mailbox(cls.tmp / "bob_inbox.jsonl")

        cls.alice_bus = AgentBus(cls.alice_ident, mailbox=cls.alice_mailbox)
        cls.bob_bus = AgentBus(cls.bob_ident, mailbox=cls.bob_mailbox)

        # Build registries with bus tools pre-registered.
        alice_reg = build_default_registry()
        bob_reg = build_default_registry()
        cls.alice_bus.register_peer_tools(alice_reg)
        cls.bob_bus.register_peer_tools(bob_reg)

        cls.alice_identity = Identity.generate()
        cls.bob_identity = Identity.generate()

        cls.alice_tm = TransferManager(
            identity=cls.alice_identity, device_id="alice-id",
            device_name="alice", tcp_port=58001, tool_registry=alice_reg,
        )
        cls.bob_tm = TransferManager(
            identity=cls.bob_identity, device_id="bob-id",
            device_name="bob", tcp_port=58002, tool_registry=bob_reg,
        )
        cls.alice_tm.on_request = lambda req: req.accept()
        cls.bob_tm.on_request = lambda req: req.accept()
        cls.alice_tm.start()
        cls.bob_tm.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.alice_tm.stop()
        cls.bob_tm.stop()

    def _bob_peer(self) -> Peer:
        return Peer(
            device_id="bob-id", name="bob", platform="test",
            ip="127.0.0.1", tcp_port=58002,
            pubkey=self.bob_identity.public_key_b64(),
        )

    def test_whoami_round_trip(self) -> None:
        out = self.alice_tm.call_remote_tool(self._bob_peer(), "agent_bus_whoami", {}, timeout=5)
        self.assertIn("result", out, out)
        self.assertEqual(out["result"]["agent_id"], "agent-bob000001")
        self.assertEqual(out["result"]["label"], "bob")

    def test_send_message_delivers_to_inbox(self) -> None:
        before = len(self.bob_bus.read_inbox())
        out = self.alice_tm.call_remote_tool(self._bob_peer(), "agent_bus_recv", {
            "from_agent_id": self.alice_ident.agent_id,
            "from_label":    self.alice_ident.label,
            "content":       "hello from alice",
        }, timeout=5)
        self.assertIn("result", out, out)
        self.assertEqual(out["result"]["status"], "delivered")
        rows = self.bob_bus.read_inbox()
        self.assertGreater(len(rows), before)
        self.assertEqual(rows[-1]["from_agent_id"], "agent-alice0001")
        self.assertEqual(rows[-1]["content"], "hello from alice")

    def test_recv_rejects_missing_content(self) -> None:
        out = self.alice_tm.call_remote_tool(self._bob_peer(), "agent_bus_recv", {
            "from_agent_id": self.alice_ident.agent_id,
        }, timeout=5)
        self.assertIn("result", out, out)
        self.assertEqual(out["result"]["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
