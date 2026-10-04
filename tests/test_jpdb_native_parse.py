from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pudge.config import AppConfig
from pudge.jpdb_parse import (
    JpdbParseError,
    convert_parse_result,
    normalized_jpdb_state,
    utf16_slice,
)
from pudge.light_novels import LightNovelError, LightNovelService


def _vocab(vid: int, sid: int, spelling: str, reading: str, state: Any = None) -> list[Any]:
    # Order = JPDB_VOCABULARY_FIELDS
    return [vid, sid, sid, spelling, reading, 100, [f"meaning of {spelling}"], state, 7 if state else None, None, ["n"]]


# Shapes below mirror a live jpdb /parse response (utf16 encoding, 2026-09-27).
PARAGRAPHS = [
    "😀猫が生きる😀",
    "今日は今日で",
    "カラスが鳴く。からすは黒い。",
    "ぽにょぽにょむぎゅら",
]
RESULT = {
    "tokens": [
        [[0, 2, 1, [["猫", "ねこ"]]], [1, 3, 1, None], [2, 4, 3, [["生", "い"], "き", "る"]]],
        [[3, 0, 2, [["今日", "きょう"]]], [4, 2, 1, None], [3, 3, 2, [["今日", "きょう"]]], [5, 5, 1, None]],
        [[6, 0, 3, None], [1, 3, 1, None], [7, 4, 2, [["鳴", "な"], "く"]], [8, 7, 3, None], [4, 10, 1, None], [9, 11, 2, [["黒", "くろ"], "い"]]],
        [[10, 0, 6, None]],
    ],
    "vocabulary": [
        _vocab(1467640, 1827755855, "猫", "ねこ", ["known"]),
        _vocab(2028930, 2204758175, "が", "が", ["blacklisted"]),
        _vocab(1378520, 3986518776, "生きる", "いきる", ["learning"]),
        _vocab(1579110, 1518746816, "今日", "きょう", ["due"]),
        _vocab(2028920, 2204744690, "は", "は", ["blacklisted"]),
        _vocab(2028980, 2204748170, "で", "で", ["blacklisted"]),
        _vocab(1171450, 1031991566, "カラス", "カラス", ["known"]),
        _vocab(1532870, 3635992132, "鳴く", "なく", ["known"]),
        _vocab(1171450, 1650459493, "からす", "からす", ["redundant", "known"]),
        _vocab(1287420, 2388173039, "黒い", "くろい", ["known"]),
        _vocab(2453010, 722197285, "ぽにょぽにょ", "ぽにょぽにょ", None),
    ],
}


def test_convert_keeps_utf16_offsets_and_surfaces_around_non_bmp() -> None:
    converted = convert_parse_result(PARAGRAPHS, RESULT)
    first = converted["tokens"][0]
    assert [t["surface"] for t in first] == ["猫", "が", "生きる"]
    assert [(t["start"], t["end"]) for t in first] == [(2, 3), (3, 4), (4, 7)]
    assert first[0]["rubies"] == [{"start": 2, "end": 3, "text": "ねこ"}]
    assert first[2]["rubies"] == [{"start": 4, "end": 5, "text": "い"}]
    assert first[0]["wordId"] == 1467640 and first[0]["readingIndex"] == 1827755855
    assert first[0]["idNamespace"] == "jpdb"
    assert first[0]["card"]["meanings"] == ["meaning of 猫"]


def test_convert_repeated_words_gaps_and_same_vid_different_sid() -> None:
    converted = convert_parse_result(PARAGRAPHS, RESULT)
    repeated = [t for t in converted["tokens"][1] if t["surface"] == "今日"]
    assert [(t["start"], t["end"]) for t in repeated] == [(0, 2), (3, 5)]
    crow = [t for t in converted["tokens"][2] if t["wordId"] == 1171450]
    assert {t["readingIndex"] for t in crow} == {1031991566, 1650459493}
    # Unparsed tail stays a gap, never a fabricated token.
    assert [t["surface"] for t in converted["tokens"][3]] == ["ぽにょぽにょ"]
    unknown = converted["tokens"][3][0]["card"]
    assert unknown["inDeck"] is False and unknown["cardState"] == []
    keys = [(v["vid"], v["sid"]) for v in converted["vocabulary"]]
    assert len(keys) == len(set(keys)) == 11


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r["tokens"].pop(), "row count"),
        (lambda r: r["tokens"][0].append([99, 0, 1, None]), "invalid token"),
        (lambda r: r["tokens"][0].append([0, 0, 1, None]), "surrogate"),
        (lambda r: r["tokens"][0].append([0, 40, 1, None]), "outside"),
        (lambda r: r["tokens"][0].append(["0", 0, 1, None]), "non-integer"),
    ],
)
def test_convert_rejects_inconsistent_responses(mutate, message: str) -> None:
    import copy

    broken = copy.deepcopy(RESULT)
    mutate(broken)
    with pytest.raises(JpdbParseError, match=message):
        convert_parse_result(PARAGRAPHS, broken)


