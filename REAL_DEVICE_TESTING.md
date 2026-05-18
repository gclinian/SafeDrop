# Real-device QA checklist

Everything in this repo has been validated against simulators / loopback so
far. This document is the manual test plan for putting SafeDrop on real
hardware across a real Wi-Fi LAN — the only path that exercises the actual
deployment story.

Tick each box and (where relevant) record a screenshot / pair code in the
notes column.

## Setup

You need at least **two** of the following on the same Wi-Fi network:

| Device | Build / install command |
| --- | --- |
| Mac laptop (desktop GUI) | `.venv/bin/python run.py` |
| Linux / Windows laptop | same |
| Mac (CLI / agent) | `.venv/bin/safedrop-mcp` (or `safedrop ls` etc) |
| Android phone (real, not emulator) | `cd android && ./gradlew installDebug` |
| iPhone (real device, requires personal Apple ID) | open `ios/SafeDrop.xcodeproj` in Xcode, set development team, *Product → Run* |

> **AP isolation warning.** Many public/eduroam APs separate clients so they
> can't talk to each other. If discovery doesn't converge, try a personal
> hotspot from one of the phones instead — that always allows client-to-
> client traffic.

## 1. Discovery convergence

For each pair below, both peers should appear in each other's "Nearby
devices" list within ~10 seconds of launch.

- [ ] Mac GUI ↔ Mac GUI (two macOS instances)
- [ ] Mac GUI ↔ Android phone
- [ ] Mac GUI ↔ iPhone
- [ ] Android ↔ Android (two real phones)
- [ ] Android ↔ iPhone
- [ ] iPhone ↔ iPhone
- [ ] Mac MCP server appears as a *distinct* peer next to Mac GUI on the same
      machine (proves the headless mode + dynamic port works)

Notes:

```
peer A name = …
peer B name = …
seconds-to-converge = …
```

## 2. End-to-end file transfer (only Mac↔Mac, Mac↔Android for now)

- [ ] Mac → Mac, ~10 MB random file. SHA-256 matches.
- [ ] Mac → Android, ~10 MB random file. Android shows pair code in the
      Accept dialog. After Accept, the file appears under
      `Android/data/com.safedrop.android/files/Download/SafeDrop/`.
- [ ] Android → Mac, image picked via SAF. Lands in `~/Downloads/SafeDrop/`.

Pair code matches on both ends in every case? ☐

## 3. Clipboard / URL / code

- [ ] Mac → Android URL → tap *Open URL* → opens in Chrome.
- [ ] Mac → iPhone URL → tap *Open* → Safari.
- [ ] Android → Mac code snippet → `pyperclip.paste()` returns the same bytes.
- [ ] iPhone → Mac text → arrives in clipboard banner on Mac GUI.

## 4. Cross-device tools (Phase 2)

From an MCP-enabled client (Claude Code with `safedrop-mcp` configured, or
`safedrop call <peer> <tool>` from a shell):

- [ ] `<peer_slug>__system_info` returns Build / `UIDevice` / `uname` info.
- [ ] `<peer_slug>__read_clipboard` returns the receiver's clipboard
      (after Allow on the receiver's dialog).
- [ ] `<peer_slug>__write_clipboard` with `{"content": "hello"}` actually
      updates the peer's clipboard (verified by reading it back from the
      peer's UI).
- [ ] `<phone_slug>__take_photo` (Android only, Phase 3) opens the system
      camera, capture+confirm, agent receives a valid JPEG. Save to a file
      and confirm magic bytes `ff d8 ff`.

## 5. Trust + audit

- [ ] First call from an unfamiliar peer triggers the Allow/Deny dialog.
      Pair code shown on **both** sides matches.
- [ ] *Always allow* persists across app restarts (Python: `~/.safedrop/trust.json`,
      Android: SharedPreferences, iOS: UserDefaults).
- [ ] *Manage trust* dialog lists the saved entries; revoke clears them.
- [ ] Audit panel records every inbound + outbound call (both ends) with
      decision and result summary.
- [ ] On Python: `~/.safedrop/audit.jsonl` accumulates entries (tail -f to
      watch live).

## 6. Network conditions

- [ ] Same Wi-Fi: works.
- [ ] Personal hotspot from one phone: works.
- [ ] Different Wi-Fi (e.g. 2.4 GHz vs 5 GHz on same SSID): works.
- [ ] Wired Ethernet (Mac) + Wi-Fi (phone) on same LAN: works.
- [ ] AP isolation enabled: discovery fails as expected; manual peer entry
      with known IP + pubkey still works.

## 7. Security spot-check

- [ ] Run Wireshark on the LAN, filter `tcp.port == 47891`. After the two
      plaintext HELLO frames, all subsequent traffic is opaque ciphertext.
- [ ] Two peers with different identities show different pair codes when
      paired with the same third peer. (Proof that pair code is per-pair,
      not per-device.)
- [ ] After "Always deny" for a (peer, tool), the dispatcher returns within
      <100 ms (proves the policy short-circuits before any UI hop).

## 8. Crash / recovery

- [ ] Force-quit one peer mid-transfer → other peer's transfer marked
      `failed` with a sensible error string.
- [ ] Toggle Wi-Fi off/on → peers reappear in the discovery list within
      one broadcast interval (~3 s) after Wi-Fi recovers.
- [ ] Multiple manual-peer entries can coexist; removing one doesn't
      affect the others.

## 9. Devices to add (Phase 3 / beyond)

Not yet covered — track here as we add them:

- [ ] iOS `take_photo` (UIImagePickerController). Right now iOS Phase 1
      exposes only `system_info` / `read_clipboard` / `write_clipboard`.
- [ ] Android `take_photo` against a real camera (was only validated on
      the emulator's synthetic camera scene).
- [ ] Cellular fallback (5G hotspot, no Wi-Fi).
- [ ] Mixed IPv4 / IPv6 networks (rare in home Wi-Fi but matters in some
      university dorms).

---

## Helpers added to make this checklist easier

| Helper | Purpose |
| --- | --- |
| `safedrop ls` | CLI peer discovery — fastest way to verify the network is reachable from a Mac/Linux box without the GUI |
| `python bench.py receive --port 47891` | Headless receiver that prints its own pubkey, so a phone can use "Add manually" |
| `adb forward tcp:48050 tcp:47891` | Lets a Mac talk to an Android emulator's TCP listener as if it were local |
| `xcrun simctl spawn booted defaults write com.safedrop.ios safedrop.trust.v1 …` | Pre-populate iOS trust for scripted tests |
| `tests/test_android_tools_interop.py 127.0.0.1 <port>` | Drives the LIST_TOOLS / CALL_TOOL protocol against any peer — works against Python desktop, Android emulator, iOS simulator alike |

When everything in §1-§5 passes on real hardware, ship.
