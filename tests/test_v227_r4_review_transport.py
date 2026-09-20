from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.identity import IdentityResolver, MediaIdentity
from pudge.manager_models import LibraryAnime
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.review_providers import (
    JITEN_API_BASE,
    JPDB_API_BASE,
    JitenReviewProvider,
    provider_capabilities,
)


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


class _Response:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.content = b"{}"

    def json(self):
        return self._payload


def test_capabilities_are_provider_native_and_strict_gate_support_is_explicit() -> None:
    jiten = provider_capabilities("jiten", "secret-a")
    jpdb = provider_capabilities("jpdb", "secret-b")

    assert jiten["api_base"] == JITEN_API_BASE
    assert jiten["card_id_namespace"] == "jiten"
    assert jiten["card_id_shape"] == "(wordId, readingIndex)"
    assert jiten["enumerate_existing_reviewable"] is True
    assert jiten["previous_review_evidence"] is True
    assert jiten["strict_gate_supported"] is True
    assert jiten["account_key"].startswith("jiten:")
    assert "secret-a" not in str(jiten)

    assert jpdb["api_base"] == JPDB_API_BASE
    assert jpdb["card_id_namespace"] == "jpdb"
    assert jpdb["card_id_shape"] == "(vid, sid)"
    assert jpdb["strict_gate_supported"] is False
    assert jpdb["account_key"].startswith("jpdb:")
    assert "secret-b" not in str(jpdb)


def test_jiten_review_is_single_send_with_stable_client_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jiten_api_key": "jiten-token", "study_backend": "jiten"})
    calls: list[tuple[str, dict]] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append((url, dict(json)))
        return _Response(200, {"success": True, "newState": 2})

    monkeypatch.setattr("pudge.review_providers.httpx.post", fake_post)
    result = service.study_action(
        "jiten",
        "review",
        123,
        4,
        grade="good",
        attempt_id="attempt-fixed-1",
        id_namespace="jiten",
    )

    assert result["outcome"] == "confirmed"
    assert len(calls) == 1
    assert calls[0][0] == f"{JITEN_API_BASE}/srs/review"
    assert calls[0][1] == {
        "wordId": 123,
        "readingIndex": 4,
        "rating": 3,
        "clientRequestId": "attempt-fixed-1",
    }


def test_jiten_ambiguous_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jiten_api_key": "jiten-token"})
    calls = 0

    def timeout(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("lost response")

    monkeypatch.setattr("pudge.review_providers.httpx.post", timeout)
    result = service.study_action(
        "jiten",
        "review",
        123,
        4,
        grade="good",
        attempt_id="attempt-unknown-1",
        id_namespace="jiten",
    )

    assert calls == 1
    assert result["ok"] is False
    assert result["outcome"] == "unknown"
    assert result["attempt_id"] == "attempt-unknown-1"


def test_jiten_candidate_api_excludes_new_cards_and_history_proves_prior_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    def fake_get(url, *, headers, params=None, timeout):
        calls.append((url, dict(params) if params is not None else None))
        if url.endswith("/srs/study-batch"):
            return _Response(
                200,
                {
                    "sessionId": "session-1",
                    "cards": [
                        {"wordId": 1, "readingIndex": 0, "isNewCard": True},
                        {"wordId": 2, "readingIndex": 1, "isNewCard": False, "due": "2026-09-14T00:00:00Z"},
                    ],
                    "reviewsRemaining": 7,
                },
            )
        return _Response(
            200,
            {
                "card": {"state": 2, "due": "2026-09-15T00:00:00Z", "lastReview": "2026-09-13T00:00:00Z"},
                "reviews": [{"rating": 3, "reviewDateTime": "2026-09-13T00:00:00Z"}],
            },
        )

    monkeypatch.setattr("pudge.review_providers.httpx.get", fake_get)
    provider = JitenReviewProvider("jiten-token")
    page = provider.list_review_candidates(limit=20)
    snapshot = provider.lookup_state(2, 1)

    assert [card["wordId"] for card in page["cards"]] == [2]
    assert calls[0][1] == {"limit": 20, "extraNewCards": 0}
    assert snapshot["exists"] is True
    assert snapshot["previously_reviewed"] is True
    assert snapshot["review_count"] == 1


def test_jpdb_native_review_uses_v1_endpoint_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "jpdb-token", "study_backend": "jpdb"})
    calls: list[tuple[str, dict]] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append((url, dict(json)))
        return _Response(200, {})

    monkeypatch.setattr("pudge.review_providers.httpx.post", fake_post)
    result = service.study_action(
        "jpdb",
        "review",
        555,
        777,
        grade="good",
        attempt_id="jpdb-attempt-1",
        id_namespace="jpdb",
    )

    assert result["outcome"] == "confirmed"
    assert calls == [(f"{JPDB_API_BASE}/review", {"vid": 555, "sid": 777, "grade": "okay"})]


