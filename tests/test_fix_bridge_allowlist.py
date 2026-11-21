"""A window's page reaches exactly the WebAppApi methods the pages call (review ARCH-9).

Before, every public method was exposed: Python-only entry points such as
``set_window``, ``import_dropped_paths`` (any paths) or the asset server's
``set_torrents_enabled`` could be called by any script in the page.
"""

from __future__ import annotations

import inspect
import re

from source_policy import sources

from pudge import js_bridge
from pudge.bridge_methods import PAGE_METHODS
from pudge.web_app import WebAppApi

# The API's own shutdown: pages name ``.close`` only for their dialogs and panels.
NOT_PAGE_CALLS = {"close"}


def _public_methods() -> set[str]:
    names = set()
    for name in dir(WebAppApi):
        if name.startswith("_"):
            continue
        member = inspect.getattr_static(WebAppApi, name)
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if inspect.isfunction(member):
            names.add(name)
    return names


def _pages() -> dict[str, str]:
    pages = {**sources("pudge/web/*.js"), **sources("pudge/web/*.html")}
    return {path: body for path, body in pages.items() if not path.rsplit("/", 1)[-1].startswith("locales.")}


def _names_in_pages() -> set[str]:
    return set(re.findall(r"[A-Za-z_$][\w$]*", "\n".join(_pages().values())))


# The bridge object as the pages reach it: pywebview.api, window.pywebview?.api,
# g.pywebview.api, g.window?.pywebview?.api, and the helpers api() / API() returning it.
_BRIDGE = (
    r"(?:(?<![\w$.])(?:(?:window|globalThis|g|g\.window)\??\.)?pywebview\??\.api"
    r"|(?<![\w$.])(?:api|API)\(\))"
)
_MEMBER = r"\s*\??\.\s*([A-Za-z_$][\w$]*)"
_DECLARATION = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]*)")
_QUOTED = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"")


def bridge_calls(text: str) -> set[str]:
    """Method names a page script reaches on the bridge object, directly or through a local
    alias (``const api = window.pywebview?.api``) that names nothing else in the file."""
    code = _QUOTED.sub("''", text)
    calls = set(re.findall(_BRIDGE + _MEMBER, code))
    values: dict[str, list[bool]] = {}
    for declaration in _DECLARATION.finditer(code):
        value = declaration.group(2)
        bridge = re.match(
            r"\s*(?:typeof\s+pywebview\s*!==?\s*''\s*\?\s*)?" + _BRIDGE + r"(?!" + _MEMBER + ")", value
        )
        values.setdefault(declaration.group(1), []).append(bool(bridge))
    for alias, kinds in values.items():
        if all(kinds):
            calls |= set(re.findall(r"(?<![\w$.])" + re.escape(alias) + _MEMBER, code))
    return calls


def test_every_listed_method_exists_and_a_page_names_it() -> None:
    assert sorted(PAGE_METHODS - _public_methods()) == []
    assert sorted(PAGE_METHODS - _names_in_pages()) == []


def test_every_method_a_page_names_is_listed() -> None:
    missing = (_public_methods() & _names_in_pages()) - PAGE_METHODS - NOT_PAGE_CALLS
    assert sorted(missing) == [], "add the page's new calls to pudge/bridge_methods.py"


def test_every_call_a_page_makes_reaches_a_listed_method_that_exists() -> None:
    public = _public_methods()
    calls = {path: bridge_calls(body) for path, body in _pages().items()}
    unknown = sorted(
        f"{path}: {name}"
        for path, names in calls.items()
        for name in names
        if name not in PAGE_METHODS or name not in public
    )
    assert unknown == [], "a page calls a method the bridge does not expose (deleted or renamed?)"
    # The scan sees the pages' calls: nearly every listed method is called through them.
    assert len(set().union(*calls.values()) & PAGE_METHODS) > 280


def test_the_call_scan_reports_a_deleted_or_renamed_method() -> None:
    page = """
    const api = window.pywebview?.api;
    async function go(){ await pywebview.api.get_state(); await window.pywebview?.api?.removed_method?.(1);
      api().also_gone(); API()?.renamed_call(); g.pywebview.api.play('x'); api?.study_word(1);
      const text = 'https://api.openai.com'; }
    """
    assert bridge_calls(page) == {
        "get_state",
        "removed_method",
        "also_gone",
        "renamed_call",
        "play",
        "study_word",
    }
    assert {"removed_method", "also_gone", "renamed_call"}.isdisjoint(PAGE_METHODS)


def test_python_only_entry_points_are_not_exposed_to_pages() -> None:
    api = WebAppApi.__new__(WebAppApi)
    exposed = {function.__name__ for function in js_bridge.bridge_functions(api)}
    assert exposed == PAGE_METHODS
    for internal in (
        "set_window",
        "start_global_shortcuts",
        "import_dropped_paths",
        "set_torrents_enabled",
        "torrent_traffic_status",
        "mpv_playback_save",
        "mpv_explain_subtitle",
        "diagnostics_text",
        "close",
    ):
        assert internal in _public_methods(), internal
        assert internal not in exposed, internal
