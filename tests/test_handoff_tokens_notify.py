"""Tests for the v1.6 cross-device primitives:

- safedrop.handoff.HandoffStore (state handoff key-value)
- safedrop_mcp.handoff_tools peer-tool wrappers
- safedrop_mcp.token_tools peer-tool wrappers (over a fresh TokenStore)
- safedrop_mcp.notification_tools bus + show_notification handler

We exercise each as it would be called over the SafeDrop CALL_TOOL
channel (i.e. via the ToolRegistry), but in-process — no sockets.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safedrop.handoff import HandoffEntry, HandoffStore, MAX_CONTENT_LEN  # noqa: E402
from safedrop.tools import ToolRegistry  # noqa: E402
from safedrop_mcp import handoff_tools as _ht  # noqa: E402
from safedrop_mcp.notification_tools import (  # noqa: E402
    _handle as notify_handle,
    bus as notification_bus,
    register_notification_peer_tool,
)
from safedrop_mcp.token_tools import (  # noqa: E402
    _list as token_list,
    _mint as token_mint,
    _revoke as token_revoke,
    make_token_peer_tools,
)
from safedrop_mcp.tokens import TokenStore  # noqa: E402


class HandoffStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sd-handoff-"))
        self.store = HandoffStore(self.tmp / "handoff.json")

    def test_save_and_load(self) -> None:
        self.store.save("draft", "hello world", "text/plain", updated_by="laptop")
        e = self.store.load("draft")
        assert e is not None
        self.assertEqual(e.content, "hello world")
        self.assertEqual(e.updated_by, "laptop")

    def test_overwrites(self) -> None:
        self.store.save("k", "first")
        self.store.save("k", "second")
        e = self.store.load("k")
        assert e is not None
        self.assertEqual(e.content, "second")

    def test_list_orders_newest_first(self) -> None:
        self.store.save("a", "x")
        time.sleep(0.01)
        self.store.save("b", "y")
        keys = [e.key for e in self.store.list()]
        self.assertEqual(keys, ["b", "a"])

    def test_persistence_round_trip(self) -> None:
        self.store.save("k", "persisted")
        store2 = HandoffStore(self.tmp / "handoff.json")
        e = store2.load("k")
        assert e is not None
        self.assertEqual(e.content, "persisted")

    def test_delete(self) -> None:
        self.store.save("k", "x")
        self.assertTrue(self.store.delete("k"))
        self.assertIsNone(self.store.load("k"))
        self.assertFalse(self.store.delete("k"))

    def test_clear(self) -> None:
        self.store.save("a", "1")
        self.store.save("b", "2")
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual(self.store.list(), [])

    def test_rejects_oversized(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("big", "x" * (MAX_CONTENT_LEN + 1))

    def test_rejects_blank_key(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("   ", "x")

    def test_summary_truncates_preview(self) -> None:
        self.store.save("p", "a" * 500)
        rows = self.store.list()
        self.assertEqual(rows[0].summary()["length"], 500)
        self.assertEqual(len(rows[0].summary()["preview"]), 120)


class HandoffPeerToolsTest(unittest.TestCase):
    """Drives the same handlers a remote peer would over CALL_TOOL."""

    def setUp(self) -> None:
        # Force the module-level singleton onto a clean temp file.
        self.tmp = Path(tempfile.mkdtemp(prefix="sd-handoff-pt-"))
        _ht._store = HandoffStore(self.tmp / "handoff.json")
        self.reg = ToolRegistry()
        _ht.register_handoff_peer_tools(self.reg)

    def test_round_trip(self) -> None:
        save = self.reg.call("handoff_save", {"key": "k1", "content": "abc"})
        self.assertEqual(save["status"], "saved")
        loaded = self.reg.call("handoff_load", {"key": "k1"})
        self.assertEqual(loaded["status"], "loaded")
        self.assertEqual(loaded["content"], "abc")
        listed = self.reg.call("handoff_list", {})
        self.assertEqual(listed["entries"][0]["key"], "k1")
        deleted = self.reg.call("handoff_delete", {"key": "k1"})
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(self.reg.call("handoff_load", {"key": "k1"})["status"], "not_found")

    def test_save_requires_key(self) -> None:
        out = self.reg.call("handoff_save", {"key": "", "content": "x"})
        self.assertEqual(out["status"], "error")


class TokenPeerToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sd-tok-"))
        self.store = TokenStore(self.tmp / "tokens.json")
        self.reg = ToolRegistry()
        for spec in make_token_peer_tools(self.store):
            self.reg.register(spec)

    def test_mint_returns_full_token_once(self) -> None:
        out = self.reg.call("tokens_mint", {"label": "phone", "scope": ["list_devices"]})
        self.assertEqual(out["status"], "minted")
        self.assertTrue(out["token"])
        self.assertEqual(out["token_suffix"], out["token"][-6:])

    def test_list_redacts(self) -> None:
        m = self.reg.call("tokens_mint", {"label": "x"})
        listed = self.reg.call("tokens_list", {})
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["tokens"][0]["token_suffix"], m["token"][-6:])
        # Full token does NOT appear in the listing.
        for row in listed["tokens"]:
            self.assertNotIn("token", row)

    def test_revoke_by_full_token(self) -> None:
        m = self.reg.call("tokens_mint", {"label": "x"})
        out = self.reg.call("tokens_revoke", {"token": m["token"]})
        self.assertEqual(out["status"], "revoked")
        self.assertEqual(out["matched"], "full_token")
        self.assertEqual(self.reg.call("tokens_list", {})["count"], 0)

    def test_revoke_by_suffix(self) -> None:
        m = self.reg.call("tokens_mint", {"label": "x"})
        out = self.reg.call("tokens_revoke", {"token": m["token_suffix"]})
        self.assertEqual(out["status"], "revoked")
        self.assertEqual(out["matched"], "suffix")

    def test_mint_requires_label(self) -> None:
        out = self.reg.call("tokens_mint", {"label": ""})
        self.assertEqual(out["status"], "error")

    def test_revoke_not_found(self) -> None:
        out = self.reg.call("tokens_revoke", {"token": "no-such-token-12345"})
        self.assertEqual(out["status"], "not_found")

    def test_mint_with_ttl_expires(self) -> None:
        out = self.reg.call("tokens_mint", {"label": "x", "ttl_seconds": -1})
        # Already expired (ttl negative); list_active should not include it.
        listed = self.reg.call("tokens_list", {})
        # tokens_list returns ALL including expired, but is_expired flagged.
        self.assertEqual(listed["count"], 1)
        self.assertTrue(listed["tokens"][0]["is_expired"])


class NotificationToolTest(unittest.TestCase):
    def setUp(self) -> None:
        # Clear any leftover state from the module-level bus.
        notification_bus._ring.clear()
        notification_bus.on_notification = None

    def test_handler_pushes_and_returns_ok(self) -> None:
        out = notify_handle({"title": "Hello", "body": "world"})
        self.assertEqual(out["status"], "shown")
        rows = notification_bus.recent()
        self.assertEqual(rows[-1]["title"], "Hello")
        self.assertEqual(rows[-1]["body"], "world")
        self.assertEqual(rows[-1]["level"], "info")

    def test_handler_rejects_empty(self) -> None:
        out = notify_handle({})
        self.assertEqual(out["status"], "error")

    def test_callback_fires(self) -> None:
        captured: list[dict] = []
        notification_bus.on_notification = captured.append
        notify_handle({"title": "T", "body": "B", "level": "warn"})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["level"], "warn")

    def test_invalid_level_falls_back_to_info(self) -> None:
        notify_handle({"title": "T", "level": "PANIC"})
        rows = notification_bus.recent()
        self.assertEqual(rows[-1]["level"], "info")

    def test_register_via_registry(self) -> None:
        reg = ToolRegistry()
        register_notification_peer_tool(reg)
        out = reg.call("show_notification", {"title": "Z", "body": "x"})
        self.assertEqual(out["status"], "shown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
