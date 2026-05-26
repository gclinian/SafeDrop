#!/usr/bin/env bash
#
# SafeDrop one-key installer for macOS + Linux.
#
# Usage (one-liner from the README):
#
#   curl -fsSL https://raw.githubusercontent.com/gclinian/SafeDrop/main/scripts/install.sh | bash
#
# Or, after a `git clone`:
#
#   ./scripts/install.sh
#
# What this does (and what it does NOT do):
#
#   * Verifies you have Python 3.10+ on PATH.
#   * Verifies the tkinter stdlib module imports — that's the one Homebrew
#     Python likes to ship without. Prints exactly which fix you need.
#   * Creates a hermetic virtualenv at $SAFEDROP_HOME/venv
#     (default: ~/.local/share/safedrop/venv) — no global pollution.
#   * Installs safedrop + the [mcp] extra from PyPI (or from the latest
#     GitHub Release wheel if --from-release is passed).
#   * Drops a `safedrop-gui` launcher into $SAFEDROP_BIN
#     (default: ~/.local/bin). Add that to your PATH if it's not already.
#
# What it does NOT do:
#
#   * Doesn't install Python for you. If you don't have one, the script
#     prints the platform-specific install command and exits.
#   * Doesn't touch /usr/local/bin or any system path.
#   * Doesn't sudo. If you see a sudo prompt, the script has a bug.
#   * Doesn't open the firewall. Allow Python through the macOS firewall
#     dialog on first launch.
#
# Re-running this script is safe — it upgrades in place.

set -euo pipefail

SAFEDROP_HOME="${SAFEDROP_HOME:-$HOME/.local/share/safedrop}"
SAFEDROP_BIN="${SAFEDROP_BIN:-$HOME/.local/bin}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
FROM_RELEASE=0
RELEASE_TAG=""

# ---- terminal pretty-printing -------------------------------------

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
    BLUE=$(printf '\033[34m'); YELLOW=$(printf '\033[33m')
    RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m')
    RESET=$(printf '\033[0m')
else
    BOLD=""; DIM=""; BLUE=""; YELLOW=""; RED=""; GREEN=""; RESET=""
fi

step() { echo "${BLUE}==>${RESET} ${BOLD}$1${RESET}"; }
warn() { echo "${YELLOW}warn:${RESET} $1" >&2; }
fail() { echo "${RED}error:${RESET} $1" >&2; exit 1; }
done_ok() { echo "${GREEN}ok${RESET}  $1"; }

# ---- args ---------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --from-release) FROM_RELEASE=1; shift ;;
        --release-tag)  RELEASE_TAG="$2"; FROM_RELEASE=1; shift 2 ;;
        --python)       PYTHON_BIN="$2"; shift 2 ;;
        --home)         SAFEDROP_HOME="$2"; shift 2 ;;
        --bin)          SAFEDROP_BIN="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,/^set -/p' "$0" | sed 's/^#\s\?//' | sed '$d'
            exit 0 ;;
        *) fail "unknown argument: $1 (use --help to see options)" ;;
    esac
done

# ---- 1. python check ---------------------------------------------

step "checking Python interpreter"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    case "$(uname -s)" in
        Darwin) hint="Install from https://www.python.org/downloads/macos/  (recommended — ships with tkinter)\n        or:  brew install python-tk@3.12";;
        Linux)  hint="On Debian/Ubuntu:  sudo apt install python3 python3-venv python3-tk\nOn Fedora:          sudo dnf install python3 python3-tkinter";;
        *)      hint="Install Python 3.10+ for your OS.";;
    esac
    fail "Python interpreter '$PYTHON_BIN' not found on PATH.

$hint

Then re-run this script (or pass --python /full/path/to/python3)."
fi

PY_VER=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJ=$(echo "$PY_VER" | cut -d. -f1)
PY_MIN=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJ" -lt 3 ] || { [ "$PY_MAJ" -eq 3 ] && [ "$PY_MIN" -lt 10 ]; }; then
    fail "Python $PY_VER is too old. SafeDrop needs Python 3.10 or newer."
fi
done_ok "Python $PY_VER at $("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"

# ---- 2. tkinter check ---------------------------------------------

step "checking tkinter (needed for the desktop GUI)"
if ! "$PYTHON_BIN" -c "import tkinter" 2>/dev/null; then
    case "$(uname -s)" in
        Darwin)
            warn "tkinter not available in this Python."
            cat <<EOF >&2

  Homebrew's python3 ships *without* Tk. Two ways to fix it, in order
  of preference:

    1.  Install python.org's distribution from
        https://www.python.org/downloads/macos/  — it bundles Tk.
        Then re-run this script with:
            ./scripts/install.sh --python /usr/local/bin/python3.12
        (or whatever python.org installed.)

    2.  brew install python-tk@3.12
        (then re-run; it should pick up the right Tk.)

  The CLI and MCP server work without tkinter — you can still run:
        $PYTHON_BIN -m pip install safedrop[mcp]
        safedrop ls
        safedrop-mcp
  …but the python run.py GUI won't launch until tkinter is available.

