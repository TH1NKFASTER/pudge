"""What a window's page may call in Python: the API methods the pages use, nothing else.

pywebview resolves a call name from the page with ``getattr`` on the ``js_api``
object, dots and underscores included (``config.qbittorrent.__setattr__``,
``get_state.__func__.__globals__.clear`` ...), so handing it the API object let
any script in the page reach the whole object graph. Windows are therefore
created without ``js_api``: each method the pages call (``bridge_methods``) is
exposed by name, and pywebview looks those names up exactly. Public methods
that only Python calls (HTTP handlers, shortcuts, drag and drop) are not exposed.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections import Counter
from typing import Any, Callable, Iterable

from .bridge_methods import PAGE_METHODS


def bridge_functions(api: Any, names: Iterable[str] = PAGE_METHODS) -> list[Callable[..., Any]]:
    """One plain function per listed public method of ``type(api)``; no attributes, no objects."""
    functions: list[Callable[..., Any]] = []
    owner = type(api)
    allowed = frozenset(names)
    for name in sorted(allowed):
        if name.startswith("_"):
            continue
        try:
            member = inspect.getattr_static(owner, name)
        except AttributeError:
            continue
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if inspect.isfunction(member):
            functions.append(_bridge_call(api, name))
    return functions


class BridgeStats:
    """How often the pages call into Python (and, through the window process, how many bytes
    come back): ``bridge=calls:…,per_min:…,kb:…,top:name:calls/kb,…`` in the memory line."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: Counter[str] = Counter()
        self._bytes: Counter[str] = Counter()
        self._since = clock()

    def record(self, name: str, size: int = 0) -> None:
        with self._lock:
            self._calls[name] += 1
            if size:
                self._bytes[name] += int(size)

    def line(self, *, top: int = 6, reset: bool = True) -> str:
        with self._lock:
            calls, sizes, since = self._calls, self._bytes, self._since
            if reset:
                self._calls, self._bytes, self._since = Counter(), Counter(), self._clock()
        minutes = max(1.0 / 60.0, (self._clock() - since) / 60.0)
        total = sum(calls.values())
        text = f"bridge=calls:{total},per_min:{total / minutes:.1f}"
        if sizes:
            text += f",kb:{sum(sizes.values()) / 1024:.0f}"
        busiest = sorted(calls, key=lambda key: (-calls[key], key))[: max(0, int(top))]
        if busiest:
            text += ",top:" + "|".join(
                f"{key}:{calls[key]}" + (f"/{sizes[key] / 1024:.0f}kb" if sizes.get(key) else "")
                for key in busiest
            )
        return text


BRIDGE_STATS = BridgeStats()


def _bridge_call(api: Any, name: str) -> Callable[..., Any]:
    def call(*args: Any) -> Any:
        BRIDGE_STATS.record(name)
        return getattr(api, name)(*args)

    call.__name__ = call.__qualname__ = name
    return call


# Low RAM mode's main process has no pywebview loop until a companion window needs one:
# set to a callable that starts it (web_app._LazyPywebview.ensure); None otherwise.
BEFORE_CREATE: Callable[[], None] | None = None


def create_window(webview: Any, title: str, api: Any, **options: Any) -> Any:
    """``webview.create_window`` whose page sees only ``bridge_functions(api)``."""
    before = BEFORE_CREATE
    if before is not None:
        before()
    window = webview.create_window(title, **options)
    expose = getattr(window, "expose", None)
    if callable(expose):
        expose(*bridge_functions(api))
    return window
