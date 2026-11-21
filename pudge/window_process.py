"""The main window in its own process (``python -m pudge.window_process``), on macOS.

In normal mode and in Low RAM mode, the main process keeps everything except
the main window: the library, the scheduler, the iPhone server, agent work, the Dock icon and
the menu (a hidden placeholder window keeps pywebview's Cocoa loop, the companion windows and
the app delegate as they are). The window (pywebview + WKWebView + the page) runs in a child
process that the main process starts when the window opens and ends a while after the window
is closed (``WindowPageUnloader`` with ``release``/``restore``: 5 minutes in normal mode, 30 s
in Low RAM mode; a reopen meanwhile shows the same window at once), so every byte of WebKit,
AppKit, IOSurface, CGImage and malloc memory the window used goes back to macOS with it.

The two talk over a private socketpair (the child inherits its end; nothing listens on a path
or a port). The child proves itself with a random token it reads from its stdin, then:

- the page's ``pywebview.api.<name>`` calls: the child exposes exactly the names the main
  process sends (``js_bridge.bridge_functions`` of the real API: the same allowlist as an
  in-process window), each forwards ``call`` to the main process, which checks the name
  against the same set again and answers JSON. ``CHILD_METHODS`` (Finder drops, fullscreen)
  run in the child because they need its window and its NSApp;
- Python → page (``evaluate_js``/``run_js``), native file dialogs (``create_file_dialog``:
  they need the child's NSApp), show/hide/activate/exit and the window state go the other way
  through ``RemoteWindow``, which stands in for the pywebview window as ``api.window``.

While it runs, the child is the app the user sees: a regular app with Pudge's menu bar (Edit,
Window, Quit) shown while its window is in front, the Dock icon (a Dock click reopens its
window) and the Cmd+Tab entry; the main process steps back to an accessory meanwhile and is the
regular app again once the child is gone (one Pudge in the Dock at any time; an accessory never
owns the menu bar, which is why the main process cannot keep it for the child's window). Cmd+Q
in the window asks the main process to quit; closing the window hides it and the main process
ends the child once nothing needs the page (``unloadVeto``). A child that dies (or never says
hello) is logged; the next reopen starts a new one.

``PUDGE_WINDOW_PROCESS=0`` (the escape hatch): the window stays in the main process as before
(read at start, so it needs a restart).
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable

FD_ENV = "PUDGE_WINDOW_FD"
# ``Pudge.app/Contents/MacOS/<launcher> --pudge-window-process``: app_entry runs this module
# instead of the app, so the window process has Pudge's bundle identity (not Python.app's).
CHILD_ARG = "--pudge-window-process"
DISABLE_ENV = "PUDGE_WINDOW_PROCESS"
MAX_FRAME_BYTES = 256 * 1024 * 1024
HELLO_TIMEOUT_SECONDS = 30.0
EVAL_TIMEOUT_SECONDS = 15.0
STATE_TIMEOUT_SECONDS = 2.0
STOP_TIMEOUT_SECONDS = 5.0
ACTIVATE_FORWARD_SECONDS = 0.5
# Methods of the allowlist that the child answers itself: they need its window / NSApp.
CHILD_METHODS = frozenset({"bind_drop_import", "toggle_fullscreen", "exit_fullscreen"})
# What the main process may ask the child to do with its window.
WINDOW_REQUESTS = frozenset({"evaluate_js", "file_dialog", "state", "focus_process"})
WINDOW_NOTES = frozenset({"run_js", "show", "hide", "activate", "exit"})
MAX_DROP_PATHS = 1000


class ChannelClosed(RuntimeError):
    pass


class RemoteError(Exception):
    """An exception raised on the other side (its type name and message)."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.remote_name = str(name or "Exception")


