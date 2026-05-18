"""HTTP / Streamable-HTTP transport for ``safedrop-mcp``.

Wraps the existing low-level MCP ``Server`` (the same one used by stdio)
inside Anthropic's ``StreamableHTTPSessionManager``, mounted under
``/mcp`` on a Starlette ASGI app served by uvicorn.

A Starlette ``BaseHTTPMiddleware`` enforces bearer-token auth: every
request must carry ``Authorization: Bearer <token>`` matching a row in
the :class:`safedrop_mcp.tokens.TokenStore`. The validated token's
scope is installed as the per-request :class:`safedrop_mcp.policy.Policy`
via a context-var so the existing ``handle_list_tools`` / ``handle_call_tool``
handlers in ``safedrop_mcp.server`` keep working unchanged.

This unlocks the "cloud agent / phone agent → on-LAN SafeDrop fabric"
deployment story when paired with Tailscale / Cloudflare Tunnel / SSH.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from .policy import Policy
from .tokens import CapabilityToken, TokenStore


# Active request token (used by the MCP handlers via the
# `current_token_policy()` helper, which `server.py`'s `_policy` global
# can be swapped to via `set_request_policy`).
_request_token: contextvars.ContextVar[CapabilityToken | None] = contextvars.ContextVar(
    "_request_token", default=None
)

logger = logging.getLogger("safedrop_mcp.http")


def current_request_policy() -> Policy | None:
    """Return the Policy associated with the in-flight HTTP request, if any.

    Used by handlers in ``safedrop_mcp.server`` to enforce per-token
    scopes on each individual request.
    """
    tok = _request_token.get()
    return tok.to_policy() if tok else None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Checks Authorization header against the [TokenStore], installs the
    bound token in a context-var so the MCP handlers can consult it."""

    def __init__(self, app, token_store: TokenStore) -> None:
        super().__init__(app)
        self.token_store = token_store

    async def dispatch(self, request: Request, call_next):
        # Healthz is unauthenticated so a load-balancer / Tailscale check works.
        if request.url.path == "/healthz":
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        raw = auth.split(" ", 1)[1].strip()
        tok = self.token_store.validate(raw)
        if tok is None:
            return JSONResponse({"error": "invalid or expired token"}, status_code=403)
        token_var = _request_token.set(tok)
        try:
            return await call_next(request)
        finally:
            _request_token.reset(token_var)


def build_app(mcp_server: Server, token_store: TokenStore) -> Starlette:
    """Build the Starlette ASGI app with bearer-token middleware + MCP mount."""
    manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=False,
        json_response=False,
    )

    async def mcp_asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "safedrop-mcp"})

    async def lifespan(app):
        async with manager.run():
            yield

    return Starlette(
        debug=False,
        routes=[
            Route("/healthz", healthz),
            Mount("/mcp", app=mcp_asgi_app),
        ],
        middleware=[Middleware(BearerAuthMiddleware, token_store=token_store)],
        lifespan=lifespan,
    )


async def serve_http(
    mcp_server: Server,
    host: str = "127.0.0.1",
    port: int = 47899,
    token_store: TokenStore | None = None,
) -> None:
    """Run the MCP server over Streamable-HTTP on ``host:port`` indefinitely.

    A :class:`TokenStore` is mandatory; if you pass ``None`` we load
    ``~/.safedrop/tokens.json`` (creating it if needed).
    """
    store = token_store or TokenStore()
    if not store.snapshot():
        logger.warning(
            "[safedrop-mcp] tokens.json is empty — no remote agent will be able "
            "to authenticate. Use `safedrop-mcp-tokens mint --scope ...` first."
        )
    app = build_app(mcp_server, store)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    await server.serve()
