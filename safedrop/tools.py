"""Cross-device tool registry.

Each SafeDrop peer can advertise a list of *tools* — named callables with
a JSON Schema for arguments — that other peers (typically an AI agent
running through ``safedrop-mcp``) can invoke remotely over the existing
encrypted TCP channel.

Wire types added to the protocol:

    { "type": "LIST_TOOLS",        "request_id": "..." }
    { "type": "TOOLS_LIST",        "request_id": "...",
      "tools": [ {name, description, inputSchema}, ... ] }
    { "type": "CALL_TOOL",         "request_id": "...",
      "name": "...", "arguments": {...} }
    { "type": "CALL_TOOL_RESULT",  "request_id": "...",
      "result": {...}  |  "error": "..." }

This module is transport-agnostic — it just owns the registry + handlers.
The framing / encryption / dispatch lives in ``transfer.py``.
"""

from __future__ import annotations

import inspect
import os
import platform
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable


# ----------------------------------------------------------------- types ----


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    """Sync function: receives a dict of arguments, returns a JSON-serialisable result."""

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolRegistry:
    """A collection of [ToolSpec]s the local peer is willing to execute."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ---- registration ------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form. Wraps ``fn(**args)`` as the handler."""
        schema = input_schema or {"type": "object", "properties": {}}

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            def handler(args: dict[str, Any]) -> Any:
                return fn(**args)
            self.register(ToolSpec(name=name, description=description, input_schema=schema, handler=handler))
            return fn
        return deco

    # ---- querying ----------------------------------------------------

    def list_manifests(self) -> list[dict[str, Any]]:
        return [t.manifest() for t in self._tools.values()]

    def has(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        spec = self._tools[name]
        # Filter argument keys against the schema so handlers can use **kwargs safely.
        accepted = set(spec.input_schema.get("properties", {}).keys())
        if accepted:
            args = {k: v for k, v in arguments.items() if k in accepted}
        else:
            args = dict(arguments)
        # Backward-compat: if the handler explicitly expects 0 args, drop them all.
        try:
            sig = inspect.signature(spec.handler)
            if len(sig.parameters) == 0:
                args = {}
        except (TypeError, ValueError):
            pass
        return spec.handler(args)


# ----------------------------------------------------- default tool impls ----


def _read_clipboard() -> dict[str, str]:
    try:
        import pyperclip  # local import — desktop only
        return {"content": pyperclip.paste() or "", "content_type": "text"}
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"clipboard unavailable: {exc}")


def _write_clipboard(content: str) -> dict[str, str]:
    try:
        import pyperclip
        pyperclip.copy(content)
        return {"status": "ok", "wrote_chars": len(content)}
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"clipboard unavailable: {exc}")


def _system_info() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": sys.version.split()[0],
    }


def _run_shell(command: str, timeout: int = 30) -> dict[str, Any]:
    """Optional, opt-in via SAFEDROP_ALLOW_SHELL=1."""
    if os.environ.get("SAFEDROP_ALLOW_SHELL") != "1":
        raise PermissionError(
            "run_shell is disabled. Set SAFEDROP_ALLOW_SHELL=1 in the peer's "
            "environment to enable it."
        )
    proc = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=timeout
    )
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-50_000:],
        "stderr": proc.stderr[-50_000:],
    }


def register_default_tools(registry: ToolRegistry) -> None:
    """Wire up the platform-agnostic tools every peer ships with."""
    registry.register(ToolSpec(
        name="system_info",
        description="Return basic info about this device: hostname, OS, machine, python.",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: _system_info(),
    ))
    registry.register(ToolSpec(
        name="read_clipboard",
        description=(
            "Read the local clipboard on this device. Returns {content, content_type}. "
            "Requires the device to be unlocked and (on iOS/Android) the SafeDrop app foregrounded."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: _read_clipboard(),
    ))
    registry.register(ToolSpec(
        name="write_clipboard",
        description="Set this device's clipboard to the given text.",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Text to put on the clipboard."},
            },
            "required": ["content"],
        },
        handler=lambda args: _write_clipboard(args["content"]),
    ))
    registry.register(ToolSpec(
        name="run_shell",
        description=(
            "Run a shell command on this device and return its output. Disabled by default. "
            "Enable on the peer by setting SAFEDROP_ALLOW_SHELL=1 in its environment."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "timeout": {"type": "integer", "default": 30, "description": "Max seconds."},
            },
            "required": ["command"],
        },
        handler=lambda args: _run_shell(args["command"], int(args.get("timeout", 30))),
    ))


def build_default_registry() -> ToolRegistry:
    r = ToolRegistry()
    register_default_tools(r)
    return r