def test_furigana_that_does_not_spell_the_token_is_dropped() -> None:
    import copy

    broken = copy.deepcopy(RESULT)
    broken["tokens"][0][2] = [2, 4, 3, [["生", "い"], "き"]]  # covers 2 of 3 units
    token = convert_parse_result(PARAGRAPHS, broken)["tokens"][0][2]
    assert token["surface"] == "生きる"
    assert token["rubies"] == []


def test_utf16_slice_rejects_split_surrogates() -> None:
    assert utf16_slice("😀猫", 2, 3) == "猫"
    with pytest.raises(JpdbParseError):
        utf16_slice("😀猫", 1, 3)


@pytest.mark.parametrize(
    ("states", "in_deck", "expected"),
    [
        ([], False, "new"),
        (["new"], True, "new"),
        (["learning"], True, "learning"),
        (["known"], True, "known"),
        (["redundant", "known"], True, "known"),
        (["never-forget"], True, "known"),
        (["due"], True, "due"),
        (["failed"], True, "due"),
        (["blacklisted"], True, "blacklisted"),
        (["suspended"], True, "unknown"),
        (["locked", "new"], True, "new"),
        ([], True, "unknown"),
    ],
)
def test_jpdb_state_normalization_never_guesses(states, in_deck, expected) -> None:
    assert normalized_jpdb_state(states, in_deck) == expected


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


def _fake_jpdb(calls: list[tuple[str, Any]]):
    def fake(action, payload=None, **_kwargs):
        calls.append((action, payload))
        assert action == "parse"
        assert payload["position_length_encoding"] == "utf16"
        rows = []
        for text in payload["text"]:
            index = PARAGRAPHS.index(text)
            rows.append(RESULT["tokens"][index])
        return {"tokens": rows, "vocabulary": RESULT["vocabulary"]}

    return fake


def test_jpdb_backend_parses_selection_natively_without_jiten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(service, "_jpdb_request", _fake_jpdb(calls))
    monkeypatch.setattr(service, "_jiten_request", lambda *a, **k: pytest.fail("Jiten must not be used"))

    payload = service.parse_study_text("😀猫が生きる😀")
    token = payload["tokens"][0][0]
    assert token["idNamespace"] == "jpdb"
    assert token["reviewable"] is True
    assert (token["wordId"], token["readingIndex"]) == (1467640, 1827755855)
    assert token["card"]["normalizedState"] == "known"
    assert token["card"]["knowledgeStatus"] == "cached_parse_state"
    by_surface = {t["surface"]: t for t in payload["tokens"][0]}
    assert by_surface["生きる"]["card"]["normalizedState"] == "learning"

    again = service.parse_study_text("😀猫が生きる😀")
    assert again["tokens"] == payload["tokens"]
    assert len(calls) == 1  # persisted cache, no second request


def test_jpdb_parse_cache_is_isolated_per_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(service, "_jpdb_request", _fake_jpdb(calls))
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    service.parse_study_text("今日は今日で")
    service.save_settings({"jpdb_api_token": "tok-b", "study_backend": "jpdb"})
    service.parse_study_text("今日は今日で")
    assert len(calls) == 2


def test_parse_owned_by_another_account_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    monkeypatch.setattr(service, "_jpdb_request", _fake_jpdb([]))
    parsed = service._parse_text("今日は今日で", "h1")
    service.save_settings({"jpdb_api_token": "tok-b"})
    tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jpdb")
    assert all(t["reviewable"] is False for t in tokens[0])
    assert all(v["knowledgeStatus"] == "provider_mismatch" for v in vocabulary)
    assert all("cardState" not in t["card"] and t["card"]["states"] == [] for t in tokens[0])

    service.save_settings({"study_backend": "jiten"})
    tokens, _ = service._provider_scoped_study_payload(parsed, "jiten")
    assert all(t["reviewable"] is False and t["idNamespace"] == "jpdb" for t in tokens[0])


