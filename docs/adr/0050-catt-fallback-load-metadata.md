# 0050. catt ≥0.13.2 fallback LOAD: title + thumb/images + BUFFERED via the library

- **Status:** Accepted
- **Date:** 2026-10-10
- **Deciders:** project maintainer
- **Amends:** [0007](0007-cast-metadata-via-castbridge.md) (the claim that catt cannot send
  any media metadata). Does **not** replace castbridge as the preferred sender.
- **Related:** ADR 0013 (custom receiver is not required for DMR chrome), ADR 0039
  (live HLS stays castbridge-only), ADR 0045 (catt-only production board; volume
  contract unchanged).

## Context

Field, 2026-10-05: Odroid N2 → Philips 43PUS9235/12, `cast_mode=dmr`,
`cast_remux=true`, catt 0.13.3, **castbridge absent**, `cast_receiver_app_id=07841171`
ignored (`receiver_app_ignored` → `CC1AD845`). A 720p H.264/AAC remux played
(`delivery: file`) but the TV showed generic player chrome — no title, no artwork,
no sensible duration.

ADR 0007 said `catt cast` sends a metadata-less LOAD and that patching catt for
metadata is impossible. catt **0.13.3** (the board binary) actually does this:

| Layer | Title | Thumb / images | contentType | streamType |
| ----- | ----- | -------------- | ----------- | ---------- |
| `DefaultCastController.play_media_url` | `title=` | `thumb=` | kwarg or `video/mp4` | kwarg (None if unset) |
| `catt cast` CLI | `-l` / `--title` | **none** | `StreamInfo.guessed_content_type` (`.mp4` → `video/mp4`) | `--stream-type`; inferred BUFFERED only for a remote URL yt-dlp gave a duration |
| nstream before this ADR | omitted | omitted | filename guess | omitted → LOAD `streamType: null` on a local remux |

`remux.cast_file` on the catt fallback runs `catt cast <cast-*.mp4>`. catt then uses
`Path.stem` as the title (a temp name) and never sets `stream_type`, so pychromecast
forwards `streamType: null` (the LIVE default is **not** applied when the kwarg is
None). `_cast_via_catt` dropped `CastMeta` entirely. Artwork is not a rewrite/LAN
problem: the CLI never sends `metadata.images`. Cinemeta poster URLs are public
HTTPS; castbridge already LOADs them unchanged (ADR 0007).

The Default Media Receiver **does** render title + poster when the LOAD includes
them (ADR 0007 field validation on this same Philips). A custom CAF id is not
required for chrome. On this board the custom id is never launched anyway
(ADR 0045).

## Decision

