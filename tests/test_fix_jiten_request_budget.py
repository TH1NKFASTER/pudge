"""Jiten request budget (audit JITEN-1, JITEN-8, JITEN-12).

Jiten parses at most 200,000 characters a minute per user (at least 1000 per call) and
answers 429 with Retry-After until the minute is over. Pudge used to send 14k-character
batches every 0.65 s (about 1.3M characters a minute), retried a 429 after 0.6 s and 1.2 s
and dropped the batches that had worked: most chapters of a bulk parse failed. Now one
client keeps every Pudge thread and process within 150 requests and 180,000 parsed
characters a minute, waits out Retry-After, keeps answered batches and pauses during an
outage.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jiten_fake_server import FakeClock, install
from pudge import jiten_client
from pudge.config import AppConfig
from pudge.light_novels import JitenMutationUnknown, JitenUnavailableError, LightNovelError, LightNovelService

PARAGRAPH = "吾輩は猫である。名前はまだ無い。" * 6  # 96 characters
CHAPTER = "\n".join(f"{PARAGRAPH}{n:03d}" for n in range(320))  # 31.7k characters: three batches of <=14k


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    service = LightNovelService(config)
    service.save_settings({"jiten_api_key": "jiten-test-key", "parse_ahead": "current"})
    return service


def test_bulk_parse_of_twenty_chapters_stays_within_both_budgets(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    for index in range(20):  # "Refresh Jiten data" over 20 unparsed chapters
        with jiten_client.background_requests():
            parsed = service.jiten_preparse(f"第{index}章\n{CHAPTER}", f"digest-{index}", interactive=False)
        assert len(parsed["tokens"]) == 321
    assert server.statuses("reader/parse").count(429) == 0
    assert server.count("reader/parse") == 60
    assert server.max_per_window(chars=True) <= jiten_client.PARSE_CHARS_PER_MINUTE
    assert server.max_per_window() <= jiten_client.REQUESTS_PER_MINUTE
    # About 620k characters at <=100k a minute (the background share): a little over 6 minutes.
    assert 5 * 60 < clock.time() - 1_000_000.0 < 8 * 60


def test_a_429_is_waited_out_for_exactly_its_retry_after_and_holds_every_caller(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    server.use_parse_budget(199_500)  # the Jiten site used this minute's budget
    with jiten_client.background_requests():
        parsed = service.jiten_preparse("猫が好きです。", interactive=False)
    assert parsed["tokens"]
    assert server.statuses("reader/parse") == [429, 200]
    assert 60.0 in clock.sleeps  # Retry-After: 60, not 0.6 s and 1.2 s
    # Another 429 now blocks every parse caller until it ends; a click fails at once
    # with a clear message instead of queueing behind it, and nothing is sent.
    server.use_parse_budget(200_000)
    answer = jiten_client.request(
        "POST", f"{jiten_client.JITEN_API_BASE}/reader/parse", json={"text": ["x" * 1500]}, interactive=False
    )
    assert answer.status_code == 429
    before = server.count("reader/parse")
    assert jiten_client.CLIENT.pause_reason() == ""  # a parse limit leaves other endpoints alone
    with pytest.raises(JitenUnavailableError) as refused:
        service._jiten_request("reader/parse", {"text": ["もう一度"]})
    assert refused.value.reason == "rate_limited"
    assert "try again in" in str(refused.value)
    assert server.count("reader/parse") == before
    assert service._jiten_request("reader/lookup-vocabulary", {"words": [[100, 0]]})["result"] == [[1]]


def test_answered_batches_of_a_failed_chapter_are_kept_and_not_asked_again(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    calls = {"n": 0}
    original = server.handle

    def handle(method, url, **kwargs):
        if url.endswith("reader/parse"):
            calls["n"] += 1
            if calls["n"] == 3:
                return httpx.Response(400, json={"detail": "bad batch"}, request=httpx.Request(method, url))
        return original(method, url, **kwargs)

    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: handle("POST", str(url), **kwargs))
    with pytest.raises(LightNovelError):
        service.jiten_preparse(CHAPTER, "chapter-a")
    assert calls["n"] == 3
    parsed = service.jiten_preparse(CHAPTER, "chapter-a")
    assert calls["n"] == 4  # only the batch that failed is sent again
    assert len(parsed["tokens"]) == 320
    with service._connection() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM ln_parse_cache WHERE parser_schema='jiten-batch-v1'"
        ).fetchone()[0]
    assert left == 0  # the kept batches go once the chapter is stored


def test_the_budget_is_shared_with_other_pudge_processes(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    window = jiten_client.JitenClient(clock=clock.time, sleep=clock.sleep, state_dir=tmp_path / "shared")
    agent = jiten_client.JitenClient(clock=clock.time, sleep=clock.sleep, state_dir=tmp_path / "shared")
    url = f"{jiten_client.JITEN_API_BASE}/reader/lookup-vocabulary"
    for _ in range(jiten_client.BACKGROUND_REQUESTS_PER_MINUTE):
        window.request("POST", url, json={"words": [[1, 0]]}, interactive=False)
    started = clock.time()
    agent.request("POST", url, json={"words": [[1, 0]]}, interactive=False)
    assert clock.time() - started >= 50  # the agent waited for the window's requests to age out
    assert server.max_per_window() <= jiten_client.REQUESTS_PER_MINUTE
    # A 429 one process saw holds the other back too.
    server.fail["lookup-vocabulary"] = [429]
    window.request("POST", url, json={"words": [[1, 0]]}, attempts=1)
    assert json.loads((tmp_path / "shared" / "budget.json").read_text())["blocked_until"] > clock.time()
    with pytest.raises(jiten_client.JitenRefused):
        agent.request("POST", url, json={"words": [[1, 0]]}, interactive=True)


def test_three_server_failures_pause_jiten_and_an_answer_ends_the_pause(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    # Three requests in a row that Jiten fails (each tried three times; one request counts once).
    server.fail["lookup-vocabulary"] = [503, httpx.ReadTimeout("hang"), 502] * 3
    for call in range(3):
        with pytest.raises(LightNovelError):
            service._jiten_request("reader/lookup-vocabulary", {"words": [[1, 0]]})
        if call == 0:
            assert jiten_client.pause_reason() == ""  # one failing request is not an outage
    # The third one's first failure starts the pause: its other tries are not sent.
    assert server.count("lookup-vocabulary") == 7
    # Paused: background work stops at once, a click fails at once; nothing is sent.
    assert jiten_client.pause_reason() == "jiten_unavailable"
    with pytest.raises(JitenUnavailableError) as refused:
        service._jiten_request("reader/lookup-vocabulary", {"words": [[1, 0]]})
    assert refused.value.reason == "unavailable"
    # A mutation refused by the pause was never sent: not an "unknown outcome".
    with pytest.raises(JitenUnavailableError) as mutation:
        service._jiten_mutation("srs/review", {"wordId": 1})
    assert not isinstance(mutation.value, JitenMutationUnknown)
    assert server.count() == 7
    server.fail.clear()  # Jiten answers again
    clock.sleep(61)
    assert service._jiten_request("reader/lookup-vocabulary", {"words": [[1, 0]]})["result"] == [[1]]
    assert jiten_client.pause_reason() == ""


def test_an_offline_mac_does_not_count_as_a_jiten_outage(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    server.fail["lookup-vocabulary"] = [httpx.ConnectError("offline")] * 6
    for _ in range(2):
        with pytest.raises(LightNovelError):
            service._jiten_request("reader/lookup-vocabulary", {"words": [[1, 0]]})
    assert jiten_client.pause_reason() == ""


def test_statistics_words_keep_a_unit_due_while_jiten_holds_requests_back():
    from types import SimpleNamespace

    from pudge.web_app import WebAppApi

    api = WebAppApi.__new__(WebAppApi)
    marked: list[str] = []
    api.light_novels = SimpleNamespace(
        jiten_preparse=lambda *_a, **_k: (_ for _ in ()).throw(
            JitenUnavailableError("paused", reason="unavailable", retry_after=60)
        )
    )
    api.logger = SimpleNamespace(info=lambda *_a, **_k: None, debug=lambda *_a, **_k: None)
    units = SimpleNamespace(mark_missing=lambda key, *_a, **_k: marked.append(key), store=lambda *_a: None)
    unit = {"key": "vn:1", "kind": "visual_novel", "source": {"text": "猫"}, "attempts": 0}
    assert api._statistics_word_unit(units, unit, {}) is None
    assert marked == []


def test_audiobook_chapter_start_hints_use_the_shared_client_without_retrying_a_4xx(tmp_path, monkeypatch):
    from pudge.audiobooks import AudiobookService

    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    audiobooks = AudiobookService.__new__(AudiobookService)
    audiobooks.db = SimpleDB(service.db_path)
    audiobooks.cache_dir = tmp_path / "cache"
    text = "第一章\n吾輩は猫である。\n名前はまだ無い。"
    server.fail["reader/parse"] = [400]
    hints, meta = audiobooks._ensure_chapter_start_reading_hints(1, 0, text, "prefix-a")
    assert hints == [] and server.count("reader/parse") == 1  # a 400 is not sent three times
    hints, meta = audiobooks._ensure_chapter_start_reading_hints(1, 0, text, "prefix-a")
    assert server.count("reader/parse") == 2 and meta["source"] == "jiten_prefix_api"
    # An answer without readings is cached too: the next opening sends nothing.
    hints, meta = audiobooks._ensure_chapter_start_reading_hints(1, 0, text, "prefix-a")
    assert server.count("reader/parse") == 2 and meta["source"] == "prefix_cache"
    assert jiten_client.CLIENT.sent == 2


class SimpleDB:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self):
        import sqlite3

        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def test_retry_after_accepts_seconds_and_http_dates():
    assert jiten_client.retry_after_seconds("37") == 37
    assert jiten_client.retry_after_seconds("") == 60
    assert jiten_client.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412460.0) == 20
    assert jiten_client.parse_charge({"text": ["猫"]}) == 1000
    assert jiten_client.parse_charge({"text": ["a" * 1500, "b" * 600]}) == 2100


def test_refresh_jiten_data_waits_out_a_jiten_pause_instead_of_failing_every_item(monkeypatch):
    import threading
    from types import SimpleNamespace

    from pudge import web_app
    from pudge.web_app import WebAppApi

    class Jobs:
        def __init__(self):
            self.finished = None

        def get(self, _job_id):
            return {"state": "running"}

        def update(self, *_a, **_k):
            pass

        def finish(self, _job_id, *, message="", result=None):
            self.finished = result

        def fail(self, *_a, **_k):
            raise AssertionError("the job must not fail")

        def cancelled(self, *_a, **_k):
            raise AssertionError("not cancelled")

    answers = [JitenUnavailableError("paused", reason="unavailable", retry_after=2.0)]
    parsed: list[str] = []

    def preparse(text, digest=None):
        assert jiten_client.in_background()
        if answers:
            raise answers.pop(0)
        parsed.append(text)

    clock = FakeClock()
    monkeypatch.setattr(web_app, "time", clock.module())
    api = WebAppApi.__new__(WebAppApi)
    api.job_center = Jobs()
    api.logger = SimpleNamespace(exception=lambda *_a, **_k: None, info=lambda *_a, **_k: None)
    api.light_novels = SimpleNamespace(
        jiten_preparse=preparse, parse_study_texts=lambda texts: parsed.extend(texts)
    )
    api._jiten_refresh_lock = threading.Lock()
    api._jiten_refresh_job_id = ""
    api._jiten_refresh_thread = None
    api._jiten_refresh_worker("job", [("一章", "h1"), ("二章", "h2")], ["吹き出し"] * 3)
    assert parsed == ["一章", "二章", "吹き出し", "吹き出し", "吹き出し"]
    assert api.job_center.finished == {"light_novel_parses": 2, "manga_parses": 3, "errors": []}
    assert sum(clock.sleeps) >= 2.0  # it waited for the pause, then parsed the same chapter