class Channel:
    """Length-prefixed JSON messages over a stream socket, requests answered by id.

    Messages: ``{"t": "req", "id", "op", "a"}`` → ``{"t": "res", "id", "ok", "v" | "e", "n"}``;
    ``{"t": "note", "op", "a"}`` (no answer). Each request is handled on its own thread, so a
    blocking one (a file dialog, a long API call) never stops the reader.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        on_request: Callable[[str, Any], Any] | None = None,
        on_note: Callable[[str, Any], None] | None = None,
        on_close: Callable[[], None] | None = None,
        on_answered: Callable[[str, Any, int], None] | None = None,
        name: str = "pudge-window-channel",
        max_frame: int = MAX_FRAME_BYTES,
    ) -> None:
        self._sock = sock
        self._on_request = on_request
        self._on_note = on_note
        self._on_close = on_close
        # (op, args, bytes sent) after a request was answered: what the answer cost, measured on
        # the frame itself (the result is not encoded a second time for statistics).
        self._on_answered = on_answered
        self._name = name
        self._max = int(max_frame)
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, list[Any]] = {}
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent_bytes = 0
        self.received_bytes = 0

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_loop, name=self._name, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ sending
    def _send(self, message: dict[str, Any]) -> int:
        # surrogatepass: a lone surrogate (a file name decoded with surrogateescape, a JS string)
        # crosses as it is, as through in-process pywebview, instead of failing the whole message.
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", errors="surrogatepass"
        )
        if len(data) > self._max:
            raise ValueError(f"message too large: {len(data)} bytes")
        if self.closed:
            raise ChannelClosed("closed")
        failed: OSError | None = None
        with self._send_lock:
            try:
                self._sock.sendall(struct.pack(">I", len(data)) + data)
                self.sent_bytes += len(data) + 4
            except OSError as exc:
                failed = exc
        if failed is not None:
            # Outside the send lock: closing runs on_close, which may wait for the process.
            self.close()
            raise ChannelClosed(str(failed)) from failed
        return len(data)

    def notify(self, op: str, args: Any = None) -> bool:
        try:
            self._send({"t": "note", "op": str(op), "a": args})
            return True
        except (ChannelClosed, ValueError, TypeError):
            return False

    def request(self, op: str, args: Any = None, *, timeout: float | None = None) -> Any:
        event = threading.Event()
        with self._lock:
            self._next_id += 1
            ident = self._next_id
            slot: list[Any] = [event, None]
            self._waiting[ident] = slot
        try:
            self._send({"t": "req", "id": ident, "op": str(op), "a": args})
            if not event.wait(timeout):
                raise TimeoutError(f"{op}: no answer in {timeout}s")
        finally:
            with self._lock:
                self._waiting.pop(ident, None)
        answer = slot[1]
        if answer is None:
            raise ChannelClosed("closed before the answer")
        if not answer.get("ok"):
            raise RemoteError(str(answer.get("n") or "Exception"), str(answer.get("e") or ""))
        return answer.get("v")

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for method in (lambda: self._sock.shutdown(socket.SHUT_RDWR), self._sock.close):
            try:
                method()
            except OSError:
                pass
        with self._lock:
            waiting, self._waiting = self._waiting, {}
        for slot in waiting.values():
            slot[0].set()
        callback, self._on_close = self._on_close, None
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - closing must finish
                pass

    # ------------------------------------------------------------------ receiving
    def _read_exact(self, size: int) -> bytes | None:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            try:
                chunk = self._sock.recv(min(remaining, 1 << 20))
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_loop(self) -> None:
        try:
            while not self.closed:
                header = self._read_exact(4)
                if header is None:
                    return
                (size,) = struct.unpack(">I", header)
                if size > self._max:
                    return
                body = self._read_exact(size)
                if body is None:
                    return
                self.received_bytes += size + 4
                try:
                    message = json.loads(body.decode("utf-8", errors="surrogatepass"))
                except ValueError:
                    return
                if not isinstance(message, dict):
                    return
                self._dispatch(message)
        finally:
            self.close()

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "res":
            with self._lock:
                slot = self._waiting.get(message.get("id"))  # type: ignore[arg-type]
            if slot is not None:
                slot[1] = message
                slot[0].set()
        elif kind == "req":
            threading.Thread(
                target=self._answer, args=(message,), name=f"{self._name}-req", daemon=True
            ).start()
        elif kind == "note" and self._on_note is not None:
            try:
                self._on_note(str(message.get("op") or ""), message.get("a"))
            except Exception:  # noqa: BLE001 - one bad note must not end the channel
                pass

    def _answer(self, message: dict[str, Any]) -> None:
        ident = message.get("id")
        try:
            if self._on_request is None:
                raise PermissionError("no requests")
            value = self._on_request(str(message.get("op") or ""), message.get("a"))
            answer = {"t": "res", "id": ident, "ok": True, "v": value}
            try:
                size = self._send(answer)
            except (TypeError, ValueError) as exc:  # not JSON (pywebview would fail the same way)
                answer = {"t": "res", "id": ident, "ok": False, "n": type(exc).__name__, "e": str(exc)}
            else:
                callback = self._on_answered
                if callback is not None:
                    try:
                        callback(str(message.get("op") or ""), message.get("a"), size)
                    except Exception:  # noqa: BLE001 - statistics only
                        pass
                return
        except Exception as exc:  # noqa: BLE001 - the error goes back to the caller
            answer = {"t": "res", "id": ident, "ok": False, "n": type(exc).__name__, "e": str(exc)}
        try:
            self._send(answer)
        except (ChannelClosed, TypeError, ValueError):
            pass


# ====================================================================== main process side
def supported(config: Any = None) -> bool:
    """The window runs in its own process: macOS, not switched off by ``PUDGE_WINDOW_PROCESS=0``.

    Both modes (``config`` is kept for callers; Low RAM mode only shortens the unload delay)."""
    if sys.platform != "darwin":
        return False
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in {"0", "false", "no", "off"}


def bundle_launcher(bundle_path: str | None = None, executable: str | None = None) -> str:
    """Pudge.app's own launcher (``Contents/MacOS/<name>``) when this process runs from it; ''.

    Run directly (not through LaunchServices/open), it starts as another process of the app
    bundle (bundle id from its Info.plist) without the single-instance lock: app_entry hands
    ``CHILD_ARG`` to this module before anything else. A Pudge started with plain Python (the
    main bundle is Python.app) has no launcher: the caller falls back to ``python -m``.
    """
    if bundle_path is None or executable is None:
        if sys.platform != "darwin":
            return ""
        try:
            from Foundation import NSBundle

            bundle = NSBundle.mainBundle()
            bundle_path = str(bundle.bundlePath() or "")
            executable = str(bundle.executablePath() or "")
        except Exception:  # noqa: BLE001 - no Cocoa: no bundle
            return ""
    if not bundle_path.endswith(".app") or not executable:
        return ""
    import plistlib

    from .branding import APP_BUNDLE_ID

    try:
        # The file on disk: the running process rewrites its in-memory infoDictionary.
        with open(os.path.join(bundle_path, "Contents", "Info.plist"), "rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""
    if info.get("CFBundleIdentifier") != APP_BUNDLE_ID:
        return ""
    expected = os.path.join(bundle_path, "Contents", "MacOS", str(info.get("CFBundleExecutable") or ""))
    if os.path.realpath(executable) != os.path.realpath(expected) or not os.access(expected, os.X_OK):
        return ""
    return expected


def page_methods(api: Any) -> frozenset[str]:
    """The names an in-process window would expose (the allowlist ∩ real API methods)."""
    from . import js_bridge

    return frozenset(function.__name__ for function in js_bridge.bridge_functions(api))


class RemoteWindow:
    """Stands in for the pywebview window as ``api.window``; everything goes to the child."""

    uid = "window-process"
    native = None

    def __init__(self, host: "WindowProcessHost") -> None:
        self._host = host
        self.after_destroy: Callable[[], None] | None = None

    def evaluate_js(self, script: str, callback: Any = None) -> Any:
        value = self._host.request("evaluate_js", {"script": str(script)}, timeout=EVAL_TIMEOUT_SECONDS)
        if callable(callback):
            callback(value)
        return value

    def run_js(self, script: str) -> None:
        self._host.notify("run_js", {"script": str(script)})

    def focus_process(self, pid: int) -> bool | None:
        """Bring the app of ``pid`` (an episode's mpv) to the front from the window process: it
        is the active app while its window is clicked, and macOS 14+ lets only the active app
        hand activation over (this main process is an accessory meanwhile, so its own attempt
        was refused). None when the window process could not be asked."""
        try:
            return bool(self._host.request("focus_process", {"pid": int(pid)}, timeout=STATE_TIMEOUT_SECONDS))
        except (ChannelClosed, RuntimeError, TimeoutError, RemoteError, ValueError):
            return None

    def create_file_dialog(
        self,
        dialog_type: int = 10,
        directory: str = "",
        allow_multiple: bool = False,
        save_filename: str = "",
        file_types: Any = (),
    ) -> Any:
        """The dialog opens in the window process (its NSApp); None when it is not running."""
        try:
            value = self._host.request(
                "file_dialog",
                {
                    "dialog_type": int(dialog_type),
                    "directory": str(directory or ""),
                    "allow_multiple": bool(allow_multiple),
                    "save_filename": str(save_filename or ""),
                    "file_types": [str(item) for item in (file_types or ())],
                },
                timeout=None,
            )
        except (ChannelClosed, RuntimeError, TimeoutError, RemoteError):
            # Not running, still starting, or its window not ready: as a cancelled dialog.
            return None
        if value is None:
            return None
        if isinstance(value, list):
            return tuple(str(item) for item in value)
        return str(value)

    def show(self) -> None:
        self._host.show()

    def hide(self) -> None:
        self._host.notify("hide")

    def destroy(self) -> None:
        # Never waits for the process (the caller may be Cocoa's main thread or the bridge).
        self._host.release("destroy")
        callback = self.after_destroy
        if callable(callback):
            callback()

    def remote_state(self, wait: float = STATE_TIMEOUT_SECONDS) -> dict[str, Any]:
        """``visible``/``occluded``/``minimized`` of the child's window (``{}`` without one)."""
        if not self._host.running:
            return {}
        try:
            return dict(self._host.request("state", None, timeout=wait) or {})
        except (ChannelClosed, RuntimeError, TimeoutError, RemoteError):
            return {}  # starting, ending, or its window not ready yet


class WindowProcessHost:
    """Starts, talks to and ends the window process (one at a time)."""

    def __init__(
        self,
        api: Any,
        *,
        page_url: Callable[[], str],
        title: str,
        window_options: dict[str, Any] | None = None,
        logger: Any = None,
        popen: Callable[..., Any] = subprocess.Popen,
        command: Callable[[], list[str]] | None = None,
        environment: Callable[[], dict[str, str]] | None = None,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self.api = api
        self._page_url = page_url
        self.title = str(title)
        self.window_options = dict(window_options or {})
        self.logger = logger
        self._popen = popen
        self._command = command or self.default_command
        self._environment = environment or self.default_environment
        self._token_factory = token_factory
        self._lock = threading.RLock()
        self._process: Any = None
        self._channel: Channel | None = None
        self._token = ""
        self._ready = threading.Event()
        self._stopping = False
        self.methods: frozenset[str] = frozenset()
        self.window = RemoteWindow(self)
        self.window_shown = False
        self.starts = 0
        self.exits = 0
        self.crashes = 0
        self.calls = 0
        self.rejected = 0
        self.last_exit: dict[str, Any] = {}
        self._last_activate = 0.0
        self._authenticated = False
        self._spawned_at = 0.0
        # Callbacks the launch code sets (all optional).
        self.on_hidden: Callable[[], None] | None = None
        self.on_shown: Callable[[], None] | None = None
        self.on_gone: Callable[[bool], None] | None = None
        self.on_quit: Callable[[], None] | None = None
        self.on_drop: Callable[[list[str]], None] | None = None
        # The child became a regular app (its window is about to show): it owns the Dock icon
        # and the menu bar from now on (_SplitWindowLifecycle.window_ready).
        self.on_ready: Callable[[], None] | None = None
        self.hello_timeout = HELLO_TIMEOUT_SECONDS

    # ------------------------------------------------------------------ defaults
    @staticmethod
    def default_command() -> list[str]:
        launcher = bundle_launcher()
        if launcher:
            return [launcher, CHILD_ARG]
        from .runtime import python_executable

        return [python_executable(), "-m", "pudge.window_process"]

    @staticmethod
    def default_environment() -> dict[str, str]:
        from .runtime import worker_environment

        return worker_environment()

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._channel is not None and not self._channel.closed

    @property
    def stopping(self) -> bool:
        """The running process was asked to end (``release``/``stop``): a reopen starts another."""
        return bool(self._stopping)

    @property
    def pid(self) -> int:
        process = self._process
        return int(getattr(process, "pid", 0) or 0) if process is not None else 0

    def describe(self) -> str:
        state = f"running:{self.pid}" if self.running else "stopped"
        return (
            f"window_process={state} window_process_starts={self.starts} "
            f"window_process_crashes={self.crashes}"
        )

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message, *args)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Start the window process (False when one is already running: it is shown).

        A process being ended (``release``) does not count: a new one starts at once and the
        old one finishes going away on its own."""
        with self._lock:
            if self.running and not self._stopping:
                running = True
            else:
                running = False
                self._spawn()
        if running:
            self.show()
            return False
        return True

    def _spawn(self) -> None:
        if not self.methods:
            self.methods = page_methods(self.api)
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self._token = str(self._token_factory())
        self._ready.clear()
        self._stopping = False
        self.window_shown = False
        self.last_exit = {}  # a crash line must not show the previous process's "bye"
        environment = dict(self._environment())
        environment[FD_ENV] = str(child.fileno())
        started = time.monotonic()
        try:
            process = self._popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                env=environment,
                pass_fds=(child.fileno(),),
                close_fds=True,
            )
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        stdin = getattr(process, "stdin", None)
        try:
            if stdin is not None:
                stdin.write((self._token + "\n").encode("ascii"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                if stdin is not None:
                    stdin.close()  # EOF: a child that missed the token stops waiting for it
            except (OSError, ValueError):
                pass
        self._process = process
        channel = Channel(
            parent,
            on_request=self._handle_request,
            on_note=lambda op, args: self._handle_note(process, op, args),
            on_close=lambda: self._closed(process),
            on_answered=self._answered,
            name="pudge-window-host",
        )
        self._channel = channel
        self._authenticated = False
        self._spawned_at = started
        self.starts += 1
        channel.start()
        self._log("EVENT window_process.start pid=%s start=%s", self.pid, self.starts)
        # A child that never says hello (stuck before its window, the token lost) would leave
        # "running" set for good, and every reopen would only "show" it: it is ended instead,
        # and the next reopen starts a new one.
        watchdog = threading.Timer(self.hello_timeout, self._hello_overdue, args=(process, channel))
        watchdog.daemon = True
        watchdog.start()

    def _hello_overdue(self, process: Any, channel: "Channel") -> None:
        with self._lock:
            if self._process is not process or self._ready.is_set() or channel.closed:
                return
        if self.logger is not None:
            self.logger.warning(
                "FAIL step=window_process.no_hello pid=%s seconds=%.0f",
                getattr(process, "pid", 0),
                self.hello_timeout,
            )
        for method in ("kill",):
            try:
                getattr(process, method)()
            except Exception:  # noqa: BLE001 - gone already
                pass
        channel.close()

    def stop(self, reason: str = "closed", timeout: float = STOP_TIMEOUT_SECONDS) -> bool:
        """Ask the window process to close its window and end; kill it if it does not."""
        ended = self._begin_stop(reason)
        if ended is None:
            return False
        self._finish_stop(*ended, timeout=timeout)
        return True

    def release(self, reason: str = "closed") -> bool:
        """``stop`` without waiting: the process is asked to end and is waited for (and killed
        if it must be) on a thread of its own. The caller (the unloader under its lock, Cocoa's
        main thread) never waits for a slow WebKit teardown."""
        ended = self._begin_stop(reason)
        if ended is None:
            return False
        threading.Thread(target=self._finish_stop, args=ended, name="pudge-window-stop", daemon=True).start()
        return True

    def _begin_stop(self, reason: str) -> tuple[Any, "Channel | None"] | None:
        with self._lock:
            process, channel = self._process, self._channel
            if process is None:
                return None
            self._stopping = True
        self._log("EVENT window_process.stop pid=%s reason=%s", getattr(process, "pid", 0), reason)
        if channel is not None:
            channel.notify("exit", {"reason": str(reason)})
        return process, channel

    def _finish_stop(
        self, process: Any, channel: "Channel | None", timeout: float = STOP_TIMEOUT_SECONDS
    ) -> None:
        try:
            process.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 - it did not end in time
            for method in ("terminate", "kill"):
                try:
                    getattr(process, method)()
                    process.wait(timeout=2.0)
                    break
                except Exception:  # noqa: BLE001
                    continue
        if channel is not None:
            channel.close()

    def show(self) -> None:
        if self.notify("show"):
            self.window_shown = True

    def activate(self) -> bool:
        """Bring the child's window forward (a deliberate reopen: a second launch, a companion)."""
        now = time.monotonic()
        if not self.running or now - self._last_activate < ACTIVATE_FORWARD_SECONDS:
            return False
        self._last_activate = now
        return self.notify("activate")

    def notify(self, op: str, args: Any = None) -> bool:
        channel = self._channel
        if channel is None or channel.closed or not self._ready.is_set():
            return False
        return channel.notify(op, args)

    def request(self, op: str, args: Any = None, *, timeout: float | None = None) -> Any:
        channel = self._channel
        if channel is None or channel.closed:
            raise ChannelClosed("window process not running")
        if not self._ready.wait(HELLO_TIMEOUT_SECONDS if timeout is None else timeout):
            raise TimeoutError("window process not ready")
        return channel.request(op, args, timeout=timeout)

    def _closed(self, process: Any) -> None:
        """The channel ended: the process exited (asked to, or not)."""
        with self._lock:
            current = self._process is process
        if not current:
            # A newer process took over (a reopen while this one was ending): only reap this
            # one, so it is never left a zombie; its state is not the host's any more.
            self._reap(process)
            self.exits += 1
            return
        with self._lock:
            if self._process is not process:
                return
            expected = self._stopping
            self._process = None
            self._channel = None
            self._ready.clear()
            self.window_shown = False
        code = self._reap(process)
        self.exits += 1
        crashed = not expected
        if crashed:
            self.crashes += 1
            if self.logger is not None:
                self.logger.warning(
                    "FAIL step=window_process.crashed pid=%s exit=%s bye=%s",
                    getattr(process, "pid", 0),
                    code,
                    self.last_exit or "{}",
                )
        else:
            self._log(
                "EVENT window_process.exit pid=%s exit=%s bye=%s",
                getattr(process, "pid", 0),
                code,
                self.last_exit,
            )
        callback = self.on_gone
        if callback is not None:
            try:
                callback(crashed)
            except Exception as exc:  # noqa: BLE001
                self._log("FALLBACK step=window_process.on_gone error=%r", str(exc)[:200])

    @staticmethod
    def _reap(process: Any) -> Any:
        code = None
        try:
            code = process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - still running without its channel: end it
            for method in ("kill",):
                try:
                    getattr(process, method)()
                    code = process.wait(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
        return code

    # ------------------------------------------------------------------ from the child
    def _answered(self, op: str, args: Any, size: int) -> None:
        """A page call answered: counted with the size of the frame that carried the result."""
        if op != "call" or not isinstance(args, dict):
            return
        name = args.get("method")
        if isinstance(name, str) and name in self.methods:
            from . import js_bridge

            js_bridge.BRIDGE_STATS.record(name, int(size))

    def _handle_request(self, op: str, args: Any) -> Any:
        if not self._authenticated:
            raise PermissionError("not authenticated")
        if op != "call" or not isinstance(args, dict):
            self.rejected += 1
            raise PermissionError(f"unknown request {op!r}")
        name = args.get("method")
        params = args.get("args")
        if not isinstance(name, str) or name not in self.methods or name in CHILD_METHODS:
            self.rejected += 1
            raise PermissionError(f"not a page method: {name!r}")
        if not isinstance(params, list):
            raise TypeError("args must be a list")
        self.calls += 1
        # Called by name on the real API object, exactly like js_bridge's in-process functions.
        # BRIDGE_STATS counts it once answered (``_answered``: the frame's size, no second dump).
        return getattr(self.api, name)(*params)

    def _handle_note(self, process: Any, op: str, args: Any) -> None:
        if op == "hello":
            self._hello(process, args)
            return
        if not self._authenticated:
            return
        data = args if isinstance(args, dict) else {}
        if op == "event":
            name = str(data.get("name") or "")
            self._log("EVENT window_process.window_%s pid=%s", name, getattr(process, "pid", 0))
            if name == "hidden":
                self.window_shown = False
                self._call(self.on_hidden)
            elif name == "shown":
                self.window_shown = True
                self._call(self.on_shown)
            elif name == "regular":
                # Its Dock icon is there: this process steps back (one Pudge in the Dock).
                try:
                    self._call(self.on_ready)
                except Exception as exc:  # noqa: BLE001 - cosmetics (Dock icon, menu bar)
                    self._log("FALLBACK step=window_process.on_ready error=%r", str(exc)[:200])
        elif op == "drop":
            paths = [str(item) for item in (data.get("paths") or []) if isinstance(item, str) and item]
            if paths and self.on_drop is not None:
                self.on_drop(paths[:MAX_DROP_PATHS])
        elif op == "quit":
            self._log("EVENT window_process.quit_requested pid=%s", getattr(process, "pid", 0))
            self._call(self.on_quit)
        elif op == "bye":
            self.last_exit = {
                key: data[key] for key in ("rss_mb", "footprint_mb", "reason", "calls") if key in data
            }
        elif op == "log":
            text = str(data.get("message") or "")[:2000]
            level = str(data.get("level") or "info")
            if self.logger is not None and text:
                (self.logger.warning if level == "warning" else self.logger.info)(
                    "EVENT window_process.log pid=%s %s", getattr(process, "pid", 0), text
                )

    def _hello(self, process: Any, args: Any) -> None:
        channel = self._channel
        token = str((args or {}).get("token") or "") if isinstance(args, dict) else ""
        if channel is None or self._authenticated or not hmac.compare_digest(token, self._token):
            self.rejected += 1
            if self.logger is not None:
                self.logger.warning("FAIL step=window_process.hello reason=bad_token")
            if channel is not None and not self._authenticated:
                channel.close()
            return
        self._authenticated = True
        self._token = ""
        channel.notify(
            "config",
            {
                "url": self._page_url(),
                "title": self.title,
                "options": self.window_options,
                "methods": sorted(self.methods),
                "child_methods": sorted(self.methods & CHILD_METHODS),
                # Like an in-process window: web storage lives for one Pudge session, so only
                # the first window process of a session starts from empty storage.
                "clear_storage": self.starts <= 1,
            },
        )
        self._ready.set()
        self.window_shown = True
        self._log(
            "EVENT window_process.ready pid=%s ms=%d methods=%d",
            getattr(process, "pid", 0),
            round((time.monotonic() - getattr(self, "_spawned_at", time.monotonic())) * 1000),
            len(self.methods),
        )

    @staticmethod
    def _call(callback: Callable[[], None] | None) -> None:
        if callback is not None:
            callback()


# ====================================================================== window process side
class _ChildWindow:
    """The child: one pywebview window whose page talks to the main process."""

    def __init__(self, channel_socket: socket.socket, token: str) -> None:
        self.token = token
        self.window: Any = None
        self.webview: Any = None
        self.config: dict[str, Any] = {}
        self._config_ready = threading.Event()
        self.exiting = False
        self.hidden = False
        self.calls = 0
        self._drop_bound = False
        self._drop_handlers: list[Any] = []
        self._fullscreen_lock = threading.Lock()
        self._fullscreen_pending = False
        self._notes: list[tuple[str, Any]] = []
        self._notes_lock = threading.Condition()
        self._notes_thread: threading.Thread | None = None
        # A quit this process answered "later" (NSTerminateLater): replied once the main
        # process ends this window (``_quit_reply(True)``), or given up (``False``).
        self._quit_pending = False
        self._quit_lock = threading.Lock()
        self._quit_reply: Callable[[bool], None] | None = None
        self._quit_timer: threading.Timer | None = None
        self.channel = Channel(
            channel_socket,
            on_request=self._handle_request,
            on_note=self._handle_note,
            on_close=self._main_gone,
            name="pudge-window-child",
        )

    def log(self, message: str, level: str = "info") -> None:
        self.channel.notify("log", {"message": str(message), "level": level})

    # ------------------------------------------------------------------ the page's API
    def proxy(self, name: str) -> Callable[..., Any]:
        if name in CHILD_METHODS:
            local = getattr(self, "_local_" + name)

            def call(*args: Any) -> Any:
                return local(*args)

        else:

            def call(*args: Any) -> Any:
                self.calls += 1
                try:
                    return self.channel.request("call", {"method": name, "args": list(args)})
                except RemoteError as exc:
                    raise _remote_exception(exc.remote_name, str(exc)) from None

        call.__name__ = call.__qualname__ = name
        return call

    def _local_bind_drop_import(self) -> dict[str, Any]:
        if self._drop_bound:
            return {"ok": True, "bound": True, "already_bound": True}
        if self.window is None:
            return {"ok": False, "bound": False, "error": "window not ready"}
        try:
            from webview.dom import DOMEventHandler

            element = self.window.dom.get_element("#app")
            if element is None:
                return {"ok": False, "bound": False, "error": "#app not ready"}

            def dropped(event: dict[str, Any]) -> None:
                transfer = event.get("dataTransfer") or event.get("domTransfer") or {}
                paths = [
                    str(item.get("pywebviewFullPath") or "")
                    for item in (transfer.get("files") or [])
                    if isinstance(item, dict) and item.get("pywebviewFullPath")
                ]
                if paths:
                    self.channel.notify("drop", {"paths": paths})
                else:
                    self.log("SKIP step=app.drop_import reason=no_absolute_paths")

            handler = DOMEventHandler(dropped, True, True)
            element.events.drop += handler
            self._drop_handlers = [element, handler]
            self._drop_bound = True
            self.log("EVENT app.drop_import_bound target=#app timing=pywebviewready")
            return {"ok": True, "bound": True, "already_bound": False}
        except Exception as exc:  # noqa: BLE001
            self.log(f"FALLBACK step=app.drop_import_bind error={str(exc)!r}", "warning")
            return {"ok": False, "bound": False, "error": str(exc)}

    def _fullscreen(self, only_exit: bool) -> dict[str, Any]:
        with self._fullscreen_lock:
            if self._fullscreen_pending:
                return {"ok": True, "scheduled": False, "pending": True}
            self._fullscreen_pending = True

        def release() -> None:
            with self._fullscreen_lock:
                self._fullscreen_pending = False

        def on_main() -> None:
            try:
                from AppKit import NSApplication, NSWindowStyleMaskFullScreen

                application = NSApplication.sharedApplication()
                native = application.keyWindow() or application.mainWindow()
                if native is not None and (
                    not only_exit or int(native.styleMask()) & int(NSWindowStyleMaskFullScreen)
                ):
                    native.toggleFullScreen_(None)
            except Exception as exc:  # noqa: BLE001
                self.log(f"FAIL step=app.fullscreen error={str(exc)!r}", "warning")
            finally:
                timer = threading.Timer(1.5, release)
                timer.daemon = True
                timer.start()

        try:
            _on_main_thread(on_main)
        except Exception:  # noqa: BLE001
            release()
            return {"ok": False, "scheduled": False}
        return {"ok": True, "scheduled": True, **({"exited": True} if only_exit else {})}

    def _local_toggle_fullscreen(self) -> dict[str, Any]:
        return self._fullscreen(False)

    def _local_exit_fullscreen(self) -> dict[str, Any]:
        return self._fullscreen(True)

    # ------------------------------------------------------------------ from the main process
    def _handle_request(self, op: str, args: Any) -> Any:
        if op not in WINDOW_REQUESTS:
            raise PermissionError(f"unknown request {op!r}")
        data = args if isinstance(args, dict) else {}
        if op == "focus_process":
            from .window_focus import bring_process_to_front

            return bool(bring_process_to_front(int(data.get("pid") or 0)))
        window = self.window
        if window is None:
            raise RuntimeError("window not ready")
        if op == "evaluate_js":
            return window.evaluate_js(str(data.get("script") or ""))
        if op == "file_dialog":
            value = window.create_file_dialog(
                int(data.get("dialog_type") or 10),
                str(data.get("directory") or ""),
                bool(data.get("allow_multiple")),
                str(data.get("save_filename") or ""),
                tuple(str(item) for item in (data.get("file_types") or [])),
            )
            if value is None:
                return None
            return [str(item) for item in value] if isinstance(value, (list, tuple)) else str(value)
        return self._window_state()

    def _window_state(self) -> dict[str, Any]:
        native = getattr(self.window, "native", None)
        if native is None:
            return {}
        answer: dict[str, Any] = {}
        done = threading.Event()

        def read() -> None:
            try:
                from .memory_diagnostics import _read_window_state

                answer.update(_read_window_state(native))
            except Exception as exc:  # noqa: BLE001
                answer["error"] = type(exc).__name__
            finally:
                done.set()

        _on_main_thread(read)
        done.wait(STATE_TIMEOUT_SECONDS)
        try:
            from .memory_diagnostics import footprint_mb, rss_mb

            answer["rss_mb"] = round(rss_mb(), 1)
            answer["footprint_mb"] = round(footprint_mb(), 1)
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
        return answer

    def _handle_note(self, op: str, args: Any) -> None:
        if op == "config":
            if not self.config and isinstance(args, dict):
                self.config = args
                self._config_ready.set()
            return
        if op == "exit":
            data = args if isinstance(args, dict) else {}
            self.close_and_exit(str(data.get("reason") or "main"))
            self._config_ready.set()  # asked to end before the window existed
            return
        if op not in WINDOW_NOTES or self.window is None:
            return
        # pywebview's run_js waits for the page (and for Cocoa's main thread): never on the
        # channel's reader, which must keep answering the page's calls. One ordered queue.
        with self._notes_lock:
            self._notes.append((op, args))
            if self._notes_thread is None:
                self._notes_thread = threading.Thread(
                    target=self._deliver_notes, name="pudge-window-notes", daemon=True
                )
                self._notes_thread.start()
            self._notes_lock.notify_all()

    def _deliver_notes(self) -> None:
        while True:
            with self._notes_lock:
                while not self._notes and not self.exiting:
                    self._notes_lock.wait()
                if self.exiting:
                    return
                op, args = self._notes.pop(0)
            try:
                self._run_note(op, args)
            except Exception as exc:  # noqa: BLE001 - a closing window
                self.log(f"SKIP step=window_process.note op={op} error={str(exc)[:200]!r}")

    def _run_note(self, op: str, args: Any) -> None:
        data = args if isinstance(args, dict) else {}
        if op == "run_js":
            runner = getattr(self.window, "run_js", None)
            (runner if callable(runner) else self.window.evaluate_js)(str(data.get("script") or ""))
        elif op == "show":
            self.show()
        elif op == "hide":
            self.hidden = True
            self.window.hide()
        elif op == "activate":
            self.activate()

    def show(self) -> None:
        native = getattr(self.window, "native", None)
        was_hidden = self.hidden
        self.hidden = False

        def restore() -> None:
            if native is not None and native.isMiniaturized():
                native.deminiaturize_(None)

        if native is not None:
            _on_main_thread(restore)
        self.window.show()
        if was_hidden:
            self.channel.notify("event", {"name": "shown"})

    def activate(self) -> None:
        native = getattr(self.window, "native", None)
        if self.hidden or native is None:
            return

        def bring() -> None:
            _activate_this_app()
            if not native.isMiniaturized():
                native.makeKeyAndOrderFront_(None)

        _on_main_thread(bring)

    def close_and_exit(self, reason: str) -> None:
        if self.exiting:
            return
        self.exiting = True
        self._exit_reason = reason
        with self._notes_lock:
            self._notes.clear()
            self._notes_lock.notify_all()
        # A quit (Cmd+Q, an Apple Event such as `osascript ... to quit`) that waits for its
        # answer gets yes now, and Cocoa ends this process the usual way.
        if not self._answer_quit(True):
            try:
                if self.window is not None:
                    self.window.destroy()
            except Exception:  # noqa: BLE001
                pass
        # A window that does not close (a modal sheet, a stuck WebKit) must not keep the process.
        timer = threading.Timer(STOP_TIMEOUT_SECONDS, lambda: os._exit(0))
        timer.daemon = True
        timer.start()

    def _main_gone(self) -> None:
        """The main process ended (or closed the channel): this window goes too."""
        self._config_ready.set()
        if not self.exiting:
            self.close_and_exit("main_gone")

    # ------------------------------------------------------------------ quit
    QUIT_ANSWER_TIMEOUT_SECONDS = 60.0

    def should_terminate(self, *, logout: bool = False) -> int:
        """applicationShouldTerminate_ of this process: 1 now, 2 later (NSTerminateLater).

        A quit of the app (Cmd+Q, the Dock, an Apple Event: ``tell application id ... to
        quit``) goes to the main process, which quits everything and ends this window; the
        quit is answered yes then. It used to be cancelled here (NSTerminateCancel) while the
        main process quit, so the sender of an Apple Event got -128 "User canceled".
        """
        if self.exiting or self.channel.closed or logout:
            # Log out / restart / shut down: the main process gets its own quit; a cancel
            # here would stop the logout ("Pudge Window" interrupted it).
            return 1
        with self._quit_lock:
            first = not self._quit_pending
            self._quit_pending = True
        if first:
            self.log("quit asked: the main process quits Pudge")
            self.channel.notify("quit")
            timer = threading.Timer(self.QUIT_ANSWER_TIMEOUT_SECONDS, self._quit_not_answered)
            timer.daemon = True
            self._quit_timer = timer
            timer.start()
        return 2

    def _quit_not_answered(self) -> None:
        if self._answer_quit(False):
            self.log("quit not finished by the main process; this window stays", level="warning")

    def _answer_quit(self, accept: bool) -> bool:
        """Reply to a quit answered "later". False when none waits."""
        with self._quit_lock:
            if not self._quit_pending:
                return False
            self._quit_pending = False
            timer, self._quit_timer = self._quit_timer, None
        if timer is not None:
            timer.cancel()
        reply = self._quit_reply
        if reply is None:
            return False
        try:
            reply(bool(accept))
        except Exception:  # noqa: BLE001 - the exit timer still ends the process
            return False
        return True

    # ------------------------------------------------------------------ window events
    def closing(self) -> bool:
        """The red button / Cmd+W: hide (the main process ends this process when it may)."""
        if self.exiting:
            return True
        try:
            self.window.hide()
        except Exception:  # noqa: BLE001
            return True
        self.hidden = True
        self.channel.notify("event", {"name": "hidden"})
        return False

    def before_show(self) -> None:
        _become_window_app()
        self.channel.notify("event", {"name": "regular"})
        self.install_app_delegate()
        # After pywebview's own menu (set right before its run loop starts): Pudge's menu bar
        # with Undo/Redo and the Window menu (Close, Minimize), shown while this window is in
        # front; Quit asks the main process to quit everything.
        _after_loop_starts(lambda: install_main_menu(_APP_NAME, view_menu=True))

    def install_app_delegate(self) -> None:
        """A quit of the app asks the main process to quit; a Dock/Finder reopen shows it."""
        try:
            from AppKit import NSApplication
            from Foundation import NSObject, YES
            from webview.platforms.cocoa import BrowserView
        except Exception:  # noqa: BLE001 - not on macOS
            return
        try:
            from AppKit import NSModalPanelRunLoopMode
            from Foundation import NSDefaultRunLoopMode

            reply_modes = [NSModalPanelRunLoopMode, NSDefaultRunLoopMode]
        except Exception:  # noqa: BLE001
            reply_modes = ["NSModalPanelRunLoopMode", "kCFRunLoopDefaultMode"]
        child = self

        class PudgeWindowProcessDelegate(NSObject):
            def applicationShouldTerminate_(self, app):
                return child.should_terminate(logout=_quit_for_logout())

            def pudgeAnswerQuit_(self, accept):
                NSApplication.sharedApplication().replyToApplicationShouldTerminate_(bool(accept))

            def applicationSupportsSecureRestorableState_(self, app):
                return YES

            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible_windows):
                child.show()
                return YES

        delegate = PudgeWindowProcessDelegate.alloc().init()
        self._delegate = delegate
        # On the main thread, also in the modal loop a "later" answer runs.
        self._quit_reply = lambda accept: (
            delegate.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
                "pudgeAnswerQuit:", accept, False, reply_modes
            )
        )
        BrowserView._shared_app_delegate = delegate
        NSApplication.sharedApplication().setDelegate_(delegate)

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        self.channel.start()
        self.channel.notify("hello", {"token": self.token, "pid": os.getpid()})
        self.token = ""
        if not self._config_ready.wait(HELLO_TIMEOUT_SECONDS) or not self.config or self.exiting:
            return 2
        config = self.config
        import webview

        if sys.platform == "darwin":
            # Importing pywebview's Cocoa backend makes the process a regular app (Dock icon):
            # import it now and become an accessory again before the window exists.
            try:
                import webview.platforms.cocoa  # noqa: F401
            except ModuleNotFoundError:  # a stand-in pywebview (tests): no Cocoa backend to import
                pass
            _become_window_app()
        self.webview = webview
        options = dict(config.get("options") or {})
        if isinstance(options.get("min_size"), list):
            options["min_size"] = tuple(options["min_size"])
        window = webview.create_window(str(config.get("title") or "Pudge"), url=str(config["url"]), **options)
        names = [str(name) for name in (config.get("methods") or []) if isinstance(name, str)]
        window.expose(*[self.proxy(name) for name in names])
        self.window = window
        window.events.closing += self.closing
        window.events.loaded += lambda: self.channel.notify("event", {"name": "loaded"})
        window.events.before_show += self.before_show
        webview.start(gui="cocoa", debug=False, private_mode=bool(config.get("clear_storage", True)))
        bye: dict[str, Any] = {"reason": getattr(self, "_exit_reason", "closed"), "calls": self.calls}
        try:
            from .memory_diagnostics import footprint_mb, rss_mb

            bye["rss_mb"] = round(rss_mb(), 1)
            bye["footprint_mb"] = round(footprint_mb(), 1)
        except Exception:  # noqa: BLE001
            pass
        self.channel.notify("bye", bye)
        self.channel.close()
        return 0


