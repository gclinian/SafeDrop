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

### `safedrop_mcp/` — MCP server (stdio + HTTP)

`safedrop_mcp/server.py` exposes four sources of tools in one combined
`tools/list` response, then dispatches `tools/call` across them:

| Source | Tool naming | Where it's set up |
| --- | --- | --- |
| Static local | `list_devices`, `send_file`, `send_text`, `wait_for_drop`, `list_remote_tools`, `call_remote_tool`, `audit_log`, `register_local_tool`, `unregister_local_tool`, `list_local_tools` | `_static_tool_defs()` |
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

### `android/` (Kotlin / Compose) and `ios/` (Swift / SwiftUI)

Both mirror the Python module split: `crypto/` (X25519, HKDF, Fernet —
re-implemented natively to match Python byte-for-byte), `net/` (frame
protocol, discovery, transfer manager with the same dispatcher), `data/`
or `Trust.swift` (per-platform trust persistence), `ui/` (Compose or
SwiftUI). The iOS code has one platform quirk worth knowing: blocking
POSIX socket syscalls run on a dedicated `DispatchQueue` (`ioQueue`),
not Swift's cooperative thread pool, because the cooperative pool
deadlocks under blocking I/O.

## Development commands

### Python core / CLI / MCP

```bash
# First-time setup (CLI + MCP work on any Python 3.10+; GUI needs tkinter — see README)
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'

# Full test suite (38 tests, takes ~14 s)
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
| `~/Downloads/SafeDrop/` | inbound files | `TransferManager._choose_save_path` |

Tests sandbox these by mutating `safedrop.config.DOWNLOAD_DIR` and
passing explicit `Path` arguments to `TrustPolicy(store_path=...)` /
`TokenStore(path=...)` — follow the same pattern when you add tests
that touch persistent state, or you will trash the user's data.

## Conventions

- `pyproject.toml` is the single source of truth for Python dependencies. There is no `requirements.txt`.
- `.gitignore` covers `dist/`, `ios/dist/`, `ios/SafeDrop.xcodeproj/`, `.venv/`, `*.egg-info` — release artifacts go to GitHub Releases, not the repo.
- Headless mode (CLI + MCP) defaults to **allow-all** for inbound tool calls because there is no UI to render an Allow/Deny dialog. The GUI clients on each platform wire `on_tool_call` to their dialog and `trust_policy` to the persistent store.
