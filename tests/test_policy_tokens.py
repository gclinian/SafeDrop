"""Unit tests for the per-agent policy + capability-token store."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safedrop_mcp.policy import Policy, resolve, write_profile, list_profiles
from safedrop_mcp.tokens import TokenStore


class PolicyTest(unittest.TestCase):
    def test_empty_allows_all(self) -> None:
        p = Policy()
        self.assertTrue(p.allow("list_devices"))
        self.assertTrue(p.allow("phone_xxx__take_photo"))

    def test_allow_glob(self) -> None:
        p = Policy(allow_globs=("list_devices", "phone_*__*"))
        self.assertTrue(p.allow("list_devices"))
        self.assertTrue(p.allow("phone_abc__read_clipboard"))
        self.assertFalse(p.allow("send_file"))

    def test_deny_overrides(self) -> None:
        p = Policy(allow_globs=("*",), deny_globs=("*__run_shell", "send_file"))
        self.assertTrue(p.allow("list_devices"))
        self.assertFalse(p.allow("send_file"))
        self.assertFalse(p.allow("pi_xx__run_shell"))

    def test_filter(self) -> None:
        p = Policy(allow_globs=("list_devices", "send_*"))
        self.assertEqual(
            p.filter(["list_devices", "send_file", "send_text", "audit_log"]),
            ["list_devices", "send_file", "send_text"],
        )

    def test_resolve_env_and_args(self) -> None:
        env = {"SAFEDROP_MCP_ALLOWED_TOOLS": "a,b,c"}
        # explicit arg wins over env
        p = resolve(allow_arg="x,y", env=env)
        self.assertEqual(p.allow_globs, ("x", "y"))
        # env used when no arg
        p = resolve(env=env)
        self.assertEqual(p.allow_globs, ("a", "b", "c"))

    def test_resolve_profile_merges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            write_profile("agent", {
                "allowed_tools": ["list_devices", "phone__*"],
                "name_suffix": "Claude Code",
                "audit_path": "/tmp/audit.jsonl",
            }, base_dir=base)
            p = resolve(profile_arg="agent", env={}, base_dir=base)
            self.assertEqual(p.allow_globs, ("list_devices", "phone__*"))
            self.assertEqual(p.name_suffix, "Claude Code")
            self.assertEqual(p.audit_path, "/tmp/audit.jsonl")
            self.assertEqual(p.profile_name, "agent")
            # CLI flag takes priority over profile
            p2 = resolve(allow_arg="only_this", profile_arg="agent", env={}, base_dir=base)
            self.assertEqual(p2.allow_globs, ("only_this",))


class TokenStoreTest(unittest.TestCase):
    def test_mint_validate_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tokens.json"
            store = TokenStore(path=path)
            t = store.mint(label="cloud", scope=["phone__*", "list_devices"])
            self.assertIsNotNone(store.validate(t.token))
            self.assertEqual(store.validate(t.token).label, "cloud")
            # Reload from disk → still valid
            store2 = TokenStore(path=path)
            self.assertIsNotNone(store2.validate(t.token))
            # Revoke removes it
            self.assertTrue(store2.revoke(t.token))
            self.assertIsNone(store2.validate(t.token))

    def test_expired(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tokens.json"
            store = TokenStore(path=path)
            t = store.mint(label="short", scope=[], ttl_seconds=0.05)
            time.sleep(0.1)
            self.assertIsNone(store.validate(t.token))

    def test_to_policy(self) -> None:
        store = TokenStore(path=None)
        t = store.mint(label="x", scope=["a", "b"])
        pol = t.to_policy()
        self.assertEqual(pol.allow_globs, ("a", "b"))
        self.assertTrue(pol.allow("a"))
        self.assertFalse(pol.allow("c"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
