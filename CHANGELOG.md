# Changelog

All notable changes to SafeDrop. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.6.2] — 2026-05-26

Bugfix — the desktop GUI **sender never displayed the pair code**,
breaking the visual man-in-the-middle check the protocol is designed
around. `SPEC.md` requires both sides to show the same 4-digit code;
the receiver's Accept dialog showed it, but the sender showed nothing,
so the user had nothing to compare against.

### Fixed
- **Sender-side pair-code verification** (`safedrop/gui.py`). When you
  start a transfer, a "Verify pair code" window now appears on the
  sender showing the 4-digit code, and stays up while waiting for the
  receiver to Accept — mirroring the receiver's dialog. It auto-
  dismisses once the transfer proceeds (`transferring`), completes, is
  rejected, or fails. The code is already carried on the wire and in
  `TransferState.pair_code`; this is purely the missing display.

### Tests
- New `test_sender_surfaces_pair_code_before_transfer` in
  `tests/test_e2e.py`: asserts the sender emits a non-empty 4-digit
  pair code while the transfer is still `pending` (before bytes flow),
  and that it matches the code the receiver independently derives.
  103 tests green.

### Known gaps (follow-ups, not in this release)
- **iOS** surfaces the pair code only *after* the transfer finishes
  (in a status line), not during the pre-Accept verification window.
- **Android** sender does not show the pair code at verification time.
  Both should mirror the desktop fix to fully honor SPEC §`pair_code`.

## [1.6.1] — 2026-05-26

Patch — make the desktop install path **actually one line**. No
protocol changes, no new dependencies.

### Added
- **`scripts/install.sh`** — one-key installer for macOS + Linux.
  Verifies Python 3.10+, asserts tkinter is importable (with a clear,
  platform-specific fix on failure), creates a hermetic venv at
  `~/.local/share/safedrop/venv`, installs `safedrop[mcp]` from PyPI
  (or `--from-release` against the latest GitHub Release wheel), and
  drops `safedrop-gui` / `safedrop` / `safedrop-mcp` /
  `safedrop-mcp-tokens` / `safedrop-agent` / `safedrop-beacon`
  launchers into `~/.local/bin`. Idempotent.
- **`scripts/install.ps1`** — PowerShell counterpart for Windows. Venv
  lives at `%LOCALAPPDATA%\SafeDrop\venv`; `.cmd` launchers under
  `%LOCALAPPDATA%\SafeDrop\bin`. Same flow, same error messages.

### Changed
- README "Quick start" now leads with the one-line install:
  ```
  curl -fsSL https://raw.githubusercontent.com/gclinian/SafeDrop/main/scripts/install.sh | bash
  ```
  (and the `iwr | iex` Windows equivalent). The previous
  `git clone` + venv + `pip install -e` flow is preserved under a new
  *Desktop — manual install* subsection for the security-conscious.
- `docs/landing.html` install snippet updated to match.

### Tests
- Smoke-verified `scripts/install.sh --from-release` against the
  published `v1.6.0` wheel: pulls the .whl from GitHub Releases,
  installs into a fresh venv, launchers fire correctly,
  `safedrop ls` runs without errors.

## [1.6.0] — 2026-05-26

The "actually finish v1.6 + introduce the project" release. Closes
out the cross-platform native rendering of v1.6 primitives and ships
the docs needed to onboard new users / contributors.

### Added — native notification rendering on all 3 platforms
- **Python tkinter banner** for inbound `show_notification`. `gui.py`
  subscribes to `safedrop.notifications.bus.on_notification` and pops
  a transient Toplevel (auto-dismiss after 8 s, color-coded by
  `info`/`warn`/`error`).
- **iOS UNUserNotificationCenter** — new `show_notification` handler
  in `ios/SafeDrop/Tools.swift` drops a system banner via
  `UNTimeIntervalNotificationTrigger`. App requests `.alert` + `.sound`
  authorization on launch.
- **Android NotificationManager** — new `show_notification` handler
  in `android/.../net/ToolRegistry.kt` posts via
  `NotificationCompat.Builder` with a lazy-created channel. Added
  `POST_NOTIFICATIONS` permission to AndroidManifest and runtime
  request via `ActivityResultContracts.RequestPermission` in
  MainActivity for API 33+.
- **Moved `notification_tools.py`** from `safedrop_mcp/` to
  `safedrop/notifications.py` so the desktop GUI can use it without
  the `[mcp]` extra installed. MCP server imports updated.

