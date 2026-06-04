#!/usr/bin/env bash
# Install nstream: the CLI via `uv tool` (entry point), the fuzzel helper, the desktop
# entry, and a config bootstrapped from the example if missing. Idempotent.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
CFG_DIR="$HOME/.config/nstream"

mkdir -p "$BIN" "$APPS" "$CFG_DIR"

# Drop legacy symlinks from the pre-package layout.
[[ -L "$BIN/nstream" ]] && rm -f "$BIN/nstream"
[[ -L "$BIN/nstream-fuzzel" ]] && rm -f "$BIN/nstream-fuzzel"

# Install the CLI (zero runtime deps, but uv gives an isolated entry point).
uv tool install --force "$REPO"

# Bash helper is not a Python entry point — install it directly.
install -m755 "$REPO/nstream-fuzzel" "$BIN/nstream-fuzzel"

install -m644 "$REPO/nstream.desktop" "$APPS/nstream.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true

if [[ ! -f "$CFG_DIR/config.json" ]]; then
	umask 077
	cp "$REPO/config.example.json" "$CFG_DIR/config.json"
	chmod 600 "$CFG_DIR/config.json"
	echo "Created $CFG_DIR/config.json — edit it and set your Real-Debrid token."
fi

# Claude Code skill (local-dev): symlink SKILL.md into the personal scope so `/nstream`
# can drive the headless CLI. Idempotent; loaded on the next Claude Code session start.
SKILL_DIR="$HOME/.claude/skills/nstream"
mkdir -p "$SKILL_DIR"
ln -sfn "$REPO/skills/nstream/SKILL.md" "$SKILL_DIR/SKILL.md"
echo "Linked nstream skill → $SKILL_DIR/SKILL.md (restart Claude Code to load)."

# Soft deps used by the skill's headless flows.
command -v mpv >/dev/null || echo "warn: mpv assente — la riproduzione locale (--local) non funziona."
command -v catt >/dev/null || echo "warn: catt assente — il cast (--cast) non funziona."
nstream --help 2>/dev/null | grep -q -- --json ||
	echo "warn: la nstream installata non espone --json — reinstalla per la modalità headless."

echo "nstream installed. Run: nstream \"<title>\"  or launch 'nstream' from fuzzel."
