"""“Episode words” for anime and “Met often, not in deck” on Statistics.

Episode words: the manga “Volume words” dialog for one episode or every episode of a title,
counted from the episode's word unit (``anime:<media_uuid>``, the one Statistics uses), with
Jiten's rate limit and the energy rules respected. Met often: unknown words ranked by how
many times they were really met in the period, from the word units joined with the word
states kept on the Mac (no Jiten request when the page opens).

Invented data only: invented titles, subtitle lines and word ids.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import episode_words, jiten_client, met_words, word_lexicon
from pudge.bridge_methods import PAGE_METHODS
from pudge.config import AppConfig
from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.consumption_words import READY, WordUnits
from pudge.database import Database
from pudge.i18n import catalog
from pudge.job_center import JobCenter
from pudge.light_novels import JitenUnavailableError, LightNovelService, credential_account_key
from pudge.web_app import WebAppApi

TITLE = "Lantern Fox Diner"
# word -> (wordId, readingIndex, meaning, parts of speech, frequency rank)
WORDS = {
    "きつね": (1, 0, "fox", ["n"], 900),
    "あかり": (2, 0, "light", ["n"], 300),
    "たべる": (3, 0, "to eat", ["v1"], 50),
    "みせ": (4, 0, "shop", ["n"], 120),
    "ハルカ": (5, 0, "Haruka", ["name", "fem"], 40000),
    "のれん": (6, 0, "shop curtain", ["n"], 9000),
}
STATES = {1: "new", 2: "mature", 3: "young", 4: "not_in_deck", 5: "new", 6: "new"}
LINES = {
    1: ["きつね あかり", "たべる きつね", "みせ ハルカ"],
    2: ["のれん のれん きつね", "みせ みせ のれん"],
    3: ["ハルカ たべる"],
}


def _srt(lines: list[str]) -> str:
    blocks = []
    for index, line in enumerate(lines):
        blocks.append(f"{index + 1}\n00:00:{index * 10:02},000 --> 00:00:{index * 10 + 8:02},000\n{line}\n")
    return "\n".join(blocks)


class _Study:
    """The part of LightNovelService the word lists use."""

    def __init__(self, *, cached: dict | None = None, named: bool = True) -> None:
        self.parses: list[str] = []
        self.live_calls: list[list[tuple[int, int]]] = []
        self.name_fetches: list[int] = []
        self.cached = dict(cached or {})
        self.lexicon: dict[tuple[int, int], dict] = {}
        self.backend = "jiten"
        self.unavailable_after: int | None = None
        if named:
            self.remember(WORDS)

    def remember(self, words) -> None:
        for spelling, (word_id, reading, meaning, pos, rank) in words.items():
            self.lexicon[(word_id, reading)] = word_lexicon.word_info(
                {
                    "spelling": spelling,
                    "reading": spelling,
                    "meanings": [meaning],
                    "partsOfSpeech": pos,
                    "frequencyRank": rank,
                }
            )

    def settings(self):
        return SimpleNamespace(study_backend=self.backend, jiten_api_key="key")

    def jiten_preparse(self, text, digest=None, *, interactive=True):
        if self.unavailable_after is not None and len(self.parses) >= self.unavailable_after:
            raise JitenUnavailableError("held back", reason="jiten_rate_limited", retry_after=60)
        self.parses.append(text)
        tokens = [
            [{"wordId": WORDS[word][0], "readingIndex": WORDS[word][1]} for word in line.split()]
            for line in text.split("\n")
        ]
        return {"tokens": tokens, "vocabulary": []}

    def jiten_cached_states(self, pairs):
        return {pair: self.cached[pair] for pair in pairs if pair in self.cached}

    def jiten_live_states(self, pairs, force=False):
        self.live_calls.append(list(pairs))
        rows = []
        for word_id, reading in pairs:
            self.cached[(word_id, reading)] = STATES[word_id]
            rows.append({"wordId": word_id, "readingIndex": reading, "normalizedState": STATES[word_id]})
        return rows

    def word_names(self, pairs, *, fetch=0):
        self.name_fetches.append(int(fetch))
        return {pair: self.lexicon[pair] for pair in pairs if pair in self.lexicon}


def _api(tmp_path: Path, study: _Study, *, subtitles=(1, 2), progress: int = 1):
    """An api with three downloaded episodes of one invented anime (episode 3 unprepared)."""
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = {}
    episodes = []
    for number in (1, 2, 3):
        video = tmp_path / f"ep{number}.mkv"
        video.write_bytes(b"")
        subtitle = None
        if number in subtitles:
            subtitle = tmp_path / f"ep{number}.srt"
            subtitle.write_text(_srt(LINES[number]), encoding="utf-8")
        media[str(video)] = ledger.ensure_media(
            kind="anime_episode", title=f"{TITLE} · {number}", aliases=[("video_path", str(video))]
        )
        episodes.append(
            SimpleNamespace(
                video_path=video,
                subtitle_path=subtitle,
                embedded_subtitle_id=None,
                episode=number,
                media_episode_end=None,
                state="watched" if number <= progress else "ready",
                watched_at=None,
            )
        )
    anime = SimpleNamespace(media_id=77, title=TITLE, episodes=3, progress=progress)
    gate = {"reason": None}
    starts: list[int] = []
    api = WebAppApi.__new__(WebAppApi)
    api.logger = logging.getLogger("test.feat_words")
    api.light_novels = study
    api.config = SimpleNamespace(
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.manager = SimpleNamespace(
        db=SimpleNamespace(
            connect=db.connect,
            get_anime=lambda media_id: anime if int(media_id) == 77 else None,
            episodes=lambda media_id: list(episodes),
        ),
        work_scheduler=SimpleNamespace(background_wait_reason=lambda: gate["reason"]),
    )
    api.consumption = SimpleNamespace(anime_episode_media=lambda path: media.get(str(path)))
    api.consumption_statistics = ConsumptionStatistics(db, count_words=True)
    api.job_center = JobCenter(db)
    api._planning_single_file_title = lambda anime: False
    api.statistics_words_start = lambda: starts.append(1) or {"started": True}
    return SimpleNamespace(
        api=api, db=db, ledger=ledger, media=media, gate=gate, starts=starts, episodes=episodes
    )


def _counted(api, episode=None, **options) -> dict:
    api.anime_episode_words_start(77, episode, **options)
    status = episode_words.manager_for(api).wait(77)
    assert status["busy"] is False
    return status


@pytest.fixture(autouse=True)
def _no_jiten_pause(monkeypatch):
    monkeypatch.setattr(jiten_client, "pause_reason", lambda: "")


# ---------------------------------------------------------------- word names kept from parses


def test_the_vocabulary_of_a_parse_is_kept_once_and_marks_proper_names(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "names.sqlite3")
    vocabulary = [
        {
            "wordId": 1,
            "readingIndex": 0,
            "spelling": "狐",
            "reading": "狐[きつね]",
            "meaningsChunks": [["fox"]],
        },
        {
            "wordId": 5,
            "readingIndex": 0,
            "spelling": "ハルカ",
            "reading": "ハルカ",
            "meanings": ["Haruka"],
            "partsOfSpeech": ["name", "fem"],
            "frequencyRank": 40000,
        },
        {"wordId": 0, "readingIndex": 0, "spelling": "x"},
    ]
    assert word_lexicon.remember(conn, vocabulary, digest="parse-1") == 2
    # The same parse read again writes nothing; a later parse fills a missing field.
    assert word_lexicon.remember(conn, vocabulary, digest="parse-1") == 0
    word_lexicon.remember(conn, [{"wordId": 1, "readingIndex": 0, "spelling": "狐", "partsOfSpeech": ["n"]}])
    found = word_lexicon.lookup(conn, [(1, 0), (5, 0), (9, 0)])
    assert set(found) == {(1, 0), (5, 0)}
    assert found[(1, 0)]["reading"] == "きつね" and found[(1, 0)]["meaning"] == "fox"
    assert found[(1, 0)]["is_name"] is False and found[(1, 0)]["pos"] == ["n"]
    assert found[(5, 0)]["is_name"] is True and found[(5, 0)]["frequency_rank"] == 40000


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(cfg)
    service.settings = lambda: SimpleNamespace(jiten_api_key="key", study_backend="jiten", jpdb_api_token="")
    return service


def test_the_service_names_words_and_reads_kept_states_without_asking_jiten(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    sent: list = []
    monkeypatch.setattr(service, "_jiten_request", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(service, "_jiten_get", lambda *a, **k: sent.append(a) or {})
    queued: list = []
    monkeypatch.setattr(
        service, "jiten_word", lambda card, background=False: queued.append((card, background))
    )
    service.remember_vocabulary(
        [{"wordId": 1, "readingIndex": 0, "spelling": "狐", "reading": "きつね", "meanings": ["fox"]}],
        digest="d1",
    )
    # A word the reader opened once is in the word cache: it is named from there.
    account = service.study_provider_capabilities("jiten").get("account_key") or "unconfigured"
    service._word_repository().put(
        account,
        {"wordId": 2, "readingIndex": 0, "spelling": "灯", "reading": "あかり", "meanings": ["light"]},
    )
    names = service.word_names([(1, 0), (2, 0), (3, 0)], fetch=0)
    assert names[(1, 0)]["spelling"] == "狐" and names[(2, 0)]["meaning"] == "light" and (3, 0) not in names
    assert queued == []
    # Asked to: the unnamed word's dictionary entry goes to the paced background queue.
    service.word_names([(1, 0), (3, 0)], fetch=5)
    assert queued == [({"wordId": 3, "readingIndex": 0}, True)]

    key = credential_account_key("jiten", "key")
    service._store_live_states(
        key,
        {
            (1, 0): {"normalizedState": "new", "wordId": 1, "readingIndex": 0},
            (2, 0): {"normalizedState": "mature", "wordId": 2, "readingIndex": 0},
        },
    )
    service._jiten_live_state_cache.clear()  # as after a restart: SQLite only
    assert service.jiten_cached_states([(1, 0), (2, 0), (3, 0)]) == {(1, 0): "new", (2, 0): "mature"}
    # A word just added to a deck here has no known state until Jiten is asked again.
    service.invalidate_jiten_live_state(1, 0)
    assert service.jiten_cached_states([(1, 0), (2, 0)]) == {(2, 0): "mature"}
    assert sent == []
    service.close()


# ---------------------------------------------------------------- Episode words: backend


def test_the_picker_starts_at_the_next_unwatched_episode(tmp_path: Path) -> None:
    world = _api(tmp_path, _Study())
    options = world.api.anime_episode_words_options(77)
    assert options["title"] == TITLE and options["default_episode"] == 2 and options["with_subtitles"] == 2
    assert [(row["episode"], row["subtitles"], row["watched"]) for row in options["episodes"]] == [
        (1, True, True),
        (2, True, False),
        (3, False, False),
    ]


def test_one_episode_reports_coverage_and_its_most_frequent_new_words(tmp_path: Path) -> None:
    study = _Study()
    world = _api(tmp_path, study)
    status = _counted(world.api, 2, limit=5)
    result = status["result"]
    assert status["state"] == "done" and result["ok"] is True and result["scope"] == "episode"
    # のれん ×3 (new), きつね ×1 (new), みせ ×2 (not in deck).
    assert result["tokens"] == 6 and result["unique_words"] == 3 and result["new_unique"] == 3
    assert [(word["spelling"], word["count"], word["meaning"]) for word in result["top_new"]] == [
        ("のれん", 3, "shop curtain"),
        ("みせ", 2, "shop"),
        ("きつね", 1, "fox"),
    ]
    assert result["top_new"][1]["state"] == "not_in_deck"
    assert result["top_new"][0]["word_id"] == 6 and result["top_new"][0]["reading_index"] == 0
    # Jiten was asked for the states in one batch, never word by word.
    assert len(study.live_calls) == 1 and len(study.live_calls[0]) == 3
    # The parse is kept as the episode's word unit: Statistics (and a second look) reuse it.
    units = WordUnits(world.db)
    key = f"anime:{world.media[str(world.episodes[1].video_path)].media_uuid}"
    kept_status, kept = units.lookup([key])[key]
    assert kept_status == READY and [cue[2] for cue in kept["cues"]] == [
        [[6, 0], [6, 0], [1, 0]],
        [[4, 0], [4, 0], [6, 0]],
    ]
    again = _counted(world.api, 2)["result"]
    assert again["tokens"] == 6 and len(study.parses) == 1 and again["episodes_parsed"] == 0
    # The job shows in the journal of long operations.
    jobs = [job for job in world.api.job_center.jobs() if job["kind"] == "episode_words"]
    assert jobs and all(job["state"] == "succeeded" for job in jobs) and TITLE in jobs[0]["title"]


def test_an_episode_without_prepared_subtitles_says_so_and_asks_nothing(tmp_path: Path) -> None:
    study = _Study()
    world = _api(tmp_path, study)
    result = _counted(world.api, 3)["result"]
    assert result["ok"] is False and result["reason"] == "no_subtitles"
    assert study.parses == [] and study.live_calls == []


def test_the_whole_title_counts_every_episode_with_subtitles(tmp_path: Path) -> None:
    study = _Study()
    world = _api(tmp_path, study)
    status = _counted(world.api, None)
    result = status["result"]
    assert result["ok"] is True and result["scope"] == "title"
    assert (result["episodes_total"], result["episodes_counted"], result["episodes_parsed"]) == (3, 2, 2)
    assert result["episodes_without_subtitles"] == [3] and result["episodes_waiting"] == []
    assert status["current"] == status["total"] == 3
    # 12 running words; known あかり ×1, learning たべる ×1.
    assert result["tokens"] == 12 and result["known_share"] == round(1 / 12, 4)
    assert result["known_or_learning_share"] == round(2 / 12, 4)
    # Equally frequent words: the more common one (frequency rank) first, like the manga dialog.
    assert [(word["spelling"], word["count"]) for word in result["top_new"]][:3] == [
        ("みせ", 3),
        ("きつね", 3),
        ("のれん", 3),
    ]
    assert result["top_new"][-1]["spelling"] == "ハルカ" and result["top_new"][-1]["is_name"] is True


def test_energy_rules_hold_back_a_whole_title_until_the_user_says_count_now(tmp_path: Path) -> None:
    study = _Study()
    world = _api(tmp_path, study)
    _counted(world.api, 1)  # one episode is already counted
    assert len(study.parses) == 1
    world.gate["reason"] = "energy_saving"
    result = _counted(world.api, None)["result"]
    # The counted episode is shown; the other waits and is queued for the Statistics job.
    assert result["ok"] is True and result["episodes_counted"] == 1 and result["episodes_waiting"] == [2]
    assert result["paused"] == "energy_saving" and len(study.parses) == 1 and world.starts == [1]
    queued = WordUnits(world.db).due(10)
    assert [unit["key"] for unit in queued] == [
        f"anime:{world.media[str(world.episodes[1].video_path)].media_uuid}"
    ]
    # One episode asked for by the user is one request: the energy rules do not hold it.
    assert _counted(world.api, 2)["result"]["ok"] is True and len(study.parses) == 2
    # "Count now" for the whole title skips the energy rules too.
    study.parses.clear()
    world3 = _api(tmp_path / "forced", study)
    world3.gate["reason"] = "energy_saving"
    forced = _counted(world3.api, None, force=True)["result"]
    assert forced["episodes_counted"] == 2 and forced["episodes_waiting"] == [] and len(study.parses) == 2


def test_jiten_rate_limit_is_never_skipped(tmp_path: Path, monkeypatch) -> None:
    study = _Study()
    world = _api(tmp_path, study)
    monkeypatch.setattr(jiten_client, "pause_reason", lambda: "jiten_rate_limited")
    for options in ({}, {"force": True}):
        result = _counted(world.api, 1, **options)["result"]
        assert result == {
            "ok": False,
            "reason": "paused",
            "scope": "episode",
            "episode": 1,
            "episodes_total": 1,
            "episodes_counted": 0,
            "episodes_parsed": 0,
            "episodes_without_subtitles": [],
            "episodes_waiting": [1],
            "paused": "jiten_rate_limited",
        }
    assert study.parses == [] and study.live_calls == []
    # Jiten holding a request back in the middle: what was counted is shown, the rest waits.
    monkeypatch.setattr(jiten_client, "pause_reason", lambda: "")
    study.unavailable_after = 1
    result = _counted(world.api, None, force=True)["result"]
    assert result["ok"] is True and result["episodes_counted"] == 1 and result["episodes_waiting"] == [2]
    assert result["paused"] == "jiten_rate_limited" and len(study.parses) == 1


def test_word_lists_need_jiten(tmp_path: Path) -> None:
    study = _Study()
    study.backend = "jpdb"
    world = _api(tmp_path, study)
    assert _counted(world.api, 1)["result"] == {"ok": False, "reason": "jiten_only"}
    assert study.parses == []


def test_words_nothing_named_are_named_on_demand(tmp_path: Path, monkeypatch) -> None:
    study = _Study(named=False)
    world = _api(tmp_path, study)
    result = _counted(world.api, 2, limit=2)["result"]
    assert result["unnamed"] == 2 and [word["spelling"] for word in result["top_new"]] == ["", ""]
    # The dialog then asks for the names of the rows it shows.
    study.remember({"のれん": WORDS["のれん"]})
    names = world.api.study_word_names([[6, 0], [4, 0], ["x", 0]])
    assert names["missing"] == 1 and [(row["word_id"], row["spelling"]) for row in names["words"]] == [
        (6, "のれん")
    ]
    assert study.name_fetches[-1] == 2
    # While Jiten's limit is reached nothing more is queued.
    monkeypatch.setattr(jiten_client, "pause_reason", lambda: "jiten_rate_limited")
    assert (
        world.api.study_word_names([[4, 0]])["paused"] == "jiten_rate_limited" and study.name_fetches[-1] == 0
    )


# ---------------------------------------------------------------- Met often, not in deck: backend


def _noon(days_ago: int, now: float) -> float:
    local = datetime.fromtimestamp(now).replace(hour=12, minute=0, second=0, microsecond=0)
    return (local - timedelta(days=days_ago)).timestamp()


def _session(ledger, media, kind: str, start: float, chunks, payload=None) -> None:
    session = ledger.start_session(kind=kind, media_uuid=media.media_uuid, started_at_utc=start)
    at = start
    for seconds, locator0, locator1 in chunks:
        ledger.append_interval(
            session_id=session,
            kind=kind,
            interval_start_utc=at,
            interval_end_utc=at + seconds,
            elapsed_monotonic_ms=seconds * 1000.0,
            media=[
                {
                    "media_uuid": media.media_uuid,
                    "source_revision": "r",
                    "role": "primary",
                    "locator_start": locator0,
                    "locator_end": locator1,
                }
            ],
            payload=payload or {},
        )
        at += seconds
    ledger.end_session(session, ended_at_utc=at)


def _watch(ledger, episode, start: float, positions: tuple[float, float]) -> None:
    a, b = positions
    chunks = []
    while a < b:
        chunks.append((10.0, {"position_seconds": a}, {"position_seconds": min(b, a + 10.0)}))
        a += 10.0
    _session(ledger, episode, "anime", start, chunks, {"duration_seconds": 1440})


CHAPTER = "c" * 64
# Light-novel chapter words: (character offset, wordId, readingIndex)
CHAPTER_WORDS = [(0, 6, 0), (5, 6, 0), (10, 4, 0), (40, 2, 0)]


def _met(tmp_path: Path, study: _Study):
    now = time.time()
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    episode1 = ledger.ensure_media(
        kind="anime_episode", title=f"{TITLE} · 1", aliases=[("video_path", str(tmp_path / "e1.mkv"))]
    )
    episode2 = ledger.ensure_media(
        kind="anime_episode", title=f"{TITLE} · 2", aliases=[("video_path", str(tmp_path / "e2.mkv"))]
    )
    volume = ledger.ensure_media(
        kind="manga",
        title="Paper Kite Alley 1",
        aliases=[("local", "5")],
        current_library_kind="manga",
        current_library_id="5",
    )
    book = ledger.ensure_media(
        kind="light_novel",
        title="Salt Road Letters",
        aliases=[("local", "9")],
        current_library_kind="light_novel",
        current_library_id="9",
    )
    units = WordUnits(db)
    keys = {
        f"anime:{episode1.media_uuid}": ("anime", {"media_uuid": episode1.media_uuid}),
        f"anime:{episode2.media_uuid}": ("anime", {"media_uuid": episode2.media_uuid}),
        f"manga:{volume.media_uuid}:001.jpg": ("manga", {"media_uuid": volume.media_uuid, "page_index": 1}),
    }
    units.enqueue(keys)
    # Episode 1: きつね あかり | きつね たべる | (not watched) ハルカ
    units.store(
        f"anime:{episode1.media_uuid}",
        {"cues": [[0.0, 20.0, [[1, 0], [2, 0]]], [30.0, 50.0, [[1, 0], [3, 0]]], [300.0, 310.0, [[5, 0]]]]},
    )
    units.store(f"anime:{episode2.media_uuid}", {"cues": [[0.0, 20.0, [[1, 0], [5, 0], [5, 0]]]]})
    units.store(f"manga:{volume.media_uuid}:001.jpg", {"words": [[6, 0], [1, 0], [5, 0]]})
    # Long ago (outside the last 7 days): episode 1 once.
    _watch(ledger, episode1, _noon(40, now), (0.0, 60.0))
    # This week: episode 1 twice, episode 2 once, the manga page, a light-novel passage.
    _watch(ledger, episode1, _noon(3, now), (0.0, 60.0))
    _watch(ledger, episode1, _noon(2, now), (0.0, 60.0))
    _watch(ledger, episode2, _noon(2, now) + 3600, (0.0, 30.0))
    page = {"page_index": 1, "page_id": "001.jpg"}
    _session(ledger, volume, "manga", _noon(1, now), [(20.0, page, page)], {"page_count": 10})
    _session(
        ledger,
        book,
        "light_novel",
        _noon(1, now) + 3600,
        [
            (
                20.0,
                {"chapter_key": CHAPTER, "character_offset": 0},
                {"chapter_key": CHAPTER, "character_offset": 20},
            )
        ],
        {"character_count": 60},
    )
    statistics = ConsumptionStatistics(
        db, count_words=True, ln_words=lambda chapter: CHAPTER_WORDS if chapter == CHAPTER else None
    )
    return SimpleNamespace(
        met=met_words.MetWords(statistics, study), db=db, ledger=ledger, statistics=statistics, now=now
    )


def test_unknown_words_are_ranked_by_how_often_they_were_met_with_where(tmp_path: Path) -> None:
    # The states this Mac keeps; word 6 (のれん) was never looked up.
    study = _Study(
        cached={(1, 0): "new", (2, 0): "mature", (3, 0): "young", (4, 0): "not_in_deck", (5, 0): "new"}
    )
    world = _met(tmp_path, study)
    result = world.met.query({"period": "7d"})
    assert result["ok"] is True
    # きつね: episode 1 twice ×2, episode 2 ×1, manga ×1 = 6; ハルカ: episode 2 ×2 + manga ×1 = 3
    # (its line in episode 1 was never on screen); みせ: the passage read ×1.
    assert [(word["spelling"], word["count"], word["state"]) for word in result["words"]] == [
        ("きつね", 6, "new"),
        ("ハルカ", 3, "new"),
        ("みせ", 1, "not_in_deck"),
    ]
    fox = result["words"][0]
    assert fox["sources"] == [
        {"title": TITLE, "kind": "anime_episode", "count": 5},
        {"title": "Paper Kite Alley 1", "kind": "manga", "count": 1},
    ]
    assert fox["meaning"] == "fox" and fox["word_id"] == 1 and fox["more_sources"] == 0
    assert result["words"][2]["sources"] == [
        {"title": "Salt Road Letters", "kind": "light_novel", "count": 1}
    ]
    assert result["candidates"] == 3 and result["has_more"] is False
    # のれん has no kept state: it is not guessed, only reported.
    assert result["unchecked"] == 1 and result["names_marked"] is True
    # Opening the page asked Jiten for nothing, and fetched no dictionary entry.
    assert study.live_calls == [] and set(study.name_fetches) == {0}
    # All time also counts the watch of 40 days ago.
    everything = world.met.query({"period": "all"})
    assert everything["words"][0]["count"] == 8


def test_checking_asks_jiten_in_one_batch_and_finds_the_unchecked_words(tmp_path: Path) -> None:
    study = _Study(
        cached={(1, 0): "new", (2, 0): "mature", (3, 0): "young", (4, 0): "not_in_deck", (5, 0): "new"}
    )
    world = _met(tmp_path, study)
    result = world.met.query({"period": "7d"}, check=True)
    # One request for the unchecked word and the words about to be shown — not one per word.
    assert len(study.live_calls) == 1 and (6, 0) in study.live_calls[0]
    assert result["unchecked"] == 0 and result["checked"] == len(study.live_calls[0])
    assert [(word["spelling"], word["count"]) for word in result["words"]] == [
        ("きつね", 6),
        ("ハルカ", 3),
        ("のれん", 3),
        ("みせ", 1),
    ]


def test_a_word_learned_since_leaves_the_list_after_the_check(tmp_path: Path) -> None:
    study = _Study(
        cached={(1, 0): "new", (5, 0): "new", (6, 0): "new", (4, 0): "new", (2, 0): "new", (3, 0): "new"}
    )
    world = _met(tmp_path, study)
    stale = world.met.query({"period": "7d"})
    assert "あかり" in [word["spelling"] for word in stale["words"]]
    checked = world.met.query({"period": "7d"}, check=True)
    assert [word["spelling"] for word in checked["words"]] == ["きつね", "ハルカ", "のれん", "みせ"]


def test_proper_names_can_be_hidden_and_the_list_is_paged(tmp_path: Path) -> None:
    study = _Study(cached={pair: STATES[pair[0]] for pair in [(n, 0) for n in range(1, 7)]})
    world = _met(tmp_path, study)
    hidden = world.met.query({"period": "7d"}, hide_names=True)
    assert [word["spelling"] for word in hidden["words"]] == ["きつね", "のれん", "みせ"]
    assert hidden["hidden_names"] == 1
    first = world.met.query({"period": "7d"}, limit=2)
    assert [word["spelling"] for word in first["words"]] == ["きつね", "ハルカ"] and first["has_more"] is True
    more = world.met.query({"period": "7d"}, limit=4)
    assert len(more["words"]) == 4 and more["has_more"] is False
    # The filters of the page apply: only anime.
    anime = world.met.query({"period": "7d", "kind": "anime"})
    assert [(word["spelling"], word["count"]) for word in anime["words"]] == [("きつね", 5), ("ハルカ", 2)]


def test_jiten_pause_keeps_the_check_from_asking(tmp_path: Path, monkeypatch) -> None:
    study = _Study(cached={(1, 0): "new"})
    world = _met(tmp_path, study)
    monkeypatch.setattr(jiten_client, "pause_reason", lambda: "jiten_rate_limited")
    result = world.met.query({"period": "7d"}, check=True)
    assert study.live_calls == [] and result["paused"] == "jiten_rate_limited"
    assert [word["spelling"] for word in result["words"]] == ["きつね"] and result["unchecked"] == 5


def test_units_not_parsed_yet_are_reported_not_guessed(tmp_path: Path) -> None:
    study = _Study(cached={(1, 0): "new"})
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    episode = ledger.ensure_media(
        kind="anime_episode", title=f"{TITLE} · 1", aliases=[("video_path", str(tmp_path / "e1.mkv"))]
    )
    _watch(ledger, episode, _noon(1, time.time()), (0.0, 60.0))
    met = met_words.MetWords(ConsumptionStatistics(db, count_words=True), study)
    assert met.query({"period": "7d"}) == {"ok": False, "reason": "no_words", "pending_units": 1}
    study.backend = "jpdb"
    assert met.query({"period": "7d"}) == {"ok": False, "reason": "jiten_only"}


def test_the_api_serves_the_card(tmp_path: Path) -> None:
    study = _Study(cached={(1, 0): "new", (5, 0): "new"})
    world = _met(tmp_path, study)
    api = WebAppApi.__new__(WebAppApi)
    api.logger = logging.getLogger("test.feat_words")
    api.light_novels = study
    api.consumption_statistics = world.statistics
    result = api.consumption_statistics_met_words({"period": "7d"}, 1, True, False)
    assert [word["spelling"] for word in result["words"]] == ["きつね"] and result["hidden_names"] == 0
    assert result["has_more"] is False and result["candidates"] == 2


# ---------------------------------------------------------------- strings, bridge, wiring


def test_strings_and_bridge_methods_exist() -> None:
    en, ru = catalog("en"), catalog("ru")
    keys = [key for key in en if key.startswith(("episodeWords.", "wordList.", "stats.met."))]
    assert len(keys) > 40
    for key in [*keys, "action.episodeWords", "action.episodeWordsAll"]:
        assert en[key] and ru[key] and en[key] != ru[key], key
    assert {
        "anime_episode_words_options",
        "anime_episode_words_start",
        "anime_episode_words_status",
        "anime_episode_words_cancel",
        "study_word_names",
        "consumption_statistics_met_words",
    } <= PAGE_METHODS


# ---------------------------------------------------------------- Episode words: the dialog


def _anime(**values):
    return {
        "media_id": 77,
        "title": TITLE,
        "episodes": 3,
        "progress": 1,
        "media_status": "FINISHED",
        "site_url": "https://anilist.co/anime/77",
        "local": None,
        **values,
    }


OPTIONS = {
    "media_id": 77,
    "title": TITLE,
    "default_episode": 2,
    "with_subtitles": 2,
    "episodes": [
        {"episode": 1, "episode_end": None, "subtitles": True, "watched": True},
        {"episode": 2, "episode_end": None, "subtitles": True, "watched": False},
        {"episode": 3, "episode_end": None, "subtitles": False, "watched": False},
    ],
}
RESULT = {
    "ok": True,
    "backend": "jiten",
    "scope": "episode",
    "episode": 2,
    "tokens": 6,
    "unique_words": 3,
    "new_unique": 3,
    "known_share": 0.5,
    "known_or_learning_share": 0.75,
    "episodes_total": 1,
    "episodes_counted": 1,
    "episodes_without_subtitles": [],
    "episodes_waiting": [],
    "paused": "",
    "unnamed": 1,
    "top_new": [
        {
            "word_id": 6,
            "reading_index": 0,
            "spelling": "のれん",
            "reading": "のれん",
            "meaning": "shop curtain",
            "count": 3,
        },
        {"word_id": 4, "reading_index": 0, "spelling": "", "reading": "", "meaning": "", "count": 2},
    ],
}
BRIDGE = r"""
__api.anime_episode_words_options = async () => (OPTIONS);
window.__status = (STATUS);
__api.anime_episode_words_start = async () => window.__status;
__api.anime_episode_words_status = async () => window.__status;
__api.study_decks = async () => [{id: '12', name: 'Mining'}];
__api.study_deck_preference = async () => ({deck_id: '12'});
__api.study_action = async () => ({ok: true});
__api.study_word_names = async () => ({ok: true, paused: '', missing: 0, words: [{word_id: 4, reading_index: 0, spelling: 'みせ', reading: 'みせ', meaning: 'shop'}]});
"""


def _status(result=None, **values):
    return {
        "media_id": 77,
        "job_id": "j1",
        "episode": 2,
        "state": "done",
        "busy": False,
        "current": 1,
        "total": 1,
        "progress": 1.0,
        "error": "",
        "result": result if result is not None else RESULT,
        **values,
    }


def _bridge(status) -> str:
    return BRIDGE.replace("OPTIONS", json.dumps(OPTIONS)).replace("STATUS", json.dumps(status))


def test_the_anime_menu_offers_episode_words_for_downloaded_titles(page) -> None:
    menu = r"""
