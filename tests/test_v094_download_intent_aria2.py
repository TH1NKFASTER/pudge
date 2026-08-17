from __future__ import annotations

from dataclasses import dataclass

from pudge.download_intents import DownloadIntentStore


class FakeDb:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def delete_state(self, key: str) -> None:
        self.state.pop(key, None)


@dataclass
class Release:
    title: str
    info_hash: str
    score: float
    seeders: int = 0
    leechers: int = 0


def test_download_intent_persists_candidate_and_winner() -> None:
    db = FakeDb()
    store = DownloadIntentStore(db)
    first = Release("first", "aaa", 200.0, 20, 3)
    second = Release("second", "bbb", 180.0, 10, 5)

    store.begin(10, 3, False, [first, second], backend="aria2")
    store.update(10, 3, False, state="downloading", selected=second)

    value = store.get(10, 3, False)
    assert value is not None
    assert value["state"] == "downloading"
    assert value["backend"] == "aria2"
    assert value["selected_hash"] == "bbb"
    assert [item["info_hash"] for item in value["candidates"]] == ["aaa", "bbb"]


def test_aria2_exposes_queue_priority_and_status_surface() -> None:
    source = open("pudge/providers/aria2.py", encoding="utf-8").read()
    assert "def prioritize(" in source
    assert "aria2.changePosition" in source
    assert "def torrent_status(" in source


def test_manager_has_aria2_candidate_failover() -> None:
    source = open("pudge/manager.py", encoding="utf-8").read()
    assert "def _race_add_candidates_aria2(" in source
    assert "self.download_intents.begin(" in source
    assert "self.download_intents.update(" in source
