"""Rendezvous beacon — cross-LAN peer discovery without traffic relay (v1.7).

A SafeDrop peer behind a NAT can still find another SafeDrop peer
behind another NAT if both sides post their current ``(agent_id,
public_ip, tcp_port, pubkey, capabilities)`` to a small shared HTTP
service. The beacon doesn't relay any application traffic — it's a
**discovery-only** registry. Once two peers know each other's address,
they fall back to the normal SafeDrop encrypted TCP path.

This is the "no-cloud-by-default, opt-in-relay-when-you-need-it" tier
of v1.7. The default install never talks to a beacon. Users who want
cross-LAN discovery either:

* run their own beacon (``safedrop-beacon --bind 0.0.0.0:47900
  --secret correct-horse-battery-staple``) on a small VPS, or
* point at a community-hosted one.

Wire format
~~~~~~~~~~~

``POST /announce``  body::

    {"agent_id": "agent-...",
     "label":    "macbook (agent)",
     "ip":       "203.0.113.42",        # public IP
     "tcp_port": 47891,
     "pubkey":   "<base64 X25519>",
     "capabilities": ["safedrop.tools"],
     "expires_in": 300}                 # seconds, optional

The beacon may override ``ip`` with the request's actual remote
address if you pass ``ip=""``.

``GET /peers``::

    [{"agent_id": "...", "label": "...", "ip": "...",
      "tcp_port": 47891, "pubkey": "...", "capabilities": [...],
      "expires_at": 1234567890.5}, ...]

``GET /healthz`` → ``ok``.

All endpoints other than ``/healthz`` require ``Authorization: Bearer
<secret>`` when the beacon is started with ``--secret``. (Without
``--secret`` the beacon is fully open — only do this on a local LAN.)

This is **not** a TURN relay, not a STUN-replacer, and does not solve
symmetric NAT — for that you still need Tailscale or WebRTC (see the
README roadmap).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Optional


@dataclass
class _Entry:
    agent_id: str
    label: str
    ip: str
    tcp_port: int
    pubkey: str
    capabilities: tuple[str, ...] = ()
    updated_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 300.0)

    def to_public(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "ip": self.ip,
            "tcp_port": self.tcp_port,
            "pubkey": self.pubkey,
            "capabilities": list(self.capabilities),
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }


class BeaconRegistry:
    """Thread-safe in-memory peer table with TTL eviction."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: dict[str, _Entry] = {}
        self._max_entries = 1000

    def announce(
        self,
        *,
        agent_id: str,
        label: str,
        ip: str,
        tcp_port: int,
        pubkey: str,
        capabilities: tuple[str, ...] = (),
        ttl_seconds: float = 300.0,
    ) -> _Entry:
        if not agent_id:
            raise ValueError("agent_id is required")
        if tcp_port <= 0:
            raise ValueError("tcp_port must be positive")
        if not pubkey:
            raise ValueError("pubkey is required")
        now = time.time()
        ttl = max(10.0, min(ttl_seconds, 3600.0))
        entry = _Entry(
            agent_id=agent_id,
            label=label,
            ip=ip,
            tcp_port=tcp_port,
            pubkey=pubkey,
            capabilities=tuple(capabilities),
            updated_at=now,
            expires_at=now + ttl,
        )
        with self._lock:
            self._entries[agent_id] = entry
            # Soft cap — evict oldest when we get too big.
            if len(self._entries) > self._max_entries:
                oldest = sorted(self._entries.items(),
                                key=lambda kv: kv[1].updated_at)[: -self._max_entries]
                for k, _ in oldest:
                    self._entries.pop(k, None)
        return entry

    def list_active(self) -> list[_Entry]:
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._entries.items() if v.expires_at < now]
            for k in stale:
                self._entries.pop(k, None)
            return list(self._entries.values())

    def evict(self, agent_id: str) -> bool:
        with self._lock:
            return self._entries.pop(agent_id, None) is not None


# --------------------------------------------------------------------------- ASGI app ---


def build_app(registry: BeaconRegistry, *, secret: Optional[str] = None):
    """Return a Starlette ASGI app for ``uvicorn`` to serve."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    class _BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if secret is None or request.url.path in ("/healthz", "/"):
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                return JSONResponse({"error": "missing bearer token"}, status_code=401)
            given = auth[7:].strip()
            # Constant-time compare — short secret is fine but we still avoid
            # a side-channel on the lookup.
            if not secrets.compare_digest(given, secret):
                return JSONResponse({"error": "invalid bearer token"}, status_code=403)
            return await call_next(request)

    async def index(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            "SafeDrop rendezvous beacon. POST /announce, GET /peers, GET /healthz.\n"
        )

    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def announce(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "expected object"}, status_code=400)
        # Fill in IP from the request if the caller passed "".
        ip = str(body.get("ip") or "").strip()
        if not ip and request.client is not None:
            ip = request.client.host
        try:
            caps_raw = body.get("capabilities") or []
            caps = tuple(str(c) for c in caps_raw) if isinstance(caps_raw, list) else ()
            entry = registry.announce(
                agent_id=str(body.get("agent_id") or "").strip(),
                label=str(body.get("label") or ""),
                ip=ip,
                tcp_port=int(body.get("tcp_port", 0) or 0),
                pubkey=str(body.get("pubkey") or "").strip(),
                capabilities=caps,
                ttl_seconds=float(body.get("expires_in", 300) or 300),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"status": "announced", "entry": entry.to_public()})

    async def peers(_request: Request) -> JSONResponse:
        return JSONResponse([e.to_public() for e in registry.list_active()])

    return Starlette(
        debug=False,
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Route("/announce", announce, methods=["POST"]),
            Route("/peers", peers, methods=["GET"]),
        ],
        middleware=[Middleware(_BearerAuth)],
    )


# --------------------------------------------------------------------------- console entry ---


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="safedrop-beacon",
        description="SafeDrop rendezvous beacon — cross-LAN peer discovery, no traffic relay.",
    )
    p.add_argument("--bind", default="127.0.0.1:47900",
                   help="host:port to bind (default 127.0.0.1:47900 — set 0.0.0.0:... to expose).")
    p.add_argument("--secret", default=None,
                   help="Bearer-token secret. If omitted, the beacon is open (only safe on LAN).")
    return p


def run(argv: Optional[list[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        host, _, port = args.bind.partition(":")
        port_i = int(port or "47900")
    except ValueError:
        raise SystemExit(f"invalid --bind {args.bind!r}; expected host:port")

    try:
        import uvicorn  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "safedrop-beacon requires the [mcp] extra (starlette + uvicorn). "
            "Run: pip install 'safedrop[mcp]'"
        ) from exc

    registry = BeaconRegistry()
    app = build_app(registry, secret=args.secret)
    if args.secret:
        print(f"safedrop-beacon listening on {host or '127.0.0.1'}:{port_i} (auth required)",
              flush=True)
    else:
        print(f"safedrop-beacon listening on {host or '127.0.0.1'}:{port_i} "
              f"(OPEN — no bearer secret set; LAN-only)", flush=True)
    uvicorn.run(app, host=host or "127.0.0.1", port=port_i, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    run()
