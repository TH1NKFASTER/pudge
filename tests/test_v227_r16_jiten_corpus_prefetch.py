from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(cfg)
    service.save_settings({"jiten_api_key": "jiten-token", "study_backend": "jiten"})
    return service


def test_batch_live_state_lookup_populates_same_r15_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    requests: list[tuple[str, dict]] = []

    def fake_request(action: str, payload=None):
        requests.append((action, payload))
        return {"result": [[2], [4, 1]], "decks": []}

    monkeypatch.setattr(service, "_jiten_request", fake_request)

    rows = service.jiten_live_states([(120, 2), (44, 0), (120, 2)])

    assert requests == [
        (
            "reader/lookup-vocabulary",
            {"words": [[120, 2], [44, 0]]},
        )
    ]
    assert [(row["wordId"], row["readingIndex"]) for row in rows] == [(120, 2), (44, 0)]
    assert rows[0]["states"] == ["mature"]
    assert rows[0]["rawStateLabel"] == "Mature"
    assert rows[1]["states"] == ["due", "young"]
    assert rows[1]["normalizedState"] == "due"

    parsed = {
        "paragraphs": ["少し"],
        "vocabulary": [
            {"wordId": 120, "readingIndex": 2, "spelling": "少し", "knownState": []}
        ],
        "tokens": [[{"wordId": 120, "readingIndex": 2, "start": 0, "end": 2}]],
    }
    tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jiten")
    assert vocabulary[0]["states"] == ["mature"]
    assert tokens[0][0]["card"]["states"] == ["mature"]

    # Fresh cache means the next renderer hydration is local-only.
    assert service.jiten_live_states([(120, 2), (44, 0)])
    assert len(requests) == 1


def test_batch_failure_keeps_stale_r15_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_jiten_request",
        lambda *_args, **_kwargs: {"result": [[2]]},
    )
    service.jiten_live_states([(120, 2)])
    key = service._jiten_live_state_key(120, 2)
    _ts, payload = service._jiten_live_state_cache[key]
    service._jiten_live_state_cache[key] = (0.0, payload)

    def offline(*_args, **_kwargs):
        raise LightNovelError("offline")

    monkeypatch.setattr(service, "_jiten_request", offline)
    result = service.jiten_live_states([(120, 2)], force=True)

    assert result[0]["states"] == ["mature"]
    assert result[0]["stale"] is True
    assert result[0]["knowledgeStatus"] == "stale_live_state_cache"


def test_corpus_orders_global_frequency_first_then_local_occurrences(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parsed = {
        "paragraphs": ["dummy"],
        "vocabulary": [
            {"wordId": 10, "readingIndex": 0, "frequencyRank": 100},
            {"wordId": 20, "readingIndex": 0, "frequencyRank": 50},
            {"wordId": 30, "readingIndex": 1, "frequencyRank": 100},
            {"wordId": 40, "readingIndex": 0},
        ],
        "tokens": [[
            {"wordId": 10, "readingIndex": 0},
            {"wordId": 20, "readingIndex": 0},
            {"wordId": 30, "readingIndex": 1},
            {"wordId": 30, "readingIndex": 1},
            {"wordId": 40, "readingIndex": 0},
        ]],
    }
    with service._connection() as conn:
        conn.execute(
            "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)",
            ("corpus", json.dumps(parsed), "jiten-v1", 1.0),
        )

    assert service.jiten_corpus_pairs() == [(20, 0), (30, 1), (10, 0), (40, 0)]


def test_corpus_prefetch_uses_batches_in_priority_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "jiten_corpus_pairs", lambda: [(1, 0), (2, 0), (3, 0)])
    calls: list[list[tuple[int, int]]] = []

    def fake_live_states(pairs, *, force=False):
        assert force is False
        calls.append(list(pairs))
        return [{"ok": True} for _ in pairs]

    monkeypatch.setattr(service, "jiten_live_states", fake_live_states)
    monkeypatch.setattr(service, "JITEN_STATE_PREFETCH_PAUSE_SECONDS", 0.0)

    result = service.jiten_prefetch_corpus_states(batch_size=2)

    assert calls == [[(1, 0), (2, 0)], [(3, 0)]]
    assert result == {"ok": True, "reason": "complete", "total": 3, "warmed": 3, "batches": 2}


def test_web_api_exposes_batch_state_lookup() -> None:
    class LightNovels:
        class Settings:
            study_backend = "jiten"

        def settings(self):
            return self.Settings()

        def jiten_live_states(self, pairs, *, force=False):
            return [{"wordId": word_id, "readingIndex": reading_index, "force": force} for word_id, reading_index in pairs]

    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = LightNovels()

    result = api.light_novel_study_states(
        {"backend": "jiten", "words": [[120, 2], [44, 0]], "force": True}
    )

    assert result == {
        "ok": True,
        "provider": "jiten",
        "states": [
            {"wordId": 120, "readingIndex": 2, "force": True},
            {"wordId": 44, "readingIndex": 0, "force": True},
        ],
    }


def test_readers_batch_hydrate_before_click_and_keep_single_card_refresh() -> None:
    tools = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    index = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "light_novel_study_states" in tools
    assert "reader/lookup-vocabulary" not in tools  # Browser never talks to Jiten directly.
    assert "queueLiveStateHydration(payload, backend)" in tools
    assert "lookupLiveStates" in tools
    assert "refreshLiveStudyState(activeToken, target)" in tools  # R15 exact-click fallback remains.
    assert "hydrateLnChapterLiveStates" in index
    assert "jiten_state_prefetch()" in app
    assert "jiten_prefetch_corpus_states" in app
