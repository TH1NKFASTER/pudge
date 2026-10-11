from __future__ import annotations

import json
from pathlib import Path

from css_support import css_rules

from pudge.cli import _find_online_subtitles
from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.models import AniListAnime, JimakuEntry, JimakuFile, SubtitleCandidate, VideoIdentity
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def relation(media_id: int, title: str) -> dict:
    return {
        "relation_type": "SIDE_STORY",
        "media_id": media_id,
        "title": title,
        "episodes": 1,
        "relations": [],
    }


def monogatari_state(tmp_path: Path) -> dict:
    """A get_state payload (real backend): three ready Monogatari entries grouped into one
    watch sequence on Home."""
    api = make_api(tmp_path)
    entries = [
        (1, "Bakemonogatari", 2),
        (2, "Nisemonogatari", 3),
        (3, "Kizumonogatari", 1),
    ]
    for media_id, title, related_id in entries:
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=title,
                status="PLANNING",
                media_status="FINISHED",
                episodes=1,
                start_date=f"20{media_id:02d}-01-01",
                relations=[relation(related_id, f"Related {related_id}")],
            )
        )
        video = tmp_path / f"{title}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title=title,
                episode=1,
                video_path=video,
                state="ready",
            )
        )
    return json.loads(json.dumps(api.get_state(), default=str))


# The watch-sequence card on Home, read from the window.
SEQUENCE = r"""
const card = document.querySelector('.ready-sequence-card');
const rows = () => [...card.querySelectorAll('.ready-sequence-row')].map(row => row.innerText.replace(/\s+/g, ' ').trim());
"""


def test_monogatari_sequence_gets_franchise_title_and_compact_card(tmp_path: Path, page) -> None:
    state = monogatari_state(tmp_path)
    group = state["home"]["completed_ready"][0]
    assert group["kind"] == "watch_sequence"
    assert group["title"] == "Monogatari Series"
    for lang in ("en", "ru"):
        result = page.run(
            SEQUENCE
            + """
            return {cards: document.querySelectorAll('.ready-sequence-card').length, series: card.title, rows: rows(),
                    lead: card.querySelector('.ready-sequence-meta strong').innerText, text: card.innerText,
                    orderLabels: ['label.watchSequence', 'label.releaseOrder', 'label.anilistOrder'].map(key => t(key))};
            """,
            lang=lang,
            state=state,
        )
        out = result.value
        assert out["cards"] == 1 and out["series"] == "Monogatari Series"
        assert out["rows"] == ["1 Bakemonogatari", "2 Nisemonogatari", "3 Kizumonogatari"]
        assert out["lead"] == "Bakemonogatari"
        assert not any(label in out["text"] for label in out["orderLabels"])  # no "Watch order" caption
    # A normal-sized card in the grid, not a full-width row.
    rules = css_rules("index.html")
    assert rules.get(".ready-sequence-card", "position") == "relative"
    assert rules.get(".ready-sequence-card", "grid-column") is None


def test_waiting_card_never_prints_episode_null(page) -> None:
    waiting = [
        {"media_id": 1, "title": "No next episode", "next_episode": None},
        {"media_id": 2, "title": "Local without number", "next_episode": None, "local": {"episode": None}},
        {"media_id": 3, "title": "Zero", "next_episode": 0},
        {"media_id": 4, "title": "Episode three", "next_episode": 3},
        {"media_id": 5, "title": "Local five", "local": {"episode": 5}},
    ]
    for lang in ("en", "ru"):
        result = page.run(
            """
            return {states: [...document.querySelectorAll('.waiting-card .airing-state')].map(el => el.innerText),
                    notReady: t('label.notReady'), three: t('label.episodeNotReady', {episode: 3}),
                    five: t('label.episodeNotReady', {episode: 5})};
            """,
            lang=lang,
            state={"home": {"waiting": waiting}},
        )
        out = result.value
        assert out["states"] == [out["notReady"], out["notReady"], out["notReady"], out["three"], out["five"]]
        assert not any("null" in text or "undefined" in text for text in out["states"])


def test_exact_title_single_special_is_accepted_without_jimaku_anilist_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.jimaku.api_key = "token"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.matching.jimaku_min_score = 45.0
    video = tmp_path / "Boku no Hero Academia - I am a hero too.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nエリ\n", encoding="utf-8")

    entry = JimakuEntry(
        id=12479,
        name="Boku no Hero Academia: I am a hero too",
        english_name="My Hero Academia: I am a hero too",
        japanese_name="僕のヒーローアカデミア I am a hero too",
        anilist_id=None,
        flags={},
    )
    file = JimakuFile(
        url="https://jimaku.cc/api/files/one",
        name="subtitle.srt",
        size=100,
        last_modified="2026-08-04T00:00:00+00:00",
    )

    def fake_search(self, *, anilist_id=None, query=None):
        return [] if anilist_id is not None else [entry]

    monkeypatch.setattr("pudge.cli.JimakuClient.search_entries", fake_search)
    monkeypatch.setattr(
        "pudge.cli.JimakuClient.files_for_episode",
        lambda self, entry_id, episode, alternative_episodes=(): [file],
    )
    monkeypatch.setattr("pudge.cli.JimakuClient.close", lambda self: None)

    def fake_materialize(client, item, identity, video, cache_dir, **kwargs):
        return [
            SubtitleCandidate(
                subtitle,
                "jimaku",
                item.score,
                item.name,
                details=dict(item.details),
            )
        ]

    monkeypatch.setattr("pudge.cli.materialize_jimaku_files", fake_materialize)

    candidates = _find_online_subtitles(
        video,
        VideoIdentity(title="Boku no Hero Academia: I am a hero too"),
        cfg,
        None,
        False,
        anime_hint=AniListAnime(
            id=211711,
            titles=["Boku no Hero Academia: I am a hero too"],
            synonyms=["My Hero Academia: I am a hero too"],
            season_year=2026,
            episodes=1,
            format="SPECIAL",
        ),
        skip_airing_lookup=True,
    )

    assert candidates
    assert candidates[0].details["entry_id"] == 12479
    assert candidates[0].details["entry_anilist_match"] is True
    assert candidates[0].details["single_special_exact_title_match"] is True
    assert candidates[0].details["episode_match"] == "exact"
