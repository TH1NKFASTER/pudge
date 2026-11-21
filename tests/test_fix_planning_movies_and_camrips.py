"""Planning discovery for released movies, and cinema recordings (camrips).

What went wrong before, and what happens now:

- A Planning entry without a premiere date was skipped without saving anything, so
  Pudge forgot it had seen the entry unreleased. When AniList later filled in a date
  that was already more than a day old, the entry was grandfathered as "ignored" and
  never searched. Now the first unreleased sighting is remembered (phase "waiting")
  and the entry is armed (searched) as soon as a premiere timestamp is known, even a
  past one. Entries first seen already released stay ignored.
- Movies that were already "ignored" (Pudge never recorded when it first saw them),
  e.g. "Mahou Shoujo Madoka☆Magica: Walpurgis no Kaiten" (premiere 2026-08-28,
  ignored on 2026-09-24), were never searched. AniList's list add time (MediaList
  createdAt) is fetched and stored now; such a movie is reviewed once: armed when it
  was added to the list before its premiere and the premiere is at most 180 days
  ago, otherwise it stays ignored and is not reviewed again. An unknown add time
  (before the next AniList sync) decides nothing. Dismissed offers and TV entries
  are untouched. A movie whose premiere is a week old is searched once a day.
- Cinema recordings ("... (CAMRIP)", HDCAM, TELESYNC, "[TS]" ...) were offered and
  could be accepted by Planning discovery. A new Downloads setting "Allow cinema
  recordings" (off by default) drops them from every release search and from
  automatic downloads; when on they are kept and scored as before.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.planned_release_discovery import _SEARCH_REVISION, PlannedReleaseDiscovery, discovery_work_due
from pudge.providers.anilist import AniListClient
from pudge.providers.nyaa import is_camrip_release
from pudge.web_app import WebAppApi
from test_planned_release_discovery import release, service
from test_source_neutral import _config, _info, _logged, _no_requests
from tests.i18n_support import en, ru


def _ts(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


MADOKA_ID = 133007
MADOKA_TITLE = "Mahou Shoujo Madoka☆Magica: Walpurgis no Kaiten"
PREMIERE = _ts(2026, 8, 28)  # date-only AniList start date -> noon UTC
NOW = _ts(2026, 10, 14)  # 47 days after the premiere
ADDED_BEFORE = _ts(2026, 6, 1)
ADDED_AFTER = _ts(2026, 9, 24)
CAMRIP = "Puella Magi Madoka Magica Movie 4 - Walpurgisnacht Rising (CAMRIP)"


def _movie(media_id: int = MADOKA_ID, *, list_added_at: float = ADDED_BEFORE, **changes) -> LibraryAnime:
    anime = LibraryAnime(
        media_id,
        MADOKA_TITLE,
        titles=[MADOKA_TITLE, "Puella Magi Madoka Magica the Movie: Walpurgisnacht: Rising"],
        status="PLANNING",
        format="MOVIE",
        episodes=1,
        start_date="2026-08-28",
        media_status="FINISHED",
        list_added_at=list_added_at,
    )
    return replace(anime, **changes)


def _state(svc, media_id: int = MADOKA_ID) -> dict:
    return json.loads(svc.db.get_state(svc._key(media_id)))


# --------------------------------------------------------------- A. learned unreleased


def test_undated_entry_seen_in_planning_is_searched_once_a_past_premiere_is_known(tmp_path):
    anime = _movie(start_date=None, media_status="NOT_YET_RELEASED", list_added_at=0.0)
    svc, mgr = service(tmp_path, anime)
    seen_at = _ts(2026, 5, 1)
    assert svc.tick(now=seen_at) == {"armed": 0, "searched": 0, "found": 0}
    assert _state(svc) == {"phase": "waiting", "seen_unreleased_at": seen_at}
    # Nothing to do while the date is unknown: the agent is not woken.
    assert not discovery_work_due(mgr.db, now=seen_at + 3600)

    # AniList fills in the date only after the premiere (more than a day ago).
    mgr.db.anime[MADOKA_ID] = replace(anime, start_date="2026-08-28", media_status="FINISHED")
    assert discovery_work_due(mgr.db, now=NOW)
    assert svc.tick(now=NOW) == {"armed": 1, "searched": 1, "found": 0}
    assert mgr.search_releases.call_args.kwargs == {
        "episode": None,
        "batch": True,
        "automatic": True,
        "allow_anilist_network": False,
    }
    state = _state(svc)
    assert state["phase"] == "armed" and state["seen_unreleased_at"] == seen_at
    assert state["airing_at"] == PREMIERE
    assert state["next_check"] == NOW + 24 * 3600  # premiere a week+ old: daily


def test_undated_tv_entry_waits_and_is_armed_for_its_future_premiere(tmp_path):
    anime = LibraryAnime(30, "Upcoming TV", status="PLANNING", format="TV")
    svc, mgr = service(tmp_path, anime)
    svc.tick(now=2_000_000_000)
    assert _state(svc, 30)["phase"] == "waiting"
    mgr.db.anime[30] = replace(anime, next_airing_episode=1, next_airing_at=2_100_000_000)
    assert svc.tick(now=2_000_000_100) == {"armed": 1, "searched": 0, "found": 0}
    assert _state(svc, 30)["next_check"] == 2_100_000_000 + 15 * 60


def test_entry_first_seen_already_released_stays_ignored(tmp_path):
    # Pudge learns about the movie only after its premiere (even though it was added
    # to the AniList list earlier): grandfathered, and never reviewed later.
    movie = _movie(list_added_at=ADDED_BEFORE)
    tv = LibraryAnime(31, "Old TV", status="PLANNING", format="TV", start_date="2026-01-10")
    svc, mgr = service(tmp_path, movie, tv)
    for now in (NOW, NOW + 60, NOW + 2 * 86400):
        assert svc.tick(now=now) == {"armed": 0, "searched": 0, "found": 0}
    assert _state(svc) == {"phase": "ignored", "seen_released_at": NOW}
    assert _state(svc, 31) == {"phase": "ignored", "seen_released_at": NOW}
    mgr.search_releases.assert_not_called()
    assert not discovery_work_due(mgr.db, now=NOW + 3 * 86400)


def test_undated_entry_already_out_keeps_waiting_for_a_future_date(tmp_path):
    anime = LibraryAnime(32, "Out, undated", status="PLANNING", media_status="FINISHED")
    svc, mgr = service(tmp_path, anime)
    svc.tick(now=NOW)
    assert mgr.db.get_state(svc._key(32)) == ""
    mgr.db.anime[32] = replace(anime, start_date="2026-01-10")
    svc.tick(now=NOW)
    assert _state(svc, 32)["phase"] == "ignored"
    mgr.search_releases.assert_not_called()


def test_placeholder_row_of_a_title_added_after_its_premiere_is_not_armed(tmp_path):
    # A row created locally before the AniList sync (no date yet) must not make an
    # old title look "seen unreleased": the list add time says it was out already.
    anime = LibraryAnime(33, "AniList #33", status="PLANNING")
    svc, mgr = service(tmp_path, anime)
    svc.tick(now=NOW)
    assert _state(svc, 33)["phase"] == "waiting"
    mgr.db.anime[33] = replace(anime, start_date="2026-01-10", media_status="FINISHED", list_added_at=NOW)
    assert svc.tick(now=NOW + 60)["armed"] == 0
    assert _state(svc, 33)["phase"] == "ignored"
    mgr.search_releases.assert_not_called()


# ----------------------------------------------------- B. old "ignored" movies, reviewed


def _legacy(tmp_path, *anime):
    svc, mgr = service(tmp_path, *anime)
    for item in anime:
        svc._save(item.media_id, {"phase": "ignored"})  # what the old code wrote
    return svc, mgr


def _reviews(mgr) -> int:
    return sum("ignored_movie_review" in str(call.args[0]) for call in mgr.logger.info.call_args_list)


def test_ignored_movie_added_before_its_premiere_is_armed_once_and_searched(tmp_path):
    svc, mgr = _legacy(tmp_path, _movie())
    assert discovery_work_due(mgr.db, now=NOW)
    assert svc.tick(now=NOW) == {"armed": 1, "searched": 1, "found": 0}
    assert mgr.search_releases.call_args.args == (MADOKA_ID,)
    assert mgr.search_releases.call_args.kwargs["batch"] is True
    state = _state(svc)
    assert state["phase"] == "armed" and state["airing_at"] == PREMIERE
    assert state["next_check"] == NOW + 24 * 3600
    assert svc.tick(now=NOW + 60) == {"armed": 0, "searched": 0, "found": 0}
    assert _reviews(mgr) == 1
    assert not discovery_work_due(mgr.db, now=NOW + 60)
    # Found on a later day -> an offer card.
    mgr.search_releases.return_value = [release()]
    assert svc.tick(now=NOW + 24 * 3600)["found"] == 1
    assert [row["media_id"] for row in svc.offers()] == [MADOKA_ID]


def test_ignored_movie_added_after_its_premiere_stays_ignored_and_is_not_rechecked(tmp_path):
    svc, mgr = _legacy(tmp_path, _movie(list_added_at=ADDED_AFTER))
    assert svc.tick(now=NOW)["armed"] == 0
    assert _state(svc) == {"phase": "ignored", "reviewed_at": NOW}
    assert not discovery_work_due(mgr.db, now=NOW + 60)
    svc.tick(now=NOW + 60)
    assert _state(svc) == {"phase": "ignored", "reviewed_at": NOW}
    assert _reviews(mgr) == 1
    mgr.search_releases.assert_not_called()


def test_ignored_movie_with_a_premiere_200_days_ago_stays_ignored(tmp_path):
    old = _movie(start_date="2026-03-28", list_added_at=_ts(2026, 1, 5))
    svc, mgr = _legacy(tmp_path, old)
    svc.tick(now=NOW)
    assert _state(svc) == {"phase": "ignored", "reviewed_at": NOW}
    mgr.search_releases.assert_not_called()


def test_ignored_tv_entry_and_dismissed_movie_are_untouched(tmp_path):
    tv = _movie(40, format="TV", episodes=12)
    dismissed = _movie(41)
    svc, mgr = _legacy(tmp_path, tv, dismissed)
    svc.dismiss(41)
    assert not discovery_work_due(mgr.db, now=NOW)
    assert svc.tick(now=NOW) == {"armed": 0, "searched": 0, "found": 0}
    assert _state(svc, 40) == {"phase": "ignored"}
    assert _state(svc, 41) == {"phase": "ignored"}
    mgr.search_releases.assert_not_called()


def test_ignored_movie_with_unknown_add_time_is_not_decided_until_a_sync_fills_it(tmp_path):
    svc, mgr = _legacy(tmp_path, _movie(list_added_at=0.0))
    assert not discovery_work_due(mgr.db, now=NOW)
    for now in (NOW, NOW + 3600):
        assert svc.tick(now=now)["armed"] == 0
        assert _state(svc) == {"phase": "ignored"}  # undecided, not "reviewed"
    # The next AniList sync stores createdAt.
    mgr.db.anime[MADOKA_ID] = _movie(list_added_at=ADDED_BEFORE)
    assert discovery_work_due(mgr.db, now=NOW + 7200)
    assert svc.tick(now=NOW + 7200) == {"armed": 1, "searched": 1, "found": 0}


def test_armed_old_movie_is_searched_once_a_day(tmp_path):
    svc, mgr = service(tmp_path, _movie())
    svc._save(
        MADOKA_ID,
        {
            "phase": "armed",
            "airing_at": PREMIERE,
            "airing_source": "date_fallback",
            "next_check": NOW,
            "attempts": 6,
            "search_revision": _SEARCH_REVISION,
        },
    )
    current = NOW
    for _ in range(3):
        assert svc.tick(now=current)["searched"] == 1
        assert _state(svc)["next_check"] == current + 86400
        assert svc.tick(now=current + 86399)["searched"] == 0
        current += 86400
    assert mgr.search_releases.call_count == 3
    # A fresh premiere still uses the short backoff.
    fresh = _movie(50, start_date=None, next_airing_episode=1, next_airing_at=int(NOW - 3600))
    svc, mgr = service(tmp_path, fresh)
    svc.tick(now=NOW)
    assert _state(svc, 50)["next_check"] == NOW + 15 * 60


def test_anilist_list_queries_fetch_created_at(monkeypatch):
    client = AniListClient("https://example.invalid", access_token="x")
    monkeypatch.setattr(client, "viewer", lambda: {"id": 7, "name": "tester"})
    queries = []

    def fake_post(query, variables=None):
        queries.append(query)
        if "MediaListCollection" not in query:
            media = {"id": MADOKA_ID, "title": {"romaji": MADOKA_TITLE}, "format": "MOVIE"}
            media["mediaListEntry"] = {"status": "PLANNING", "progress": 0, "createdAt": 1780272000}
            return {"Media": media}
        entry = {
            "progress": 0,
            "status": "PLANNING",
            "createdAt": 1780272000,
            "media": {
                "id": MADOKA_ID,
                "title": {"romaji": MADOKA_TITLE},
                "format": "MOVIE",
                "startDate": {"year": 2026, "month": 8, "day": 28},
            },
        }
        return {"MediaListCollection": {"lists": [{"status": "PLANNING", "entries": [entry]}]}}

    monkeypatch.setattr(client, "_post", fake_post)
    try:
        full = client.library()
        compact = client.library_compact()
        single = client.library_anime(MADOKA_ID)
    finally:
        client.close()
    assert len(queries) == 3 and all("createdAt" in query for query in queries)
    assert full[0].list_added_at == compact[0].list_added_at == single.list_added_at == 1780272000.0


def test_list_added_at_is_stored_and_not_overwritten_by_zero(tmp_path):
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    db.upsert_anime(_movie(list_added_at=ADDED_BEFORE))
    assert db.get_anime(MADOKA_ID).list_added_at == ADDED_BEFORE
    # Rows built from relation graphs or local files do not carry the add time.
    db.upsert_anime(_movie(list_added_at=0.0, title="Renamed"))
    assert db.get_anime(MADOKA_ID).title == "Renamed"
    assert db.anime_list(("PLANNING",))[0].list_added_at == ADDED_BEFORE
    db.upsert_anime(_movie(list_added_at=ADDED_AFTER))  # re-added to the list
    assert db.get_anime(MADOKA_ID).list_added_at == ADDED_AFTER

    # A database from before the column: the agent reads it unmigrated, the app adds it.
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE anime DROP COLUMN list_added_at")
    conn.commit()
    conn.close()
    assert Database(path, initialize=False).get_anime(MADOKA_ID).list_added_at == 0.0
    migrated = Database(path)
    assert migrated.get_anime(MADOKA_ID).list_added_at == 0.0
    migrated.upsert_anime(_movie(list_added_at=ADDED_BEFORE))
    assert migrated.get_anime(MADOKA_ID).list_added_at == ADDED_BEFORE


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = _config(tmp_path)
    cfg.nyaa.enabled = True
    return AnimeManager(cfg, log=lambda _message: None)


def _release(title: str, info_hash: str, *, score: float = 150.0) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://releases.invalid/view/{info_hash[:4]}",
        torrent_url=f"magnet:?xt=urn:btih:{info_hash}",
        info_hash=info_hash,
        size_text="2.0 GiB",
        size_bytes=2 * 1024**3,
        seeders=40,
        leechers=2,
        downloads=100,
        trusted=True,
        remake=False,
        group="",
        score=score,
        reasons=["title=100"],
    )


def test_madoka_after_the_update_is_searched_and_a_camrip_is_not_an_offer(tmp_path, monkeypatch, caplog):
    manager = _manager(tmp_path)
    db = manager.db
    discovery = PlannedReleaseDiscovery(manager)
    # The update: the row has no add time yet, the old state says "ignored".
    db.upsert_anime(_movie(list_added_at=0.0))
    discovery._save(MADOKA_ID, {"phase": "ignored"})
    assert not discovery_work_due(db, now=NOW)
    assert discovery.tick(now=NOW) == {"armed": 0, "searched": 0, "found": 0}
    assert discovery._state(MADOKA_ID) == {"phase": "ignored"}

    # The next AniList sync stores createdAt (added in June, premiere in August).
    db.upsert_anime(_movie(list_added_at=ADDED_BEFORE))
    assert discovery_work_due(db, now=NOW + 300)
    sources = []

    def source(name):
        def search(*_args, **_kwargs):
            sources.append(name)
            return [_release(CAMRIP, "c" * 40)]

        return search

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", source("rss"))
    monkeypatch.setattr("pudge.manager.search_ranked", source("nyaa"))
    monkeypatch.setattr("pudge.manager.search_shana_ranked", source("shana"))
    _no_requests(monkeypatch)
    with _info(caplog):
        assert discovery.tick(now=NOW + 300) == {"armed": 1, "searched": 1, "found": 0}
    # Every source was asked: a camrip is not a suitable release.
    assert sources == ["rss", "nyaa", "shana"]
    assert _logged(caplog, "reason=camrip dropped=1") == 1
    state = discovery._state(MADOKA_ID)
    assert state["phase"] == "armed" and state["next_check"] == NOW + 300 + 86400
    assert discovery.offers() == []
    assert not discovery_work_due(db, now=NOW + 3600)
    assert discovery_work_due(db, now=NOW + 300 + 86400)


# ------------------------------------------------------------------------ C. camrips


@pytest.mark.parametrize(
    "title",
    [
        CAMRIP,
        "[Group] Some Movie (2026) CAM 1080p",
        "Some Movie Cam-Rip x264",
        "Some Movie CAM RIP",
        "Some.Movie.2026.CAMRip.720p.mkv",
        "Some Movie HDCAM",
        "Some Movie HD-CAM",
        "Some Movie TELESYNC",
        "Some Movie HDTS",
        "Some Movie HD-TS",
        "Some Movie TELECINE",
        "Some Movie HDTC",
        "[Group] Some Movie [TS]",
        "[Group] Some Movie (tc) [1080p]",
    ],
)
def test_camrip_titles_are_detected(title):
    assert is_camrip_release(title)


@pytest.mark.parametrize(
    "title",
    [
        "[SubsPlease] Dorohedoro - 01 (1080p).mkv",
        "[SubsPlease] Show - 01 (1080p).mkv",
        "Toaru Kagaku no Railgun TS - 03 [1080p]",
        "[Erai-raws] Tsurune - 01 [1080p].mkv",
        "[Group] Tsubasa Chronicle - 01",
        "Show - 01.ts",
        "Show - 01 HD.ts",
        "[Group] Yuru Camp - 01 [1080p]",
        "Laid-Back Camp Movie [BD 1080p]",
        "[TV] Show - 01 (AT-X 1440x1080 MPEG2 TS)",
        "cats",
        "Webcam Diaries - 02",
    ],
)
def test_ordinary_titles_are_not_camrips(title):
    assert not is_camrip_release(title)


def test_release_search_drops_camrips_unless_allowed(tmp_path, monkeypatch, caplog):
    manager = _manager(tmp_path)
    manager.db.upsert_anime(_movie())
    good = _release(f"[Group] {MADOKA_TITLE} [BD 1080p]", "a" * 40)
    cam = _release(CAMRIP, "c" * 40, score=160.0)
    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", lambda *a, **k: [cam, good])
    monkeypatch.setattr("pudge.manager.search_ranked", lambda *a, **k: [])
    monkeypatch.setattr("pudge.manager.search_shana_ranked", lambda *a, **k: [])
    _no_requests(monkeypatch)
    assert manager.config.nyaa.allow_camrips is False
    for automatic in (False, True):
        with _info(caplog):
            caplog.clear()
            found = manager.search_releases(MADOKA_ID, batch=True, automatic=automatic)
        assert [item.title for item in found] == [good.title]
        assert _logged(caplog, "SKIP step=release.search") == 1
        assert _logged(caplog, "reason=camrip dropped=1") == 1

    manager.config.nyaa.allow_camrips = True
    found = manager.search_releases(MADOKA_ID, batch=True)
    assert [item.title for item in found] == [cam.title, good.title]


def test_camrip_is_not_allowed_for_automatic_downloads_when_off(tmp_path):
    manager = _manager(tmp_path)
    cam = _release(CAMRIP, "c" * 40)
    cam.group = "Erai-raws"
    good = replace(cam, title=f"[Erai-raws] {MADOKA_TITLE} [1080p]")
    assert manager._release_is_allowed_for_auto(good) is True
    assert manager._release_is_allowed_for_auto(cam) is False
    manager.config.nyaa.allow_camrips = True
    assert manager._release_is_allowed_for_auto(cam) is True


def test_allow_camrips_defaults_off_and_round_trips_config(tmp_path):
    cfg = AppConfig()
    assert cfg.nyaa.allow_camrips is False
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    assert "allow_camrips = false" in path.read_text(encoding="utf-8")
    assert load_config(path).nyaa.allow_camrips is False
    cfg.nyaa.allow_camrips = True
    write_config(cfg, path)
    assert load_config(path).nyaa.allow_camrips is True


CAMRIP_TOGGLE = """
const box = $('s_allow_camrips');
const label = box.closest('label');
const hint = label.nextElementSibling.nextElementSibling;
const out = {
  label: label.innerText.trim(), expected: t('settings.allowCamrips'),
  hint: hint.innerText.trim(), expectedHint: t('settings.allowCamripsHint'),
  checked: box.checked, inNyaaBlock: !!box.closest('#settings-nyaa-fields'),
};
await __input('#s_allow_camrips', true);
await __click('#saveSettings');
await __tick(10);
return out;
"""


def test_allow_camrips_toggle_is_shown_and_saved_by_web_settings(tmp_path, page):
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    settings = json.loads(json.dumps(api._settings_payload(), default=str))
    assert settings["allow_camrips"] is False
    assert en("settings.allowCamrips") == "Allow cinema recordings (CAM, TS)"
    assert ru("settings.allowCamrips") == "Разрешить записи из кинотеатра (CAM, TS)"

    for lang in ("en", "ru"):
        result = page.run(
            CAMRIP_TOGGLE,
            lang=lang,
            state={"settings": {**settings, "language": lang}},
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        assert result.value["label"] == result.value["expected"]
        assert result.value["hint"] == result.value["expectedHint"]
        assert result.value["checked"] is False and result.value["inNyaaBlock"] is True
        [[values]] = result.calls_of("save_settings")
        assert values["allow_camrips"] is True
    saved = api.save_settings(values)
    assert saved["settings"]["allow_camrips"] is True
    assert api.config.nyaa.allow_camrips is True
    assert load_config(cfg.config_path).nyaa.allow_camrips is True
