# nstream

Native, terminal-first Stremio alternative. No Electron, no embedded browser, no Node
server — just Python stdlib driving `fzf` and `mpv` over the Stremio addon HTTP APIs.

Replicates the core Stremio flow:

1. **Search** a title via **Cinemeta** (official Stremio metadata addon), or
   **browse** a catalog (Popular / New / Top-rated).
2. Pick the title (and, for series, the episode) with **fzf**.
3. Optionally pick **subtitles** via the **OpenSubtitles v3** addon.
4. Fetch streams from **Torrentio** using your existing Real-Debrid config.
5. Play the chosen stream in **mpv** (Real-Debrid resolves it to a direct link).

Watch progress is tracked over mpv's IPC socket, so **resume** and a
**continue-watching** menu work across sessions. For series, a Netflix-style
**next-episode overlay** appears near the end and auto-advances the binge.

## Why

The Stremio Flatpak beta (new Rust shell) crashes on Wayland/Hyprland (ozone-X11
`Missing X server`) and the Qt build is heavy. nstream reuses the same Torrentio +
Real-Debrid pipeline with a fraction of the footprint, and integrates with the native
mpv setup.

## Requirements

Runtime: `python3` (>=3.13, **zero third-party deps**), `mpv`, `fzf`, `ffmpeg` (its `ffprobe`
powers the pre-play track menu; nstream degrades gracefully without it), plus `foot` for the
desktop launcher. Optional: `catt` to cast to a Chromecast (`--cast`; per-LAN device discovery
is built in — a background `catt scan` at startup plus a verified disk cache, so casting never
stalls the TUI), `chafa` to render poster thumbnails in the fzf
preview pane (falls back to text-only without it), and `vainfo` (Arch: `libva-utils`) for the
hardware-aware stream ranking. The native cast senders — **castbridge** (metadata + events)
and the `--mirror` realtime sender — are separate openscreen-fork builds (`$CASTBRIDGE_BIN` /
`$CAST_MIRROR_BIN`); nstream falls back to `catt` / the file path without them (see
[Casting backends](#casting-backends)). Install/dev: `uv`. All native.

## Install

| Method            | Command                       | Notes                                      |
| ----------------- | ----------------------------- | ------------------------------------------ |
| Arch repo         | `sudo pacman -Syu nstream`    | from the `[gianluca]` personal repo        |
| Arch (local)      | `cd packaging && makepkg -si` | builds from the tagged source tarball      |
| PyPI              | `pipx install nstream`        | or `pip install --user nstream`            |
| From source (dev) | `./install.sh`                | `uv tool install` → `~/.local/bin/nstream` |

The Arch package installs `nstream` + `nstream-fuzzel` to `/usr/bin`, the desktop entry, and
`config.example.json` under `/usr/share/nstream/`. It does **not** touch `$HOME`: after install,
create your config (see below). `./install.sh` instead bootstraps `~/.config/nstream/config.json`
from the example automatically.

## Develop

```sh
uvx ruff check . && uvx ruff format --check .   # lint + format
uvx ty check                                    # type check
uv run python -m pytest                          # unit tests
uv run nstream "the matrix"                      # run from source
```

Runtime stays stdlib-only; `ruff`/`ty`/`pytest` are dev-group tools.

## Config — `~/.config/nstream/config.json`

```json
{
  "torrentio_base": "sort=qualitysize|realdebrid=YOUR_RD_API_TOKEN",
  "cinemeta": "https://v3-cinemeta.strem.io",
  "mpv_args": []
}
```

- `torrentio_base`: the Torrentio config string (everything between the host and
  `/manifest.json` in your Torrentio addon URL). It carries your **debrid** key as
  `<provider>=<KEY>`. Torrentio supports 8 providers (use the settings menu to pick one and enter the
  key, or edit by hand): `realdebrid`, `alldebrid`, `premiumize`, `torbox`, `debridlink`,
  `easydebrid`, `offcloud`, `putio` — e.g. `sort=qualitysize|alldebrid=YOUR_KEY`. RealDebrid token:
  <https://real-debrid.com/apitoken>. nstream detects each provider's cached marker (`[RD+]`,
  `[AD+]`, `[TB+]`, `[Putio+]`, …) so the "prefer instant" ranking works on any of them.
- `opensubtitles`: base URL of the OpenSubtitles v3 addon (default shown).
- `subtitle_langs`: preferred subtitle languages, in order. Used to sort/auto-pick subtitles
  and passed to mpv as `--slang` (non-overriding).
- `audio_langs`: preferred audio languages, in order (default `["ita","eng"]`). Passed to mpv as
  `--alang` so your language is auto-selected when the file has multiple audio tracks — injected
  only if you haven't set `alang` yourself. The **first** entry is the _primary_ language and the
  rest are acceptable fallbacks (see `primary_lang`).
- `primary_lang`: your native language (default `""` = first of `audio_langs`). It drives two
  guards on the auto-pick (local mpv): (1) a release whose name only says **"Dual"/"MULTI"** is
  _not_ trusted to contain it — that token can be any pair (e.g. Latino+Eng), so nstream confirms
  the real tracks with `ffprobe` (reading the track `title`, e.g. `Italian`, when the language tag
  is `und`); (2) if the chosen file has no primary-language audio, nstream first tries the next-best
  sources for one that does, and otherwise plays the best fallback (e.g. English) **with
  primary-language subtitles turned on automatically** — so you're never left watching a foreign dub
  with no safety net. Releases that explicitly name a preferred language outrank bare "Dual" ones.
- `addons`: extra Stremio addon manifest URLs (e.g. another stream or subtitle provider). Streams
  and subtitles are aggregated across the built-in providers plus these. Manage them from the
  settings menu.
- `history_enabled`: keep a watch history for resume / continue-watching (`true` by default).
- `hwdec`: mpv hardware decoding mode (default `auto-safe`). An explicit `--hwdec` in `mpv_args`,
  or a **concrete** method in `~/.config/mpv/mpv.conf`, is always respected. The ambiguous `auto`
  family (`auto`/`auto-safe`/…) is **auto-upgraded to the GPU's real method** (VAAPI, detected via
  `vainfo`) so mpv doesn't probe experimental Vulkan decode or a missing CUDA first — on a
  `gpu-api=vulkan` context that probing causes `VK_KHR_video_decode_queue`/`libcuda` errors and a
  software fallback. nstream's CLI flag overrides `mpv.conf` _only_ for that `auto`→vaapi upgrade.
  Set `""` to disable injection entirely.
