"""Behaviour tests for the window pages (audit 71Б).

The pages run in node (tests/js/page.cjs: a small DOM, no browser). A test opens a
fresh page, runs JavaScript in it and gets back what that code returns, plus the
pywebview.api calls the page made and the errors it did not handle::

    def test_library_shows_unsynced_episodes(page):
        result = page.run(
            "renderLibrary(); return document.querySelector('.library-state').innerText",
            lang="ru",
            state={"library": [...]},
        )
        assert result.value == "не синхронизированы"

`run(code, ...)` options:
  lang         "en" / "ru" (the default state's settings.language)
  state        merged over a minimal get_state answer (settings and home are merged too)
  api          {"method": value} answers for pywebview.api (JSON values); for answers
               that depend on arguments use `before`
  before       JavaScript run before the page's scripts; `__api.method = async (...args) => ...`
  name         another page: "mpv_assistant.html", "audiobook_float.html"
  ready=False  do not fire pywebviewready (the page stays as before the bridge exists)
  settle=ms    advance the fake clock after start-up
  now          wall-clock time in ms (default 2026-10-01 12:00 UTC); timers run only on tick()
  viewport, media, localStorage, confirm, prompt, apiDefault, missingApi, bootstrap
  allow_errors errors the page did not handle fail the test unless this is set

In the page: everything the page defines (ui, t(), render functions ...), `document`,
`window`, and helpers:
  await __tick(ms)          advance the fake clock (timers, intervals and frames run only then)
  await __flush()           let pending promise callbacks run
  await __settle(promise)   await a page promise that needs time (frames/timers): ticks until it
                            settles. `await openLightNovel(7)` alone would wait forever.
  await __click(sel, init) / __input(sel, value) / __key('Escape', {metaKey: true}, sel)
  await __fire(sel, 'pointerdown', init, 'PointerEvent')
  await __setHidden(true)   window to the background (document.hidden, visibilitychange)
  await __setFocused(false) window loses focus (blur) / gains it (focus)
  __text(sel) innerText, __visible(sel), __toasts() current toast messages
Recorded: `__apiCalls`, `__dialogs`, `__clipboard`, `__fetches`, `__opened`.
Visibility follows the hidden attribute, inline styles, closed <details> and the stylesheets'
`display` rules (including @media by viewport width).

`before` runs inside a block: its `const`s do not clash with the page's globals; assign to
`window.x` / `__api.x` to leave something behind. Start-up calls have default answers
(page.cjs startupAnswers: versions, maintenance, permissions, empty LN/manga/audiobook
lists, review gates granted ...); get_state/get_state_fast answer with `state` (merged over
a minimal state), so a resync replaces changes a test made to ui.state directly.
Write JavaScript in Python raw strings (r-prefixed) so backslash escapes reach the page as written.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "js" / "page_worker.cjs"
TIMEOUT = float(os.environ.get("PUDGE_PAGE_TIMEOUT", "120"))


@dataclass
class PageResult:
    value: Any
    errors: list[str]
    load_errors: list[str]
    console: list[str]
    calls: list[dict[str, Any]]
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    clipboard: list[str] = field(default_factory=list)
    opened: list[dict[str, Any]] = field(default_factory=list)

    def calls_of(self, name: str) -> list[list[Any]]:
        """Argument lists of every pywebview.api.<name>(...) call, in order."""
        return [call["args"] for call in self.calls if call["name"] == name]

    def called(self, name: str) -> bool:
        return any(call["name"] == name for call in self.calls)


class _Worker:
    """The node page worker, started detached from the test process.

    The app's quit cleanup stops every descendant of its own process (process_cleanup:
    OwnedProcessTree of os.getpid()); tests exercise that path inside the test process.
    So the worker is started through a shell that exits at once: node is then not a
    descendant of pytest and no such cleanup can stop it in the middle of a page."""

    def __init__(self, node: str) -> None:
        # fd 3 keeps the pipe: a background job's own stdin would be /dev/null.
        command = f'exec 3<&0; "{node}" "{WORKER}" <&3 3<&- & echo $!'
        self._shell = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(ROOT),
            bufsize=1,
            start_new_session=True,
        )
        self.stdin = self._shell.stdin
        self.stdout = self._shell.stdout
        self.stderr = self._shell.stderr
        self.pid = int(self.stdout.readline().strip())  # type: ignore[union-attr]
        self._shell.wait()

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        try:
            with open(f"/proc/{self.pid}/status", encoding="utf-8") as handle:
                return not any(line.startswith("State:") and "Z" in line for line in handle)
        except OSError:
            return True

    def state(self) -> str:
        if not self.alive():
            return "the worker is gone"
        try:
            with open(f"/proc/{self.pid}/status", encoding="utf-8") as handle:
                return " ".join(line.strip() for line in handle if line.startswith("State"))
        except OSError:
            return "the worker is running"

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.stdin.close()  # type: ignore[union-attr]  # the worker exits at end of input
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.kill()


RECYCLE_AFTER = 120


class PageRunner:
    def __init__(self) -> None:
        self._process: _Worker | None = None
        self._lock = threading.Lock()
        self._next = 0
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=200)

    def _start(self) -> _Worker:
        if self._process is None or not self._process.alive():
            node = shutil.which("node")
            if node is None:
                pytest.skip("node is not installed")
            process = self._process = _Worker(node)

            # Drain the worker's stderr: a full pipe would block node (pipes are synchronous
            # on Linux) and every later page would time out. The tail is kept for messages.
            def drain() -> None:
                for line in process.stderr:  # type: ignore[union-attr]
                    self._stderr_tail.append(line)

            threading.Thread(target=drain, daemon=True, name="page-worker-stderr").start()
        return self._process

    def close(self) -> None:
        if self._process is not None:
            self._process.close()
            self._process = None

    def run(
        self,
        code: str = "",
        *,
        lang: str = "en",
        state: dict[str, Any] | None = None,
        api: dict[str, Any] | None = None,
        before: str = "",
        allow_errors: bool = False,
        **options: Any,
    ) -> PageResult:
        request_options = {key: value for key, value in options.items() if value is not None}
        request_options["lang"] = lang
        if state is not None:
            request_options["state"] = state
        if api is not None:
            request_options["api"] = api
        with self._lock:
            process = self._start()
            self._next += 1
            request = {
                "id": self._next,
                "options": request_options,
                "before": before,
                "run": _with_helpers(code),
            }
            line = json.dumps(request, ensure_ascii=False)
            result: dict[str, Any] = {}

            def exchange() -> None:
                process.stdin.write(line + "\n")  # type: ignore[union-attr]
                process.stdin.flush()  # type: ignore[union-attr]
                answer = process.stdout.readline()  # type: ignore[union-attr]
                result["line"] = answer

            worker = threading.Thread(target=exchange, daemon=True)
            worker.start()
            worker.join(TIMEOUT)
            if not worker.is_alive() and not result.get("line") and not process.alive():
                # The worker died under this request (e.g. the OS reclaimed its memory): pages are
                # independent, so the request runs once more on a fresh worker.
                self._process = None
                process = self._start()
                worker = threading.Thread(target=exchange, daemon=True)
                worker.start()
                worker.join(TIMEOUT)
            if worker.is_alive() or not result.get("line"):
                state = ("no answer; " if worker.is_alive() else "no output; ") + process.state()
                process.kill()
                self._process = None
                stderr = "".join(self._stderr_tail)[-4000:]
                raise AssertionError(
                    f"page did not answer within {TIMEOUT:.0f} s ({state}; an awaited promise that never settles?)\n{stderr}"
                )
            response = json.loads(result["line"])
            # A long-lived worker keeps growing (every page's modules and caches): start a fresh one
            # now and then so a full suite never runs it out of memory.
            if self._next % RECYCLE_AFTER == 0:
                self.close()
        if not response.get("ok"):
            raise AssertionError("page harness failed:\n" + str(response.get("failure")))
        page_result = PageResult(
            value=response.get("value"),
            errors=list(response.get("errors") or []),
            load_errors=list(response.get("loadErrors") or []),
            console=list(response.get("console") or []),
            calls=list(response.get("calls") or []),
            dialogs=list(response.get("dialogs") or []),
            clipboard=list(response.get("clipboard") or []),
            opened=list(response.get("opened") or []),
        )
        problems = page_result.load_errors + page_result.errors
        if problems and not allow_errors:
            raise AssertionError("the page raised errors it did not handle:\n" + "\n---\n".join(problems))
        return page_result


def _with_helpers(code: str) -> str:
    # The page's fake clock and a microtask flush, reachable from test code.
    return (
        "const __tick = async (ms = 0) => { await globalThis.__pageTick(ms); };"
        "const __flush = async () => { await globalThis.__pageTick(0); };"
        "const __click = (selector, init = {}) => globalThis.__page.click(selector, init);"
        "const __input = (selector, value, options) => globalThis.__page.input(selector, value, options);"
        "const __key = (key, init = {}, selector = null) => globalThis.__page.key(key, init, selector);"
        "const __fire = (selector, type, init = {}, kind) => globalThis.__page.fire(selector, type, init, kind);"
        "const __text = selector => document.querySelector(selector)?.innerText ?? null;"
        "const __visible = selector => { const el = document.querySelector(selector); return Boolean(el && el.checkVisibility()); };"
        "const __toasts = () => [...document.querySelectorAll('#toast .toast-item:not(.leaving)')].map(el => el.textContent);"
        "const __settle = (promise, options) => globalThis.__page.settle(promise, options);"
        "const __setHidden = hidden => globalThis.__page.setHidden(hidden);"
        "const __setFocused = focused => globalThis.__page.setFocused(focused);\n" + code
    )


_RUNNER: PageRunner | None = None


def web_module(name: str) -> str:
    """Source of one window module (pudge/web/<name>), to RUN it in node with stand-ins.

    For a module tested on its own through its public API; the page harness (`page`) is
    the usual way. Never search or cut this text: tests check behaviour (audit 71Б)."""
    return (ROOT / "pudge" / "web" / name).read_text(encoding="utf-8")


def runner() -> PageRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = PageRunner()
    return _RUNNER


def close_runner() -> None:
    global _RUNNER
    if _RUNNER is not None:
        _RUNNER.close()
        _RUNNER = None
