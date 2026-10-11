"""Anime menu: Delete (downloaded episodes, with a status choice) or a red Drop (nothing downloaded)."""

from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.web_app import WebAppApi

OPEN_MENU = r"""
setPage('current', true); renderCurrent(); await __tick(20);
await __fire('#currentContent .waiting-card strong', 'contextmenu', {button: 2, clientX: 40, clientY: 40}, 'MouseEvent');
await __tick(10);
const out = {actions: [...document.querySelectorAll('#contextMenu.open [data-context-action]')].map(node => node.dataset.contextAction)};
const danger = document.querySelector('#contextMenu .danger-action');
out.dangerAction = danger?.dataset.contextAction; out.dangerText = danger?.textContent;
"""

DELETE_FLOW = (
    OPEN_MENU
    + r"""
await __click('#contextMenu [data-context-action="delete-files"]'); await __tick(20);
out.confirm = __text('#pudgeConfirmMessage');
out.choiceVisible = __visible('#pudgeConfirmChoice');
out.choiceLabel = __text('#pudgeConfirmChoiceLabel');
out.options = [...document.querySelectorAll('#pudgeConfirmChoice option')].map(node => [node.value, node.textContent]);
out.selected = document.getElementById('pudgeConfirmChoice').value;
out.okText = __text('[data-pudge-confirm="ok"]');
out.okDanger = document.querySelector('[data-pudge-confirm="ok"]').classList.contains('danger');
if (window.__choose) { await __input('#pudgeConfirmChoice', window.__choose); }
await __click(window.__cancel ? '[data-pudge-confirm="cancel"]' : '[data-pudge-confirm="ok"]'); await __tick(50);
out.toasts = __toasts();
return out;
"""
)

DROP_FLOW = (
    OPEN_MENU
    + r"""
await __click('#contextMenu [data-context-action="drop"]'); await __tick(20);
out.confirm = __text('#pudgeConfirmMessage');
out.choiceVisible = __visible('#pudgeConfirmChoice');
out.okDanger = document.querySelector('[data-pudge-confirm="ok"]').classList.contains('danger');
await __click('[data-pudge-confirm="ok"]'); await __tick(50);
out.toasts = __toasts();
return out;
"""
)


def _show(**extra):
    return {
        "media_id": 4,
        "title": "Airing Waiting",
        "site_url": "https://anilist.co/anime/4",
        "next_episode": 3,
        "list_status": "PAUSED",
        **extra,
    }


def _run(page, code, show, lang="en", before=""):
    return page.run(
        code,
        lang=lang,
        before=before,
        state={"current": [show], "home": {"waiting": [show]}},
        api={
            "drop_anime": {"ok": True, "state": {"home": {}}},
            "final_sequel_option": {"ok": True, "sequel": None},
        },
    )


def test_downloaded_anime_offers_delete_with_its_current_status_preselected(page) -> None:
    show = _show(local_count=2, local={"video_path": "/v/A - 03.mkv", "episode": 3, "state": "ready"})
    for lang in ("en", "ru"):
        result = _run(page, DELETE_FLOW, show, lang)
        out = result.value
        texts = page.run(
            "return {del: t('action.deleteAnimeFiles'), drop: t('action.drop'), confirm: t('confirm.deleteAnimeFiles') + '\\n' + t('confirm.deleteAnimeFilesNoRedownload'),"
            " label: t('label.statusAfterDelete'), paused: t('anilistStatus.paused'),"
            " toast: t('toast.animeFilesDeleted', {status: t('anilistStatus.paused')})}",
            lang=lang,
        ).value
        assert "drop" not in out["actions"] and out["dangerAction"] == "delete-files"
        assert out["dangerText"] == texts["del"]
        assert out["confirm"] == texts["confirm"] and out["choiceVisible"]
        assert out["choiceLabel"] == texts["label"]
        assert [value for value, _ in out["options"]] == [
            "CURRENT",
            "PLANNING",
            "COMPLETED",
            "PAUSED",
            "DROPPED",
            "REPEATING",
        ]
        assert all(label for _, label in out["options"])
        assert out["selected"] == "PAUSED", "defaults to the status the anime had"
        assert out["okText"] == texts["del"] and out["okDanger"]
        assert result.calls_of("drop_anime") == [[4, "PAUSED"]]
        assert texts["toast"] in out["toasts"]


