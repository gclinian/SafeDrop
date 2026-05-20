#!/usr/bin/env bash
#
# Auto-detect-and-release script.
#
# Reads the version from pyproject.toml. If a tag for that version
# already exists, exits cleanly (nothing to do). Otherwise:
#
#   1. refuses if the git tree is dirty
#   2. refuses if CHANGELOG.md has no `## [<version>]` section
#   3. runs the full Python test suite
#   4. builds:  iOS unsigned IPA, Android debug APK, Python wheel + sdist
#   5. tags `v<version>` and pushes the tag
#   6. creates a GitHub Release with the four artifacts attached and the
#      matching CHANGELOG section as release notes
#
# Usage:
#   ./scripts/release.sh                 # release if pyproject version is new
#   ./scripts/release.sh --dry-run       # show plan, don't tag / push / release
#   ./scripts/release.sh --force         # re-release even if the tag exists
#                                        # (deletes the existing release first)

set -euo pipefail

DRY_RUN=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --force)      FORCE=1   ;;
        *) echo "unknown arg: $arg"; exit 2 ;;
    esac
done

step() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m⚠  %s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m❌ %s\033[0m\n" "$*"; exit 1; }
do_or_say() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf "    [dry-run] would run: %s\n" "$*"
    else
        "$@"
    fi
}

cd "$(git rev-parse --show-toplevel)"

# 1. read version from pyproject.toml ----------------------------------------
VERSION=$(python3 -c '
import sys, tomllib
with open("pyproject.toml", "rb") as f:
    print(tomllib.load(f)["project"]["version"])
')
TAG="v$VERSION"
step "version detected: $TAG"

# 2. check tag status --------------------------------------------------------
if git rev-parse "$TAG" >/dev/null 2>&1; then
    if [[ $FORCE -eq 1 ]]; then
        warn "tag $TAG exists; --force will delete existing release and re-publish"
    else
        echo "✅ tag $TAG already exists — bump the version in pyproject.toml first."
        echo "   Nothing to do. (Use --force to re-release the same version.)"
        exit 0
    fi
fi

# 3. clean tree --------------------------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
        warn "git tree is dirty (would block a real release; allowed for --dry-run)"
    else
        git status --short
        fail "git tree is dirty. Commit or stash, then retry."
    fi
fi

# 4. CHANGELOG entry exists --------------------------------------------------
if ! grep -q "^## \[$VERSION\]" CHANGELOG.md; then
    fail "CHANGELOG.md has no '## [$VERSION]' section. Add one (see Keep-a-Changelog format)."
fi

# 5. tests -------------------------------------------------------------------
step "running tests"
if [[ -x .venv/bin/python ]]; then
    PY=.venv/bin/python
else
    PY=python3
fi
do_or_say "$PY" -m unittest discover -s tests -q

# 6. build artifacts ---------------------------------------------------------
step "building iOS unsigned IPA"
do_or_say rm -rf ios/dist dist
do_or_say mkdir -p dist
do_or_say ./ios/scripts/build-ipa.sh
# Rename to a canonical filename: <project>-ios-<version>-unsigned.ipa
if [[ $DRY_RUN -eq 0 ]]; then
    IPA_RAW=$(ls -1 ios/dist/SafeDrop-*-unsigned.ipa | head -1)
    mv "$IPA_RAW" "dist/SafeDrop-ios-${VERSION}-unsigned.ipa"
fi

step "building Android debug APK"
do_or_say bash -c 'cd android && ./gradlew --quiet assembleDebug'
do_or_say cp android/app/build/outputs/apk/debug/app-debug.apk "dist/SafeDrop-android-${VERSION}-debug.apk"

step "building Python wheel + sdist"
do_or_say "$PY" -m pip install --quiet build
do_or_say "$PY" -m build --sdist --wheel --outdir dist

if [[ $DRY_RUN -eq 0 ]]; then
    echo
    ls -la dist/
fi

# 7. extract CHANGELOG section as release notes -----------------------------
NOTES_FILE=$(mktemp)
"$PY" - "$VERSION" >"$NOTES_FILE" <<'PYEOF'
import re, sys
version = sys.argv[1]
with open("CHANGELOG.md", encoding="utf-8") as f:
    text = f.read()
pat = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)"
m = re.search(pat, text, re.MULTILINE | re.DOTALL)
print((m.group(1).strip() if m else "").strip())
PYEOF

if [[ ! -s "$NOTES_FILE" ]]; then
    fail "extracted CHANGELOG section is empty — check formatting of '## [$VERSION]'"
fi
step "release notes preview (first 20 lines)"
head -20 "$NOTES_FILE"

# 8. tag + push --------------------------------------------------------------
if [[ $FORCE -eq 1 ]] && git rev-parse "$TAG" >/dev/null 2>&1; then
    step "force mode: deleting existing tag + release for $TAG"
    do_or_say gh release delete "$TAG" --yes --cleanup-tag || true
fi

step "tagging $TAG and pushing"
do_or_say git tag -a "$TAG" -m "SafeDrop $TAG"
do_or_say git push origin "$TAG"

# 9. GitHub release ----------------------------------------------------------
step "creating GitHub release"
ARTIFACTS=(
    dist/SafeDrop-ios-${VERSION}-unsigned.ipa
    dist/SafeDrop-android-${VERSION}-debug.apk
    dist/safedrop-${VERSION}-py3-none-any.whl
    dist/safedrop-${VERSION}.tar.gz
)
do_or_say gh release create "$TAG" "${ARTIFACTS[@]}" \
    --title "SafeDrop $TAG" \
    --notes-file "$NOTES_FILE"

rm -f "$NOTES_FILE"

if [[ $DRY_RUN -eq 0 ]]; then
    echo
    echo "✅ released $TAG"
    gh release view "$TAG" --json url -q .url
else
    echo
    echo "ℹ️  dry-run finished. To actually release, re-run without --dry-run."
fi