- `auto_play`: frictionless playback (`true` by default). Pressing **Enter** on a title plays the
  best stream immediately — no stream or track menu, since the format is already filtered for your
  hardware and audio/subtitles default to your preferred languages. Press **Tab** in the list to
  pick the source and tracks by hand for that title. Set `false` to make manual the default (then
  Tab plays instantly). `--play` forces auto regardless of this setting.
- `prefer_cast`: send playback to a Chromecast (preferred sender: **castbridge** when available,
  else `catt`) instead of mpv by default (`false`). The `--cast` flag forces casting for one run;
  `--local` forces mpv even when this is on. Casting keeps full parity — resume, continue-watching
  and series auto-advance work via castbridge events (or by polling `catt info` on the fallback). The
  embedded-track menu is mpv-only, so in cast mode subtitles are sent as an external file (when you
  pass `--subs`/`--sub-lang`) and the rest is left to the receiver. Stream selection is **Cast-aware**:
  it ranks against the Chromecast's decode profile (H.264/HEVC/VP9 up to 4K; AV1 and 8K dropped to
  the ⚠ section). The Default Media Receiver plays HEVC/4K/HDR natively but can't decode **Dolby**
  audio (AC-3/E-AC-3/DTS/TrueHD → silent), so selection **prefers an AAC release** (cast it directly,
  instant). When a title is only available with Dolby/DTS audio, nstream casts it anyway via an
  **on-host remux** — it keeps the original video (`-c copy`, so 4K/HDR/HEVC are preserved) and
  transcodes only the audio to AAC, then serves the file to the TV. That path downloads the file
  first (a short "preparo l'audio…" wait), so it caps remuxed releases to **1080p** by default
  (`cast_remux_max_resolution`) to avoid fetching a full 4K — direct AAC casts stay uncapped.
  If the TV is silent, also check the Cast volume isn't at 0 (`catt volume N`
  or the remote); nstream warns when it sees a zero volume. During a cast, press **`a`** to change
  the audio language when more than one is available — nstream re-casts a release in that language
  from the current position (the Chromecast plays the file's default track, so this works best with
  single-language dubs). Locally (mpv) audio is switched with mpv's native `#` key. **Casting from
  the TUI:** press **Alt-C** on any title/episode/continue row to cast that pick (regardless of
  `prefer_cast`); during local mpv playback, **Alt-C** moves the stream to the TV from the current
  position. **Device choice:** when casting, if more than one Chromecast is discovered (`catt scan`)
  or you used Alt-C, nstream shows a device picker instead of guessing.
