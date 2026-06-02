# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What SafeDrop is

A zero-config LAN file + clipboard sharing tool **plus** a Model Context
Protocol fabric. Three native implementations (Python desktop, Kotlin
Android, Swift iOS) speak byte-for-byte the same X25519 + Fernet +
JSON-over-TCP protocol. Authoritative user-facing docs:
[`README.md`](README.md), [`SPEC.md`](SPEC.md),
[`MCP_AGENT_GUIDE.md`](MCP_AGENT_GUIDE.md).

## The single most important rule: the wire protocol is the contract

`SPEC.md` is the source of truth for what goes on the wire. Any change
to it must land in **all three** implementations in the same change
set, and must come with a regression in `tests/test_*_interop.py`. The
interop tests are how we know Python/Kotlin/Swift agree byte-for-byte
on Fernet token format, X25519 + HKDF derivation, and frame layout —
breaking them is how a release goes silently wrong on one platform.

If you're adding a wire-level feature: update `SPEC.md` first, then
mirror in `safedrop/`, `android/app/src/main/java/com/safedrop/android/`,
and `ios/SafeDrop/` simultaneously.

## Architecture orientation

### `safedrop/` — Python core

`safedrop/headless.py` is the **shared service abstraction**: it owns
one `Identity` + `DiscoveryService` + `TransferManager` + (default)
`ToolRegistry` and is used by both the `safedrop` CLI and the
`safedrop-mcp` server. Tests import it directly to spin up in-process
peers. The GUI in `safedrop/gui.py` doesn't use `HeadlessSafeDrop`
because it builds its own pieces — keep the two flows in sync when
you add a backend feature.

`safedrop/transfer.py` is where the dispatcher splits on the first
encrypted frame's `type`: `REQUEST` (file/clipboard transfer) vs
`LIST_TOOLS` (return registry) vs `CALL_TOOL` (run tool through
trust + authorizer + audit). All three platforms have the same split.

`safedrop/trust.py` (TrustPolicy + AuditWriter) — per-(peer_device_id,
tool_name) decisions persisted to `~/.safedrop/trust.json`. Decisions
are `"allow"` / `"deny"` / `"ask"`. The dispatcher consults the policy
first; only `"ask"` falls through to the `on_tool_call` callback.
**Headless instances default to allow-all** because their
`on_tool_call` is `None`.

`safedrop/agent.py` (v1.4) — `safedrop-agent` console script. A
headless SafeDrop peer whose `on_clipboard` callback feeds inbound
text to Anthropic's Messages API and replies via `send_clipboard`.
Per-`peer_name` conversation isolation; slash commands handled locally
(no LLM round-trip). The `anthropic` import is lazy so the rest of the
package works without the `[agent]` extra installed.

`safedrop/handoff.py` (v1.6) — `HandoffStore` persistent KV store at
`~/.safedrop/handoff.json` (atomic write, 0o600 on POSIX, 1 MB cap).
Wrapped as peer + MCP tools (`handoff_save/load/list/delete`) via
`safedrop_mcp/handoff_tools.py`. The continuity primitive: save a
draft on one device, load on another over CALL_TOOL.

`safedrop/tailscale.py` (v1.7) — parses `tailscale status --json`
into `TailscalePeer` rows and emits SafeDrop manual-peer stubs. Used
by `safedrop tailscale list` CLI subcommand. Opt-in cross-LAN —
Tailscale handles WireGuard routing, SafeDrop's Fernet stays on top.

### `safedrop_mcp/` — MCP server (stdio + HTTP)

`safedrop_mcp/server.py` exposes four sources of tools in one combined
`tools/list` response, then dispatches `tools/call` across them:

| Source | Tool naming | Where it's set up |
| --- | --- | --- |
| Static local | transfer (`list_devices`, `send_file`, `send_text`, `wait_for_drop`), explicit-remote (`list_remote_tools`, `call_remote_tool`), audit + dynamic (`audit_log`, `register_local_tool`, `unregister_local_tool`, `list_local_tools`), agent-bus (`whoami`, `list_agents`, `send_message`, `recv_messages`), tokens (`tokens_list`, `tokens_mint`, `tokens_revoke`), handoff (`handoff_save/load/list/delete`), notifications (`show_notification`, `notifications_recent`) | `_static_tool_defs()` |
| Dynamic peer tools | `<peer_slug>__<tool>` | `_fetch_peer_tools()` polls `safedrop.tools` peers in parallel with a 20 s TTL cache |
| MCP bridge | `bridge.<name>.<tool>` | `safedrop_mcp/bridge.py` spawns other MCP servers (from `~/.safedrop/bridges.json`) as stdio subprocesses |
| Runtime-registered | whatever the agent names it | `register_local_tool` stores a `handler_url` and POSTs to it on each call |

