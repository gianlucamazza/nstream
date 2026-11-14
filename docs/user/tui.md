# TUI & keyboard cheatsheet

Interactive UI language is **Italian**. Launch from the app menu (`nstream.desktop` →
`nstream-fuzzel` → foot) or run `nstream` in a terminal.

## Home menu

| Entry | Role |
| ----- | ---- |
| In onda · … | Confirmed active cast, independent of watch-history preferences; reopens its panel |
| Cerca… | Search titles; ★ marks watchlisted titles |
| Continua a guardare | Dedicated movie/series resume list with metadata preview |
| Watchlist | Metadata-only local list; Alt-W on a title toggles membership |
| Film / Serie TV | Type-scoped search, continue-watching, catalogs and genres |
| Ricerche recenti | Searches from `library.json` |
| Impostazioni | Cast, P2P, quality defaults, languages and diagnostics |
| Aiuto tasti | Navigation help |

Home and Film/Serie show actions first. Empty collections remain accessible and explain
how to populate them. Every picker has a context title and a persistent keyboard legend;
transient notices occupy a separate header. Leaf lists retain their existing shortcuts.

Film/Serie sections each have popular / new / top IMDb catalogs and **Generi…**. When
unlocked addon manifests (`cfg.addons`, Impostazioni → Fonti) declare browsable catalogs,
those appear under **── cataloghi addon ──** (labels `Addon · name`; Italian chrome, manifest
name as-is). Cinemeta-shaped (`movie` / `series`) catalogs stay in their section; a catalog
whose type is not a board type (today: `anime`) appears in both. Selecting a row opens the
usual browse → play path. Catalogs that require an extra other than `skip` / `genre`
(search-only, …) stay off the board. Addon catalogs that declare `genre` open a genre
picker; those that declare `skip` (and Cinemeta catalogs) show **↓ altri…** for pagination.
No marketplace. A user-configured Trakt catalog addon (Impostazioni → Fonti → **Trakt
(cataloghi)**, or `trakt_addon` / `NSTREAM_TRAKT_ADDON`) appears under **── Trakt ──** —
cronologia / watchlist / liste / popular from that addon, not local continue-watching and
not a stream indexer (ADR 0049).

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

## Preparation and cast panel

The compact panel keeps source lookup, audio preparation and subtitles on separate rows.
It shows title, destination, playback state, source/quality and position/duration when
known. Percentages appear only for measurable work. Filter counts stay short; **d** opens
the bounded detail/notice list while the cast panel is open. Background updates never
write over an open picker.

| Key | Action |
| --- | ------ |
| **Space** | Pause / resume on DMR |
| **← / →** | Seek −30 / +30 seconds on DMR |
| **+ / −** | Cast volume ±5 percentage points on DMR (not TV OSD) |
| **a** | Choose another audio language/source at the current position on a managed DMR cast |
| **d** | View notices and details |
| **s** | Stop the cast and return home |
| **Esc / Ctrl-C** | During preparation: cancel startup. After startup: return home while playback continues |

The active-cast row reopens the same monitor without loading the media again. A cast
started elsewhere can also be observed and controlled without a LOAD; its source list
is unavailable, so audio reselection is hidden. Mirror exposes home, details and stop.
Unavailable receiver values stay unknown, and failed controls show their actual outcome.
The host application must remain running for its in-process services. Returning home
leaves the current item playing; automatic episode advancement continues only while
following the attached playback flow.

Local mpv audio switch: mpv's `#`. Details: [cast.md](cast.md).

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

Menus inherit the terminal foreground/background and ANSI palette. Selection uses reverse
video; titles use emphasis and default symbols occupy one cell. No theme configuration
is required. Narrow terminals reduce panel details and keep the return-home hint visible.

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
