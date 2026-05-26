# SafeDrop

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-95%2F95_passing-brightgreen.svg)](#tests)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows%20%7C%20Android%20%7C%20iOS-lightgrey.svg)](#platforms)
[![MCP](https://img.shields.io/badge/MCP-stdio%20%2B%20HTTP-orange.svg)](MCP_AGENT_GUIDE.md)

> **Secure, zero-config LAN file & clipboard sharing — with a built-in
> cross-device Model Context Protocol fabric for AI agents.**

SafeDrop lets devices on the same Wi-Fi automatically discover each
other and exchange data through a direct, end-to-end encrypted TCP
connection. **No cloud, no account, no manual IP.** And because the
same protocol drives a built-in [MCP](https://modelcontextprotocol.io/)
server, any AI agent (Claude Code, Cursor, Goose, Cline, Hermes via
Ollama, a cloud agent over HTTPS …) can use SafeDrop to send files,
read clipboards, or invoke tools on **any** of your paired devices.

<sub>Originally a final project for NTU CN Lab Spring 2026 — now an
open-source toolkit.</sub>

## Table of contents

- [Features](#features)
- [Quick start](#quick-start)
- [Platforms](#platforms)
- [AI agent integration](#ai-agent-integration)
- [Architecture](#architecture)
- [Security model](#security-model)
- [Documentation](#documentation)
- [Tests](#tests)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)

## Features

| | |
| --- | --- |
| ⚡ **Zero-configuration discovery** | UDP broadcast (`HELLO` / `BYE`) — same-Wi-Fi devices appear automatically |
| 🔒 **End-to-end encrypted** | X25519 ECDH per session, then Fernet (AES-128-CBC + HMAC-SHA256). 4-digit pair code derived from the shared secret for visual MITM check |
| 📁 **Files & clipboards** | 64-KB chunked TCP, progress + speed UI; text / URL / code snippets with copy-or-open-URL receive prompt |
| 🤝 **Receiver consent** | Every inbound transfer surfaces an Allow/Deny dialog with the pair code |
| 🛠 **Cross-device tools** | Every peer exposes a `ToolRegistry` (`system_info` / `read_clipboard` / `write_clipboard` / `run_shell` / `take_photo` on Android); other peers can list and invoke them |
| 🤖 **MCP server, stdio + HTTP** | Drop into Claude Code / Cursor / Goose / Cline / Claude Desktop. HTTP transport with scoped bearer tokens for remote / cloud agents |
| 🧩 **MCP bridge** | Import any other MCP server (filesystem, github, fetch, …) — its tools become callable from every paired SafeDrop peer on the LAN |
| ➕ **Runtime tool registration** | `register_local_tool` lets an agent add new tools at runtime via an HTTP callback URL |
| 📜 **Trust + audit** | Per-(peer, tool) decisions persisted on disk. Allow once / Always allow / Deny once / Always deny. Audit log on all three platforms. Append-only JSONL on Python |
| 📲 **Native apps** | Python desktop (tkinter), Android (Kotlin / Compose), iOS (Swift / SwiftUI) — all speak the same protocol byte-for-byte |

## Quick start

### Desktop (Python)

```bash
git clone https://github.com/gclinian/SafeDrop.git
cd SafeDrop
python3 -m venv .venv
```

Then activate the venv — the exact line depends on your shell:

```bash
# Linux / macOS (bash / zsh)
source .venv/bin/activate

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

# Windows (cmd.exe)
.venv\Scripts\activate.bat
```

After activation, everything is platform-uniform:

```bash
pip install -e .[mcp]

safedrop-mcp --help        # CLI + MCP server (any Python 3.10+)
python run.py              # Desktop GUI (tkinter required, see notes)
```

Run `python run.py` on two machines on the same Wi-Fi. Both peers
appear in each other's *Nearby devices* list within ~10 s. Pick one,
choose a file or paste some text, click **Send**. The other side gets
an Allow/Deny dialog with the pair code; on Accept, the transfer
begins.

> **macOS tkinter note.** Homebrew's `python3` ships without Tk — if
> `python3 -c "import tkinter"` fails, install
> [python.org's distribution](https://www.python.org/downloads/macos/)
> or `brew install python-tk@3.12` and recreate the venv with that
> interpreter (e.g.
> `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m venv .venv`).
> The **CLI + MCP server work with any Python 3.10+** — tkinter is
> only needed for the desktop GUI.

> **Windows note.** Python from [python.org](https://www.python.org/downloads/windows/)
> includes tkinter by default — no extra setup. The first GUI / MCP
> launch will trigger a **Windows Defender Firewall** prompt; tick
> "Private networks" so peers on the same Wi-Fi can reach the TCP
> listener (default port 47891) and UDP discovery (47890). Token
> file permissions (`~/.safedrop/tokens.json`) fall back to NTFS user
> ACLs rather than POSIX 0600, so don't share the file with other
> Windows accounts.

### CLI (any shell or bash-tool agent)

```bash
safedrop ls                                          # list nearby peers
safedrop send-file <device> <path>                   # push a file
safedrop send-text <device> "https://…" --type url   # push a URL
safedrop call <device> read_clipboard                # invoke a remote tool
safedrop wait --timeout 120                          # block until someone drops something
```

### Android / iOS

See the [Platforms](#platforms) section for build instructions.

## Platforms

| Platform | Tech | Status |
| --- | --- | --- |
| **macOS / Linux / Windows desktop** | Python 3.10+, tkinter | ✅ files, clipboard, MCP server, CLI, trust+audit UI. Same code path on all three — Windows users get tkinter out-of-the-box from python.org and need to allow the app through Windows Defender Firewall on first launch |
| **Android** | Kotlin, Jetpack Compose, AGP 8.13 | ✅ files, clipboard, tools (`system_info` / `read_clipboard` / `write_clipboard` / `take_photo`), trust+audit UI |
| **iOS** | Swift 6, SwiftUI, iOS 17+ | ✅ Phase 1 — tools (`system_info` / `read_clipboard` / `write_clipboard`), trust+audit UI. File picker on the roadmap |

```bash
# Android
cd android && ./gradlew installDebug

# iOS (xcodegen + Xcode required) — Simulator
cd ios && xcodegen generate
xcodebuild -project SafeDrop.xcodeproj -scheme SafeDrop \
    -destination 'platform=iOS Simulator,name=iPhone 17' build

# iOS — build a redistributable unsigned IPA
./ios/scripts/build-ipa.sh         # → ios/dist/SafeDrop-<version>-unsigned.ipa
```

> **Distributing the iOS app to other people** is non-trivial because
> Apple doesn't allow handing over a signed `.ipa` the way Android
> allows handing over an `.apk`. The recommended free path is the
> unsigned IPA produced above + recipients sideload with their own
> Apple ID via [AltStore](https://altstore.io/) or
> [Sideloadly](https://sideloadly.io/). For TestFlight / proper
> ad-hoc distribution you need the $99/year Apple Developer Program.
> Full guide: [`ios/DISTRIBUTION.md`](ios/DISTRIBUTION.md).

## AI agent integration

SafeDrop ships a Model Context Protocol server: **`safedrop-mcp`**.
Two flavours of usage:

### Local agent (stdio)

Plug it into any MCP-supporting agent and the agent gets a tool surface
that includes the peer fabric:

```bash
claude mcp add safedrop -- /path/to/.venv/bin/safedrop-mcp
```

What the agent sees:

```
list_devices               # the LAN peers + slugs + capabilities
send_file / send_text      # push to a peer
wait_for_drop              # block until someone pushes to us
list_remote_tools          # explicit access to a peer's tools
call_remote_tool           # explicit invocation
audit_log                  # local cross-device call history
register_local_tool        # let the agent add new tools at runtime
list_local_tools           # see dynamic + bridged tools
pi_a3f2b1__system_info     # dynamic: any tool from a peer named "pi" (slug)
bridge.github.create_issue # any tool from another MCP server you've bridged
```

### Remote agent (HTTP, scoped tokens)

```bash
# Mint a narrow-scope, time-bounded capability token
safedrop-mcp-tokens mint --label "cloud-agent" --ttl 86400 \
    --scope "list_devices,send_text,phone_*__read_clipboard"

# Run the HTTP MCP server (combine with Tailscale / Cloudflare Tunnel)
safedrop-mcp --http 127.0.0.1:47899
```

The agent connects to `http://<host>:47899/mcp/` with
`Authorization: Bearer <token>`. Tokens have fine-grained scope,
expiry, and can be `safedrop-mcp-tokens revoke`'d instantly.

### Per-agent policy

Run multiple `safedrop-mcp` instances side-by-side — each agent gets
its own scope:

```bash
safedrop-mcp --profile claude-readonly        # ~/.safedrop/mcp-profiles/*.json
safedrop-mcp --allow "list_devices,phone_*__read_clipboard"
safedrop-mcp --deny "*__run_shell"
```

**See [`MCP_AGENT_GUIDE.md`](MCP_AGENT_GUIDE.md) for the full walk-through**
covering Claude Code, Cursor, Goose, Cline, Hermes (Ollama), cloud
agents, bridges, and dynamic registration patterns.

## Architecture

Five layers; the same protocol contract on all three implementations.

```
┌────────────────────────────────────────────────────┐
│  User Interface          tkinter / Compose / SwiftUI│
├────────────────────────────────────────────────────┤
│  Discovery               UDP broadcast (HELLO / BYE)│
├────────────────────────────────────────────────────┤
│  Control protocol        JSON over TCP              │
│                          REQUEST / ACCEPT / CHUNK   │
│                          LIST_TOOLS / CALL_TOOL ... │
├────────────────────────────────────────────────────┤
│  Data transfer           TCP socket, 64 KB chunked  │
├────────────────────────────────────────────────────┤
│  Security                X25519 ECDH → HKDF-SHA256  │
│                          → Fernet (AES-128 + HMAC)  │
└────────────────────────────────────────────────────┘
```

### Repository layout

```
safedrop/         Python core (config, crypto, discovery, protocol,
                  transfer, tools, trust, headless, gui, cli)
safedrop_mcp/     MCP server: stdio + HTTP, policy, tokens, bridge,
                  dynamic tool registration
android/          Native Kotlin / Compose client (xcodegen-equiv via
                  gradle wrapper, checked in)
ios/              Native Swift / SwiftUI client (xcodegen-managed)
tests/            95 Python tests covering everything above
SPEC.md           Protocol specification
MCP_AGENT_GUIDE.md       Agent integration guide
REAL_DEVICE_TESTING.md   Manual QA checklist
CONTRIBUTING.md   How to contribute
CHANGELOG.md      Version history
LICENSE           MIT
```

## Security model

1. **Discovery is plaintext** but carries only `(name, pubkey, capabilities)` — no payload data.
2. **Each TCP connection** starts with one plaintext HELLO each way carrying the X25519 public keys.
3. **Shared secret** = X25519 ECDH; **session key** = HKDF-SHA256(shared, info=`"SafeDrop v1 fernet key"`); **pair code** = HKDF-SHA256(shared, info=`"SafeDrop v1 pair code"`)[:4 bytes] mod 10000.
4. **Every subsequent frame** (request, ACCEPT/REJECT, chunk, tool call, result) is **Fernet-encrypted** (AES-128-CBC + HMAC-SHA256).
5. **Receiver consent** is required for every transfer and tool call (with persistent "Always allow" / "Always deny" overrides per (peer, tool)).
6. **MCP HTTP transport** adds bearer-token auth + per-token scope enforcement.

You can verify with Wireshark — filter `tcp.port == 47891` and only the
two HELLO frames are readable.

## Documentation

| Document | What's inside |
| --- | --- |
| **[`SPEC.md`](SPEC.md)** | Protocol specification — every message type, framing, layer architecture |
| **[`MCP_AGENT_GUIDE.md`](MCP_AGENT_GUIDE.md)** | Agent-integration walkthrough: stdio + HTTP + bridge + dynamic registration + per-agent policy + capability tokens, with copy-paste configs for Claude Code / Cursor / Goose / Cline / Hermes / cloud agents |
| **[`REAL_DEVICE_TESTING.md`](REAL_DEVICE_TESTING.md)** | 9-section manual QA checklist for putting SafeDrop on real hardware across a real Wi-Fi LAN |
| **[`CHANGELOG.md`](CHANGELOG.md)** | Per-version release notes |
| **[`CONTRIBUTING.md`](CONTRIBUTING.md)** | Setup, style, PR checklist |

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
# Ran 95 tests in ~45 s — OK
```

Coverage at a glance:

| Suite | Cases | What it proves |
| --- | --- | --- |
| `test_e2e.py` | 4 | File / clipboard / reject / pair-code on two in-process `TransferManager`s |
| `test_mcp.py` | 5 | List devices / send-file / send-text / ambiguity / dynamic ports across two `HeadlessSafeDrop`s |
| `test_mcp_protocol.py` | 1 | Spawns `safedrop-mcp`, drives it via the official MCP client SDK, asserts namespaced peer tools appear |
| `test_tools.py` | 7 | Cross-device tools protocol: HELLO capabilities, LIST_TOOLS, CALL_TOOL, authorizer deny, audit log |
| `test_trust.py` | 7 | TrustPolicy short-circuits the authorizer; persistence round-trip; AuditWriter JSONL |
| `test_android_interop.py` | 1 | Cross-language pair-code match (Python ↔ Kotlin ↔ Swift all derive the same number) |
| `test_android_tools_interop.py` | 2 | LIST_TOOLS / CALL_TOOL against the Android emulator (also works against the iOS Simulator with a different port) |
| `test_policy_tokens.py` | 9 | Policy resolution + TokenStore mint/validate/revoke/expired |
| `test_http_transport.py` | 3 | Spawns `safedrop-mcp --http`, asserts auth + scope enforcement via the official MCP HTTP client |
| `test_dynamic_tools.py` | 2 | `register_local_tool` HTTP round-trip; MCP bridge to a third-party stdio server |

## Roadmap

- [x] **v1.0** — LAN file + clipboard sharing (Python + Android)
- [x] **v1.1** — MCP server + CLI + cross-device tools protocol
- [x] **v1.2** — `take_photo` on Android, namespace-flatten in MCP, persistent trust + audit
- [x] **v1.3** — iOS Phase 1, MCP HTTP transport with scoped tokens, MCP bridge, runtime `register_local_tool`, Trust UI on all platforms
- [x] **v1.4** — `safedrop-agent` headless Anthropic-SDK bot with per-peer conversation isolation, slash commands (`/reset`, `/tools`, `/whoami`, `/history`, `/model`, `/system`), launchd / systemd unit files; iOS file picker (send-from-iPhone)
- [x] **v1.5** — Multi-agent mesh: agent-bus MCP tools (`send_message`, `recv_messages`, `list_agents`, `whoami`), persistent `agent_id` at `~/.safedrop/agent_id.json` (survives MCP restarts); iOS `take_photo` (Phase 2)
- [x] **v1.6** — Continuity primitives: state handoff (`handoff_save`/`load`/`list`/`delete`), notification mirroring (`show_notification`/`notifications_recent`), capability-token cross-device tools + token UI in the Python desktop GUI (Android/iOS UIs deferred — peer tools work today)
- [x] **v1.7** — Opt-in cross-LAN: `safedrop tailscale` CLI parses `tailscale status` and exports manual-peer stubs; `safedrop-beacon` discovery-only HTTP service for cross-NAT peer rendezvous (does not relay traffic). WebRTC hole-punching deferred to a future release — Tailscale + beacon cover most real-world cross-LAN needs without it.
- [ ] **v2.0** — "Continuity OS" wedge: cross-device input sharing (keyboard / mouse, à la Logitech Flow), Sidecar-style window streaming, phone-as-webcam, workspace tags (walk to any desk → tap → your environment appears)

## Contributing

PRs, issues, and translations welcome. Please read
[`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup, the wire
protocol contract, and the PR checklist.

### Reporting bugs

Open an issue with reproduction steps, OS / device versions, and any
relevant log output.

### Security disclosures

Don't open a public issue — use GitHub's private vulnerability
reporting on this repo.

## License

[MIT](LICENSE) © 2026 SafeDrop contributors.

## Acknowledgments

- The project started as the Spring 2026 final for NTU's **CN Lab** —
  thanks to the course staff for the prompt that turned into this.
- Cryptography: [Curve25519](https://en.wikipedia.org/wiki/Curve25519),
  [HKDF](https://datatracker.ietf.org/doc/html/rfc5869),
  [Fernet](https://github.com/fernet/spec) (we re-implement the same
  token format on each platform).
- [Anthropic's Model Context Protocol](https://modelcontextprotocol.io/)
  for the tool-server standard the AI integration layer plugs into.
- Prior art that informed the design: [LocalSend](https://localsend.org/),
  [KDE Connect](https://kdeconnect.kde.org/), Apple Continuity.
