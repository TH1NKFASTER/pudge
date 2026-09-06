from __future__ import annotations

from types import SimpleNamespace

from pudge.manager_models import NyaaRelease


def _release() -> NyaaRelease:
    return NyaaRelease(
        title="TMW Japanese Audiobooks Collection (Part 7) - Light Novels III",
        link="https://nyaa.invalid/view/7",
        torrent_url="https://nyaa.invalid/download/7.torrent",
        info_hash="b" * 40,
        size_text="121.8 GiB",
        size_bytes=121 * 1024**3,
        seeders=10,
        leechers=1,
        downloads=100,
        trusted=True,
        remake=False,
        category_id="2_2",
    )


def test_ln_audiobook_search_uses_audio_category_and_qbt_metadata_fallback(monkeypatch) -> None:
    import pudge.web_app as web_app

    seen_categories: list[str] = []
    fallback_calls: list[str] = []

    class FakeNyaaClient:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("category") == "2_0"

        def search(self, query: str, **kwargs):
            seen_categories.append(str(kwargs.get("category") or ""))
            if query == "TMW Japanese Audiobooks Collection":
                return [_release()]
            return []

        def fetch_torrent_payload(self, url: str) -> bytes:
            raise RuntimeError("direct torrent timeout")

        def close(self) -> None:
            pass

    class FakeQBittorrentClient:
        def __init__(self, *args, **kwargs):
            pass

        def inspect_release_files(self, release, *, save_path, metadata_timeout):
            fallback_calls.append(release.info_hash)
            return [
                {"index": 0, "name": "[支倉 凍砂]/狼と香辛料/[01] 狼と香辛料I.m4b", "size": 100},
                {"index": 1, "name": "[支倉 凍砂]/狼と香辛料/[02] 狼と香辛料II [B0C58KN4W9].m4b", "size": 200},
            ]

        def close(self) -> None:
            pass

    class FakeLightNovels:
        def book(self, book_id: int):
            return {"id": book_id, "title": "狼と香辛料II", "series_title": "狼と香辛料", "volume": 2}

        @staticmethod
        def _nyaa_title(value: str) -> str:
            return value

        @staticmethod
        def _release_volume_match(title: str, target: int):
            return False, None, False

    monkeypatch.setattr(web_app, "NyaaClient", FakeNyaaClient)
    monkeypatch.setattr(web_app, "QBittorrentClient", FakeQBittorrentClient)

    api = object.__new__(web_app.WebAppApi)
    api.light_novels = FakeLightNovels()
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(
            base_url="https://nyaa.invalid",
            proxy_mode="direct",
            proxy_url="",
            pre_search_command="",
        ),
        qbittorrent=SimpleNamespace(
            enabled=True,
            base_url="http://127.0.0.1:8080",
            username="",
            password="",
            api_key="",
            verify_tls=True,
            pre_download_command="",
            auto_start_app=False,
        ),
    )
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)

    result = api.light_novel_search_audiobook_nyaa(180)
    assert seen_categories
    assert set(seen_categories) == {"2_0"}
    assert fallback_calls == ["b" * 40]
    assert len(result["matches"]) == 1
    assert result["matches"][0]["metadata_source"] == "magnet"
    assert result["matches"][0]["selected_files"] == [
        "[支倉 凍砂]/狼と香辛料/[02] 狼と香辛料II [B0C58KN4W9].m4b"
    ]
    assert result["category"] == "2_0"


def test_qbt_metadata_inspection_does_not_modify_existing_torrent(monkeypatch, tmp_path) -> None:
    from pudge.providers.qbittorrent import QBittorrentClient

    qbt = object.__new__(QBittorrentClient)
    qbt.login = lambda: None
    qbt._torrent_by_hash = lambda value: {"hash": value}
    qbt.files = lambda value: [{"index": 0, "name": "existing/book.m4b", "size": 123}]
    deleted: list[str] = []
    qbt.delete = lambda value, **kwargs: deleted.append(value)

    rows = qbt.inspect_release_files(_release(), save_path=tmp_path)
    assert rows == [{"index": 0, "name": "existing/book.m4b", "size": 123}]
    assert deleted == []
