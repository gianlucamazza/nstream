# 0050. catt 0.13 fallback LOAD carries title + BUFFERED; poster stays on castbridge

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

nstream's catt fallback (direct URL and remux/file) builds argv with
`caster.catt_cast_argv`: `-l` from `catt_display_title` and `--stream-type BUFFERED`
for complete VOD / remux files. Poster / Movie / TvShow blocks stay on
**castbridge** (`bridge._media_load_args`). No `--thumb` wrapper, no pychromecast
import, no remux-resolution or `cast_mode` change, no OSD volume map (ADR 0045).

`caster.catt_media_info` / `catt_cli_media_info` pin the MediaInformation body
catt 0.13 would put on LOAD (fixture `tests/data/catt_load_mediainfo_*.json`).
`contentId` is never logged or emitted in `--json`.

## Rationale

| Option | Title | Artwork | Cost | Verdict |
| --- | --- | --- | --- | --- |
| Leave catt argv bare | temp stem / empty | no | status quo | Rejected |
| **CLI `-l` + `--stream-type BUFFERED`** | Cinemeta title | no | flags catt already has | **Chosen** |
| nstream Range server + `CattDevice.play_url(thumb=)` | yes | yes | second sender stack; yt-dlp on a served URL is unsafe (`catt cast http://…` extracts) | Rejected (ADR 0007 already refused mixed sessions / extra Cast stacks) |
| Require castbridge / custom receiver for all chrome | yes | yes | binary absent on the production board | Honest residual for **poster only** |
| Remux-forever 720 / always-remux | — | — | quality downgrade | Rejected (ADR 0045) |

## Consequences

- Remux/file and direct catt casts show the title on DMR chrome; `streamType:
  BUFFERED` lets the receiver expose duration from the complete MP4.
- Artwork on a catt-only host remains generic. That is a **CLI gap**, not a
  receiver-hard limit. Install castbridge for `metadata.images`.
- Live HLS is unchanged (castbridge-only, ADR 0039).
- `--json` shape unchanged (no new fields). `--status` already reports receiver
  `title` / `content_type` / `stream_type`.

## References

- catt v0.13.3 `catt/cli.py` (`--title`, `--stream-type`), `catt/controllers.py`
  (`play_media_url`), `catt/stream_info.py` (`video_title` / `video_thumbnail` /
  `guessed_content_type`).
- pychromecast `MediaController._send_start_play_media` (GENERIC metadataType,
  `thumb` → `images[]`).
- Symbols: `caster.catt_cast_argv`, `caster.catt_display_title`,
  `caster.catt_media_info`, `caster.catt_cli_media_info`, `caster._cast_via_catt`,
  `remux.cast_file`, `bridge._media_load_args`.
