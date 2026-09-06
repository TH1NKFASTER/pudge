from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.manager_models import NyaaRelease


def _release(title: str, info_hash: str) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://nyaa.invalid/view/{info_hash[:4]}",
        torrent_url=f"https://nyaa.invalid/download/{info_hash[:4]}.torrent",
        info_hash=info_hash,
        size_text="1.2 GiB",
        size_bytes=1200,
        seeders=20,
        leechers=1,
        downloads=100,
        trusted=True,
        remake=False,
        category_id="2_2",
    )


def test_exact_direct_audiobook_title_is_offered_without_torrent_metadata(monkeypatch) -> None:
    import pudge.web_app as web_app

    exact = _release(
        "狼と香辛料II～完全版オーディオブック (Spice and Wolf, Vol. 2 - Unabridged Japanese Audiobook)",
        "a" * 40,
    )
    wrong = _release(
        "狼と香辛料X～完全版オーディオブック (Spice and Wolf, Vol. 10 - Unabridged Japanese Audiobook)",
        "b" * 40,
    )
    fetched: list[str] = []

    class FakeNyaaClient:
        def __init__(self, *args, **kwargs):
            pass

        def search(self, query: str, **kwargs):
            if query == "狼と香辛料":
                return [exact, wrong]
            return []

        def fetch_torrent_payload(self, url: str) -> bytes:
            fetched.append(url)
            raise AssertionError("exact direct audiobook releases must not fetch torrent metadata during search")

        def close(self):
            pass

    class FakeLightNovels:
        def book(self, book_id: int):
            return {"id": book_id, "title": "狼と香辛料II", "series_title": "狼と香辛料", "volume": 2}

        @staticmethod
        def _nyaa_title(value: str) -> str:
            return value

        @staticmethod
        def _release_volume_match(title: str, target: int):
            if "Vol. 2 " in title and target == 2:
                return True, (2, 2), True
            if "Vol. 10 " in title:
                return False, (10, 10), False
            return False, None, False

    monkeypatch.setattr(web_app, "NyaaClient", FakeNyaaClient)
    api = object.__new__(web_app.WebAppApi)
    api.light_novels = FakeLightNovels()
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(base_url="https://nyaa.invalid", proxy_mode="direct", proxy_url="", pre_search_command=""),
        qbittorrent=SimpleNamespace(enabled=False),
        aria2=SimpleNamespace(enabled=True),
    )
    api.logger = SimpleNamespace(info=lambda *a, **k: None)

    result = api.light_novel_search_audiobook_nyaa(180)
    assert fetched == []
    assert len(result["matches"]) == 1
    row = result["matches"][0]
    assert row["title"] == exact.title
    assert row["metadata_source"] == "title"
    assert row["selected_files"] == []
    assert row["selected_track_count"] is None


def test_selective_audiobook_download_can_use_aria2_backend(monkeypatch, tmp_path: Path) -> None:
    import pudge.web_app as web_app

    priorities: list[tuple[list[int], int]] = []
    started: list[str] = []
    thread_args: list[tuple] = []

    class FakeTorrent:
        def add_release(self, *args, **kwargs):
            assert kwargs["paused"] is True
            assert kwargs["stop_at_metadata"] is True
            return "aria-hash"

        def files(self, torrent_hash: str):
            return [
                {"index": 0, "name": "狼と香辛料II/book.m4b", "size": 100, "progress": 0.0, "priority": 1},
                {"index": 1, "name": "cover.jpg", "size": 10, "progress": 0.0, "priority": 1},
            ]

        def set_file_priority(self, torrent_hash: str, ids, priority: int):
            priorities.append((list(ids), int(priority)))

        def start(self, torrent_hash: str):
            started.append(torrent_hash)

        def delete(self, *args, **kwargs):
            pass

        def close(self):
            pass

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            thread_args.append(tuple(args))
        def start(self):
            pass

    class FakeLightNovels:
        @staticmethod
        def book(book_id: int):
            return {"id": book_id, "title": "狼と香辛料II", "series_title": "狼と香辛料", "volume": 2}
        @staticmethod
        def _release_volume_match(title: str, target: int):
            return (target == 2), (2, 2), (target == 2)

    api = object.__new__(web_app.WebAppApi)
    api.light_novels = FakeLightNovels()
    api.config = SimpleNamespace(
        qbittorrent=SimpleNamespace(enabled=False),
        aria2=SimpleNamespace(enabled=True),
        paths=SimpleNamespace(download_dirs=[str(tmp_path)]),
        library=SimpleNamespace(root_dir=str(tmp_path)),
    )
    api.logger = SimpleNamespace(info=lambda *a, **k: None)
    api._audiobook_torrent_client = lambda: ("aria2", FakeTorrent())
    monkeypatch.setattr(web_app.threading, "Thread", FakeThread)

    result = api.light_novel_download_audiobook_nyaa(180, {
        "title": "狼と香辛料II～完全版オーディオブック (Spice and Wolf, Vol. 2 - Unabridged Japanese Audiobook)",
        "link": "https://nyaa.invalid/view/2",
        "torrent_url": "https://nyaa.invalid/download/2.torrent",
        "info_hash": "a" * 40,
        "size": "1.2 GiB",
        "seeders": 20,
        "trusted": True,
        "target_volume": 2,
        "collection": False,
        "selected_files": [],
    })

    assert priorities == [([1], 0)]
    assert started == ["aria-hash"]
    assert result["selected_files"] == ["狼と香辛料II/book.m4b"]
    assert thread_args and thread_args[0][-1] == {0}
