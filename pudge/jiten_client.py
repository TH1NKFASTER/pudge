"""One Jiten API client for the whole app (window, agent and helper processes).

Jiten allows 300 requests a minute with an API key and parses at most 200,000
characters a minute per user (at least 1000 are charged per parse call); over
either limit it answers 429 with ``Retry-After``. Pudge keeps to half of the
request limit and 90% of the parse budget, counted together by every thread and
every Pudge process through a small shared file (like the AniList cool-down):

* at most ``REQUESTS_PER_MINUTE`` requests and ``PARSE_CHARS_PER_MINUTE`` parsed
  characters in any 60 seconds; background work leaves a share to the window;
* a 429's ``Retry-After`` holds back every caller (parse calls only, for a 429
  of the parse budget);
* after ``BREAKER_FAILURES`` server errors, timeouts or dropped answers in a row nothing is sent
  for 1, 2, 5, 10, then 15 minutes (any answer ends it): background jobs pause,
  clicks fail at once;
* one keep-alive ``httpx.Client`` for every request.

A refused request (budget, Retry-After, outage) raises ``JitenRefused`` before
anything is sent, so callers may treat it as "not sent".
"""

from __future__ import annotations

import contextvars
import email.utils
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .i18n import err

JITEN_API_BASE = "https://api.jiten.moe/api"
WINDOW_SECONDS = 60.0
REQUESTS_PER_MINUTE = 150
PARSE_CHARS_PER_MINUTE = 180_000
PARSE_MINIMUM_CHARGE = 1000
# Background work (parse-ahead, statistics, iPhone packages, refreshes) leaves the rest
# of each budget to what the user is looking at: 80k characters a minute are always
# free for opening a long chapter (a 63k one is five 14k batches).
BACKGROUND_REQUESTS_PER_MINUTE = 120
BACKGROUND_PARSE_CHARS_PER_MINUTE = 100_000
# A click waits at most this long for the budget or a Retry-After; background work longer.
# A chapter parsed on its own thread for the open reader may wait longer (wait_limit).
INTERACTIVE_MAX_WAIT_SECONDS = 10.0
BACKGROUND_MAX_WAIT_SECONDS = 300.0
BACKGROUND_RETRY_AFTER_MAX_SECONDS = 65.0
BREAKER_FAILURES = 3
BREAKER_STEPS_SECONDS = (60.0, 120.0, 300.0, 600.0, 900.0)

_LOGGER = logging.getLogger(__name__)
_BACKGROUND: contextvars.ContextVar[bool] = contextvars.ContextVar("pudge_jiten_background", default=False)
_WAIT_LIMIT: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "pudge_jiten_wait_limit", default=None
)
_GENUINE = {"GET": httpx.get, "POST": httpx.post}
# What counts towards the outage pause: Jiten hanging or dropping answers. A Mac
# without network (connect/DNS errors) fails at once and holds nothing up.
_SERVER_TROUBLE = (httpx.TimeoutException, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)
_GENUINE_CLIENT = httpx.Client


class JitenRefused(RuntimeError):
    """Pudge did not send this request (rate limit, Retry-After or a Jiten outage)."""

    def __init__(self, message: str, *, reason: str, retry_after: float) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_after = max(0.0, float(retry_after))


@contextmanager
def background_requests(enabled: bool = True) -> Iterator[None]:
    """Jiten calls made inside are background work (smaller budget share, long waits)."""
    token = _BACKGROUND.set(bool(enabled))
    try:
        yield
    finally:
        _BACKGROUND.reset(token)


def in_background() -> bool:
    return _BACKGROUND.get()


@contextmanager
def wait_limit(seconds: float) -> Iterator[None]:
    """Jiten calls made inside wait at most ``seconds`` for the budget or a Retry-After.

    Longer than a click's 10 s only off the UI and request threads (a chapter parsed for
    the open reader); shorter where something else waits (a lease held meanwhile)."""
    token = _WAIT_LIMIT.set(max(0.0, float(seconds)))
    try:
        yield
    finally:
        _WAIT_LIMIT.reset(token)


def parse_charge(payload: Any) -> int:
    """Characters Jiten charges for ``reader/parse`` (UTF-16 units, at least 1000)."""
    texts = payload.get("text") if isinstance(payload, dict) else None
    if isinstance(texts, str):
        texts = [texts]
    joined = "".join(str(text) for text in texts or [])
    return max(PARSE_MINIMUM_CHARGE, len(joined.encode("utf-16-le")) // 2)


def retry_after_seconds(value: Any, *, now: float | None = None, default: float = 60.0) -> float:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError, IndexError, OverflowError):
            return default
        seconds = moment - (time.time() if now is None else now)
    return max(1.0, min(900.0, seconds))


