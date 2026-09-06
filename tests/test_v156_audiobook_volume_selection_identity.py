from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import audiobook_torrent_pack_plan


def test_bare_numeric_chapter_folder_is_not_a_volume_without_series_context() -> None:
    plan = audiobook_torrent_pack_plan([
        {"index": 0, "name": "02/001.mp3", "size": 100},
        {"index": 1, "name": "03/001.mp3", "size": 100},
    ], allow_bare_numeric_volume_dirs=False)
    assert plan["volumes"] == []
    assert [row["file_id"] for row in plan["ungrouped_audio"]] == [0, 1]


def test_numeric_directory_is_volume_only_immediately_under_matching_series() -> None:
    files = [
        {"index": 0, "name": "狼と香辛料/02/01/track-01.mp3", "size": 100},
        {"index": 1, "name": "狼と香辛料/02/02/track-02.mp3", "size": 200},
        {"index": 2, "name": "狼と香辛料/03/01/track-01.mp3", "size": 300},
    ]
    plan = audiobook_torrent_pack_plan(files, series_title="狼と香辛料")
    assert [row["volume"] for row in plan["volumes"]] == [2, 3]
    assert plan["volumes"][0]["file_ids"] == [0, 1]


def test_direct_volume_ten_release_does_not_match_target_two_from_chapter_folder() -> None:
    import pudge.web_app as web_app

    class FakeLightNovels:
        @staticmethod
        def _release_volume_match(title: str, target: int):
            return False, None, False

    api = object.__new__(web_app.WebAppApi)
    api.light_novels = FakeLightNovels()
    selected, plan = api._audiobook_nyaa_volume_selection(
        [{"index": 0, "name": "02/001.mp3", "size": 100}],
        2,
        "狼と香辛料X～完全版オーディオブック (Spice and Wolf, Vol. 10)",
        series_title="狼と香辛料",
    )
    assert selected == []
    assert plan["volumes"] == []


def test_download_uses_exact_file_list_returned_by_search(monkeypatch, tmp_path: Path) -> None:
    import pudge.web_app as web_app

    priorities: list[tuple[list[int], int]] = []
    started: list[str] = []
    thread_args: list[tuple] = []

    class FakeQbt:
        def __init__(self, *args, **kwargs):
            pass

        def add_release(self, *args, **kwargs):
            return "abc"

        def files(self, torrent_hash: str):
            return [
                {"index": 0, "name": "[A]/狼と香辛料/[02] 狼と香辛料II.m4b", "size": 100},
                {"index": 1, "name": "[B]/別作品/[02] 別作品II.m4b", "size": 200},
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
            return {
                "id": book_id,
                "title": "狼と香辛料II",
                "series_title": "狼と香辛料",
                "volume": 2,
            }

    monkeypatch.setattr(web_app, "QBittorrentClient", FakeQbt)
    monkeypatch.setattr(web_app.threading, "Thread", FakeThread)

    api = object.__new__(web_app.WebAppApi)
    api.light_novels = FakeLightNovels()
    api.config = SimpleNamespace(
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
        paths=SimpleNamespace(download_dirs=[str(tmp_path)]),
        library=SimpleNamespace(root_dir=str(tmp_path)),
    )
    api.logger = SimpleNamespace(info=lambda *a, **k: None)

    result = api.light_novel_download_audiobook_nyaa(180, {
        "title": "TMW Japanese Audiobooks Collection",
        "link": "https://nyaa.invalid/view/1",
        "torrent_url": "https://nyaa.invalid/download/1.torrent",
        "info_hash": "a" * 40,
        "size": "1 GiB",
        "seeders": 1,
        "trusted": True,
        "target_volume": 2,
        "collection": True,
        "selected_files": ["[A]/狼と香辛料/[02] 狼と香辛料II.m4b"],
    })

    assert priorities == [([0, 1], 0), ([0], 6)]
    assert started == ["abc"]
    assert result["selected_files"] == ["[A]/狼と香辛料/[02] 狼と香辛料II.m4b"]
    assert thread_args and thread_args[0][-1] == {0}


def test_nyaa_modal_shows_volume_identity_not_chapter_file_paths() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "volumeTitle=row.volume_title" in html
    assert "selected_track_count" in html
    assert "files.map(escapeHtml).join" not in html
