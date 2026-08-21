from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.manager_models import LibraryAnime, LibraryEpisode
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
    api._anime_payload = lambda anime: {
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
    rows = api._recently_watched_payloads(
        {10: LibraryAnime(media_id=10, title="Missing")}
    )
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
    assert _global_search_normalize(" 葬送のフリーレン！ ") == _global_search_normalize(
        "葬送のフリーレン"
    )


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

    assert checks == [
        {"service": "Jimaku", "status": "empty", "count": 0}
    ]


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


def test_frontend_contracts_are_present() -> None:
    index = (
        Path(__file__).resolve().parents[1] / "pudge" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    assert "openGlobalSearch();" in index
    assert "global_media_search(cleaned,40)" in index
    assert "recently_watched" in index
    assert "recentlyWatchedHomeCard" in index
    assert "applyLnReaderSettingsPatch({reader_font_size:next},true)" in index
    assert "const debug=(planned||mediaInHomeSection('caught_up',a.media_id))" in index
    assert "compactJitenNumber(shown.word_count)+' words'" in index
    assert "Number(book.character_count||0)" in index
