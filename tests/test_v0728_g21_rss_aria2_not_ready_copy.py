from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.manager import AnimeManager, ManagerError
from pudge.manager_models import LibraryAnime, NyaaRelease


ROOT = Path(__file__).resolve().parents[1]


def test_waiting_card_has_no_final_suffix_after_not_ready() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    start = html.index("function waitingHomeCard(a){")
    end = html.index("function downloadAvailableHomeCard(a){", start)
    block = html[start:end]
    assert "const state=episodePresentationStatus(a,episode);" in block
    assert "finalSuffix" not in block
    assert "is_final_episode" not in block


def test_sync_download_observation_failure_does_not_abort_maintenance() -> None:
    manager = object.__new__(AnimeManager)
    messages: list[str] = []
    warnings: list[tuple] = []
    manager.sync_downloads = lambda: (_ for _ in ()).throw(ManagerError("aria2: aria2 RPC не запущен"))
    manager.log = messages.append
    manager.logger = SimpleNamespace(
        warning=lambda *args, **kwargs: warnings.append(args),
        info=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    stats = {"downloads": 0}

    AnimeManager._sync_downloads_for_stats(manager, stats)

    assert stats["downloads"] == 0
    assert messages == ["aria2: aria2 RPC не запущен"]
    assert warnings and "continue=maintenance" in warnings[0][0]


class _Db:
    def __init__(self, anime: LibraryAnime) -> None:
        self.anime = anime
        self.recorded: list[tuple] = []

    def get_anime(self, media_id: int):
        return self.anime if media_id == self.anime.media_id else None

    def record_release(self, *args, **kwargs):
        self.recorded.append((args, kwargs))

    def upsert_download(self, _item):
        raise AssertionError("verification is disabled in this fake")


class _LazyAria:
    backend_name = "aria2"
    torrents = None

    def __init__(self) -> None:
        self.running = False
        self.calls: list[str] = []

    def ensure_running(self) -> None:
        self.calls.append("ensure")
        self.running = True

    def add_release(self, *_args, **_kwargs) -> str:
        assert self.running, "aria2 must be started before add"
        self.calls.append("add")
        return "gid"

    def close(self) -> None:
        self.calls.append("close")


def test_aria2_write_path_starts_sidecar_before_duplicate_probe(tmp_path: Path) -> None:
    anime = LibraryAnime(media_id=42, title="Example", status="CURRENT")
    client = _LazyAria()
    manager = object.__new__(AnimeManager)
    manager.config = SimpleNamespace(
        config_path=None,
        library=SimpleNamespace(root_dir=tmp_path / "library"),
        qbittorrent=SimpleNamespace(enabled=False, category="pudge", paused_on_add=False),
        aria2=SimpleNamespace(enabled=True, paused_on_add=False),
        nyaa=SimpleNamespace(torrents_enabled=True),
    )
    manager.db = _Db(anime)
    manager.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    manager.log = lambda *_args, **_kwargs: None
    manager.torrent_clients = lambda: []
    manager.qbt_client = lambda: client
    manager._existing_download_for_request = lambda *_args, **_kwargs: (
        None if client.running else (_ for _ in ()).throw(RuntimeError("read before start"))
    )

    release = NyaaRelease(
        title="[SubsPlease] Example - 12 (1080p).mkv",
        link="https://example.invalid/view/1",
        torrent_url="https://example.invalid/download/1.torrent",
        info_hash="a" * 40,
        size_text="1 GiB",
        size_bytes=1_000_000_000,
        seeders=0,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        category_id="subsplease-rss",
        score=250.0,
        group="SubsPlease",
    )

    assert AnimeManager.add_release(manager, 42, release, episode=12, batch=False) is True
    assert client.calls == ["ensure", "close", "add", "close"]
    assert manager.db.recorded


class _SharedAriaState:
    def __init__(self) -> None:
        self.running = False
        self.events: list[str] = []


class _SharedAriaClient:
    backend_name = "aria2"
    torrents = None

    def __init__(self, state: _SharedAriaState, role: str) -> None:
        self.state = state
        self.role = role

    def ensure_running(self) -> None:
        self.state.events.append(f"{self.role}:ensure")
        self.state.running = True

    def add_release(self, *_args, **_kwargs) -> str:
        assert self.state.running
        self.state.events.append(f"{self.role}:add")
        return "gid"

    def close(self) -> None:
        self.state.events.append(f"{self.role}:close")


def test_g22_aria2_is_ready_before_cross_backend_duplicate_probe(tmp_path: Path) -> None:
    anime = LibraryAnime(media_id=43, title="Example Two", status="CURRENT")
    state = _SharedAriaState()
    primary = _SharedAriaClient(state, "primary")
    probe = _SharedAriaClient(state, "probe")
    manager = object.__new__(AnimeManager)
    manager.config = SimpleNamespace(
        config_path=None,
        library=SimpleNamespace(root_dir=tmp_path / "library"),
        qbittorrent=SimpleNamespace(enabled=False, category="pudge", paused_on_add=False),
        aria2=SimpleNamespace(enabled=True, paused_on_add=False),
        nyaa=SimpleNamespace(torrents_enabled=True),
    )
    manager.db = _Db(anime)
    manager.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    manager.log = lambda *_args, **_kwargs: None
    manager.qbt_client = lambda: primary
    manager.torrent_clients = lambda: [("aria2", probe)]

    def duplicate_probe(client, *_args, **_kwargs):
        assert state.running, "duplicate probe ran before managed aria2 readiness"
        state.events.append(f"{client.role}:probe")
        return None

    manager._existing_download_for_request = duplicate_probe

    release = NyaaRelease(
        title="[SubsPlease] Example Two - 12 (1080p).mkv",
        link="https://example.invalid/view/2",
        torrent_url="https://example.invalid/download/2.torrent",
        info_hash="b" * 40,
        size_text="1 GiB",
        size_bytes=1_000_000_000,
        seeders=0,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        category_id="subsplease-rss",
        score=250.0,
        group="SubsPlease",
    )

    assert AnimeManager.add_release(manager, 43, release, episode=12, batch=False) is True
    assert state.events.index("primary:ensure") < state.events.index("probe:probe")
