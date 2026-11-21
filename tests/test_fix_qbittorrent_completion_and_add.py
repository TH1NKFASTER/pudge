"""qBittorrent: "99.9 %" is not complete, adds keep the feed's trackers and the save path.

- A 56 GB batch at 99.91 % (52 MiB left, still "downloading") counted as complete:
  sync registered it and the subtitle job was not postponed for its missing pieces.
- The add sent a magnet rebuilt from the info hash (no trackers): only DHT could find
  peers, while the SubsPlease feed's magnet lists its trackers.
- With qBittorrent's default torrent management set to Automatic, savepath was ignored
  and setCategory moved every torrent into the category's path, which was the first
  downloaded title's folder.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl

import httpx

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem
from pudge.providers.nyaa import parse_rss, parse_subsplease_rss
from pudge.providers.qbittorrent import QBittorrentClient

HASH = "0123456789abcdef0123456789abcdef01234567"


def _item(progress: float, state: str, **raw) -> DownloadItem:
    return DownloadItem(
        torrent_hash="ab" * 20,
        name="b",
        state=state,
        progress=progress,
        save_path="/x",
        content_path="/x/b",
        media_id=1,
        is_batch=True,
        raw={"backend": "qbittorrent", **raw},
    )


def test_qbittorrent_99_9_percent_is_not_complete(tmp_path: Path):
    assert not AnimeManager._download_is_complete(_item(0.9991, "downloading", amount_left=52 * 1024**2))
    assert AnimeManager._download_is_complete(_item(1.0, "stalledUP", amount_left=0))
    assert AnimeManager._download_is_complete(_item(0.9995, "uploading", amount_left=0))
    # Without the byte count only a settled state counts.
    assert not AnimeManager._download_is_complete(_item(0.9995, "downloading"))
    assert AnimeManager._download_is_complete(_item(0.9995, "pausedUP"))
    assert AnimeManager._download_is_complete(_item(1.0, "downloading"))

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    manager = AnimeManager(cfg, log=lambda _m: None)
    manager.db.upsert_download(_item(0.9991, "downloading", amount_left=52 * 1024**2))
    assert Path("/x/b").resolve() in manager.incomplete_download_paths()


class _Server:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = (
            dict(parse_qsl(request.content.decode()))
            if request.method == "POST"
            else dict(request.url.params)
        )
        self.calls.append((request.url.path, form))
        if request.url.path == "/api/v2/app/version":
            return httpx.Response(200, text="v4.6.7")
        if request.url.path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={})
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[])
        return httpx.Response(200, text="Ok.")

    def forms(self, suffix: str) -> list[dict[str, str]]:
        return [form for path, form in self.calls if path.endswith(suffix)]


def _client(server: _Server) -> QBittorrentClient:
    client = QBittorrentClient("http://qbt.local")
    client.client.close()
    client.client = httpx.Client(base_url="http://qbt.local", transport=httpx.MockTransport(server.handler))
    return client


SUBSPLEASE_RSS = (
    "<rss><channel><item><title>[SubsPlease] Show - 05 (1080p).mkv</title><link>magnet:?xt=urn:btih:"
    f"{HASH}&amp;dn=Show&amp;tr=udp%3A%2F%2Ftracker.example%3A1337&amp;tr=http%3A%2F%2Ftracker.example%2Fannounce"
    "</link><guid>g</guid><description>1.4 GiB</description></item></channel></rss>"
)


def test_add_keeps_feed_trackers_and_disables_automatic_management(tmp_path: Path):
    release = parse_subsplease_rss(SUBSPLEASE_RSS)[0]
    assert "tr=" not in release.magnet  # what was sent before
    server = _Server()
    client = _client(server)
    client.category_save_path = tmp_path / "library"
    try:
        client.add_release(
            release, save_path=tmp_path / "library" / "Show", category="pudge", tags=["pudge"], paused=False
        )
    finally:
        client.close()
    (add,) = server.forms("/torrents/add")
    assert add["urls"].count("&tr=") == 2 and HASH in add["urls"]
    assert add["autoTMM"] == "false"
    assert add["savepath"] == str(tmp_path / "library" / "Show")
    (category,) = server.forms("/torrents/createCategory")
    assert category["savePath"] == str(tmp_path / "library")


def test_nyaa_release_without_a_feed_magnet_still_adds_by_hash(tmp_path: Path):
    xml = (
        '<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item><title>[Grp] Show - 05</title>'
        "<link>https://nyaa.example/download/1.torrent</link><guid>https://nyaa.example/view/1</guid>"
        f"<nyaa:infoHash>{HASH}</nyaa:infoHash><nyaa:size>1.4 GiB</nyaa:size></item></channel></rss>"
    )
    release = parse_rss(xml)[0]
    server = _Server()
    client = _client(server)
    try:
        client.add_release(release, save_path=tmp_path / "Show", category="pudge", tags=[], paused=False)
    finally:
        client.close()
    (add,) = server.forms("/torrents/add")
    assert add["urls"] == release.magnet and add["autoTMM"] == "false"
    # No library root given (light novels, audiobooks): the category keeps the add's folder.
    assert server.forms("/torrents/createCategory")[0]["savePath"] == str(tmp_path / "Show")


def test_manager_client_points_the_category_at_the_library_root(tmp_path: Path):
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _m: None)
    client = manager.qbt_client()
    try:
        assert isinstance(client, QBittorrentClient)
        assert client.category_save_path == tmp_path / "library"
    finally:
        client.close()
