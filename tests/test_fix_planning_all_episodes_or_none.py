"""Planning downloads a season's episodes all together or not at all.

User: "Given downloaded badly. In general I don't want to download episodes without the first
one", then: "probably better not to download the anime if not all episodes were found: it is
bad when 3 random episodes download and have to be deleted". The season pack (25.7 GiB) did not
fit next to the unfinished downloads, the pack step counted that as "no pack", and single
episodes followed: old torrents with a few seeders, episode 1 not found at all.

Now a pack refused for disk space stops the search with that reason. Without a pack every
missing episode is searched first; they are started only when each one was found and fits on
the disk, and one that does not start removes (with its files) what this search added.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pudge.manager import ManagerError, NotEnoughDiskSpaceError
from test_fix_season_search_robustness import _planning_api, _season_2


class _Manager:
    def __init__(
        self,
        batch: object = None,
        found: set[int] = frozenset(),
        starts: set[int] | None = None,
        space: Exception | None = None,
    ) -> None:
        self.batch, self.found, self.space = batch, set(found), space
        self.starts = set(found) if starts is None else set(starts)
        self.calls: list[tuple[str, object]] = []
        self.added: list[SimpleNamespace] = []
        self.db = SimpleNamespace(downloads=lambda: list(self.added))

    @staticmethod
    def downloads_enabled() -> bool:
        return False

    @staticmethod
    def scan_library() -> list[object]:
        return []

    def search_and_add_best(self, _media_id: int, **kwargs):
        self.calls.append(("pack", None))
        if isinstance(self.batch, Exception):
            raise self.batch
        return self.batch

    def release_candidates(self, _media_id: int, **kwargs):
        episode = kwargs["episode"]
        self.calls.append(("search", episode))
        if episode not in self.found:
            return []
        return [SimpleNamespace(title=f"[Grp] Show - {episode:02d}", size_bytes=500 * 2**20)]

    def add_release_candidates(self, media_id: int, eligible, **kwargs):
        episode = kwargs["episode"]
        self.calls.append(("add", episode))
        if episode not in self.starts:
            return None
        self.added.append(SimpleNamespace(torrent_hash=f"h{episode}", media_id=media_id, episode=episode))
        return eligible[0]

    def _ensure_download_space(self, size: int) -> None:
        self.calls.append(("space", size))
        if self.space is not None:
            raise self.space


def _run(manager: _Manager, local: list[int], total: int = 6):
    api = _planning_api([])
    api._planning_local_episodes = lambda *_a, **_k: list(local)
    removed: list[tuple[str, str, bool]] = []
    api.torrent_download_action = lambda value, action, delete_files=False, backend="": removed.append(
        (value, action, delete_files)
    )
    api.logger.info = lambda *_a, **_k: None
    api._run_planning_episode_download(manager, _season_2(), total)
    return api.planning_episode_download_status(), removed


def _statuses(state: dict) -> list[tuple[object, str]]:
    return [(row["episode"], row["status"]) for row in state["episodes"]]


def test_a_pack_refused_for_disk_space_stops_instead_of_single_episodes() -> None:
    error = NotEnoughDiskSpaceError("Not enough free disk space for this download (25.7 GB): ...")
    manager = _Manager(batch=error, found={1, 2, 3, 4, 5, 6})
    state, _removed = _run(manager, local=[])
    assert manager.calls == [("pack", None)]  # no episode searches
    assert state["status"] == "failed" and _statuses(state) == [(None, "error")]
    assert (
        "25.7 GB" in state["episodes"][0]["error"]
        and "not downloaded instead" in state["episodes"][0]["error"]
    )
    assert issubclass(NotEnoughDiskSpaceError, ManagerError)


def test_one_missing_episode_downloads_nothing() -> None:
    manager = _Manager(found={1, 2, 4, 5, 6})
    state, removed = _run(manager, local=[])
    assert manager.calls == [
        ("pack", None),
        ("search", 1),
        ("search", 2),
        ("search", 3),
    ]  # stops at 3, adds nothing
    assert state["status"] == "failed"
    assert _statuses(state) == [(1, "skipped"), (2, "skipped"), (3, "missing"), (None, "error")]
    assert "Episode 3 was not found" in state["episodes"][-1]["error"] and removed == []


def test_all_found_are_searched_first_then_started() -> None:
    manager = _Manager(found={3, 4, 5, 6})
    state, removed = _run(manager, local=[1, 2])
    searches = [("search", n) for n in (3, 4, 5, 6)]
    adds = [("add", n) for n in (3, 4, 5, 6)]
    assert manager.calls == [("pack", None), *searches, ("space", 4 * 500 * 2**20), *adds]
    assert state["status"] == "done" and removed == []
    assert _statuses(state) == [
        (1, "local"),
        (2, "local"),
        (3, "added"),
        (4, "added"),
        (5, "added"),
        (6, "added"),
    ]


def test_an_episode_that_does_not_start_removes_what_this_search_added() -> None:
    manager = _Manager(found={1, 2, 3, 4}, starts={1, 2, 4})
    manager.added.append(
        SimpleNamespace(torrent_hash="old", media_id=21733, episode=None)
    )  # not this search's
    state, removed = _run(manager, local=[], total=4)
    assert [call for call in manager.calls if call[0] == "add"] == [("add", 1), ("add", 2), ("add", 3)]
    assert sorted(removed) == [("h1", "remove", True), ("h2", "remove", True)]
    assert state["status"] == "failed"
    assert _statuses(state) == [(1, "skipped"), (2, "skipped"), (3, "error"), (4, "skipped"), (None, "error")]
    assert "Episode 3 did not start" in state["episodes"][-1]["error"]


def test_episodes_that_do_not_fit_together_are_not_started() -> None:
    manager = _Manager(
        found={1, 2, 3}, space=NotEnoughDiskSpaceError("Not enough free disk space (1.5 GB): ...")
    )
    state, removed = _run(manager, local=[], total=3)
    assert not [call for call in manager.calls if call[0] == "add"] and removed == []
    assert state["status"] == "failed" and "1.5 GB" in state["episodes"][-1]["error"]


def test_the_space_message_names_unfinished_downloads(tmp_path, monkeypatch) -> None:
    from pudge import disk_space
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager

    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    manager = AnimeManager(config, log=lambda _message: None)
    gib = 1024**3
    monkeypatch.setattr(disk_space, "volume_usage", lambda _path: (500 * gib, int(139.5 * gib)))
    monkeypatch.setattr(disk_space, "free_space_reserve", lambda *_a: int(46.3 * gib))
    monkeypatch.setattr(manager, "_storage_usage", lambda: (0, int(78.7 * gib), []))
    monkeypatch.setattr(manager, "_admitted_bytes", lambda _known: 0)
    with pytest.raises(NotEnoughDiskSpaceError) as error:
        manager._ensure_download_space(int(25.7 * gib))
    message = str(error.value)
    assert (
        "(25.7 GB)" in message
        and "139.5 GB free" in message
        and "78.7 GB" in message
        and "46.3 GB" in message
    )


def test_search_and_add_best_is_candidates_then_add(monkeypatch) -> None:
    from pudge.manager import AnimeManager

    manager = object.__new__(AnimeManager)
    seen = []
    monkeypatch.setattr(manager, "release_candidates", lambda media_id, **kw: seen.append(("c", kw)) or ["r"])
    monkeypatch.setattr(
        manager,
        "add_release_candidates",
        lambda media_id, eligible, **kw: seen.append(("a", eligible)) or "added",
    )
    assert manager.search_and_add_best(1, episode=2, batch=False) == "added"
    assert seen[0][1]["episode"] == 2 and seen[1] == ("a", ["r"])
    monkeypatch.setattr(manager, "release_candidates", lambda media_id, **kw: [])
    assert manager.search_and_add_best(1, episode=2, batch=False) is None