- `cast_device`: preferred Chromecast **name** (default `""`). Set it from the settings menu
  (**Dispositivo cast** → discovery + pick) or here. Device resolution is **dynamic per-network**
  and **never blocks the TUI**: a `catt scan` runs in a background thread at startup while you
  browse, and its result feeds a 24h disk cache (`devices.json`). At cast time a cached device
  that answers a ~1s reachability probe is used **instantly**; otherwise nstream waits briefly on
  the pending scan (~6s, Ctrl-C skips straight to local playback) and casts by the device's
  current **IP** (`catt -d <ip>`), which is robust to mDNS name-resolution flakiness after a
  network change. A saved `cast_device` is honoured only when that device is actually reachable,
  otherwise nstream re-discovers (a single device is used directly, several prompt a picker).
  **If no Chromecast is reachable on the current network, nstream falls back to local mpv** (with
  a notice) instead of failing — quickly, without sitting through a full scan timeout. (Note:
  `catt scan -j` is broken in current catt, so discovery parses the text `catt scan`.)
- `cast_remux`: allow the Tier-2 on-host audio remux for Dolby/DTS-only releases (`true` by
  default; with `false` they are cast directly — likely silent on the Default Media Receiver).
- `cast_audio_codec`: target audio codec for the cast remux (default `"aac"`, DMR-decodable).
- `cast_remux_max_resolution`: resolution cap applied **only** to remuxed (downloaded) releases
  (default `1080`); direct casts are never capped.
- `cast_remux_max_size_gb`: guard against a runaway download (default `20`): releases that would
  need a remux above this size are demoted in ranking, and an interactive run asks for
  confirmation before fetching one. A free-disk pre-check always runs (with a minimum-headroom
  floor when the release size is unknown).
- `cast_mode`: `"dmr"` (default) hands a file/URL to the Default Media Receiver (direct or
  remux); `"mirror"` plays the stream in mpv on a hidden output and mirrors it to the TV in
  realtime — instant start, no download, but 1080p SDR. `--mirror` forces it for one run. Needs
  the openscreen Cast Streaming sender (`$CAST_MIRROR_BIN`) plus Hyprland + PipeWire.
- `mirror_bitrate`, `mirror_playout_ms`: mirror tuning; `0` (default) = built-in defaults
  (16 Mbps ceiling, 500 ms playout buffer).
- `autoplay`: show the in-video next-episode overlay for series and auto-advance (`true` by
  default). The overlay is drawn by a bundled mpv Lua script loaded via `--script` — it does
  **not** touch your `mpv.conf`. During a binge, subtitles (`--subs`) and stream selection are
  picked automatically per episode.
- `autoplay_lead`: seconds before the end of an episode at which the overlay appears (default `15`).
- `mpv_quiet`: hide mpv's track list and decoder/driver warnings, keeping the progress line and
  real errors (`true` by default). Injected as `--msg-level` only if you haven't set `msg-level`
  in `mpv.conf`/`mpv_args`. Toggle it from the settings menu.
- `hw_filter`: hardware-aware stream ranking (`true` by default). Torrentio sorts by size, so the
  first result is usually an 8K "AI upscale" or a 60-100GB Dolby Vision REMUX. nstream detects what
  the GPU can decode (via `vainfo`, cached) and auto-picks the best stream it can actually play;
  unsupported streams (8K, Dolby Vision P5, codecs with no HW decode) are excluded from auto-pick
  and shown at the bottom of the stream menu marked `⚠` (still selectable).
