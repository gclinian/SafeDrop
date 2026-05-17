"""Phase 2.1 — TrustPolicy persistence + integration with TransferManager."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safedrop.config as _config  # noqa: E402
_DL = Path(tempfile.mkdtemp(prefix="safedrop-trust-test-"))
_config.DOWNLOAD_DIR = _DL
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _DL

from safedrop.headless import HeadlessSafeDrop  # noqa: E402
from safedrop.tools import ToolRegistry, ToolSpec, register_default_tools  # noqa: E402
from safedrop.trust import (  # noqa: E402
    DECISION_ALLOW,
    DECISION_DENY,
    AuditWriter,
    TrustPolicy,
)


def _wait_for(predicate, timeout=12.0, interval=0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TrustPolicyUnitTest(unittest.TestCase):
    def test_default_is_ask(self) -> None:
        tp = TrustPolicy(store_path=None)
        self.assertEqual(tp.check("peer-a", "read_clipboard"), "ask")

    def test_round_trip_persists(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="trust-")) / "trust.json"
        tp = TrustPolicy(store_path=path)
        tp.set("peer-a", "read_clipboard", DECISION_ALLOW)
        tp.set("peer-a", "run_shell", DECISION_DENY)
        tp.set("peer-b", "system_info", DECISION_ALLOW)
        # Reload from disk
        tp2 = TrustPolicy(store_path=path)
        self.assertEqual(tp2.check("peer-a", "read_clipboard"), DECISION_ALLOW)
        self.assertEqual(tp2.check("peer-a", "run_shell"), DECISION_DENY)
        self.assertEqual(tp2.check("peer-b", "system_info"), DECISION_ALLOW)
        self.assertEqual(tp2.check("peer-b", "run_shell"), "ask")

    def test_clear_removes(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="trust-")) / "trust.json"
        tp = TrustPolicy(store_path=path)
        tp.set("peer-a", "read_clipboard", DECISION_ALLOW)
        tp.set("peer-a", "system_info", DECISION_ALLOW)
        tp.clear("peer-a", "read_clipboard")
        self.assertEqual(tp.check("peer-a", "read_clipboard"), "ask")
        self.assertEqual(tp.check("peer-a", "system_info"), DECISION_ALLOW)
        tp.clear("peer-a")  # clear all for this peer
        self.assertEqual(tp.check("peer-a", "system_info"), "ask")


class AuditWriterUnitTest(unittest.TestCase):
    def test_appends_jsonl(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="audit-")) / "audit.jsonl"
        w = AuditWriter(path=path)
        from safedrop.transfer import ToolCallAuditEntry
        e = ToolCallAuditEntry(
            timestamp=time.time(),
            direction="inbound",
            peer_name="Alice",
            peer_ip="10.0.0.5",
            tool_name="read_clipboard",
            arguments={},
            decision="allowed",
            result_summary="hello",
        )
        w.append(e)
        w.append(e)
        tailed = w.tail(limit=10)
        self.assertEqual(len(tailed), 2)
        self.assertEqual(tailed[0]["peer_name"], "Alice")
        self.assertEqual(tailed[0]["decision"], "allowed")
        self.assertIn("timestamp_iso", tailed[0])


class TrustPolicyIntegrationTest(unittest.TestCase):
    """Two peers; verify that trust_policy short-circuits the authorizer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.alice = HeadlessSafeDrop(name_suffix="A")

        bob_reg = ToolRegistry()
        register_default_tools(bob_reg)
        bob_reg.register(ToolSpec(
            name="ping",
            description="returns pong",
            input_schema={"type": "object", "properties": {}},
            handler=lambda _: {"reply": "pong"},
        ))
        cls.bob_trust = TrustPolicy(store_path=None)  # in-memory only
        cls.bob = HeadlessSafeDrop(name_suffix="B", tool_registry=bob_reg)
        cls.bob.transfer.trust_policy = cls.bob_trust

        cls.alice.start()
        cls.bob.start()

        ok = _wait_for(
            lambda: cls.alice.discovery
            and any(", B)" in p.name for p in cls.alice.discovery.snapshot().values()),
            timeout=12,
        )
        if not ok:
            cls.alice.stop()
            cls.bob.stop()
            raise AssertionError("UDP discovery did not converge")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.alice.stop()
        cls.bob.stop()

    def setUp(self) -> None:
        # Reset trust + authorizer between tests
        self.bob_trust._policies.clear()
        self.bob.transfer.on_tool_call = None

    def test_trust_allow_short_circuits_authorizer(self) -> None:
        peer_b = self.alice.find_peer(", B)")
        authorizer_calls: list = []
        self.bob.transfer.on_tool_call = lambda req: authorizer_calls.append(req) or False  # would deny
        # But trust policy says allow:
        self.bob_trust.set(self.alice.device_id, "ping", DECISION_ALLOW)

        out = self.alice.transfer.call_remote_tool(peer_b, "ping", {}, timeout=5)
        self.assertIn("result", out, out)
        self.assertEqual(out["result"], {"reply": "pong"})
        # The authorizer must NOT have been consulted:
        self.assertEqual(authorizer_calls, [])

    def test_trust_deny_short_circuits_authorizer(self) -> None:
        peer_b = self.alice.find_peer(", B)")
        authorizer_calls: list = []
        self.bob.transfer.on_tool_call = lambda req: authorizer_calls.append(req) or True  # would allow
        # But trust policy says deny:
        self.bob_trust.set(self.alice.device_id, "ping", DECISION_DENY)

        out = self.alice.transfer.call_remote_tool(peer_b, "ping", {}, timeout=5)
        self.assertIn("error", out, out)
        self.assertEqual(authorizer_calls, [])

    def test_trust_ask_falls_through_to_authorizer(self) -> None:
        peer_b = self.alice.find_peer(", B)")
        seen: list = []
        # Default "ask" (no policy set) → authorizer is consulted.
        self.bob.transfer.on_tool_call = lambda req: (seen.append(req), True)[1]
        out = self.alice.transfer.call_remote_tool(peer_b, "ping", {}, timeout=5)
        self.assertEqual(out.get("result"), {"reply": "pong"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].tool_name, "ping")


if __name__ == "__main__":
    unittest.main(verbosity=2)
