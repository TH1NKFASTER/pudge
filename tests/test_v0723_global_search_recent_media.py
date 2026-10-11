"""Global search, recently watched, Jiten lengths and reader font shortcuts of v0.7.24
(behaviour; audit 71Б rewrite of the old source-text checks).

Covered elsewhere: Cmd+F opens the global search and asks the backend with the cleaned query
(test_v0556_planning_search_and_downloads.py::test_cmd_f_opens_global_search_on_any_page);
no "Debug" action for caught-up or planned titles
(test_accumulated_0700_debug_manga.py::test_debug_window_opens_from_the_anime_menu_switches_tabs_and_exports)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.manager_models import LibraryAnime, LibraryEpisode
from t71g18_support import ln_book
from tests.i18n_support import en, ru
from pudge.web_app import (
    WebAppApi,
    _global_search_normalize,
    _global_search_score,
)


def _fake_api(tmp_path: Path, episodes: list[LibraryEpisode]) -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        agent=SimpleNamespace(delete_after_watched_hours=6.0),
        playback=SimpleNamespace(rewind_seconds=10.0),
    )
    db = SimpleNamespace(episodes=lambda *args, **kwargs: list(episodes))
    api.manager = SimpleNamespace(
        db=db,
        incomplete_download_paths=lambda: set(),
        _path_within=lambda _path, _roots: False,
    )
    api._anime_payload = lambda anime, **_options: {
        "media_id": anime.media_id,
        "title": anime.title,
        "cover": "",
        "site_url": anime.site_url,
        "media_status": anime.media_status or "",
    }
    return api


def test_recently_watched_keeps_only_latest_episode_per_anime(tmp_path: Path) -> None:
    now = time.time()
    paths = [tmp_path / f"ep{n}.mkv" for n in (1, 2, 3)]
    for path in paths:
        path.write_bytes(b"x")

    episodes = [
        LibraryEpisode(
            media_id=10,
            title="Example",
            episode=1,
            video_path=paths[0],
            state="watched",
            watched_at=now - 3600,
            playback_position=120.0,
            playback_duration=1400.0,
        ),
        LibraryEpisode(
            media_id=10,
            title="Example",
            episode=2,
            video_path=paths[1],
            state="watched",
            watched_at=now - 600,
            playback_position=900.0,
            playback_duration=1400.0,
        ),
        LibraryEpisode(
            media_id=20,
            title="Old",
            episode=3,
            video_path=paths[2],
            state="watched",
            watched_at=now - 7 * 3600,
            playback_position=300.0,
            playback_duration=1200.0,
        ),
    ]
    api = _fake_api(tmp_path, episodes)
    anime = {
        10: LibraryAnime(media_id=10, title="Example", format="TV"),
        20: LibraryAnime(media_id=20, title="Old", format="TV"),
    }

    rows = api._recently_watched_payloads(anime)

    assert len(rows) == 1
    assert rows[0]["media_id"] == 10
    assert rows[0]["episode"] == 2
    assert rows[0]["video_path"] == str(paths[1])
    assert rows[0]["resume_start"] == pytest.approx(890.0)


def test_recently_watched_missing_file_is_not_exposed(tmp_path: Path) -> None:
    episode = LibraryEpisode(
        media_id=10,
        title="Missing",
        episode=1,
        video_path=tmp_path / "missing.mkv",
        state="watched",
        watched_at=time.time() - 60,
    )
    api = _fake_api(tmp_path, [episode])
    rows = api._recently_watched_payloads({10: LibraryAnime(media_id=10, title="Missing")})
    assert rows == []


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("Frieren", ["Sousou no Frieren", "Frieren: Beyond Journey's End", "葬送のフリーレン"]),
        ("葬送のフリーレン", ["Sousou no Frieren", "Frieren: Beyond Journey's End", "葬送のフリーレン"]),
        ("Beyond Journey", ["Sousou no Frieren", "Frieren: Beyond Journey's End", "葬送のフリーレン"]),
    ],
)
def test_global_search_matches_english_native_and_alternative_titles(
    query: str,
    names: list[str],
) -> None:
    score, matched = _global_search_score(query, names)
    assert score >= 85
    assert matched


def test_global_search_normalization_keeps_japanese_and_ignores_punctuation() -> None:
    assert _global_search_normalize(" 葬送のフリーレン！ ") == _global_search_normalize("葬送のフリーレン")


class _FakeResponse:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")

    def json(self):
        return self._payload


def test_jimaku_empty_validation_is_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        jimaku=SimpleNamespace(
            personal_api_key="abc",
            base_url="https://jimaku.cc",
        )
    )
    api.light_novels = SimpleNamespace()
    monkeypatch.setattr(
        "pudge.web_app.httpx.get",
        lambda *args, **kwargs: _FakeResponse(200, []),
    )

    checks = api.test_saved_credentials(["jimaku"])

    assert checks == [{"service": "Jimaku", "status": "empty", "count": 0}]


def test_jimaku_nonempty_validation_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        jimaku=SimpleNamespace(
            personal_api_key="abc",
            base_url="https://jimaku.cc",
        )
    )
    api.light_novels = SimpleNamespace()
    monkeypatch.setattr(
        "pudge.web_app.httpx.get",
        lambda *args, **kwargs: _FakeResponse(200, [{"id": 1}]),
    )

    checks = api.test_saved_credentials(["jimaku"])

    assert checks[0]["status"] == "ok"
    assert checks[0]["count"] == 1


RECENT = {
    "media_id": 4,
    "title": "Watched Show",
    "episode": 2,
    "video_path": "/v/4.mkv",
    "progress_percent": 60,
    "site_url": "https://anilist.co/anime/4",
}
RECENT_MOVIE = {
    "media_id": 8,
    "title": "Some Movie",
    "is_movie": True,
    "video_path": "/v/8.mkv",
    "site_url": "https://anilist.co/anime/8",
}


def test_home_lists_recently_watched_titles_and_a_click_plays_them_again(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('current'); renderCurrent(); await __tick(20);
            const section = document.querySelector('#current [data-home-section="ui.recentlyWatched2"]');
            const card = id => section.querySelector(`[data-media-id="${id}"] .airing-state`).innerText;
            const out = {title: section.querySelector('h2').innerText, episode: card(4), movie: card(8),
                         expected: [t('ui.recentlyWatched2'), `${t('label.episode')} 2`, t('label.movie')]};
            await __fire('#current [data-home-section="ui.recentlyWatched2"] [data-media-id="4"]', 'pointerup', {button: 0}, 'PointerEvent');
            await __tick(20);
            return out;
            """,
            lang=lang,
            state={"home": {"recently_watched": [RECENT, RECENT_MOVIE]}},
        )
        out = result.value
        assert [out["title"], out["episode"], out["movie"]] == out["expected"]
        assert [call[0] for call in result.calls_of("play")] == ["/v/4.mkv"]