showAnimeMenu(77, false, 10, 10);
return [...document.querySelectorAll('#contextMenu button')].map(b => [b.dataset.contextAction, b.textContent]);
"""
    none = page.run(menu, state={"current": [_anime(local_count=0)]}).value
    assert not [row for row in none if str(row[0]).startswith("episode-words")]
    some = dict(page.run(menu, state={"current": [_anime(local_count=2)]}).value)
    assert some["episode-words"] == "Episode words…" and some["episode-words-all"] == "Words of all episodes"
    ru = dict(page.run(menu, lang="ru", state={"current": [_anime(local_count=2)]}).value)
    assert ru["episode-words"] == "Слова серии…" and ru["episode-words-all"] == "Слова всех серий"


def test_the_dialog_starts_at_the_next_episode_shows_coverage_and_adds_checked_words(page) -> None:
    result = page.run(
        r"""
showAnimeMenu(77, false, 10, 10);
await __click('[data-context-action="episode-words"]');
for (let i = 0; i < 4; i++) await __flush();
const out = {title: __text('#modalTitle'), pick: document.querySelector('#episodeWordsPick').value,
  options: [...document.querySelectorAll('#episodeWordsPick option')].map(o => o.textContent),
  summary: __text('[data-episode-words-summary]'),
  expected: t('manga.youKnowOfTheRunning', {value: '50%', value2: '75%', unique_words: 3, new_unique: 3}),
  rows: [...document.querySelectorAll('#modalBody [data-word-list-item]')].map(box => [box.closest('label').querySelector('.title').textContent, box.checked])};
