"""Low-level HTTP-JSON client shared by the addon dispatch (`api`) and the native debrid
resolver. A retrying GET→JSON (gzip-aware, exponential backoff honouring `Retry-After`) plus a
cheap reachability probe.

Split out of `api` so `addons` can fetch manifests without importing `api`: `api` dispatches
over `addons.effective_addons`, so the legitimate direction is `api → addons`; keeping the HTTP
primitive in a module below both breaks the former `addons ↔ api` import cycle. Leaf: imports
only `log`/`util` + stdlib. Error messages never include the request URL (which may embed a
debrid token); callers pass a `what=` label instead.
"""

from __future__ import annotations

import contextlib
import contextvars
import gzip
import io
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Full, Queue

from . import log, util

_log = log.get_logger("net")

UA = "Mozilla/5.0 nstream"
TIMEOUT = 20.0
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 32 * 1024 * 1024
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "http_deadline", default=None
)


@contextlib.contextmanager
def request_budget(deadline: float):
    """Share a monotonic deadline across every HTTP retry in an addon task."""
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float:
    deadline = _deadline.get()
    left = TIMEOUT if deadline is None else deadline - time.monotonic()
    if left <= 0:
        raise NetworkError("budget rete esaurito")
    return min(TIMEOUT, left)


class NetworkError(Exception):
    """A request failed after exhausting retries, or hit a non-retryable status."""


class ClientError(NetworkError):
    """A non-retryable HTTP 4xx: the server is up and answered — this request (path, id,
    config) is wrong. Not an availability signal, so circuit breakers don't count it."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class AddonPool:
    """Fixed daemon workers and a bounded queue; stalled DNS cannot hold CLI exit.

    Python cannot interrupt a running system resolver. These workers own only
    reads; the caller owns persistent effects and cancels pending futures.
    """

    def __init__(self, workers: int = 8, pending: int = 64):
        self.queue: Queue = Queue(maxsize=pending)
        self.workers = workers
        self.started = False
        self.lock = threading.Lock()

    def submit(self, fn, *args) -> Future:
        future = Future()
        with self.lock:
            if not self.started:
                for index in range(self.workers):
                    threading.Thread(target=self._work, name=f"addon-{index}", daemon=True).start()
                self.started = True
        try:
            self.queue.put_nowait((future, fn, args))
        except Full:
            future.set_exception(NetworkError("fonti occupate; riprova più tardi"))
        return future

    def _work(self):
        while True:
            future, fn, args = self.queue.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                self.queue.task_done()


# --- stream availability probe (ADR 0025) --------------------------------------------

LIVE = "live"
GONE = "gone"
UNKNOWN = "unknown"

# A served total below `max(_MIN_REAL_BYTES, expected * _MIN_REAL_RATIO)` isn't the movie:
# it's a placeholder ("file not available" clip), an emptied file, or a transfer still in
# flight. Deliberately generous — no complete release lands here.
_MIN_REAL_BYTES = 8 * 1024 * 1024
_MIN_REAL_RATIO = 0.02


def _mib(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MiB"


@dataclass(frozen=True)
class Probe:
    """The classified outcome of an availability probe. `state` separates a source that is
    provably removed (`gone` — worth remembering) from one that merely failed right now
    (`unknown` — a transport hiccup must never ban a source)."""

    state: str
    status: int | None = None
    served_bytes: int | None = None  # total size the server reports for the resource
    reason: str = ""

    @property
    def usable(self) -> bool:
        """Can we hand this url to a player right now? True for `live` and for the benefit-of-
        the-doubt `unknown` (method/range rejected but the resource exists); False for `gone`,
        for a transport failure, and for an incomplete file — preserving the pre-ADR-0025
        fallback behaviour."""
        return self.state == LIVE or (self.state == UNKNOWN and self.status in (403, 405, 416))

    @property
    def dead(self) -> bool:
        """Proven absent — the only state that earns a place in the persistent denylist.
        Deliberately narrow: a verdict that outlives the run carries a higher burden of proof
        than one that only skips a candidate."""
        return self.state == GONE


def _served_total(headers, status: int) -> int | None:
    """Total resource size from a probe response: `Content-Range` (206) wins, else
    `Content-Length` on a full 200. None when the server doesn't say (chunked/no header)."""
    if headers is None:
        return None
    crange = headers.get("Content-Range") or ""
    if "/" in crange:
        total = crange.rsplit("/", 1)[1].strip()
        if total.isdigit():
            return int(total)
    if status == 200:
        length = headers.get("Content-Length")
        if length and str(length).isdigit():
            return int(length)
    return None