Everything passes through `_active_policy().allow(name)` before being
listed or called. The active policy is either the global one resolved
at startup (`--allow`/`--deny`/`--profile`/env vars) or a per-request
token-bound one when the HTTP transport is in use.

`safedrop_mcp/http_server.py` wraps the same low-level `Server` in
Anthropic's `StreamableHTTPSessionManager` behind a Starlette
`BearerAuthMiddleware`. The middleware validates the token against
`TokenStore` and stashes the matched token in a `ContextVar` so
`_active_policy()` sees the right scope per request.

`safedrop_mcp/agent_identity.py` + `safedrop_mcp/agent_bus.py` (v1.5)
— the multi-agent mesh. `AgentIdentity` reads/writes
`~/.safedrop/agent_id.json` and survives MCP restarts. `AgentBus`
registers two SafeDrop peer tools (`agent_bus_whoami`, `agent_bus_recv`)
on the shared `ToolRegistry`; the four MCP tools (`whoami`,
`list_agents`, `send_message`, `recv_messages`) in `server.py` use
those peer tools over the existing encrypted CALL_TOOL channel. Inbox
is JSON Lines at `~/.safedrop/agent_bus/inbox.jsonl`.

`safedrop_mcp/token_tools.py` + `safedrop_mcp/handoff_tools.py` +
`safedrop_mcp/notification_tools.py` (v1.6) — same dual-surface pattern
as agent_bus: each module exposes a small set of handlers, registers
them as SafeDrop peer tools (so phones / other paired devices can
call them via CALL_TOOL), AND `server.py` dispatches the same handlers
for the MCP-side static tool surface. The token UI in `safedrop/gui.py`
talks to `TokenStore` directly (same store the HTTP transport reads
from).

`safedrop_mcp/rendezvous.py` (v1.7) — `safedrop-beacon` console script.
Tiny Starlette app: POST `/announce` registers `(agent_id, ip,
tcp_port, pubkey, capabilities, ttl)` in an in-memory `BeaconRegistry`;
GET `/peers` returns active entries; optional Bearer-token auth via
`--secret`. Discovery-only — no traffic relay. The default install
never talks to a beacon; users opt in by pointing at one.

### `android/` (Kotlin / Compose) and `ios/` (Swift / SwiftUI)

