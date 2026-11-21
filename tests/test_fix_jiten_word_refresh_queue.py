"""Jiten word refreshes: background ones (an iPhone batch of stale/incomplete words) go
through a paced, deduplicated, bounded queue; a word the user opened goes first; a rate
limit pauses the queue (one log line) instead of failing every queued word."""

from __future__ import annotations

import logging
import threading
import time

from pudge import jiten_client
from pudge.jiten_words import JitenWordRepository
from pudge.light_novels import JitenUnavailableError


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _card(n: int) -> dict:
    return {"wordId": 1000 + n, "readingIndex": 0, "spelling": f"語{n}"}


def _answer(card: dict) -> dict:
    return {**card, "reading": "ご", "meanings": ["word"], "dictionary_complete": True}


def _paced(tmp_path, **attrs):
    clock = _Clock()
    repository = JitenWordRepository(tmp_path / "words.sqlite", logging.getLogger("t.jiten"), clock=clock)
    submitted: list = []
    repository.pool.submit = submitted.append  # run nothing on threads: the test drives the queue
    for name, value in attrs.items():
        setattr(repository, name, value)
    return repository, clock, submitted


def _fetcher(calls: list, card: dict, error: Exception | None = None):
    def fetch():
        calls.append(card["wordId"])
        if error is not None:
            raise error
        return _answer(card)

    return fetch


def test_background_refreshes_are_paced_by_a_token_bucket(tmp_path) -> None:
    repository, clock, submitted = _paced(tmp_path)
    calls: list = []
    for n in range(20):
        repository.resolve("acct", _card(n), _fetcher(calls, _card(n)), background=True)
    assert len(submitted) == 20 and len(repository._background) == 20
    taken = []
    while True:
        key, job = repository._next_job(clock())
        if key is None:
            break
        taken.append(key)
    assert len(taken) == int(repository.BACKGROUND_BURST)  # a short burst, then a wait
    assert 0 < job <= 1.0 / repository.BACKGROUND_RATE + 1e-9
    clock.now += 2.0  # 2 s at 1.5/s: three more
    more = 0
    while repository._next_job(clock())[0] is not None:
        more += 1
    assert more == 3
    repository.close()


def test_a_word_the_user_opened_jumps_the_queue_and_queued_keys_are_not_duplicated(tmp_path) -> None:
    repository, clock, submitted = _paced(tmp_path, BACKGROUND_BURST=1.0)
    calls: list = []
    for n in range(10):
        repository.resolve("acct", _card(n), _fetcher(calls, _card(n)), background=True)
    # The same words asked for again (the iPhone sends its next batch) are not queued twice.
    for n in range(10):
        repository.resolve("acct", _card(n), _fetcher(calls, _card(n)), background=True)
    assert len(submitted) == 10
    key, job = repository._next_job(clock())
    job()  # the one token
    assert calls == [1000]
    # Tapped: a new word and one that waits at the end of the background queue.
    repository.resolve("acct", _card(99), _fetcher(calls, _card(99)))
    repository.resolve("acct", _card(9), _fetcher(calls, _card(9)))
    for _ in range(2):
        key, job = repository._next_job(clock())  # no token needed
        assert key is not None
        job()
    assert calls == [1000, 1099, 1009]
    assert repository._next_job(clock())[0] is None  # the rest still waits for tokens
    assert repository.get("acct", _card(9))[0]["dictionary_complete"] is True
    repository.close()


def test_the_background_queue_is_bounded(tmp_path) -> None:
    repository, _clock, submitted = _paced(tmp_path, BACKGROUND_QUEUE_LIMIT=5)
    for n in range(12):
        repository.resolve("acct", _card(n), lambda: None, background=True)
    assert len(submitted) == 5 and len(repository._background) == 5
    # A dropped word is not "in flight": it is queued again by a later batch.
    assert ("acct", repository.key(_card(11))) not in repository.flights
    # A foreground lookup is never dropped.
    repository.resolve("acct", _card(50), lambda: None)
    assert len(submitted) == 6
    repository.close()


