"""Cross-device capability-token management.

The :func:`make_token_peer_tools` factory returns a list of :class:`ToolSpec`
entries that wrap :class:`safedrop_mcp.tokens.TokenStore` operations as
SafeDrop *peer tools* — so other paired devices (a phone, another laptop)
can list / mint / revoke this machine's tokens without using the CLI.

This is the v1.6 "Token UI without CLI" primitive. The desktop GUI calls
the same TokenStore directly; the mobile clients call these peer tools
over the encrypted CALL_TOOL channel.

Security
~~~~~~~~

- ``tokens_list`` redacts each token to its last 6 chars. Use
  ``tokens_mint`` if you need a fresh secret you can copy in full.
- ``tokens_mint`` returns the full token string ONCE, never again.
- ``tokens_revoke`` accepts either the full token *or* a 6-char suffix
  matching exactly one row from ``tokens_list``.

Anyone who can call SafeDrop tools on this machine is already paired
(and either the local user, or someone who explicitly Allowed them in
the trust dialog). Exposing token management to them deliberately
mirrors what they could do by running the CLI in your terminal.
"""

from __future__ import annotations

import time
from typing import Any

from safedrop.tools import ToolSpec

from .tokens import CapabilityToken, TokenStore


def _summarise(t: CapabilityToken) -> dict[str, Any]:
    return {
        "label": t.label,
        "token_suffix": t.token[-6:],
        "scope": list(t.scope),
        "created_at": t.created_at,
        "expires_at": t.expires_at,
        "is_expired": t.is_expired(),
    }


def _mint(store: TokenStore, args: dict[str, Any]) -> dict[str, Any]:
    label = str(args.get("label") or "").strip()
    if not label:
        return {"status": "error", "error": "label is required"}
    scope_raw = args.get("scope") or []
    if isinstance(scope_raw, str):
        scope = tuple(s.strip() for s in scope_raw.split(",") if s.strip())
    else:
        scope = tuple(str(s) for s in scope_raw)
    ttl = args.get("ttl_seconds")
    ttl_f = float(ttl) if (ttl is not None and ttl != "") else None
    minted = store.mint(label=label, scope=scope, ttl_seconds=ttl_f)
    return {
        "status": "minted",
        # *Full* token returned here, deliberately. The caller is
        # responsible for copying it now — there is no recover-later.
        "token": minted.token,
        "label": minted.label,
        "scope": list(minted.scope),
        "created_at": minted.created_at,
        "expires_at": minted.expires_at,
        "token_suffix": minted.token[-6:],
    }


def _revoke(store: TokenStore, args: dict[str, Any]) -> dict[str, Any]:
    token = str(args.get("token") or "").strip()
    if not token:
        return {"status": "error", "error": "token is required"}
    if store.revoke(token):
        return {"status": "revoked", "matched": "full_token"}
    # Try suffix match.
    matches = [t for t in store.snapshot() if t.token.endswith(token)]
    if len(matches) == 1:
        store.revoke(matches[0].token)
        return {"status": "revoked", "matched": "suffix", "label": matches[0].label}
    if len(matches) > 1:
        return {
            "status": "error",
            "error": f"ambiguous suffix {token!r}: matches {len(matches)} tokens",
            "labels": [m.label for m in matches],
        }
    return {"status": "not_found"}


def _list(store: TokenStore, _args: dict[str, Any]) -> dict[str, Any]:
    rows = [_summarise(t) for t in store.snapshot()]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"tokens": rows, "count": len(rows)}


def make_token_peer_tools(store: TokenStore) -> list[ToolSpec]:
    """Build the three peer-tool specs for this :class:`TokenStore`."""

    return [
        ToolSpec(
            name="tokens_list",
            description=(
                "List capability tokens persisted on this device "
                "(~/.safedrop/tokens.json). Tokens are redacted — only the "
                "label, scope, timestamps, and last 6 chars are returned. "
                "Use tokens_mint to get a fresh full secret."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: _list(store, args),
        ),
        ToolSpec(
            name="tokens_mint",
            description=(
                "Mint a new capability token. Returns the FULL token string "
                "ONCE — copy it immediately; it cannot be retrieved later. "
                "Required: label. Optional: scope (list of fnmatch globs, "
                "default empty = allow-all), ttl_seconds."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "label":       {"type": "string"},
                    "scope":       {"type": "array", "items": {"type": "string"}},
                    "ttl_seconds": {"type": "number"},
                },
                "required": ["label"],
            },
            handler=lambda args: _mint(store, args),
        ),
        ToolSpec(
            name="tokens_revoke",
            description=(
                "Revoke a token by its full string OR by 6-char suffix "
                "(matching exactly one row from tokens_list). Returns "
                "{status: 'revoked'|'not_found'|'error', ...}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "token": {"type": "string",
                              "description": "Full token, or 6-char suffix."},
                },
                "required": ["token"],
            },
            handler=lambda args: _revoke(store, args),
        ),
    ]
