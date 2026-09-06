from __future__ import annotations

from types import SimpleNamespace

from pudge.audiobooks import audiobook_torrent_files_from_payload, audiobook_torrent_pack_plan
from pudge.manager_models import NyaaRelease


def _bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return _bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        rows = []
        for key in sorted(value):
            rows.append(_bencode(key))
            rows.append(_bencode(value[key]))
        return b"d" + b"".join(rows) + b"e"
    raise TypeError(type(value))


def _release(title: str = "TMW Japanese Audiobooks Collection Part 8") -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://nyaa.invalid/view/1",
        torrent_url="https://nyaa.invalid/download/1.torrent",
        info_hash="a" * 40,
        size_text="200 GiB",
        size_bytes=200 * 1024**3,
        seeders=12,
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        category_id="3_3",
    )


def test_japanese_roman_volume_suffix_with_asin_is_detected() -> None:
    files = [
        {"index": 0, "name": "[支倉 凍砂]/狼と香辛料/[01] 狼と香辛料I [B0AAA111].m4b", "size": 100},
        {"index": 1, "name": "[支倉 凍砂]/狼と香辛料/[02] 狼と香辛料II [B0C58KN4W9].m4b", "size": 200},
        {"index": 2, "name": "[支倉 凍砂]/狼と香辛料/[03] 狼と香辛料III [B0AAA333].m4b", "size": 300},
    ]
    plan = audiobook_torrent_pack_plan(files)
    assert [row["volume"] for row in plan["volumes"]] == [1, 2, 3]
    assert plan["volumes"][1]["file_ids"] == [1]


def test_generic_collection_volume_selection_requires_series_path() -> None:
    from pudge.web_app import WebAppApi

    class FakeLightNovels:
        @staticmethod
        def _release_volume_match(title: str, target: int):
            return False, None, False

    api = object.__new__(WebAppApi)
    api.light_novels = FakeLightNovels()
    files = [
        {"index": 0, "name": "Other Series/[02] 別作品II [B000].m4b", "size": 100},
        {"index": 1, "name": "[支倉 凍砂]/狼と香辛料/[02] 狼と香辛料II [B0C58KN4W9].m4b", "size": 200},
    ]
    selected, _plan = api._audiobook_nyaa_volume_selection(
        files,
        2,
        "TMW Japanese Audiobooks Collection",
        series_title="狼と香辛料",
        require_series_match=True,
    )
    assert [row["file_id"] for row in selected] == [1]


def test_search_discovers_series_inside_generic_audiobook_collection(monkeypatch) -> None:
    import pudge.web_app as web_app

    payload = _bencode({
        b"info": {
            b"name": b"TMW Japanese Audiobooks Collection",
            b"files": [
                {b"length": 100, b"path": ["Other Series", "[02] 別作品II [B000].m4b"]},
                {b"length": 200, b"path": ["[支倉 凍砂]", "狼と香辛料", "[02] 狼と香辛料II [B0C58KN4W9].m4b"]},
            ],
        }
    })

    class FakeNyaaClient:
        queries: list[str] = []

        def __init__(self, *args, **kwargs):
            pass

        def search(self, query: str, **kwargs):
            self.queries.append(query)
            if "Collection" in query:
                return [_release()]
            return []

        def fetch_torrent_payload(self, url: str) -> bytes:
            return payload

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
    api = object.__new__(web_app.WebAppApi)
    api.light_novels = FakeLightNovels()
    api.config = SimpleNamespace(nyaa=SimpleNamespace(
        base_url="https://nyaa.invalid",
        proxy_mode="direct",
        proxy_url="",
        pre_search_command="",
    ))
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)

    result = api.light_novel_search_audiobook_nyaa(42)
    assert result["collection_releases"] == 1
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["generic_collection"] is True
    assert match["selected_files"] == [
        "[支倉 凍砂]/狼と香辛料/[02] 狼と香辛料II [B0C58KN4W9].m4b"
    ]
    assert any("Japanese Audiobooks Collection" in q for q in FakeNyaaClient.queries)
