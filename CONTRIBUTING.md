# Contributing to SafeDrop

Thanks for considering a contribution! SafeDrop started as a course
final project and has grown into a cross-platform, AI-agent-aware
LAN sharing fabric. We welcome bug reports, feature ideas, and pull
requests.

## Ways to contribute

- **Bug reports** — open an issue with steps to reproduce, your OS /
  device versions, and any relevant log output.
- **Feature ideas** — open a discussion or issue first; we'll talk
  about scope before you write code, so nothing gets wasted.
- **Pull requests** — read the [Development setup](#development-setup)
  + [Style + testing](#style--testing) sections below, then send the PR.
- **Documentation** — fixing typos, clarifying examples, translating
  docs to other languages are all welcome.

## Development setup

```bash
git clone https://github.com/gclinian/SafeDrop.git
cd SafeDrop

python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'

# Run the MCP server (works on any Python 3.10+)
.venv/bin/safedrop-mcp --help

# Run the desktop GUI (tkinter required — see note below)
.venv/bin/python run.py
```

> **macOS tkinter.** Only the desktop GUI needs tkinter; the CLI / MCP
> server / tests don't. If `python3 -c "import tkinter"` fails on your
> Mac (common with Homebrew Python), grab
> [python.org's installer](https://www.python.org/downloads/macos/) or
> `brew install python-tk@3.12` and build the venv with that
> interpreter instead.

For Android:

```bash
cd android
./gradlew assembleDebug                       # debug APK
./gradlew installDebug                        # install to attached device/emulator
```

For iOS:

```bash
cd ios
xcodegen generate                             # creates SafeDrop.xcodeproj
xcodebuild -project SafeDrop.xcodeproj \
           -scheme SafeDrop \
           -destination 'platform=iOS Simulator,name=iPhone 17' \
           build
```

## Repository layout

```
safedrop/         Python core: crypto, discovery, transfer, CLI, tkinter GUI
safedrop_mcp/     Python MCP server: stdio + HTTP, policy, tokens, bridge
android/          Native Kotlin / Jetpack Compose client
ios/              Native Swift / SwiftUI client (xcodegen-managed)
tests/            38 Python tests (unit + e2e + cross-language interop)
spec.md           Protocol specification
MCP_AGENT_GUIDE.md      Agent integration walkthrough
REAL_DEVICE_TESTING.md  Manual QA checklist for real-hardware deploys
```

## Protocol contract

The wire protocol is the **single source of truth** binding the three
languages (Python / Kotlin / Swift). Any change that touches it must:

1. Be discussed in an issue first.
2. Update [`spec.md`](spec.md) with the new format.
3. Land in all three implementations *in the same PR* (or sequenced
   PRs that don't break the cross-language interop tests).
4. Include a regression case in `tests/test_*_interop.py`.

The interop tests (`test_android_interop.py`, `test_android_tools_interop.py`)
work just as well against the iOS Simulator — point them at the right
forwarded port. They are the gold standard for byte-for-byte compat.

## Style + testing

### Python

- Type hints encouraged, especially on public APIs and protocol surfaces.
- Tests use stdlib `unittest`. Run the whole suite with:

  ```bash
  .venv/bin/python -m unittest discover -s tests
  ```

- For protocol changes, add at least one e2e test (two `HeadlessSafeDrop`
  instances or one `safedrop-mcp` subprocess + the official `mcp` client SDK).

### Kotlin / Android

- Follow the existing patterns in `android/app/src/main/java/com/safedrop/android/`.
- Compose UI lives in `ui/`, networking in `net/`, persistence in `data/`.
- Build with `./gradlew assembleDebug`; the wrapper is checked in.

### Swift / iOS

- The project is xcodegen-managed — edit `ios/project.yml`, not the
  generated `.xcodeproj` (which is .gitignored).
- All blocking I/O happens on a dedicated `DispatchQueue`, not on
  Swift's cooperative thread pool. Follow that pattern for any new
  network code.

## Pull request checklist

Before opening a PR:

- [ ] `python -m unittest discover -s tests` is green
- [ ] `android/ ./gradlew assembleDebug` succeeds
- [ ] `cd ios && xcodebuild ... build` succeeds (if you touched Swift)
- [ ] Protocol changes are reflected in `spec.md`
- [ ] User-visible changes are mentioned in `CHANGELOG.md`
- [ ] If you added a new dependency, it's in `pyproject.toml` (Python)
  or the relevant gradle/SPM file — not duplicated elsewhere.

## Security disclosures

Don't open a public issue for security bugs. Email the maintainer
listed in the repo profile, or use GitHub's private vulnerability
reporting. We aim to acknowledge within 72 hours.

## License

By contributing, you agree that your contributions will be licensed
under the [MIT License](LICENSE).
