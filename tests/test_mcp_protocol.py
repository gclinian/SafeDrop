"""End-to-end MCP wire test.

Spawns a real ``safedrop-mcp`` subprocess, drives it over the real MCP
stdio JSON-RPC protocol with the official client SDK, and verifies that:

  * The four tools are advertised with the expected names + schemas.
  * list_devices returns a JSON array (peer-discovery convergence is
    racy on a CI box so we only check the shape).
  * send_text against an in-process receiver actually delivers.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue as _queue
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safedrop.config as _config  # noqa: E402
_DL = Path(tempfile.mkdtemp(prefix="safedrop-mcp-proto-"))
_config.DOWNLOAD_DIR = _DL
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _DL

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from safedrop.headless import HeadlessSafeDrop  # noqa: E402


SAFEDROP_MCP = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "safedrop-mcp")


def _wait_for(predicate, timeout=12.0, interval=0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


async def _drive(receiver_name_substr: str) -> dict:
    """Connect to safedrop-mcp via stdio, exercise tools, return results."""
    server = StdioServerParameters(command=SAFEDROP_MCP, args=[], env=dict(os.environ))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            static_tool_names = sorted(t.name for t in tools.tools)

            # Give discovery a moment to converge so the receiver's tools
            # appear as namespaced entries in the next list_tools.
            await asyncio.sleep(4.0)

            tools_after = await session.list_tools()
            all_names = sorted(t.name for t in tools_after.tools)
            namespaced = [n for n in all_names if "__" in n]

            ls = await session.call_tool("list_devices", {})
            ls_text = ls.content[0].text  # type: ignore[attr-defined]
            peers = json.loads(ls_text)
            peer_matches = [p for p in peers if receiver_name_substr in p["name"]]

            send = await session.call_tool(
                "send_text",
                {
                    "device": receiver_name_substr,
                    "content": "drop via MCP 🚀",
                    "content_type": "text",
                    "timeout_seconds": 10,
                },
            )
            send_text = send.content[0].text  # type: ignore[attr-defined]
            send_result = json.loads(send_text)

            # Call a namespaced remote tool: <recv_slug>__system_info.
            namespaced_result = None
            recv_slug_match = None
            if peer_matches and namespaced:
                recv_slug_match = peer_matches[0]["slug"]
                target = f"{recv_slug_match}__system_info"
                if target in all_names:
                    res = await session.call_tool(target, {})
                    namespaced_result = json.loads(res.content[0].text)  # type: ignore[attr-defined]

            return {
                "static_tool_names": static_tool_names,
                "all_tool_names": all_names,
                "namespaced": namespaced,
                "peers": peers,
                "peer_matches": peer_matches,
                "send_result": send_result,
                "recv_slug": recv_slug_match,
                "namespaced_result": namespaced_result,
            }


class MCPProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Path(SAFEDROP_MCP).is_file():
            raise unittest.SkipTest(f"missing {SAFEDROP_MCP} — run `pip install -e .` first")

    def test_full_protocol_handshake_and_call(self) -> None:
        # In-process auto-accepting receiver (peer of the MCP subprocess).
        receiver = HeadlessSafeDrop(name_suffix="RECV")
        receiver.start()
        try:
            out = asyncio.run(_drive(", RECV)"))

            STATIC = {
                "audit_log",
                "call_remote_tool",
                "list_devices",
                "list_remote_tools",
                "send_file",
                "send_text",
                "wait_for_drop",
            }
            self.assertTrue(STATIC.issubset(set(out["static_tool_names"])),
                            f"static tools missing: {STATIC - set(out['static_tool_names'])}")

            self.assertTrue(out["peer_matches"], f"RECV not in peers: {out['peers']}")
            self.assertIn("slug", out["peers"][0],
                          f"list_devices entry missing slug: {out['peers'][0]}")

            # After convergence, RECV's tools should be namespaced into all_tool_names.
            self.assertTrue(out["namespaced"],
                            f"no namespaced tools — all={out['all_tool_names']}")
            recv_namespaced = [n for n in out["namespaced"] if n.startswith(out["recv_slug"] + "__")]
            self.assertTrue(recv_namespaced,
                            f"no namespaced tools for RECV slug={out['recv_slug']}: {out['namespaced']}")
            # Defaults Bob registers — these should all show up:
            for expected in ("system_info", "read_clipboard", "write_clipboard"):
                self.assertIn(f"{out['recv_slug']}__{expected}", out["all_tool_names"])

            # The namespaced system_info call should have returned a structured result.
            self.assertIsNotNone(out["namespaced_result"])
            self.assertIn("result", out["namespaced_result"])
            self.assertIn("hostname", out["namespaced_result"]["result"])

            # send_text still works
            self.assertEqual(out["send_result"]["status"], "done", out["send_result"])
            self.assertEqual(out["send_result"]["kind"], "clipboard")
            try:
                got = receiver._drop_queue.get(timeout=5)
            except _queue.Empty:
                self.fail("receiver did not see the drop")
            self.assertEqual(got.clipboard_content, "drop via MCP 🚀")
        finally:
            receiver.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