def test_jpdb_ambiguous_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "jpdb-token", "study_backend": "jpdb"})
    calls = 0

    def timeout(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("lost response")

    monkeypatch.setattr("pudge.review_providers.httpx.post", timeout)
    result = service.study_action(
        "jpdb",
        "review",
        555,
        777,
        grade="easy",
        attempt_id="jpdb-attempt-unknown",
        id_namespace="jpdb",
    )

    assert calls == 1
    assert result["outcome"] == "unknown"
    assert result["attempt_id"] == "jpdb-attempt-unknown"


def test_jpdb_never_casts_jiten_ids_into_vid_sid(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "jpdb-token", "study_backend": "jpdb"})

    with pytest.raises(LightNovelError, match="native vid/sid mapping is unavailable"):
        service.study_action(
            "jpdb",
            "review",
            123,
            4,
            grade="good",
            attempt_id="attempt-1",
            id_namespace="jiten",
        )


def test_jpdb_selected_does_not_leak_jiten_knowledge_state(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parsed = {
        "paragraphs": ["猫"],
        "vocabulary": [
            {
                "wordId": 10,
                "readingIndex": 1,
                "spelling": "猫",
                "knownState": ["known"],
            }
        ],
        "tokens": [
            [
                {
                    "wordId": 10,
                    "readingIndex": 1,
                    "start": 0,
                    "end": 1,
                    "card": {"spelling": "猫", "cardState": ["known"]},
                }
            ]
        ],
    }

    tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jpdb")

    card = vocabulary[0]
    token_card = tokens[0][0]["card"]
    assert card["states"] == []
    assert card["normalizedState"] == "unknown"
    assert card["knowledgeStatus"] == "unavailable_mapping"
    assert card["idNamespace"] == "jiten"
    assert card["reviewable"] is False
    assert "knownState" not in card and "cardState" not in card
    assert token_card["normalizedState"] == "unknown"
    assert token_card["reviewable"] is False


def test_jiten_selected_keeps_jiten_state_and_namespace(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parsed = {
        "paragraphs": ["猫"],
        "vocabulary": [{"wordId": 10, "readingIndex": 1, "knownState": ["known"]}],
        "tokens": [[{"wordId": 10, "readingIndex": 1, "start": 0, "end": 1}]],
    }
    tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jiten")
    assert vocabulary[0]["normalizedState"] == "known"
    assert vocabulary[0]["reviewable"] is True
    assert tokens[0][0]["idNamespace"] == "jiten"


def test_identity_lookup_never_matches_unrelated_empty_hash(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    resolver = IdentityResolver(database)
    database.upsert_anime(LibraryAnime(media_id=1, title="One", status="CURRENT"))
    database.upsert_anime(LibraryAnime(media_id=2, title="Two", status="CURRENT"))
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    resolver.record(MediaIdentity(1, 1, video_path=first, source="scan"))
    resolver.record(MediaIdentity(2, 1, video_path=second, source="scan"))

    found = resolver.lookup(video_path=first)
    assert found is not None
    assert found["media_id"] == 1


def test_identity_hash_ambiguity_does_not_choose_arbitrary_row(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    resolver = IdentityResolver(database)
    database.upsert_anime(LibraryAnime(media_id=1, title="One", status="CURRENT"))
    database.upsert_anime(LibraryAnime(media_id=2, title="Two", status="CURRENT"))
    resolver.record(MediaIdentity(1, 1, torrent_hash="ABC", source="scan"))
    resolver.record(MediaIdentity(2, 1, torrent_hash="ABC", source="scan"))

    assert resolver.lookup(torrent_hash="abc") is None


def test_review_ui_has_single_flight_attempt_and_generation_guard() -> None:
    source = Path("pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "pendingReview" in source
    assert "reviewOutcomeUnknown" in source
    assert "crypto?.randomUUID?.()" in source
    assert "attempt_id: attemptId" in source
    assert "id_namespace: current.idNamespace" in source
    assert "activeToken !== current" in source
    assert "activeToken?.generation !== current.generation" in source
    assert "automatic retry is blocked" in source
    index = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "lnLegacyReviewPending" in index
    assert "attempt_id:attemptId" in index
    assert "id_namespace:'jiten'" in index