class JitenClient:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        state_dir: Path | str | None = None,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self._state_dir = Path(state_dir).expanduser() if state_dir else None
        self._memory: dict[str, Any] = {}
        self._http: httpx.Client | None = None
        self.sent = 0  # requests sent by this process (diagnostics, tests)

    # ----------------------------------------------------------------- state
    def configure(self, cache_dir: Path | str | None) -> None:
        """Share the budget with the other Pudge processes through ``cache_dir``."""
        with self._lock:
            self._state_dir = Path(cache_dir).expanduser() / "jiten-api" if cache_dir else None

    def reset(self) -> None:
        with self._lock:
            self._memory = {}
            self.sent = 0
            if self._state_dir is not None:
                (self._state_dir / "budget.json").unlink(missing_ok=True)

    @contextmanager
    def _state(self) -> Iterator[dict[str, Any]]:
        """The shared state, locked across threads and processes while inside."""
        with self._lock:
            folder = self._state_dir
            if folder is None:
                yield self._memory
                return
            try:
                folder.mkdir(parents=True, exist_ok=True)
                handle = os.open(folder / "budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                yield self._memory
                return
            try:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            try:
                path = folder / "budget.json"
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                before = json.dumps(state, sort_keys=True)
                yield state
                after = json.dumps(state, sort_keys=True)
                if after != before:
                    temporary = path.with_name(f"budget.{os.getpid()}.{threading.get_ident()}.tmp")
                    try:
                        temporary.write_text(after, encoding="utf-8")
                        temporary.replace(path)
                    except OSError as exc:
                        temporary.unlink(missing_ok=True)
                        _LOGGER.warning("FALLBACK step=jiten.budget reason=state_write_failed error=%r", exc)
            finally:
                os.close(handle)

    def _window(self, state: dict[str, Any], now: float) -> list[list[float]]:
        rows = []
        for row in state.get("window") or []:
            try:
                at, chars = float(row[0]), float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if now - WINDOW_SECONDS < at <= now + 1.0:
                rows.append([at, chars])
        rows.sort()
        state["window"] = rows
        return rows

    def pause_reason(self) -> str:
        """Why background Jiten work should wait now ("" when it may run)."""
        with self._state() as state:
            now = self.clock()
            if float(state.get("breaker_until") or 0.0) > now:
                return "jiten_unavailable"
            if float(state.get("blocked_until") or 0.0) > now:
                return "jiten_rate_limited"
        return ""

    def retry_in(self) -> float:
        """Seconds until a paused background job may try Jiten again."""
        with self._state() as state:
            now = self.clock()
            return max(
                0.0,
                float(state.get("breaker_until") or 0.0) - now,
                float(state.get("blocked_until") or 0.0) - now,
            )

    # ------------------------------------------------------------- admission
    @staticmethod
    def _wait_limit(interactive: bool, max_wait: float | None) -> float:
        if max_wait is None:
            max_wait = _WAIT_LIMIT.get()
        if max_wait is not None:
            return max(0.0, float(max_wait))
        return INTERACTIVE_MAX_WAIT_SECONDS if interactive else BACKGROUND_MAX_WAIT_SECONDS

    def _admit(self, parse_chars: int, interactive: bool, max_wait: float | None = None) -> None:
        started = self.clock()
        limit = self._wait_limit(interactive, max_wait)
        max_requests = REQUESTS_PER_MINUTE if interactive else BACKGROUND_REQUESTS_PER_MINUTE
        max_chars = PARSE_CHARS_PER_MINUTE if interactive else BACKGROUND_PARSE_CHARS_PER_MINUTE
        charge = min(max_chars, max(PARSE_MINIMUM_CHARGE, int(parse_chars))) if parse_chars else 0
        while True:
            with self._state() as state:
                now = self.clock()
                breaker_until = float(state.get("breaker_until") or 0.0)
                if breaker_until > now:
                    wait = breaker_until - now
                    raise JitenRefused(
                        err("ln.jitenUnavailable", seconds=int(wait + 0.999)),
                        reason="unavailable",
                        retry_after=wait,
                    )
                blocked = float(state.get("blocked_until") or 0.0)
                if charge:
                    blocked = max(blocked, float(state.get("parse_blocked_until") or 0.0))
                reason = "retry_after"
                if blocked > now:
                    wait = blocked - now
                else:
                    rows = self._window(state, now)
                    used = sum(row[1] for row in rows)
                    if len(rows) < max_requests and used + charge <= max_chars:
                        rows.append([now, float(charge)])
                        return
                    reason = "budget"
                    wait = 0.0
                    if len(rows) >= max_requests:
                        wait = rows[len(rows) - max_requests][0] + WINDOW_SECONDS - now
                    if used + charge > max_chars:
                        freed = 0.0
                        for at, chars in rows:
                            freed += chars
                            if used - freed + charge <= max_chars:
                                wait = max(wait, at + WINDOW_SECONDS - now)
                                break
                    wait = max(0.05, wait)
            if self.clock() + wait - started > limit:
                raise JitenRefused(
                    err("ln.jitenRateLimited", seconds=int(wait + 0.999)),
                    reason="rate_limited",
                    retry_after=wait,
                )
            _LOGGER.info(
                "WAIT step=jiten.budget reason=%s wait_s=%.1f parse_chars=%s background=%s",
                reason,
                wait,
                charge,
                not interactive,
            )
            self.sleep(min(wait, 5.0) if reason == "budget" else wait)

    # --------------------------------------------------------------- results
    def _note_rate_limited(self, response: httpx.Response, *, parse: bool) -> float:
        now = self.clock()
        seconds = retry_after_seconds(response.headers.get("Retry-After"), now=now)
        with self._state() as state:
            key = "parse_blocked_until" if parse else "blocked_until"
            state[key] = max(float(state.get(key) or 0.0), now + seconds)
        _LOGGER.warning("RATE_LIMIT step=jiten.http status=429 parse=%s retry_after_s=%.0f", parse, seconds)
        return seconds

    def _note_failure(self, detail: str) -> None:
        with self._state() as state:
            now = self.clock()
            failures = int(state.get("breaker_failures") or 0) + 1
            if failures >= BREAKER_FAILURES:
                step = int(state.get("breaker_step") or 0)
                seconds = BREAKER_STEPS_SECONDS[min(step, len(BREAKER_STEPS_SECONDS) - 1)]
                state["breaker_until"] = now + seconds
                state["breaker_step"] = step + 1
                # After the pause one more failure starts the next (longer) pause.
                failures = BREAKER_FAILURES - 1
                _LOGGER.warning("BACKOFF step=jiten.breaker pause_s=%.0f detail=%r", seconds, detail[:200])
            state["breaker_failures"] = failures

    def _note_answered(self) -> None:
        with self._state() as state:
            if state.get("breaker_failures") or state.get("breaker_step") or state.get("breaker_until"):
                if state.get("breaker_step"):
                    _LOGGER.info("DONE step=jiten.breaker reason=answered")
                for key in ("breaker_failures", "breaker_step", "breaker_until"):
                    state.pop(key, None)

    # ------------------------------------------------------------- transport
    def _client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(
                    timeout=30.0,
                    limits=httpx.Limits(
                        max_connections=8, max_keepalive_connections=4, keepalive_expiry=60.0
                    ),
                )
            return self._http

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        replaced = getattr(httpx, method.lower())
        if replaced is not _GENUINE[method]:
            # httpx.get/post were replaced (instrumentation, tests): use them as they are.
            return replaced(url, **kwargs)
        if httpx.Client is not _GENUINE_CLIENT:
            with httpx.Client() as client:
                return client.request(method, url, **kwargs)
        return self._client().request(method, url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        attempts: int = 1,
        interactive: bool | None = None,
        parse_chars: int = 0,
        backoff: float = 0.6,
        max_wait: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send one Jiten request within the budget; retry 429/5xx/transport errors ``attempts`` times.

        Returns the last response (also a final 429 or 5xx); raises the last httpx
        transport error, or ``JitenRefused`` when nothing could be sent. ``max_wait``
        bounds the waits for the budget and Retry-After (else ``wait_limit`` or the default).
        One call counts at most once towards the outage pause, however often it retries.
        """
        method = method.upper()
        interactive = (not _BACKGROUND.get()) if interactive is None else bool(interactive)
        parse = url.rstrip("/").endswith("reader/parse")
        if parse and not parse_chars:
            parse_chars = parse_charge(kwargs.get("json"))
        attempts = max(1, int(attempts))
        limit = self._wait_limit(interactive, max_wait)
        if max_wait is None and _WAIT_LIMIT.get() is None and not interactive:
            limit = min(limit, BACKGROUND_RETRY_AFTER_MAX_SECONDS)  # per Retry-After below
        failed = False
        for attempt in range(attempts):
            last = attempt == attempts - 1
            self._admit(parse_chars if parse else 0, interactive, max_wait)
            try:
                self.sent += 1
                response = self._send(method, url, **kwargs)
            except httpx.TransportError as exc:
                if isinstance(exc, _SERVER_TROUBLE) and not failed:
                    failed = True
                    self._note_failure(f"{type(exc).__name__}: {exc}")
                if last:
                    raise
                self.sleep(backoff * (2**attempt))
                continue
            status = int(response.status_code)
            if status == 429:
                seconds = self._note_rate_limited(response, parse=parse)
                if last:
                    return response
                if seconds > limit:
                    raise JitenRefused(
                        err("ln.jitenRateLimited", seconds=int(seconds + 0.999)),
                        reason="rate_limited",
                        retry_after=seconds,
                    )
                continue  # the next admission waits for Retry-After
            if status >= 500:
                if not failed:
                    failed = True
                    self._note_failure(f"HTTP {status}")
                if last:
                    return response
                self.sleep(backoff * (2**attempt))
                continue
            self._note_answered()
            return response
        raise AssertionError("unreachable")


CLIENT = JitenClient()


def configure(cache_dir: Path | str | None) -> None:
    CLIENT.configure(cache_dir)


def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    return CLIENT.request(method, url, **kwargs)


def pause_reason() -> str:
    return CLIENT.pause_reason()
