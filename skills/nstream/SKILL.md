---
name: nstream
description: >
  Play or cast movies and TV series headlessly via the nstream CLI. Use when the user wants to
  watch / put on / stream a specific film or series — locally on this laptop (mpv) or on the TV
  (Chromecast), and to stop/check/set-volume on the cast it started. Always non-interactive:
  drives `nstream --json` (no fzf, no TTY) and parses the JSON result. For mirroring the desktop
  or casting an arbitrary file/URL use the `skill-cast` skill instead. Trigger keywords: metti sul TV,
  guarda, riproduci, streaming film, casta il film, metti la serie, guarda l'episodio, mettimi un
  film, put on the TV, watch, play movie, stream series, cast the movie, watch the episode.
---

# nstream — headless play / cast

Drive the local `nstream` CLI in **`--json`** mode. **Contract (actions, fields, error codes,
guarantees):** repository file `docs/headless.md` — treat it as authoritative; this skill is
the agent playbook and must not contradict it.

## Before anything

- `command -v nstream` — if missing: `./install.sh` in the nstream repo (`uv tool install`).
- Confirm: `nstream --help | grep -q -- --json`. If absent, reinstall from source.

## Intent → command

Always pass `--json`. Quote the title.

| Intent | Command |
| ------ | ------- |
| Cast to TV | `nstream --json --cast "X"` |
| Local play | `nstream --json --local "X"` |
| Mirror cast (1080p SDR) | `nstream --json --mirror "X"` (not desktop mirror — use `skill-cast`) |
| Never mirror this run | `nstream --json --cast --no-mirror "X"` |
| Year | `… --year 1999` |
| Movies / series only | `… --movies` or `… --series` |
| Episode | `… --season 1 --episode 3` |
| Device | `… --device "Salotto"` |
| Subs | `… --subs` or `… --sub-lang ita` |
| Sub retime | `… --sub-offset -2.5` / `… --sub-fps 25:23.976` |
| Force dub | `… --audio-lang eng` |
| Quality | `… --quality 1080` (`4k`/`2160`, `720`, `auto`) |
| Probe (no play) | `nstream --json --probe "X"` |
| Episodes list | `nstream --json --probe "X"` without `--episode` → `action: "episodes"` |
| Continue / next | `nstream --json -c ["X"]` |
| Explain | `nstream --json --explain "X"` (add `--cast` for TV profile) |
| Browse | `nstream --json --cast --browse popolari\|nuovi\|top` |
| Lifecycle | `--stop` / `--status` / `--pause` / `--resume` / `--seek SEC` / `--volume N` (Cast 0–100% ↔ 0–1; ≠ TV OSD; MASTER/`step=null` on the Philips DMR; catt may quantize 14→13) |
| Subtitles early/late during a live cast | `nstream --json --sub-shift +1.5` (+ = later; cumulative) |
| Clear dead denylist | `nstream --json --forget-dead` |
| Clear addon circuit breakers | `nstream --json --forget-breakers` (ADR 0027; Open addons skipped without network) |

### Rules of engagement

- **Never invent stream URLs or tokens.** JSON never contains them (by design). Do not log or
  reconstruct Torrentio/debrid URLs.
- **`ok: true` means delivery started** for cast/play (ADR 0031). On live HLS that is
  PLAYING/PAUSED/BUFFERING, not a playlist GET (ADR 0044). On `cast_failed`, do not
  report success. If `delivery: live` then `--status` shows `receiver_error`, the start
  lied — stop and recast; do not tell the user it is playing.
- **Do not silently change language or quality** on `audio_lang_unavailable` /
  `quality_unavailable` — show `available_*` and ask.
- A soft `audio_langs` preference may start a later language when the preferred dub
  needs a full remux and a direct MP4/WebM exists (ADR 0035). Read `notice` and
  `audio_lang` — that is the dub that started. `--audio-lang` still forces the dub.
- **`sources_removed` / `sources_truncated`:** do not blindly retry the same command.
- **Cast is fire-and-return by default**; use `--stop` to persist position; `--follow` only
  when the user wants the process to hold for the full runtime (JSONL events).
- **Local `--local` blocks** and opens a window — prefer `--cast` unless the user is at the
  machine.
- Series without season/episode → **S01E01**; no auto-binge in headless.
- Stream sources come from **config** (`torrentio_enabled`, `addons`, `playback_backend`) —
  no `--addon` flag. Guide the user through settings; never paste secret-bearing manifests
  into chat.

### Subtitles & audio (summary)

- Tiers: `subtitles_match` = `hash` | `audio` | `lang` (ADR 0020). Manual offset/fps win.
- `--audio-lang` is hard; ffprobe-verified when possible (`audio_verified`).
- `--quality` holds across reselects (ADR 0021). Combined with audio-lang: quality first.

### Tier-2 remux & mirror

- Dolby/DTS-only → host remux when `cast_remux` on; JSON `reencoded: true`. Prefer AAC picks.
- Large remux may auto-switch to mirror (`cast_mirror_over_remux_gb`); `--no-mirror` suppresses.
  The mirror needs Hyprland; `--doctor` says when it's off.
- `remux_infeasible` (ADR 0036): the remux would be refused (`reason`: disk/cap/ffmpeg) and no
  mirror or direct release could replace it. Don't retry unchanged — suggest `--quality 1080`,
  freeing disk, or `--local`.
- `audio_verified: false` with `audio_lang: null` after a cast = the remux failed mid-way and
  the file went out as-is: warn the user the audio may be missing.
- Remux/file via catt (no castbridge): TV should show title + Cinemeta poster when
  `catt.api` is importable (ADR 0050). CLI-only hosts still get the title. Not a remux-720 issue.
- Details: `docs/user/cast.md`, `docs/headless.md`.

### Parsing

`notices` (every result/error) carries what stderr said; act on coded ones (`p2p_blocked`,
`p2p_no_vpn`, `audio_lang_absent`, `remux_failed`, `subs_not_delivered`) and relay the rest.

Read `ok`. On success surface `title`, `action`, `device`, short `stream` summary, and when
relevant: `quality`, `available_resolutions`, `audio_lang` / `available_audio` /
`audio_verified`, `duration_verified`, `reencoded`, `selection` (`exact`|`year`|`first`).

Full error table and recovery: **`docs/headless.md`** (codes include `no_result`,
`no_stream_sources`, `no_streams`, `id_untranslated`, `no_playable_stream`, `cast_failed`,
`sources_removed`, `sources_truncated`, `video_codec_unsupported`, `remux_infeasible`,
`audio_lang_unavailable`, `quality_unavailable`, `episode_not_found`, `series_completed`,
`device_not_found`, `network`, `usage`, `config`).

## Related repo docs

- Contract: `docs/headless.md`
- Ranking: `docs/selection.md`
- Config: `docs/user/config.md`
- Troubleshooting: `docs/user/troubleshooting.md`
