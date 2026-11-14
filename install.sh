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
# --reinstall: uv caches the built wheel by version, so without it an unchanged
# version number silently deploys a STALE build of the local checkout.
uv tool install --force --reinstall "$REPO"

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

# Omarchy bar widget: copied, not linked — Omarchy refuses symlinked plugins and its
# hot-reload watches the plugin directory itself. Enabling it stays the user's call.
OMARCHY_PLUGINS="$HOME/.config/omarchy/plugins"
if [[ -d "$HOME/.config/omarchy" ]]; then
	PLUGIN_SRC="$REPO/integrations/omarchy-nstream"
	PLUGIN_DST="$OMARCHY_PLUGINS/gianlucamazza.nstream"
	mkdir -p "$PLUGIN_DST/i18n"
	for f in manifest.json Panel.qml helper.py LICENSE README.md; do
		install -m644 "$PLUGIN_SRC/$f" "$PLUGIN_DST/$f"
	done
	install -m644 "$PLUGIN_SRC"/i18n/*.json "$PLUGIN_DST/i18n/"
	echo "Installed Omarchy widget → $PLUGIN_DST (enable: omarchy plugin enable gianlucamazza.nstream)."
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
