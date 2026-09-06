from __future__ import annotations

import json
from pathlib import Path

import pytest

import pudge.llm as llm_module
import pudge.web_app as web_app_module
from pudge.config import AppConfig, load_config, write_config
from pudge.llm import OllamaClient, list_models
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


def test_llm_provider_roundtrip(tmp_path: Path) -> None:
    config = AppConfig()
    path = tmp_path / "config.toml"
    config.config_path = path
    config.llm.enabled = True
    config.llm.provider = "openai"
    config.llm.base_url = "https://gateway.example/v1-root"
    config.llm.api_key = "secret"
    config.llm.model = "arbitrary-model"
    write_config(config, path)
    loaded = load_config(path)
    assert loaded.llm.provider == "openai"
    assert loaded.llm.base_url == "https://gateway.example/v1-root"
    assert loaded.llm.model == "arbitrary-model"


def test_openai_model_listing_uses_v1_models(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "z-model"}, {"id": "a-model"}]}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return Response()

    monkeypatch.setattr(llm_module.httpx, "get", fake_get)
    assert list_models("https://llm.example", "key", provider="openai") == ["a-model", "z-model"]
    assert seen["url"] == "https://llm.example/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer key"

    assert list_models("https://llm.example/v1/", "key", provider="openai") == ["a-model", "z-model"]
    assert seen["url"] == "https://llm.example/v1/models"


def test_openai_chat_completion_parses_fenced_json(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Response:
        is_success = True

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '```json\\n{"ok": true}\\n```'}}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, *, json):
            calls.append((url, json))
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(llm_module.httpx, "Client", Client)
    cfg = AppConfig().llm
    cfg.provider = "openai"
    cfg.base_url = "https://llm.example"
    cfg.model = "anything"
    client = OllamaClient(cfg)
    try:
        assert client.json_chat("system", "user") == {"ok": True}
    finally:
        client.close()
    assert calls[0][0] == "https://llm.example/v1/chat/completions"
    assert calls[0][1]["model"] == "anything"
    assert "think" not in calls[0][1]


def test_test_llm_provider_accepts_gateway_without_model_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(web_app_module, "list_models", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("404")))

    class FakeClient:
        def __init__(self, cfg):
            assert cfg.provider == "openai"
            assert cfg.model == "custom-model"

        def json_chat(self, _system, _user):
            return {"ok": True}

        def close(self):
            pass

    monkeypatch.setattr(web_app_module, "OllamaClient", FakeClient)
    result = api.test_llm_provider(
        {"provider": "openai", "url": "https://gateway.example", "api_key": "key", "model": "custom-model"}
    )
    assert result["ok"] is True
    assert result["provider"] == "openai"
    assert result["model"] == "custom-model"


def test_voice_units_preserve_source_exactly() -> None:
    source = "地の文。『こんにちは』そして「またね」。終わり。"
    units = WebAppApi._audiobook_voice_units(source)
    assert "".join(str(unit["text"]) for unit in units) == source
    assert [bool(unit["dialogue"]) for unit in units] == [False, True, False, True, False]
    assert sum(int(unit["consumed"]) for unit in units) == len(source)


def test_character_voice_annotation_uses_known_characters_and_stable_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    api.config.llm.enabled = True
    api.config.llm.provider = "openai"
    api.config.llm.base_url = "https://llm.example"
    api.config.llm.model = "speaker-model"
    prompts = []

    class FakeClient:
        def __init__(self, _cfg):
            pass

        def json_chat(self, _system, user):
            payload = json.loads(user)
            prompts.append(payload)
            return {
                "assignments": [
                    {"id": row["id"], "speaker": "堀北鈴音", "caption": "若い女性。低めで澄んだ声。落ち着いた硬質な話し方。"}
                    for row in payload["segments"]
                    if row["kind"] == "dialogue"
                ]
            }

        def close(self):
            pass

    monkeypatch.setattr(web_app_module, "OllamaClient", FakeClient)
    known = [{"source": "堀北", "preferred": "堀北鈴音"}]
    source = "彼女は振り返った。「行くわよ」その声は静かだった。"
    first = api._audiobook_speaker_parts(
        book_id=12,
        book_title="Test LN",
        chapter_index=1,
        chapter_text=source,
        base_caption="narrator",
        known_characters=known,
    )
    assert prompts and prompts[0]["known_characters"] == [
        {"source": "堀北", "preferred": "堀北鈴音", "reading": ""}
    ]
    assert any(speaker == "堀北鈴音" for _, _, _, speaker in first)
    assert sum(consumed for _, consumed, _, _ in first) == len(source)

    # Cached chapter/profile must make the second call LLM-free and keep the same voice caption.
    monkeypatch.setattr(web_app_module, "OllamaClient", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("LLM should not run")))
    second = api._audiobook_speaker_parts(
        book_id=12,
        book_title="Test LN",
        chapter_index=1,
        chapter_text=source,
        base_caption="narrator",
        known_characters=known,
    )
    assert second == first


def test_character_voice_llm_failure_falls_back_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    api.config.llm.enabled = True
    api.config.llm.provider = "openai"
    api.config.llm.model = "bad-model"

    class FakeClient:
        def __init__(self, _cfg):
            pass

        def json_chat(self, *_a):
            return None

        def close(self):
            pass

    monkeypatch.setattr(web_app_module, "OllamaClient", FakeClient)
    source = "  地の文。「台詞です」続き。  "
    parts = api._audiobook_speaker_parts(
        book_id=13,
        book_title="Test LN",
        chapter_index=1,
        chapter_text=source,
        base_caption="narrator voice",
    )
    assert sum(consumed for _, consumed, _, _ in parts) == len(source)
    assert all(caption == "narrator voice" for _, _, caption, _ in parts)


def test_tts_source_includes_anilist_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    source_file = tmp_path / "volume.txt"
    source_file.write_text("本文", encoding="utf-8")
    # Minimal DB insert avoids depending on import parsers.
    now = 1.0
    with api.light_novels._connect() as conn:
        cur = conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("LN", str(source_file), "txt", 1, 12345, now, now),
        )
        book_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "Chapter", "本文", "hash"),
        )
    assert api.light_novels.tts_source(book_id)["anilist_id"] == 12345


def test_v92_ui_avoids_tts_resume_full_render_and_has_generic_llm_settings() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="s_llm_provider"' in html
    assert "OpenAI-compatible API" in html
    assert 'id="testLlmProvider"' in html
    assert 'id="s_ln_character_voices"' in html
    poll = html.split("function scheduleLnStatePoll", 1)[1].split("function lnBookCard", 1)[0]
    assert "wasAudiobookActive||nextAudiobookActive" in poll
    assert "patchLnAudiobookProgress(next)" in poll
    assert "bookListChanged" in poll
    assert "wasAudiobookActive&&!nextAudiobookActive" in poll
