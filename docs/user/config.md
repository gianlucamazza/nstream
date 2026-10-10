# Configuration reference

Path: `$XDG_CONFIG_HOME/nstream/config.json` (default `~/.config/nstream/config.json`).
Written `chmod 600` (`config.save` / `atomic_write`); `load()` also tightens a
world-readable file so a hand-copied config is not left 0644. Template:
[`config.example.json`](../../config.example.json) (defaults match `Config` in
`src/nstream/config.py` — enforced by `tests/test_config_docs.py`).

Edit via `nstream --settings` (fzf menu) or by hand. On first run without a config, nstream
offers a debrid-provider wizard and writes a file.

## Quick start values

| Goal | Set |
| ---- | --- |
| Free P2P (default) | `playback_backend: "local"` + install [TorrServer](https://github.com/YouROK/TorrServer); optional VPN (`p2p_require_vpn`) |
| Instant debrid | `playback_backend: "debrid"` and `torrentio_base: "sort=qualitysize\|realdebrid=YOUR_TOKEN"` (any of the 8 providers) |
| Hybrid | `playback_backend: "auto"` (cached debrid preferred, P2P fallback) |
| Native provider API | `playback_backend: "native"` (TorBox / Premiumize; RealDebrid stays on `debrid` — ADR 0002) |

Provider tokens live only in `torrentio_base` as `<provider>=<KEY>`. Supported keys:
`realdebrid`, `alldebrid`, `premiumize`, `torbox`, `debridlink`, `easydebrid`, `offcloud`,
`putio`. RealDebrid token: <https://real-debrid.com/apitoken>.

## Playback backends

| Value | Behaviour |
| ----- | --------- |
| `local` (default) | Token-less torrent discovery; stream via local TorrServer (LAN HTTP URL for mpv/cast) |
| `debrid` | Ready debrid URLs from Torrentio (needs provider key in `torrentio_base`) |
| `auto` | Merge debrid + pure-torrent by filename; prefer cached, fall back to P2P |
| `native` | Pure-torrent discovery; resolve via provider API (`debrid.py`); P2P fallback |

> **P2P privacy.** Local / auto / native P2P joins the swarm — your IP is visible to peers.
> nstream shows a one-time notice (`p2p_ack`), warns without a VPN, and can refuse P2P
> (`p2p_require_vpn: true`). Bind TorrServer to the VPN interface; do not rely on “VPN is on”
> alone. Debrid paths do not expose your IP to peers. See ADR 0032 for the privacy gate.

## Stream sources

| Key | Default | Meaning |
| --- | ------- | ------- |
| `torrentio_enabled` | `true` | Built-in Torrentio provider |
| `addons` | `[]` | Extra Stremio manifest URLs (stream / subtitle / catalog) |
| `trakt_addon` | `""` | Trakt *catalog* manifest URL (ADR 0049). Env `NSTREAM_TRAKT_ADDON` wins. Not an indexer. |
| `torrentio_base` | `"sort=qualitysize"` | Torrentio config path segment (sort + optional debrid key) |

Manage sources from **settings → Fonti stream / plugin** (toggle Torrentio, curated presets
Comet / MediaFusion / AIOStreams / TorrentsDB, paste a user-generated `…/manifest.json`, or
**Trakt (cataloghi)** — ADR 0049, not a stream indexer). Presets open the public configure
page; **you** paste the URL (tokens in the path — never hard-coded). Aggregation and
fuse/dedup: ADR 0024.

## Keys by group

Defaults and bounds come from `Config` / `INT_BOUNDS` / `_ENUM_VALUES` in `config.py`.

### Sources & metadata

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `torrentio_base` | str | `sort=qualitysize` | Debrid segment optional |
| `cinemeta` | str | `https://v3-cinemeta.strem.io` | Metadata / catalogs |
| `opensubtitles` | str | `https://opensubtitles-v3.strem.io` | Subtitle addon |
| `torrentio_enabled` | bool | `true` | |
| `addons` | list[str] | `[]` | Manifest URLs |
| `trakt_addon` | str | `""` | Trakt catalog `…/manifest.json` (token in the path stays here or in `NSTREAM_TRAKT_ADDON`). Empty = off. Not a stream source. |

### Languages

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `subtitle_langs` | list[str] | `["ita","eng"]` | Sort/auto-pick + mpv `--slang` |
| `audio_langs` | list[str] | `["ita","eng"]` | Preference order; drives score `lang` + `--alang` |
| `primary_lang` | str | `""` | Native language; `""` = first of `audio_langs` |

`primary_lang` drives auto-pick guards: Dual/MULTI releases are ffprobe-confirmed; missing
primary audio → try next source or play fallback with primary-language safety subtitles.

### Local playback (mpv)

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `hwdec` | str | `auto-safe` | Injected unless you set a concrete method in `mpv.conf`/`mpv_args`; `auto*` upgraded to real VAAPI via vainfo; `""` disables injection |
| `mpv_quiet` | bool | `true` | Quiet track/decoder noise unless you set `msg-level` |
| `mpv_args` | list[str] | `[]` | Extra mpv flags |
| `auto_play` | bool | `true` | Enter auto-picks; Tab = manual for that title |
| `autoplay` | bool | `true` | Next-episode overlay (Lua script via `--script`) |
| `autoplay_lead` | int | `15` | Seconds before end; bounds 1–120 |
| `history_enabled` | bool | `true` | Resume / continue-watching |

### Cast

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `prefer_cast` | bool | `false` | Default destination Chromecast instead of mpv |
| `cast_device` | str | `""` | Preferred device **name**; dynamic discovery (ADR 0010) |
| `cast_receiver_app_id` | str | `""` | Custom Cast application id (ADR 0013). Empty keeps `CC1AD845`. **castbridge only** — catt cannot launch an arbitrary id and stays on the Default Media Receiver (`receiver_app_ignored`, ADR 0045). |
| `cast_mode` | enum | `dmr` | `dmr` \| `mirror` |
| `cast_remux` | bool | `true` | Tier-2 host remux for Dolby/DTS (ADR 0005) |
| `cast_live` | bool | `true` | Tier-2 as a live HLS-TS playlist: starts in seconds, stereo AAC (ADR 0039); `false` = complete-file remux only |
| `cast_lan_proxy` | bool | `true` | Range-serve a remote debrid url from the host LAN (ADR 0045 Phase 1). Video stays native. `false` = old WAN-direct (debug) |
| `cast_audio_codec` | str | `aac` | Remux target audio |
| `cast_remux_max_resolution` | int | `1080` | Cap **remuxed** picks only; `0` = no cap; bounds 0–4320 |
| `cast_remux_max_size_gb` | int | `20` | Demote / confirm oversized remux; bounds 0–1000 |
| `cast_mirror_over_remux_gb` | int | `10` | Auto-prefer mirror when remux would exceed this GB (ADR 0015); `0` = off; bounds 0–1000 |
| `mirror_bitrate` | int | `0` | `0` = built-in (~16 Mbps); bounds 0–1e8 |
| `mirror_playout_ms` | int | `0` | `0` = built-in (500 ms); bounds 0–5000 |

Full cast behaviour: [cast.md](cast.md).

### Stream filter & ranking

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `hw_filter` | bool | `true` | Hardware-aware ranking master switch |
| `max_resolution` | int | `2160` | GPU safety ceiling; `0` = none; bounds 0–4320 |
| `allow_software` | bool | `false` | Keep codecs without HW decode |
| `allow_dv5` | bool | `false` | Keep Dolby Vision Profile 5 |
| `lang_filter` | bool | `true` | Demote non-preferred-only tagged releases |
| `exclude_camrip` | bool | `true` | CAM/TS/TC/SCR → ⚠ section |
| `min_seeders` | int | `3` | Demote uncached below this; `0` = off; bounds 0–100 |
| `dedup` | bool | `true` | Collapse same release across trackers |
| `max_streams` | int | `20` | Manual menu cap; `0` = none; bounds 0–500 |
| `default_quality` | int\|null | `null` | `null` = TUI asks each title; `0` = Auto without picker; `N` = exact res (TUI + headless + binge) |
| `home_continue_max` | int | `12` | Continue rows on home before “…altri”; `0` = all |

Per-invocation `--quality` is **not** a config default — use the CLI flag or TUI picker.
Details: [../selection.md](../selection.md).

### Engine (TorrServer / P2P)

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `playback_backend` | enum | `local` | `local` \| `debrid` \| `auto` \| `native` |
| `engine_port` | int | `8090` | bounds 1024–65535 |
| `engine_cache_mb` | int | `256` | bounds 32–4096 |
| `engine_download_dir` | str | `""` | `""` → `$XDG_CACHE_HOME/nstream/torrents` |
| `p2p_ack` | bool | `false` | One-time privacy notice acknowledged |
| `p2p_require_vpn` | bool | `false` | Refuse P2P without VPN interface |

### Subtitles (alignment)

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `sub_align` | bool | `true` | Native audio-anchored alignment (ADR 0020) |
| `sub_align_budget_s` | int | `240` | Max seconds for alignment work; bounds 60–600 |

### TUI appearance

| Key | Type | Default | Notes |
| --- | ---- | ------- | ----- |
| `nerd_font` | enum | `auto` | `auto` \| `on` \| `off` |
| `posters` | bool | `true` | fzf preview posters (`chafa`) |
| `image_mode` | enum | `auto` | `auto` \| `off` |

## Related paths (not in config.json)

| Path | Role |
| ---- | ---- |
| `$XDG_STATE_HOME/nstream/history.json` | Watch progress / resume |
| `$XDG_STATE_HOME/nstream/library.json` | Recent searches + metadata-only watchlist |
| `$XDG_STATE_HOME/nstream/dead-sources.json` | Proven-gone denylist (ADR 0025); `--forget-dead` |
| `$XDG_STATE_HOME/nstream/addon-breakers.json` | Per-addon circuit breakers (ADR 0027); `--forget-breakers` |
| `$XDG_STATE_HOME/nstream/nstream.log` | Rotating log (redacted) |
| `$XDG_STATE_HOME/nstream/torrserver.log` | Spawned TorrServer output |
| `$XDG_CACHE_HOME/nstream/` | manifests, hwcaps, devices, meta, posters, torrents |

No stream URL, debrid token, or Trakt session is stored outside `config.json` (or
`NSTREAM_TRAKT_ADDON` in the environment).
