"""Global search hides leftover anime rows (not listed, not local, not downloading)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from css_support import css_rules

from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def _api(
    tmp_path: Path, anime: list[LibraryAnime], episodes: list[LibraryEpisode], downloads: list
) -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    db = SimpleNamespace(
        anime_list=lambda *a, **k: list(anime),
        episodes=lambda *a, **k: list(episodes),
        downloads=lambda: list(downloads),
    )
    api.manager = SimpleNamespace(
        db=db, incomplete_download_paths=lambda: set(), _path_within=lambda *_: False
    )
    api.light_novels = SimpleNamespace(state=lambda: {"books": []})
    api.manga = SimpleNamespace(state=lambda: {"books": []})
    api.audiobooks = SimpleNamespace(search_catalog=lambda: [])
    api._global_anilist_aliases = lambda ids, allow_network=False: {}
    api._cover_uri = lambda item: ""
    return api


def test_orphan_release_named_rows_are_not_search_hits(tmp_path: Path, monkeypatch) -> None:
    # Scoring is not under test; keep it independent of rapidfuzz.
    monkeypatch.setattr(
        "pudge.web_app._global_search_score",
        lambda query, names: next(
            ((100.0, n) for n in names if query.casefold() in str(n).casefold()), (0.0, "")
        ),
    )
    video = tmp_path / "ep1.mkv"
    video.write_bytes(b"x")
    anime = [
        LibraryAnime(
            media_id=153152,
            title="[Erai-raws] Boku no Kokoro no Yabai Yatsu - 01 ~ 12 [720p][Multiple Subtitle]",
            status="",
        ),
        LibraryAnime(media_id=1, title="Yabai Local", status=""),
        LibraryAnime(media_id=2, title="Yabai Downloading", status=""),
        LibraryAnime(media_id=3, title="Yabai Planned", status="PLANNING"),
    ]
    episodes = [LibraryEpisode(media_id=1, title="Yabai Local", episode=1, video_path=video, state="ready")]
    downloads = [SimpleNamespace(media_id=2)]
    rows = _api(tmp_path, anime, episodes, downloads).global_media_search("yabai", 40)
    ids = {row["media_id"] for row in rows if row["kind"] == "anime"}
    assert 153152 not in ids
    assert {1, 2, 3} <= ids


def test_search_overlay_has_no_backdrop_filter(page) -> None:
    # In WKWebView a backdrop blur left a stale translucent rectangle next to the search
    # surface (the review gate had the same artifact): the overlay is a plain dark layer.
    result = page.run(
        """
        await __key('f', {metaKey: true});
        await __tick(20);
        const overlay = $('globalSearchOverlay');
        return {open: __visible('#globalSearchOverlay'),
                classes: [...new Set([overlay, ...overlay.querySelectorAll('*')].flatMap(el => [...el.classList]))],
                blurred: [overlay, ...overlay.querySelectorAll('*')].filter(el => /backdrop/i.test(el.getAttribute('style') || '')).length};
        """
    )
    assert result.value["open"] is True and result.value["blurred"] == 0
    assert "pudge-search-surface" in result.value["classes"]
    rules = css_rules("index.html")
    for media, selector, declarations in rules.rules:
        if "#globalSearchOverlay" in selector or any(
            f".{name}" in selector for name in result.value["classes"]
        ):
            assert not any("backdrop-filter" in prop for prop in declarations), (media, selector)
