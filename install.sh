#!/usr/bin/env bash
# Braintwin: one-step install for your own computer.
# Installs into a private Python environment and connects every AI coding tool it finds (or one named tool).
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

CLIENT="${1:-all}"
STORAGE="${3:-apify}"
[ "$#" -gt 0 ] || set -- all
case "$CLIENT" in all|claude-code|codex|cursor|gemini-cli) ;; *) fail "choose all, claude-code, codex, cursor or gemini-cli." ;; esac
if [ "$#" -ne 1 ] && { [ "$#" -ne 3 ] || [ "$2" != "--storage" ]; }; then
  fail "use install.sh [all|CLIENT] [--storage apify|local]."
fi
case "$STORAGE" in local|apify) ;; *) fail "choose apify or local storage." ;; esac

# Which AI tools are on this computer: the command, or the tool's own settings folder.
found_tool() {
  case "$1" in
    claude-code) command -v claude >/dev/null 2>&1 || [ -d "$HOME/.claude" ] ;;
    codex) command -v codex >/dev/null 2>&1 || [ -d "$HOME/.codex" ] ;;
    cursor) command -v cursor >/dev/null 2>&1 || command -v cursor-agent >/dev/null 2>&1 || [ -d "$HOME/.cursor" ] ;;
    gemini-cli) command -v gemini >/dev/null 2>&1 || [ -d "$HOME/.gemini" ] ;;
  esac
}

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
TOOLS=""
if [ "$CLIENT" = all ]; then
  for tool in claude-code codex cursor gemini-cli; do
    if found_tool "$tool"; then TOOLS="$TOOLS $tool"; fi
  done
else
  TOOLS=" $CLIENT"
fi
if [ -z "$TOOLS" ]; then
  say "Braintwin is installed, but no Claude Code, Codex, Cursor or Gemini CLI was found on this computer."
  say "Install one of them, then run: $CLI connect claude-code   (or codex, cursor, gemini-cli)"
  say "Chat apps such as Claude and ChatGPT connect with this address instead: https://braintwin.dev/mcp/"
  exit 0
fi
if [ "$STORAGE" = apify ]; then
  say "Your memories will be kept in your own Apify account and shown at https://braintwin.dev/app/."
  say "Saving or changing a memory costs \$0.01. Reading is free. For a computer-only memory, add --storage local."
  say "Anyone with an active Actor container URL can access its store. Choose local storage for sensitive memories."
  say "Apify asks you to sign in once in your browser. Your system keyring keeps the sign-in for every tool."
fi
CONNECTED=""
for tool in $TOOLS; do
  say "Connecting $tool ..."
  if "$CLI" connect "$tool" --storage "$STORAGE"; then CONNECTED="$CONNECTED $tool"; else say "Could not connect $tool. The others continue."; fi
done
[ -n "$CONNECTED" ] || fail "no tool could be connected."
say "Connected:$CONNECTED. Restart them so they pick up Braintwin."
case "$CONNECTED" in *codex*) say "In Codex, open /hooks once and allow the Braintwin commands." ;; esac
say 'From now on, normal messages are saved and recalled on their own. Try: I prefer weekly status reports on Fridays.'
if [ "$STORAGE" = apify ]; then
  say "See what was saved: https://braintwin.dev/app/"
else
  say "In a new session in the same project, ask: When do I prefer weekly reports?"
fi
say "Disconnect one tool: $CLI disconnect TOOL"