def test_a_rate_limit_pauses_the_whole_queue_with_one_log_line(tmp_path, caplog) -> None:
    repository, clock, submitted = _paced(tmp_path)
    calls: list = []
    limited = JitenUnavailableError(
        "Too many Jiten requests right now; try again in 11 s", reason="rate_limited", retry_after=11.0
    )
    cards = [_card(n) for n in range(40)]
    for card in cards[:3]:
        repository.resolve("acct", card, _fetcher(calls, card, limited), background=True)
    for card in cards[3:]:
        repository.resolve("acct", card, _fetcher(calls, card), background=True)
    caplog.set_level(logging.INFO, logger="t.jiten")
    # Three workers took the first three words before Jiten answered 429.
    jobs = [repository._next_job(clock())[1] for _ in range(3)]
    for job in jobs:
        job()
    pauses = [r for r in caplog.records if "PAUSE jiten.word_refresh" in r.getMessage()]
    failures = [r for r in caplog.records if "FAIL jiten.word_cache" in r.getMessage()]
    assert len(pauses) == 1 and not failures
    key, wait = repository._next_job(clock())
    assert key is None and 10.0 < wait <= 11.0  # nothing is sent until Retry-After
    # The refused words wait at the front again (not marked failed for 30 s).
    first = ("acct", repository.key(cards[0]))
    assert first in repository.flights and first not in repository.retry_after
    assert list(repository._background)[:3] == [("acct", repository.key(c)) for c in reversed(cards[:3])]
    assert len(submitted) == 40 + 3  # one more drain each for the re-queued words
    clock.now += 11.5
    key, _job = repository._next_job(clock())
    assert key is not None
    repository.close()


def test_background_fetch_never_waits_for_the_jiten_budget(tmp_path) -> None:
    repository, clock, _submitted = _paced(tmp_path)
    seen: dict = {}

    def fetch():
        seen["background"] = jiten_client.in_background()
        seen["wait_limit"] = jiten_client._WAIT_LIMIT.get()
        return None

    repository.resolve("acct", _card(1), fetch, background=True)
    repository._next_job(clock())[1]()
    assert seen == {"background": True, "wait_limit": 0.0}
    seen.clear()
    repository.resolve("acct", _card(2), fetch)
    repository._next_job(clock())[1]()
    assert seen == {"background": False, "wait_limit": None}
    repository.close()


def test_threads_pace_a_large_batch_and_serve_a_tap_at_once(tmp_path) -> None:
    repository = JitenWordRepository(tmp_path / "words.sqlite")
    started: list[tuple[float, int]] = []
    lock = threading.Lock()

    def fetcher(card):
        def fetch():
            with lock:
                started.append((time.monotonic(), card["wordId"]))
            return _answer(card)

        return fetch

    try:
        t0 = time.monotonic()
        for n in range(60):
            repository.resolve("acct", _card(n), fetcher(_card(n)), background=True)
        time.sleep(0.3)
        repository.resolve("acct", _card(500), fetcher(_card(500)))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(word == 1500 for _at, word in started):
            time.sleep(0.02)
        with lock:
            snapshot = list(started)
        tapped = [at for at, word in snapshot if word == 1500]
        assert tapped and tapped[0] - t0 < 1.0
        background_first_second = [at for at, word in snapshot if word != 1500 and at - t0 < 1.0]
        assert len(background_first_second) <= repository.BACKGROUND_BURST + 2
    finally:
        repository.close()


def test_iphone_word_batches_refresh_in_the_background(tmp_path, monkeypatch) -> None:
    from pudge.config import AppConfig
    from pudge.light_novels import LightNovelService

    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    service = LightNovelService(cfg)
    repository = service._word_repository()
    modes: list = []
    monkeypatch.setattr(
        repository,
        "resolve",
        lambda account, card, fetch=None, background=False: modes.append(background) or card,
    )
    service.jiten_words_cached([_card(1), _card(2)])
    service.jiten_word(_card(3))
    assert modes == [True, True, False]
    repository.close()
