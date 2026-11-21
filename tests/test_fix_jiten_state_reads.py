"""Jiten word-state follow-ups (independent review).

* A long text's payload read the kept states with one SQLite connection per word, and again for every
  word SQLite did not have (0.4 s for 1500 words): now one read per payload, and a word SQLite lacks is
  not looked for again this run.
* The overview-version another Pudge process (the agent, the mpv study helper) saved first hid the
  change from this one, which kept stale colours for up to 15 minutes: the version this process saw
  last decides, and a "stale before" another process saved is adopted.
"""

from __future__ import annotations

import threading
from pathlib import Path


from jiten_fake_server import install
from pudge.config import AppConfig
from pudge.light_novels import LightNovelService

KEY = "jiten-test-key"


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _service(tmp_path: Path) -> LightNovelService:
    service = LightNovelService(_config(tmp_path))
    service.save_settings({"jiten_api_key": KEY, "parse_ahead": "current"})
    return service


def _join(prefix: str) -> None:
    for thread in threading.enumerate():
        if thread.name.startswith(prefix):
            thread.join(timeout=10)


def _account() -> str:
    from pudge.review_providers import credential_account_key

    return credential_account_key("jiten", KEY)


def test_a_long_payload_reads_the_kept_word_states_with_one_query(tmp_path, monkeypatch):
    first = _service(tmp_path)
    kept = {(100 + n, 0): {"states": ["mature"], "normalizedState": "mature"} for n in range(0, 1500, 3)}
    first._store_live_states(_account(), kept)
    service = LightNovelService(_config(tmp_path))  # the next launch: nothing in memory
    words = [(100 + n, 0) for n in range(1500)]
    parsed = {
        "paragraphs": ["x" * 1500],
        "tokens": [
            [{"wordId": w, "readingIndex": r, "start": i, "end": i + 1} for i, (w, r) in enumerate(words)]
        ],
        "vocabulary": [{"wordId": w, "readingIndex": r, "knownState": ["new"]} for w, r in words],
    }
    opened = {"n": 0}
    original = service._connection

    def counted():
        opened["n"] += 1
        return original()

    monkeypatch.setattr(service, "_connection", counted)
    _tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jiten")
    assert opened["n"] <= 3  # settings and one read of the kept states (before: one per word)
    states = {(row["wordId"], row["readingIndex"]): row["states"] for row in vocabulary}
    assert states[(100, 0)] == ["mature"] and states[(101, 0)] == ["new"]
    opened["n"] = 0
    service._provider_scoped_study_payload(parsed, "jiten")
    assert opened["n"] <= 2  # the words SQLite does not have are not asked for again


def test_an_overview_change_seen_first_by_another_process_still_makes_states_stale(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    window = _service(tmp_path)
    pairs = [(100 + n, 0) for n in range(10)]
    window.jiten_live_states(pairs)
    _join("ln-jiten-overview")
    agent = LightNovelService(_config(tmp_path))  # another Pudge process on the same database
    assert agent.check_jiten_overview_version(KEY, _account()) is False
    server.overview_version = 2  # a review on the Jiten site
    server.states[(100, 0)] = [2]
    assert agent.check_jiten_overview_version(KEY, _account()) is True  # the agent saw it first
    clock.sleep(61)
    window.jiten_live_states(pairs)  # the window's check (in the background) ...
    _join("ln-jiten-overview")
    rows = window.jiten_live_states(pairs)  # ... makes its states stale: asked again
    assert rows[0]["states"] == ["mature"]
    assert server.count("lookup-vocabulary") == 2


def test_a_stale_mark_saved_by_another_process_is_adopted(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    window = _service(tmp_path)
    pairs = [(100 + n, 0) for n in range(10)]
    window.jiten_live_states(pairs)
    _join("ln-jiten-overview")
    server.overview_version = 2
    clock.sleep(5)
    # The other process noticed the change, marked every state stale and saved version 2
    # before this process checked for the first time this minute.
    agent = LightNovelService(_config(tmp_path))
    agent._live_state_extras().setdefault("versions", {})[_account()] = "1"
    assert agent.check_jiten_overview_version(KEY, _account()) is True
    window._live_state_extras().setdefault("versions", {}).pop(_account(), None)  # as on a first check
    clock.sleep(61)
    window.jiten_live_states(pairs)
    _join("ln-jiten-overview")
    window.jiten_live_states(pairs)
    assert server.count("lookup-vocabulary") == 2
