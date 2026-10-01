"""Configuration loading, `Config` schema, and per-invocation `PlayOpts`."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import util


class ConfigError(Exception):
    """Raised when the config file is missing or malformed."""


@dataclass(frozen=True)
class PlayOpts:
    """Per-invocation playback preferences threaded through the flow."""

    auto: bool  # auto-pick the top stream (skip the stream menu)
    cast: bool  # send playback to a Chromecast (catt) instead of mpv
    sub_mode: str | None  # None = no subs, "auto" = pick preferred lang, "menu" = fzf
    sub_lang: str | None  # force this language for sub_mode="auto"
    history: bool  # record/resume watch history
    autoplay: bool  # offer the next-episode overlay for series
    cast_choose: bool = False  # force the device picker (explicit "cast this" action)
    audio_lang: str | None = None  # force this audio/dub language (--audio-lang; TUI + headless)
    # Mirror backend, tri-state (ADR 0021): None = the config decides (cli folds
    # cast_mode + the ADR-0015 auto-switch applies); True = forced (--mirror);
    # False = suppressed for this invocation (--no-mirror: no auto-switch, and an
    # undecodable-video cast raises instead of silently mirroring — manual wins).
    mirror: bool | None = None
    # Stream resolution filter for this invocation:
    #   None = undecided (TUI offers an in-flow quality picker; headless = no filter)
    #   0    = Auto (no exact-resolution filter; skip picker — binge sticky)
    #   N    = hard-filter to streams with StreamInfo.resolution == N (e.g. 1080)
    # INVARIANT (ADR 0021): downstream of `prepare_stream` the callers replace() this
    # with the RESOLVED VettedStream.quality (always an int ≥ 0) — every reselect path
    # in the cast decision tree relies on it; None exists only before resolution.
    quality: int | None = None
    # Manual subtitle retime (ADR 0018), applied to the downloaded SRT before use so it
    # holds identically for mpv and for the cast's WebVTT: t' = t * sub_scale + sub_offset.
    # Offset fixes a constant shift; scale fixes framerate drift (--sub-fps SRC:DST).
    sub_offset: float = 0.0
    sub_scale: float = 1.0


@dataclass(frozen=True)
class Config:
    # Torrentio config string. With a debrid segment (`…|realdebrid=TOKEN`) Torrentio
    # returns ready debrid urls; without one it returns pure-torrent streams that the
    # local P2P engine resolves. Default is token-less so local playback works out of box.
    torrentio_base: str = "sort=qualitysize"
    cinemeta: str = "https://v3-cinemeta.strem.io"
    opensubtitles: str = "https://opensubtitles-v3.strem.io"
    subtitle_langs: list[str] = field(default_factory=lambda: ["ita", "eng"])
    # Preferred audio languages for mpv track auto-selection (--alang), in order.
    audio_langs: list[str] = field(default_factory=lambda: ["ita", "eng"])
    # Primary (native) language. Drives audio selection and the safety-subtitle logic
    # (subtitles auto-on when the actual audio isn't this language). Empty = first of
    # audio_langs; the remaining audio_langs are acceptable fallbacks.
    primary_lang: str = ""
    # Extra Stremio addon manifest URLs (beyond the built-in Cinemeta/Torrentio/
    # OpenSubtitles), aggregated for streams/subtitles/catalogs. Stream alternatives
    # to Torrentio (Comet, MediaFusion, AIOStreams, …) land here as user-generated
    # manifest URLs — see `sources.STREAM_PRESETS` and the settings "Fonti stream" menu.
    addons: list[str] = field(default_factory=list)
    # Include the built-in Torrentio stream provider. Off = discovery only from
    # `addons` (and any other non-stream builtins stay). Useful when Torrentio is
    # down or the user prefers Comet/MediaFusion/AIO as the sole stream source.
    torrentio_enabled: bool = True
    history_enabled: bool = True
    # mpv hardware decoding, injected only if the user hasn't set hwdec themselves
    # (in mpv.conf or mpv_args). Empty string disables the injection.
    hwdec: str = "auto-safe"
    # Default playback mode: Enter on a title plays the best stream immediately
    # (no stream/track menu); Tab in the list flips to manual for that pick. Off
    # makes manual the default and Tab the auto shortcut.
    auto_play: bool = True
    # Send playback to a Chromecast (via `catt`) instead of mpv by default.
    # --cast / --local override per session. cast_device pins a device name for
    # `catt -d`; empty = resolve per-LAN via a fresh `catt scan` (see caster.py).
    prefer_cast: bool = False
    cast_device: str = ""
    # Registered Cast application id (Custom Receiver). Empty keeps the Default Media
    # Receiver (`CC1AD845`). A non-empty value is forwarded on media-load (ADR 0013).
    cast_receiver_app_id: str = ""
    # Tier-2 cast: when the chosen stream's audio is one the Chromecast Default Media
    # Receiver can't decode (AC-3/E-AC-3/DTS/TrueHD → silent), remux on the host (video
    # `-c copy`, audio → cast_audio_codec) to a complete temp file and let catt serve it,
    # so the original HEVC/4K/HDR video plays at native fidelity with audible audio. The
    # whole file is remuxed before casting (this DMR only plays a complete, Range-served
    # file — streaming-while-transcoding doesn't work on it), so it costs a prepare wait;
    # AAC releases are preferred first (no remux). Off → Dolby/DTS titles are demoted as
    # before.
    cast_remux: bool = True
    cast_audio_codec: str = "aac"  # target audio codec for the cast remux (DMR-decodable)
    # A Tier-2 remux downloads + rewrites the whole file before playback, so a 4K Dolby
    # title means a 30-60 GB fetch. A *direct* cast streams 4K for free (no host download),
    # so this cap applies ONLY to releases that need remuxing: among them, prefer ≤ this
    # resolution (default 1080p). 0 = no cap. A preference, not an exclusion — a sole 4K
    # Dolby release is still cast.
    cast_remux_max_resolution: int = 1080
    # Ceiling (GB) on how big a release a Tier-2 remux may fetch: a remux downloads + rewrites
    # the whole file, so a 4K Dolby title can be 30-90 GB. This both demotes oversized
    # likely-remux releases in cast ranking (`quality.remux_within_size` — size is the real cost
    # an unlabelled 4K REMUX hides from the resolution cap) and, above the threshold, asks for
    # confirmation on an interactive tty before downloading (headless proceeds — it was the only
    # castable release). A free-disk pre-check always runs regardless. 0 = off (disk check only).
    cast_remux_max_size_gb: int = 20
    # Cast backend: "dmr" (default) hands the file to the Chromecast Default Media Receiver
    # (direct or Tier-2 remux); "mirror" decodes locally in mpv on a headless output and
    # mirrors it to the TV via the native Cast Streaming sender (instant start, no download,
    # but 1080p SDR — no 4K/HDR/Dolby). `--mirror` forces it per-invocation. Needs the
    # openscreen sender (`$CAST_MIRROR_BIN`) + Hyprland + PipeWire.
    cast_mode: str = "dmr"
    # Mirror backend tuning. 0 = built-in default (16 Mbps ceiling; 500 ms playout — a movie
    # isn't interactive, and the sender's 120 ms mirror buffer starves the audio in-flight
    # budget into constant drops near 110 ms RTT).
    mirror_bitrate: int = 0
    mirror_playout_ms: int = 0
    # When a Tier-2 remux would have to fetch a file at least this many GB — a 4K Dolby-only
    # release with no AAC alternative — auto-prefer the realtime mirror instead (ADR 0015):
    # it starts in seconds with no download, at the cost of 1080p SDR + ~120 ms latency.
    # Below the threshold the remux wins (native video/HDR, no latency once prepared). A
    # size-unknown 4K/8K release counts as above-threshold (the resolution can't hide the
    # fetch cost). 0 = never auto-switch (always remux). Only applies when the mirror backend
    # is available; `cast_mode: "mirror"`/`--mirror` still force the mirror regardless.
    cast_mirror_over_remux_gb: int = 10
    # ADR 0020: audio-anchored subtitle alignment against the LOCAL media file (the
    # Tier-2 remux output). Full-signal, confidence-gated, refusal-first; the sparse
    # remote mode was measured unviable (100-300 MB/cast) and lives only in the bench.
    sub_align: bool = True
    sub_align_budget_s: int = 240
    # Autoplay the next episode of a series via the in-video overlay.
    autoplay: bool = True
    # Seconds before the end of an episode at which the overlay appears.
    autoplay_lead: int = 15
    # Quiet mpv's terminal output (hide the track list and decoder/driver warnings,
    # keep the progress line and errors). Injected only if you haven't set msg-level.
    mpv_quiet: bool = True
    # Hardware-aware stream ranking: auto-pick the best stream the GPU can actually
    # play, excluding e.g. 8K and Dolby Vision P5 (and codecs the GPU can't decode).
    hw_filter: bool = True
    max_resolution: int = 2160  # 0 = no cap
    allow_software: bool = False  # keep streams whose codec has no HW decode
    allow_dv5: bool = False  # keep Dolby Vision Profile 5 streams
    # Stream-list curation (Torrentio returns ~150/title): keep only preferred-language
    # or untagged releases in the main list, drop camrips / near-dead torrents / dupes,
    # and cap how many are shown (a "show all" entry expands the rest).
    lang_filter: bool = True  # demote releases tagged only with non-preferred languages
    exclude_camrip: bool = True  # CAM/TS/TC/SCR out of the main list
    min_seeders: int = 3  # non-cached torrents below this are near-dead (0 = off)
    dedup: bool = True  # collapse the same release across trackers
    max_streams: int = 20  # cap the manual menu (0 = no cap)
    # Per-title quality preference for the interactive picker (not a hard HW cap —
    # that remains max_resolution). None = ask every title (fzf); 0 = Auto without
    # asking; N = hard exact resolution (same meaning as --quality N). Headless still
    # only filters when --quality is passed.
    default_quality: int | None = None
    # Cap continue-watching rows on the home menu before a "…altri" expand is needed.
    # 0 = no cap (show all resumable entries).
    home_continue_max: int = 12
    mpv_args: list[str] = field(default_factory=list)
    # TUI appearance. nerd_font: "auto" (env opt-in) | "on" | "off"; posters: render
    # poster thumbnails in the fzf preview pane (needs chafa); image_mode: "auto" |
    # "off" to force the image protocol off regardless of terminal.
    nerd_font: str = "auto"
    posters: bool = True
    image_mode: str = "auto"
    # Playback backend: "local" streams torrents peer-to-peer through a TorrServer
    # instance nstream drives (free, default); "debrid" plays the ready urls Torrentio
    # returns for a configured debrid provider; "auto" is hybrid — prefer cached debrid
    # urls and fall back to local P2P (merges both Torrentio queries by filename);
    # "native" discovers pure-torrent streams (token-less Torrentio) and resolves the
    # chosen one through the provider's own API (TorBox/Premiumize — see debrid.py),
    # falling back to local P2P on failure. Pure-torrent streams always go local.
    playback_backend: str = "local"
    engine_port: int = 8090  # TorrServer HTTP port (also the one nstream spawns)
    engine_cache_mb: int = 256  # TorrServer in-memory read-ahead cache
    engine_download_dir: str = ""  # torrent data dir; "" → $XDG_CACHE_HOME/nstream/torrents
    p2p_ack: bool = False  # user acknowledged the P2P privacy notice (IP exposed to peers)
    # Block local P2P streaming unless a VPN interface is detected (default off: only warn).
    p2p_require_vpn: bool = False

    @property
    def primary(self) -> str:
        """Primary/native language code: explicit `primary_lang`, else first audio lang."""
        return self.primary_lang or (self.audio_langs[0] if self.audio_langs else "")

    @property
    def fallback_langs(self) -> list[str]:
        """Acceptable non-primary audio languages, in preference order."""
        return [code for code in self.audio_langs if code != self.primary]


# Debrid provider keys Torrentio understands, embedded in `torrentio_base` as `key=token`.
# Single source of truth shared by settings (provider picker) and addons (strip for local
# backend); kept provider-agnostic — code never special-cases an individual provider.
# Single source of truth (key → display name); both the provider key set and the settings
# picker derive from it, and log.py builds its redaction regex from the keys — no drift.
DEBRID_PROVIDER_NAMES: dict[str, str] = {
    "realdebrid": "RealDebrid",
    "alldebrid": "AllDebrid",
    "premiumize": "Premiumize",
    "torbox": "TorBox",
    "debridlink": "Debrid-Link",
    "easydebrid": "EasyDebrid",
    "offcloud": "Offcloud",
    "putio": "Put.io",
}
DEBRID_PROVIDERS: tuple[str, ...] = tuple(DEBRID_PROVIDER_NAMES)


# Allowed values for the enum-like string config fields (bad values fall back to default).
_ENUM_VALUES: dict[str, set[str]] = {
    "cast_mode": {"dmr", "mirror"},
    "nerd_font": {"auto", "on", "off"},
    "image_mode": {"auto", "off"},
    "playback_backend": {"local", "debrid", "auto", "native"},
}


def debrid_credentials(base: str) -> tuple[str, str] | None:
    """Extract the (provider, token) pair from a Torrentio config string, or None when no
    debrid segment is present. Single source of truth for the token — the native resolver
    reads it from here too (sent as an Authorization header), so there's no second copy to
    keep in sync. Only the first debrid segment counts (Torrentio expects one)."""
    for seg in base.split("|"):
        key, sep, val = seg.partition("=")
        if sep and key in DEBRID_PROVIDERS and val:
            return (key, val)
    return None


def _enum_str(raw: dict, key: str, default: str) -> str:
    val = str(raw.get(key, default))
    return val if val in _ENUM_VALUES[key] else default


# (min, max) bounds for the integer config fields, shared with the settings editor so
# validation lives in one place. max_resolution's 0 means "no cap"; 4320 is 8K.
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "autoplay_lead": (1, 120),
    "max_resolution": (0, 4320),
    "cast_remux_max_resolution": (0, 4320),
    "cast_remux_max_size_gb": (0, 1000),
    "mirror_bitrate": (0, 100_000_000),
    "mirror_playout_ms": (0, 5000),
    "cast_mirror_over_remux_gb": (0, 1000),
    "sub_align_budget_s": (60, 600),
    "min_seeders": (0, 100),
    "max_streams": (0, 500),
    "engine_port": (1024, 65535),
    "engine_cache_mb": (32, 4096),
    "home_continue_max": (0, 200),
}


def config_path() -> Path:
    """Resolve the config path, honouring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "nstream" / "config.json"


