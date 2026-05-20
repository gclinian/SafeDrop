#!/usr/bin/env bash
#
# Build an unsigned SafeDrop.ipa for distribution.
#
# Output:    ios/dist/SafeDrop-<version>-unsigned.ipa
# Recipient: installs via AltStore / Sideloadly with their own Apple ID
#            (free re-signing every 7 days), or via your TestFlight if you
#            re-export with a paid developer profile.
#
# Why unsigned? A free Apple ID can only sign apps for *your own* devices,
# so distributing a signed IPA to other people isn't possible without
# Apple Developer Program ($99/year). Unsigned IPAs are the standard
# open-source distribution form — every iOS sideloader handles re-signing.

set -euo pipefail

cd "$(dirname "$0")/.."   # cd into ios/

PROJECT=SafeDrop.xcodeproj
SCHEME=SafeDrop
BUILD_DIR=./build
DIST_DIR=./dist

# Derive version from project.yml CFBundleShortVersionString fallback "1.0"
VERSION=$(awk -F': *' '/CFBundleShortVersionString:/ {gsub(/"/,"",$2); print $2; exit}' project.yml 2>/dev/null || echo "1.0")
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
STAMP="${VERSION}-${GIT_SHA}"

echo "==> regenerating Xcode project"
xcodegen generate >/dev/null

echo "==> building Release for generic iOS device (arm64, unsigned)"
xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -derivedDataPath "$BUILD_DIR" \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  CODE_SIGN_IDENTITY="" \
  build >/dev/null

APP="$BUILD_DIR/Build/Products/Release-iphoneos/SafeDrop.app"
if [[ ! -d "$APP" ]]; then
  echo "ERROR: build succeeded but $APP not found"
  exit 1
fi

echo "==> packaging Payload/SafeDrop.app into .ipa"
mkdir -p "$DIST_DIR"
STAGE=$(mktemp -d)
mkdir -p "$STAGE/Payload"
cp -R "$APP" "$STAGE/Payload/"
IPA="$DIST_DIR/SafeDrop-${STAMP}-unsigned.ipa"
( cd "$STAGE" && zip -qry "$OLDPWD/$IPA" Payload )
rm -rf "$STAGE"

SIZE=$(du -h "$IPA" | awk '{print $1}')
echo ""
echo "✅ $IPA  ($SIZE)"
echo ""
echo "Recipient install paths:"
echo "  - AltStore (free Apple ID)   https://altstore.io"
echo "  - Sideloadly (free Apple ID) https://sideloadly.io"
echo "  - Your TestFlight / ad-hoc   (requires Apple Developer Program)"
echo ""
echo "Or test it yourself by dragging the .ipa into an iOS Simulator window"
echo "after extracting Payload/SafeDrop.app and using 'xcrun simctl install'."
