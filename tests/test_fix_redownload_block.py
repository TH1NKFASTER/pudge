"""Delete that keeps the title in the list blocks automatic re-downloading of the deleted episodes."""

from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.web_app import WebAppApi


class FakeAniList:
    def set_list_status(self, media_id: int, status: str) -> dict:
        return {"status": status}

    def close(self) -> None:
        pass


def _release(episode: int) -> NyaaRelease:
    return NyaaRelease(
        title=f"[Group] Airing - {episode:02d} [1080p]",
        link="",
        torrent_url=f"https://example.invalid/{episode}.torrent",
        info_hash=f"{episode:040d}",
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


def _api(tmp_path: Path, monkeypatch, *, status: str = "CURRENT", local=(3, 4)) -> tuple[WebAppApi, list]:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.nyaa.enabled = True
    config.nyaa.auto_download_current = True
    config.nyaa.max_auto_download_per_anime = 5
    config.qbittorrent.enabled = True
    write_config(config, config.config_path)
    api = WebAppApi(config.config_path)
    api._anilist_client = lambda: FakeAniList()  # type: ignore[method-assign]
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=9,
            title="Airing",
            status=status,
            media_status="RELEASING",
            progress=2,
            episodes=12,
            next_airing_episode=6,
        )
    )
    for episode in local:
        video = tmp_path / f"Airing - {episode:02d}.mkv"
        video.write_bytes(b"v")
        api.manager.db.upsert_episode(
            LibraryEpisode(media_id=9, title="Airing", episode=episode, video_path=video, state="ready")
        )
    searched: list[int] = []
    manager = api.manager

    def search(media_id, *, episode=None, batch=False, automatic=False, **kwargs):
        searched.append(episode)
        return [_release(int(episode or 0))]

    monkeypatch.setattr(manager, "search_releases", search)
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)
    monkeypatch.setattr(manager, "downloads_configured", lambda: True)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(manager, "torrent_paused_on_add", lambda: True)
    return api, searched


def _forget_files(api: WebAppApi, tmp_path: Path) -> None:
    """What cleanup does once the scheduled deletion is due."""
    for item in api.manager.db.episodes(9):
        item.video_path.unlink(missing_ok=True)
        api.manager.db.delete_episode_record(item.video_path)


def test_delete_keeping_watching_blocks_only_the_deleted_episodes(tmp_path: Path, monkeypatch) -> None:
    api, searched = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api.manager, "add_release", lambda *a, **k: True)
    api.drop_anime(9, "CURRENT")
    _forget_files(api, tmp_path)
    api.manager.auto_search_current()
    assert searched == [5], "3 and 4 were deleted by the user; 5 is new and still downloads"


def test_menu_action_lifts_the_block(tmp_path: Path, monkeypatch) -> None:
    api, searched = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api.manager, "add_release", lambda *a, **k: True)
    api.drop_anime(9, "CURRENT")
    anime = api.manager.db.get_anime(9)
    assert api._anime_payload(anime, relations=False)["redownload_blocked"] == 2
    result = api.allow_redownload(9)
    assert result["lifted"] == 2
    assert api._anime_payload(anime, relations=False)["redownload_blocked"] == 0
    _forget_files(api, tmp_path)
    api.manager.auto_search_current()
    assert searched == [3, 4, 5]


def test_the_block_survives_a_restart(tmp_path: Path, monkeypatch) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    api.drop_anime(9, "PAUSED")
    again = WebAppApi(api.config.config_path)
    assert again.manager.db.redownload_blocked(9) == {3, 4}


def test_manual_download_of_a_deleted_episode_works_and_lifts_its_block(tmp_path: Path, monkeypatch) -> None:
    api, searched = _api(tmp_path, monkeypatch)
    added: list[str] = []

    class FakeClient:
        def add_release(self, release, **kwargs):
            added.append(release.title)

        def close(self):
            pass

    monkeypatch.setattr(api.manager, "qbt_client", lambda: FakeClient())
    monkeypatch.setattr(api.manager, "_torrent_admission", lambda: None)
    api.drop_anime(9, "CURRENT")
    _forget_files(api, tmp_path)
    api.manager.add_release(9, _release(3), episode=3, batch=False, manual=True)
    assert api.manager.db.redownload_blocked(9) == {4}
    # A user-requested best-release download (not the agent) is allowed too.
    api.manager.search_and_add_best(9, episode=4, batch=False, automatic=False)
    assert 4 in searched and api.manager.db.redownload_blocked(9) == set()
    assert added == [_release(3).title, _release(4).title]


