"""Grammar via OpenAI: JSON is requested at API level and no successful call disappears silently."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

import pudge.light_novels as ln_module
import pudge.llm as llm_module
from pudge.config import AppConfig, LLMConfig
from pudge.light_novels import LightNovelService
from pudge.llm import OllamaClient, build_openai_chat_payload


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status_code = status
        self.is_success = 200 <= status < 300
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.is_success:
            request = httpx.Request("POST", "https://api.example/v1/chat/completions")
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(self.status_code, request=request, json=self._payload))


def _client(monkeypatch, replies: list[_Resp], calls: list[dict]) -> OllamaClient:
    class Client:
        def __init__(self, *a, **k):
            pass

        def post(self, _url, *, json):
            calls.append(dict(json))
            return replies.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(llm_module.httpx, "Client", Client)
    return OllamaClient(LLMConfig(provider="openai", base_url="https://api.example", model="gpt-6-luna", reasoning_effort="low"))


def _ok(content: str, finish: str = "stop") -> _Resp:
    return _Resp(200, {"choices": [{"finish_reason": finish, "message": {"content": content}}]})


def test_openai_payload_requests_json_object() -> None:
    payload = build_openai_chat_payload(LLMConfig(provider="openai", model="m"), "s", "u")
    assert payload["response_format"] == {"type": "json_object"}


def test_gateway_without_response_format_is_retried_without_it(monkeypatch) -> None:
    calls: list[dict] = []
    client = _client(monkeypatch, [_Resp(400, {"error": {"message": "Unsupported parameter: response_format"}}), _ok('{"ok": true}')], calls)
    assert client.json_chat("s", "u") == {"ok": True}
    assert "response_format" in calls[0] and "response_format" not in calls[1]


def test_non_json_and_empty_content_are_explained(monkeypatch, caplog) -> None:
    calls: list[dict] = []
    client = _client(monkeypatch, [_ok("Here is prose, no object"), _ok("", finish="length")], calls)
    assert client.json_chat("s", "u") is None
    assert client.last_error == "LLM returned non-JSON content"
    assert client.json_chat("s", "u") is None
    assert client.last_error == "LLM returned empty content (finish_reason=length)"


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.llm.enabled = True
    cfg.llm.provider = "openai"
    cfg.llm.model = "gpt-6-luna"
    return LightNovelService(cfg)


class _FakeLLM:
    replies: list[Any] = []
    last_error = ""

    def __init__(self, _cfg):
        pass

    def json_chat(self, _s, _u):
        value = type(self).replies.pop(0)
        type(self).last_error = "" if value is not None else "LLM returned non-JSON content"
        return value

    def close(self):
        pass


def test_valid_answer_is_cached(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    _FakeLLM.replies = [{"translation": "I like cats", "points": [{"pattern": "～が好き", "quotes": [{"text": "が好き"}]}]}]
    monkeypatch.setattr(ln_module, "OllamaClient", _FakeLLM)
    result = service.analyze_grammar("猫が好きだ。", "", "r1")
    assert result["status"] == "complete" and result["points"][0]["spans"][0]["quote"] == "が好き"
    with sqlite3.connect(tmp_path / "pudge.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM ln_grammar_cache").fetchone()[0] == 1


def test_no_json_is_a_visible_llm_error_not_cached(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    _FakeLLM.replies = [None, None]
    monkeypatch.setattr(ln_module, "OllamaClient", _FakeLLM)
    result = service.analyze_grammar("猫が好きだ。", "", "r2")
    assert result["status"] == "error" and result["reason"] == "llm_no_json"
    assert "non-JSON" in result["detail"]
    with sqlite3.connect(tmp_path / "pudge.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM ln_grammar_cache").fetchone()[0] == 0


def test_wrong_schema_is_reported_with_its_keys(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    _FakeLLM.replies = [{"explanation": "x"}, {"explanation": "y"}]
    monkeypatch.setattr(ln_module, "OllamaClient", _FakeLLM)
    result = service.analyze_grammar("猫が好きだ。", "", "r3")
    assert result["reason"] == "invalid_structure"
    assert "explanation" in result["detail"]


def test_cache_write_failure_still_returns_the_analysis(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    _FakeLLM.replies = [{"translation": "", "points": []}]
    monkeypatch.setattr(ln_module, "OllamaClient", _FakeLLM)
    with sqlite3.connect(tmp_path / "pudge.sqlite3") as conn:
        conn.execute("DROP TABLE ln_grammar_cache")
    result = service.analyze_grammar("猫が好きだ。", "", "r4")
    assert result["status"] == "complete"


def test_clicking_inside_the_translation_popup_does_not_close_it() -> None:
    """Regression: the legacy LN pointerdown handler hid #pudgeTranslationPop, so
    "Explain grammar" never received its click."""
    html = (Path(__file__).resolve().parents[1] / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "if(!e.target.closest('#lnTranslatePop')&&!e.target.closest('#pudgeTranslationPop')&&!e.target.closest('#lnReader'))hideLnTranslation();" in html


def test_unsupported_effort_value_steps_down_instead_of_sending_temperature(monkeypatch) -> None:
    calls: list[dict] = []
    replies = [
        _Resp(400, {"error": {"message": "Unsupported value: 'reasoning_effort' does not support 'max' with this model.", "code": "unsupported_value"}}),
        _ok('{"ok": true}'),
    ]
    client = _client(monkeypatch, replies, calls)
    client.config.reasoning_effort = "max"
    assert client.json_chat("s", "u") == {"ok": True}
    assert calls[0]["reasoning_effort"] == "max"
    assert calls[1]["reasoning_effort"] == "xhigh" and "temperature" not in calls[1]


def test_rejected_temperature_is_dropped(monkeypatch) -> None:
    calls: list[dict] = []
    replies = [
        _Resp(400, {"error": {"message": "Unsupported parameter: reasoning_effort"}}),
        _Resp(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.0 with this model. Only the default (1) value is supported.", "code": "unsupported_value"}}),
        _ok('{"ok": true}'),
    ]
    client = _client(monkeypatch, replies, calls)
    assert client.json_chat("s", "u") == {"ok": True}
    assert "temperature" in calls[1] and "temperature" not in calls[2]


def test_learned_rejections_are_not_repeated_on_the_next_call(monkeypatch) -> None:
    calls: list[dict] = []
    replies = [
        _Resp(400, {"error": {"message": "Unsupported value: 'reasoning_effort' does not support 'max' with this model."}}),
        _ok('{"ok": 1}'),
        _ok('{"ok": 2}'),
    ]
    client = _client(monkeypatch, replies, calls)
    client.config.reasoning_effort = "max"
    assert client.json_chat("s", "u") == {"ok": 1}
    assert client.json_chat("s", "u") == {"ok": 2}
    assert len(calls) == 3 and calls[2]["reasoning_effort"] == "xhigh", "second call starts with the learned effort"


def test_chat_text_is_free_text_with_assistant_effort_and_usage_logged(monkeypatch, caplog) -> None:
    calls: list[dict] = []
    reply = _Resp(200, {"choices": [{"finish_reason": "stop", "message": {"content": "Это форма ～てしまう."}}],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 12}}})
    client = _client(monkeypatch, [reply], calls)
    client.effort_override = "low"
    client.operation = "assistant.chat"
    logged: list[str] = []
    monkeypatch.setattr(client.logger, "info", lambda msg, *a: logged.append(msg % a if a else msg))
    assert client.chat_text([{"role": "user", "content": "что это?"}]) == "Это форма ～てしまう."
    assert "response_format" not in calls[0] and calls[0]["reasoning_effort"] == "low"
    line = next(x for x in logged if "step=llm.call" in x)
    assert "operation=assistant.chat" in line and "reasoning_tokens=12" in line and "attempts=1" in line


def test_prompt_cache_key_is_sent_and_dropped_when_rejected(monkeypatch) -> None:
    calls: list[dict] = []
    replies = [_Resp(400, {"error": {"message": "Unrecognized request argument supplied: prompt_cache_key"}}), _ok('{"ok": true}'), _ok('{"ok": 2}')]
    client = _client(monkeypatch, replies, calls)
    client.operation = "grammar"
    assert client.json_chat("s", "u") == {"ok": True}
    assert calls[0]["prompt_cache_key"] == "pudge-grammar" and "prompt_cache_key" not in calls[1]
    client.json_chat("s", "u")
    assert "prompt_cache_key" not in calls[2], "learned for the session"
