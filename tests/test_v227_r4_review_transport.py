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
from tests.i18n_support import en, ru


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


def test_jiten_ambiguous_failure_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_jpdb_native_review_uses_v1_endpoint_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        # S2: the UI sends the jpdb-native action id of the active mode.
        grade="okay",
        attempt_id="jpdb-attempt-1",
        id_namespace="jpdb",
    )

    assert result["outcome"] == "confirmed"
    assert calls == [(f"{JPDB_API_BASE}/review", {"vid": 555, "sid": 777, "grade": "okay"})]


def test_jpdb_ambiguous_failure_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert vocabulary[0]["normalizedState"] == "mature"  # an older "known" is Jiten's Mature
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


# Reader words whose cards come from a cached parse ("young, in no deck"): a grade waits for
# the card's live refresh. study_states answers are held per word until release(id).
REVIEWS = r"""
window.__held = []; window.__hold = true;
window.__result = {ok: true, outcome: 'confirmed'};
__api.study_provider_capabilities = async () => ({account_key: 'jiten:me', configured: true});
__api.study_action = async () => window.__result;
window.__live = {'1:0': {states: ['young'], normalizedState: 'learning', studyDeckIds: []},
                 '2:0': {states: ['mature'], normalizedState: 'learning', rawStateLabel: 'Mature', studyDeckIds: []},
                 '3:0': {states: ['young'], normalizedState: 'learning', studyDeckIds: []}};
__api.study_states = payload => {
  const answer = () => ({ok: true, account_scope: 'jiten:me', states: payload.words.map(([w, r]) => ({ok: true, wordId: w, readingIndex: r, ...window.__live[`${w}:${r}`]}))});
  if (!window.__hold) return Promise.resolve(answer());
  return new Promise(resolve => window.__held.push({ids: payload.words.map(([w]) => w), go: () => resolve(answer())}));
};
const release = async id => { for (const h of window.__held.filter(h => h.ids.includes(id))) { window.__held.splice(window.__held.indexOf(h), 1); h.go(); } await __tick(50); };
const words = [['少し', 1], ['猫', 2], ['犬', 3]];
let pos = 0;
const tokens = words.map(([text, wordId]) => { const start = pos; pos += text.length;
  return {wordId, readingIndex: 0, start, end: pos, card: {spelling: text, states: ['young'], studyDeckIds: []}}; });
const box = document.createElement('div');
box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: [words.map(w => w[0]).join('')], tokens: [tokens], vocabulary: []}, {backend: 'jiten'});
document.body.appendChild(box);
await __tick(100);
const open = async text => { const node = [...box.querySelectorAll('[data-pudge-study-token]')].find(n => n.textContent === text);
  await __click(`[data-pudge-study-token="${node.dataset.pudgeStudyToken}"]`); await __tick(20); };
const good = async () => { await __click('#pudgeStudyCard [data-pudge-study-review="good"]'); await __tick(20); };
const reviews = () => __apiCalls.filter(c => c.name === 'study_action').map(c => c.args[0]);
"""


