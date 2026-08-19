from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return config


def test_jiten_series_uses_parent_stats_and_volume_subdecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LightNovelService(_config(tmp_path))
    service.save_settings({"jiten_api_key": "key"})
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_get(action: str, params=None):
        params = dict(params or {})
        calls.append((action, params))
        if action == "media-deck/get-media-decks":
            return {
                "data": [
                    {
                        "deckId": 100,
                        "originalTitle": "One Piece",
                        "links": [
                            {"linkType": 4, "url": "https://anilist.co/manga/30013"}
                        ],
                    }
                ]
            }
        assert action == "media-deck/100/detail"
        return {
            "data": {
                "parentDeck": None,
                "mainDeck": {
                    "deckId": 100,
                    "originalTitle": "One Piece",
                    "characterCount": 2_500_000,
                    "wordCount": 900_000,
                    "uniqueWordCount": 40_000,
                    "difficulty": 2,
                    "difficultyRaw": 2.2,
                    "coverage": 88.0,
                    "youngCoverage": 91.0,
                },
                "subDecks": [
                    {
                        "deckId": 101,
                        "originalTitle": "One Piece Vol. 1",
                        "characterCount": 18_123,
                        "wordCount": 6_000,
                        "uniqueWordCount": 2_100,
                        "difficulty": 1,
                        "coverage": 92.0,
                    },
                    {
                        "deckId": 102,
                        "originalTitle": "One Piece Vol. 2",
                        "characterCount": 19_456,
                        "wordCount": 6_500,
                        "uniqueWordCount": 2_250,
                        "difficulty": 2,
                        "coverage": 90.0,
                    },
                ],
            },
            "totalItems": 2,
            "pageSize": 25,
            "currentOffset": 0,
        }

    monkeypatch.setattr(service, "_jiten_get", fake_get)
    stats = service.jiten_media_stats(30013, "manga", "MANGA", ["One Piece"])

    assert stats["character_count"] == 2_500_000
    assert stats["volumes"]["1"]["character_count"] == 18_123
    assert stats["volumes"]["2"]["character_count"] == 19_456
    assert stats["volumes"]["1"]["deck_id"] == 101
    assert stats["volumes"]["2"]["url"].endswith("/102/detail")
    assert calls[-1] == ("media-deck/100/detail", {"offset": 0})


def test_jiten_subdeck_pagination_and_order_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LightNovelService(_config(tmp_path))

    first_children = [
        {"deckId": 1000 + i, "originalTitle": f"Part {i}", "characterCount": i * 1000}
        for i in range(1, 26)
    ]
    last_child = {"deckId": 1026, "originalTitle": "Part final", "characterCount": 26_000}

    def fake_get(action: str, params=None):
        params = dict(params or {})
        if action == "media-deck/get-media-decks":
            return {
                "data": [
                    {
                        "deckId": 900,
                        "originalTitle": "シリーズ",
                        "links": [
                            {"linkType": 4, "url": "https://anilist.co/manga/777"}
                        ],
                    }
                ]
            }
        if params.get("offset") == 25:
            return {
                "data": {
                    "mainDeck": {"deckId": 900},
                    "subDecks": [last_child],
                },
                "totalItems": 26,
                "pageSize": 25,
                "currentOffset": 25,
            }
        return {
            "data": {
                "mainDeck": {"deckId": 900, "characterCount": 100_000},
                "subDecks": first_children,
            },
            "totalItems": 26,
            "pageSize": 25,
            "currentOffset": 0,
        }

    monkeypatch.setattr(service, "_jiten_get", fake_get)
    stats = service.jiten_media_stats(777, "novel", "NOVEL", ["シリーズ"])

    # The child titles intentionally do not say volume. Default Jiten subdeck
    # order therefore becomes the volume order.
    assert stats["volumes"]["1"]["deck_id"] == 1001
    assert stats["volumes"]["26"]["deck_id"] == 1026
    assert stats["volumes"]["26"]["character_count"] == 26_000


def test_jiten_links_series_header_volume_stats_and_hidden_scroll_guard() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    manga = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "https://jiten.moe/settings" in html
    assert "https://jpdb.io/settings" in html
    assert "API token at bottom of Settings" in html
    assert "data-ln-series-jiten" in html
    assert "data-manga-series-jiten" in manga
    assert "data-jiten-volume" in html
    assert "data-jiten-volume" in manga
    assert "stats?.volumes?.[String(requestedVolume)]" in html
    assert "scroller.getClientRects().length===0" in html
    assert "firstHeight<50" in html
    assert "focusSeriesScrollersSoon(box)" in html
