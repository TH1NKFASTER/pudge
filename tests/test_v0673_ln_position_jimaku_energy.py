"""v0.6.73: light-novel reader width and position, Jimaku rate limits, subtitle-job energy
throttle (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import pytest

from css_support import css_rules
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manager import AnimeManager, _subtitle_retry_is_network_error, _subtitle_retry_is_rate_limit, err
from pudge.providers.jimaku import JimakuClient, JimakuError
from t71g19_support import LN_PAIRED, OPEN

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    return cfg


# Plain reading (no audiobook): book 9 reopens at half of chapter 0; chapter 0 is still
# being parsed by Jiten when it opens. The reader scroll box is 2000 px tall, 500 px visible.
READING = (
    LN_PAIRED
    + r"""
window.AUDIO.playing = false;
const openBook = __api.light_novel_open, chapter = __api.light_novel_chapter;
__api.light_novel_open = async id => ({...(await openBook(id)), current_offset: 0.5, paired_audio: null,
  chapters: [{chapter_index: 0, title: 'One'}, {chapter_index: 1, title: 'Two'}]});
__api.light_novel_chapter = async (id, index) => ({...(await chapter(id, index)), parsing: index === 0});
__api.light_novel_chapter_parse_status = async () => ({ready: false});
__api.light_novel_bookmark = async (id, chapter, offset, source) => ({current_chapter: chapter, current_offset: offset});
"""
)
OPEN_READING = r"""
const scroller = $('lnReaderScroll'); scroller.scrollHeight = 2000; scroller.clientHeight = 500;
await __settle(openLightNovel(9)); await __tick(100);
const scrollTo = top => { scroller.scrollTop = top; scroller.dispatchEvent(new Event('scroll')); };
const bookmarks = () => __apiCalls.filter(call => call.name === 'light_novel_bookmark').map(call => call.args);
"""


def test_v0673_reader_reopens_where_it_was_left_and_bookmarks_while_reading(page) -> None:
    result = page.run(
        OPEN_READING
        + """
        const out = {restored: scroller.scrollTop};
        await __tick(19000); out.early = bookmarks();
        scrollTo(1200);                                  // reading on postpones the bookmark
        await __tick(15000); out.postponed = bookmarks();
        await __tick(6000); out.saved = bookmarks();
        return out;
        """,
        before=READING,
    )
    out = result.value
    assert out["restored"] == 750  # offset 0.5 of the 1500 px that scroll
    assert out["early"] == [] and out["postponed"] == []
    assert out["saved"] == [[9, 0, 0.8, "auto"]]


def test_v0673_changing_chapter_bookmarks_the_new_chapter_and_stops_the_old_parse_check(page) -> None:
    result = page.run(
        OPEN_READING
        + """
        await __tick(1000);
        scrollTo(900);                                   // a bookmark for chapter 0 is pending
        await __settle(loadLightNovelChapter(1, 0));
        const since = __apiCalls.length;
        await __tick(21000);
        return {bookmarks: bookmarks(), parseChecks: __apiCalls.slice(since).filter(call => call.name === 'light_novel_chapter_parse_status').map(call => call.args)};
        """,
        before=READING,
    )
    assert [args[:2] + args[3:] for args in result.value["bookmarks"]] == [[9, 1, "auto"]]
    assert result.value["parseChecks"] == []
    assert [9, 0] in result.calls_of("light_novel_chapter_parse_status")  # chapter 0 was being checked


def test_v0673_reader_width_goes_up_to_2400_px(page) -> None:
    result = page.run(
        OPEN
        + """
        const input = $('lnrWidth');
        return {input: [input.type, input.min, input.max], width: $('lnReader').style.getPropertyValue('--ln-content-width')};
        """,
        before=LN_PAIRED,
        api={"light_novel_state": {"version": 1, "settings": {"reader_width": 2200}, "books": []}},
    )
    assert result.value == {"input": ["number", "360", "2400"], "width": "2200px"}
    rules = css_rules("index.html")
    # The text column is as wide as the setting (no fixed 900 px centring padding).
    assert rules.get(".ln-reader", "max-width") == "var(--ln-content-width,900px)"
    assert rules.get(".ln-reader-scroll", "padding") == "42px 24px 100px"


def test_v0673_word_popup_without_reading_tools_shows_reading_and_study_state(page) -> None:
    # The built-in popup (used when the reading tools are unavailable) shows the reading
    # as furigana and the study state in words, not a raw "State: known".
    for lang in ("en", "ru"):
        result = page.run(
            OPEN
            + """
            AUDIO.playing = false;
            delete PudgeReadingTools.study.open;
            const node = wordNodes()[4];                       // 話した, learning (Jiten's Young)
            await showLnPopup(node.dataset.lnToken, node); await __tick(10);
            const pop = $('lnStudyPop');
            return {open: pop.classList.contains('open'), reading: pop.querySelector('.reading').innerHTML,
                    state: pop.querySelector('.subtle').textContent, label: t('reading.state.young')};
            """,
            lang=lang,
            before=LN_PAIRED,
        )
        out = result.value
        assert out["open"] is True
        assert out["reading"] == "<ruby>話<rt>はな</rt></ruby>す"
        assert out["state"].startswith(out["label"]) and "State" not in out["state"]


def test_v0673_word_popup_furigana_covers_every_kanji_of_the_reading(page) -> None:
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        delete PudgeReadingTools.study.open;
        const node = wordNodes()[2];                           // 大人, reading 大人[おとな]
        await showLnPopup(node.dataset.lnToken, node); await __tick(10);
        return $('lnStudyPop').querySelector('.reading').innerHTML;
        """,
        before=LN_PAIRED,
    )
    assert result.value == "<ruby>大人<rt>おとな</rt></ruby>"


