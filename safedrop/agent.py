"""SafeDrop headless agent — turn any machine into a SafeDrop-reachable AI bot.

The pattern:

    Phone → SafeDrop send-text "weather today?" → ``safedrop-agent``
                                                       │
                                            Anthropic Messages API
                                                       │
    Phone ← SafeDrop reply ←  "Sunny, 25°C..." ──────┘

The agent runs as a headless SafeDrop peer (its own X25519 identity, UDP
discovery, listener on a dynamic port) so it coexists fine with the
desktop GUI, the MCP server, and other peers. Per-sender conversation
isolation: each peer keeps its own message history.

**Slash commands** (handled before the LLM sees the message):

* ``/help``    — list slash commands
* ``/reset``   — clear conversation for this sender
* ``/tools``   — list SafeDrop peer tools visible on the LAN
* ``/whoami``  — show agent info
* ``/model``   — show or set model
* ``/history`` — number of turns in this conversation

**Requirements**

* The ``anthropic`` package — install with ``pip install safedrop[agent]``.
* An ``ANTHROPIC_API_KEY`` env var (or pass ``--api-key``).

**Limitation (v1.4)** — conversations are keyed by ``peer_name`` (i.e.
hostname + suffix). Two peers with the *same* hostname will share a
conversation. The v1.5 ``agent_bus`` provides device-id-stable
addressing via ``agent_id`` if that's a problem for your fleet.

Entry point: ``safedrop-agent`` (see pyproject.toml ``[project.scripts]``)
or ``python -m safedrop.agent``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .headless import HeadlessSafeDrop
from .transfer import ClipboardPayload


DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_SYSTEM_PROMPT = (
    "You are SafeDrop's on-device assistant. Replies are delivered back "
    "to the sender via SafeDrop (clipboard text), so keep them short and "
    "readable on a phone screen. Use Markdown sparingly. Prefer plain "
    "prose, lists when appropriate, and end with the next step if there "
    "is one."
)


# --------------------------------------------------------- conversation state ---


@dataclass
class PeerConversation:
    """In-memory chat history for one sender."""
    peer_name: str
    messages: list[dict] = field(default_factory=list)
    last_seen: float = field(default_factory=time.time)
    model_override: Optional[str] = None


# ---------------------------------------------------------------------- agent ---


class Agent:
    """Owns a HeadlessSafeDrop, per-peer conversations, and the Anthropic client."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: Optional[str] = None,
        name_suffix: str = "agent",
        echo: bool = False,
    ) -> None:
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "safedrop-agent requires the 'anthropic' package. "
                "Install with: pip install 'safedrop[agent]'"
            ) from exc

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "No Anthropic API key. Set ANTHROPIC_API_KEY in env or "
                "pass --api-key on the command line."
            )

        self.client = Anthropic(**kwargs)
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.echo = echo

        self.service = HeadlessSafeDrop(name_suffix=name_suffix)
        self.conversations: dict[str, PeerConversation] = {}
        self.service.transfer.on_clipboard = self._handle_clipboard

    # ---- lifecycle -------------------------------------------------

    def start(self) -> None:
        self.service.start()
        self._log(
            f"running as {self.service.device_name!r} "
            f"(model={self.model}, peers will see this device on the LAN)"
        )

    def stop(self) -> None:
        try:
            self.service.stop()
        except Exception:
            pass

    # ---- inbound text → LLM → reply --------------------------------

    def _handle_clipboard(self, payload: ClipboardPayload) -> None:
        text = (payload.content or "").strip()
        if not text:
            return
        self._log(f"<- {payload.peer_name}: {text[:120]!r}")

        conv = self.conversations.get(payload.peer_name)
        if conv is None:
            conv = PeerConversation(peer_name=payload.peer_name)
            self.conversations[payload.peer_name] = conv
        conv.last_seen = time.time()

        # Slash command handling — handled locally, no API call.
        if text.startswith("/"):
            reply = self._handle_slash(conv, text)
            self._reply(payload.peer_name, reply)
            return

        conv.messages.append({"role": "user", "content": text})
        reply_text = self._call_anthropic(conv)
        conv.messages.append({"role": "assistant", "content": reply_text})
        self._reply(payload.peer_name, reply_text)

    def _call_anthropic(self, conv: PeerConversation) -> str:
        model = conv.model_override or self.model
        try:
            # Pass a defensive copy: the SDK consumes the list, and we
            # mutate conv.messages right after this call to record the
            # assistant turn — sharing the same list confuses tests
            # (and any future SDK that does post-hoc inspection).
            response = self.client.messages.create(
                model=model,
                system=self.system_prompt,
                max_tokens=self.max_tokens,
                messages=list(conv.messages),
            )
            parts: list[str] = []
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    parts.append(getattr(block, "text", "") or "")
            text = "".join(parts).strip()
            return text or "(empty reply)"
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self._log(f"anthropic error: {err}", err=True)
            return f"WARNING: {err}"

    # ---- slash commands --------------------------------------------

    def _handle_slash(self, conv: PeerConversation, text: str) -> str:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower()
        rest = rest.strip()

        if cmd in ("/help", "/?"):
            return (
                "SafeDrop agent commands:\n"
                "  /reset           — clear this conversation\n"
                "  /tools           — list SafeDrop peer tools on the LAN\n"
                "  /whoami          — show agent info\n"
                "  /history         — turn count in this conversation\n"
                "  /model [<name>]  — show or set model for this peer\n"
                "  /system <text>   — show or set system prompt"
            )
        if cmd == "/reset":
            n = len(conv.messages)
            conv.messages.clear()
            conv.model_override = None
            return f"Conversation cleared ({n} turns dropped)."
        if cmd == "/whoami":
            return (
                f"safedrop-agent\n"
                f"device: {self.service.device_name}\n"
                f"model:  {conv.model_override or self.model}\n"
                f"turns:  {len(conv.messages)}"
            )
        if cmd == "/history":
            if not conv.messages:
                return "No conversation yet."
            return f"{len(conv.messages)} turns. Last 3:\n" + "\n".join(
                f"  [{m['role']}] {str(m['content'])[:80]}"
                for m in conv.messages[-3:]
            )
        if cmd == "/model":
            if not rest:
                eff = conv.model_override or self.model
                return f"Current model for this peer: {eff} (default: {self.model})"
            old = conv.model_override or self.model
            conv.model_override = rest
            return f"Model for this peer changed: {old} -> {rest}"
        if cmd == "/system":
            if not rest:
                return f"Current system prompt:\n{self.system_prompt}"
            return "System prompt is locked to the agent process. Restart with --system-prompt to change."
        if cmd == "/tools":
            return self._list_peer_tools()
        return f"Unknown command: {cmd}. Try /help."

    def _list_peer_tools(self) -> str:
        if self.service.discovery is None:
            return "Discovery not running."
        peers = self.service.discovery.snapshot()
        rows: list[str] = []
        for p in peers.values():
            if not p.has_capability("safedrop.tools"):
                continue
            try:
                tools = self.service.transfer.list_remote_tools(p, timeout=2.0)
            except Exception:
                tools = []
            if tools:
                names = ", ".join(t.get("name", "?") for t in tools)
                rows.append(f"- {p.name}: {names}")
        if not rows:
            return "No SafeDrop peer tools currently visible."
        return "Peer tools on the LAN:\n" + "\n".join(rows)

    # ---- outbound reply --------------------------------------------

    def _reply(self, peer_name: str, content: str) -> None:
        try:
            peer = self.service.find_peer(peer_name)
        except LookupError as exc:
            self._log(f"cannot find peer for reply: {exc}", err=True)
            return
        try:
            self.service.transfer.send_clipboard(peer, content, "text")
            self._log(f"-> {peer.name}: {content[:120]!r}")
        except Exception as exc:
            self._log(f"send_clipboard failed: {type(exc).__name__}: {exc}", err=True)

    # ---- logging ---------------------------------------------------

    def _log(self, msg: str, *, err: bool = False) -> None:
        stream = sys.stderr if err else sys.stdout
        print(f"[safedrop-agent] {msg}", file=stream, flush=True)
        if self.echo and not err:
            pass  # placeholder for future per-message hooks