### Added — iOS token management UI
- New `ios/SafeDrop/TokenAdminView.swift` — SwiftUI screen that calls
  the cross-device peer tools (`tokens_list`, `tokens_mint`,
  `tokens_revoke`) on a selected SafeDrop peer (the desktop running
  `safedrop-mcp`). Mint form with label / scope / TTL fields;
  one-time secret reveal sheet on success.
- Toolbar **key** icon in `HomeView` opens `TokenAdminView` when the
  selected peer advertises `safedrop.tools`.

### Added — introducing the project
- **`docs/landing.html`** — single-page self-contained landing page
  (no external deps, dark theme, hero + features + architecture
  diagram + install snippet + cross-platform capability matrix +
  security model + roadmap). The thing you link from a README badge.
- **`docs/pitch.md`** — 30-second + 2-minute pitches for a HN post /
  Twitter thread.
- **`docs/demo-recipes.md`** — six tight live-demo recipes (each
  ~30-90 s), with exact commands + audience-facing punchline. Covers
  cross-platform AirDrop, agent-with-phone-camera, two-agent mesh,
  state handoff, mobile token mint, and Tailscale cross-LAN.

### Changed
- `pyproject.toml` → `1.6.0`.

## [1.5.0] — 2026-05-26

The "continuity primitives + opt-in cross-LAN" release. Closes out the
roadmap v1.6 and v1.7 items (sans WebRTC, which stays research-grade).

### Added — v1.6 continuity primitives

- **Capability-token cross-device management.** Three new tools
  (`tokens_list`, `tokens_mint`, `tokens_revoke`) wired *both* as
  SafeDrop peer tools (registered on the local `ToolRegistry`, so any
  paired device can call them over the encrypted CALL_TOOL channel)
  *and* as static MCP tools (so the local agent sees them too). Mint
  returns the full secret once; list redacts to the last 6 chars;
  revoke accepts the full token or that 6-char suffix.
- **Token management UI** in the Python desktop GUI
  (`safedrop/gui.py`): new "🔑 Tokens" button in the header opens a
  Toplevel with mint form + scope/TTL fields, redacted list with
  expiry display, prune-expired button, and a one-time "copy this
  now" reveal sheet when a new token is minted.
- **State handoff** (`safedrop/handoff.py`). Tiny persistent
  key-value store at `~/.safedrop/handoff.json` for "save the draft
  on my laptop, pick it up on my phone" continuity. Atomic writes,
  0o600 on POSIX, 1 MB content cap (anything bigger is a file
  transfer). Peer + MCP tools: `handoff_save`, `handoff_load`,
  `handoff_list`, `handoff_delete`.
- **Notification mirroring** (`safedrop_mcp/notification_tools.py`).
  `show_notification(title, body, level)` peer tool with an in-process
  `NotificationBus` ring buffer + optional callback the GUI installs.
  `notifications_recent(limit)` MCP tool reads back what's been
  pushed. Wire shape is platform-neutral so iOS/Android can render
  natively in follow-up releases.

### Added — v1.7 opt-in cross-LAN

- **Tailscale integration** (`safedrop/tailscale.py`). Parses
  `tailscale status --json`, exposes `discover_peers()` /
  `TailscalePeer.to_safedrop_peer_stub()`. New CLI subcommand
  `safedrop tailscale list` prints visible tailnet peers ready to
  drop into a SafeDrop manual-peer entry. `pubkey` field stays empty
  until first SafeDrop handshake — Tailscale is just for routing,
  SafeDrop's Fernet stays on top.
- **Rendezvous beacon** (`safedrop_mcp/rendezvous.py`) +
  `safedrop-beacon` console script. Discovery-only HTTP service:
  POST `/announce` registers `(agent_id, ip, tcp_port, pubkey,
  capabilities, ttl)`; GET `/peers` returns active entries;
  `/healthz` open for load balancers; optional Bearer-token auth
  via `--secret`. **Does not relay traffic** — just lets two SafeDrop
  peers across NATs learn each other's public address, then they
  fall back to the normal encrypted TCP path.

### Changed
- `pyproject.toml` -> `1.5.0`; new `safedrop-beacon` console entry
  alongside `safedrop`, `safedrop-mcp`, `safedrop-mcp-tokens`,
  `safedrop-agent`.
- `safedrop_mcp/server.py` `_main_async` now instantiates
  `TokenStore` once and registers handoff + notification peer tools
  alongside the existing agent_bus peer tools.

