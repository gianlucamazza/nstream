# Headless / automation API (`--json`)

Non-interactive mode: no fzf, no TTY prompts. One JSON object on **stdout** (except
`--follow`, which streams **JSONL**). Exit code non-zero on failure paths; always parse
`ok` in the JSON.

This page is the **contract** for scripts and agents. The agent skill
[`skills/nstream/SKILL.md`](../skills/nstream/SKILL.md) is a playbook that must stay aligned
with this document.

## Guarantees

1. **No secrets in JSON** — stream URLs, debrid tokens, and resolve paths are never emitted.
   Work only with descriptive fields (`stream.resolution`, `audio_lang`, …).
2. **`ok: true` requires delivery started** for cast/play (ADR 0031). A failed hand-off is
   `ok: false` / `cast_failed`, never a success envelope.
3. **Per-invocation constraints hold on every selection path** (ADR 0021): `--quality` and
   `--audio-lang` apply through reselects (language, cast vet), not only the first pick.
4. **Explicit `--year` is hard** (ADR 0030): no soft fall-through to another year.

## Invocation patterns

Always pass `--json`. Quote the title.

| Intent                            | Command                                               |
| --------------------------------- | ----------------------------------------------------- |
| Cast to TV                        | `nstream --json --cast "X"`                           |
| Local mpv                         | `nstream --json --local "X"`                          |
| Mirror cast (1080p SDR)           | `nstream --json --mirror "X"`                         |
| Suppress mirror / auto-over-remux | `nstream --json --cast --no-mirror "X"`               |
| Disambiguate year                 | `… --year 1999`                                       |
| Movies / series only              | `… --movies` or `… --series`                          |
| Episode                           | `… --season 1 --episode 3`                            |
| Device                            | `… --device "Salotto"`                                |
| Subtitles                         | `… --subs` or `… --sub-lang ita`                      |
| Sub retime                        | `… --sub-offset -2.5` / `… --sub-fps 25:23.976`       |
| Force dub                         | `… --audio-lang eng`                                  |
| Force resolution                  | `… --quality 1080` (`4k`/`2160`, `720`, `auto`, …)    |
| Probe (no play)                   | `nstream --json --probe "X"`                          |
| List episodes                     | `nstream --json --probe "Series"` (no `--episode`)    |
| Explain ranking                   | `nstream --json --explain "X"`                        |
| Continue / next                   | `nstream --json -c ["X"]`                             |
| Browse catalog                    | `nstream --json --cast --browse popolari\|nuovi\|top` |
| Stop / status / pause / resume    | `--stop` / `--status` / `--pause` / `--resume`        |
| Seek / volume                     | `--seek SEC` / `--volume N`                           |
| Subtitle shift (live cast)        | `--sub-shift ±SEC` (cumulative, + = later) → `{"action": "sub_shift", "shift": total}`; `usage` when the cast is not live |
| Clear dead denylist               | `nstream --json --forget-dead`                        |
| Clear addon circuit breakers      | `nstream --json --forget-breakers`                    |

Incompatible with `--json`: interactive-only flags such as `--sub-menu` → `error: usage`.

### Fire-and-return vs `--follow`

- Default cast is **fire-and-return** (`--no-follow`): returns when the receiver has media.
  Prefer `--stop` later so position is persisted; `-c` also probes the receiver once.
- `--follow` holds until end and emits JSONL events:
  `{"action":"cast","event":"started|playing|paused|ended|failed|disconnected", ...}` with
  `position`/`duration` on playing/ended/disconnected. Read until `ended` or `failed`.
  **`disconnected` ≠ `ended`**: telemetry lost; receiver may still play.

Local `--local` **blocks** until mpv closes (opens a window). For non-blocking local, run in
the background. Series default to **S01E01** when season/episode omitted; no auto-binge in
headless.

## Success shape (`ok: true`)

Common fields:

| Field                                                | Meaning                                                                                                                                                      |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `action`                                             | `cast` \| `play` \| `probe` \| `episodes` \| `explain` \| lifecycle (`stop`/`status`/`pause`/`resume`/`seek`/`volume`) \| `forget_dead` \| `forget_breakers` |
| `title`                                              | Display title                                                                                                                                                |
| `device`                                             | Cast device (when casting)                                                                                                                                   |
| `stream`                                             | Descriptive pick (`resolution`, `codec`, `size_gb`, …) — **no URL**                                                                                          |
| `quality`                                            | Requested filter (`null` = none/Auto)                                                                                                                        |
| `available_resolutions`                              | Tiers on offer                                                                                                                                               |
| `audio_lang` / `available_audio` / `audio_verified`  | Dub played and alternatives                                                                                                                                  |
| `duration_verified`                                  | `true` if runtime check passed; `null` if not checkable                                                                                                      |
| `reencoded`                                          | Tier-2 remux used                                                                                                                                            |
| `delivery` | How it reached the screen: `live` (Tier-2 live HLS-TS, ADR 0039: starts in seconds, stereo AAC), `file` (complete remux), `direct`, `mirror`, `local` |
| `selection`                                          | Title pick: `exact` \| `year` \| `first`                                                                                                                     |
| `subtitles` / `subtitles_match` / `subtitles_offset` | Sub lang and evidence tier                                                                                                                                   |
| `volume` / `muted` / `notice`                        | Cast audio state. `audio_lang` is the dub that started. When the preferred language would need a full-file remux and a direct MP4/WebM exists in a later `audio_langs` entry, headless starts that direct cast and `notice` says why (ADR 0035). `--audio-lang` does not take this shortcut. |
| `notices` | Domain notices of this run (ADR 0037) — what stderr says, as `[{text, code, level}]`, on every result and error object (not on `--follow` event lines). `code` is stable when set: `p2p_blocked`, `p2p_no_vpn`, `audio_lang_absent`, `remux_failed`, `subs_not_delivered`, `subs_unverified` (the track is a language guess, sync unchecked), `volume_zero`; `""` = informational. Note: `volume` in JSON is the receiver level 0–1, while `--volume` takes 0–100 |

