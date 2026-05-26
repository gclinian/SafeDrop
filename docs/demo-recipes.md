# SafeDrop demo recipes

Five tight, copy-pasteable demos for a five-minute show-and-tell.
Each recipe includes the exact commands, what the audience sees, and
the *single* line that makes the demo land.

## Setup, once

```bash
git clone https://github.com/gclinian/SafeDrop && cd SafeDrop
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[mcp,agent]'
```

Open two terminal windows on the same machine (or two machines on the
same Wi-Fi). Recipes assume two SafeDrop nodes — easiest setup is:

```bash
# Terminal 1 — desktop GUI
python run.py

# Terminal 2 — MCP server (the one Claude Code / Cursor connects to)
safedrop-mcp
```

Both show up on the LAN as visible peers within ~3 seconds.

---

## Recipe 1 · "AirDrop, but it works across iPhone and Android" (45s)

**What:** Send a photo from an iPhone to a Linux box.

```text
1. Open the SafeDrop iOS app (Releases → SafeDrop-ios-…-unsigned.ipa,
   sideload with AltStore).
2. Tap the desktop peer in the list.
3. Tap "Send file…" → choose any image from Files.
4. Watch the GUI on Linux pop the Allow dialog. Click Allow.
5. The file lands in ~/Downloads/SafeDrop/.
```

**Punchline:** *"That was end-to-end encrypted, never touched a cloud,
and the iPhone and the Linux box have never met before this morning."*

---

## Recipe 2 · "Give Claude Code the phone's camera" (90s)

**What:** A Claude Code session on the laptop calls
`take_photo` on the iPhone; an image flows back as base64 JPEG.

```bash
# In Claude Code's MCP config
claude mcp add safedrop -- $(which safedrop-mcp)
```

In Claude Code:

```text
> list_devices
[ { name: "iPhone 15 (iOS 17, …)", slug: "iphone_a3b1d2", … } ]

> iphone_a3b1d2__take_photo
```

The iPhone pops the trust dialog → user taps Allow → camera opens →
user shutters → JPEG bytes appear in Claude Code as `{mime_type,
size_bytes, data_b64}`.

**Punchline:** *"I never wrote a 'camera API for Claude'. Any tool I
ship in SafeDrop is automatically reachable from any agent."*

---

## Recipe 3 · "Two AI agents talking to each other" (60s)

**What:** Two `safedrop-mcp` instances on different machines, each
fronting a different LLM (Claude on Mac, GPT or another Claude on
Linux). They coordinate without a cloud relay.

```bash
# On laptop A (running Claude Code)
> whoami
{ "agent_id": "agent-mac01a3", "label": "macbook (mcp)" }

> list_agents
[
  { "agent_id": "agent-mac01a3", "label": "macbook (mcp)", "is_self": true },
  { "agent_id": "agent-pi04ff",  "label": "raspi (mcp)",   "peer_slug": "raspi_x" }
]

> send_message(to_agent="agent-pi04ff",
               content="What does `uptime` say on the Pi?")
```

A few seconds later, on laptop A:

```text
> recv_messages(since_ts=0)
{
  "agent_id": "agent-mac01a3",
  "messages": [{
     "from_agent_id": "agent-pi04ff",
     "from_label": "raspi (mcp)",
     "content": "load avg 0.42 0.31 0.27, up 12 days"
  }]
}
```

**Punchline:** *"No relay, no shared queue, no cloud — the agents
talked directly over a Fernet-encrypted TCP socket on my LAN."*

---

## Recipe 4 · "State handoff: draft on Mac, finish on phone" (40s)

**What:** Save half a draft on the laptop, pick it up on the phone.

In Claude Code on the Mac:

```text
> handoff_save(key="email-to-boss",
               content="Hey Sarah, re: Q3 roadmap — three things\n\n1.")
```

On the iPhone, in any Claude / Cursor session pointed at the laptop's
`safedrop-mcp` via `<mac_slug>__handoff_load`:

```text
> macbook_9f02__handoff_load(key="email-to-boss")
{ "content": "Hey Sarah, re: Q3 roadmap — three things\n\n1.", … }
```

**Punchline:** *"This is iCloud Continuity for any app, on any device,
with no Apple ID."*

---

## Recipe 5 · "Mint a scoped token from your phone, without a CLI" (75s)

**What:** Phone mints an HTTP-transport capability token on the desktop,
restricted to `list_devices` + `send_text`, expiring in 24 hours. A
cloud agent can now hit the desktop's MCP HTTP server with that
token and nothing else.

On iOS:
1. Open SafeDrop, tap your desktop peer.
2. Tap the **key** icon (top-right) → opens the Token admin view.
3. Fill in `label = cloud-bot`, `scope = list_devices, send_text`,
   `TTL = 86400`.
4. Tap **Mint token** → one-time secret screen appears.
5. Tap **Copy to clipboard**.

On the desktop:

```bash
safedrop-mcp --http 127.0.0.1:47899
```

From any cloud agent:

```bash
curl -H "Authorization: Bearer <token-from-phone>" \
     http://your.desktop.lan:47899/mcp
```

**Punchline:** *"I just provisioned a scoped, revocable, time-bounded
API key from my phone — for an MCP server running on my laptop —
without typing a single CLI command on the laptop."*

---

## Recipe 6 · (bonus) "Cross-LAN over Tailscale, no cloud relay" (45s)

**What:** Same as Recipe 1, but the laptop is in a café and the
desktop is at home — they find each other via Tailscale.

```bash
tailscale status
# both machines must be on the same tailnet

safedrop tailscale list
# name                         ip                platform   online
# home-mac (tailscale)         100.64.1.10       darwin     True
# cafe-laptop (tailscale)      100.64.1.12       linux      True
```

In the SafeDrop GUI → **+ Add manually** → paste the IP from the table.
First handshake fills in the pubkey. From here it's identical to
Recipe 1.

**Punchline:** *"Same encryption, same trust dialog, same audit log —
SafeDrop didn't even know it crossed a LAN boundary."*

---

## What NOT to demo

* **iOS receive-file** — iOS Phase 1 doesn't accept inbound files yet
  (only clipboard / tools). Don't promise this.
* **Android `take_photo` on a brand-new install** — needs the
  CAMERA permission grant via the system intent; the user has to tap
  through. Pre-grant on the demo device.
* **WebRTC across two cellular networks** — explicitly deferred from
  v1.7. Don't get drawn into demoing it.

## Tips for a smooth live demo

1. **Mute the IDE.** Claude Code can stream a lot of trace output.
   `--quiet` or hide the tool-output pane.
2. **Set a fast pair name.** `--name-suffix demo` so the peer rows
   read `Hostname (Darwin, demo)` not `Hostname (Darwin, MCP)`.
3. **Pre-trust your peers** with "Always allow" — saves you a click
   every time during the demo. Reset via the 🔒 Manage trust dialog
   afterwards.
4. **Have the audit log open.** Every cross-device call appears in
   `~/.safedrop/audit.jsonl` — `tail -f` it on a side monitor for
   instant credibility.
