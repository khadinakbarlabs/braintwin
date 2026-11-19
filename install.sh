#!/usr/bin/env bash
# Braintwin: one-step install for your own computer.
# Installs into a private Python environment and optionally configures one named client.
set -euo pipefail
umask 077

HOME_DIR="${BRAINTWIN_HOME:-$HOME/.braintwin}"
REPO_URL="${BRAINTWIN_REPO:-https://github.com/khadinakbarlabs/braintwin.git}"
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

say() { printf '%s\n' "$*"; }
fail() { printf 'Braintwin install stopped: %s\n' "$*" >&2; exit 1; }

CLIENT="${1:-}"
STORAGE="${3:-local}"
if [ "$#" -gt 0 ]; then
  case "$CLIENT" in claude-code|codex|cursor|gemini-cli) ;; *) fail "choose claude-code, codex, cursor or gemini-cli." ;; esac
  if [ "$#" -ne 1 ] && { [ "$#" -ne 3 ] || [ "$2" != "--storage" ]; }; then
    fail "use install.sh CLIENT [--storage local|apify]."
  fi
  case "$STORAGE" in local|apify) ;; *) fail "choose local or apify storage." ;; esac
fi

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

CLI="$HOME_DIR/venv/bin/braintwin-cli"
if [ -n "$CLIENT" ]; then
  if [ "$STORAGE" = apify ]; then
    say "Selected preferences and decisions will go to your Apify account. Hosted memory charges apply."
    say "Anyone with an active Actor container URL can access its store. Choose local storage for sensitive memories."
    say "Apify requests full API access. Your system keyring holds the authorization."
  fi
  "$CLI" connect "$CLIENT" --storage "$STORAGE"
  say "Restart $CLIENT and allow its Braintwin hooks when prompted."
  if [ "$CLIENT" = codex ]; then say "In Codex, open /hooks and review the Braintwin commands."; fi
  say 'Try a normal message: I prefer weekly status reports on Fridays.'
  if [ "$STORAGE" = apify ]; then
    say "Open Memories or Waiting for review at https://braintwin.dev/app/ with the same account."
    say "See the last eligible save: $CLI connection-status $CLIENT"
  else
    say "In a new session in the same project, ask: When do I prefer weekly reports?"
  fi
  say "Disconnect: $CLI disconnect $CLIENT"
else
  say "Braintwin is installed. Add your connector and automatic saving together (choose your tool):"
  for tool in claude-code codex cursor gemini-cli; do say "  $CLI connect $tool"; done
  say "For an Apify account, add --storage apify. For manual MCP setup, the executable is $BIN."
  say "Setup guides: https://braintwin.dev/guides/"
fi
