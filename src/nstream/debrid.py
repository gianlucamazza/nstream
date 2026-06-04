"""Native debrid resolution: talk to a provider's own API to (optionally) batch-check the
cache and resolve a torrent infoHash to a ready HTTP url — the *same* `http://…` contract as
`engine.resolve` or a Torrentio debrid url, so the rest of the pipeline is unchanged.

Used only by the "native" playback backend. RealDebrid is intentionally **not** implemented
here: it removed its `instantAvailability` endpoint in Nov 2024 (see docs/adr/0002), so a
native cache check is impossible and RealDebrid stays resolved via Torrentio. TorBox and
Premiumize keep a live cache check and are the two providers wired here (docs/adr/0003-0004).

Leaf below `cli` (like `engine`/`caster`): imports only `config`/`engine`/`log` + stdlib —
`engine` purely for the shared magnet builder. Best-effort: every provider/transport error
raises `DebridUnavailable`, which the caller degrades to local P2P; it never crashes the picker.
Authentication is an `Authorization: Bearer <token>` header (kept out of URLs/logs), with the
token read from the single source of truth (`config.debrid_credentials`), never duplicated.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from . import engine, log, util
from .config import Config, Stream, debrid_credentials

_log = log.get_logger("debrid")

UA = "nstream"
_TIMEOUT = 30.0
_CHUNK = 100  # hashes per cache-check request, to keep URLs/bodies bounded


class DebridUnavailable(Exception):
    """The native debrid resolver can't serve this stream (auth/transport error, not cached,
    or the provider rejected the add). The caller degrades to local P2P / Torrentio."""


@runtime_checkable
class DebridResolver(Protocol):
    """A provider adapter. `cached()` is optional by contract — a provider with no live cache
    check returns an empty set rather than guessing — so callers never special-case a provider."""

    name: str  # config key: "torbox" | "premiumize"
    marker: str  # the [XX+] prefix reused by quality's cached marker (e.g. "TB", "PM")

    def cached(self, hashes: Sequence[str]) -> set[str]: ...

    def resolve(self, stream: Stream) -> str: ...


# --- transport -----------------------------------------------------------


def _urlopen(req: urllib.request.Request, timeout: float):  # indirection seam for tests
    return urllib.request.urlopen(req, timeout=timeout)


def _request(
    method: str,
    url: str,
    *,
    what: str,
    token: str | None = None,
    params: dict | None = None,
    form: dict | None = None,
    retries: int = 3,
) -> dict:
    """One JSON request, retried like `api.http_get_json`: transient failures (429, 5xx,
    connection/timeout) back off and retry (honouring `Retry-After`), other 4xx and bad
    bodies fail fast. `params` are appended to the query, `form` is sent url-encoded; auth is
    a Bearer header. Every failure becomes DebridUnavailable — the message carries `what`,
    never the token (the log filter also scrubs Bearer/token)."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else None
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _urlopen(req, _TIMEOUT) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower() if resp.headers else ""
                if "gzip" in enc or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            if e.code != 429 and not (500 <= e.code < 600):  # auth/not-found/bad-input: fail fast
                raise DebridUnavailable(f"{what}: HTTP {e.code}") from None
            last, wait = e, util.retry_after(e)
        except (json.JSONDecodeError, gzip.BadGzipFile, EOFError) as e:
            raise DebridUnavailable(f"{what}: risposta non valida") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last, wait = e, None
        if attempt >= retries:
            break
        _log.debug("%s: tentativo %d fallito (%s), retry", what, attempt + 1, last)
        time.sleep(wait if wait is not None else util.backoff(attempt))
    raise DebridUnavailable(f"{what}: non raggiungibile dopo {retries + 1} tentativi") from last


