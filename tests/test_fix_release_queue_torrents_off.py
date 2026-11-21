"""Download while Torrents are off: the chosen release is queued, not refused.

With "Torrents: off" the release list's Download answered "Torrent downloads are disabled
in Settings". Now the choice is kept (web_app.add_release -> manager.queue_release, one
entry per title/episode in the shared state table, so it survives restarts and the
background agent sees it), shown in Downloads and on the Home card as "Waiting for
torrents to be turned on", cancellable, and added through the normal add path
(manager.add_release, manual=True) by the first pass after Torrents are on
(auto_search_current, which the On toggle runs, drains it before its own search).
The automatic downloader already defers (a "waiting" download intent, searched again on
On); its picks are listed the same way while Torrents are off."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.queued_releases import QueuedReleaseStore
from pudge.web_app import WebAppApi


def _release(title: str = "[Group] Show - 03 (1080p).mkv", info_hash: str = "a" * 40) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://example.invalid/view/1",
        torrent_url="https://example.invalid/download/1.torrent",
        info_hash=info_hash,
        size_text="1.2 GiB",
        size_bytes=1_300_000_000,
        seeders=40,
        leechers=2,
        downloads=100,
        trusted=True,
        remake=False,
    )


def _api(tmp_path: Path, *, torrents_on: bool = False) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.aria2.enabled = True
    cfg.nyaa.torrents_enabled = torrents_on
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=77, title="Queued Show", status="CURRENT", episodes=12, media_status="FINISHED")
    )
    return api


def _turn_on(api: WebAppApi) -> None:
    """Torrents on as the other process sees it (config + shared admission)."""
    api.config.nyaa.torrents_enabled = True
    api.manager.config.nyaa.torrents_enabled = True
    api._torrent_session_enabled = True
    shared = api._torrent_shared_admission()
    with shared.locked():
        shared.publish(True)


def test_download_while_torrents_off_is_queued_not_refused(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    added: list[tuple] = []
    monkeypatch.setattr(api.manager, "add_release", lambda *a, **k: added.append((a, k)) or True)

    result = api.add_release(77, 3, False, asdict(_release()))

    assert result["ok"] is True and result["queued"] is True and result["already_queued"] is False
    assert added == []  # nothing reaches the torrent client while Off
    queued = api.torrent_downloads(None, refresh=False)["queued"]
    assert [(row["media_id"], row["episode"], row["batch"], row["origin"]) for row in queued] == [
        (77, 3, False, "manual")
    ]
    assert queued[0]["anime_title"] == "Queued Show" and queued[0]["cancellable"] is True
    assert api.get_state()["queued_releases"][0]["id"] == queued[0]["id"]


def test_same_choice_again_is_not_a_duplicate_and_new_choice_replaces(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    again = api.add_release(77, 3, False, asdict(_release()))
    assert again["queued"] is True and again["already_queued"] is True
    other = api.add_release(77, 3, False, asdict(_release("[Other] Show - 03.mkv", "b" * 40)))
    assert other["already_queued"] is False
    rows = api.torrent_downloads(None, refresh=False)["queued"]
    assert len(rows) == 1 and rows[0]["title"] == "[Other] Show - 03.mkv"
    # A whole-title pick is its own entry.
    api.add_release(77, None, True, asdict(_release("[Group] Show (01-12) [Batch]", "c" * 40)))
    assert len(api.torrent_downloads(None, refresh=False)["queued"]) == 2


def test_queue_survives_a_restart(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    restarted = WebAppApi(api.config.config_path)
    rows = restarted.torrent_downloads(None, refresh=False)["queued"]
    assert [row["title"] for row in rows] == ["[Group] Show - 03 (1080p).mkv"]


def test_cancel_removes_the_queued_release(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    entry_id = api.torrent_downloads(None, refresh=False)["queued"][0]["id"]
    result = api.cancel_queued_release(entry_id)
    assert result["removed"] is True and result["queued"] == []
    assert api.cancel_queued_release(entry_id)["removed"] is False


def test_turning_torrents_on_adds_the_queued_release_through_the_normal_path(
    tmp_path: Path, monkeypatch
) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    calls: list[tuple] = []

    def add_release(media_id, release, *, episode, batch, manual=False, upgrade=False):
        calls.append((media_id, release.title, release.info_hash, episode, batch, manual))
        return True

    monkeypatch.setattr(api.manager, "add_release", add_release)
    # Off: the periodic pass leaves it alone.
    assert api.manager.start_queued_releases() == 0
    assert calls == []

    _turn_on(api)
    monkeypatch.setattr(api.manager, "ensure_torrent_backend_ready", lambda: "aria2", raising=False)
    monkeypatch.setattr(api, "_resume_incomplete_torrent_jobs", lambda: 0)
    api._torrent_toggle_auto_search()

    assert calls == [(77, "[Group] Show - 03 (1080p).mkv", "a" * 40, 3, False, True)]
    assert api.torrent_downloads(None, refresh=False)["queued"] == []
    # Started once: the next pass adds nothing again.
    assert api.manager.start_queued_releases() == 0 and len(calls) == 1


def test_with_torrents_on_download_goes_straight_to_the_client(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path, torrents_on=True)
    _turn_on(api)
    added: list[int] = []
    monkeypatch.setattr(api.manager, "add_release", lambda media_id, *a, **k: added.append(media_id) or True)
    result = api.add_release(77, 3, False, asdict(_release()))
    assert added == [77] and not result.get("queued")


def test_failed_start_keeps_the_entry_with_its_error_and_gives_up_later(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    _turn_on(api)

    def broken(*_a, **_k):
        raise RuntimeError("aria2 is not answering")

    monkeypatch.setattr(api.manager, "add_release", broken)
    for _ in range(QueuedReleaseStore.MAX_ATTEMPTS + 2):
        assert api.manager.start_queued_releases() == 0
    [row] = api.torrent_downloads(None, refresh=False)["queued"]
    assert "aria2 is not answering" in row["error"]
    entry = api.manager.queued_releases.list()[0]
    assert entry["attempts"] == QueuedReleaseStore.MAX_ATTEMPTS and entry["state"] == "queued"


def test_off_again_during_the_start_keeps_everything_queued(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    api.add_release(77, 4, False, asdict(_release("[Group] Show - 04.mkv", "d" * 40)))
    _turn_on(api)

    def off_meanwhile(*_a, **_k):
        shared = api._torrent_shared_admission()
        with shared.locked():
            shared.publish(False)
        api.manager.config.nyaa.torrents_enabled = False
        raise RuntimeError("Torrent traffic is off")

    monkeypatch.setattr(api.manager, "add_release", off_meanwhile)
    assert api.manager.start_queued_releases() == 0
    entries = api.manager.queued_releases.list()
    assert [(row["state"], row["attempts"], row["error"]) for row in entries] == [("queued", 0, "")] * 2


def test_two_processes_never_start_the_same_entry(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    first = QueuedReleaseStore(api.manager.db).claim(now=1000.0)
    second = QueuedReleaseStore(api.manager.db).claim(now=1001.0)
    assert len(first) == 1 and second == []
    # A claim of a process that died is taken over later.
    assert len(QueuedReleaseStore(api.manager.db).claim(now=1000.0 + 601)) == 1


def test_already_downloading_release_is_not_queued(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    from types import SimpleNamespace

    monkeypatch.setattr(api.manager.db, "downloads", lambda: [SimpleNamespace(torrent_hash="A" * 40)])
    result = api.add_release(77, 3, False, asdict(_release()))
    assert result["queued"] is False and result["already_downloading"] is True
    assert api.manager.queued_releases.list() == []


def test_home_card_says_waiting_for_torrents(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.add_release(77, None, True, asdict(_release("[Group] Show (01-12) [Batch]")))
    state = api.get_state()
    cards = [card for rows in state["home"].values() for card in rows if card.get("media_id") == 77]
    assert cards, state["home"]
    assert any(
        card.get("queued_release", {}).get("batch") is True
        and card["presentation"]["status"] == "queued_torrents_off"
        for card in cards
    )


def test_automatic_downloader_picks_are_listed_while_off_only(tmp_path: Path) -> None:
    api = _api(tmp_path)
    intents = api.manager.download_intents
    intent = intents.begin(77, 5, False, [_release("[Auto] Show - 05.mkv", "e" * 40)], backend="aria2")
    intents.update(
        77,
        5,
        False,
        state="waiting",
        selected=_release("[Auto] Show - 05.mkv", "e" * 40),
        backend="aria2",
        detail="Torrent traffic is off",
        expected_revision=int(intent["revision"]),
    )
    rows = api.torrent_downloads(None, refresh=False)["queued"]
    assert [(row["episode"], row["origin"], row["cancellable"]) for row in rows] == [(5, "auto", False)]
    assert rows[0]["anime_title"] == "Queued Show"
    # On: the automatic downloader searches again itself, nothing waits any more.
    _turn_on(api)
    assert api.torrent_downloads(None, refresh=False)["queued"] == []


@pytest.mark.parametrize("batch", [False, True])
def test_store_entry_ids(batch: bool) -> None:
    assert QueuedReleaseStore.entry_id(7, 3, batch) == ("7:batch" if batch else "7:episode:3")


# Review of the queue (fix-rel review): double starts, stale claims, exhausted entries.


def _row(media_id=77, episode=3, *, batch=False, torrent_hash="f" * 40):
    from types import SimpleNamespace

    return SimpleNamespace(
        media_id=media_id, episode=episode, media_episode=episode, is_batch=batch, torrent_hash=torrent_hash
    )


def test_direct_add_after_turning_on_drops_the_queued_release(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    _turn_on(api)
    monkeypatch.setattr(api.manager, "add_release", lambda *a, **k: True)
    api.add_release(77, 3, False, asdict(_release("[Other] Show - 03.mkv", "b" * 40)))
    assert api.manager.queued_releases.list() == []


@pytest.mark.parametrize("how", ["download", "file"])
def test_queued_release_for_an_episode_present_by_now_is_dropped(tmp_path: Path, monkeypatch, how) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    _turn_on(api)
    added: list[int] = []
    monkeypatch.setattr(api.manager, "add_release", lambda media_id, *a, **k: added.append(media_id) or True)
    if how == "download":
        monkeypatch.setattr(api.manager.db, "downloads", lambda: [_row()])
    else:
        monkeypatch.setattr(api.manager.db, "has_episode", lambda media_id, episode: episode == 3)
    assert api.manager.start_queued_releases() == 0
    assert added == [] and api.manager.queued_releases.list() == []


def test_entries_are_claimed_one_at_a_time(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    for episode in (3, 4, 5):
        api.add_release(77, episode, False, asdict(_release(f"Show - 0{episode}.mkv", str(episode) * 40)))
    _turn_on(api)
    starting_during_add: list[int] = []

    def add_release(*_a, **_k):
        rows = api.manager.queued_releases.list()
        starting_during_add.append(sum(1 for row in rows if row.get("state") == "starting"))
        return True

    monkeypatch.setattr(api.manager, "add_release", add_release)
    assert api.manager.start_queued_releases() == 3
    # Only the entry being added is claimed: the others stay free for the other process.
    assert starting_during_add == [1, 1, 1]
    assert QueuedReleaseStore(api.manager.db).claim(limit=1) == []


def test_exhausted_entries_say_so_and_get_their_tries_back_on_turning_on(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    _turn_on(api)

    def broken(*_a, **_k):
        raise RuntimeError("aria2 is not answering")

    monkeypatch.setattr(api.manager, "add_release", broken)
    for _ in range(QueuedReleaseStore.MAX_ATTEMPTS):
        api.manager.start_queued_releases()
    [row] = api.torrent_downloads(None, refresh=False)["queued"]
    assert row["exhausted"] is True and row["attempts"] == QueuedReleaseStore.MAX_ATTEMPTS

    added: list[int] = []
    monkeypatch.setattr(api.manager, "add_release", lambda media_id, *a, **k: added.append(media_id) or True)
    monkeypatch.setattr(api.manager, "ensure_torrent_backend_ready", lambda: "aria2", raising=False)
    monkeypatch.setattr(api, "_resume_incomplete_torrent_jobs", lambda: 0)
    api._torrent_toggle_auto_search()
    assert added == [77] and api.manager.queued_releases.list() == []


def test_requeueing_the_same_release_leaves_a_start_under_way_alone(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    store = api.manager.queued_releases
    [claimed] = store.claim(now=1000.0, limit=1)
    again = api.add_release(77, 3, False, asdict(_release()))
    assert again["already_queued"] is True
    [entry] = store.list()
    assert entry["state"] == "starting" and entry["claimed_at"] == 1000.0
    # Another release meanwhile: the claim stays the starting pass's own.
    api.add_release(77, 3, False, asdict(_release("[Other] Show - 03.mkv", "b" * 40)))
    [entry] = store.list()
    assert entry["state"] == "starting" and entry["claimed_at"] == 1000.0
    assert entry["release"]["title"] == "[Other] Show - 03.mkv"


def test_database_error_while_finishing_does_not_end_the_pass(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    api.add_release(77, 4, False, asdict(_release("Show - 04.mkv", "d" * 40)))
    _turn_on(api)
    added: list[int] = []
    monkeypatch.setattr(api.manager, "add_release", lambda media_id, *a, **k: added.append(media_id) or True)
    store = api.manager.queued_releases
    real_finish = store.finish
    failures = iter([True])

    def finish(entry):
        if next(failures, False):
            raise RuntimeError("database is locked")
        real_finish(entry)

    monkeypatch.setattr(store, "finish", finish)
    assert api.manager.start_queued_releases() == 2
    assert added == [77, 77]


def test_queued_release_of_a_removed_title_is_dropped(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    _turn_on(api)
    added: list[int] = []
    monkeypatch.setattr(api.manager, "add_release", lambda media_id, *a, **k: added.append(media_id) or True)
    monkeypatch.setattr(api.manager.db, "get_anime", lambda media_id: None)
    assert api.manager.start_queued_releases() == 0
    assert added == [] and api.manager.queued_releases.list() == []


def test_get_state_lists_the_queue_once(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.add_release(77, 3, False, asdict(_release()))
    calls: list[int] = []
    real = api._queued_release_payloads
    monkeypatch.setattr(api, "_queued_release_payloads", lambda *a: calls.append(1) or real(*a))
    state = api.get_state()
    assert len(calls) == 1 and len(state["queued_releases"]) == 1


def _waiting_intent(api, episode, batch=False):
    intents = api.manager.download_intents
    intents.update(
        77,
        episode,
        batch,
        state="waiting",
        selected=_release("[Auto] Show.mkv", "e" * 40),
        backend="aria2",
        detail="Torrent traffic is off",
    )


def test_automatic_rows_skip_bad_episode_numbers_and_complete_titles(tmp_path: Path, monkeypatch) -> None:
    import json as _json

    api = _api(tmp_path)
    db = api.manager.db
    db.set_state(
        "download_intent:77:episode:x",
        _json.dumps(
            {"media_id": 77, "episode": "x", "batch": False, "state": "waiting", "selected_title": "Bad"}
        ),
    )
    _waiting_intent(api, None, batch=True)
    assert [row["batch"] for row in api.torrent_downloads(None, refresh=False)["queued"]] == [True]
    monkeypatch.setattr(db, "has_episode", lambda media_id, episode: True)
    assert api.torrent_downloads(None, refresh=False)["queued"] == []