def _remote_exception(name: str, message: str) -> Exception:
    """An exception whose type name is the main process's (pywebview shows ``name`` to the page)."""
    safe = name if name.isidentifier() else "Exception"
    return type(safe, (Exception,), {})(message)


def _on_main_thread(function: Callable[[], None]) -> None:
    if threading.current_thread() is threading.main_thread():
        function()
        return
    from PyObjCTools import AppHelper

    AppHelper.callAfter(function)


def _become_accessory() -> None:
    """No Dock icon and no Cmd+Tab entry while the process starts (no window yet)."""
    if sys.platform != "darwin" or os.environ.get("PUDGE_NO_DOCK_CHANGES"):
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        from Foundation import NSProcessInfo

        NSProcessInfo.processInfo().setProcessName_("Pudge Window")
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:  # noqa: BLE001 - cosmetics
        pass


_APP_NAME = "Pudge"
try:  # the brand's name (Pudge); a plain default keeps this module importable alone
    from .branding import APP_NAME as _APP_NAME
except Exception:  # noqa: BLE001
    pass


def _become_window_app() -> None:
    """While it runs, the window process is the app the user sees: a regular app, so its menu
    bar (Pudge, Edit, Window) is the one shown while its window is in front, it is in Cmd+Tab and
    a Dock click reopens its window. The main process steps back to an accessory meanwhile
    (``_SplitWindowLifecycle.window_ready``): one Pudge in the Dock at any time."""
    if sys.platform != "darwin" or os.environ.get("PUDGE_NO_DOCK_CHANGES"):
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular

        application = NSApplication.sharedApplication()
        application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        if not bundle_has_own_icon():
            # Started with plain Python (python -m): Pudge's icon instead of Python's rocket.
            icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "app-icon.png")
            from AppKit import NSImage

            image = NSImage.alloc().initWithContentsOfFile_(icon) if os.path.isfile(icon) else None
            if image is not None:
                application.setApplicationIconImage_(image)
    except Exception:  # noqa: BLE001 - cosmetics
        pass


