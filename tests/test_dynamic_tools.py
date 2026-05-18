"""Tests for `register_local_tool` (Phase X.3) and the MCP bridge (Phase X.4).

For `register_local_tool` we stand up a tiny aiohttp-style ASGI handler
that responds to POSTs from the safedrop-mcp dispatcher, register it as
a SafeDrop tool, then invoke it via the MCP wire.

For the bridge we spawn a minimal `stdio` MCP server (written inline)
that exposes one tool, point bridges.json at it, and verify the tool
shows up as `bridge.<name>.<tool>` in safedrop-mcp's tool list and that
calling it forwards correctly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safedrop.config as _config  # noqa: E402
_TMP = Path(tempfile.mkdtemp(prefix="safedrop-dyn-"))
_config.DOWNLOAD_DIR = _TMP / "downloads"
_config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _config.DOWNLOAD_DIR


# ----------------------------------- tiny HTTP handler for dynamic tool ----


class _DynamicToolHandler(BaseHTTPRequestHandler):
    """Handles POSTs from safedrop-mcp's `register_local_tool` dispatch."""

    def log_message(self, *_args, **_kwargs) -> None:
        pass  # silence

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        args = payload.get("arguments", {})
        # The "tool" is: return a+b
        a = float(args.get("a", 0)); b = float(args.get("b", 0))
        resp = {"result": {"sum": a + b}}
        body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ------------------------- tiny stdio MCP server we'll bridge to ----------

# This is a script we write to disk; safedrop_mcp.bridge will spawn it as
# a subprocess MCP server.
_BRIDGE_SERVER_SRC = """
import asyncio
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
import mcp.types as types

server = Server('bridgee')

@server.list_tools()
async def lt():
    return [types.Tool(
        name='echo',
        description='echo back the message argument',
        inputSchema={'type':'object','properties':{'message':{'type':'string'}}},
    )]

@server.call_tool()
async def ct(name, args):
    return [types.TextContent(type='text', text=(args or {}).get('message','') )]

async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, InitializationOptions(
            server_name='bridgee', server_version='0',
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        ))

asyncio.run(main())
"""


SAFEDROP_MCP = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "safedrop-mcp")


class DynamicToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Path(SAFEDROP_MCP).is_file():
            raise unittest.SkipTest(f"missing {SAFEDROP_MCP}")
        # Start the dynamic tool HTTP handler
        cls.dyn_server = ThreadingHTTPServer(("127.0.0.1", 0), _DynamicToolHandler)
        cls.dyn_port = cls.dyn_server.server_address[1]
        cls.dyn_thread = threading.Thread(target=cls.dyn_server.serve_forever, daemon=True)
        cls.dyn_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.dyn_server.shutdown()

    def _drive(self, action):
        """Spawn safedrop-mcp via stdio, run an async `action(session)`."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def go():
            params = StdioServerParameters(command=SAFEDROP_MCP, args=["--no-bridges"],
                                            env=dict(os.environ))
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return await action(session)
        return asyncio.run(go())

    def test_register_call_unregister(self) -> None:
        url = f"http://127.0.0.1:{self.dyn_port}/"

        async def action(session):
            # 1. Register the dynamic tool
            r = await session.call_tool("register_local_tool", {
                "name": "add",
                "description": "Return a + b",
                "input_schema": {
                    "type": "object",
                    "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                    "required": ["a", "b"],
                },
                "handler_url": url,
            })
            reg = json.loads(r.content[0].text)
            assert reg["status"] == "registered", reg
            # 2. List tools — "add" should appear
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "add" in names, names
            # 3. Call "add" — dispatcher should POST to our handler
            r = await session.call_tool("add", {"a": 40, "b": 2})
            outcome = json.loads(r.content[0].text)
            assert outcome.get("result") == {"sum": 42}, outcome
            # 4. Unregister + verify gone
            await session.call_tool("unregister_local_tool", {"name": "add"})
            tools = await session.list_tools()
            assert "add" not in {t.name for t in tools.tools}

        self._drive(action)


class MCPBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Path(SAFEDROP_MCP).is_file():
            raise unittest.SkipTest(f"missing {SAFEDROP_MCP}")
        # Write the bridged server script.
        cls.bridge_dir = Path(tempfile.mkdtemp(prefix="safedrop-bridge-"))
        cls.bridge_script = cls.bridge_dir / "echo_server.py"
        cls.bridge_script.write_text(_BRIDGE_SERVER_SRC)
        cls.bridges_json = cls.bridge_dir / "bridges.json"
        cls.bridges_json.write_text(json.dumps({
            "bridges": [{
                "name": "echoer",
                "command": sys.executable,
                "args": [str(cls.bridge_script)],
            }]
        }))

    def test_bridge_lists_and_invokes(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def go():
            params = StdioServerParameters(
                command=SAFEDROP_MCP,
                args=["--bridges", str(self.bridges_json)],
                env=dict(os.environ),
            )
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    # Give the bridge a moment to spin up its subprocess and list_tools.
                    await asyncio.sleep(0.5)
                    tools = await session.list_tools()
                    names = sorted(t.name for t in tools.tools)
                    assert "bridge.echoer.echo" in names, f"missing bridge tool in {names}"
                    # Call the bridged tool — should round-trip through subprocess
                    r2 = await session.call_tool("bridge.echoer.echo", {"message": "hi"})
                    txt = r2.content[0].text
                    outcome = json.loads(txt)
                    assert outcome.get("result") == "hi", outcome

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
