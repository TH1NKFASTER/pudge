from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from tests.css_support import css_rules
from tests.i18n_support import en, ru


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.qbittorrent.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def test_invalidated_missing_ready_forces_full_rebuild_even_with_same_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video.write_bytes(b"video")
    missing = (manager.config.paths.cache_dir / "playback-srt" / "v12-gone.srt").resolve()
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title="Akiba Meido Sensou",
            episode=1,
            video_path=video,
            subtitle_path=missing,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.set_state(manager._subtitle_candidate_fingerprint_state_key(video), "same")
    manager.db.invalidate_subtitle(video, 151379, 1, "Prepared subtitle cache file is missing")
    manager.db.queue_subtitle_job(video, 151379, 1, priority=260)

    force_key = manager._subtitle_force_rebuild_state_key(video)
    assert manager.db.get_state(force_key, "") == "1"
    assert manager.db.get_state(manager._subtitle_candidate_fingerprint_state_key(video), "") == ""

    prepared = (manager.config.paths.cache_dir / "playback-srt" / "v15-new.srt").resolve()
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            commands.append(list(command))
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                f"PREPARED_SUBTITLE={prepared}\n"
                "SUBTITLE_CANDIDATE_FINGERPRINT=same\n"
                "PREPARE_STATUS=ready\n"
                'PREPARED_SUBTITLE_META={"source":"jimaku","name":"same.srt"}\n',
                "",
            )

    # Keep the test focused on subtitle preparation.  On macOS the work
    # scheduler probes battery/thermal state via subprocess.run(["pmset", ...]),
    # which internally uses the same subprocess.Popen object.  If we replace
    # Popen globally before bypassing that resource probe, the first captured
    # command is pmset rather than the Pudge prepare command.
    monkeypatch.setattr(manager.work_scheduler, "background_allowed", lambda **_kwargs: True)
    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)
    assert manager.process_subtitle_jobs(limit=1) == 1
    assert commands
    command = commands[0]
    assert "--force-search" in command
    assert "--resync" in command
    assert "--previous-candidate-fingerprint" not in command

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_path is not None and row.subtitle_path.is_file()
    assert row.subtitle_path.parent == manager.config.library.database_path.parent / "prepared-subtitles"
    assert manager.db.get_state(force_key, "") == ""
    assert manager.db.subtitle_jobs() == []


def test_subtitle_queue_prioritizes_each_titles_watchable_frontier_before_middle(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Series A", progress=0, episodes=12))
    db.upsert_anime(LibraryAnime(media_id=2, title="Series B", progress=4, episodes=12))
    db.upsert_anime(LibraryAnime(media_id=3, title="Series C", progress=0, episodes=12))

    # Deliberately give middle episodes much larger static priorities.  Viewer
    # availability still wins: A1/B5 are the exact next unwatched episodes, and
    # C7 is the first broken episode of its local unresolved range.
    rows = [
        (1, 1, 0),
        (1, 2, 900),
        (1, 3, 1000),
        (2, 5, 0),
        (2, 6, 950),
        (3, 7, 0),
        (3, 8, 999),
    ]
    for media_id, episode, priority in rows:
        video = tmp_path / f"m{media_id}-e{episode}.mkv"
        video.write_bytes(b"video")
        db.queue_subtitle_job(video, media_id, episode, priority=priority)

    due = [(int(row["media_id"]), int(row["episode"])) for row in db.due_subtitle_jobs(limit=20)]
    assert set(due[:2]) == {(1, 1), (2, 5)}
    assert due.index((3, 7)) < due.index((3, 8))
    assert due.index((1, 1)) < due.index((1, 2))
    assert due.index((2, 5)) < due.index((2, 6))

    claimed = [(int(row["media_id"]), int(row["episode"])) for row in db.claim_due_subtitle_jobs(limit=2)]
    assert set(claimed) == {(1, 1), (2, 5)}


def _audiobook(
    book_id: int, volume: int, *, finished: bool = False, series: str = "spice", title: str = ""
) -> dict:
    return {
        "id": book_id,
        "title": title or f"狼と香辛料 {volume}",
        "series_key": series,
        "series_title": "狼と香辛料",
        "volume": volume,
        "duration": 600,
        "position": 600 if finished else 10,
        "finished": finished,
        "speed": 1,
        "chapters": [],
    }


SERIES_BOOKS = [
    _audiobook(1, 1, finished=True),
    _audiobook(2, 2),
    _audiobook(3, 3),
    _audiobook(4, 0, series="other", title="Other Book"),
]
# No layout in the test DOM: volume cards are stacked 200 px apart (card N at top 200*N).
STACKED_CARDS = r"""
const elementProto = Object.getPrototypeOf(document.createElement('div'));
Object.defineProperty(elementProto, '__rect', {configurable: true,
  get() { const id = Number(this.dataset?.audiobookId || 0); return id ? {top: 200 * id, left: 0, width: 600, height: 180} : this.__ownRect; },
  set(value) { this.__ownRect = value; }});
"""


def test_audiobook_series_ui_is_one_large_scrollable_card_with_volume_labels(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            STACKED_CARDS
            + r"""
            setPage('audiobooks', true); await __tick(300);
            const cards = () => [...document.querySelectorAll('.audiobook-series-card')];
            const view = () => cards().map(card => ({
              head: card.querySelector('.audiobook-series-card-head').innerText.split('\n'),
              volumes: [...card.querySelectorAll('.audiobook-series-scroll .audiobook-card .audiobook-title-row strong')].map(x => x.innerText),
              scroll: card.querySelector('.audiobook-series-scroll').scrollTop}));
            const out = {series: view(),
                         loose: [...document.querySelectorAll('#audiobooksContent .audiobook-card')].filter(c => !c.closest('.audiobook-series-card'))
                           .map(c => c.querySelector('.audiobook-title-row strong').innerText)};
            // The user scrolls the series; the next refresh keeps that position.
            document.querySelector('.audiobook-series-scroll').scrollTop = 123;
            await __settle(PudgeMedia.loadAudio()); await __tick(50);
            out.redrawn = view();
            out.texts = {volumes: tn('media.volumeCount', 3), unfinished: t('media.unfinished', {unread: 2}),
                         labels: [1, 2, 3].map(volume => t('ui.volume', {volume}))};
            return out;
            """,
            lang=lang,
            api={"audiobook_state": {"books": SERIES_BOOKS}},
        )
        out = result.value
        texts = out["texts"]
        # One card for the whole series (title, volume count, unfinished count), volumes labelled by number.
        assert out["series"] == [
            {
                "head": ["狼と香辛料", texts["volumes"], texts["unfinished"]],
                "volumes": texts["labels"],
                "scroll": 200 * 2 - 4,
            }
        ]  # opened at the first unfinished volume
        assert out["loose"] == ["Other Book"]  # a single book keeps its own title, outside any series card
        assert out["redrawn"][0]["scroll"] == 123
    assert en("ui.volume", volume=3) == "Volume 3" and ru("ui.volume", volume=3) == "Том 3"
    rules = css_rules("media.css")
    # A long series scrolls inside its card instead of stretching the page.
    assert rules.get(".audiobook-series-scroll", "max-height") == "430px"
    assert rules.get(".audiobook-series-scroll", "overflow-y") == "auto"
