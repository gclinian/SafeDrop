"""End-to-end HTTP / Streamable-MCP transport test.

Boots ``safedrop-mcp --http 127.0.0.1:<dyn>`` in a subprocess (which
internally spins up uvicorn + Starlette + Streamable-HTTP), mints a
capability token with a narrow scope, then drives the server with the
official MCP HTTP client. Verifies:

  * unauthenticated requests get 401
  * a valid token reaches initialize → list_tools
  * the scope filter actually hides tools outside the allowlist
  * a denied tool name returns a structured error
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Sandbox the trust/audit/download dirs so we don't litter ~/.safedrop.
_TMP = Path(tempfile.mkdtemp(prefix="safedrop-http-"))
os.environ.setdefault("HOME", str(_TMP))  # not strictly used here but keeps things tidy
import safedrop.config as _config  # noqa: E402
_DL = _TMP / "downloads"
_DL.mkdir(parents=True, exist_ok=True)
_config.DOWNLOAD_DIR = _DL
import safedrop.transfer as _transfer  # noqa: E402
_transfer.DOWNLOAD_DIR = _DL

import httpx  # noqa: E402

from safedrop_mcp.tokens import TokenStore  # noqa: E402

SAFEDROP_MCP = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "safedrop-mcp")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class HTTPTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not Path(SAFEDROP_MCP).is_file():
            raise unittest.SkipTest(f"missing {SAFEDROP_MCP} — run `pip install -e .[mcp]` first")
        # The subprocess's TokenStore() reads $HOME/.safedrop/tokens.json
        # (since we override HOME below); mint there so it picks it up.
        cls.token_path = _TMP / ".safedrop" / "tokens.json"
        cls.token_path.parent.mkdir(parents=True, exist_ok=True)
        store = TokenStore(path=cls.token_path)
        cls.token = store.mint(label="test", scope=["list_devices", "audit_log"])

        cls.host = "127.0.0.1"
        cls.port = _free_port()

        env = dict(os.environ)
        env["HOME"] = str(_TMP)
        env["SAFEDROP_MCP_PROFILE"] = ""
        cls.proc = subprocess.Popen(
            [SAFEDROP_MCP, "--http", f"{cls.host}:{cls.port}", "--no-bridges"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not _wait_listening(cls.host, cls.port):
            cls.proc.terminate()
            out, err = cls.proc.communicate(timeout=3)
            raise AssertionError(
                "safedrop-mcp --http did not start listening\n"
                f"stdout: {out.decode(errors='replace')}\n"
                f"stderr: {err.decode(errors='replace')}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
        except Exception:
            cls.proc.kill()

    def test_healthz_works_unauthenticated(self) -> None:
        url = f"http://{self.host}:{self.port}/healthz"
        r = httpx.get(url, timeout=5)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["service"], "safedrop-mcp")

    def test_mcp_endpoint_rejects_no_token(self) -> None:
        url = f"http://{self.host}:{self.port}/mcp/"
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, timeout=5)
        self.assertIn(r.status_code, (401, 403, 405))

    def test_full_streamable_http_flow(self) -> None:
        # Drive the server via the official MCP client.
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def go():
            url = f"http://{self.host}:{self.port}/mcp/"
            headers = {"Authorization": f"Bearer {self.token.token}"}
            async with streamablehttp_client(url, headers=headers) as (read, write, _close):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = sorted(t.name for t in tools.tools)
                    # Scope was ["list_devices", "audit_log"] → only those two static
                    # tools should be visible. (Dynamic / namespaced ones also
                    # filtered by the same allowlist.)
                    self.assertEqual(names, ["audit_log", "list_devices"])

                    # Allowed call: list_devices returns a JSON array
                    r = await session.call_tool("list_devices", {})
                    payload = r.content[0].text  # type: ignore[attr-defined]
                    self.assertIsInstance(json.loads(payload), list)

                    # Denied call: send_text isn't in scope → structured error
                    r = await session.call_tool("send_text",
                                                {"device": "x", "content": "hi"})
                    txt = r.content[0].text  # type: ignore[attr-defined]
                    self.assertIn("blocked by policy", txt)

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main(verbosity=2)