await __input('#modalBody [data-word-list-item="1"]', false);
await __click('#modalBody [data-word-list-add]'); for (let i = 0; i < 4; i++) await __flush();
out.progress = __text('#modalBody [data-word-list-progress]');
out.left = [...document.querySelectorAll('#modalBody [data-word-list-item]')].map(box => box.closest('label').querySelector('.title').textContent);
return out;
""",
        state={"current": [_anime(local_count=2)]},
        before=_bridge(_status()),
    )
    out = result.value
    assert TITLE in out["title"] and out["pick"] == "2"
    assert out["options"] == [
        "Episode 1",
        "Episode 2",
        "Episode 3 — subtitles not prepared yet",
        "All episodes (with subtitles: 2)",
    ]
    assert out["summary"] == out["expected"]
    # The word nothing had named was named on demand (study_word_names), both are checked.
    assert out["rows"] == [["のれん", True], ["みせ", True]]
    assert result.calls_of("anime_episode_words_start") == [[77, 2, 40, False]]
    assert result.calls_of("study_word_names") == [[[[4, 0]]]]
    assert result.calls_of("study_action") == [
        [
            {
                "backend": "jiten",
                "action": "add",
                "word_id": 6,
                "reading_index": 0,
                "deck_id": "12",
                "id_namespace": "jiten",
            }
        ]
    ]
    assert out["progress"] == "Added: 1" and out["left"] == ["みせ"]


def test_all_episodes_show_progress_then_what_waits_and_count_now(page) -> None:
    waiting = {
        **RESULT,
        "scope": "title",
        "episode": None,
        "episodes_total": 3,
        "episodes_counted": 1,
        "episodes_without_subtitles": [3],
        "episodes_waiting": [2],
        "paused": "energy_saving",
    }
    running = _status(None, state="running", busy=True, current=1, total=3, progress=1 / 3, episode=None)
    running["result"] = None
    result = page.run(
        r"""