def test_real_search_gate_returns_nothing_for_a_blocked_episode(tmp_path: Path, monkeypatch) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    api.drop_anime(9, "CURRENT")
    monkeypatch.undo()
    # The unpatched search: an automatic search of a blocked episode never reaches a provider.
    assert api.manager.search_releases(9, episode=3, batch=False, automatic=True) == []


def test_drop_does_not_block_but_cancels_running_downloads(tmp_path: Path, monkeypatch) -> None:
    api, _ = _api(tmp_path, monkeypatch, local=())
    running = DownloadItem(
        "a" * 40, "Airing - 05", "downloading", 0.4, "/dl", "/dl/a.mkv", media_id=9, episode=5
    )
    done = DownloadItem("b" * 40, "Airing - 04", "complete", 1.0, "/dl", "/dl/b.mkv", media_id=9, episode=4)
    other = DownloadItem(
        "c" * 40, "Other - 01", "downloading", 0.1, "/dl", "/dl/c.mkv", media_id=10, episode=1
    )
    monkeypatch.setattr(api.manager.db, "downloads", lambda: [running, done, other])
    removed: list[tuple] = []
    monkeypatch.setattr(
        api,
        "_torrent_download_action_impl",
        lambda torrent_hash, action, delete_files=False, backend="": removed.append(
            (torrent_hash, action, delete_files)
        ),
    )
    result = api.drop_anime(9)
    assert removed == [("a" * 40, "remove", True)], "only the dropped title's unfinished torrent"
    assert result["cancelled_downloads"] == 1
    assert api.manager.db.redownload_blocked(9) == set()


def test_delete_keeping_the_title_leaves_running_downloads(tmp_path: Path, monkeypatch) -> None:
    api, _ = _api(tmp_path, monkeypatch)
    running = DownloadItem(
        "a" * 40, "Airing - 05", "downloading", 0.4, "/dl", "/dl/a.mkv", media_id=9, episode=5
    )
    monkeypatch.setattr(api.manager.db, "downloads", lambda: [running])
    removed: list = []
    monkeypatch.setattr(api, "_torrent_download_action_impl", lambda *a, **k: removed.append(a))
    api.drop_anime(9, "CURRENT")
    assert removed == []


# --- window ------------------------------------------------------------------

MENU = r"""
setPage('current', true); renderCurrent(); await __tick(20);
const open = async () => { await __fire('#currentContent .waiting-card strong', 'contextmenu', {button: 2, clientX: 40, clientY: 40}, 'MouseEvent'); await __tick(10); };
await open();
const out = {actions: [...document.querySelectorAll('#contextMenu.open [data-context-action]')].map(node => node.dataset.contextAction)};
out.danger = [...document.querySelectorAll('#contextMenu .danger-action')].map(node => node.dataset.contextAction);
out.allowText = __text('#contextMenu [data-context-action="allow-redownload"]');
out.expectedAllow = t('action.allowRedownload');
if (out.actions.includes('allow-redownload')) {
  await __click('#contextMenu [data-context-action="allow-redownload"]'); await __tick(50);
}
out.toasts = __toasts(); out.expectedToast = t('toast.redownloadAllowed');
if (window.__delete) {
  await open();
  await __click('#contextMenu [data-context-action="delete-files"]'); await __tick(20);
  out.confirm = __text('#pudgeConfirmMessage'); out.warning = t('confirm.deleteAnimeFilesNoRedownload');
}
return out;
"""


def _show(**extra):
    return {
        "media_id": 4,
        "title": "Airing Waiting",
        "site_url": "https://anilist.co/anime/4",
        "next_episode": 3,
        "list_status": "CURRENT",
        **extra,
    }


def _menu(page, show, lang="en", before=""):
    return page.run(
        MENU,
        lang=lang,
        before=before,
        state={"current": [show], "home": {"waiting": [show]}},
        api={
            "allow_redownload": {
                "ok": True,
                "lifted": 2,
                "state": {"current": [show], "home": {"waiting": [show]}},
            },
            "final_sequel_option": {"ok": True, "sequel": None},
        },
    )


def test_menu_offers_allow_redownload_only_while_something_is_blocked(page) -> None:
    for lang in ("en", "ru"):
        result = _menu(
            page, _show(redownload_blocked=2, local_count=1), lang, before="window.__delete = true;"
        )
        out = result.value
        assert "allow-redownload" in out["actions"] and out["allowText"] == out["expectedAllow"]
        assert result.calls_of("allow_redownload") == [[4]]
        assert out["expectedToast"] in out["toasts"]
        assert out["warning"] in out["confirm"], "the Delete dialog warns about the block"
        assert out["danger"] == ["delete-files"], "Delete is red"
    plain = _menu(page, _show(redownload_blocked=0, local_count=0)).value
    assert "allow-redownload" not in plain["actions"]
    assert plain["danger"] == ["drop"], "Drop is red"
