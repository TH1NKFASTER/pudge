from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import _leecher_activity_bonus


ANIME = LibraryAnime(
    media_id=20755,
    title="Ansatsu Kyoushitsu",
    titles=["Assassination Classroom"],
    episodes=22,
    format="TV",
)


def release(name: str, score: float, info_hash: str) -> NyaaRelease:
    return NyaaRelease(
        title=name,
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash=info_hash,
        size_text="10 GiB",
        size_bytes=10 * 1024**3,
        seeders=30,
        leechers=5,
        downloads=100,
        trusted=True,
        remake=False,
        score=score,
        is_batch=True,
        group="Test",
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeQbt:
    def __init__(self, live: set[str]) -> None:
        self.live = {value.casefold() for value in live}
        self.added: list[str] = []
        self.deleted: list[str] = []
        self.locations: list[tuple[str, Path]] = []
        self.metadata: list[str] = []

    def torrent_status(self, torrent_hash: str):
        key = torrent_hash.casefold()
        if key not in {value.casefold() for value in self.added}:
            return None
        if key in self.live:
            return {"dlspeed": 1024, "downloaded": 1024 * 1024, "progress": 0.001}
        return {"dlspeed": 0, "downloaded": 0, "progress": 0.0}

    def add_release(self, item, *, save_path, category, tags, paused=False):
        self.added.append(item.info_hash)
        return item.info_hash

    def start(self, torrent_hash: str) -> None:
        pass

    def delete(self, torrent_hash: str, *, delete_files=True) -> None:
        self.deleted.append(torrent_hash)

    def set_location(self, torrent_hash: str, location: Path) -> None:
        self.locations.append((torrent_hash, location))

    def remove_tags(self, torrent_hash: str, tags) -> None:
        pass

    def set_metadata(self, torrent_hash: str, *, category: str, tags) -> None:
        self.metadata.append(torrent_hash)

    def delete_tags(self, tags) -> int:
        return len(tags)

    def torrents(self, *, category=""):
        return []

    def close(self) -> None:
        pass


class FakeDb:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    def get_anime(self, media_id: int):
        return ANIME if media_id == ANIME.media_id else None

    def record_release(self, info_hash, media_id, episode, title, score) -> None:
        self.recorded.append(info_hash)

    def upsert_download(self, item) -> None:
        pass


def manager(tmp_path: Path, qbt: FakeQbt) -> AnimeManager:
    obj = object.__new__(AnimeManager)
    cfg = AppConfig()
    cfg.qbittorrent.enabled = True
    cfg.qbittorrent.paused_on_add = False
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    obj.config = cfg
    obj.db = FakeDb()
    obj.log = lambda _message: None
    obj.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    obj.qbt_client = lambda: qbt
    return obj


def test_leecher_bonus_is_small_and_requires_seeders() -> None:
    assert _leecher_activity_bonus(100, 0) == 0.0
    assert 0 < _leecher_activity_bonus(5, 10) < 6.0
    assert _leecher_activity_bonus(1000, 10) <= 8.0


def test_race_fast_path_does_not_start_alternatives(tmp_path, monkeypatch) -> None:
    from pudge import manager as manager_module

    clock = Clock()
    monkeypatch.setattr(manager_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(manager_module.time, "sleep", clock.sleep)
    qbt = FakeQbt({"best"})
    m = manager(tmp_path, qbt)
    best = release("best", 300, "best")
    second = release("second", 250, "second")
    third = release("third", 200, "third")

    winner = m._race_add_candidates(
        ANIME.media_id,
        [best, second, third],
        episode=None,
        batch=True,
        fast_seconds=10,
        total_seconds=20,
        poll_seconds=2,
    )

    assert winner is best
    assert qbt.added == ["best"]
    assert qbt.deleted == []


def test_race_starts_alternatives_after_dead_top_one_and_keeps_best_live(
    tmp_path, monkeypatch
) -> None:
    from pudge import manager as manager_module

    clock = Clock()
    monkeypatch.setattr(manager_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(manager_module.time, "sleep", clock.sleep)
    qbt = FakeQbt({"second", "third"})
    m = manager(tmp_path, qbt)
    best = release("best", 300, "best")
    second = release("second", 250, "second")
    third = release("third", 200, "third")

    winner = m._race_add_candidates(
        ANIME.media_id,
        [best, second, third],
        episode=None,
        batch=True,
        fast_seconds=10,
        total_seconds=14,
        poll_seconds=2,
    )

    assert winner is second
    assert qbt.added == ["best", "second", "third"]
    assert set(qbt.deleted) == {"best", "third"}
    assert qbt.metadata == ["second"]