def state_path() -> Path:
    """Resolve the watch-history path, honouring XDG_STATE_HOME."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "nstream" / "history.json"


def library_path() -> Path:
    """Resolve the local discovery library (watchlist and recent searches)."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "nstream" / "library.json"


def dead_sources_path() -> Path:
    """Resolve the negative cache of sources proven removed/dead (ADR 0025). Separate from
    the library because it is machine-written diagnostics, not user-curated data, and must
    work regardless of `history_enabled`."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "nstream" / "dead-sources.json"


def remux_dir() -> Path:
    """Where Tier-2 cast remuxes are written (`$XDG_CACHE_HOME/nstream/remux`). Shared by
    `remux` (writes) and `quality` (ranks against the free space there)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "nstream" / "remux"


def _bounded_int(raw: dict, key: str, default: int) -> int:
    """Coerce a config int, falling back to `default` on a bad value and clamping to the
    field's INT_BOUNDS range (so out-of-range values can't break ranking/overlay)."""
    lo, hi = INT_BOUNDS[key]
    try:
        return max(lo, min(int(raw.get(key, default)), hi))
    except (TypeError, ValueError, OverflowError):
        return default


def _optional_quality(raw: dict) -> int | None:
    """Tri-state quality default: missing/null → None (ask each title); 0 → Auto;
    positive N → exact resolution. Invalid values fall back to None."""
    if "default_quality" not in raw or raw["default_quality"] is None:
        return None
    try:
        val = int(raw["default_quality"])
    except (TypeError, ValueError, OverflowError):
        return None
    if val < 0:
        return None
    # 0 = Auto; upper bound matches max_resolution (8K).
    return max(0, min(val, 4320))


