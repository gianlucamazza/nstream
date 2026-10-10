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
and `streamType: BUFFERED`. `media_info.metadata.metadataType` is 1 (Movie) or
2 (TvShow) — pychromecast `play_media(metadata=)` supports this and is cheap via
catt's `media_info` (the wrapper does not pass `metadata=` but **does** pass
`media_info`, which overwrites `media.metadata` before title/thumb are applied,
so the type sticks).

- In-process `import catt.api` when the env already has catt (not a declared
  nstream dependency).
- Else `_catt_load.py` on the `catt` console-script interpreter.
- CLI fallback (`caster.catt_cast_argv`: `-l` + `--stream-type`) when import
  fails. The CLI still has no `--thumb`.

A remux file is served by nstream's Range server (ADR 0007 split: we own the
bytes, catt is only the sender). `play_url(resolve=False)` — never yt-dlp on a
LAN or debrid URL. Poster is never proxied through the remux/debrid host.

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
  A timeout is **loaded, unconfirmed**: check `catt info` on the same device
  before any CLI fallback, remux server kill, or follow shutdown. Fall back
  only when no LOAD was sent or the receiver is not playing/buffering our
  content. Friendly names use `name=`, not `ip_addr=`. Poster `thumb` is
  Cinemeta/metahub HTTPS only. In-process version is imported catt
  (`catt.__version__` / `importlib.metadata`), not the PATH binary.
- Worst-case send: library 55s + one status confirm 10s + one CLI 30s ≈ 95s.
  Click-flag retry is CLI-only (no library), 30+30=60s.
- Live HLS is unchanged (castbridge-only, ADR 0039). Event-sourced `--follow`
  JSONL still prefers castbridge.
- `--json` shape unchanged (no new fields). `--status` already reports receiver
  `title` / `content_type` / `stream_type`.

## References

- catt v0.13.3 `catt/cli.py` (`--title`, `--stream-type`), `catt/controllers.py`
  (`play_media_url`), `catt/stream_info.py` (`video_title` / `video_thumbnail` /
  `guessed_content_type`).
- pychromecast `MediaController._send_start_play_media` (GENERIC metadataType,
  `thumb` → `images[]`).
- Symbols:   `caster.catt_lib_play`, `caster.catt_receiver_has_load`, `caster.catt_play_kwargs`,
  `caster.catt_lib_media_info`, `caster.catt_cast_argv`, `caster._cast_via_catt`,
  `nstream._catt_load`, `remux._cast_file_via_catt_lib`, `remux.cast_file`,
  `bridge._media_load_args`.