JITEN = {
    "available": True,
    "difficulty": 2,
    "word_count": 250000,
    "volumes": {"1": {"difficulty": 1, "word_count": 1002}, "2": {"difficulty": 1, "word_count": 1}},
}


def _volume(book_id: int, volume: int, characters: int) -> dict:
    return {
        "id": book_id,
        "title": f"Spice {volume}",
        "series_key": "spice",
        "series_title": "Spice",
        "volume": volume,
        "character_count": characters,
        "anilist_id": 31,
    }


@pytest.mark.parametrize(("lang", "say", "decimal"), [("en", en, "1.0"), ("ru", ru, "1,0")])
def test_light_novel_cards_show_their_length_in_characters_and_jiten_words(page, lang, say, decimal) -> None:
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        const card = volume => document.querySelector(`#lightNovelsContent [data-ln-volume="${volume}"]`);
        return [1, 2].map(volume => ({meta: card(volume).querySelector('.ln-card-meta').innerText,
                                      jiten: card(volume).querySelector('[data-ln-card-jiten]').innerText}));
        """,
        lang=lang,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [_volume(1, 1, 123456), _volume(2, 2, 0)],
            },
            "planning_jiten_stats": JITEN,
        },
    )
    first, second = result.value
    assert f"{say('uiA.number.thousands', value='123')} {say('ui.chars')}" in first["meta"]
    assert say("ui.chars") not in second["meta"]  # no count, no "0 chars"
    # A compacted number takes the noun form of a large count: 1 002 words are "1.0k слов", not "слова".
    assert (
        say("uiA.jiten.length", count=1000, value=say("uiA.number.thousands", value=decimal))
        in first["jiten"].splitlines()
    )
    assert say("uiA.jiten.length", count=1, value="1") in second["jiten"].splitlines()


def test_jiten_length_catalog_texts() -> None:
    assert en("uiA.jiten.length", count=1000, value="1.2k") == "Length: 1.2k words"
    assert en("uiA.jiten.length", count=1, value="1") == "Length: 1 word"
    assert ru("uiA.jiten.length", count=1000, value="1.2k") == "Объём: 1.2k слов"


def test_cmd_minus_plus_and_zero_change_the_reader_font_size_and_save_it(page) -> None:
    result = page.run(
        r"""
        await __settle(openLightNovel(6)); await __tick(300);
        const size = () => $('lnReader').style.getPropertyValue('--ln-font-size');
        const out = {start: size()};
        const press = async (key, code) => { await __key(key, {metaKey: true, code}, 'body'); await __tick(10); return size(); };
        out.bigger = [await press('=', 'Equal'), await press('+', 'Equal')];
        out.smaller = await press('-', 'Minus');
        await __tick(1000);
        out.reset = await press('0', 'Digit0');
        await __tick(1000);
        return out;
        """,
        before=ln_book(settings={"reader_font_size": 30}),
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    out = result.value
    assert out == {"start": "30px", "bigger": ["32px", "34px"], "smaller": "32px", "reset": "30px"}
    saves = [
        call[0]["reader_font_size"]
        for call in result.calls_of("light_novel_save_settings")
        if "reader_font_size" in call[0]
    ]
    assert saves == [32, 30]  # one save after each burst of key presses