def test_jiten_backend_and_jiten_only_callers_keep_jiten_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jiten_api_key": "jk", "jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    jiten_calls: list[Any] = []

    def fake_jiten(action, payload=None):
        jiten_calls.append(action)
        return {"tokens": [[{"wordId": 1, "readingIndex": 0, "start": 0, "end": 1}]], "vocabulary": []}

    monkeypatch.setattr(service, "_jiten_request", fake_jiten)
    monkeypatch.setattr(service, "_jpdb_request", lambda *a, **k: pytest.fail("jpdb not expected"))
    # Episode gate / Jiten statistics use jiten_preparse even when jpdb is selected.
    parsed = service.jiten_preparse("猫")
    assert parsed.get("parseProvider", "jiten") == "jiten"
    assert jiten_calls == ["reader/parse"]
    assert service._cached_parse(
        __import__("hashlib").sha256(("study-v1\0猫").encode()).hexdigest(), provider="jpdb"
    ) is None


def test_jpdb_native_token_reviews_with_its_own_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    monkeypatch.setattr(service, "_jpdb_request", _fake_jpdb([]))
    token = service.parse_study_text("😀猫が生きる😀")["tokens"][0][0]
    sent: list[dict] = []

    class _Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {}

    def fake_post(url, *, headers, json, timeout):
        sent.append(dict(json))
        return _Resp()

    monkeypatch.setattr("pudge.review_providers.httpx.post", fake_post)
    result = service.study_action(
        "jpdb", "review", token["wordId"], token["readingIndex"],
        grade="okay", attempt_id="a1", id_namespace=token["idNamespace"],
    )
    assert result["outcome"] == "confirmed"
    assert sent == [{"vid": 1467640, "sid": 1827755855, "grade": "okay"}]


def test_invalid_jpdb_parse_response_surfaces_error_and_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    calls: list[Any] = []

    def broken(action, payload=None, **_kwargs):
        calls.append(action)
        return {"tokens": [], "vocabulary": []}

    monkeypatch.setattr(service, "_jpdb_request", broken)
    with pytest.raises(LightNovelError, match="invalid response"):
        service.parse_study_text("猫")
    with pytest.raises(LightNovelError):
        service.parse_study_text("猫")
    assert len(calls) == 2


def test_account_switch_between_batches_never_pollutes_other_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P2: A starts, settings switch to B mid-parse; B's answers must not land under A."""
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    monkeypatch.setattr(service, "JPDB_PARSE_BATCH_CHARS", 8)  # force several batches
    tokens_used: list[str] = []
    inner = _fake_jpdb([])

    def fake(action, payload=None, *, token=None):
        tokens_used.append(token)
        if len(tokens_used) == 1:
            service.save_settings({"jpdb_api_token": "tok-b"})  # switch while A's parse runs
        return inner(action, payload)

    monkeypatch.setattr(service, "_jpdb_request", fake)
    text = "\n".join(PARAGRAPHS[:3])
    with pytest.raises(LightNovelError, match="account changed during parsing"):
        service._parse_text(text, "h-switch")
    assert set(tokens_used) == {"tok-a"}, "every batch uses the token the operation started with"
    service.save_settings({"jpdb_api_token": "tok-a"})
    scope_a = service._parse_scope("jpdb")
    cached_a = service._cached_parse("h-switch", scope=scope_a)
    assert cached_a is not None and cached_a["parseAccountKey"] == scope_a[1]
    service.save_settings({"jpdb_api_token": "tok-b"})
    assert service._cached_parse("h-switch", scope=service._parse_scope("jpdb")) is None


def test_parse_refuses_to_start_when_scope_and_token_disagree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    scope_a = service._parse_scope("jpdb")
    service.save_settings({"jpdb_api_token": "tok-b"})  # switched while waiting for a slot
    monkeypatch.setattr(service, "_jpdb_request", lambda *a, **k: pytest.fail("no request with a mismatched token"))
    with pytest.raises(LightNovelError, match="account changed"):
        service._parse_text_jpdb("今日は今日で", "h2", scope_a, interactive=True)