def probe_url(url: str, *, expected_bytes: int = 0, timeout: float = 6.0) -> Probe:
    """Classify a ready (debrid) stream url (ADR 0025). Asks for the first byte and judges
    both the status AND the size the server reports: a `200` proves the url resolves, not
    that the content is playable — the server may be serving a file that is still arriving,
    or a placeholder left where the content used to be.

    Each signal decides only what it can actually prove:

    - **status** (404/410) proves the resource is *not there* → `gone`, the one verdict
      strong enough to be remembered across runs.
    - **size** (served total far below `expected_bytes`) proves the file is *not usable now*
      → `unknown`. It cannot tell a growing transfer from an emptied file, so it never
      escalates to `gone`: inferring removal from an incomplete read is exactly the mistake
      the ADR 0025 post-scriptum records.

    Real-Debrid can't report a cached-miss, so this catches dead links and resolve errors,
    not every non-cached case."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            served = _served_total(getattr(resp, "headers", None), status)
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 416):  # method/range not allowed, but the resource exists
            return Probe(UNKNOWN, status=e.code, reason="metodo o range rifiutato")
        if 500 <= e.code < 600:
            return Probe(UNKNOWN, status=e.code, reason=f"errore server HTTP {e.code}")
        return Probe(
            GONE if e.code in (404, 410) else UNKNOWN, status=e.code, reason=f"HTTP {e.code}"
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return Probe(UNKNOWN, reason=f"irraggiungibile ({type(e).__name__})")

    if status >= 400:
        return Probe(
            GONE if status in (404, 410) else UNKNOWN, status=status, reason=f"HTTP {status}"
        )
    if expected_bytes > 0 and served is not None:
        floor = max(_MIN_REAL_BYTES, int(expected_bytes * _MIN_REAL_RATIO))
        if served < floor:
            return Probe(
                UNKNOWN,
                status=status,
                served_bytes=served,
                reason="file incompleto sul debrid "
                f"({_mib(served)} di {_mib(expected_bytes)} annunciati)",
            )
    return Probe(LIVE, status=status, served_bytes=served)


def url_playable(url: str, *, timeout: float = 6.0) -> bool:
    """Boolean façade over `probe_url` for callers that only need "can I play this now?"."""
    return probe_url(url, timeout=timeout).usable


def _read_json(resp) -> dict:
    """Read a urllib response body, transparently gunzipping when needed."""
    raw = bytearray()
    read = getattr(resp, "read1", resp.read)
    while True:
        remaining()
        chunk = read(min(65536, MAX_JSON_BYTES + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > MAX_JSON_BYTES:
            raise NetworkError("risposta troppo grande")
    enc = (resp.headers.get("Content-Encoding") or "").lower() if resp.headers else ""
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as zipped:
            raw = zipped.read(MAX_DECODED_BYTES + 1)
        if len(raw) > MAX_DECODED_BYTES:
            raise NetworkError("risposta decompressa troppo grande")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise NetworkError("risposta non valida: atteso oggetto JSON")
    return value


def http_get_json(url: str, *, what: str = "richiesta", retries: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=remaining()) as resp:
                return _read_json(resp)
        except urllib.error.HTTPError as e:
            # Don't retry client errors (auth, not found, bad config).
            if e.code != 429 and not (500 <= e.code < 600):
                raise ClientError(f"{what}: HTTP {e.code}", e.code) from None
            last_exc = e
            wait = util.retry_after(e)
        except (ValueError, gzip.BadGzipFile, EOFError) as e:
            # Bad body (incl. corrupt gzip) — caught before the broad OSError branch
            # since BadGzipFile is an OSError; retrying wouldn't help.
            raise NetworkError(f"{what}: risposta non valida") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            wait = None

        if attempt >= retries:
            break
        _log.debug("%s: tentativo %d fallito (%s), retry", what, attempt + 1, last_exc)
        delay = wait if wait is not None else util.backoff(attempt)
        if _deadline.get() is not None:
            delay = min(delay, remaining())
        time.sleep(delay)

    _log.warning("%s: rete non raggiungibile dopo %d tentativi", what, retries + 1)
    raise NetworkError(f"{what}: rete non raggiungibile dopo {retries + 1} tentativi") from last_exc
