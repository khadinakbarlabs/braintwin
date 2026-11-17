#!/usr/bin/env bash
# Braintwin: one-step install for your own computer.
# Installs into ~/.braintwin (its own private Python environment) and prints
# the lines to paste into your AI tool. Nothing is sent anywhere.
set -euo pipefail

HOME_DIR="${BRAINTWIN_HOME:-$HOME/.braintwin}"
REPO_URL="${BRAINTWIN_REPO:-https://github.com/khadinakbarlabs/braintwin.git}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

say() { printf '%s\n' "$*"; }
fail() { printf 'Braintwin install stopped: %s\n' "$*" >&2; exit 1; }

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
      PY="$candidate"
      break
    fi
  fi
done
[ -n "$PY" ] || fail "Python 3.11 or newer is needed. Get it from https://www.python.org/downloads/ and run this again."

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -f "$SCRIPT_DIR/my_actor/stdio.py" ]; then
  SOURCE="$SCRIPT_DIR"
elif [ -n "$REPO_URL" ] && [ "$REPO_URL" != "__REPO""_URL__" ]; then
  command -v git >/dev/null 2>&1 || fail "git is needed to download Braintwin."
  SOURCE="git+$REPO_URL"
else
  fail "run this script from inside the Braintwin folder (the one with pyproject.toml)."
fi

say "Installing Braintwin into $HOME_DIR ..."
mkdir -p "$HOME_DIR"
"$PY" -m venv "$HOME_DIR/venv"
"$HOME_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$HOME_DIR/venv/bin/python" -m pip install --quiet --upgrade "$SOURCE"

BIN="$HOME_DIR/venv/bin/braintwin"
[ -x "$BIN" ] || fail "the install finished but $BIN is missing."

cat <<EOF

Braintwin is installed.

Your memories live in one private file on this computer:
  ~/.second-brain/brain.sqlite

Connect an AI tool (pick yours):

  Claude Code:
    claude mcp add braintwin -- $BIN

  Cursor (~/.cursor/mcp.json) and Claude Desktop (claude_desktop_config.json):
    { "mcpServers": { "braintwin": { "command": "$BIN" } } }

  Codex (~/.codex/config.toml):
    [mcp_servers.braintwin]
    command = "$BIN"

Restart the tool after adding it.

Check it works:
  $HOME_DIR/venv/bin/braintwin-cli health

Update later: run this installer again.
Remove: delete $HOME_DIR (your memories stay in ~/.second-brain).
EOF