def test_word_card_sends_each_review_once_and_only_for_the_open_card(page) -> None:
    assert en("reading.reviewOutcomeUnknownGradeAgain").startswith(
        "Jiten did not answer; the review may be saved."
    )
    assert ru("reading.reviewOutcomeUnknownGradeAgain").startswith(
        "Jiten не ответил; повторение могло сохраниться."
    )
    result = page.run(
        REVIEWS
        + """
        const out = {};
        await open('少し'); await good(); await good();          // two quick clicks while the refresh is pending
        out.whilePending = reviews().length;
        await release(1);
        out.once = reviews().map(r => [r.word_id, r.grade]);
        await open('猫'); await good();                          // graded, then closed before the refresh answered
        await __click('[data-pudge-study-close]');
        await open('犬');
        await release(2);                                        // 猫's late answer arrives while 犬 is open
        out.afterClose = reviews().length;
        out.otherCardBadge = __text('#pudgeStudyCard .pudge-study-state');
        window.__hold = false;
        window.__result = {ok: true, outcome: 'unknown'};        // the provider's answer was lost
        await release(3); await good(); await __tick(50);
        window.__result = {ok: true, outcome: 'unknown', message: 'Jiten did not answer'};
        await open('少し'); await good(); await __tick(50);
        out.toasts = __toasts();
        out.unknown = t('reading.reviewOutcomeUnknownGradeAgain');
        out.young = t('reading.state.young');
        return out;
        """,
    )
    out = result.value
    assert out["whilePending"] == 0 and out["once"] == [[1, "good"]]
    assert out["afterClose"] == 1  # nothing sent for the closed card
    assert out["otherCardBadge"] == out["young"]  # 犬 keeps its own state, not 猫's "Mature"
    # An unknown outcome is not retried automatically; the toast says grading again is
    # safe (the backend resends the same attempt id), whatever the provider's message.
    assert out["toasts"] == [out["unknown"]]  # the same toast for both
    sent = [call for [call] in result.calls_of("study_action")]
    assert [row["word_id"] for row in sent] == [1, 3, 1]
    # Every review carries its own attempt id (the backend's idempotency key) and the id namespace.
    assert len({row["attempt_id"] for row in sent}) == 3 and all(row["attempt_id"] for row in sent)
    assert {row["id_namespace"] for row in sent} == {"jiten"}


def test_jpdb_capabilities_match_official_openapi_contract() -> None:
    from pudge.review_providers import JPDB_GRADES

    jpdb = provider_capabilities("jpdb", "secret-b")
    # Official /api/v1/review grade enum (jpdb.stoplight.io, 2026-09-27).
    assert JPDB_GRADES == ("nothing", "something", "hard", "okay", "easy", "fail", "pass")
    assert jpdb["grades"] == list(JPDB_GRADES)
    assert jpdb["grade_modes"] == {
        "native": ["nothing", "something", "hard", "okay", "easy"],
        "binary": ["fail", "pass"],
    }
    assert jpdb["source_revision"] != "unverified"
    assert jpdb["undo"].startswith("unsupported")
    assert jpdb["previous_review_evidence"] is False
    assert jpdb["strict_gate_supported"] is False

    jiten = provider_capabilities("jiten", "secret-a")
    assert jiten["grade_modes"] == {"native": ["again", "hard", "good", "easy"]}
    assert "binary" not in jiten["grade_modes"]


@pytest.mark.parametrize(
    ("grade", "wire"),
    [
        ("nothing", "nothing"),
        ("something", "something"),
        ("hard", "hard"),
        ("okay", "okay"),
        ("easy", "easy"),
        ("pass", "pass"),
        ("fail", "fail"),
        ("PASS", "pass"),
        # Legacy 4-button names keep their v0.7.29 wire mapping.
        ("again", "fail"),
        ("good", "okay"),
    ],
)
def test_jpdb_submit_review_sends_native_grade(
    monkeypatch: pytest.MonkeyPatch, grade: str, wire: str
) -> None:
    from pudge.review_providers import JpdbReviewProvider

    calls: list[dict] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append(dict(json))
        return _Response(200, {})

    monkeypatch.setattr("pudge.review_providers.httpx.post", fake_post)
    result = JpdbReviewProvider("tok").submit_review(1, 2, grade, attempt_id="a1")
    assert result["outcome"] == "confirmed"
    assert calls == [{"vid": 1, "sid": 2, "grade": wire}]


@pytest.mark.parametrize("grade", ["", "good-ish", "1", "blacklist", "know"])
def test_jpdb_submit_review_rejects_unknown_grade_before_network(
    monkeypatch: pytest.MonkeyPatch, grade: str
) -> None:
    from pudge.review_providers import JpdbReviewProvider, ReviewProviderError

    def fake_post(*_args, **_kwargs):
        raise AssertionError("invalid grade must not reach the network")

    monkeypatch.setattr("pudge.review_providers.httpx.post", fake_post)
    with pytest.raises(ReviewProviderError, match="Unsupported JPDB review grade"):
        JpdbReviewProvider("tok").submit_review(1, 2, grade, attempt_id="a1")
