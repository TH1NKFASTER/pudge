"""OpenAI-compatible LLM: reasoning effort (behaviour; audit 71Б rewrite of the settings source checks)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import pudge.llm as llm_module
import pudge.web_app as web_app_module
from pudge.config import AppConfig, LLMConfig, load_config, write_config
from pudge.llm import OllamaClient, build_openai_chat_payload
from pudge.web_app import WebAppApi


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebAppApi:
    monkeypatch.setattr(web_app_module, "DATA_DIR", tmp_path / "data")
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    write_config(config, config.config_path)
    return WebAppApi(config.config_path)


def test_openai_reasoning_defaults_to_low_and_roundtrips(tmp_path: Path) -> None:
    assert LLMConfig().reasoning_effort == "low"
    config = AppConfig()
    path = tmp_path / "config.toml"
    config.llm.reasoning_effort = "high"
    write_config(config, path)
    assert load_config(path).llm.reasoning_effort == "high"


def test_openai_low_reasoning_omits_temperature() -> None:
    config = LLMConfig(provider="openai", model="gpt-5.6-luna", reasoning_effort="low", temperature=0.7)
    payload = build_openai_chat_payload(config, "system", "user")
    assert payload["reasoning_effort"] == "low"
    assert "temperature" not in payload


def test_openai_none_reasoning_keeps_temperature() -> None:
    config = LLMConfig(provider="openai", model="gpt-5.6-luna", reasoning_effort="none", temperature=0.3)
    payload = build_openai_chat_payload(config, "system", "user")
    assert payload["reasoning_effort"] == "none"
    assert payload["temperature"] == 0.3


def test_openai_compatible_retries_without_reasoning_when_gateway_rejects_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class Response:
        def __init__(self, *, ok: bool, status: int, payload: dict):
            self.is_success = ok
            self.status_code = status
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.is_success:
                request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
                response = httpx.Response(self.status_code, request=request, json=self._payload)
                raise httpx.HTTPStatusError("failed", request=request, response=response)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, _url, *, json):
            calls.append(dict(json))
            if len(calls) == 1:
                return Response(
                    ok=False,
                    status=400,
                    payload={"error": {"message": "Unsupported parameter: reasoning_effort"}},
                )
            return Response(
                ok=True,
                status=200,
                payload={"choices": [{"message": {"content": '{"ok": true}'}}]},
            )

        def close(self):
            pass

    monkeypatch.setattr(llm_module.httpx, "Client", Client)
    client = OllamaClient(
        LLMConfig(
            provider="openai",
            base_url="https://gateway.example",
            model="custom-model",
            reasoning_effort="low",
            temperature=0.2,
        )
    )
    try:
        assert client.json_chat("system", "user") == {"ok": True}
    finally:
        client.close()
    assert calls[0]["reasoning_effort"] == "low"
    assert "temperature" not in calls[0]
    assert "reasoning_effort" not in calls[1]
    assert calls[1]["temperature"] == 0.2


def test_llm_test_surfaces_provider_error_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(web_app_module, "list_models", lambda *a, **k: [])

    class FakeClient:
        def __init__(self, cfg):
            assert cfg.reasoning_effort == "low"
            self.last_error = "HTTP 429: You exceeded your current quota."

        def json_chat(self, _system, _user):
            return None

        def close(self):
            pass

    monkeypatch.setattr(web_app_module, "OllamaClient", FakeClient)
    with pytest.raises(RuntimeError, match="HTTP 429: You exceeded your current quota"):
        api.test_llm_provider(
            {
                "provider": "openai",
                "url": "https://api.openai.com/v1",
                "api_key": "key",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
            }
        )


LLM_ON = {
    "llm_enabled": True,
    "llm_provider": "openai",
    "llm_url": "https://api.openai.com/v1",
    "llm_model": "gpt-5.6-luna",
}
SAVE = "__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});"


def test_reasoning_effort_is_an_openai_setting_that_defaults_to_low_and_is_saved_and_tested(page) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(100);
        await __click('[data-settings-category="advanced"]'); await __tick(10);
        const reasoning = () => __visible('label[for="s_llm_reasoning"]');
        const out = {shown: $('s_llm_reasoning').value, visible: reasoning()};
        await __input('#s_llm_provider', 'ollama'); out.ollama = reasoning();
        await __input('#s_llm_provider', 'openai');
        await __input('#s_llm_reasoning', 'high');
        await __click('#testLlmProvider'); await __tick(10);
        await __click('#saveSettings'); await __tick(10);
        return out;
        """,
        state={"settings": LLM_ON},  # saved before reasoning effort existed
        before=SAVE
        + "__api.test_llm_provider = async values => ({ok: true, provider: values.provider, model: values.model});",
    )
    out = result.value
    assert out == {"shown": "low", "visible": True, "ollama": False}
    [[tested]] = result.calls_of("test_llm_provider")
    assert (tested["provider"], tested["model"], tested["reasoning_effort"]) == (
        "openai",
        "gpt-5.6-luna",
        "high",
    )
    assert result.calls_of("save_settings")[0][0]["llm_reasoning_effort"] == "high"
    stored = page.run(
        "setPage('settings'); await __tick(100); return $('s_llm_reasoning').value;",
        state={"settings": {**LLM_ON, "llm_reasoning_effort": "medium"}},
    )
    assert stored.value == "medium"


def test_web_settings_save_and_return_reasoning_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    result = api.save_settings({"llm_provider": "openai", "llm_reasoning_effort": "high"})
    assert result["ok"] is True
    assert api.config.llm.reasoning_effort == "high"
    assert api.get_state()["settings"]["llm_reasoning_effort"] == "high"