nstream's catt fallback prefers **catt as a library** (`CattDevice.controller.
play_media_url`, not `play_url` — that one waits 10s for PLAYING and false-misses
a remux start) so one LOAD carries title, `thumb` (Cinemeta/metahub **https**
poster → `metadata.images[0].url`), `contentType` (`video/mp4` on remux/file),
and `streamType: BUFFERED`. `media_info.metadata` is Movie (1) or TvShow (2) **and** carries
`images: [{"url": <https poster>}]` when the poster is an allowlisted
Cinemeta/metahub URL. catt 0.13.3 `play_media_url` (controllers.py:597-608)
forwards `thumb=` and `media_info=` to pychromecast 14.0.1
`_send_start_play_media` (media.py:475-493): `**media_info` **replaces**
`media.metadata`, then `thumb=` is copied into `metadata.thumb` and appended
to `images[]` only when `thumb` is truthy. A metadata dict that only set
`metadataType` therefore left the LOAD without `images` whenever `thumb` was
empty or dropped. nstream now puts `images[]` on the Movie/TvShow block
itself (`catt_play_kwargs` ← `catt_lib_media_info`). The LAN Range-proxy
serves the poster at `/cast/<token>/poster.jpg` (`image/jpeg`) and the LOAD
`images[0].url` is that LAN URL (same origin as the stream). Metahub
`/poster/small/` is webp — the Philips DMR strips it — so the upstream is
`/poster/medium/` (JPEG), verified `image/jpeg` before serving. HTTPS JPEG
fallback only when there is no LAN route. Never webp, never a debrid host.

- In-process `import catt.api` when the env already has catt (not a declared
  nstream dependency).
- Else `_catt_load.py` on the `catt` console-script interpreter.
- CLI fallback (`caster.catt_cast_argv`: `-l` + `--stream-type`) when import
  fails. The CLI still has no `--thumb`.

A remux file is served by nstream's Range server (ADR 0007 split: we own the
bytes, catt is only the sender). `play_url(resolve=False)` — never yt-dlp on a
LAN or debrid URL. Poster bytes are served from the nstream Range server
(JPEG), never through a debrid host.

No remux-resolution / `cast_mode` / ranking / OSD-volume change (ADR 0045).

`caster.catt_lib_media_info` pins the library LOAD body (fixture
`tests/data/catt_load_mediainfo_with_poster.json`, `images[0].url` present).
`contentId` is never logged or emitted in `--json`.

## Rationale

| Option | Title | Artwork | Cost | Verdict |
| --- | --- | --- | --- | --- |
| Leave catt argv bare | temp stem / empty | no | status quo | Rejected |
| CLI `-l` + `--stream-type` only | Cinemeta title | no | flags catt already has | Fallback when `catt.api` is missing |
| **catt library `play_media_url(thumb=)`** (+ nstream serve for remux) | yes | yes | uses the catt already on PATH; no yt-dlp | **Chosen** |
| Require castbridge / custom receiver for chrome | yes | yes | binary absent on the production board | Not required for DMR title+poster |
| Remux-forever 720 / always-remux | — | — | quality downgrade | Rejected (ADR 0045) |

## Consequences

- Remux/file and direct catt casts show title + poster on DMR chrome when
  catt is **≥0.13.2** and `catt.api` is importable (in-process or catt's
  interpreter). `streamType: BUFFERED` lets the receiver expose duration from
  the complete MP4.
- **Version gate:** catt 0.13.0/0.13.1 have no `-l/--title`, drop `media_info`,
  and 0.13.1 `play_media_url` can block with no timeout. nstream omits the new
  CLI flags and skips the library path there (and retries once without the
  flags if click still says `No such option`). A working cast must not become
  `cast_failed`.
- **CLI-only residual:** if catt cannot be imported, argv has no `--thumb` —
  title + BUFFERED still go out; artwork needs the library path or castbridge.
- In-cast `a` (audio switch) reuses the same library/helper LOAD. A CLI-only
  switch still has title on ≥0.13.2 (`-l`) but no artwork.
- In-process `CattDevice` / `prep_app` / `play_media_url` is bounded by
  `CATT_LIB_LOAD_TIMEOUT` (55s: connect + prep + catt's own 30s PLAYING wait).
  **Sent** is `MediaController.play_media` returning (hook on
  `controller._controller.play_media`) or catt 0.13.3's post-LOAD `CastError`
  (`"media session to become active timed out"`). A raise before that
  (NotConnected, RequestTimeout, TypeError on kwargs) is never-sent. The
  helper (`_catt_load`) uses rc 4 for post-LOAD session wait and rc 1 for
  never-sent. Friendly names use `name=`, not `ip_addr=`. Poster `thumb` is
  Cinemeta/metahub HTTPS only. In-process version is imported catt
  (`catt.__version__` / `importlib.metadata`), not the PATH binary.
- `catt_lib_outcome` is three-way (`ok` / `fail` / `unconfirmed`).
  `catt_lib_play` is confirmed-only (`ok`). On `unconfirmed`,
  `catt_receiver_load_state` grace-polls (`CATT_LIB_CONFIRM_GRACE` 20s) for
  our content_id or an explicit refuse (`LOAD_FAILED` / idle_reason ERROR
  whose content_id is empty or ours). `INTERRUPTED` is a replaced session,
  not a refuse. ERROR/LOAD_FAILED for a *different* content_id is leftover
  from an earlier cast → keep polling / `unconfirmed`. Match → `ok`.
  Explicit fail → `fail` (caller may CLI or kill). Still unknown at the
  bound → `unconfirmed`: no second CLI LOAD (keeps metadataType 1 / images).
  Honest `CastResult`: `started=False`, `error=cast_never_started`,
  `unconfirmed=True` (internal; not a `--json` field). `--json` reuses
  ADR 0031: `ok: false`, `error: cast_failed`, `cast_error: cast_never_started`.
  UI notice `code=cast_unconfirmed`. Log `catt sender=lib unconfirmed`.
- Leftover remux/LAN/subtitle servers after an unconfirmed LOAD: each
  reaper owns one handle/pid (`serve.schedule_reap(..., handle=,
  skip_if=has_load, idle_for=)`). The wait loop is cheap (`idle_for() >=
  seconds` and/or a monotonic deadline). `skip_if()` (`catt info`) runs
  **once** at the fire point; True re-arms a full new window from now —
  never on the 1s poll (a mid-film `catt info` storm would open a Cast
  connection every second). A later `register_inproc_proxy` cancels the
  earlier generation. Self-fire clears `_inproc_proxy_shutdown`.
  Headless: the parent timer dies with the CLI; the real bound is the
  detached idle reaper (`serve.IDLE_EXIT_S` = 3 h) and `--stop`.
- Worst-case send: library 55s + grace 20s + one status 10s ≈ 85s. CLI
  +30s only when no LOAD was sent. Remux follow-unconfirmed adds
  `_await_start` (40s). Click-flag retry is CLI-only, 30+30=60s.
- Live HLS is unchanged (castbridge-only, ADR 0039). Event-sourced `--follow`
  JSONL still prefers castbridge.
- `--json` shape unchanged (no new fields). `--status` already reports receiver
  `title` / `content_type` / `stream_type`.
- **LAN / HEVC (board 2026-10-10, main @ 26970282):** catt `play_media_url`
  sends the LOAD (metadataType 1 + `thumb`/`images`) then waits for the media
  session (`WAIT_TIMEOUT` ≈ 30s). A native HEVC LAN start can miss that window
  and raise after the LOAD is already on the TV. Treating that as `_LIB_FAIL`
  CLI-overwrote chrome to GENERIC 0; treating every unconfirmed as
  `started=True` hid a real `LOAD_FAILED`. The three-way outcome above is the
  fix. Applies to LAN Range-proxy, hev1 rewrap (`remux.cast_file`), and
  direct LAN URLs. CLI remains only when `catt_can_lib_load()` is false, the
  LOAD was never sent, or the receiver refused it. Logged as
  `catt sender=lib` / `catt sender=lib unconfirmed` / `catt sender=cli`
  (never the URL).
- **Poster echo (board 2026-10-10, main @ cd284770):** Odroid → Philips
  43PUS9235 DMR. LAN delivery and `metadataType` 1 / title were present;
  `catt info` `media_metadata` had **no `images` key** in two reads.
  `catt info` reports the receiver echo (`MediaStatus.media_metadata` ←
  STATUS `media.metadata`, pychromecast 14.0.1 media.py:332/260-264), not
  the LOAD we sent. Putting `images[]` on `media_info.metadata` makes the
  Movie payload match the fixture even if `thumb=` is missing; a later
  empty echo then means the DMR dropped a failed fetch, not a sender omit.
- **Poster JPEG / headless (board 2026-10-10, tip 8282d240):** Odroid
  `--json --cast` on the Philips. `catt info` still had
  `{metadataType 1, title}` and **no `images`**. Two causes: (1) Cinemeta
  `poster/small/` is `image/webp` — DMR rejects and strips it; (2) LOAD
  `images[0].url` was still the WAN https URL, not the LAN origin the TV
  already fetches for the stream. Headless **does** pass `meta.poster`
  (`headless.py` `CastMeta(poster=…)` → `cast_flow.run_cast` →
  `caster.cast` / `lan_media(poster=)`). Fix: serve JPEG on
  `/cast/<token>/poster.jpg`, point `images[0].url` there, rewrite
  small→medium, verify `image/jpeg` (`serve.fetch_poster_jpeg`). Debug
  log `catt lib LOAD media=` (`--debug` / `NSTREAM_DEBUG`) with the
  token redacted so the next live run can grep the exact LOAD.
  Poster GET uses `ProxyHandler({})` (env `HTTPS_PROXY` ignored) and a
  redirect handler that re-checks every hop against the Cinemeta/metahub
  allowlist (https only, max 3 hops). Deadline 3 s across connect+read.
  A failed LAN JPEG fetch falls back to the allowlisted https JPEG
  rather than omitting `images`. Cache dir pruned to 200 newest files.
- **Mute flip on LOAD:** nstream's library path never sends `SET_VOLUME` or
  `set_volume_muted`. catt 0.13.3 `play_media_url` (controllers.py:597-608)
  forwards only url/content_type/current_time/title/thumb/subtitles/
  stream_type/media_info. `CastController.volume` / `volumemute`
  (controllers.py:465/474) are CLI-only. A standby wake that flips
  `volume_muted` true→false (volume level untouched) is **receiver
  behaviour on a new DMR session**, not an nstream unmute.

## References

- catt v0.13.3 `catt/cli.py` (`--title`, `--stream-type`), `catt/controllers.py`
  (`play_media_url`), `catt/stream_info.py` (`video_title` / `video_thumbnail` /
  `guessed_content_type`).
- pychromecast **14.0.1** (catt 0.13.3 pin `>=14.0.1,<15`)
  `controllers/media.py` `_send_start_play_media` (475-493: `**media_info`
  then `thumb` → `images[]`; 260-264 / 332: STATUS echo `media_metadata`).
- Symbols:   `caster.catt_lib_outcome`, `caster.catt_lib_play`,
  `caster.catt_receiver_load_state`, `caster.catt_receiver_has_load`,
  `caster.catt_play_kwargs`, `caster.catt_lib_media_info`, `caster.catt_load_media`,
  `caster.catt_image_url`, `caster.catt_jpeg_poster_url`, `caster.catt_cast_argv`,
  `serve.fetch_poster_jpeg`, `serve.served_poster_url`, `serve.poster_host_allowed`,
  `serve._poster_http`, `serve._PosterRedirect`,
  `caster._cast_via_catt`, `caster._catt_inprocess_play`, `caster._catt_lib_finish`,
  `caster._hook_catt_play_media`, `caster._log_catt_load`, `caster._schedule_unconfirmed_sub_reap`,
  `serve.schedule_reap`, `serve.cancel_reap`, `serve.register_inproc_proxy`,
  `nstream._catt_load`, `remux._cast_file_via_catt_lib`, `remux.cast_file`,
  `bridge._media_load_args`.
