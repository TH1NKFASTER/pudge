from __future__ import annotations

from types import SimpleNamespace

from pudge.download_intents import DownloadIntentStore


def _candidate(title: str = "Episode 1"):
    return SimpleNamespace(title=title, info_hash="a" * 40, score=100.0, seeders=10, leechers=0)


def test_discovered_selecting_candidate_counts_as_waiting() -> None:
    store = DownloadIntentStore(SimpleNamespace())
    store.begin(10, 1, False, [_candidate()])
    assert store.waiting_count() == 1


def test_empty_selecting_intent_does_not_count() -> None:
    store = DownloadIntentStore(SimpleNamespace())
    store.begin(10, 1, False, [])
    assert store.waiting_count() == 0


def test_waiting_counts_but_downloading_and_complete_do_not() -> None:
    store = DownloadIntentStore(SimpleNamespace())
    store.begin(10, 1, False, [_candidate()])
    store.update(10, 1, False, state="waiting", selected=_candidate())
    assert store.waiting_count() == 1
    store.update(10, 1, False, state="downloading", selected=_candidate())
    assert store.waiting_count() == 0
    store.update(10, 1, False, state="complete", selected=_candidate())
    assert store.waiting_count() == 0