`--status` adds `player_state`, `position`, `duration`, `active_tracks`, `receiver_error`.

### Subtitle evidence tiers (ADR 0020)

| `subtitles_match` | Meaning                                                                  |
| ----------------- | ------------------------------------------------------------------------ |
| `hash`            | OpenSubtitles moviehash of the exact file                                |
| `audio`           | Native alignment against local/remux media; offset in `subtitles_offset` |
| `lang`            | Language guess only                                                      |
| `embedded`        | The release's own text track, as an HLS rendition on a live cast (ADR 0042); not verified against the audio |

Manual `--sub-offset` / `--sub-fps` always win (engine steps aside). Toggle engine:
`sub_align`, `sub_align_budget_s`.

## Error codes

| `error`                   | Meaning                                                                                                                                                                  | Recovery                                                        |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------- |
| `no_result`               | No title match / empty history; with `--year`, every candidate has another year                                                                                          | Retry spelling; show `years`; `--movies`/`--series`             |
| `no_stream_sources`       | No addon configured                                                                                                                                                      | Enable Torrentio or add manifests                               |
| `no_streams`              | Title ok, catalog empty for it                                                                                                                                           | Wait / other source                                             |
| `no_playable_stream`      | Filters or not-ready urls (e.g. debrid still fetching). `message` carries the reason when nstream knows it — blocked P2P gate, no direct link from the debrid (ADR 0033) | Retry later; lower quality; `--local`; act on the reason        |
| `cast_failed`             | Delivery never started                                                                                                                                                   | Check TV/network; read `cast_error` / stderr; history untouched |
| `sources_removed`         | All candidates proven 404/410                                                                                                                                            | Do not retry; `--local` or `--forget-dead` if believed back     |
| (slow / missing addon)    | Not an error code — Open circuit breaker (ADR 0027)                                                                                                                      | `--explain` lists Open addons; `--forget-breakers` to reset     |
| `sources_truncated`       | Duration ≪ expected runtime                                                                                                                                              | Other quality/backend; check `expected_runtime_s`               |
| `video_codec_unsupported` | Real codec not castable                                                                                                                                                  | `--local` or other quality                                      |
| `remux_infeasible`       | The pick needs a host remux (undecodable audio / .mkv) that would be refused (disk, size cap, ffmpeg) and neither the mirror nor a verified direct release can stand in; `reason` says which | Free disk; `--quality 1080`; `--local`; do not retry unchanged |
| `audio_lang_unavailable`  | Requested dub missing                                                                                                                                                    | Show `available_audio`                                          |
| `quality_unavailable`     | Requested res missing                                                                                                                                                    | Show `available_resolutions`                                    |
| `episode_not_found`       | Bad season/episode                                                                                                                                                       | Show `available`                                                |
| `series_completed`        | Past finale on continue/next                                                                                                                                             | Inform user                                                     |
| `device_not_found`        | No cast target                                                                                                                                                           | `catt scan`; `--device`                                         |
| `network`                 | Addon/API failure                                                                                                                                                        | Retry later; `message`                                          |
| `usage`                   | Bad flag combo                                                                                                                                                           | Fix command                                                     |
| `config`                  | Config/token problem (some paths)                                                                                                                                        | `nstream --settings`; stderr                                    |

Argument/config errors emit one JSON object and exit non-zero. Local playback failure
uses `player_missing` or `player_failed`; interruption emits `cancelled` and exits 130.
Ordinary commands emit one result object; existing `--cast --follow` keeps its JSONL
events followed by the final result. Backend event fields are scrubbed for URLs/tokens.

`nstream --json --doctor` reports required/optional local tools, config validity and
permissions. It runs before config loading/onboarding and does not change permissions,
start playback, or contact a provider. Its `network_tested` and `playback_tested` fields
are false by design. See [verification](verification.md).

## Stream sources (config only)

No `--addon` CLI flag. Headless uses `~/.config/nstream/config.json`:

- Built-in Torrentio when `torrentio_enabled`
- Extra manifests in `addons`
- `playback_backend` selects debrid / local / auto / native

Do not paste third-party manifest URLs that embed secrets into logs or chat.

## Scripting example

```sh
nstream --json --cast --device "TV" --quality 1080 --sub-lang ita "Dune" \
  | jq '{ok, title, action, audio_lang, reencoded, stream}'
```

## Related

- Ranking: [selection.md](selection.md)
- Cast delivery: [user/cast.md](user/cast.md)
- ADR 0021, 0029, 0030, 0031
