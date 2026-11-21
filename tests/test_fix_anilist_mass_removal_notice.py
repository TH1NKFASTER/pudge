"""More than five titles leave the AniList list in one sync: Pudge still keeps them (a glitch is
likelier than the user), but now says so in the log and in a notice with two answers: Remove from
Pudge (as a removal on AniList) or Keep (never asked again for those titles). Behaviour; invented
titles; the window runs in node (tests/page_harness.py)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime
from test_web_app import make_api
from tests.i18n_support import en, ru

IDS = list(range(101, 109))  # 8 titles on the list


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.access_token = "invented-token"
    manager = AnimeManager(cfg)
    for media_id in IDS:
        manager.db.upsert_anime(LibraryAnime(media_id=media_id, title=f"Show {media_id}", status="CURRENT"))
    return manager


def _status(manager: AnimeManager, media_id: int) -> str:
    return manager.db.get_anime(media_id).status


def _mass_removal(manager: AnimeManager) -> list[str]:
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    manager.logger.addHandler(handler)
    try:
        assert manager._mark_removed_on_anilist(set(IDS), {}) == 0  # the first full list
        assert manager._mark_removed_on_anilist({101, 102}, {}) == 0  # 6 gone at once
    finally:
        manager.logger.removeHandler(handler)
    return lines


def test_a_mass_removal_is_kept_logged_and_asked_about(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    lines = _mass_removal(manager)
    assert all(_status(manager, media_id) == "CURRENT" for media_id in IDS)
    held = [line for line in lines if "EVENT anilist.list_removal_held count=6" in line]
    assert held and "'Show 103'" in held[0] and "'Show 108'" in held[0]
    notice = manager.anilist_removal_notice()
    assert [item["media_id"] for item in notice["titles"]] == IDS[2:]
    assert notice["titles"][0]["title"] == "Show 103"
    # The next sync of the same list does not forget the question.
    assert manager._mark_removed_on_anilist({101, 102}, {}) == 0
    assert manager.anilist_removal_notice() == notice


def test_remove_from_pudge_treats_them_as_removed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _mass_removal(manager)
    notice = manager.anilist_removal_notice()
    result = manager.resolve_anilist_removal_notice("remove", notice["id"])
    assert result == {"ok": True, "changed": 6, "action": "remove"}
    assert [_status(manager, media_id) for media_id in IDS] == ["CURRENT"] * 2 + ["REMOVED"] * 6
    assert manager.anilist_removal_notice() == {}


def test_keep_is_remembered_and_not_asked_again(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _mass_removal(manager)
    notice = manager.anilist_removal_notice()
    assert manager.resolve_anilist_removal_notice("keep", notice["id"])["changed"] == 6
    assert manager.anilist_removal_notice() == {}
    assert all(_status(manager, media_id) == "CURRENT" for media_id in IDS)
    # The same titles vanish at once again (they came back, then went again): no new question.
    assert manager._mark_removed_on_anilist(set(IDS), {}) == 0
    assert manager._mark_removed_on_anilist({101, 102}, {}) == 0
    assert manager.anilist_removal_notice() == {}


def test_titles_back_on_the_list_are_not_asked_about(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _mass_removal(manager)
    manager._mark_removed_on_anilist({101, 102, 103, 104, 105, 106}, {})  # 4 came back
    assert [item["media_id"] for item in manager.anilist_removal_notice()["titles"]] == [107, 108]
    # A stale answer (the window still showed six) changes nothing.
    assert manager.resolve_anilist_removal_notice("remove", "stale-id") == {
        "ok": False,
        "changed": 0,
        "stale": True,
    }
    assert _status(manager, 107) == "CURRENT"


def test_the_bridge_answers_with_fresh_state_and_the_settings_carry_the_notice(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    try:
        manager = api.manager
        manager.config.anilist.access_token = "invented-token"
        for media_id in IDS:
            manager.db.upsert_anime(
                LibraryAnime(media_id=media_id, title=f"Show {media_id}", status="CURRENT")
            )
        _mass_removal(manager)
        notice = api._settings_payload()["anilist_removal_notice"]
        assert len(notice["titles"]) == 6
        result = api.resolve_anilist_removal_notice("remove", notice["id"])
        assert result["ok"] is True and result["changed"] == 6 and isinstance(result["state"], dict)
        assert api._settings_payload()["anilist_removal_notice"] == {}
    finally:
        api.close()


# ---------------------------------------------------------------------------------------
# The window.
NOTICE = {
    "id": "held-1",
    "titles": [{"media_id": media_id, "title": f"Show <{media_id}>"} for media_id in IDS[2:]],
}
READ = r"""
const notice = () => {
  const body = document.getElementById('modalBody');
  const open = document.getElementById('modalBackdrop').classList.contains('open') && !!body.querySelector('[data-anilist-removal-notice]');
  return open ? {title: __text('#modalTitle'), text: __text('#modalBody'), items: [...body.querySelectorAll('li')].map(li => li.textContent),
                 buttons: [...body.querySelectorAll('[data-anilist-removal]')].map(b => b.textContent)} : null;
};
"""


@pytest.mark.parametrize("lang,tr", [("en", en), ("ru", ru)])
def test_the_notice_lists_the_titles_and_offers_remove_and_keep(page, lang, tr):
    result = page.run(
        READ
        + r"""
        await __tick(50);
        const first = notice();
        document.getElementById('modalClose').click();
        await __tick(10);
        renderDataPages(true); await __tick(10);
        return {first, again: notice()};
        """,
        lang=lang,
        state={"settings": {"anilist_removal_notice": NOTICE}},
    )
    first = result.value["first"]
    assert first is not None, result.console
    assert first["title"] == tr("anilistRemoval.title")
    assert tr("anilistRemoval.why", count=6) in first["text"]
    assert first["items"] == [f"Show <{media_id}>" for media_id in IDS[2:]]
    assert first["buttons"] == [tr("anilistRemoval.remove"), tr("anilistRemoval.keep")]
    assert result.value["again"] is None  # once per held set in a session


@pytest.mark.parametrize(
    "action,toast", [("remove", "anilistRemoval.removed"), ("keep", "anilistRemoval.kept")]
)
def test_an_answer_goes_to_pudge_and_closes_the_notice(page, action, toast):
    result = page.run(
        READ
        + f"""
        await __tick(50);
        await __click('[data-anilist-removal="{action}"]'); await __tick(20);
        return {{open: notice(), toasts: __toasts(), expected: PudgeI18n.t('{toast}', {{count: 6}})}};
        """,
        state={"settings": {"anilist_removal_notice": NOTICE}},
        api={"resolve_anilist_removal_notice": {"ok": True, "changed": 6, "action": action}},
    )
    assert result.calls_of("resolve_anilist_removal_notice") == [[action, "held-1"]]
    assert result.value["open"] is None
    assert result.value["toasts"] == [result.value["expected"]]


def test_the_notice_waits_while_another_dialog_is_open(page):
    result = page.run(
        READ
        + r"""
        await __tick(50);
        const empty = notice();
        void pudgeConfirm('Something else');
        ui.state.settings.anilist_removal_notice = {id: 'held-2', titles: [{media_id: 1, title: 'Show 1'}]};
        renderDataPages(true); await __tick(10);
        const waiting = notice();
        PudgeConfirm.closeIfOpen(); renderDataPages(true); await __tick(10);
        return {empty, waiting, after: notice()?.items};
        """,
        state={"settings": {"anilist_removal_notice": {"id": "x", "titles": []}}},
    )
    assert result.value["empty"] is None and result.value["waiting"] is None
    assert result.value["after"] == ["Show 1"]