# ---------------------------------------------------------------------- main ---


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="safedrop-agent",
        description="SafeDrop headless agent — turn this machine into an LLM bot reachable on the LAN.",
    )
    p.add_argument("--model", default=os.environ.get("SAFEDROP_AGENT_MODEL", DEFAULT_MODEL),
                   help=f"Anthropic model name (default: {DEFAULT_MODEL}). "
                        f"Also reads SAFEDROP_AGENT_MODEL env.")
    p.add_argument("--max-tokens", type=int, default=int(os.environ.get("SAFEDROP_AGENT_MAX_TOKENS",
                                                                          DEFAULT_MAX_TOKENS)),
                   help=f"Max tokens per reply (default: {DEFAULT_MAX_TOKENS}).")
    p.add_argument("--api-key", default=None,
                   help="Anthropic API key (default: ANTHROPIC_API_KEY env).")
    p.add_argument("--name-suffix", default="agent",
                   help="Suffix shown after this device's name on the LAN (default: 'agent').")
    p.add_argument("--system-prompt", default=None,
                   help="Override system prompt. Defaults to the built-in 'short reply' prompt.")
    return p


def run(argv: Optional[list[str]] = None) -> None:
    args = _build_parser().parse_args(argv)
    agent = Agent(
        model=args.model,
        system_prompt=args.system_prompt or DEFAULT_SYSTEM_PROMPT,
        max_tokens=args.max_tokens,
        api_key=args.api_key,
        name_suffix=args.name_suffix,
    )
    agent.start()
    try:
        # The HeadlessSafeDrop service runs in background threads. We just
        # sleep — Ctrl-C / SIGTERM / launchd-stop kills the process.
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()


if __name__ == "__main__":  # pragma: no cover
    run()
