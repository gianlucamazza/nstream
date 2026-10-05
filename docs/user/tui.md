# TUI & keyboard cheatsheet

Interactive UI language is **Italian**. Launch from the app menu (`nstream.desktop` →
`nstream-fuzzel` → foot) or run `nstream` in a terminal.

## Home menu

| Entry | Role |
| ----- | ---- |
| 📺 In onda · … | Active cast session (status / pause / seek / volume / stop) on home **and** Film/Serie |
| Continue-watching | Mixed movies + series from history (capped by `home_continue_max`; “…altri” expands) |
| 🔍 Search | Type inside fzf; accent-insensitive ranking; ★ marks watchlisted titles |
| Recent searches | From `library.json` |
| ★ Watchlist | Metadata-only local list (no stream URLs) |
| 🎬 Film / 📺 Serie TV | Type-scoped continue, search, catalogs, **Generi…** |
| ⚙ Impostazioni | Settings (`nstream --settings`) — cast, P2P, quality default, diagnostics |
| Aiuto tasti | Tab / Alt-C / Alt-W / Ctrl-/ / ESC |

Film/Serie sections each have popular / new / top IMDb catalogs and **Generi…**. When
unlocked addon manifests (`cfg.addons`, Impostazioni → Fonti) declare browsable catalogs,
those appear under **── cataloghi addon ──** (labels `Addon · name`; Italian chrome, manifest
name as-is). Cinemeta-shaped (`movie` / `series`) catalogs stay in their section; a catalog
whose type is not a board type (today: `anime`) appears in both. Selecting a row opens the
usual browse → play path. Catalogs that require an extra other than `skip` / `genre`
(search-only, …) stay off the board. Addon catalogs that declare `genre` open a genre
picker; those that declare `skip` (and Cinemeta catalogs) show **↓ altri…** for pagination.
No marketplace, no new config key.

Multi-season series open a **season menu** before episodes. ESC steps back one level;
after play (or no sources) you return to the list (app does not quit).

## Leaf-list keys

| Key | Action |
| --- | ------ |
| **Enter** | Play (auto-pick when `auto_play`; else stream menu) |
| **Tab** | Manual mode for this pick: stream menu → pre-play tracks |
| **Alt-C** | Cast this pick (device picker if needed); during mpv = move to TV |
| **Alt-W** | Toggle watchlist for selected title |
| **Ctrl-/** | Toggle preview pane |
| **ESC** | Back / cancel (`None` — distinct from fzf sentinels) |

## Quality picker

Without `--quality` and without config `default_quality`, the TUI shows an in-flow quality
picker (Auto + resolutions present) before auto-pick or stream menu. Settings → **Qualità
predefinita** can skip the picker (Auto, fixed tier, or ask every time). Series binge keeps
the first choice sticky via `VettedStream.quality`. CLI: `nstream --quality 1080 "…"`
(aliases: `4k`/`2160`, `fhd`/`1080`, `hd`/`720`, `sd`/`480`, `auto`). If a fixed tier has no
playable stream, a sticky notice lists available resolutions (same idea as headless
`quality_unavailable`).

## Manual mode (Tab)

1. **Stream menu** — ranked playable rows; ⚠ section for excluded; header shows filter notices;
   capped by `max_streams` with “show all”.
2. **Pre-play** — `▶ Avvia · 🔊 Audio · 💬 Sottotitoli` (ffprobe → mpv `--aid`/`--sid` or
   OpenSubtitles). `▶ Avvia` is default (one Enter with language preference). Without
   `ffprobe`, silent fallback to mpv defaults.

Flip default with **Riproduzione automatica** (`auto_play`). Binge auto-advance always
auto-plays. Force auto once: `--play`.

## Cast keys (while casting)

| Key | Action |
| --- | ------ |
| **a** | Re-cast another release in a different audio language (from current position) |

Local mpv audio switch: mpv’s `#`. Details: [cast.md](cast.md).

## Series binge

Near the end, a bundled Lua overlay (`nstream.lua` via `--script`) offers next episode.
Controlled by `autoplay` / `autoplay_lead` / `--no-autoplay`. Does not modify your `mpv.conf`.
If you set `--end` in `mpv_args`, the overlay will not appear. With `keep-open=yes`, advance
still works via `time-pos` / `end-file`. Policy: ADR 0029.

## Settings menu

`nstream --settings` (or ⚙ in home): languages, autoplay, hwdec, history, debrid provider +
masked key, stream sources, cast device discovery, filter knobs, playback backend, etc.
On first run without config, a debrid wizard may write the initial file.

## Appearance

| Config | Effect |
| ------ | ------ |
| `nerd_font` | Glyph set: `auto` / `on` / `off` |
| `posters` | Poster thumbnails in fzf preview (`chafa`) |
| `image_mode` | Terminal image protocol: `auto` / `off` |
| `NO_COLOR` | Honoured by `ui` design system |

## Related

- Ranking: [../selection.md](../selection.md)
- Config: [config.md](config.md)
- Headless (no fzf): [../headless.md](../headless.md)