def _chunks(seq: Sequence[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def _basename(path: str) -> str:
    return (path or "").replace("\\", "/").split("/")[-1]


def _want_filename(stream: Stream) -> str | None:
    """The release's target file name (Torrentio `behaviorHints.filename`), basename only.
    This is the reliable key for picking the right file in a multi-file (series pack) torrent —
    the provider's own file order is not guaranteed to match the torrent's `fileIdx`."""
    fn = (stream.get("behaviorHints") or {}).get("filename")
    return _basename(fn) if fn else None


def _match_by_name(items: list[dict], want: str | None, name_of) -> dict | None:
    """The item whose basename matches `want` (exact, else either-endswith), or None."""
    if not want:
        return None
    for it in items:
        n = _basename(name_of(it))
        if n and (n == want or n.endswith(want) or want.endswith(n)):
            return it
    return None


# --- TorBox --------------------------------------------------------------

_TORBOX_API = "https://api.torbox.app/v1/api"
_MYLIST_TRIES = 3  # poll mylist this many times for a just-added torrent's file list
_MYLIST_DELAY = 0.5  # seconds between mylist polls


@dataclass(frozen=True)
class TorBoxResolver:
    """TorBox native resolver. Keeps a live batch cache check; resolves a cached torrent to a
    fresh CDN url via `requestdl` (resolved now, not handed to the player as a token permalink).
    `createtorrent` is rate-limited to 60/h, so only the chosen stream is added (cached only)."""

    token: str
    name: str = "torbox"
    marker: str = "TB"

    def cached(self, hashes: Sequence[str]) -> set[str]:
        out: set[str] = set()
        for chunk in _chunks(hashes, _CHUNK):
            data = _request(
                "GET",
                f"{_TORBOX_API}/torrents/checkcached",
                what="torbox checkcached",
                token=self.token,
                params={"hash": chunk, "format": "list"},
            )
            out |= _torbox_cached_hashes(data.get("data"))
        return out

    def resolve(self, stream: Stream) -> str:
        if not stream.get("infoHash"):
            raise DebridUnavailable("infoHash mancante")
        magnet = engine.magnet_from_stream(stream)
        added = _request(
            "POST",
            f"{_TORBOX_API}/torrents/createtorrent",
            what="torbox createtorrent",
            token=self.token,
            form={"magnet": magnet, "add_only_if_cached": "true"},
        )
        if not added.get("success"):
            raise DebridUnavailable(f"torbox add: {added.get('detail') or 'non in cache'}")
        tid = (added.get("data") or {}).get("torrent_id")
        if tid is None:
            raise DebridUnavailable("torbox: torrent_id assente")
        file_id = _torbox_file_id(self.token, tid, stream)
        # requestdl authenticates via the `token` query param — TorBox's documented permalink
        # mechanism (it has no Bearer variant). The returned CDN url (`data`) carries no token
        # and is what reaches the player; this request's token is scrubbed from logs (log.py).
        dl = _request(
            "GET",
            f"{_TORBOX_API}/torrents/requestdl",
            what="torbox requestdl",
            params={
                "token": self.token,
                "torrent_id": tid,
                "file_id": file_id,
                "redirect": "false",
            },
        )
        url = dl.get("data")
        if not isinstance(url, str) or not url:
            raise DebridUnavailable("torbox: url non disponibile")
        return url


def _torbox_cached_hashes(payload: object) -> set[str]:
    """Hashes TorBox reports cached, from `checkcached` data (a list of objects with a `hash`,
    or a hash-keyed object). Lower-cased to match Torrentio's infoHash casing."""
    if isinstance(payload, dict):
        return {str(h).lower() for h in payload}
    if isinstance(payload, list):
        items = cast("list[Any]", payload)
        return {str(it["hash"]).lower() for it in items if isinstance(it, dict) and it.get("hash")}
    return set()


def _torbox_file_id(token: str, torrent_id: object, stream: Stream) -> object:
    """The TorBox file id to stream: match the release's filename first (correct for series
    packs), then the `fileIdx` when in range, else the largest file (the feature video). Reads
    `mylist` for the just-added torrent, polling a few times since a freshly-added (even cached)
    torrent may not list its files instantly."""
    files: list[dict] = []
    for attempt in range(_MYLIST_TRIES):
        data = _request(
            "GET",
            f"{_TORBOX_API}/torrents/mylist",
            what="torbox mylist",
            token=token,
            params={"id": torrent_id},
        )
        files = (data.get("data") or {}).get("files") or []
        if files:
            break
        if attempt < _MYLIST_TRIES - 1:
            time.sleep(_MYLIST_DELAY)
    if not files:
        raise DebridUnavailable("torbox: nessun file nel torrent")
    match = _match_by_name(files, _want_filename(stream), lambda f: f.get("name") or "")
    if match:
        return match.get("id")
    file_idx = stream.get("fileIdx")
    if isinstance(file_idx, int) and 0 <= file_idx < len(files):
        return files[file_idx].get("id")
    return max(files, key=lambda f: f.get("size", 0)).get("id")


# --- Premiumize ----------------------------------------------------------

_PM_API = "https://www.premiumize.me/api"


@dataclass(frozen=True)
class PremiumizeResolver:
    """Premiumize native resolver. `cache/check` is best-effort (a miss is not authoritative);
    `transfer/directdl` returns instant urls for cached content in one call. Async cloud fetch
    (`transfer/create`) is deliberately out of scope — uncached content falls through to P2P."""

    token: str
    name: str = "premiumize"
    marker: str = "PM"

    def cached(self, hashes: Sequence[str]) -> set[str]:
        out: set[str] = set()
        for chunk in _chunks(hashes, _CHUNK):
            data = _request(
                "POST",
                f"{_PM_API}/cache/check",
                what="premiumize cache/check",
                token=self.token,
                form={"items[]": chunk},
            )
            # A bad token still returns HTTP 200 with status="error" — surface it instead of
            # silently reporting everything as uncached (which would hide a misconfigured key).
            if data.get("status") != "success":
                raise DebridUnavailable(
                    f"premiumize cache/check: {data.get('message') or 'errore'}"
                )
            resp = data.get("response") or []
            if len(resp) != len(chunk):
                _log.debug("premiumize cache/check: %d risposte per %d hash", len(resp), len(chunk))
            for h, ok in zip(chunk, resp, strict=False):
                if ok:
                    out.add(h.lower())
        return out

    def resolve(self, stream: Stream) -> str:
        magnet = engine.magnet_from_stream(stream)
        data = _request(
            "POST",
            f"{_PM_API}/transfer/directdl",
            what="premiumize directdl",
            token=self.token,
            form={"src": magnet},
        )
        if data.get("status") != "success":
            raise DebridUnavailable(f"premiumize: {data.get('message') or 'directdl fallito'}")
        content = data.get("content") or []
        if not content:
            raise DebridUnavailable("premiumize: nessun file (non in cache?)")
        chosen = _pm_pick_file(content, stream)
        link = chosen.get("link") or chosen.get("stream_link")
        if not link:
            raise DebridUnavailable("premiumize: link assente")
        return str(link)


def _pm_pick_file(content: list[dict], stream: Stream) -> dict:
    """Pick the file to play from a directdl listing: match the release's filename first
    (correct for series packs), then the `fileIdx` when in range, else the largest file.
    `content` is guaranteed non-empty by the caller."""
    match = _match_by_name(content, _want_filename(stream), lambda c: c.get("path") or "")
    if match:
        return match
    file_idx = stream.get("fileIdx")
    if isinstance(file_idx, int) and 0 <= file_idx < len(content):
        return content[file_idx]
    return max(content, key=lambda f: f.get("size", 0))


# --- factory -------------------------------------------------------------

_RESOLVERS: dict[str, type] = {
    "torbox": TorBoxResolver,
    "premiumize": PremiumizeResolver,
}
NATIVE_PROVIDERS: tuple[str, ...] = tuple(_RESOLVERS)


def supports_native(provider: str) -> bool:
    """Whether a native resolver exists for this debrid provider key."""
    return provider in _RESOLVERS


def get_resolver(cfg: Config) -> DebridResolver | None:
    """The native resolver for the configured debrid provider, or None when there is no token,
    the provider has no native implementation (e.g. realdebrid), so the caller falls back to
    Torrentio/P2P. Reads the token from the single source (`config.debrid_credentials`)."""
    creds = debrid_credentials(cfg.torrentio_base)
    if creds is None:
        return None
    provider, token = creds
    factory = _RESOLVERS.get(provider)
    if factory is None:
        return None
    return factory(token=token)


# --- diagnostics ---------------------------------------------------------


def _mask_url(url: str) -> str:
    """A resolved url with its query string dropped (it may carry a signed token), for printing."""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", "")) + ("?…" if p.query else "")


def selftest(cfg: Config, info_hash: str) -> str:
    """Smoke-test the configured native resolver against a real account: batch cache-check the
    given infoHash, then resolve it (which **adds the torrent** to the account). Verifies the
    provider response field names match the implementation. Read-mostly diagnostic — it never
    plays. The resolved url is printed with its query masked."""
    resolver = get_resolver(cfg)
    if resolver is None:
        creds = debrid_credentials(cfg.torrentio_base)
        provider = creds[0] if creds else "nessuno"
        return (
            f"nessun resolver nativo per il provider configurato ({provider}). "
            f"Imposta un token {', '.join(NATIVE_PROVIDERS)} e backend=native nei settings."
        )
    ih = info_hash.strip().lower()
    lines = [f"provider: {resolver.name} (marker [{resolver.marker}+])", f"infoHash: {ih}"]
    try:
        cached = resolver.cached([ih])
        lines.append(
            f"cached(): {'sì' if ih in cached else 'no'} (hash cached riportati: {len(cached)})"
        )
    except DebridUnavailable as e:
        lines.append(f"cached(): errore — {e}")
    lines.append("resolve(): aggiungo il torrent all'account e risolvo l'url…")
    try:
        url = resolver.resolve({"infoHash": ih})
        lines.append(f"resolve() → {_mask_url(url)}")
        lines.append("OK: i field-name delle risposte combaciano con l'implementazione.")
    except DebridUnavailable as e:
        lines.append(f"resolve(): errore — {e}")
    return "\n".join(lines)
