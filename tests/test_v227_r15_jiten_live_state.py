from __future__ import annotations

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


def test_live_jiten_state_uses_known_state_endpoint_and_short_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    calls: list[str] = []

    def fake_get(action: str, params=None):
        calls.append(action)
        assert params is None
        return [2]  # Jiten KnownState.Mature

    monkeypatch.setattr(service, "_jiten_get", fake_get)
    monkeypatch.setattr(
        service,
        "_parse_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("chapter parse must not run")),
    )

    first = service.jiten_live_state(120, 2)
    second = service.jiten_live_state(120, 2)

    assert calls == ["vocabulary/120/2/known-state"]
    assert first["states"] == ["mature"]
    assert first["rawStateLabel"] == "Mature"
    assert first["normalizedState"] == "learning"
    assert first["knowledgeStatus"] == "live_provider_state"
    assert second["states"] == ["mature"]
    assert second["knowledgeStatus"] == "live_state_cache"


def test_live_jiten_state_accepts_string_enum_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_jiten_get", lambda *_args, **_kwargs: ["Due", "Mature"])

    result = service.jiten_live_state(10, 0)

    assert result["states"] == ["due", "mature"]
    # Jiten displays the durable tier as the primary word status; Due remains
    # present in states for coloring/review logic.
    assert result["rawStateLabel"] == "Mature"
    assert result["normalizedState"] == "due"


def test_network_failure_reuses_last_live_state_instead_of_demoting_to_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_jiten_get", lambda *_args, **_kwargs: [2])
    assert service.jiten_live_state(120, 2)["states"] == ["mature"]

    # Expire the short TTL but retain the last-known provider snapshot.
    key = service._jiten_live_state_key(120, 2)
    _ts, payload = service._jiten_live_state_cache[key]
    service._jiten_live_state_cache[key] = (0.0, payload)

    def offline(*_args, **_kwargs):
        raise LightNovelError("offline")

    monkeypatch.setattr(service, "_jiten_get", offline)
    result = service.jiten_live_state(120, 2, force=True)

    assert result["states"] == ["mature"]
    assert result["rawStateLabel"] == "Mature"
    assert result["knowledgeStatus"] == "stale_live_state_cache"
    assert result["stale"] is True


def test_live_state_cache_is_isolated_by_jiten_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    responses = iter(([2], [1]))
    calls = 0

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr(service, "_jiten_get", fake_get)
    assert service.jiten_live_state(44, 0)["states"] == ["mature"]
    service.save_settings({"jiten_api_key": "different-account-token", "study_backend": "jiten"})
    assert service.jiten_live_state(44, 0)["states"] == ["young"]
    assert calls == 2


def test_provider_payload_prefers_live_state_over_stale_chapter_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_jiten_get", lambda *_args, **_kwargs: [2])
    service.jiten_live_state(120, 2)
    parsed = {
        "paragraphs": ["少し"],
        "vocabulary": [
            {
                "wordId": 120,
                "readingIndex": 2,
                "spelling": "少し",
                "knownState": [],  # stale chapter parse said New
            }
        ],
        "tokens": [[{"wordId": 120, "readingIndex": 2, "start": 0, "end": 2}]],
    }

    tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jiten")

    assert vocabulary[0]["states"] == ["mature"]
    assert vocabulary[0]["rawStateLabel"] == "Mature"
    assert vocabulary[0]["knowledgeStatus"] in {"live_provider_state", "live_state_cache"}
    assert tokens[0][0]["card"]["states"] == ["mature"]


def test_confirmed_review_invalidates_exact_live_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_jiten_get", lambda *_args, **_kwargs: [2])
    service.jiten_live_state(120, 2)
    assert service._cached_jiten_live_state(120, 2) is not None

    monkeypatch.setattr(
        "pudge.light_novels.JitenReviewProvider.submit_review",
        lambda *_args, **_kwargs: {"ok": True, "outcome": "confirmed", "provider": "jiten"},
    )
    result = service.study_action(
        "jiten",
        "review",
        120,
        2,
        grade="good",
        attempt_id="r15-review-1",
        id_namespace="jiten",
    )

    assert result["outcome"] == "confirmed"
    assert service._cached_jiten_live_state(120, 2, allow_stale=True) is None


def test_web_api_exposes_live_study_state_without_chapter_reparse() -> None:
    class LightNovels:
        class Settings:
            study_backend = "jiten"

        def settings(self):
            return self.Settings()

        def jiten_live_state(self, word_id, reading_index, *, force=False):
            return {
                "ok": True,
                "states": ["mature"],
                "wordId": word_id,
                "readingIndex": reading_index,
                "force": force,
            }

    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = LightNovels()

    result = api.light_novel_study_state(
        {"backend": "jiten", "word_id": 120, "reading_index": 2, "force": True}
    )

    assert result == {
        "ok": True,
        "states": ["mature"],
        "wordId": 120,
        "readingIndex": 2,
        "force": True,
    }


def test_reader_refreshes_live_state_and_shows_exact_jiten_tier() -> None:
    source = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "light_novel_study_state" in source
    assert "https://jiten.moe/vocabulary/${wordId}/${readingIndex}" in source
    assert "API().open_url(url)" in source
    assert "refreshLiveStudyState(activeToken, target)" in source
    assert "rawStateLabel" in source
    assert "['blacklisted','Blacklisted']" in source
    assert "['mature','Mature']" in source
    assert "Network failure must not demote" in source