showAnimeMenu(77, false, 10, 10);
await __click('[data-context-action="episode-words-all"]');
for (let i = 0; i < 4; i++) await __flush();
const out = {pick: document.querySelector('#episodeWordsPick').value, running: __text('.episode-words-job'),
  bar: Number(document.querySelector('.episode-words-job progress').value), cancel: !!document.querySelector('[data-episode-words-cancel]')};
window.__status = DONE;
await __tick(800); for (let i = 0; i < 4; i++) await __flush();
out.notes = __text('.episode-words-notes');
await __click('[data-episode-words-force]'); for (let i = 0; i < 4; i++) await __flush();
return out;
""".replace("DONE", json.dumps(_status(waiting, episode=None))),
        state={"current": [_anime(local_count=3)]},
        before=_bridge(running),
    )
    out = result.value
    assert (
        out["pick"] == "all"
        and "episode 1 of 3" in out["running"]
        and out["bar"] == 33
        and out["cancel"] is True
    )
    assert "Episodes counted: 1 of 3." in out["notes"] and "Subtitles not prepared yet: 3." in out["notes"]
    assert "Not counted yet: 2 (energy saving is on)" in out["notes"]
    assert result.calls_of("anime_episode_words_start") == [[77, None, 40, False], [77, None, 40, True]]


def test_an_unprepared_episode_and_a_jiten_limit_are_explained(page) -> None:
    code = r"""
