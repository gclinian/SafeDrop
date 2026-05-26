# SafeDrop — the elevator pitch

> Zero-config LAN file & clipboard sharing — with a built-in
> cross-device Model Context Protocol fabric so AI agents on your
> laptop, phone, and Pi can see each other's tools.

## 30-second version (for a Twitter / HN thread)

AirDrop is great until you leave the Apple walled garden. SafeDrop is
a five-platform (macOS / Linux / Windows desktop, Android, iOS),
fully open-source, **MIT-licensed** LAN drop tool that's also an
**MCP fabric** — paired devices expose their clipboard, camera, shell,
and any custom tool to AI agents on other paired devices, with
per-tool trust prompts and an audit log.

* X25519 + Fernet over JSON-over-TCP, byte-for-byte compatible across
  Python / Kotlin / Swift
* Discovery via UDP broadcast on the LAN — no router setup, no
  accounts, no cloud
* MCP server (stdio + HTTP Streamable) plugs straight into Claude
  Code, Cursor, Goose, Cline, Claude Desktop
* Multi-agent mesh: two SafeDrop-MCP agents on different machines
  can `send_message` / `recv_messages` over the encrypted channel —
  the agents themselves coordinate, no human in the loop
* Capability tokens, per-agent scope policies, per-(peer, tool) trust
  decisions, append-only audit log — every cross-device call is
  authorised and logged
* Opt-in Tailscale integration + reference rendezvous beacon for
  cross-LAN — the default stays LAN-only with no third party seeing
  anything

> 95 → **102** Python tests passing. iOS IPA + Android APK + Python
> wheel published on every release. v1.7 just shipped (v2.0 is
> Continuity OS territory — keyboard / mouse share, Sidecar streaming,
> phone-as-webcam).

[github.com/gclinian/SafeDrop](https://github.com/gclinian/SafeDrop)

## 2-minute version (for a blog post intro)

The idea is simple: most of the cross-device features Apple ships
(AirDrop, Universal Clipboard, Continuity Camera, Sidecar) work *only*
in the Apple ecosystem, and *only* between hardware you've signed into
the same iCloud account. SafeDrop reproduces the file + clipboard
parts on a fully open protocol that runs on five platforms, with no
sign-in.

But that's just the floor. Once you have a secure, paired, peer-to-peer
channel between your devices, you can do something much more
interesting: expose **tools** across it. Phone, run `take_photo`.
Pi, run a shell command. Desktop, read my clipboard. SafeDrop ships
this as a Model Context Protocol layer that:

1. Surfaces every paired device's tools to any MCP-aware agent
   (Claude Code, Cursor, Goose, Cline, …) as flat
   `<peer_slug>__<tool>` entries — the agent doesn't need to know
   anything about routing
2. Has its own multi-agent mesh primitive so two AI agents on
   different machines coordinate directly over the encrypted channel,
   not through a cloud service
3. Bridges third-party MCP servers (filesystem, GitHub, fetch, …) into
   the same namespace so the agent sees one unified tool list

There is no cloud. There is no relay (unless you opt in to the
reference rendezvous beacon for cross-LAN). Your devices talk
directly over Wi-Fi with X25519 + Fernet encryption, and the only
thing that ever leaves your LAN is what you (or your agent) chose to
send.

## Who it's for

* **Power users** who want AirDrop but across iPhone + Android + Linux,
  not just Mac + iPhone.
* **Devs** who want to give Claude Code / Cursor access to their phone
  ("take a photo of this whiteboard and explain it") or their Pi
  ("what's CPU load on the homelab right now?") without writing a
  bespoke API for each.
* **Privacy-minded teams** who can't or won't use a cloud sync service
  but still want continuity across desktops + phones.
* **MCP server authors** who want their tool to be reachable from
  *any* device on the LAN, not just the laptop the agent's running on.

## What we shipped (as of v1.7)

| Phase | What |
|---|---|
| v1.0 | LAN file + clipboard sharing (Python + Android) |
| v1.1 | MCP server + CLI + cross-device tools protocol |
| v1.2 | Android `take_photo`, MCP namespace flatten, persistent trust + audit |
| v1.3 | iOS Phase 1, MCP HTTP transport with scoped tokens, MCP bridge, runtime `register_local_tool`, trust UI |
| v1.4 | `safedrop-agent` headless Anthropic bot with per-peer convs + slash commands; iOS file picker; launchd / systemd units |
| v1.5 | Multi-agent mesh: persistent `agent_id`, `send_message`, `recv_messages`, `list_agents`; iOS `take_photo` |
| v1.6 | Continuity primitives: token mint/revoke UI, state handoff (`handoff_save`/`load`), notification mirroring (native on all 3 platforms) |
| v1.7 | Opt-in cross-LAN: Tailscale integration + reference rendezvous beacon |

102 Python tests cover the wire protocol + every feature above.

## How to try it (3 lines)

```bash
git clone https://github.com/gclinian/SafeDrop && cd SafeDrop
pip install -e '.[mcp]'
python run.py      # GUI on desktop · `safedrop-mcp` for an agent · APK / IPA on Releases
```

Then on another machine on the same Wi-Fi, install the same way (or
grab the [Release](https://github.com/gclinian/SafeDrop/releases) IPA
/ APK), and they find each other in seconds. Drop a file, send a
clipboard, give Claude Code your phone's camera as a tool.

## Why MIT-licensed

This started as a final project for NTU's Computer Networks Lab; the
spec was "build something that uses the network." We thought:
AirDrop-style sharing is *the* network thing every student wishes
they had on Linux, and AI agents are *the* thing every dev is wiring
up right now. Composing the two felt obviously correct.

MIT means anyone can fork, ship, embed. No CLA, no contributor
restrictions. The protocol is the contract — three implementations in
the repo, more welcome.

## Links

* Code: [github.com/gclinian/SafeDrop](https://github.com/gclinian/SafeDrop)
* Releases (IPA, APK, wheel): [github.com/gclinian/SafeDrop/releases](https://github.com/gclinian/SafeDrop/releases)
* MCP integration guide: [MCP_AGENT_GUIDE.md](../MCP_AGENT_GUIDE.md)
* Wire protocol spec: [SPEC.md](../SPEC.md)
* Demo recipes: [demo-recipes.md](demo-recipes.md)
* Contributing: [CONTRIBUTING.md](../CONTRIBUTING.md)