- `max_resolution`: cap for the filter (default `2160`; `0` = no cap).
- `allow_software`: keep streams whose codec the GPU can't decode in hardware (default `false`).
- `allow_dv5`: keep Dolby Vision Profile 5 streams (default `false`; they look wrong without DV).
- `lang_filter`: keep only releases tagged with a preferred audio language (`audio_langs`) or
  untagged (original) in the main list; releases tagged **only** with other languages drop to the
  `⚠` section (default `true`).
- `exclude_camrip`: move CAM/TS/TC/SCR cinema rips to the `⚠` section (default `true`).
- `min_seeders`: non-cached torrents below this many seeders are treated as near-dead and demoted
  (default `3`; `0` = off; cached `[RD+]` are exempt).
- `dedup`: collapse the same release seen on multiple trackers, keeping the best (default `true`).
- `max_streams`: how many streams the menu shows before a `↓ mostra tutti` entry reveals the rest
  and the `⚠` excluded ones (default `20`; `0` = no cap).
- `mpv_args`: extra flags passed to mpv (e.g. `["--sub-auto=fuzzy"]`).
- `nerd_font`: TUI glyph set — `"auto"` (env opt-in), `"on"`, `"off"`.
- `posters`: render poster thumbnails in the fzf preview pane, needs `chafa` (`true` by default).
- `image_mode`: `"auto"`, or `"off"` to force the terminal image protocol off.
- `playback_backend`: how streams are played (default `"local"`).
  - `"debrid"`: Torrentio returns ready debrid URLs (needs a provider key in `torrentio_base`),
    resolved instantly — the classic path.
  - `"local"`: query Torrentio **token-less** (drops the debrid segment) to get pure-torrent
    results, and stream them peer-to-peer through a local **TorrServer** instance nstream drives —
    no paid service. Set it from the settings menu (it warns if TorrServer isn't installed).
  - `"auto"`: hybrid — run **both** Torrentio queries (with and without the token) and merge them
    by filename, so a release can play via debrid (cached = instant) _and_ fall back to local P2P.
  - `"native"`: discover pure-torrent streams (token-less Torrentio) and resolve the chosen one
    through the provider's **own API** — independent of Torrentio's debrid resolution. Supports
    **TorBox** and **Premiumize** (they keep a live cache check); RealDebrid stays on the `debrid`
    path (it removed its cache endpoint in 2024). Falls back to local P2P if resolution fails. See
    [docs/adr/](docs/adr/) (ADR 0001–0004) for the rationale.
- `engine_port`: TorrServer HTTP port (default `8090`). nstream reuses a server already listening
  there, otherwise spawns one (and stops only the instance it spawned, never yours).
- `engine_cache_mb`: TorrServer in-memory read-ahead cache, in MB (default `256`).
- `engine_download_dir`: where TorrServer keeps torrent data (default `""` →
  `$XDG_CACHE_HOME/nstream/torrents`).
- `p2p_ack`: set to `true` once you've acknowledged the one-time P2P privacy notice (see below); it
  isn't shown again.
- `p2p_require_vpn`: block local P2P streaming unless a VPN interface is detected (default `false` —
  nstream only _warns_). With `true`, P2P is refused when no VPN is up (use debrid or enable the VPN).

### Playback backend: debrid or local P2P

By default nstream needs no paid service: with `playback_backend = "local"` it streams torrents
peer-to-peer via **TorrServer** — an external single-binary HTTP torrent server (like `mpv`/`fzf`,
user-installed, never bundled; on Arch: `yay -S torrserver-bin`). nstream finds a running instance
or spawns one on `engine_port`, adds the torrent by infoHash, waits for the initial read-ahead
buffer, then hands mpv/`catt` a plain `http://…/stream?…` URL — the _same contract_ as a debrid URL,
so resume, casting and series auto-advance are unchanged. The stream host is the machine's LAN IP so
a Chromecast can reach it too. If TorrServer isn't installed, nstream says so and you can switch to a
debrid provider in the settings. Prefer instant, hands-off playback and already pay for a debrid
service? Set `playback_backend = "debrid"`.

> **P2P privacy.** Local streaming joins the torrent swarm, so your IP is visible to peers (as with
> any torrent client). nstream shows this notice once and records `p2p_ack`, warns when no VPN is
> detected, and can refuse P2P without one (`p2p_require_vpn`). Debrid playback does **not** expose
> your IP to peers.

#### P2P + VPN (recommended)

If you use the `local`/`auto` backend, run TorrServer behind a VPN. Best practice (a 2025 study
found interface-binding cut real-IP leaks from ~31% to ~0.4%):

- **Bind TorrServer to the VPN interface**, don't just "have a VPN on". Either run it inside a VPN
  network namespace, or pass its torrent listener the VPN address (`torrserver --torrentaddr <vpn-ip>:<port>`).
- **Disable IPv6** on the torrent path if your VPN doesn't tunnel it (a common leak vector).
- **Avoid free/unaudited VPNs** — several have been caught logging/selling P2P sessions.
- nstream's `p2p_require_vpn = true` adds a local guard: it refuses to stream P2P when it can't
  detect a VPN interface (`tun*`/`wg*`/…). It's a safety net, **not** a substitute for binding.

The file holds your debrid key, so it is created `chmod 600` and git-ignored.
Watch history lives separately in `~/.local/state/nstream/history.json` (no secrets).

### Casting backends

Casting prefers the native **castbridge** sender daemon when its binary is present
(`$CASTBRIDGE_BIN`, a separate openscreen-fork build): unlike `catt` it sends media metadata
(title/poster/season/episode → the TV's now-playing card) and a real event stream that drives
`--follow` and resume. Without it nstream falls back to `catt` transparently (same cast, no
metadata). Dolby/DTS-only releases go through the Tier-2 **on-host remux** (see `prefer_cast`
above), served to the TV by nstream's own Range HTTP server: the receiver fetches the file
_inbound_ from your machine (ports 45000-47000), so on a default-deny `ufw` nstream auto-adds
the matching LAN allow rule (best-effort, needs passwordless sudo; on failure it prints the rule
to add by hand). Ordinary direct casts and the daemon channel are outbound-only — no firewall
change needed. The third backend, `--mirror` / `cast_mode: "mirror"`, skips the file path
entirely: mpv decodes locally on a hidden output and the openscreen sender mirrors it to the TV
in realtime (instant start, 1080p SDR).

## Logging & diagnostics

nstream writes a rotating log to `~/.local/state/nstream/nstream.log` (512 KB × 3). An unexpected
crash is captured there (handy when running inside the foot launcher, where the traceback would
otherwise scroll away) — on a crash nstream prints `errore inatteso — dettagli in <path>`. Run with
`--debug` (or `NSTREAM_DEBUG=1`) to also echo verbose logs to stderr. A redacting formatter scrubs the
debrid token from **every** log record, exception tracebacks included (`<provider>=…` segments and
`/resolve/<provider>/<token>/` paths), so the log never contains secrets.

## Security & limitations

- **Real-Debrid token.** It lives only in `config.json` (`chmod 600`). The Torrentio
  playback URL embeds the token by design (same as Stremio), so it is passed to `mpv` on
  its command line — visible to your own user via `/proc`, but nstream never prints it, never
  writes it to a log, and never persists it elsewhere. nstream also runs mpv with
  `--no-resume-playback` (nstream owns resume), and mpv's default
  `--write-filename-in-watch-later-config=no` keeps the URL out of `watch_later` files.
- **Autoplay overlay.** If you set `--end` in `mpv_args`, the overlay won't appear (mpv still
  reports the full media duration). With `keep-open=yes` the auto-advance still fires, via the
  `time-pos`/`end-file` path.

## Usage

```sh
nstream "the matrix"        # search → Enter plays the best stream; Tab picks source/tracks
nstream                     # continue-watching menu (if any), else prompts for a query
nstream --cast "dune"       # cast to a Chromecast instead of mpv (castbridge, else catt)
nstream --mirror "dune"     # realtime mirror cast (instant, 1080p; needs the openscreen sender)
nstream --local "dune"      # force local mpv even when prefer_cast is on
nstream --play "dune"       # force auto-pick even when auto_play is off
nstream --subs "dune"       # auto-pick subtitles in your preferred language
nstream --sub-menu "dune"   # pick subtitles by hand (fzf)
nstream --sub-lang eng ...  # force the auto-picked subtitle language
nstream --browse            # browse the Popular catalog (movies + series)
nstream --browse nuovi      # browse New; also: popolari, top
nstream --series "fargo"    # only TV series (search/browse/continue); --movies for films only
nstream -c                  # continue watching from history (resumes + keeps bingeing)
nstream --settings          # open the settings menu (also: ⚙ entry in the startup menu)
nstream --no-autoplay ...   # don't show the next-episode overlay
nstream --no-history ...    # don't record this session
nstream --json --cast --device TV "dune"  # headless: no fzf, one JSON object on stdout
nstream --json --cast --follow ...        # follow the cast to the end (resume tracked)
nstream --json --stop       # stop a cast started by nstream; --status shows its state
nstream --json --audio-lang eng ...       # force the dub language for a headless play/cast
nstream --quality 1080 "matrix"           # hard-filter to 1080p (TUI + --json; aliases: 4k, 720, auto)
nstream --json --quality 4k --cast "…"    # headless quality filter; fails with quality_unavailable
```

From Hyprland: launch **nstream** from your app launcher → it opens a **home menu** in foot
(continue-watching · 🔍 search · recent searches · ★ watchlist · 🎬 Film · 📺 Serie TV · ⚙ settings).
The Film/Serie sections are
type-scoped: their own continue-watching, search, catalogs (popular/new/top IMDb), and **Generi…**
(Cinemeta genre filter on Top). Catalog pages that return a full batch show **↓ altri…** to load
the next page. Multi-season / long series open a **season menu** before episodes. Search is typed
inside fzf (same chrome as the rest of the TUI). Leaf lists hint **Tab** / **Alt-C** / **Ctrl-/**
(preview); **Alt-W** toggles the selected title in the local watchlist. Search results prioritize
exact and accent-insensitive matches and show rating/genre context when supplied by the catalog.
The top-level search and continue-watching stay mixed. All UI lives in the TUI; the
launcher only opens it. ESC steps back one level; after a title plays (or has no sources) you
return to the list rather than the app quitting.

By default a title plays straight away. Without `--quality`, the TUI offers an in-flow **quality
picker** (Auto · resolutions present for that title) before the auto-pick or stream menu; series
binge keeps the first choice sticky. Press **Tab** in any title/episode/continue list to enter
manual mode for that pick: the curated **stream menu** (filter notices stay in the fzf header)
followed by a **pre-play screen** (`▶ Avvia · 🔊 Audio · 💬 Sottotitoli`) to choose the exact
embedded audio/subtitle track (probed with `ffprobe`, mapped to mpv `--aid`/`--sid`) or external
OpenSubtitles. `▶ Avvia` is the default (one Enter starts with the auto language preference);
without `ffprobe` it falls back silently to mpv's defaults. Flip the default with the
**Riproduzione automatica** setting (`auto_play`); auto-advancing binge episodes always play
automatically.

## Settings

`nstream --settings` (or the **⚙ Impostazioni** entry in the startup menu) opens a native fzf
menu to edit languages, autoplay, hardware decoding, history, the **debrid provider + key** (pick
RealDebrid/AllDebrid/TorBox/… then enter the key masked, never printed), and **Stremio addons** — add/remove extra manifest URLs to aggregate more
stream/subtitle/catalog providers alongside the built-in Cinemeta/Torrentio/OpenSubtitles. On
first run, if no config exists, nstream asks which debrid provider to use and writes one.

How streams and audio are ranked (and how to debug a pick with `nstream "<title>" --explain`)
is documented in [docs/selection.md](docs/selection.md). Architectural decisions — why the code
is shaped the way it is — are recorded as ADRs under [docs/adr/](docs/adr/).

## Files

| Path                           | Role                                                                                                                                           |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/nstream/cli.py`           | argparse entry point, fzf/mpv orchestration, home + typed sections (Generi…, paginated browse), resume                                          |
| `src/nstream/headless.py`      | headless `--json` subsystem: non-interactive play/cast/probe/stop/status, one JSON object on stdout (no fzf, no TTY)                           |
| `src/nstream/series.py`        | series-only flow: season-first/episode picker, binge auto-advance, per-episode resume (injected player)                                        |
| `src/nstream/stream_select.py` | stream pick/resolve + vetting guards (`prepare_stream`); cached-miss fallback, P2P + audio-language guards                                     |
| `src/nstream/subs.py`          | subtitle fetch/rank/download (OpenSubtitles) + pre-play `choose_tracks` menu                                                                   |
| `src/nstream/labels.py`        | display-label formatting for the fzf/mpv UI                                                                                                    |
| `src/nstream/api.py`           | addon resource dispatch with retry/backoff, gzip, concurrent per-addon fetch, short in-process metadata cache (streams/subtitles never cached) |
| `src/nstream/addons.py`        | Stremio addon-protocol client (manifests, dispatch, cache)                                                                                     |
| `src/nstream/net.py`           | retrying HTTP-JSON client (backoff, `Retry-After`) shared by `api`/`addons`                                                                    |
| `src/nstream/debrid.py`        | native debrid resolver (TorBox/Premiumize API: cache check + resolve) for the `native` backend                                                 |
| `src/nstream/engine.py`        | local P2P backend: drives an external TorrServer (spawn, add by infoHash, buffer wait)                                                         |
| `src/nstream/quality.py`       | hardware-aware stream parsing/ranking (vainfo caps, filter)                                                                                    |
| `src/nstream/player.py`        | local mpv playback: launch, IPC position tracking, hwdec/quiet/lang defaults                                                                   |
| `src/nstream/caster.py`        | Chromecast playback: device resolution, cast (castbridge or catt), status poll                                                                 |
| `src/nstream/cast_flow.py`     | shared cast decision tree (`run_cast`): audio vetting → mirror gate → Tier-2 remux → direct cast                                               |
| `src/nstream/cast_delivery.py` | shared castbridge event-loop driver (`drive_bridge`): fallback policy, pos/dur, Ctrl-C (ADR 0011)                                              |
| `src/nstream/discovery.py`     | background Chromecast discovery: `catt scan` thread, 24h disk cache, TCP verify (ADR 0010)                                                     |
| `src/nstream/bridge.py`        | IPC client for the castbridge daemon (metadata-rich LOAD + playback events)                                                                    |
| `src/nstream/serve.py`         | Range-capable HTTP server delivering the Tier-2 remux file to the TV (+ ufw rule)                                                              |
| `src/nstream/remux.py`         | Tier-2 cast: on-host audio remux (Dolby/DTS → AAC) to a complete temp MP4                                                                      |
| `src/nstream/mirror.py`        | realtime cast backend: mpv on a headless output mirrored via the openscreen sender                                                             |
| `src/nstream/tracks.py`        | ffprobe audio/subtitle track probing (memoized per url)                                                                                        |
| `src/nstream/languages.py`     | single source of truth for language tokens/flags/display names                                                                                 |
| `src/nstream/picker.py`        | shared fzf pickers: fzf/fzf_key/multi/index, confirm, ask_query                                                                                |
| `src/nstream/preview.py`       | poster thumbnail + metadata card for the fzf preview pane (`__preview`)                                                                        |
| `src/nstream/ui.py`            | TUI design system: caps, palette, glyphs, status/progress, layout, NO_COLOR                                                                    |
| `src/nstream/explain.py`       | `--explain` diagnostic renderer (why a stream/audio was auto-picked)                                                                           |
| `src/nstream/settings.py`      | native fzf settings menu (config + addons)                                                                                                     |
| `src/nstream/config.py`        | config load/save (XDG, atomic 0600) + payload types                                                                                            |
| `src/nstream/state.py`         | watch-history persistence, local watchlist and recent searches                                                                                   |
| `src/nstream/log.py`           | rotating file log + debug console; redacting formatter (token never logged)                                                                    |
| `src/nstream/util.py`          | stdlib-only low-level helpers (atomic write, JSON load, subprocess)                                                                            |
| `src/nstream/nstream.lua`      | mpv overlay for the next-episode countdown (loaded via `--script`)                                                                             |
| `nstream-fuzzel`               | thin launcher → opens the TUI home menu in foot                                                                                                |
| `nstream.desktop`              | app launcher entry                                                                                                                             |
| `pyproject.toml`               | metadata, entry point, ruff/ty config                                                                                                          |
| `config.example.json`          | config template (no token)                                                                                                                     |
| `install.sh`                   | uv tool install + desktop + config bootstrap                                                                                                   |

## Possible extensions

- Trakt sync.