showAnimeMenu(77, false, 10, 10);
await __click('[data-context-action="episode-words"]');
for (let i = 0; i < 4; i++) await __flush();
return {text: __text('#modalBody .empty'), force: !!document.querySelector('[data-episode-words-force]')};
"""
    state = {"current": [_anime(local_count=2)]}
    missing = page.run(
        code, state=state, before=_bridge(_status({"ok": False, "reason": "no_subtitles"}))
    ).value
    assert missing["text"] == "Japanese subtitles of this episode are not prepared yet."
    limited = {
        "ok": False,
        "reason": "paused",
        "paused": "jiten_rate_limited",
        "scope": "episode",
        "episodes_waiting": [2],
    }
    paused = page.run(code, lang="ru", state=state, before=_bridge(_status(limited))).value
    assert "лимит запросов к Jiten пока исчерпан" in paused["text"]
    assert paused["force"] is False  # Jiten's limit cannot be skipped


# ---------------------------------------------------------------- Met often: the Statistics card


def _statistics_payload():
    return {
        "schema": 1,
        "timezone": {"label": "UTC"},
        "summary": {"total_seconds": 600, "active_days": 1, "sessions": 1, "works": 1},
        "breakdown": {"anime": 600.0},
        "volume": {"by_kind": {}, "by_media": {}},
        "days": [{"day": "2026-10-01", "seconds": 600.0, "breakdown": {"anime": 600.0}, "event_count": 1}],
        "works": [],
        "journal": [],
        "journal_total": 0,
        "media_options": [],
        "highlights": None,
    }


def _word(word_id, spelling, count, **values):
    return {
        "word_id": word_id,
        "reading_index": 0,
        "spelling": spelling,
        "reading": spelling,
        "meaning": f"meaning {word_id}",
        "count": count,
        "is_name": False,
        "state": "new",
        "sources": [{"title": TITLE, "kind": "anime_episode", "count": count}],
        "more_sources": 0,
        **values,
    }


def _card(words, **values):
    return {
        "ok": True,
        "backend": "jiten",
        "words": words,
        "candidates": 120,
        "has_more": True,
        "unchecked": 0,
        "checked": 0,
        "pending_units": 0,
        "hidden_names": 0,
        "names_marked": True,
        "unnamed": 0,
        "paused": "",
        **values,
    }


STATISTICS = r"""
__api.consumption_statistics_query = async () => (PAYLOAD);
window.__cards = CARDS;
__api.consumption_statistics_met_words = async (scope, limit, hideNames, check) => window.__cards[check ? 'checked' : (hideNames ? 'hidden' : 'first')];
__api.study_decks = async () => [{id: '12', name: 'Mining'}];
__api.study_deck_preference = async () => ({deck_id: '12'});
__api.study_action = async () => ({ok: true});
setPage('statistics', true); for (let i = 0; i < 8; i++) await __tick(50);
const rows = () => [...document.querySelectorAll('#statsMetWords [data-word-list-row]')].map(row => [row.querySelector('.title').textContent, row.querySelector('input').checked, row.querySelector('.word-list-sources')?.textContent || '']);
"""


def _statistics(page, code, cards, **options):
    script = STATISTICS.replace("PAYLOAD", json.dumps(_statistics_payload())).replace(
        "CARDS", json.dumps(cards)
    )
    return page.run(script + code, **options)


def test_the_card_lists_words_with_sources_and_checks_states_in_one_batch(page) -> None:
    first = _card([_word(1, "きつね", 6), _word(2, "あかり", 4)], unchecked=7)
    checked = _card([_word(1, "きつね", 6), _word(6, "のれん", 3)], checked=9)
    result = _statistics(
        page,
        r"""
