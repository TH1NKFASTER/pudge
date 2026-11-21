"""SEQPRE: the next entry after an anime's final episode is computed ahead, not when the sheet opens.

``final_sequel_option`` read the AniList relations graph each time the rating sheet asked; a title
without a sequel always forced a fresh graph (several AniList requests), so the phone's sheet
showed "Checking AniList…" for a long time. The answer is now cached per title (memory + a small
file, final_sequel_cache) and warmed in the background when the final episode starts (mpv, a
phone's player/stream/download/place). Invented titles and ids only; no network.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.database import Database
from pudge.final_sequel_cache import FinalSequelCache, ends_title
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi


def _option(sequel_id: int | None = 902, status: str = "") -> dict:
    if sequel_id is None:
        return {"ok": True, "sequel": None}
    return {"ok": True, "sequel": {"media_id": sequel_id, "title": "Invented 2", "list_status": status}}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_an_answer_is_computed_once_and_kept_on_disk_for_its_ttl(tmp_path) -> None:
    calls: list[tuple[int, bool]] = []
    clock = _Clock()

    def compute(media_id: int, background: bool) -> dict:
        calls.append((media_id, background))
        return _option(None)

    path = tmp_path / "final-sequel.json"
    cache = FinalSequelCache(compute, path=path, clock=clock, ttl=100)
    assert cache.option(901) == {"ok": True, "sequel": None}
    assert cache.option(901) == {"ok": True, "sequel": None}
    assert calls == [(901, False)]
    # Another process / a restart reads the file.
    again = FinalSequelCache(compute, path=path, clock=clock, ttl=100)
    assert again.cached(901) == {"ok": True, "sequel": None}
    clock.now += 101
    assert again.cached(901) is None
    again.option(901)
    assert calls == [(901, False), (901, False)]


def test_a_fallback_answer_lives_briefly_and_a_failure_is_not_kept(tmp_path) -> None:
    clock = _Clock()
    answers = [{"ok": True, "sequel": None, "partial": True}]
    cache = FinalSequelCache(lambda *_: answers[0], clock=clock, ttl=1000, partial_ttl=10)
    cache.option(901)
    assert cache.cached(901) is not None
    clock.now += 11
    assert cache.cached(901) is None

    def broken(*_):
        raise RuntimeError("AniList away")

    failing = FinalSequelCache(broken, clock=clock)
    try:
        failing.option(901)
    except RuntimeError:
        pass
    assert failing.cached(901) is None


def test_a_changed_list_status_of_the_sequel_drops_the_answer() -> None:
    statuses: dict[int, str | None] = {902: ""}
    calls: list[int] = []

    def compute(media_id: int, _background: bool) -> dict:
        calls.append(media_id)
        return _option(902, statuses[902] or "")

    cache = FinalSequelCache(compute, status_of=lambda sid: statuses.get(sid))
    assert cache.option(901)["sequel"]["list_status"] == ""
    cache.option(901)
    assert calls == [901]
    statuses[902] = "PLANNING"  # Planning from the sheet, or an AniList sync
    assert cache.option(901)["sequel"]["list_status"] == "PLANNING"
    assert calls == [901, 901]
    cache.invalidate(sequel_id=902)
    cache.option(901)
    assert calls == [901, 901, 901]
    cache.invalidate(901)
    assert cache.cached(901) is None


def test_warm_up_runs_in_the_background_and_a_request_reuses_it() -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[int, bool]] = []

    def compute(media_id: int, background: bool) -> dict:
        calls.append((media_id, background))
        started.set()
        release.wait(5)
        return _option(902)

    cache = FinalSequelCache(compute)
    assert cache.warm(901) is True
    assert started.wait(5)
    assert cache.warm(901) is False  # already on its way
    answer: list[dict] = []
    asker = threading.Thread(target=lambda: answer.append(cache.option(901)))
    asker.start()
    time.sleep(0.1)
    assert answer == []  # waits for the warm-up instead of asking AniList again
    release.set()
    asker.join(5)
    assert answer and answer[0]["sequel"]["media_id"] == 902
    assert calls == [(901, True)]
    assert cache.warm(901) is False  # fresh
    assert cache.warm(0) is False


def test_ends_title_uses_the_finale_rule(tmp_path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,updated_at)"
            " VALUES(901,'Invented Show','CURRENT',11,12,0)"
        )
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,format,updated_at)"
            " VALUES(904,'Invented Movie','CURRENT',0,'MOVIE',0)"
        )
    assert ends_title(db, 901, 12) is True
    assert ends_title(db, 901, 11) is False
    assert ends_title(db, 904, 0) is True
    assert ends_title(db, 999, 12) is False
    db.mark_rating_prompted(901, 12)
    assert ends_title(db, 901, 12) is False


def _api(graphs: list[dict], tmp_path=None) -> tuple[WebAppApi, list[tuple[int, bool, bool]]]:
    from pudge.providers import anilist

    calls: list[tuple[int, bool, bool]] = []

    def relation_graph(media_id, force_refresh=False):
        calls.append((int(media_id), bool(force_refresh), anilist._BULK.get()))
        return graphs[min(len(calls), len(graphs)) - 1]

    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(
        relation_graph=relation_graph, db=SimpleNamespace(get_anime=lambda _id: None)
    )
    api.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    if tmp_path is not None:
        api.config = SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path))
    return api, calls


_NO_SEQUEL = {"nodes": [{"media_id": 901, "title": "Invented"}], "edges": []}


def test_a_title_without_a_sequel_no_longer_refetches_the_graph_each_time(tmp_path) -> None:
    api, calls = _api([_NO_SEQUEL], tmp_path)
    assert api.final_sequel_option(901) == {"ok": True, "sequel": None}
    assert api.final_sequel_option(901) == {"ok": True, "sequel": None}
    # Before: two graph reads (the second one a forced AniList refresh) on every sheet.
    assert calls == [(901, False, False), (901, True, False)]
    assert (tmp_path / "final-sequel.json").is_file()


def test_warm_up_is_paced_as_bulk_work_and_only_for_the_final_episode(tmp_path) -> None:
    api, calls = _api([_NO_SEQUEL])
    api.manager.db = SimpleNamespace(get_anime=lambda _id: None)
    # Not a final episode (unknown title): nothing asked.
    assert api.warm_final_sequel(901, 5) is False
    api._final_sequel_cache_store()._threaded = False
    assert api.warm_final_sequel(901) is True
    assert calls == [(901, False, True), (901, True, True)]
    assert api.final_sequel_option(901) == {"ok": True, "sequel": None}
    assert len(calls) == 2


def test_planning_from_the_sheet_drops_the_suggestion(tmp_path) -> None:
    sequel_graph = {
        "nodes": [{"media_id": 901, "title": "Invented"}, {"media_id": 902, "title": "Invented 2"}],
        "edges": [{"source": 901, "target": 902, "relation_type": "SEQUEL"}],
    }
    api, calls = _api([sequel_graph], tmp_path)
    assert api.final_sequel_option(901)["sequel"]["can_planning"] is True
    api._invalidate_final_sequel(sequel_id=902)
    api.final_sequel_option(901)
    assert len(calls) == 2


def _mac(tmp_path):
    from pudge.iphone_service import IPhoneService

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    db = Database(cfg.library.database_path)
    ids = {}
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,media_status,updated_at)"
            " VALUES(901,'Invented Show','CURRENT',11,12,'FINISHED',0)"
        )
        for number in (11, 12):
            video = (tmp_path / f"Invented - {number:02d}.mkv").resolve()
            video.write_bytes(b"x")
            ids[number] = conn.execute(
                "INSERT INTO episodes(media_id,title,episode,media_episode,video_path,state,updated_at)"
                " VALUES(901,'Invented',?,?,?,'ready',0)",
                (number, number, str(video)),
            ).lastrowid
    service = IPhoneService(db, tmp_path / "mobile")
    warmed: list[tuple[int, int]] = []

    def warm(media_id, episode=None):
        warmed.append((media_id, episode))
        return ends_title(db, media_id, episode)

    service.api = SimpleNamespace(manager=SimpleNamespace(db=db), warm_final_sequel=warm)
    return service, ids, warmed


def test_a_phone_playing_or_downloading_the_final_episode_warms_the_mac(tmp_path, monkeypatch) -> None:
    service, ids, warmed = _mac(tmp_path)
    media = service.media
    # A place reported in the final episode (a downloaded copy played there).
    assert media.episode_position(ids[12], {"seconds": 30, "duration": 1440}) == {"ok": True}
    assert warmed == [(901, 12)]
    # Every 15 s: checked once per episode for a while.
    media.episode_position(ids[12], {"seconds": 45, "duration": 1440})
    assert warmed == [(901, 12)]
    media.episode_position(ids[11], {"seconds": 45, "duration": 1440})
    assert warmed == [(901, 12), (901, 11)]
    # Download / stream of the final episode: prepare_episode.
    media.finale._warm_checked.clear()
    monkeypatch.setattr(media, "_prepare_worker", lambda *a, **k: None)
    media.prepare_episode(ids[12])
    assert warmed[-1] == (901, 12)


def test_mpv_start_of_the_final_episode_warms_the_cache(tmp_path) -> None:
    api = object.__new__(WebAppApi)
    episode = SimpleNamespace(media_id=901, last_episode=12, episode=12)
    asked: list[tuple] = []
    api.manager = SimpleNamespace(db=SimpleNamespace(episode_by_path=lambda _p: episode))
    api.warm_final_sequel = lambda media_id, number=None: asked.append((media_id, number)) or True
    api._warm_final_sequel_for_path(tmp_path / "x.mkv")
    assert asked == [(901, 12)]
    api.manager = SimpleNamespace(db=SimpleNamespace(episode_by_path=lambda _p: None))
    api._warm_final_sequel_for_path(tmp_path / "x.mkv")
    assert asked == [(901, 12)]


def test_local_status_change_invalidates_through_set_local_status(tmp_path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=902, title="Invented 2", status="", cover_url="", site_url=""))
    api = object.__new__(WebAppApi)
    dropped: list = []
    api._invalidate_final_sequel = lambda media_id=None, sequel_id=None: dropped.append(sequel_id)
    api.manager = SimpleNamespace(db=db)
    api._set_local_anilist_status(902, "PLANNING")
    assert dropped == [902]