### Deferred (intentionally out of scope)
- **WebRTC hole-punching via STUN/TURN** — multi-week effort, deferred
  to a future release. Tailscale + the rendezvous beacon cover most
  real-world cross-LAN needs without the complexity.
- **Android Compose token UI** — to avoid build-breakage risk without
  a proper Android test loop. The cross-device token tools are
  callable from Android today (they appear as `<desktop_slug>__tokens_*`
  in any MCP client); a native Compose screen is straightforward
  follow-up work.
- **iOS token UI** — same reasoning; the underlying peer tools work,
  the SwiftUI screen is a follow-up.

### Tests
- 40 new tests across `test_handoff_tokens_notify.py` (23),
  `test_tailscale.py` (8), `test_beacon.py` (9). Beacon HTTP tests
  drive the Starlette ASGI app in-process via `httpx.ASGITransport`
  — no real socket needed.

## [1.4.0] — 2026-05-26

The "multi-agent mesh" release. SafeDrop becomes a fabric for AI agents
to *talk to each other* — not just to humans — across devices on the
LAN, with a persistent agent identity that survives MCP restarts.

### Added
- **`safedrop-agent`** (`safedrop/agent.py`) — headless Anthropic-SDK
  chat loop running as a SafeDrop peer. Per-sender conversation
  isolation; slash commands (`/help`, `/reset`, `/tools`, `/whoami`,
  `/history`, `/model`, `/system`). Send any text to the agent's
  SafeDrop endpoint from a phone or laptop; the reply comes back as a
  clipboard payload. Install with `pip install 'safedrop[agent]'`,
  set `ANTHROPIC_API_KEY`, run `safedrop-agent`.
- **launchd + systemd unit files** (`scripts/com.safedrop.agent.plist`,
  `scripts/safedrop-agent.service`) — install the agent as a managed
  user service on macOS / Linux. KeepAlive on macOS,
  Restart=on-failure on Linux.
- **Persistent agent identity** (`safedrop_mcp/agent_identity.py`).
  `~/.safedrop/agent_id.json` (0o600 on POSIX). Stable across MCP
  restarts; primary key for the agent-bus mailbox below. Auto-creates
  on first run, recovers from corruption.
- **Agent bus** (`safedrop_mcp/agent_bus.py`) — multi-agent messaging
  layer. Adds two SafeDrop peer tools (`agent_bus_whoami`,
  `agent_bus_recv`) on every `safedrop-mcp` instance, plus four MCP
  tools an agent can call:
    - `list_agents` — discover other agents on the LAN by their
      stable `agent_id` (plus label, peer slug, platform);
    - `send_message` — send text to a target agent (by `agent_id` or
      peer slug); delivered via SafeDrop's encrypted CALL_TOOL.
    - `recv_messages` — drain this agent's inbox (JSON-Lines mailbox
      at `~/.safedrop/agent_bus/inbox.jsonl`);
    - `whoami` — return this agent's stable identity.
- **iOS file picker** (`UIDocumentPicker` via SwiftUI `.fileImporter`)
  — pick any file from Files and send it to a SafeDrop peer with a
  single tap. Outbound chunking matches Python/Android byte-for-byte.
- **iOS `take_photo`** (Phase 2). `Photo.swift` with
  `PhotoCaptureBroker` + `UIImagePickerController` SwiftUI wrapper.
  When a remote agent calls `take_photo`, the iOS app pops the camera
  (after the Allow/Deny dialog), the user shutters, and a resized JPEG
  (long-edge cap 1600 px, q=0.82) flows back as `{mime_type,
  size_bytes, data_b64}` — same shape as the Android port.
- **NSCameraUsageDescription** + **NSPhotoLibraryUsageDescription** in
  `ios/project.yml` / `ios/SafeDrop/Info.plist` (regenerated by
  xcodegen).

### Changed
- `pyproject.toml` adds the `[agent]` extra (`anthropic>=0.40`) and the
  `safedrop-agent` console script.

### Tests
- 17 new unit + e2e tests in `tests/test_agent.py` and
  `tests/test_agent_bus.py`: AgentIdentity load/save/corruption,
  Mailbox JSONL append + read filtering, AgentBus peer-tool round-trip
  over real TransferManagers, Agent slash command coverage, per-peer
  conversation isolation, and dispatch-without-Anthropic stub.

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