return {title: __text('#statsMetWords .stats-met-head strong'), rows: rows(), summary: __text('#statsMetWords'),
  inOverview: !!document.querySelector('#statsBody .stats-shell > #statsMetWords')};
""",
        {"first": first, "checked": checked, "hidden": first},
    )
    out = result.value
    assert out["title"] == "Met often, not in deck" and out["inOverview"] is True
    # Nothing is ticked by default; each word says where it was met.
    assert out["rows"] == [
        ["きつね", False, f"{TITLE} ×6"],
        ["のれん", False, f"{TITLE} ×3"],
    ]
    assert "Shown: 2 of 120" in out["summary"]
    # The page period goes to the card; the first call asks Jiten for nothing, then one check.
    calls = result.calls_of("consumption_statistics_met_words")
    assert [call[1:] for call in calls] == [[50, False, False], [50, False, True]]
    assert calls[0][0]["period"] == "30d"


def test_show_more_hide_names_and_add_checked_words(page) -> None:
    words = [_word(1, "きつね", 6), _word(5, "ハルカ", 3, is_name=True), _word(6, "のれん", 3)]
    first = _card(words)
    hidden = _card([words[0], words[2]], hidden_names=1)
    result = _statistics(
        page,
        r"""
const out = {};
await __click('#statsMetWords [data-met-words-more]'); for (let i = 0; i < 4; i++) await __flush();
await __input('#statsMetHideNames', true); for (let i = 0; i < 4; i++) await __flush();
out.hidden = rows().map(row => row[0]); out.note = __text('#statsMetWords .stats-met-notes');
out.stored = localStorage.getItem('pudge.metWords.hideNames');
await __input('#statsMetWords [data-word-list-item="1"]', true);
await __click('#statsMetWords [data-word-list-add]'); for (let i = 0; i < 4; i++) await __flush();
out.left = rows().map(row => row[0]); out.text = __text('#statsMetWords');
return out;
""",
        {"first": first, "checked": first, "hidden": hidden},
    )
    out = result.value
    assert (
        out["hidden"] == ["きつね", "のれん"]
        and "proper names hidden: 1" in out["note"]
        and out["stored"] == "1"
    )
    calls = [call[1:] for call in result.calls_of("consumption_statistics_met_words")]
    assert [50, False, False] in calls and [100, False, False] in calls and [100, True, False] in calls
    assert result.calls_of("study_action") == [
        [
            {
                "backend": "jiten",
                "action": "add",
                "word_id": 6,
                "reading_index": 0,
                "deck_id": "12",
                "id_namespace": "jiten",
            }
        ]
    ]
    assert out["left"] == ["きつね"] and "Shown: 1 of 119" in out["text"]


def test_the_card_explains_when_there_is_nothing_to_show(page) -> None:
    counting = {"ok": False, "reason": "no_words", "pending_units": 3}
    out = _statistics(
        page,
        r"return {text: __text('#statsMetWords'), list: !!document.querySelector('#statsMetWords [data-met-words-list]')};",
        {"first": counting, "checked": counting, "hidden": counting},
    )
    assert "still counting 3 items" in out.value["text"] and out.value["list"] is False
    assert len(out.calls_of("consumption_statistics_met_words")) == 1
    ru = _statistics(
        page,
        r"return __text('#statsMetWords');",
        {"first": {"ok": False, "reason": "jiten_only"}, "checked": {}, "hidden": {}},
        lang="ru",
    )
    assert "Часто встречались, но не в колоде" in ru.value and "только с Jiten" in ru.value