Both mirror the Python module split: `crypto/` (X25519, HKDF, Fernet —
re-implemented natively to match Python byte-for-byte), `net/` (frame
protocol, discovery, transfer manager with the same dispatcher), `data/`
or `Trust.swift` (per-platform trust persistence), `ui/` (Compose or
SwiftUI). The iOS code has one platform quirk worth knowing: blocking
POSIX socket syscalls run on a dedicated `DispatchQueue` (`ioQueue`),
not Swift's cooperative thread pool, because the cooperative pool
deadlocks under blocking I/O. This applies to **both** `TransferManager`
(TCP) **and** `Discovery` (UDP) — `Discovery` is a `final class` on its
own queue, not an `actor`, for exactly this reason (an `actor` running a
blocking `recvfrom` stalls its executor). Discovery also broadcasts to
loopback + per-interface subnet broadcast + `255.255.255.255`, never
just the global broadcast (which doesn't traverse most real Wi-Fi).

## Development commands

### Python core / CLI / MCP

```bash
# First-time setup (CLI + MCP work on any Python 3.10+; GUI needs tkinter — see README)
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'

# Full test suite (102 tests, takes ~15 s)
.venv/bin/python -m unittest discover -s tests

# Single test
.venv/bin/python tests/test_policy_tokens.py
.venv/bin/python tests/test_http_transport.py
.venv/bin/python -m unittest tests.test_trust.TrustPolicyIntegrationTest.test_trust_allow_short_circuits_authorizer

# Run the GUI desktop client
.venv/bin/python run.py

# Run the CLI
.venv/bin/safedrop ls
.venv/bin/safedrop send-text <peer> "hello"

# Run the MCP server (stdio for Claude Code / Cursor / Goose / Cline)
.venv/bin/safedrop-mcp
# With per-agent scope
.venv/bin/safedrop-mcp --allow 'list_devices,phone_*__*' --deny '*__run_shell'
# HTTP transport (needs a minted token to be reachable)
.venv/bin/safedrop-mcp-tokens mint --label cloud --scope 'list_devices' --ttl 86400
.venv/bin/safedrop-mcp --http 127.0.0.1:47899

# Run the headless LLM agent (v1.4) — pip install -e '.[agent]' first
ANTHROPIC_API_KEY=... .venv/bin/safedrop-agent --name-suffix agent

# Run the cross-LAN rendezvous beacon (v1.7) — discovery-only, no relay
.venv/bin/safedrop-beacon --bind 127.0.0.1:47900               # open (LAN-only)
.venv/bin/safedrop-beacon --bind 0.0.0.0:47900 --secret SECRET # auth required

# List Tailscale tailnet peers (v1.7) — requires `tailscale` CLI installed
.venv/bin/safedrop tailscale list
```

Several tests spawn `safedrop-mcp` as a subprocess (e.g.
`test_mcp_protocol.py`, `test_http_transport.py`, `test_dynamic_tools.py`).
They will skip with a clear error if the entry point isn't on disk —
make sure `pip install -e '.[mcp]'` has been run.

### Android

```bash
cd android
./gradlew assembleDebug          # debug APK in app/build/outputs/apk/debug/
./gradlew installDebug           # install on attached device/emulator
```

Requires JDK 17 + Android SDK platform 36 + build-tools 36.x. AGP 8.13,
Kotlin 2.1.20, Gradle 9.0. The gradle wrapper is checked in.

### iOS

```bash
cd ios
xcodegen generate                # regenerates SafeDrop.xcodeproj (gitignored)
xcodebuild -project SafeDrop.xcodeproj -scheme SafeDrop \
    -destination 'platform=iOS Simulator,name=iPhone 17' build
./scripts/build-ipa.sh           # builds Release-iphoneos and packages as unsigned .ipa
```

The `.xcodeproj` is **not** checked in — `xcodegen generate` builds it
from `ios/project.yml`. Edit the YAML, not the generated project.

## Release flow

Versioning lives in **one** place: the `version` field in
`pyproject.toml`. To ship `vX.Y.Z`:

1. Bump `pyproject.toml` `version` to `X.Y.Z`.
2. Add a `## [X.Y.Z] — YYYY-MM-DD` section to `CHANGELOG.md` (Keep-a-Changelog format).
3. Commit and push to `main`.

`.github/workflows/release.yml` then runs `scripts/release.sh` on a
`macos-14` runner; the script auto-skips if the tag already exists,
otherwise it runs the tests, builds the IPA + APK + Python wheel/sdist,
tags `vX.Y.Z`, and creates the GitHub Release with the matching
CHANGELOG section as release notes.

You can also run `./scripts/release.sh` (or `--dry-run` / `--force`)
locally; the path is the same.

## State the apps own on disk

These directories carry persistent state that survives across runs and
matter for tests:

| Path | What lives there | Owner |
| --- | --- | --- |
| `~/.safedrop/trust.json` | `TrustPolicy` decisions (atomic write) | desktop GUI |
| `~/.safedrop/audit.jsonl` | append-only audit log | desktop GUI |
| `~/.safedrop/tokens.json` | HTTP transport capability tokens (0o600 on POSIX) | `safedrop-mcp-tokens` |
| `~/.safedrop/mcp-profiles/<name>.json` | per-agent policy presets | `safedrop-mcp --profile` |
| `~/.safedrop/bridges.json` | other MCP servers to import as `bridge.<name>.<tool>` | `safedrop_mcp/bridge.py` |
| `~/.safedrop/agent_id.json` | persistent agent identity (0o600 on POSIX) | `safedrop_mcp/agent_identity.py` |
| `~/.safedrop/agent_bus/inbox.jsonl` | inbound agent-bus messages (JSON Lines) | `safedrop_mcp/agent_bus.py` |
| `~/.safedrop/handoff.json` | state-handoff KV (0o600 on POSIX, 1 MB cap) | `safedrop/handoff.py` |
| `~/Downloads/SafeDrop/` | inbound files | `TransferManager._choose_save_path` |

Tests sandbox these by mutating `safedrop.config.DOWNLOAD_DIR` and
passing explicit `Path` arguments to `TrustPolicy(store_path=...)` /
`TokenStore(path=...)` — follow the same pattern when you add tests
that touch persistent state, or you will trash the user's data.

## Conventions

- `pyproject.toml` is the single source of truth for Python dependencies. There is no `requirements.txt`.
- `.gitignore` covers `dist/`, `ios/dist/`, `ios/SafeDrop.xcodeproj/`, `.venv/`, `*.egg-info` — release artifacts go to GitHub Releases, not the repo.
- Headless mode (CLI + MCP) defaults to **allow-all** for inbound tool calls because there is no UI to render an Allow/Deny dialog. The GUI clients on each platform wire `on_tool_call` to their dialog and `trust_policy` to the persistent store.