def background_launch_once() -> bool:
    """True once after a background launch (``--pudge-background``): that first bring-to-front is
    skipped so an installer relaunch never takes the screen; later activations work as usual."""
    return os.environ.pop("PUDGE_LAUNCH_BACKGROUND", "") == "1"


def _activate_this_app() -> None:
    """Bring this app to the front (``activate`` on macOS 14+, where the older call is deprecated)."""
    if background_launch_once():
        return
    from AppKit import NSApp

    if NSApp.respondsToSelector_("activate"):
        NSApp.activate()
    else:
        NSApp.activateIgnoringOtherApps_(True)


def _after_loop_starts(function: Callable[[], Any]) -> None:
    """Run ``function`` on the main thread once its run loop runs (after pywebview's set-up)."""
    if sys.platform != "darwin":
        return
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(function)
    except Exception:  # noqa: BLE001 - cosmetics
        pass


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


# kAEQuitReason values of a quit sent for log out, restart or shut down (AERegistry.h).
LOGOUT_QUIT_REASONS = frozenset(_fourcc(code) for code in ("logo", "rlgo", "rrst", "rsdn", "rest", "shut"))


def quit_reason_is_logout(reason: int | None) -> bool:
    return reason is not None and int(reason) in LOGOUT_QUIT_REASONS


