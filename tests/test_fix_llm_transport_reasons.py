"""Grammar explain: an LLM transport failure (401/403 key, 429, 5xx, no connection) is
reported as that cause, not as "the LLM answered something unreadable" (llm_no_json),
and a rejected key is not asked a second time."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import pudge.llm as llm_module
from pudge import iphone_grammar
from pudge.config import AppConfig, LLMConfig
from pudge.i18n import err
from pudge.light_novels import LightNovelService
from pudge.llm import OllamaClient
from test_grammar_analysis import READER

_REAL_CLIENT = httpx.Client
SENTENCE = "猫が好きだ。"
INVALIDATED = {
    "error": {
        "message": "Your API key has been invalidated.",
        "type": "invalid_request_error",
        "code": "token_invalidated",
    }
}


def _llm(monkeypatch, answers: list) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(
        llm_module.httpx,
        "Client",
        lambda **kwargs: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    return seen


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.llm.enabled = True
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://llm.example/v1"
    cfg.llm.model = "model-x"
    return LightNovelService(cfg)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx.Response(401, json=INVALIDATED), "llm_auth"),
        (httpx.Response(403, json={"error": {"message": "forbidden"}}), "llm_auth"),
        (
            httpx.Response(429, json={"error": {"message": "quota", "code": "insufficient_quota"}}),
            "llm_rate_limited",
        ),
        (httpx.Response(503, text="upstream down"), "llm_service_unavailable"),
        (httpx.ConnectError("connection refused"), "llm_service_unavailable"),
        (httpx.ReadTimeout("timed out"), "llm_service_unavailable"),
        (_ok("prose, no object"), "llm_no_json"),
    ],
)
def test_client_classifies_why_a_call_failed(monkeypatch, answer, reason) -> None:
    _llm(monkeypatch, [answer])
    client = OllamaClient(LLMConfig(provider="openai", base_url="https://llm.example/v1", model="model-x"))
    assert client.json_chat("s", "u") is None
    assert client.last_error_reason == reason


def test_success_clears_the_reason(monkeypatch) -> None:
    _llm(monkeypatch, [httpx.Response(401, json=INVALIDATED), _ok('{"a": 1}')])
    client = OllamaClient(LLMConfig(provider="openai", base_url="https://llm.example/v1", model="model-x"))
    assert client.json_chat("s", "u") is None and client.last_error_reason == "llm_auth"
    assert client.json_chat("s", "u") == {"a": 1} and client.last_error_reason == ""


def test_invalidated_key_is_reported_as_such_and_not_asked_twice(tmp_path, monkeypatch) -> None:
    seen = _llm(monkeypatch, [httpx.Response(401, json=INVALIDATED), _ok("{}")])
    result = _service(tmp_path).analyze_grammar(SENTENCE, "", "r1")
    assert result["status"] == "error" and result["reason"] == "llm_auth"
    assert "token_invalidated" in result["detail"]
    assert len(seen) == 1  # no schema-reminder retry after a rejected key


@pytest.mark.parametrize(
    ("answers", "reason", "calls"),
    [
        ([httpx.Response(429, json={"error": {"message": "slow down"}})], "llm_rate_limited", 1),
        ([httpx.Response(502, text="bad gateway")], "llm_service_unavailable", 1),
        ([httpx.ConnectError("no route")], "llm_service_unavailable", 1),
        ([_ok("not json"), _ok("still not json")], "llm_no_json", 2),
    ],
)
def test_grammar_reason_follows_the_transport(tmp_path, monkeypatch, answers, reason, calls) -> None:
    seen = _llm(monkeypatch, list(answers))
    result = _service(tmp_path).analyze_grammar(SENTENCE, "", "r")
    assert result["reason"] == reason and len(seen) == calls


def test_reading_assistant_reports_the_transport_reason(tmp_path, monkeypatch) -> None:
    _llm(monkeypatch, [httpx.Response(401, json=INVALIDATED)])
    result = _service(tmp_path).assistant_chat([{"role": "user", "content": "なぜ？"}], SENTENCE)
    assert result["ok"] is False and result["reason"] == "llm_auth"


def test_phone_gets_a_readable_message_per_cause() -> None:
    assert iphone_grammar._reason("llm_auth") == err("iphone.grammarLlmKey")
    assert iphone_grammar._reason("llm_rate_limited") == err("iphone.grammarLlmLimited")
    assert iphone_grammar._reason("llm_service_unavailable") == err("iphone.grammarLlmDown")
    assert iphone_grammar._reason("llm_no_json") == err("iphone.grammarInvalidAnswer")


def test_mac_reader_shows_the_cause(page) -> None:
    reasons = {
        "llm_auth": "reading.grammar.llmAuth",
        "llm_rate_limited": "reading.grammar.llmRateLimited",
        "llm_service_unavailable": "reading.grammar.llmUnavailable",
        "llm_no_json": "reading.grammar.llmNoJson",
    }
    for lang in ("en", "ru"):
        result = page.run(
            f"""
            const reasons = {json.dumps(reasons)};
            const out = {{}};
            for (const [reason, key] of Object.entries(reasons)) {{
              const html = PudgeReadingTools.grammar.html({{status: 'error', reason, detail: 'HTTP 401'}});
              out[reason] = {{html, want: PudgeI18n.t(key), key}};
            }}
            return out;
            """,
            lang=lang,
            **READER,
        )
        for reason, row in result.value.items():
            assert row["want"] and row["want"] != row["key"], (lang, reason)
            assert row["want"] in row["html"].replace("&#39;", "'").replace("&quot;", '"'), (lang, reason)
