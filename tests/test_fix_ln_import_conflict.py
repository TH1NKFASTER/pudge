"""Importing a light novel whose title (and volume) is already in the library with other
contents asks the user: Keep both (a separate book, its title numbered), Replace (as before:
the library's copy and reading place carry over) or Cancel. Before, the second EPUB silently
overwrote the first (review r2-lightnovels-1 #1). Behaviour; invented books; the window runs in
node (tests/page_harness.py)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.light_novels import LightNovelImportConflict
from test_fix_light_novel_import_safety import _epub, _service
from test_web_app import make_api
from tests.i18n_support import en, ru

TITLE = "架空の外伝集"
FIRST = "一冊目の本文です。少年は駅の前で待っていた。" * 10
SECOND = "二冊目の本文です。少女は川の向こうを眺めていた。" * 10


def _books(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _epub(tmp_path / "Downloads" / "a" / "book.epub", TITLE, [FIRST]),
        _epub(tmp_path / "Downloads" / "b" / "book.epub", TITLE, [SECOND]),
    )


def _text(service, book_id: int) -> str:
    with service._connection() as conn:
        rows = conn.execute("SELECT text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (book_id,))
        return "".join(str(row["text"]) for row in rows)


def test_ask_imports_nothing_and_says_which_book_is_there(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first, second = _books(tmp_path)
    book = service.import_file(first)
    with pytest.raises(LightNovelImportConflict) as conflict:
        service.import_file(second, on_conflict="ask")
    assert conflict.value.existing["id"] == book["id"] and conflict.value.existing["title"] == TITLE
    assert [row["id"] for row in service.state()["books"]] == [book["id"]]
    assert FIRST[:20] in _text(service, book["id"])


def test_keep_both_imports_a_separate_book_with_a_numbered_title(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first, second = _books(tmp_path)
    book = service.import_file(first)
    kept = service.import_file(second, on_conflict="keep_both")
    assert kept["id"] != book["id"]
    assert kept["title"] == f"{TITLE} (2)"
    assert Path(kept["file_path"]).name == "book (2).epub"
    assert FIRST[:20] in _text(service, book["id"]) and SECOND[:20] in _text(service, kept["id"])
    # Reading the managed copy again (a rescan, a re-index) keeps the number.
    again = service.import_file(Path(kept["file_path"]), explicit=False)
    assert again["id"] == kept["id"] and again["title"] == f"{TITLE} (2)"


def test_replace_keeps_todays_behaviour(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first, second = _books(tmp_path)
    book = service.import_file(first)
    replaced = service.import_file(second, on_conflict="replace")
    assert replaced["id"] == book["id"] and replaced["title"] == TITLE
    assert SECOND[:20] in _text(service, book["id"])
    assert len(service.state()["books"]) == 1


def test_the_window_import_asks_and_answers_with_a_token(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    try:
        api.light_novels.root = tmp_path / "Light Novels"
        api.light_novels.root.mkdir()
        first, second = _books(tmp_path)
        chosen = [first]

        class Window:
            def create_file_dialog(self, kind, **options):
                return tuple(str(path) for path in chosen)

        monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10))
        api.window = Window()
        assert len(api.choose_light_novel_file()["books"]) == 1
        chosen[:] = [second]
        result = api.choose_light_novel_file()
        assert result["books"] == [] and result["errors"] == []
        (conflict,) = result["conflicts"]
        assert conflict["name"] == "book.epub" and conflict["title"] == TITLE
        assert str(second) not in str(conflict)  # the page never sends a path back

        answer = api.light_novel_resolve_import_conflict(conflict["token"], "keep_both")
        assert answer["ok"] is True and answer["book"]["title"] == f"{TITLE} (2)"
        assert len(answer["state"]["books"]) == 2
        # A token answers once.
        assert api.light_novel_resolve_import_conflict(conflict["token"], "replace")["ok"] is False

        # A drop asks the same; Cancel imports nothing.
        third = _epub(tmp_path / "Downloads" / "c" / "book.epub", TITLE, ["三冊目の本文です。" * 20])
        drop = api.import_dropped_paths([str(third)])
        assert drop["imports"] == [] and len(drop["ln_conflicts"]) == 1
        cancel = api.light_novel_resolve_import_conflict(drop["ln_conflicts"][0]["token"], "cancel")
        assert cancel == {"ok": True, "cancelled": True}
        assert len(api.light_novels.state()["books"]) == 2
    finally:
        api.close()


# ---------------------------------------------------------------------------------------
# The window.
CONFLICT = {"token": "t-1", "name": "book.epub", "title": "Invented <Book>", "existing_id": 3}
DIALOG = r"""
const dialog = () => {
  const backdrop = document.getElementById('pudgeConfirmBackdrop');
  if (!backdrop?.classList.contains('open')) return null;
  return {title: __text('#pudgeConfirmTitle'), message: document.getElementById('pudgeConfirmMessage').textContent,
          buttons: [...backdrop.querySelectorAll('.pudge-confirm-actions button')].filter(b => !b.hidden).map(b => b.textContent)};
};
"""


@pytest.mark.parametrize("lang,tr", [("en", en), ("ru", ru)])
@pytest.mark.parametrize(
    "button,choice", [("keep_both", "keep_both"), ("replace", "replace"), (None, "cancel")]
)
def test_the_import_dialog_offers_keep_both_replace_and_cancel(page, lang, tr, button, choice):
    click = (
        f"await __click('[data-pudge-choose=\"{button}\"]');"
        if button
        else "await __click('[data-pudge-confirm=\"cancel\"]');"
    )
    result = page.run(
        DIALOG
        + r"""
        setPage('lightnovels'); await __tick(20);
        const clicked = __click('#lnImport');
        await __tick(20);
        const shown = dialog();
        """
        + click
        + r"""
        await __tick(20); await clicked;
        return {shown, after: dialog()};
        """,
        lang=lang,
        api={
            "choose_light_novel_file": {
                "cancelled": False,
                "books": [],
                "errors": [],
                "conflicts": [CONFLICT],
                "state": {"books": [], "settings": {}},
            },
            "light_novel_resolve_import_conflict": {"ok": True, "cancelled": True},
        },
    )
    shown = result.value["shown"]
    assert shown is not None, result.console
    assert shown["title"] == tr("lnImportConflict.title")
    assert shown["message"] == tr("lnImportConflict.message", name="book.epub", title="Invented <Book>")
    assert shown["buttons"] == [
        tr("action.cancel"),
        tr("lnImportConflict.keepBoth"),
        tr("lnImportConflict.replace"),
    ]
    assert result.calls_of("light_novel_resolve_import_conflict") == [["t-1", choice]]
    assert result.value["after"] is None


def test_a_drop_with_a_known_title_asks_instead_of_saying_it_failed(page):
    result = page.run(
        DIALOG
        + r"""
        window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: {ok: false, imports: [], errors: [], failed: [], ln_conflicts: ["""
        + '{"token": "t-2", "name": "x.epub", "title": "Invented"}'
        + r"""]}}));
        await __tick(20);
        const shown = dialog();
        await __click('[data-pudge-choose="keep_both"]'); await __tick(20);
        return {shown: Boolean(shown), toasts: __toasts(), failed: PudgeI18n.t('ui.couldNotImportFile')};
        """,
        api={
            "light_novel_resolve_import_conflict": {
                "ok": True,
                "book": {"id": 9},
                "state": {"books": [], "settings": {}},
            }
        },
    )
    assert result.value["shown"] is True
    assert result.value["failed"] not in result.value["toasts"]
    assert result.calls_of("light_novel_resolve_import_conflict") == [["t-2", "keep_both"]]
