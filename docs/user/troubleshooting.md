# Troubleshooting

## No streams / nothing playable

| Symptom | Likely cause | What to do |
| ------- | ------------ | ---------- |
| Immediate empty / config error | No sources | Enable Torrentio or add manifests (`settings → Fonti stream`). Headless: `error: no_stream_sources` |
| Title found, zero sources | Not released / catalog gap | Retry later; try another addon; headless `no_streams` |
| Board row / `id_untranslated` | Catalog id (`tmdb:`, `kitsu:`, …) has no IMDb `tt` (ADR 0047) | Unlock that addon's meta; retry. nstream will not invent a `tt` |
| Sources exist, none playable | Filters or debrid still downloading | Lower `--quality`; wait for `[RD download]`; try `--local`; check `hw_filter` / `max_resolution` |
| Headless `sources_removed` | Every url answered 404/410 | Do **not** retry same command; try P2P/`--local`; if you believe sources returned, `nstream --forget-dead` |
| One addon always missing / long freezes stopped | Circuit breaker Open (ADR 0027) | `nstream --explain` lists Open addons; `nstream --forget-breakers` to reset |
| Headless `sources_truncated` | File much shorter than runtime | Placeholder/sample; try other quality/backend/source |
| Headless `quality_unavailable` | No stream at that exact res | Show `available_resolutions`; drop or change `--quality` |
| Headless `audio_lang_unavailable` | No dub match | Show `available_audio`; drop or change `--audio-lang` |

Debug ranking without playing:

```sh
nstream "title" --explain
nstream --json --explain --cast "title"
```

## Wrong audio language

- Release **name** tags are heuristics; real tracks come from ffprobe (`selection.md` — stream vs track language).
- Untagged English-only can win on quality; prefer tagged preferred-language or `--audio-lang`.
- Cast: DMR plays **default** track only; `a` re-casts another file, does not switch tracks.
- Dual/MULTI: nstream probes; primary_lang guards apply on auto-pick.

## Silent Chromecast / black screen

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| Silent | Dolby/DTS on DMR | Ensure `cast_remux: true` (default); or pick AAC; check Cast volume ≠ 0 |
| Quiet / OSD ≠ `--volume` | Cast 0–1 MASTER vs TV OSD ticks | `--volume` is Cast percent, not OSD. On Philips, Cast 14 ≈ OSD 8 if the OSD is 0–60. `--status` reports `volume_control_type`. Target OSD 12–15 ⇒ raise Cast percent after measuring the OSD, do not invent a scale (ADR 0045) |
| Stutter on 1080 HEVC, `ok: true` | Direct catt: TV pulls the remote URL | Not a decoder miss on this class of TV (HEVC/4K/HDR already plays from LAN Range / live HLS). Need castbridge for live; a video-copy remux if disk allows. **Not** a 720 H.264 default. Missing `cast_sender` only blocks mirror |
| Custom `cast_receiver_app_id` but session is `CC1AD845` | catt cannot launch that id | Install/use castbridge, or expect `receiver_app_ignored`. Live HLS always uses the DMR |
| Black video | Unsupported real video codec | Headless `video_codec_unsupported`; use `--local` or other quality; mirror if available |
| Long wait then cast | Tier-2 remux download | Expected for Dolby-only; lower remux caps or use `--mirror` |
| Cast never starts | Device/network/sender | Headless `cast_failed` + `cast_error`; check TV on, `catt scan`, castbridge binary |

## Device discovery

- No TV found → local mpv fallback (not a hard fail).
- Stale name after network change → discovery uses IP + cache re-verify.
- Multiple TVs → picker or `--device "Name"`.
- `device_not_found` headless → specify `--device` or ensure one reachable cast target.

## P2P / TorrServer

| Symptom | Fix |
| ------- | --- |
| TorrServer missing | Install (Arch: `yay -S torrserver-bin`); or switch to `playback_backend: debrid` |
| Port in use | Change `engine_port`; error names the conflict |
| Startup die | Read `$XDG_STATE_HOME/nstream/torrserver.log` |
| VPN required message | Bring up `tun*`/`wg*` or set `p2p_require_vpn: false` / use debrid |
| Privacy: want hard gate | `p2p_require_vpn: true` and bind TorrServer to VPN (ADR 0032) |

## Subtitles out of sync

| Kind | Flag |
| ---- | ---- |
| Constant shift | `--sub-offset -2.5` (seconds) |
| Framerate drift | `--sub-fps 25:23.976` |
| Prefer hash / auto align | `sub_align` (default on); see ADR 0020 |

Changing offset on a running cast needs re-cast. `subtitles_match` in JSON: `hash` / `audio` / `lang`.

## Year / title ambiguity

- Headless: `--year 1999` is a **hard** constraint (ADR 0030). Failure → `no_result` with `years` list.
- Prefer `--movies` / `--series` when same name exists as both.

## Logs & secrets

- File: `$XDG_STATE_HOME/nstream/nstream.log` (rotating). Crash message points here.
- Verbose: `--debug` or `NSTREAM_DEBUG=1` → stderr DEBUG.
- Tokens and stream URLs are **redacted** in logs; never paste them into chats.
- Unexpected crash in foot launcher: check the log path printed as `errore inatteso — dettagli in …`.

## Headless error index

Full recovery table: [../headless.md](../headless.md#error-codes).