def test_delete_dialog_applies_a_changed_status_and_cancel_does_nothing(page) -> None:
    show = _show(local_count=1)
    changed = _run(page, DELETE_FLOW, show, before="window.__choose = 'COMPLETED';")
    assert changed.calls_of("drop_anime") == [[4, "COMPLETED"]]
    cancelled = _run(page, DELETE_FLOW, show, before="window.__cancel = true;")
    assert cancelled.calls_of("drop_anime") == []


def test_anime_without_downloads_gets_a_red_drop(page) -> None:
    show = _show(local_count=0, local=None, list_status="CURRENT")
    for lang in ("en", "ru"):
        result = _run(page, DROP_FLOW, show, lang)
        out = result.value
        texts = page.run(
            "return {drop: t('action.drop'), confirm: t('confirm.drop'), toast: t('toast.dropped')}",
            lang=lang,
        ).value
        assert "delete-files" not in out["actions"] and out["dangerAction"] == "drop"
        assert out["dangerText"] == texts["drop"]
        assert out["confirm"] == texts["confirm"] and not out["choiceVisible"] and out["okDanger"]
        assert result.calls_of("drop_anime") == [[4]]
        assert texts["toast"] in out["toasts"]


# --- backend ---------------------------------------------------------------


class FakeAniList:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def set_list_status(self, media_id: int, status: str) -> dict:
        self.calls.append((media_id, status))
        return {"status": status}

    def close(self) -> None:
        pass


def _api(tmp_path: Path, status: str) -> tuple[WebAppApi, list, Path]:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    write_config(config, config.config_path)
    api = WebAppApi(config.config_path)
    calls: list = []
    api._anilist_client = lambda: FakeAniList(calls)  # type: ignore[method-assign]
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=7, title="Show", status=status, progress=2, episodes=12)
    )
    video = tmp_path / "Show - 03.mkv"
    video.write_bytes(b"v")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=7, title="Show", episode=3, video_path=video, state="ready")
    )
    return api, calls, video


def test_delete_keeps_an_unchanged_status_and_schedules_the_files(tmp_path: Path) -> None:
    api, calls, _ = _api(tmp_path, "PAUSED")
    result = api.drop_anime(7, "PAUSED")
    assert result["ok"] and result["queued"] == 1
    assert calls == [], "AniList is not touched when the status did not change"
    assert api.manager.db.get_anime(7).status == "PAUSED"
    episode = api.manager.db.episodes(7)[0]
    assert episode.state == "dropped" and episode.delete_after is not None


def test_delete_with_a_changed_status_updates_anilist(tmp_path: Path) -> None:
    api, calls, _ = _api(tmp_path, "CURRENT")
    api.drop_anime(7, "completed")
    assert calls == [(7, "COMPLETED")]
    assert api.manager.db.get_anime(7).status == "COMPLETED"
    assert api.manager.db.episodes(7)[0].delete_after is not None


def test_plain_drop_still_drops(tmp_path: Path) -> None:
    api, calls, _ = _api(tmp_path, "CURRENT")
    api.drop_anime(7)
    assert calls == [(7, "DROPPED")]
    assert api.manager.db.get_anime(7).status == "DROPPED"


def test_anime_payload_counts_downloaded_files(tmp_path: Path) -> None:
    api, _, video = _api(tmp_path, "CURRENT")
    anime = api.manager.db.get_anime(7)
    assert api._anime_payload(anime, relations=False)["local_count"] == 1
    video.unlink()
    assert api._anime_payload(anime, relations=False)["local_count"] == 0


def _searched_after_status(tmp_path: Path, monkeypatch, status: str) -> list[int]:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.nyaa.enabled = True
    config.nyaa.auto_download_current = True
    config.qbittorrent.enabled = True
    manager = AnimeManager(config)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=9, title="Airing", status=status, media_status="RELEASING", progress=2, episodes=12
        )
    )
    release = NyaaRelease(
        title="[Group] Airing - 03 [1080p]",
        link="",
        torrent_url="https://example.invalid/3.torrent",
        info_hash="c" * 40,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=10,
        leechers=0,
        downloads=1,
        trusted=True,
        remake=False,
        score=100.0,
        group="Group",
    )
    searched: list[int] = []
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda _m, *, episode, batch, automatic=False: searched.append(episode) or [release],
    )
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)
    monkeypatch.setattr(manager, "add_release", lambda *a, **k: release)
    manager.auto_search_current()
    return searched


def test_dropped_anime_is_no_longer_downloaded_automatically(tmp_path: Path, monkeypatch) -> None:
    assert _searched_after_status(tmp_path / "current", monkeypatch, "CURRENT"), (
        "control: Watching is followed"
    )
    assert _searched_after_status(tmp_path / "dropped", monkeypatch, "DROPPED") == []
