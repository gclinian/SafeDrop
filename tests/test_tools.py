"""Cross-device tools (Phase 2) integration tests.

Two HeadlessSafeDrop peers on localhost. Peer A lists peer B's tools,
invokes them remotely over the encrypted TCP channel, and verifies that
the authorizer + audit-log machinery does the right thing.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safedrop.config as _config  # noqa: E402
_DL = Path(tempfile.mkdtemp(prefix="safedrop-tools-test-"))
_config.DOWNLOAD_DIR = _DL
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _DL

from safedrop.headless import HeadlessSafeDrop  # noqa: E402
from safedrop.tools import ToolRegistry, ToolSpec  # noqa: E402


def _wait_for(predicate, timeout=12.0, interval=0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class CrossDeviceToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.alice = HeadlessSafeDrop(name_suffix="ALICE")

        # Bob runs a custom registry — adds an `add` tool we can call and
        # verify, alongside the defaults (system_info, read_clipboard, …).
        bob_registry = ToolRegistry()
        from safedrop.tools import register_default_tools
        register_default_tools(bob_registry)
        bob_registry.register(ToolSpec(
            name="add",
            description="Return a + b",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"}, "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
            handler=lambda args: {"sum": args["a"] + args["b"]},
        ))
        cls.bob = HeadlessSafeDrop(name_suffix="BOB", tool_registry=bob_registry)

        cls.alice.start()
        cls.bob.start()

        ok = _wait_for(
            lambda: cls.alice.discovery
            and any(", BOB)" in p.name for p in cls.alice.discovery.snapshot().values())
            and cls.bob.discovery
            and any(", ALICE)" in p.name for p in cls.bob.discovery.snapshot().values()),
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

    def _peer_bob(self):
        return self.alice.find_peer(", BOB)")

    def test_hello_carries_capabilities(self) -> None:
        bob = self._peer_bob()
        self.assertIn("safedrop.tools", bob.capabilities)
        self.assertIn("safedrop.transfer", bob.capabilities)
        self.assertTrue(bob.has_capability("safedrop.tools"))

    def test_list_remote_tools(self) -> None:
        bob = self._peer_bob()
        tools = self.alice.transfer.list_remote_tools(bob, timeout=5)
        names = {t["name"] for t in tools}
        # Defaults Bob registered:
        self.assertIn("system_info", names)
        self.assertIn("read_clipboard", names)
        self.assertIn("write_clipboard", names)
        # Bob's custom tool:
        self.assertIn("add", names)

    def test_call_remote_tool_success(self) -> None:
        bob = self._peer_bob()
        out = self.alice.transfer.call_remote_tool(bob, "add", {"a": 4, "b": 38}, timeout=5)
        self.assertIn("result", out, out)
        self.assertEqual(out["result"], {"sum": 42})

    def test_call_remote_tool_unknown_name(self) -> None:
        bob = self._peer_bob()
        out = self.alice.transfer.call_remote_tool(bob, "no_such_tool", {}, timeout=5)
        self.assertIn("error", out)
        self.assertIn("not available", out["error"])

    def test_authorizer_can_deny(self) -> None:
        bob = self._peer_bob()
        # Deny every inbound CALL_TOOL on Bob's side.
        self.bob.transfer.on_tool_call = lambda req: False
        try:
            out = self.alice.transfer.call_remote_tool(bob, "add", {"a": 1, "b": 2}, timeout=5)
            self.assertIn("error", out)
            self.assertIn("denied", out["error"])
        finally:
            self.bob.transfer.on_tool_call = None

    def test_audit_log_records_both_sides(self) -> None:
        bob = self._peer_bob()
        self.alice.transfer.audit_log.clear()
        self.bob.transfer.audit_log.clear()
        self.alice.transfer.call_remote_tool(bob, "system_info", {}, timeout=5)
        self.assertTrue(_wait_for(lambda: len(self.bob.transfer.audit_log) >= 1, timeout=3))
        self.assertEqual(self.alice.transfer.audit_log[-1].direction, "outbound")
        self.assertEqual(self.alice.transfer.audit_log[-1].decision, "allowed")
        self.assertEqual(self.bob.transfer.audit_log[-1].direction, "inbound")
        self.assertEqual(self.bob.transfer.audit_log[-1].decision, "allowed")
        self.assertEqual(self.bob.transfer.audit_log[-1].tool_name, "system_info")

    def test_run_shell_is_disabled_by_default(self) -> None:
        bob = self._peer_bob()
        out = self.alice.transfer.call_remote_tool(bob, "run_shell", {"command": "echo ok"}, timeout=5)
        self.assertIn("error", out, out)
        # Either a PermissionError ("disabled") or general error — match defensively.
        self.assertTrue("SAFEDROP_ALLOW_SHELL" in out["error"] or "disabled" in out["error"].lower(), out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
