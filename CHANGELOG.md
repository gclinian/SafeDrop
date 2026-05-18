# Changelog

All notable changes to SafeDrop. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.3.0] — 2026-05-18

The "MCP fabric" release. SafeDrop is no longer just a LAN file/clipboard
tool; it's a scoped, auditable, cross-device Model Context Protocol
fabric that any AI agent can plug into.

### Added
- **iOS Phase 1 client** (`ios/`). Native Swift / SwiftUI, same X25519 +
  Fernet + JSON-over-TCP protocol as Python and Android. Default tools:
  `system_info`, `read_clipboard`, `write_clipboard`. Persistent trust
  store via UserDefaults. Allow/Deny dialog + audit list.
- **HTTP / Streamable-MCP transport** (`safedrop-mcp --http`). Bearer-
  token auth, per-token scope enforcement on every request. `/healthz`
  open for load balancers. Built on Anthropic's MCP SDK
  `StreamableHTTPSessionManager` + Starlette.
- **Capability tokens** (`safedrop-mcp-tokens`). Mint scoped, time-
  bounded tokens persisted to `~/.safedrop/tokens.json` (0o600).
  Revocable instantly. CLI subcommands: `mint / list / revoke / prune`.
- **Per-agent policy** (`--allow`, `--deny`, `--profile`). Three knobs
  combinable; CLI > env > profile-file precedence. fnmatch-style globs
  (e.g. `phone_*__*`). Profile files at
  `~/.safedrop/mcp-profiles/<name>.json`.
- **MCP bridge** (`safedrop_mcp/bridge.py`). Spawns other MCP servers
  as stdio subprocesses, imports their tools as `bridge.<name>.<tool>`.
  Example config in `safedrop_mcp/bridges.example.json`. Turns SafeDrop
  into a fabric that surfaces filesystem / github / fetch / etc.
  servers to every paired peer.
- **`register_local_tool` MCP tool**. Agents can introduce new tools
  at runtime by providing an HTTP handler URL; SafeDrop forwards
  CALL_TOOL invocations to it. Optional bearer secret.
- **Trust management UIs** on Python tkinter and Android Compose.
  Review and revoke saved (peer, tool) decisions.
- **`MCP_AGENT_GUIDE.md`** — full agent-integration guide.
- **`REAL_DEVICE_TESTING.md`** — manual QA checklist for real hardware.
- **`LICENSE` / `CONTRIBUTING.md` / `CHANGELOG.md`** — open-source
  housekeeping.

### Changed
- `pyproject.toml` `[mcp]` extras now include `starlette`, `uvicorn`,
  `httpx` (needed for HTTP transport + dynamic tool dispatch).
- `safedrop-mcp` is now an argparse front-end with `--allow`,
  `--deny`, `--profile`, `--name-suffix`, `--bridges`, `--no-bridges`,
  `--http`.
- `requirements.txt` removed — `pyproject.toml` is the single source
  of truth for Python dependencies.

### Tests
- 38 / 38 green (24 pre-existing + 14 new): policy / tokens / HTTP
  e2e / register_local_tool / bridge.

## [1.2.0] — 2026-05-17

The "cross-device tools complete" release.

### Added
- **Android `take_photo`** (Phase 3). System camera Intent → returns
  JPEG bytes through SafeDrop. End-to-end verified on emulator.
- **MCP namespace flatten** — remote peer tools now appear directly as
  `<peer_slug>__<tool>` in the agent's tools list (no more two-step
  `call_remote_tool("peer", "tool")`).
- **Persistent trust + audit** on Python desktop (~/.safedrop/trust.json,
  ~/.safedrop/audit.jsonl).

## [1.1.0] — 2026-05-15

The "MCP server" release.

### Added
- **`safedrop-mcp` MCP server** (`safedrop_mcp/`). 4 tools:
  `list_devices`, `send_file`, `send_text`, `wait_for_drop`.
- **`safedrop` CLI** (`safedrop/cli.py`). Same actions as MCP, plus
  `tools`, `call`, `audit`, `ls`, `send-file`, `send-text`, `wait`.
- **Cross-device tool protocol** — `LIST_TOOLS`, `TOOLS_LIST`,
  `CALL_TOOL`, `CALL_TOOL_RESULT` added to the encrypted TCP layer.
  HELLO now advertises `capabilities`.
- **Default tools**: `system_info`, `read_clipboard`, `write_clipboard`,
  optional `run_shell` (off by default).
- **Android tools + Allow/Deny dialog** mirroring the Python side.

## [1.0.0] — 2026-05-13

Initial public release. LAN file + clipboard sharing.

### Added
- Python desktop GUI (tkinter) with UDP discovery and X25519 + Fernet
  encrypted TCP transfer.
- Android client (native Kotlin / Jetpack Compose) speaking the same
  on-the-wire protocol.
- `bench.py` — throughput benchmark with sha256 verification.
