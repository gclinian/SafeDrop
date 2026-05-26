"""Tests for v1.4 safedrop_agent.

We don't talk to the real Anthropic API here — we monkey-patch the
``anthropic.Anthropic`` class with a stub so the agent code path is
exercised but no network call happens.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safedrop.config as _config  # noqa: E402

_DL = Path(tempfile.mkdtemp(prefix="safedrop-agent-test-"))
_config.DOWNLOAD_DIR = _DL
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _DL

from safedrop.transfer import ClipboardPayload  # noqa: E402


def _wait_for(predicate, timeout=8.0, interval=0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _FakeAnthropic:
    """Stub for ``anthropic.Anthropic`` that records prompts and returns canned text."""

    instances: list["_FakeAnthropic"] = []

    def __init__(self, **kwargs) -> None:
        _FakeAnthropic.instances.append(self)
        self.calls: list[dict] = []
        self.canned_reply = "stubbed reply"

        class _Messages:
            def __init__(inner_self, parent: "_FakeAnthropic") -> None:
                inner_self.parent = parent

            def create(inner_self, **kwargs):
                inner_self.parent.calls.append(kwargs)
                block = SimpleNamespace(type="text", text=inner_self.parent.canned_reply)
                return SimpleNamespace(content=[block])

        self.messages = _Messages(self)


class AgentSlashCommandTest(unittest.TestCase):
    """Unit tests for the agent's slash-command handler. No network."""

    def setUp(self) -> None:
        # Inject the fake before importing the agent module — the agent
        # imports anthropic lazily in __init__, so we just need it
        # mocked at instantiation time.
        self.fake_module = SimpleNamespace(Anthropic=_FakeAnthropic)
        with mock.patch.dict(sys.modules, {"anthropic": self.fake_module}):
            from safedrop.agent import Agent, PeerConversation
        self.Agent = Agent
        self.PeerConversation = PeerConversation

    def _make_agent(self):
        with mock.patch.dict(sys.modules, {"anthropic": self.fake_module}):
            return self.Agent(api_key="sk-test-fake", name_suffix="SLASH")

    def test_help_lists_commands(self) -> None:
        agent = self._make_agent()
        try:
            conv = self.PeerConversation(peer_name="phone")
            out = agent._handle_slash(conv, "/help")
            self.assertIn("/reset", out)
            self.assertIn("/tools", out)
            self.assertIn("/whoami", out)
        finally:
            agent.stop()

    def test_reset_clears_history(self) -> None:
        agent = self._make_agent()
        try:
            conv = self.PeerConversation(peer_name="phone")
            conv.messages = [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ]
            out = agent._handle_slash(conv, "/reset")
            self.assertIn("cleared", out.lower())
            self.assertEqual(conv.messages, [])
        finally:
            agent.stop()

    def test_model_set_only_affects_this_conv(self) -> None:
        agent = self._make_agent()
        try:
            conv = self.PeerConversation(peer_name="phone")
            other = self.PeerConversation(peer_name="laptop")
            agent._handle_slash(conv, "/model claude-opus-4-7")
            self.assertEqual(conv.model_override, "claude-opus-4-7")
            self.assertIsNone(other.model_override)
        finally:
            agent.stop()


class AgentDispatchTest(unittest.TestCase):
    """The ClipboardPayload → LLM → send_clipboard reply chain. Mocked Anthropic."""

    def setUp(self) -> None:
        self.fake_module = SimpleNamespace(Anthropic=_FakeAnthropic)

    def test_user_message_goes_to_llm_then_reply(self) -> None:
        with mock.patch.dict(sys.modules, {"anthropic": self.fake_module}):
            from safedrop.agent import Agent
            agent = Agent(api_key="sk-test-fake", name_suffix="DISPATCH")
        try:
            # Replace _reply with a recorder so we don't need a real peer
            # — _reply normally calls find_peer + send_clipboard.
            replies: list[tuple[str, str]] = []
            agent._reply = lambda peer, msg: replies.append((peer, msg))  # type: ignore[method-assign]

            fake = _FakeAnthropic.instances[-1]
            fake.canned_reply = "the weather is fine"

            payload = ClipboardPayload(
                transfer_id="t1",
                peer_name="iPhone-test",
                content_type="text",
                content="what is the weather?",
            )
            agent._handle_clipboard(payload)

            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0][0], "iPhone-test")
            self.assertEqual(replies[0][1], "the weather is fine")
            # Conversation now has 2 turns logged.
            conv = agent.conversations["iPhone-test"]
            self.assertEqual(len(conv.messages), 2)
            # The Anthropic call carried the full user message.
            self.assertEqual(fake.calls[-1]["messages"][-1],
                             {"role": "user", "content": "what is the weather?"})
        finally:
            agent.stop()

    def test_slash_does_not_hit_llm(self) -> None:
        with mock.patch.dict(sys.modules, {"anthropic": self.fake_module}):
            from safedrop.agent import Agent
            agent = Agent(api_key="sk-test-fake", name_suffix="NOHTML")
        try:
            replies: list[tuple[str, str]] = []
            agent._reply = lambda peer, msg: replies.append((peer, msg))  # type: ignore[method-assign]
            fake = _FakeAnthropic.instances[-1]
            calls_before = len(fake.calls)

            payload = ClipboardPayload(
                transfer_id="t2",
                peer_name="iPhone-test",
                content_type="text",
                content="/whoami",
            )
            agent._handle_clipboard(payload)
            # /whoami got handled locally — no Anthropic call.
            self.assertEqual(len(fake.calls), calls_before)
            self.assertEqual(len(replies), 1)
            self.assertIn("safedrop-agent", replies[0][1])
        finally:
            agent.stop()

    def test_per_peer_conversation_isolation(self) -> None:
        with mock.patch.dict(sys.modules, {"anthropic": self.fake_module}):
            from safedrop.agent import Agent
            agent = Agent(api_key="sk-test-fake", name_suffix="ISO")
        try:
            agent._reply = lambda peer, msg: None  # type: ignore[method-assign]
            fake = _FakeAnthropic.instances[-1]
            fake.canned_reply = "ack"
            agent._handle_clipboard(ClipboardPayload(
                transfer_id="t3", peer_name="phone", content_type="text", content="hello"))
            agent._handle_clipboard(ClipboardPayload(
                transfer_id="t4", peer_name="laptop", content_type="text", content="hi"))
            self.assertIn("phone", agent.conversations)
            self.assertIn("laptop", agent.conversations)
            self.assertEqual(len(agent.conversations["phone"].messages), 2)
            self.assertEqual(len(agent.conversations["laptop"].messages), 2)
        finally:
            agent.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
