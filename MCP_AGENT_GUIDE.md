# SafeDrop ↔ AI agents — integration guide

Everything from "spin up a local stdio MCP server for Claude Code" to
"expose the cross-device tool fabric to a cloud agent via HTTP with a
scoped capability token". Pick the path that matches your agent.

```
┌──────────────────────────────────────────────────────────────────────┐
│             ┌────────────────────────────────────────────────┐       │
│             │      safedrop-mcp  (per-instance config)       │       │
│             │  • policy: --allow / --deny / --profile        │       │
│             │  • transport: stdio  |  --http HOST:PORT       │       │
│             │  • bridges (other MCP servers → namespaced)    │       │
│             │  • dynamic register_local_tool (handler URL)   │       │
│             └─┬──────────────────────────────────────────────┘       │
│   stdio       │ http(s) + bearer token                                │
│   ┌───────────▼────────┐    ┌──────────────────────────────────┐     │
│   │ Claude Code        │    │ Cloud agent / remote Hermes      │     │
│   │ Cursor / Cline     │    │ Custom GPT / Anthropic skill     │     │
│   │ Goose / Continue   │    │ ChatGPT scheduled task           │     │
│   └────────────────────┘    └──────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────────┘
```

## 0. Tools you'll see

A SafeDrop MCP server (stdio or HTTP, doesn't matter) advertises the
following **static** tools to whoever connects, plus **dynamic** ones
(remote-peer tools + register_local_tool entries + bridged MCP tools)
that get spliced in at every ``tools/list`` request:

| Static tool | Phase 1 | What it does |
| --- | --- | --- |
| `list_devices` | ✓ | LAN-discovered peers + their slugs / capabilities |
| `send_file` | ✓ | push a file to a peer (receiver must Accept) |
| `send_text` | ✓ | push text/url/code |
| `wait_for_drop` | ✓ | block until someone drops something at us |
| `list_remote_tools` / `call_remote_tool` | ✓ | explicit access to remote tools |
| `audit_log` | ✓ | local audit log |
| `register_local_tool` / `unregister_local_tool` / `list_local_tools` | **NEW** | dynamic tool registration via HTTP callback |

| Dynamic tool name | Source | Example |
| --- | --- | --- |
| `<peer_slug>__<tool>` | other SafeDrop peer | `pi_a3f2b1__system_info` |
| `bridge.<name>.<tool>` | another MCP server you've bridged | `bridge.github.create_issue` |
| `<your_name>` | `register_local_tool` you added at runtime | `add`, `summarise`, `query_db` |

Any tool can be hidden/exposed per agent via the policy layer (§3).

---

## 1. Stdio path — Claude Code / Cursor / Goose / Cline

This is what every MCP-supporting agent expects out of the box.

```bash
.venv/bin/pip install -e '.[mcp]'

# Claude Code
claude mcp add safedrop -- /full/path/to/.venv/bin/safedrop-mcp

# Claude Desktop  ~/Library/Application Support/Claude/claude_desktop_config.json
{
  "mcpServers": {
    "safedrop": {
      "command": "/full/path/to/.venv/bin/safedrop-mcp"
    }
  }
}

# Cursor (settings.json)
{ "mcp": { "servers": [
    { "name": "safedrop",
      "command": "/full/path/to/.venv/bin/safedrop-mcp" } ] } }

# Goose  ~/.config/goose/profiles.yaml
default:
  provider: openai
  mcp:
    safedrop:
      command: /full/path/to/.venv/bin/safedrop-mcp

# Cline (VS Code extension settings)  cline.mcpServers
[{ "name": "safedrop", "command": "/full/path/to/.venv/bin/safedrop-mcp" }]
```

Confirm in the agent:

```
> list_devices
> list_remote_tools(device="phone")
> phone_xxxxx__read_clipboard   # namespaced
```

## 2. Bash path — any agent that can run a subprocess

If the agent supports `bash` / shell-tools (basically every coding
agent — OpenHands, Aider, SWE-agent, custom LangChain agents, Hermes
inside Ollama with a function-calling wrapper…), just teach it the CLI:

```bash
safedrop ls --json
safedrop tools <device> --json
safedrop call <device> <tool> --args '{"content":"hi"}' --json
safedrop send-text <device> "https://…" --type url
```

For Hermes specifically — Hermes-2-Pro / OpenHermes-2.5 with Ollama's
[function calling](https://ollama.com/blog/tool-support) — define a
tool that wraps `subprocess.run(["safedrop", "call", ...])` and pass
its OpenAPI schema in the Modelfile or your inference loop.

## 3. Per-agent policy (allow / deny / profile)

Multiple agents on the same machine? Give each one its own narrow
surface. Three knobs, in precedence order (CLI > env > profile):

```bash
# Direct allowlist (only let this Claude Code instance see read-only tools)
safedrop-mcp --allow "list_devices,audit_log,phone_*__read_clipboard"

# Or via env
SAFEDROP_MCP_ALLOWED_TOOLS='*' \
SAFEDROP_MCP_DENIED_TOOLS='*__run_shell' \
safedrop-mcp

# Or a saved profile (lives at ~/.safedrop/mcp-profiles/<name>.json)
echo '{
  "allowed_tools": ["list_devices", "send_text", "phone_*__*"],
  "denied_tools": ["*__run_shell"],
  "name_suffix": "Claude Code (read-only)"
}' > ~/.safedrop/mcp-profiles/claude-readonly.json

safedrop-mcp --profile claude-readonly
```

Globs are `fnmatch`-style — `phone_*__*` matches every tool on any
phone peer.

Each `safedrop-mcp` invocation is its own headless SafeDrop peer with a
dynamic TCP port, so you can run multiple side-by-side (Claude Code +
Cursor + a sandboxed one for an automation script) on the same
machine without conflict — they all see each other and the same set of
remote peers, but each enforces its own policy.

## 4. HTTP / Streamable-MCP transport — cloud agents

Stdio means "agent must be a child process on this machine". For a
**remote** agent (ChatGPT custom action, Anthropic skill via HTTPS, a
Hermes server running on another host) use the HTTP transport.

```bash
# Mint a scoped, time-bounded capability token
safedrop-mcp-tokens mint --label "cloud-agent" --ttl 86400 \
    --scope "list_devices,send_text,phone_*__read_clipboard"
# → prints the bearer token; save it.

# Start the HTTP MCP server (use 0.0.0.0 if reaching from another host;
# bind to localhost + Tailscale / Cloudflare Tunnel for safety)
safedrop-mcp --http 127.0.0.1:47899 --profile cloud
```

The agent connects to `http://<host>:47899/mcp/` with header
`Authorization: Bearer <token>`. Health check at `/healthz` is open.

Token scope is enforced **on every individual request** — list_tools
hides anything outside scope, call_tool returns `{"error":"… blocked
by policy"}` for denied names. Combine with `--profile` to layer
profile-level deny rules under a per-token allow list.

For OAuth-style flow (mint token → embed in `.well-known/mcp.json`),
expose `/healthz` publicly and gate `/mcp/*` behind your gateway's auth
proxy that re-injects the bearer token.

## 5. Bridging other MCP servers — make SafeDrop a fabric

Write a `~/.safedrop/bridges.json` listing other MCP servers to import:

```json
{
  "bridges": [
    {
      "name": "fs",
      "command": "uvx",
      "args": ["mcp-server-filesystem", "/Users/me/Documents"]
    },
    {
      "name": "github",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_…" }
    }
  ]
}
```

Each tool from those servers appears in safedrop-mcp's `tools/list` as
`bridge.fs.read_file`, `bridge.github.create_issue`, etc. — **and they
get the same namespace treatment when accessed via a peer**.

That means: an iPad agent talking to your home Mac's SafeDrop can call
`bridge.github.create_issue` even though there's no MCP server on the
iPad — the iPad's request hops over SafeDrop's encrypted TCP channel to
the Mac, the Mac forwards to the bridged github server, and the iPad
gets the result. **SafeDrop is now a cross-device MCP fabric.**

Set `--no-bridges` to disable bridging for a particular `safedrop-mcp`
instance (useful for sandboxed agent configs).

## 6. Dynamic tool registration — make your agent extensible

`register_local_tool` lets the agent add new callable tools at runtime
without restarting safedrop-mcp. The handler is any HTTP endpoint
(typically a tiny FastAPI/Flask app running alongside the agent):

```python
# my_handler.py — agent-side
from fastapi import FastAPI, Request
app = FastAPI()

@app.post("/tools/summarise")
async def summarise(req: Request):
    payload = await req.json()
    content = payload["arguments"].get("content", "")
    return {"result": {"summary": content[:200] + "..."}}

# then `uvicorn my_handler:app --port 5678`
```

Agent calls (via MCP):

```
register_local_tool({
  "name": "summarise",
  "description": "Return a 200-char preview",
  "input_schema": {"type":"object","properties":{"content":{"type":"string"}},"required":["content"]},
  "handler_url": "http://127.0.0.1:5678/tools/summarise",
  "secret": "shared-token-if-you-want-auth"
})
```

That `summarise` tool now shows up in `tools/list` for *this*
safedrop-mcp instance — and other SafeDrop peers see it as
`<my_slug>__summarise` and can call it.

Combined with §5, you get: **the agent introduces a tool → other devices
on the LAN can call it → cloud agents over the HTTP transport can call
it too**.

## 7. Putting it all together — a "secure shared workspace"

```
   ┌────────────────┐  Tailscale  ┌─────────────────────────┐
   │  Hermes server │ ─────────── │  safedrop-mcp --http     │ ←─ token "writer"
   │  (cloud)       │             │      (your Mac)          │     scope = …
   └────────────────┘             │  bridges: fs, github     │
                                   │  trusted: phone, ipad   │
                                   └─┬─────────┬───────────┬─┘
                                     │ SafeDrop encrypted   │
                       ┌─────────────▼───┐ ┌────▼────────┐ ┌▼──────┐
                       │ iPhone (camera) │ │ Raspberry Pi │ │ Mac 2 │
                       │ take_photo      │ │ run_shell    │ │ ...   │
                       └─────────────────┘ └──────────────┘ └───────┘
```

Hermes:

```
list_devices
  → [phone, pi, mac2, ...]
bridge.github.create_issue({...})         # bridged from your Mac
phone_xxxxx__take_photo({})               # peer tool over SafeDrop
register_local_tool({ name: "summarise", handler_url: ... })
phone_xxxxx__read_clipboard({})
```

All authenticated, all scoped, all logged in
`~/.safedrop/audit.jsonl`. No data leaves your LAN unless Hermes itself
needs to round-trip (and your token can be revoked instantly with
`safedrop-mcp-tokens revoke`).

## 8. CLI quick reference

| Command | Purpose |
| --- | --- |
| `safedrop-mcp` | stdio MCP server (default) |
| `safedrop-mcp --http HOST:PORT` | HTTP Streamable-MCP server |
| `safedrop-mcp --profile NAME` | load `~/.safedrop/mcp-profiles/NAME.json` |
| `safedrop-mcp --allow CSV --deny CSV` | inline allowlist / denylist |
| `safedrop-mcp --bridges PATH --no-bridges` | bridge config / off-switch |
| `safedrop-mcp-tokens mint --label … --scope … [--ttl …]` | mint capability token |
| `safedrop-mcp-tokens list / revoke / prune` | manage tokens |
| `safedrop ls / send-file / send-text / call / tools / wait` | CLI for bash-tool agents |

See [README.md §8](README.md) for the basic stdio setup, [SPEC.md §15–16](SPEC.md) for protocol details, and `tests/test_*.py` for executable examples of every flow above.
