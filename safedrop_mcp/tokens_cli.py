"""CLI front-end for managing HTTP capability tokens.

    safedrop-mcp-tokens mint   --label cloud-agent --scope 'list_devices,send_text,phone__*' [--ttl 86400]
    safedrop-mcp-tokens list
    safedrop-mcp-tokens revoke <token>
    safedrop-mcp-tokens prune
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .tokens import TokenStore


def cmd_mint(args: argparse.Namespace) -> int:
    store = TokenStore()
    scope = [s.strip() for s in (args.scope or "").split(",") if s.strip()]
    t = store.mint(label=args.label, scope=scope,
                   ttl_seconds=(args.ttl if args.ttl > 0 else None))
    out = {
        "token": t.token,
        "label": t.label,
        "scope": list(t.scope),
        "expires_at": t.expires_at,
        "expires_in_seconds": (t.expires_at - time.time()) if t.expires_at else None,
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"Minted token for {t.label}")
        print(f"  scope:       {', '.join(t.scope) or '(unrestricted within profile)'}")
        if t.expires_at:
            print(f"  expires at:  {time.ctime(t.expires_at)}  ({int(t.expires_at - time.time())} s)")
        else:
            print("  expires at:  (no expiry)")
        print()
        print("Send this header on every MCP request:")
        print(f"  Authorization: Bearer {t.token}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = TokenStore()
    rows = store.snapshot()
    if args.json:
        print(json.dumps([{
            "token_preview": t.token[:8] + "…",
            "label": t.label,
            "scope": list(t.scope),
            "created_at": t.created_at,
            "expires_at": t.expires_at,
        } for t in rows], indent=2))
        return 0
    if not rows:
        print("(no tokens)")
        return 0
    print(f"{'TOKEN':<14} {'LABEL':<24} {'EXPIRES':<24} SCOPE")
    for t in rows:
        exp = "(none)" if t.expires_at is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(t.expires_at))
        print(f"{t.token[:8] + '…':<14} {t.label:<24} {exp:<24} {', '.join(t.scope) or '*'}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    store = TokenStore()
    # Accept either a full token or a unique prefix.
    candidates = [t.token for t in store.snapshot() if t.token.startswith(args.token)]
    if not candidates:
        print(f"error: no token matches prefix {args.token!r}", file=sys.stderr)
        return 2
    if len(candidates) > 1:
        print(f"error: prefix matches {len(candidates)} tokens; be more specific", file=sys.stderr)
        return 2
    store.revoke(candidates[0])
    print("revoked")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    n = TokenStore().prune_expired()
    print(f"pruned {n} expired token(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safedrop-mcp-tokens",
                                description="Manage HTTP capability tokens for safedrop-mcp")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mint", help="mint a new token")
    pm.add_argument("--label", required=True)
    pm.add_argument("--scope", default="", help="comma-separated glob list (e.g. 'list_devices,phone__*')")
    pm.add_argument("--ttl", type=float, default=0, help="seconds until expiry (0 = no expiry)")
    pm.set_defaults(func=cmd_mint)

    pl = sub.add_parser("list", help="list existing tokens (truncated)")
    pl.set_defaults(func=cmd_list)

    pr = sub.add_parser("revoke", help="revoke a token by prefix")
    pr.add_argument("token", help="token or unique prefix")
    pr.set_defaults(func=cmd_revoke)

    pp = sub.add_parser("prune", help="remove expired tokens")
    pp.set_defaults(func=cmd_prune)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
