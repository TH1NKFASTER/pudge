"""What a window's page may call in Python: the API's public methods, nothing else.

pywebview resolves a call name from the page with ``getattr`` on the ``js_api``
object, dots and underscores included (``config.qbittorrent.__setattr__``,
``get_state.__func__.__globals__.clear`` ...), so handing it the API object let
any script in the page reach the whole object graph. Windows are therefore
created without ``js_api``: each public method is exposed by name, and pywebview
looks those names up exactly.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable


def bridge_functions(api: Any) -> list[Callable[..., Any]]:
    """One plain function per public method of ``type(api)``; no attributes, no objects."""
    functions: list[Callable[..., Any]] = []
    owner = type(api)
    for name in dir(owner):
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


def _bridge_call(api: Any, name: str) -> Callable[..., Any]:
    def call(*args: Any) -> Any:
        return getattr(api, name)(*args)

    call.__name__ = call.__qualname__ = name
    return call


def create_window(webview: Any, title: str, api: Any, **options: Any) -> Any:
    """``webview.create_window`` whose page sees only ``bridge_functions(api)``."""
    window = webview.create_window(title, **options)
    expose = getattr(window, "expose", None)
    if callable(expose):
        expose(*bridge_functions(api))
    return window
