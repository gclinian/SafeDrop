# Distributing SafeDrop iOS to other people

Apple does not let you hand someone an `.ipa` the way you can hand them
an `.apk`. Every install path has trade-offs. This doc lays them out
honestly so you can pick the one that matches your goal.

## Quick reference

| Path | Cost | Recipient effort | Re-sign cadence | Audience cap |
| --- | --- | --- | --- | --- |
| **Unsigned IPA + AltStore / Sideloadly** | Free | Install one helper app, sign in with their own Apple ID | Every 7 days (automatic in AltStore) | Anyone willing to sideload |
| **Apple Developer Program — ad-hoc** | $99/yr | Tap a link | 1 year (until cert expires) | 100 iPhones/year per type (UDIDs you register) |
| **Apple Developer Program — TestFlight** | $99/yr | Install the TestFlight app, tap your link | 90 days per build | 10,000 external testers |
| **Apple Developer Program — App Store** | $99/yr + review | Tap the App Store link | Forever | Everyone |

## Option 1 — Unsigned IPA (open-source default)

This is what `ios/scripts/build-ipa.sh` produces.

```bash
./ios/scripts/build-ipa.sh
# → ios/dist/SafeDrop-<version>-<git-sha>-unsigned.ipa
```

The IPA contains an arm64 Mach-O for iOS device, but **no signature**
— so it cannot install directly on iOS. The recipient re-signs it on
their own machine with their own Apple ID. Two ways for them to do that:

### Recipient setup: AltStore (recommended)

1. Install [AltServer](https://altstore.io) on their Mac/PC.
2. Plug their iPhone in once, install AltStore on the phone.
3. Open AltStore on the phone, **Files → +** → pick `SafeDrop-…-unsigned.ipa`.
4. Sign in with any Apple ID; AltStore re-signs every 7 days automatically
   as long as the phone and the host machine are on the same Wi-Fi.

### Recipient setup: Sideloadly (one-shot, no resident helper)

1. Install [Sideloadly](https://sideloadly.io).
2. Plug iPhone in, drag the IPA, enter Apple ID password.
3. App lasts 7 days; to extend, repeat the drag.

### Why does the cert expire every 7 days?

Free Apple IDs get a "personal development" certificate that Apple
issues without charge but capped at 7 days. AltStore quietly re-signs
in the background, but the host machine and phone must reach each
other (typically same Wi-Fi). On a paid Developer Program account the
cert is good for one year.

## Option 2 — Apple Developer Program ad-hoc

If you've enrolled at <https://developer.apple.com/programs/> ($99 USD/year):

1. In Xcode → Signing & Capabilities, set the team to your developer team.
2. In <https://developer.apple.com/account> add each tester's iPhone UDID
   (you can use the *Devices* tab — up to 100 iPhones per year per type).
3. Create an **Ad Hoc** provisioning profile that includes those devices.
4. Build + export:

   ```bash
   cd ios
   xcodebuild \
     -project SafeDrop.xcodeproj \
     -scheme SafeDrop \
     -configuration Release \
     -archivePath ./build/SafeDrop.xcarchive \
     archive

   cat > ./build/ExportOptions.plist <<'EOF'
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0"><dict>
     <key>method</key><string>ad-hoc</string>
     <key>teamID</key><string>YOUR_TEAM_ID</string>
     <key>signingStyle</key><string>automatic</string>
     <key>stripSwiftSymbols</key><true/>
   </dict></plist>
   EOF

   xcodebuild -exportArchive \
     -archivePath ./build/SafeDrop.xcarchive \
     -exportPath ./build/ipa \
     -exportOptionsPlist ./build/ExportOptions.plist
   # → ios/build/ipa/SafeDrop.ipa
   ```

5. Host the IPA on GitHub Releases / your own server, write a tiny
   **manifest.plist**, then have testers tap an `itms-services://?action=download-manifest&url=…`
   link. Apple's [doc](https://developer.apple.com/documentation/xcode/distributing_your_app_to_registered_devices)
   has the exact manifest format.

## Option 3 — TestFlight (best mass-distribution)

Same Developer Program + App Store Connect setup, but:

1. `xcodebuild archive` then **upload** the archive to App Store Connect
   (Xcode Organizer's *Distribute App* button, or `xcrun altool` /
   `xcrun notarytool upload`).
2. Apple does a lightweight beta review (usually <24 h for first
   submission, faster afterwards).
3. Send a public TestFlight link — any iPhone owner who taps it,
   installs the TestFlight app, joins the beta. Up to 10 000 external
   testers per build. Builds expire in 90 days.

This is the only first-class way to give a stranger an iPhone link
that "just works". For an open-source project this is the right answer
once you have $99 to spend.

## Verifying the IPA before you ship

```bash
# Check what's inside
unzip -l ios/dist/SafeDrop-*-unsigned.ipa | head -20

# Confirm the architecture
unzip -p ios/dist/SafeDrop-*-unsigned.ipa Payload/SafeDrop.app/SafeDrop | file -
# → expected: Mach-O 64-bit executable arm64

# Smoke-test in the Simulator by extracting the Release-iphoneos build
# (note: simulator needs Release-iphonesimulator, so this only proves
# the device binary is well-formed, not that it boots — boot test is
# AltStore or a paid TestFlight cycle.)
```

## What SafeDrop's IPA does (and doesn't) contain

- ✅ The iOS 17+ universal binary with SwiftUI UI
- ✅ `NSLocalNetworkUsageDescription` + Bonjour services — first launch on
  a real device will prompt "SafeDrop wants to find devices on your local
  network"
- ✅ All SafeDrop features documented in [`../README.md`](../README.md):
  X25519 + Fernet, discovery, send_text, peer tool registry, Allow/Deny,
  trust list, audit log
- ❌ No file picker yet (iOS Phase 1) — you can't pick a local file to send
  *from* iOS. Receiving clipboard from a Mac/Android peer works; sending
  files is on the roadmap. (Android already has it; same protocol so
  porting back is straightforward.)
- ❌ No camera tool (`take_photo`) — Android has it; iOS Phase 2 will add it.

## TL;DR

For an **open-source project with no budget**: ship the unsigned IPA as
a GitHub Release, link to this doc, recipients use AltStore. That's how
projects like Mastodon clients / WireGuard clients / etc used to do it
before they got Developer Programs.

For **a real demo or actual users on real devices long-term**: $99 to
Apple, TestFlight beta, public link.