EOF
            ;;
        Linux)
            warn "tkinter not available in this Python."
            cat <<EOF >&2

  Most distros ship tkinter as a separate package:
    Debian / Ubuntu:  sudo apt install python3-tk
    Fedora:           sudo dnf install python3-tkinter
    Arch:             sudo pacman -S tk

  Then re-run this script. CLI + MCP work without tkinter; only the GUI needs it.

EOF
            ;;
        *)
            warn "tkinter missing — install your platform's python-tk package, then re-run."
            ;;
    esac
    fail "tkinter is required for the SafeDrop GUI. See the note above."
fi
done_ok "tkinter $("$PYTHON_BIN" -c 'import tkinter; print(tkinter.TkVersion)') available"

# ---- 3. create venv -----------------------------------------------

VENV="$SAFEDROP_HOME/venv"
step "creating venv at $DIM$VENV$RESET"
mkdir -p "$SAFEDROP_HOME"
if [ ! -f "$VENV/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV"
fi
done_ok "venv ready"

# ---- 4. install safedrop ------------------------------------------

step "installing safedrop into the venv"
"$VENV/bin/pip" install --upgrade --quiet pip
if [ "$FROM_RELEASE" -eq 1 ]; then
    if [ -z "$RELEASE_TAG" ]; then
        # Pick latest release tag from the GitHub API.
        if command -v curl >/dev/null 2>&1; then
            RELEASE_TAG=$(curl -fsSL https://api.github.com/repos/gclinian/SafeDrop/releases/latest \
                          | grep '"tag_name"' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
        fi
        [ -z "$RELEASE_TAG" ] && fail "could not determine latest release tag"
    fi
    VERSION=${RELEASE_TAG#v}
    WHEEL="safedrop-${VERSION}-py3-none-any.whl"
    URL="https://github.com/gclinian/SafeDrop/releases/download/${RELEASE_TAG}/${WHEEL}"
    step "fetching $URL"
    TMPDIR=$(mktemp -d)
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --progress-bar -o "$TMPDIR/$WHEEL" "$URL"
    else
        wget -O "$TMPDIR/$WHEEL" "$URL"
    fi
    "$VENV/bin/pip" install --quiet "$TMPDIR/$WHEEL[mcp]"
    rm -rf "$TMPDIR"
else
    "$VENV/bin/pip" install --quiet --upgrade 'safedrop[mcp]'
fi
INSTALLED_VERSION=$("$VENV/bin/safedrop" --help 2>/dev/null | head -1 || echo "unknown")
done_ok "installed safedrop"

# ---- 5. launcher --------------------------------------------------

step "creating launcher at $DIM$SAFEDROP_BIN/$RESET"
mkdir -p "$SAFEDROP_BIN"
cat > "$SAFEDROP_BIN/safedrop-gui" <<EOF
#!/usr/bin/env bash
# Auto-generated by SafeDrop install.sh — launch the desktop GUI.
exec "$VENV/bin/python" -m safedrop "\$@"
EOF
chmod +x "$SAFEDROP_BIN/safedrop-gui"

# Symlink the CLI + MCP + agent + beacon entry points so `safedrop ls`
# works without activating the venv.
for tool in safedrop safedrop-mcp safedrop-mcp-tokens safedrop-agent safedrop-beacon; do
    if [ -x "$VENV/bin/$tool" ]; then
        ln -sf "$VENV/bin/$tool" "$SAFEDROP_BIN/$tool"
    fi
done
done_ok "launchers in $SAFEDROP_BIN/"

# ---- 6. final report ----------------------------------------------

case ":$PATH:" in
    *":$SAFEDROP_BIN:"*) IN_PATH=1 ;;
    *)                   IN_PATH=0 ;;
esac

echo
echo "${BOLD}${GREEN}SafeDrop is installed.${RESET}"
echo
echo "  GUI:        safedrop-gui"
echo "  CLI:        safedrop ls"
echo "  MCP:        safedrop-mcp        (point Claude Code / Cursor at this)"
echo "  agent:      ANTHROPIC_API_KEY=... safedrop-agent"
echo "  beacon:     safedrop-beacon --bind 127.0.0.1:47900"
echo
if [ "$IN_PATH" -eq 0 ]; then
    case "$(basename "${SHELL:-/bin/bash}")" in
        zsh)  RC="~/.zshrc" ;;
        fish) RC="~/.config/fish/config.fish" ;;
        *)    RC="~/.bashrc" ;;
    esac
    echo "${YELLOW}Note:${RESET} $SAFEDROP_BIN is not on your PATH."
    echo "      Add this line to $RC :"
    echo
    echo "        export PATH=\"$SAFEDROP_BIN:\$PATH\""
    echo
    echo "      …then open a new terminal. Or run safedrop with the full path right now:"
    echo "        $SAFEDROP_BIN/safedrop-gui"
fi
