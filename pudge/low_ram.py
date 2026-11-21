"""Memory practices of the window process and the worker processes, and Low RAM mode.

Normal mode and Low RAM mode (experimental, Settings → Advanced, off by default) both use:

1. ``WindowPageUnloader``: a while after the main window is closed (hidden; Pudge keeps
   running), its page is let go: with the window process (pudge/window_process.py) that
   process ends (``release``/``restore``), so WebKit's memory goes back to macOS; reopening
   (Dock, menu, a second launch, a notification, the audiobook companion) brings it back.
   Normal mode waits ``NORMAL_UNLOAD_DELAY_SECONDS`` (a reopen within it shows the existing
   window at once), Low RAM mode ``UNLOAD_DELAY_SECONDS``. Scripts Python sends to the page
   meanwhile wait and run once the new page is ready. The page can veto an unload
   (``window.PudgeLowRam.unloadVeto()``: an episode it is following in mpv, unsaved UI
   preferences, a dialog), and so can Python (mpv playing). Background work never depends on
   the page. The in-process window (``PUDGE_WINDOW_PROCESS=0``) swaps its page for
   ``web/blank.html`` instead, and only in Low RAM mode.
2. ``run_worker``: heavy one-off work (Statistics, the AniList list/refresh, the intro pass)
   runs in a short-lived ``python -m pudge.low_ram_worker`` process that answers compact
   JSON, so its peak never stays in the main process. Any failure falls back to the same
   work in-process.

Only Low RAM mode: the lighter page (index.html / low_ram.js): off-screen cards skip layout
and painting, hidden tabs drop their DOM after a short while, covers load at list size and
do not move.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import subprocess
import threading
import time
from typing import Any, Callable, Iterator

UNLOAD_DELAY_SECONDS = 30.0  # Low RAM mode: a quick close/reopen keeps the page
NORMAL_UNLOAD_DELAY_SECONDS = 300.0  # normal mode: a reopen within 5 minutes is instant
UNLOAD_RETRY_SECONDS = 60.0  # vetoed (mpv, unsaved state): asked again while hidden
RELOAD_STALE_SECONDS = 30.0  # a reload whose page never reported ready is started again
MAX_PENDING_SCRIPTS = 32
WORKER_TIMEOUT_SECONDS = 120.0
PARK_DELAY_SECONDS = 5.0  # blank.html has loaded by then (a finished navigation re-attaches)
UNLOADED_REPORT_SECONDS = 60.0  # the memory line with the page let go
PARKED_CHECK_SECONDS = 2.0  # a parked window shown some other way comes back within this


# Set in a worker's environment (and while a worker task runs): work already in a worker
# never starts another one.
WORKER_ENV = "PUDGE_LOW_RAM_WORKER"
# ``PUDGE_WORKERS=0``: every worker task runs in-process (the test suite; an escape hatch).
WORKERS_DISABLE_ENV = "PUDGE_WORKERS"
_INLINE_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("pudge_low_ram_inline", default=False)


def enabled(config: Any) -> bool:
    return bool(getattr(getattr(config, "power", None), "low_ram_mode", False))


def in_worker() -> bool:
    return _INLINE_ONLY.get() or os.environ.get(WORKER_ENV) == "1"


def worker_allowed(config: Any = None) -> bool:
    """Heavy one-off work may go to a worker: this is not already a worker (both modes) and
    ``PUDGE_WORKERS=0`` is not set."""
    if os.environ.get(WORKERS_DISABLE_ENV, "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return not in_worker()


def window_unload_delay(config: Any) -> float:
    """Seconds after the window closed before its window process ends."""
    return UNLOAD_DELAY_SECONDS if enabled(config) else NORMAL_UNLOAD_DELAY_SECONDS


@contextlib.contextmanager
def inline_only() -> Iterator[None]:
    """A worker task: everything it calls does its work right here."""
    token = _INLINE_ONLY.set(True)
    try:
        yield
    finally:
        _INLINE_ONLY.reset(token)


def log_state(logger: Any, value: bool, reason: str) -> None:
    if logger is not None:
        try:
            logger.info("EVENT low_ram_mode enabled=%s reason=%s", str(bool(value)).lower(), reason)
        except Exception:  # noqa: BLE001 - logging must not break a setting
            pass


class WindowPageUnloader:
    """The unload/reload state machine of the main window's page.

    States: ``loaded`` (the real page), ``unloaded`` (the blank page), ``reloading`` (the real
    page is loading; scripts wait for ``page_ready``). ``hidden``/``shown`` come from the
    window lifecycle, ``page_ready`` from the page (``ui_push_ready``).
    """

    LOADED = "loaded"
    UNLOADED = "unloaded"
    RELOADING = "reloading"

    def __init__(
        self,
        window: Any,
        *,
        enabled: Callable[[], bool],
        page_url: Callable[[], str],
        blank_url: Callable[[], str],
        python_veto: Callable[[], str] | None = None,
        before_reload: Callable[[], None] | None = None,
        parking: Any = None,
        unloaded_report: Callable[[], None] | None = None,
        release: Callable[[], None] | None = None,
        restore: Callable[[], None] | None = None,
        logger: Any = None,
        delay: float | Callable[[], float] = UNLOAD_DELAY_SECONDS,
        retry: float = UNLOAD_RETRY_SECONDS,
        timer: Callable[[float, Callable[[], None]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window = window
        self._enabled = enabled
        self._page_url = page_url
        self._blank_url = blank_url
        self._python_veto = python_veto
        self._before_reload = before_reload
        # Optional: takes the web view out of the hidden window (its layers, IOSurfaces) and
        # puts it back (``park()``, ``unpark()``, ``visible()``); see CocoaWebViewParking.
        self._parking = parking
        self._unloaded_report = unloaded_report
        # Optional: what letting the page go and bringing it back mean instead of loading
        # blank.html / the page (the window process: end it / start it).
        self._release = release
        self._restore = restore
        self._side_timers: list[Any] = []
        self.parked = False
        self.logger = logger
        # A number, or a callable read at every close (Low RAM mode switched meanwhile).
        self._delay = delay
        self.retry = float(retry)
        self._timer_factory = timer or _daemon_timer
        self._clock = clock
        self._lock = threading.RLock()
        self._timer: Any = None
        self._pending: list[str] = []
        self._reload_started = 0.0
        self.state = self.LOADED
        self.hidden = False
        self.closed = False
        self.unloads = 0
        self.reloads = 0

    @property
    def delay(self) -> float:
        value = self._delay
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                value = UNLOAD_DELAY_SECONDS
        return float(value)

    # ------------------------------------------------------------------ lifecycle
    def hidden_now(self) -> None:
        """The window was closed to the background."""
        with self._lock:
            self.hidden = True
            if self.closed or not self._is_enabled():
                return
            self._schedule(self.delay)

    def shown_now(self) -> None:
        """The window is coming back: bring the page back if it was let go."""
        with self._lock:
            self.hidden = False
            self._cancel()
            self._cancel_side()
            self._unpark()
            stale = (
                self.state == self.RELOADING and self._clock() - self._reload_started >= RELOAD_STALE_SECONDS
            )
            if self.closed or not (self.state == self.UNLOADED or stale):
                return
            self._reload()

    def page_ready(self) -> None:
        """The real page finished starting (it installed window.PudgePush)."""
        with self._lock:
            # Only the real page reports (the blank page never does), also when the blank
            # page brought it back by itself (it reloads when it becomes visible).
            if self.state == self.UNLOADED:
                self._log("EVENT low_ram.window_page_back source=page")
                self._cancel_side()
                self._unpark()
            self.state = self.LOADED
            pending, self._pending = self._pending, []
            if self.hidden and not self.closed and self._timer is None and self._is_enabled():
                # Closed again while it loaded: its unload timer found it still loading and
                # stopped, so nothing would let the page go until the next close.
                self._schedule(self.delay)
        for script in pending:
            self._run(script)

    def loading(self) -> None:
        """The page is starting (a window process's first page): scripts wait for page_ready."""
        with self._lock:
            if self.closed:
                return
            self.state = self.RELOADING
            self._reload_started = self._clock()

    def page_gone(self) -> None:
        """The page went away by itself (its window process ended): the next reopen brings it back."""
        with self._lock:
            self.hidden = True
            if self.state == self.UNLOADED:
                return  # let go on purpose: its memory report stays planned
            self._cancel()
            self._cancel_side()
            self.state = self.UNLOADED
            self.unloads += 1

    def close(self) -> None:
        with self._lock:
            self.closed = True
            self._cancel()
            self._cancel_side()
            self._pending = []

    # ------------------------------------------------------------------ scripts
    def run_js(self, script: str) -> bool:
        """Run ``script`` in the real page now, or once it is back. True when it ran now."""
        with self._lock:
            if self.state != self.LOADED:
                if not self.closed:
                    self._pending = (self._pending + [str(script)])[-MAX_PENDING_SCRIPTS:]
                return False
        self._run(script)
        return True

    def _run(self, script: str) -> None:
        try:
            runner = getattr(self.window, "run_js", None)
            (runner if callable(runner) else self.window.evaluate_js)(script)
        except Exception as exc:  # noqa: BLE001 - a closing window
            self._log("SKIP step=low_ram.page_script error=%r", str(exc)[:200])

    # ------------------------------------------------------------------ unload
    def _is_enabled(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:  # noqa: BLE001
            return False

    def _schedule(self, delay: float) -> None:
        self._cancel()
        self._timer = self._timer_factory(max(0.0, float(delay)), self._unload_if_hidden)

    def _cancel(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass

    def veto(self) -> str:
        """Why the page must stay now ('' when it may go)."""
        if self._python_veto is not None:
            try:
                reason = str(self._python_veto() or "")
            except Exception as exc:  # noqa: BLE001
                reason = f"python_error:{type(exc).__name__}"
            if reason:
                return reason
        try:
            answer = self.window.evaluate_js("window.PudgeLowRam?window.PudgeLowRam.unloadVeto():'no_page'")
        except Exception as exc:  # noqa: BLE001 - no answer: keep the page
            return f"page_error:{type(exc).__name__}"
        return "" if answer in ("", None, False) else str(answer)

    def _unload_if_hidden(self) -> None:
        with self._lock:
            self._timer = None
            if self.closed or not self.hidden or self.state != self.LOADED or not self._is_enabled():
                return
        reason = self.veto()  # outside the lock: it waits for the page
        with self._lock:
            if self.closed or not self.hidden or self.state != self.LOADED:
                return
            if reason:
                self._log("EVENT low_ram.window_unload skipped=%s", reason)
                self._schedule(self.retry)
                return
            try:
                if self._release is not None:
                    self._release()
                else:
                    self.window.load_url(self._blank_url())
            except Exception as exc:  # noqa: BLE001 - the page simply stays
                self._log("FALLBACK step=low_ram.window_unload error=%r", str(exc)[:200])
                return
            self.state = self.UNLOADED
            self.unloads += 1
            if self._parking is not None:
                self._side(PARK_DELAY_SECONDS, self._park_if_unloaded)
            if self._unloaded_report is not None:
                self._side(UNLOADED_REPORT_SECONDS, self._report_if_unloaded)
        self._log(
            "EVENT low_ram.window_unload action=%s", "release" if self._release is not None else "blank"
        )

    # ------------------------------------------------------------------ parking
    def _side(self, delay: float, callback: Callable[[], None]) -> None:
        # Timers that already ran are dropped: the parked-window check re-arms itself every
        # few seconds, so a window hidden overnight kept tens of thousands of finished ones.
        self._side_timers = [timer for timer in self._side_timers if not _finished(timer)]
        self._side_timers.append(self._timer_factory(max(0.0, float(delay)), callback))

    def _cancel_side(self) -> None:
        timers, self._side_timers = self._side_timers, []
        for timer in timers:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _still_unloaded(self) -> bool:
        return not self.closed and self.hidden and self.state == self.UNLOADED

    def _park_if_unloaded(self) -> None:
        with self._lock:
            if not self._still_unloaded() or self.parked:
                return
            try:
                self._parking.park()
            except Exception as exc:  # noqa: BLE001 - the blank page stays in the window
                self._log("FALLBACK step=low_ram.webview_park error=%r", str(exc)[:200])
                return
            self.parked = True
            self._side(PARKED_CHECK_SECONDS, self._check_parked_visible)

    def _check_parked_visible(self) -> None:
        """A parked window brought forward without a reopen (the Window menu, Mission Control):
        put the web view back and load the page, as a reopen would."""
        with self._lock:
            if not self.parked or self.closed:
                return
            try:
                visible = bool(self._parking.visible())
            except Exception:  # noqa: BLE001 - unknown: look again
                visible = False
            if not visible:
                self._side(PARKED_CHECK_SECONDS, self._check_parked_visible)
                return
        self._log("EVENT low_ram.window_shown_while_parked")
        self.shown_now()

    def _unpark(self) -> None:
        if not self.parked:
            return
        self.parked = False
        try:
            self._parking.unpark()
        except Exception as exc:  # noqa: BLE001 - a finished navigation re-attaches it too
            self._log("FALLBACK step=low_ram.webview_unpark error=%r", str(exc)[:200])

    def _report_if_unloaded(self) -> None:
        with self._lock:
            if not self._still_unloaded():
                return
        try:
            self._unloaded_report()
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            self._log("FALLBACK step=low_ram.unloaded_report error=%r", str(exc)[:200])

    def _reload(self) -> None:
        if self._before_reload is not None:
            try:
                self._before_reload()
            except Exception as exc:  # noqa: BLE001 - the page loads anyway
                self._log("FALLBACK step=low_ram.before_reload error=%r", str(exc)[:200])
        try:
            if self._restore is not None:
                self._restore()
            else:
                self.window.load_url(self._page_url())
        except Exception as exc:  # noqa: BLE001 - the blank page reloads itself when shown
            self._log("FALLBACK step=low_ram.window_reload error=%r", str(exc)[:200])
        self.state = self.RELOADING
        self._reload_started = self._clock()
        self.reloads += 1
        self._log("EVENT low_ram.window_reload")

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message, *args)
            except Exception:  # noqa: BLE001
                pass


class CocoaWebViewParking:
    """Takes pywebview's WKWebView out of the hidden main window and puts it back (macOS).

    With the view out of any window WebKit treats the page as not in a window and lets go of
    its layer tree: the tiles and backing stores (IOSurfaces, mapped into this process too).
    The view object itself, its JS bridge and its delegates stay as they are, so evaluate_js,
    load_url and the page's calls into Python keep working; nothing of pywebview is rebuilt.
    A placeholder view of the window's background holds the window's content meanwhile.
    pywebview puts a view that finished a navigation back into its window by itself
    (``webView_didFinishNavigation_``), so parking waits until blank.html has loaded and a
    missed unpark is repaired by the next page load. All AppKit calls run on the main thread.
    """

    RETRY_SECONDS = 1.0
    MAX_RETRIES = 10

    def __init__(
        self,
        window: Any,
        *,
        logger: Any = None,
        call_after: Callable[..., Any] | None = None,
        browser_view: Callable[[], Any] | None = None,
        placeholder: Callable[[Any], Any] | None = None,
        later: Callable[[float, Callable[[], None]], Any] | None = None,
    ) -> None:
        self.window = window
        self.logger = logger
        self._call_after = call_after
        self._browser_view = browser_view
        self._placeholder = placeholder
        self._later = later or _daemon_timer
        self.wanted = False
        self.parked = False

    # ------------------------------------------------------------------ plumbing
    def _post(self, function: Callable[..., Any], *args: Any) -> None:
        if self._call_after is None:
            from PyObjCTools import AppHelper

            self._call_after = AppHelper.callAfter
        self._call_after(function, *args)

    def _view_host(self) -> Any:
        if self._browser_view is not None:
            return self._browser_view()
        from webview.platforms.cocoa import BrowserView

        return BrowserView.instances.get(getattr(self.window, "uid", None))

    def _new_placeholder(self, frame: Any) -> Any:
        if self._placeholder is not None:
            return self._placeholder(frame)
        import AppKit

        return AppKit.NSView.alloc().initWithFrame_(frame)

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message, *args)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ API
    def park(self) -> None:
        self.wanted = True
        self._post(self._park_now, 0)

    def unpark(self) -> None:
        self.wanted = False
        self._post(self._unpark_now)

    def visible(self) -> bool:
        native = getattr(self.window, "native", None)
        return bool(native is not None and native.isVisible())

    # ------------------------------------------------------------------ main thread
    def _park_now(self, attempt: int) -> None:
        if not self.wanted:
            return  # reopened meanwhile
        host = self._view_host()
        view = getattr(host, "webview", None)
        native = getattr(host, "window", None)
        if view is None or native is None:
            return
        if view.window() is None:
            self.parked = True
            return
        if view.isLoading():  # blank.html still loading: its finish would re-attach the view
            if attempt < self.MAX_RETRIES:
                self._later(self.RETRY_SECONDS, lambda: self._post(self._park_now, attempt + 1))
            return
        native.setContentView_(self._new_placeholder(native.contentView().frame()))
        self.parked = True
        self._log("EVENT low_ram.webview_parked")

    def _unpark_now(self) -> None:
        host = self._view_host()
        view = getattr(host, "webview", None)
        native = getattr(host, "window", None)
        self.parked = False
        if view is None or native is None or view.window() is not None:
            return
        native.setContentView_(view)
        native.makeFirstResponder_(view)
        self._log("EVENT low_ram.webview_back")


def _finished(timer: Any) -> bool:
    """A timer that ran or was cancelled (stand-ins without ``finished`` count as pending)."""
    done = getattr(timer, "finished", None)
    is_set = getattr(done, "is_set", None)
    alive = getattr(timer, "is_alive", None)
    if callable(is_set) and is_set():
        return not (callable(alive) and alive())
    return False


def _daemon_timer(delay: float, callback: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.name = "pudge-low-ram-unload"
    timer.start()
    return timer


# ---------------------------------------------------------------------- workers
class WorkerFailed(RuntimeError):
    pass


_ANSWER = threading.local()  # size of this thread's last worker answer (for the EVENT line)


def _last_json_line(stdout: str) -> Any:
    """The last non-empty line parsed, without copying the whole output into a list of lines
    first (an answer can be MBs; everything copied stays in this process's heap)."""
    _ANSWER.bytes = len(stdout)
    end = len(stdout.rstrip())
    if not end:
        return None
    start = stdout.rfind("\n", 0, end) + 1
    try:
        return json.loads(stdout[start:end])
    except ValueError:
        return None


def run_worker(
    task: str,
    payload: dict[str, Any],
    *,
    timeout: float = WORKER_TIMEOUT_SECONDS,
    runner: Callable[..., Any] = subprocess.run,
) -> Any:
    """Run ``python -m pudge.low_ram_worker <task>`` with ``payload`` as JSON on stdin.

    Returns the worker's ``result``; raises ``WorkerFailed`` for anything else (no answer,
    an error answer, a timeout), so the caller can do the work in-process instead.
    """
    from .runtime import python_executable, worker_environment

    try:
        completed = runner(
            [python_executable(), "-m", "pudge.low_ram_worker", str(task)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=float(timeout),
            env=worker_environment({WORKER_ENV: "1"}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkerFailed(f"{type(exc).__name__}: {exc}") from exc
    answer = _last_json_line(str(completed.stdout or ""))
    if not isinstance(answer, dict) or not answer.get("ok") or completed.returncode != 0:
        detail = answer.get("error") if isinstance(answer, dict) else str(completed.stderr or "")[-300:]
        raise WorkerFailed(f"exit={completed.returncode} {detail}")
    return answer.get("result")


def worker_or_inline(
    task: str,
    payload: dict[str, Any],
    inline: Callable[[], Any],
    *,
    use_worker: bool,
    logger: Any = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = WORKER_TIMEOUT_SECONDS,
) -> Any:
    """The worker's answer when ``use_worker`` and it works, ``inline()`` otherwise."""
    if not use_worker:
        return inline()
    started = time.monotonic()
    try:
        result = run_worker(task, payload, runner=runner, timeout=timeout)
    except WorkerFailed as exc:
        if logger is not None:
            logger.warning("FALLBACK step=low_ram.worker task=%s error=%r", task, str(exc)[:300])
        return inline()
    if logger is not None:
        logger.info(
            "EVENT low_ram.worker task=%s ms=%d bytes=%d",
            task,
            round((time.monotonic() - started) * 1000),
            int(getattr(_ANSWER, "bytes", 0) or 0),
        )
    return result
