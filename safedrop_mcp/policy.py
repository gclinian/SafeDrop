"""Per-agent policy for ``safedrop-mcp``.

A policy controls *which* MCP tools an agent gets to see and call. Three
flavours, in order of precedence:

1. **--allow CSV** flag or ``SAFEDROP_MCP_ALLOWED_TOOLS`` env var — a
   comma-separated list of tool-name globs (``fnmatch``-style, so
   ``phone__*`` works).  Empty means "allow everything".
2. **--profile NAME** flag or ``SAFEDROP_MCP_PROFILE`` env var — loads
   ``~/.safedrop/mcp-profiles/<name>.json`` for a richer config:
   ``allowed_tools``, ``name_suffix``, ``audit_path``.
3. **--deny CSV** flag — globs that are *always* rejected, even if
   ``--allow`` is broader.

If neither flag nor env var is set the agent sees every tool (current
behavior, suitable for a developer's own machine).

Profiles live on disk so the same machine can run multiple
``safedrop-mcp`` instances side-by-side (one per agent, e.g. Claude Code
gets ``read-only`` while Cursor gets ``full-access``); each instance is
its own headless SafeDrop peer with its own dynamic TCP port, so they
don't conflict.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROFILE_DIR = Path.home() / ".safedrop" / "mcp-profiles"


@dataclass
class Policy:
    """Compiled per-instance policy. Resolution order matches the docstring."""

    allow_globs: tuple[str, ...] = ()
    deny_globs: tuple[str, ...] = ()
    name_suffix: str = "MCP"
    audit_path: str | None = None
    profile_name: str | None = None

    def allow(self, tool_name: str) -> bool:
        """``True`` iff ``tool_name`` is permitted by this policy."""
        for pat in self.deny_globs:
            if fnmatch.fnmatchcase(tool_name, pat):
                return False
        if not self.allow_globs:
            return True
        for pat in self.allow_globs:
            if fnmatch.fnmatchcase(tool_name, pat):
                return True
        return False

    def filter(self, tool_names: list[str]) -> list[str]:
        return [n for n in tool_names if self.allow(n)]


def _split_csv(s: str | None) -> tuple[str, ...]:
    if not s:
        return ()
    return tuple(p.strip() for p in s.split(",") if p.strip())


def load_profile(name: str, base_dir: Path = DEFAULT_PROFILE_DIR) -> dict:
    path = base_dir / f"{name}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve(
    *,
    allow_arg: str | None = None,
    deny_arg: str | None = None,
    profile_arg: str | None = None,
    env: dict | None = None,
    base_dir: Path = DEFAULT_PROFILE_DIR,
) -> Policy:
    """Combine CLI args + env var + profile file into a single Policy."""
    e = env if env is not None else os.environ
    allow_csv = allow_arg or e.get("SAFEDROP_MCP_ALLOWED_TOOLS") or ""
    deny_csv = deny_arg or e.get("SAFEDROP_MCP_DENIED_TOOLS") or ""
    profile_name = profile_arg or e.get("SAFEDROP_MCP_PROFILE") or None

    allow_globs = list(_split_csv(allow_csv))
    deny_globs = list(_split_csv(deny_csv))
    name_suffix = "MCP"
    audit_path: str | None = None

    if profile_name:
        prof = load_profile(profile_name, base_dir=base_dir)
        # Profile values are merged but never override an explicit flag.
        if not allow_globs and isinstance(prof.get("allowed_tools"), list):
            allow_globs.extend(str(x) for x in prof["allowed_tools"])
        if not deny_globs and isinstance(prof.get("denied_tools"), list):
            deny_globs.extend(str(x) for x in prof["denied_tools"])
        if isinstance(prof.get("name_suffix"), str):
            name_suffix = prof["name_suffix"]
        if isinstance(prof.get("audit_path"), str):
            audit_path = prof["audit_path"]

    return Policy(
        allow_globs=tuple(allow_globs),
        deny_globs=tuple(deny_globs),
        name_suffix=name_suffix,
        audit_path=audit_path,
        profile_name=profile_name,
    )


def write_profile(name: str, data: dict, base_dir: Path = DEFAULT_PROFILE_DIR) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_profiles(base_dir: Path = DEFAULT_PROFILE_DIR) -> list[str]:
    if not base_dir.exists():
        return []
    return sorted(p.stem for p in base_dir.glob("*.json"))