def _quit_for_logout() -> bool:
    """The quit being handled comes from a log out / restart / shut down (not Cmd+Q)."""
    try:
        from Foundation import NSAppleEventManager

        event = NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
        if event is None:
            return False
        reason = event.attributeDescriptorForKeyword_(_fourcc("why?"))
        return quit_reason_is_logout(None if reason is None else int(reason.enumCodeValue()))
    except Exception:  # noqa: BLE001 - unknown: an ordinary quit
        return False


def bundle_has_own_icon(bundle_path: str | None = None) -> bool:
    """This process runs from Pudge.app (its Info.plist on disk: Pudge's bundle id) and the
    bundle has its icon file: the Dock, Cmd+Tab and Force Quit show that icon by themselves,
    so nothing needs to decode an icon image in this process."""
    if bundle_path is None:
        if sys.platform != "darwin":
            return False
        try:
            from Foundation import NSBundle

            bundle_path = str(NSBundle.mainBundle().bundlePath() or "")
        except Exception:  # noqa: BLE001 - no Cocoa: no bundle
            return False
    if not str(bundle_path).endswith(".app"):
        return False
    import plistlib

    from .branding import APP_BUNDLE_ID

    try:
        with open(os.path.join(bundle_path, "Contents", "Info.plist"), "rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    icon = str(info.get("CFBundleIconFile") or "")
    if info.get("CFBundleIdentifier") != APP_BUNDLE_ID or not icon:
        return False
    if not os.path.splitext(icon)[1]:
        icon += ".icns"
    return os.path.isfile(os.path.join(bundle_path, "Contents", "Resources", icon))


def main_menu_items(
    app_name: str, *, view_menu: bool = False
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """(menu title, [(label, action, key)]) of Pudge's menu bar ('' label: a separator). A key in
    capitals means Shift; "^f" means Control+Command+F."""
    menus: list[tuple[str, list[tuple[str, str, str]]]] = [
        (
            app_name,
            [
                (f"About {app_name}", "orderFrontStandardAboutPanel:", ""),
                ("", "", ""),
                (f"Hide {app_name}", "hide:", "h"),
                ("Hide Others", "hideOtherApplications:", "~h"),
                ("Show All", "unhideAllApplications:", ""),
                ("", "", ""),
                (f"Quit {app_name}", "terminate:", "q"),
            ],
        ),
        (
            "Edit",
            [
                ("Undo", "undo:", "z"),
                ("Redo", "redo:", "Z"),
                ("", "", ""),
                ("Cut", "cut:", "x"),
                ("Copy", "copy:", "c"),
                ("Paste", "paste:", "v"),
                ("Select All", "selectAll:", "a"),
            ],
        ),
    ]
    if view_menu:
        menus.append(("View", [("Enter Full Screen", "toggleFullScreen:", "^f")]))
    menus.append(
        (
            "Window",
            [
                ("Minimize", "performMiniaturize:", "m"),
                ("Zoom", "performZoom:", ""),
                ("Close", "performClose:", "w"),
                ("", "", ""),
                ("Bring All to Front", "arrangeInFront:", ""),
            ],
        )
    )
    return menus


def install_main_menu(app_name: str, *, view_menu: bool = False) -> bool:
    """Pudge's menu bar for this process (Low RAM mode: the main process and the window process
    each show it while they are the app in front)."""
    try:
        from AppKit import (
            NSAlternateKeyMask,
            NSApplication,
            NSCommandKeyMask,
            NSControlKeyMask,
            NSMenu,
            NSMenuItem,
        )

        application = NSApplication.sharedApplication()
        bar = NSMenu.alloc().init()
        windows = None
        for title, items in main_menu_items(app_name, view_menu=view_menu):
            holder = NSMenuItem.alloc().init()
            menu = NSMenu.alloc().initWithTitle_(title)
            for label, action, key in items:
                if not label:
                    menu.addItem_(NSMenuItem.separatorItem())
                    continue
                mask = None
                if key.startswith("~"):
                    key, mask = key[1:], NSAlternateKeyMask | NSCommandKeyMask
                elif key.startswith("^"):
                    key, mask = key[1:], NSControlKeyMask | NSCommandKeyMask
                item = menu.addItemWithTitle_action_keyEquivalent_(label, action, key)
                if mask is not None and item is not None:
                    item.setKeyEquivalentModifierMask_(mask)
            holder.setSubmenu_(menu)
            bar.addItem_(holder)
            if title == "Window":
                windows = menu
        application.setMainMenu_(bar)
        if windows is not None:
            application.setWindowsMenu_(windows)
        return True
    except Exception:  # noqa: BLE001 - the app works without a menu bar
        return False


def main() -> int:
    try:
        fd = int(os.environ.pop(FD_ENV, ""))
    except ValueError:
        print("pudge.window_process: started without its channel", file=sys.stderr)
        return 2
    token = sys.stdin.readline().strip()
    try:
        sys.stdin.close()
    except OSError:
        pass
    _become_accessory()
    # Inherited through pass_fds (inheritable): nothing this process starts may get the channel.
    try:
        os.set_inheritable(fd, False)
    except OSError:
        pass
    channel_socket = socket.socket(fileno=fd)
    return _ChildWindow(channel_socket, token).run()


if __name__ == "__main__":
    raise SystemExit(main())
