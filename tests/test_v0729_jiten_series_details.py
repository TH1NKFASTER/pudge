from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService

from test_v0727_drop_selection_series_jiten import SCROLLERS, SERIES_LAYOUT
from tests.i18n_support import en


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
                        "links": [{"linkType": 4, "url": "https://anilist.co/manga/30013"}],
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


def test_jiten_subdeck_pagination_and_order_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = LightNovelService(_config(tmp_path))

    first_children = [
        {"deckId": 1000 + i, "originalTitle": f"Part {i}", "characterCount": i * 1000} for i in range(1, 26)
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
                        "links": [{"linkType": 4, "url": "https://anilist.co/manga/777"}],
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


def test_settings_link_to_the_jiten_and_jpdb_key_pages(page) -> None:
    assert en("ui.apiTokenAtBottomOf").startswith("API token at bottom of Settings")
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const link = id => document.querySelector(`#${id}`).closest('.credential-field').querySelector('[data-action=url]');
            const out = {jiten: link('s_ln_jiten_key').innerText, jpdb: link('s_ln_jpdb_key').innerText,
                         texts: [t('ui.jitenApiKey'), t('ui.apiTokenAtBottomOf')]};
            link('s_ln_jiten_key').click();
            link('s_ln_jpdb_key').click();
            await __flush();
            return out;
            """,
            lang=lang,
        )
        out = result.value
        assert [out["jiten"], out["jpdb"]] == out["texts"]
        assert result.calls_of("open_url") == [["https://jiten.moe/settings"], ["https://jpdb.io/settings"]]


LN_STATS = {
    "available": True,
    "difficulty": 3,
    "word_count": 250000,
    "unique_word_count": 12000,
    "url": "https://jiten.moe/decks/media/700/detail",
    "volumes": {
        "1": {"difficulty": 1, "word_count": 60000, "unique_word_count": 5000, "deck_id": 701},
        "2": {"difficulty": 2, "word_count": 65000, "unique_word_count": 5200, "deck_id": 702},
    },
}


def _ln(book_id: int, volume: int) -> dict[str, object]:
    return {
        "id": book_id,
        "title": f"Spice {volume}",
        "series_key": "spice",
        "series_title": "Spice",
        "volume": volume,
        "character_count": 1000,
        "anilist_id": 31,
    }


def test_light_novel_series_header_and_volume_cards_show_their_own_jiten_stats(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('lightnovels'); await __tick(50);
            const level = n => t('uiA.jiten.difficulty', {level: t(`uiA.jiten.level${n}`)});
            const card = volume => document.querySelector(`#lightNovelsContent [data-ln-volume="${volume}"] [data-ln-card-jiten]`).innerText;
            const series = document.querySelector('#lightNovelsContent [data-ln-series-jiten]');
            return {cards: [card(1), card(2), card(3)], series: series.innerText, seriesUrl: series.querySelector('button')?.dataset.url,
                    levels: [level(1), level(2), level(3)], seriesLength: tn('uiA.jiten.length', 1000, {value: compactJitenNumber(250000)}),
                    volumeLength: tn('uiA.jiten.length', 1000, {value: compactJitenNumber(60000)})};
            """,
            lang=lang,
            api={
                "light_novel_state": {
                    "version": 1,
                    "settings": {},
                    "books": [_ln(1, 1), _ln(2, 2), _ln(3, 3)],
                },
                "planning_jiten_stats": LN_STATS,
            },
        )
        out = result.value
        one, two, three = out["levels"]
        assert one in out["cards"][0] and out["volumeLength"] in out["cards"][0]
        assert two in out["cards"][1]
        assert three in out["cards"][2]  # no deck for volume 3: the series numbers
        # The header shows the whole series (without its total length) and links to its deck.
        assert three in out["series"] and out["seriesLength"] not in out["series"]
        assert out["seriesUrl"] == LN_STATS["url"]
        calls = result.calls_of("planning_jiten_stats")
        assert calls and all(call[:3] == [31, "novel", "NOVEL"] for call in calls)


def test_series_scroll_waits_for_real_card_sizes_and_measures_again_on_return(page) -> None:
    # Hidden tabs and covers that have not loaded report tiny sizes; that measurement must not
    # stick as the scroller height. Coming back to the page measures again.
    result = page.run(
        SCROLLERS
        + r"""
        window.cardHeight = 20;
        setPage('lightnovels'); await __tick(100);
        const tiny = view('spice');
        window.cardHeight = 120;
        setPage('current'); await __tick(100);
        setPage('lightnovels'); await __tick(100);
        return {tiny, real: view('spice')};
        """,
        api={
            "light_novel_state": {"version": 1, "settings": {}, "books": [_ln(1, 1), _ln(2, 2), _ln(3, 3)]},
            "planning_jiten_stats": {"available": False},
        },
        before=SERIES_LAYOUT,
    )
    assert result.value["tiny"] == {"height": "", "top": 0}
    assert result.value["real"] == {"height": "247px", "top": 0}