def _ensure_private(path: Path) -> None:
    """Tighten config/state files that hold secrets to 0600 (best-effort).

    `save()` always writes 0600 via atomic_write, but a hand-edited or copied
    config may land as 0644 — tighten on load so the debrid token is not
    world-readable after the first successful nstream start.
    """
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            path.chmod(0o600)
    except OSError:
        pass


def load(*, secure_permissions: bool = True) -> Config:
    path = config_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config mancante: {path}") from e
    except (ValueError, UnicodeError) as e:
        raise ConfigError(f"config non valido ({path}): {e}") from e
    except OSError:
        raise ConfigError("config non leggibile; controlla i permessi") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"config non valido ({path}): atteso un oggetto JSON")
    for key in ("subtitle_langs", "audio_langs", "addons", "mpv_args"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"config non valido: {key} deve essere una lista di stringhe")
    if secure_permissions:
        _ensure_private(path)

    # Token-less default so a fresh config still streams locally; a debrid segment is
    # added to torrentio_base only when the user opts into a paid provider.
    base = raw.get("torrentio_base") or Config.torrentio_base
    # Absent → default; explicit "", false or null → disabled.
    hwdec_raw = raw.get("hwdec", Config.hwdec)
    hwdec = str(hwdec_raw) if hwdec_raw else ""
    return Config(
        torrentio_base=base,
        cinemeta=raw.get("cinemeta", Config.cinemeta),
        opensubtitles=raw.get("opensubtitles", Config.opensubtitles),
        subtitle_langs=list(raw.get("subtitle_langs", ["ita", "eng"])),
        audio_langs=list(raw.get("audio_langs", ["ita", "eng"])),
        primary_lang=str(raw.get("primary_lang", "") or ""),
        addons=list(raw.get("addons", [])),
        torrentio_enabled=bool(raw.get("torrentio_enabled", Config.torrentio_enabled)),
        history_enabled=bool(raw.get("history_enabled", True)),
        hwdec=hwdec,
        auto_play=bool(raw.get("auto_play", Config.auto_play)),
        prefer_cast=bool(raw.get("prefer_cast", Config.prefer_cast)),
        cast_device=str(raw.get("cast_device", Config.cast_device) or ""),
        cast_receiver_app_id=str(
            raw.get("cast_receiver_app_id", Config.cast_receiver_app_id) or ""
        ),
        cast_remux=bool(raw.get("cast_remux", Config.cast_remux)),
        cast_audio_codec=str(raw.get("cast_audio_codec", Config.cast_audio_codec) or "aac"),
        cast_remux_max_resolution=_bounded_int(
            raw, "cast_remux_max_resolution", Config.cast_remux_max_resolution
        ),
        cast_remux_max_size_gb=_bounded_int(
            raw, "cast_remux_max_size_gb", Config.cast_remux_max_size_gb
        ),
        cast_mode=_enum_str(raw, "cast_mode", Config.cast_mode),
        mirror_bitrate=_bounded_int(raw, "mirror_bitrate", Config.mirror_bitrate),
        mirror_playout_ms=_bounded_int(raw, "mirror_playout_ms", Config.mirror_playout_ms),
        cast_mirror_over_remux_gb=_bounded_int(
            raw, "cast_mirror_over_remux_gb", Config.cast_mirror_over_remux_gb
        ),
        autoplay=bool(raw.get("autoplay", Config.autoplay)),
        autoplay_lead=_bounded_int(raw, "autoplay_lead", Config.autoplay_lead),
        mpv_quiet=bool(raw.get("mpv_quiet", Config.mpv_quiet)),
        hw_filter=bool(raw.get("hw_filter", Config.hw_filter)),
        max_resolution=_bounded_int(raw, "max_resolution", Config.max_resolution),
        allow_software=bool(raw.get("allow_software", Config.allow_software)),
        allow_dv5=bool(raw.get("allow_dv5", Config.allow_dv5)),
        lang_filter=bool(raw.get("lang_filter", Config.lang_filter)),
        exclude_camrip=bool(raw.get("exclude_camrip", Config.exclude_camrip)),
        min_seeders=_bounded_int(raw, "min_seeders", Config.min_seeders),
        dedup=bool(raw.get("dedup", Config.dedup)),
        max_streams=_bounded_int(raw, "max_streams", Config.max_streams),
        default_quality=_optional_quality(raw),
        home_continue_max=_bounded_int(raw, "home_continue_max", Config.home_continue_max),
        mpv_args=list(raw.get("mpv_args", [])),
        nerd_font=_enum_str(raw, "nerd_font", Config.nerd_font),
        posters=bool(raw.get("posters", Config.posters)),
        image_mode=_enum_str(raw, "image_mode", Config.image_mode),
        playback_backend=_enum_str(raw, "playback_backend", Config.playback_backend),
        engine_port=_bounded_int(raw, "engine_port", Config.engine_port),
        engine_cache_mb=_bounded_int(raw, "engine_cache_mb", Config.engine_cache_mb),
        engine_download_dir=str(raw.get("engine_download_dir", Config.engine_download_dir) or ""),
        p2p_ack=bool(raw.get("p2p_ack", Config.p2p_ack)),
        p2p_require_vpn=bool(raw.get("p2p_require_vpn", Config.p2p_require_vpn)),
        sub_align=bool(raw.get("sub_align", Config.sub_align)),
        sub_align_budget_s=_bounded_int(raw, "sub_align_budget_s", Config.sub_align_budget_s),
    )


def load_raw() -> dict:
    """Return the raw config dict, or {} if the file is missing/corrupt."""
    return util.load_json(config_path(), {})


def save(updates: dict) -> None:
    """Merge `updates` into the on-disk config and rewrite it atomically (0600),
    preserving keys nstream doesn't model. The file holds the RD token."""
    data = load_raw()
    data.update(updates)
    util.atomic_write(
        config_path(),
        lambda f: json.dump(data, f, ensure_ascii=False, indent=2),
        prefix=".config-",
    )
