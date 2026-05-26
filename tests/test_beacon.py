"""Tests for safedrop_mcp.rendezvous — the cross-LAN discovery beacon.

We exercise both the registry (sync, in-process) and the Starlette ASGI
app via httpx's ASGITransport (no real socket needed).
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safedrop_mcp.rendezvous import BeaconRegistry, build_app  # noqa: E402


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = BeaconRegistry()

    def test_announce_and_list(self) -> None:
        self.reg.announce(
            agent_id="agent-001", label="laptop",
            ip="203.0.113.1", tcp_port=47891,
            pubkey="abc=",
        )
        rows = self.reg.list_active()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].agent_id, "agent-001")
        self.assertEqual(rows[0].ip, "203.0.113.1")

    def test_announce_overwrites_same_agent(self) -> None:
        self.reg.announce(agent_id="a", label="x", ip="1.2.3.4",
                          tcp_port=47891, pubkey="k")
        self.reg.announce(agent_id="a", label="y", ip="5.6.7.8",
                          tcp_port=47891, pubkey="k")
        rows = self.reg.list_active()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].label, "y")
        self.assertEqual(rows[0].ip, "5.6.7.8")

    def test_ttl_evicts(self) -> None:
        e = self.reg.announce(agent_id="a", label="x", ip="1.2.3.4",
                              tcp_port=47891, pubkey="k", ttl_seconds=10)
        # Force expiry by hand.
        e.expires_at = time.time() - 1
        self.assertEqual(self.reg.list_active(), [])

    def test_validates_required_fields(self) -> None:
        with self.assertRaises(ValueError):
            self.reg.announce(agent_id="", label="x", ip="1.2.3.4",
                              tcp_port=47891, pubkey="k")
        with self.assertRaises(ValueError):
            self.reg.announce(agent_id="a", label="x", ip="1.2.3.4",
                              tcp_port=0, pubkey="k")
        with self.assertRaises(ValueError):
            self.reg.announce(agent_id="a", label="x", ip="1.2.3.4",
                              tcp_port=47891, pubkey="")

    def test_ttl_clamped(self) -> None:
        e1 = self.reg.announce(agent_id="a", label="x", ip="1.2.3.4",
                               tcp_port=47891, pubkey="k", ttl_seconds=1)
        # Lower bound: 10 s.
        self.assertGreaterEqual(e1.expires_at - e1.updated_at, 10.0 - 0.5)
        e2 = self.reg.announce(agent_id="b", label="x", ip="1.2.3.4",
                               tcp_port=47891, pubkey="k", ttl_seconds=99999)
        # Upper bound: 3600 s.
        self.assertLessEqual(e2.expires_at - e2.updated_at, 3600.0 + 0.5)


class HTTPTest(unittest.IsolatedAsyncioTestCase):
    """Exercise the ASGI app via in-process httpx."""

    async def asyncSetUp(self) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError:
            self.skipTest("httpx not installed (install safedrop[mcp])")
        self.reg = BeaconRegistry()
        self.app = build_app(self.reg, secret=None)

    async def test_healthz_open(self) -> None:
        import httpx
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            r = await client.get("/healthz")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.text, "ok")

    async def test_announce_then_peers_round_trip(self) -> None:
        import httpx
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            payload = {
                "agent_id": "agent-xyz",
                "label": "macbook",
                "ip": "203.0.113.5",
                "tcp_port": 47891,
                "pubkey": "pk_base64==",
                "capabilities": ["safedrop.tools"],
                "expires_in": 60,
            }
            r = await client.post("/announce", json=payload)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], "announced")

            r = await client.get("/peers")
            self.assertEqual(r.status_code, 200)
            rows = r.json()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["agent_id"], "agent-xyz")
            self.assertEqual(rows[0]["capabilities"], ["safedrop.tools"])

    async def test_announce_rejects_missing_fields(self) -> None:
        import httpx
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            r = await client.post("/announce", json={"agent_id": "a"})
            self.assertEqual(r.status_code, 400)

    async def test_bearer_auth_required_when_secret_set(self) -> None:
        import httpx
        app_secured = build_app(self.reg, secret="hunter2")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_secured),
            base_url="http://test",
        ) as client:
            # No token
            r = await client.get("/peers")
            self.assertEqual(r.status_code, 401)
            # Wrong token
            r = await client.get("/peers", headers={"Authorization": "Bearer nope"})
            self.assertEqual(r.status_code, 403)
            # Right token
            r = await client.get("/peers", headers={"Authorization": "Bearer hunter2"})
            self.assertEqual(r.status_code, 200)
            # healthz stays open
            r = await client.get("/healthz")
            self.assertEqual(r.status_code, 200)

    async def test_announce_fills_ip_from_request_when_blank(self) -> None:
        import httpx
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        ) as client:
            r = await client.post("/announce", json={
                "agent_id": "agent-no-ip",
                "label": "x",
                "ip": "",          # let the beacon fill from request
                "tcp_port": 47891,
                "pubkey": "k",
            })
            self.assertEqual(r.status_code, 200, r.text)
            entry = r.json()["entry"]
            # httpx ASGITransport reports the test client as "127.0.0.1".
            self.assertTrue(entry["ip"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
