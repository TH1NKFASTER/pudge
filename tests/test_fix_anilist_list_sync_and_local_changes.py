"""The AniList list refresh and every list change keep working while AniList refuses.

- A title deleted from the AniList list on anilist.co stayed Watching/Planning here
  forever (its episodes kept downloading, Planning kept offering it).
- Drop, Delete (with a status), Add to / Remove from Planning, Reset progress and
  "Keep" failed outright under a 403 or without a token, so files could not even be
  deleted; deleting the files of a title not on the list created a Dropped entry.
- The manual Sync said "saved" when AniList refused; cleanup read a flag another
  thread's refresh could reset; relation graphs were refreshed right after a failed
  sync; a weekly show stopped being searched one episode after the last good sync;
  a dropped folder was forgotten with "no anime recognized" while AniList refused.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

import pudge.web_app as web_app_module
from pudge.anilist_tracking import pending_scores, pending_statuses, pending_summary, queue_pending_status
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.providers import anilist as al
from pudge.providers.anilist import AniListClient, AniListHTTPError
from test_fix_anilist_sync_and_movie_stats import _config
from test_web_app import make_api


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    al.configure_rate_limit_dir(tmp_path / "rl")
    monkeypatch.setattr(al.time, "sleep", lambda _seconds: None)

    def make(handler):
        def factory(*_args, **_kwargs):
            client = AniListClient("https://graphql.anilist.co", "tok")
            headers = client.client.headers
            client.client.close()
            client.client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
            return client

        return factory

    yield make
    al.configure_rate_limit_dir(None)


def _entry(media_id, status, progress, title):
    return {
        "progress": progress,
        "status": status,
        "score": 0,
        "createdAt": 1,
        "media": {
            "id": media_id,
            "title": {"romaji": title, "english": None, "native": None, "userPreferred": title},
            "synonyms": [],
            "episodes": 12,
            "format": "TV",
            "status": "RELEASING",
            "siteUrl": f"https://anilist.co/anime/{media_id}",
            "coverImage": {},
            "nextAiringEpisode": None,
        },
    }


def _list_handler(lists):
    def handler(request):
        query = json.loads(request.content)["query"]
        if "Viewer" in query:
            return httpx.Response(200, json={"data": {"Viewer": {"id": 1}}})
        assert "MediaListCollection" in query
        return httpx.Response(200, json={"data": {"MediaListCollection": {"lists": lists}}})

    return handler


# ----------------------------------------------------------- list removal (ANI-1)


def test_entry_removed_on_anilist_becomes_removed_here(tmp_path, monkeypatch, real_client):
    from pudge.manager import AnimeManager

    cfg = _config(tmp_path)
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=10, title="Local folder", status="", episodes=12))
    before = [
        {
            "status": "CURRENT",
            "entries": [
                _entry(5, "CURRENT", 3, "Deleted"),
                _entry(6, "CURRENT", 1, "Kept"),
                _entry(7, "CURRENT", 4, "Completed on site"),
                _entry(9, "CURRENT", 0, "Queued"),
            ],
        },
        {"status": "PLANNING", "entries": [_entry(8, "PLANNING", 0, "Deleted plan")]},
    ]
    after = [
        {"status": "CURRENT", "entries": [_entry(6, "CURRENT", 1, "Kept")]},
        {"status": "COMPLETED", "entries": [_entry(7, "COMPLETED", 12, "Completed on site")]},
    ]
    lists = {"now": before}

    def handler(request):
        if "mediaId" in (json.loads(request.content).get("variables") or {}):
            # Sending the queued status fails for now (a 5xx): it stays queued.
            return httpx.Response(502, text="bad gateway")
        return _list_handler(lists["now"])(request)

    monkeypatch.setattr("pudge.manager.AniListClient", real_client(handler))
    manager = AnimeManager(cfg)
    manager.sync_anilist()  # the first full list: remembered, nothing removed
    queue_pending_status(db, 9, "PAUSED", previous="CURRENT")  # set in the window, not sent yet
    db.set_anime_status(9, "PAUSED")
    lists["now"] = after
    manager.sync_anilist()

    assert manager.db.get_anime(7).status == "COMPLETED"
    assert manager.db.get_anime(5).status == "REMOVED" and manager.db.get_anime(8).status == "REMOVED"
    assert 5 not in {item.media_id for item in manager.db.anime_list(("CURRENT",))}
    assert manager.db.get_anime(6).status == "CURRENT"
    assert manager.db.get_anime(9).status == "PAUSED"  # its change still waits to be sent
    assert manager.db.get_anime(10).status == ""  # local-only titles are not AniList's


def test_titles_followed_before_adding_a_token_are_not_removed(tmp_path, monkeypatch, real_client):
    api = make_api(tmp_path)
    for config in (api.config, api.manager.config):
        config.anilist.enabled = True
        config.anilist.access_token = ""
    api.manager.db.upsert_anime(LibraryAnime(media_id=14, title="Followed locally", status="", episodes=12))
    api.manager.db.upsert_anime(LibraryAnime(media_id=15, title="Planned locally", status="", episodes=12))
    api.set_anime_watching(14)
    api.add_to_planning(15)
    for config in (api.config, api.manager.config):
        config.anilist.access_token = "tok"
    lists = [{"status": "CURRENT", "entries": [_entry(6, "CURRENT", 1, "Other")]}]
    monkeypatch.setattr("pudge.manager.AniListClient", real_client(_list_handler(lists)))
    for _ in range(2):
        api.manager.sync_anilist()
        assert api.manager.db.get_anime(14).status == "CURRENT"
        assert api.manager.db.get_anime(15).status == "PLANNING"


def test_mass_or_uncertain_removals_are_skipped(tmp_path, monkeypatch, real_client):
    from pudge.manager import AnimeManager

    cfg = _config(tmp_path)
    full = [{"status": "CURRENT", "entries": [_entry(i, "CURRENT", 0, f"T{i}") for i in range(1, 11)]}]
    lists = {"now": full}
    monkeypatch.setattr(
        "pudge.manager.AniListClient", real_client(lambda request: _list_handler(lists["now"])(request))
    )
    manager = AnimeManager(cfg)
    warnings: list[str] = []
    real_warning = manager.logger.warning
    monkeypatch.setattr(
        manager.logger, "warning", lambda msg, *args: warnings.append(msg % args) or real_warning(msg, *args)
    )
    manager.sync_anilist()

    lists["now"] = [{"status": "CURRENT", "entries": [_entry(1, "CURRENT", 0, "T1")]}]  # 9 gone at once
    manager.sync_anilist()
    assert any("reason=too_many" in text for text in warnings)
    assert {manager.db.get_anime(i).status for i in range(2, 11)} == {"CURRENT"}

    lists["now"] = full
    manager.sync_anilist()
    # One title is missing now, but another entry came back without its media.
    entries = [_entry(i, "CURRENT", 0, f"T{i}") for i in range(3, 11)]
    lists["now"] = [
        {"status": "CURRENT", "entries": [*entries, {"progress": 0, "status": "CURRENT", "media": None}]}
    ]
    warnings.clear()
    manager.sync_anilist()
    assert any("reason=entry_without_media" in text for text in warnings)
    assert manager.db.get_anime(1).status == "CURRENT" and manager.db.get_anime(2).status == "CURRENT"


def test_unsent_status_of_a_title_outside_watching_survives_the_refresh(tmp_path, monkeypatch, real_client):
    from pudge.manager import AnimeManager

    cfg = _config(tmp_path)
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=7, title="Done", status="DROPPED", progress=12, episodes=12))
    queue_pending_status(db, 7, "DROPPED", previous="COMPLETED")
    lists = [{"status": "COMPLETED", "entries": [_entry(7, "COMPLETED", 12, "Done")]}]

    def handler(request):
        if "mediaId" in (json.loads(request.content).get("variables") or {}):
            return httpx.Response(502, text="bad gateway")
        return _list_handler(lists)(request)

    monkeypatch.setattr("pudge.manager.AniListClient", real_client(handler))
    AnimeManager(cfg).sync_anilist()
    assert db.get_anime(7).status == "DROPPED"
    assert pending_statuses(db) == {7: "DROPPED"}


def test_list_from_cache_or_an_empty_list_removes_nothing(tmp_path, monkeypatch, real_client):
    from pudge.manager import AnimeManager

    cfg = _config(tmp_path)
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=5, title="Watching", status="CURRENT", progress=3, episodes=12))

    refusing = real_client(lambda request: httpx.Response(403, json={"errors": [{"message": "disabled"}]}))
    monkeypatch.setattr("pudge.manager.AniListClient", refusing)
    manager = AnimeManager(cfg)
    result = manager.sync_anilist()
    assert result.used_cache is True and "disabled" in result.error
    assert manager.db.get_anime(5).status == "CURRENT"

    al._PROCESS_COOLDOWN_UNTIL = 0.0
    al._write_marker("rate-limit.json", None)
    monkeypatch.setattr("pudge.manager.AniListClient", real_client(_list_handler([])))
    assert manager.sync_anilist().used_cache is False
    assert manager.db.get_anime(5).status == "CURRENT"


# ------------------------------------------------------- manual sync (ANI-9/12)


def test_manual_sync_reports_anilist_refusing_and_skips_relation_graphs(tmp_path, monkeypatch, real_client):
    from pudge.manager import AnimeManager

    cfg = _config(tmp_path)
    Database(cfg.library.database_path).upsert_anime(
        LibraryAnime(media_id=5, title="Watching", status="CURRENT", progress=3, episodes=12)
    )
    monkeypatch.setattr(
        "pudge.manager.AniListClient",
        real_client(
            lambda request: httpx.Response(
                403, json={"errors": [{"message": "The AniList API has been temporarily disabled"}]}
            )
        ),
    )
    manager = AnimeManager(cfg)
    graphs: list[int] = []
    monkeypatch.setattr(manager, "refresh_due_relation_graphs", lambda **_kw: graphs.append(1) or 1)
    monkeypatch.setattr(manager, "prefetch_covers", lambda *_a, **_k: 0)

    stats = manager.refresh_anilist_cache()

    assert stats["cached"] is True
    assert "HTTP 403" in stats["error"] and "temporarily disabled" in stats["error"]
    assert graphs == []
    assert manager.db.get_state("anilist_last_error") == stats["error"]


def test_sync_toast_and_settings_line_say_why_anilist_is_unavailable(tmp_path, page):
    state = {
        "anilist_status": {
            "episodes": 3,
            "titles": 1,
            "statuses": 1,
            "scores": 2,
            "last_error": "",
            "token_invalid": False,
            "refused": {
                "status": 403,
                "message": "The AniList API has been temporarily disabled",
                "minutes": 12,
            },
        }
    }
    code = """
    await syncAniList(false);
    await __tick(5);
    return {toast: __toasts().at(-1), line: anilistQueueStatusText(ui.state.anilist_status),
            invalid: anilistQueueStatusText({token_invalid: true}),
            error: t('toast.anilistUnavailable', {error: PudgeI18n.message(ERROR)}),
            refusing: PudgeI18n.t('ui.anilistRefusing', {message: 'The AniList API has been temporarily disabled', minutes: 12}),
            episodes: PudgeI18n.tn('ui.anilistEpisodesWaiting', 3), changes: PudgeI18n.tn('ui.anilistChangesWaiting', 1),
            scores: PudgeI18n.tn('ui.anilistScoresWaiting', 2),
            rejected: PudgeI18n.t('ui.anilistTokenRejected')};
    """.replace(
        "ERROR",
        json.dumps("AniList refused the request (HTTP 403: The AniList API has been temporarily disabled)"),
    )
    for lang in ("en", "ru"):
        answer = {
            "skipped": False,
            "state": state,
            "stats": {
                "anime": 1,
                "cached": True,
                "warning": "AniList did not return the list for now; showing the last saved data",
                "error": "AniList refused the request (HTTP 403: The AniList API has been temporarily disabled)",
            },
        }
        out = page.run(code, lang=lang, state=state, api={"sync_anilist": answer}).value
        assert out["toast"] == out["error"], lang
        assert "403" in out["toast"]
        assert out["line"] == " · ".join([out["refusing"], out["episodes"], out["changes"], out["scores"]])
        assert out["invalid"] == out["rejected"]


# --------------------------------------------------------- MGR-12 races


def test_cleanup_trusts_its_own_sync_result_not_a_shared_flag(tmp_path, monkeypatch):
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    cfg.agent.delete_only_managed_files = False
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = (tmp_path / "Example - 05.mkv").resolve()
    video.write_bytes(b"v")
    manager.db.upsert_anime(LibraryAnime(media_id=88, title="Example", status="CURRENT", progress=5))
    manager.db.upsert_episode(LibraryEpisode(88, "Example", 5, video, state="ready"))
    manager.db.schedule_cleanup(video, 0.0)

    class CachedList(list):
        used_cache = True

    def sync_from_cache():
        # Another thread's successful refresh finishes meanwhile and resets any
        # instance-wide "used the cache" flag.
        manager._last_anilist_used_cache = False
        return CachedList(manager.db.anime_list(("CURRENT", "PLANNING")))

    monkeypatch.setattr(manager, "sync_anilist", sync_from_cache)
    assert manager.cleanup() == 0
    assert video.exists()


def test_same_episode_is_not_acquired_twice_by_two_threads(tmp_path, monkeypatch):
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)
    monkeypatch.setattr(manager, "_ensure_download_space", lambda *_a, **_k: None)
    monkeypatch.setattr(manager, "_race_pool", lambda candidates, **_k: candidates)
    monkeypatch.setattr(manager, "_validated_release_identity", lambda _a, release, *_r: release)
    monkeypatch.setattr(manager.download_intents, "get", lambda *_a, **_k: None)
    monkeypatch.setattr(manager, "_note_storage_admission", lambda *_a, **_k: None)
    started: list[str] = []
    release_gate = threading.Event()

    def impl(*_args, **_kwargs):
        started.append(threading.current_thread().name)
        release_gate.wait(5)
        return None

    monkeypatch.setattr(manager, "_race_add_candidates_impl", impl)

    class SlowSet(set):
        def __contains__(self, item):
            # Widen the check-then-add window as a busy machine would.
            result = super().__contains__(item)
            time.sleep(0.05)
            return result

    manager._active_acquisition_keys = SlowSet()
    candidate = type("Release", (), {"size_bytes": 1, "info_hash": "a" * 40, "title": "x"})()
    threads = [
        threading.Thread(
            target=manager._race_add_candidates, args=(1, [candidate]), kwargs={"episode": 3, "batch": False}
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    release_gate.set()
    for thread in threads:
        thread.join(5)
    assert len(started) == 1


# ------------------------------------------------- weekly show during an outage


def test_weekly_show_keeps_searching_when_anilist_sync_is_stale(tmp_path, monkeypatch):
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.qbittorrent.enabled = True
    cfg.nyaa.max_auto_download_per_anime = 5
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    manager = AnimeManager(cfg)
    week = 7 * 86400
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=1,
            title="Weekly",
            status="CURRENT",
            progress=4,
            episodes=12,
            media_status="RELEASING",
            next_airing_episode=5,
            next_airing_at=int(time.time()) - 2 * week - 3600,
        )
    )
    searched: list[int] = []
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda _m, *, episode, batch, automatic=False: searched.append(episode) or [],
    )

    manager.db.set_state("anilist_synced_at", str(time.time() - 3 * 86400))  # AniList refusing for days
    manager.auto_search_current()
    assert searched == [5, 6, 7]

    searched.clear()
    manager.db.set_state("anilist_synced_at", str(time.time() - 600))
    manager.auto_search_current()
    assert searched == [5]


# ------------------------------------------------------ dropped folder (SCEN-4)


def _drop_frieren(tmp_path, monkeypatch, *, local_entry: bool):
    import pudge.manager as manager_module

    api = make_api(tmp_path)
    api.config.anilist.enabled = True
    api.manager.config.anilist.enabled = True
    if local_entry:
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id=154587,
                title="Sousou no Frieren",
                titles=["Sousou no Frieren", "Frieren: Beyond Journey's End"],
                status="PLANNING",
                episodes=28,
                format="TV",
                media_status="FINISHED",
                season_year=2023,
            )
        )

    class Refusing:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, identity):
            raise AniListHTTPError("AniList refused the request (HTTP 403: disabled)", status_code=403)

        def close(self):
            pass

    monkeypatch.setattr(manager_module, "AniListClient", Refusing)
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))
    folder = tmp_path / "Downloads" / "[SubsPlease] Sousou no Frieren (01-28) (1080p) [Batch]"
    folder.mkdir(parents=True)
    for number in range(1, 4):
        (
            folder / f"[SubsPlease] Sousou no Frieren - {number:02d} (1080p) [ABCD{number:04d}].mkv"
        ).write_bytes(b"v")
    return api, folder, api.import_dropped_paths([str(folder)])


def test_dropped_folder_is_kept_and_explained_while_anilist_refuses(tmp_path, monkeypatch):
    from pudge.i18n import err

    api, folder, result = _drop_frieren(tmp_path, monkeypatch, local_entry=False)
    assert [item["error"] for item in result["failed"]] == [err("web.dropFolderAniListUnavailable")]
    assert folder.resolve() in api.manager.dropped_media_folders()


def test_dropped_folder_of_a_listed_title_matches_without_anilist(tmp_path, monkeypatch):
    api, _folder, result = _drop_frieren(tmp_path, monkeypatch, local_entry=True)
    assert not result.get("failed"), result.get("failed")
    assert {int(item.media_id) for item in api.manager.db.episodes()} == {154587}


# --------------------------------------- list changes without AniList (WEBFLOW-2)


class _AniList:
    calls: list[tuple[str, int, str]] = []
    refuse = True

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def _call(self, name, media_id, value=""):
        _AniList.calls.append((name, int(media_id), value))
        if _AniList.refuse:
            raise AniListHTTPError("AniList refused the request (HTTP 403: disabled)", status_code=403)
        return {"status": value, "progress": 0}

    def set_list_status(self, media_id, status):
        return self._call("status", media_id, status)

    def delete_list_entry(self, media_id):
        self._call("delete", media_id)
        return True

    def set_progress(self, media_id, progress, status=None):
        return self._call("progress", media_id, str(status))

    def set_score(self, media_id, score):
        result = self._call("score", media_id, str(score))
        return {**result, "score": score}

    def close(self):
        pass


@pytest.fixture
def api(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    api.config.anilist.enabled = True
    api.config.anilist.access_token = "token"
    _AniList.calls = []
    _AniList.refuse = True
    monkeypatch.setattr(web_app_module, "AniListClient", _AniList)
    monkeypatch.setattr(api.manager, "refresh_anilist_cache", lambda: {"anime": 0, "covers": 0})
    return api


def _title_with_files(api, tmp_path: Path, media_id: int, status: str) -> list[Path]:
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=media_id, title=f"Title {media_id}", status=status, episodes=2)
    )
    videos = []
    for episode in (1, 2):
        video = tmp_path / f"Title {media_id} - {episode:02d}.mkv"
        video.write_bytes(b"v")
        api.manager.db.upsert_episode(
            LibraryEpisode(media_id, f"Title {media_id}", episode, video, state="ready")
        )
        videos.append(video)
    return videos


def _scheduled(api, videos) -> bool:
    rows = [api.manager.db.episode_by_path(video) for video in videos]
    return all(row.state == "dropped" and row.delete_after for row in rows)


def test_delete_files_works_without_token_and_under_403(api, tmp_path):
    videos = _title_with_files(api, tmp_path, 7, "CURRENT")
    result = api.drop_anime(7, "DROPPED")
    assert result["ok"] is True and result["anilist_pending"] is True
    assert _scheduled(api, videos)
    assert pending_statuses(api.manager.db) == {7: "DROPPED"}
    assert api.manager.db.get_anime(7).status == "DROPPED"

    # "Keep": AniList still has Watching, so the queued Dropped is cancelled, never sent.
    kept = api.keep_dropped_anime(7)
    assert kept["status"] == "CURRENT" and "warning" not in kept
    assert pending_statuses(api.manager.db) == {}
    assert api.manager.db.get_anime(7).status == "CURRENT"

    api.config.anilist.access_token = ""
    _AniList.calls = []
    others = _title_with_files(api, tmp_path, 8, "CURRENT")
    result = api.drop_anime(8, "DROPPED")
    assert result["ok"] is True and result["anilist_pending"] is False
    assert _scheduled(api, others) and _AniList.calls == []
    assert pending_statuses(api.manager.db) == {}


def test_deleting_files_of_an_unlisted_title_creates_no_anilist_entry(api, tmp_path):
    _AniList.refuse = False
    videos = _title_with_files(api, tmp_path, 9, "")
    result = api.drop_anime(9, "DROPPED")  # the dialog's default for a title not on the list
    assert result["ok"] is True and _scheduled(api, videos)
    assert _AniList.calls == []
    assert api.manager.db.get_anime(9).status == ""


def test_drop_and_planning_changes_queue_under_403(api, tmp_path):
    videos = _title_with_files(api, tmp_path, 11, "CURRENT")
    assert api.drop_anime(11)["anilist_pending"] is True
    assert _scheduled(api, videos)

    api.manager.db.upsert_anime(LibraryAnime(media_id=12, title="Sequel", status=""))
    added = api.add_to_planning(12)
    assert added["ok"] is True and added["anilist_pending"] is True
    assert api.manager.db.get_anime(12).status == "PLANNING"

    api.manager.db.upsert_anime(LibraryAnime(media_id=13, title="Planned", status="PLANNING"))
    removed = api.remove_from_planning(13)
    assert removed["ok"] is True and removed["anilist_pending"] is True
    assert api.manager.db.get_anime(13).status == "REMOVED"
    assert pending_statuses(api.manager.db) == {11: "DROPPED", 12: "PLANNING", 13: "REMOVED"}


def test_watching_and_reset_without_token_are_local(api, tmp_path):
    api.config.anilist.access_token = ""
    api.manager.db.upsert_anime(LibraryAnime(media_id=14, title="Plan", status="PLANNING"))
    assert api.set_anime_watching(14)["anilist_pending"] is False
    assert api.manager.db.get_anime(14).status == "CURRENT"
    _title_with_files(api, tmp_path, 15, "CURRENT")
    assert api.reset_anime_progress(15)["ok"] is True
    assert _AniList.calls == [] and pending_statuses(api.manager.db) == {}


def test_reset_progress_under_403_says_it_needs_anilist(api, tmp_path):
    from pudge.i18n import err

    _title_with_files(api, tmp_path, 16, "CURRENT")
    with pytest.raises(RuntimeError) as caught:
        api.reset_anime_progress(16)
    assert str(caught.value).startswith(err("web.resetProgressNeedsAniList", error="X").split("(")[0])


def test_score_queues_under_403_last_value_wins_and_is_sent_after_statuses(api, monkeypatch):
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=20, title="Rated", status="CURRENT", progress=12, episodes=12)
    )
    first = api.set_anilist_score(20, 8, 12)
    assert first["ok"] is True and first["anilist_pending"] is True
    assert api.set_anilist_score(20, 9, 12)["anilist_pending"] is True
    assert api.manager.db.get_anime(20).user_score == 9
    assert pending_scores(api.manager.db) == {20: 9.0}
    assert pending_summary(api.manager.db)["scores"] == 1
    assert api.get_state()["anilist_status"]["scores"] == 1
    queue_pending_status(api.manager.db, 21, "PLANNING", previous="")
    order: list[tuple] = []

    class Back:
        fail_scores = True

        def __init__(self, *_args, **_kwargs):
            self.last_list_entries = {20: {"status": "CURRENT", "progress": 12, "episodes": 12}}

        def set_list_status(self, media_id, status):
            order.append(("status", media_id, status))

        def set_score(self, media_id, score):
            order.append(("score", media_id, score))
            if Back.fail_scores:
                raise AniListHTTPError("AniList error: HTTP 502", status_code=502)
            return {"score": score}

        def library_compact(self):
            # AniList still has the old score until the queued one is sent.
            return [
                LibraryAnime(
                    media_id=20, title="Rated", status="CURRENT", progress=12, episodes=12, user_score=5.0
                )
            ]

        def close(self):
            pass

    monkeypatch.setattr("pudge.manager.AniListClient", Back)
    api.manager.config.anilist.enabled = True
    api.manager.config.anilist.access_token = "token"
    api.manager.sync_anilist()
    assert order == [("status", 21, "PLANNING"), ("score", 20, 9.0)]
    assert api.manager.db.get_anime(20).user_score == 9  # the queued score stays local
    assert pending_scores(api.manager.db) == {20: 9.0}

    Back.fail_scores = False
    order.clear()
    api.manager.sync_anilist()
    assert order == [("score", 20, 9.0)]
    assert pending_scores(api.manager.db) == {}


def test_score_without_token_is_local(api):
    api.config.anilist.access_token = ""
    api.manager.db.upsert_anime(LibraryAnime(media_id=22, title="Local", status="CURRENT"))
    result = api.set_anilist_score(22, 7)
    assert result["ok"] is True and result["anilist_pending"] is False
    assert api.manager.db.get_anime(22).user_score == 7
    assert _AniList.calls == [] and pending_scores(api.manager.db) == {}