def test_v0673_reader_width_accepts_wide_displays(tmp_path: Path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    saved = service.save_settings({"reader_width": 2200})
    assert saved["reader_width"] == 2200
    saved = service.save_settings({"reader_width": 9999})
    assert saved["reader_width"] == 2400


def test_v0673_rate_limit_helpers_recognize_real_httpx_message() -> None:
    detail = "Client error '429 Too Many Requests' for url 'https://jimaku.cc/api/entries/search'"
    assert _subtitle_retry_is_rate_limit(detail)
    assert _subtitle_retry_is_network_error(detail)
    assert _subtitle_retry_is_rate_limit("Jimaku rate limited (429); retry in 8 min")


def test_v0673_jimaku_429_creates_shared_cooldown_without_rehitting(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, request=request)

    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path / "cache")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(JimakuError) as exc:
        client.search_entries(query="Rate Limited Anime")
    message = str(exc.value)
    assert "rate limited (429)" in message
    assert "retry in" in message
    assert "https://" not in message
    assert calls == 1

    def should_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Jimaku network was hit during shared cooldown")

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(should_not_run))
    with pytest.raises(JimakuError):
        client.search_entries(query="Rate Limited Anime")
    client.close()
    assert (tmp_path / "cache" / "jimaku-api" / "rate-limit.json").is_file()


def test_v0673_jimaku_429_uses_stale_nonempty_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=[{"id": 7, "name": "Cached", "anilist_id": 77}], request=request)
        return httpx.Response(429, headers={"Retry-After": "60"}, request=request)

    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path / "cache", cache_ttl_seconds=0.01)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    first = client.search_entries(query="Cached")
    assert first and first[0].id == 7
    time.sleep(0.02)
    second = client.search_entries(query="Cached")
    client.close()
    assert second and second[0].id == 7
    assert calls == 2


def test_v0673_defer_does_not_increment_attempts(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Anime - 01.mkv"
    video.write_bytes(b"video")
    db.queue_subtitle_job(video, 1, 1)
    with db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET attempts=4,state='processing' WHERE video_path=?", (str(video),)
        )
    db.defer_subtitle_job(video, "Jimaku rate limited (429); retry in 10 min", 600)
    row = next(row for row in db.subtitle_jobs() if row["video_path"] == str(video))
    assert int(row["attempts"]) == 4
    assert row["state"] == "pending"
    assert "rate limited" in row["last_error"]


def _queue(manager: AnimeManager, tmp_path: Path, name: str, *, migration: bool) -> Path:
    # No video file: a job that runs is postponed with "video not available yet".
    video = tmp_path / f"{name} - 01.mkv"
    manager.db.queue_subtitle_job(video, 1, 1)
    if migration:
        with manager.db.connect() as conn:
            conn.execute(
                "UPDATE subtitle_jobs SET last_error=? WHERE video_path=?",
                (err("jobs.requeueSyncUpdate"), str(video)),
            )
    return video


def _jobs(manager: AnimeManager) -> dict[str, tuple[str, int]]:
    return {
        Path(row["video_path"]).stem: (str(row["last_error"]), int(row["attempts"] or 0))
        for row in manager.db.subtitle_jobs()
    }


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_v0673_resync_jobs_wait_while_regular_subtitle_work_runs(tmp_path: Path) -> None:
    # Jobs re-queued by the subtitle-sync update are energy-throttled: none runs next to
    # regular jobs, and at most one per pass when only they are due.
    cfg = _cfg(tmp_path)
    cfg.agent.subtitle_poll_minutes = 10
    manager = AnimeManager(cfg, log=lambda _message: None)
    for name, migration in (("A", False), ("B", True), ("C", True)):
        _queue(manager, tmp_path, name, migration=migration)
    started = time.time()
    log = _Records()
    manager.logger.addHandler(log)
    try:
        manager.process_subtitle_jobs(limit=3)
    finally:
        manager.logger.removeHandler(log)
    resync, missing = err("jobs.requeueSyncUpdate"), err("jobs.videoNotAvailableYet")
    assert _jobs(manager) == {"A - 01": (missing, 1), "B - 01": (resync, 0), "C - 01": (resync, 0)}
    deferred = [row for row in manager.db.subtitle_jobs() if row["last_error"] == resync]
    assert all(float(row["next_check"]) >= started + 600 for row in deferred)  # max(5 min, poll interval)
    assert sum("reason=energy_throttle" in message for message in log.messages) == 2

    only = AnimeManager(_cfg(tmp_path / "only"), log=lambda _message: None)
    for name in ("D", "E"):
        _queue(only, tmp_path, name, migration=True)
    only.process_subtitle_jobs(limit=2)
    assert sorted(_jobs(only).values()) == sorted([(missing, 1), (resync, 0)])


def test_v0673_llm_handoff_note_is_not_shipped() -> None:
    assert not (ROOT / "docs" / "LLM_HANDOFF.md").exists()
