from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
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


def test_provider_settings_are_migrated_and_managed_irodori_hides_api_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    api.light_novels.save_settings({"irodori_tts_enabled": True, "irodori_tts_caption": "calm"})
    settings = api.light_novels.settings_payload()
    assert settings["audiobook_generation_provider"] == "irodori"

    managed = api._audiobook_generation_runtime(
        {
            "provider": "irodori",
            "url": "https://should-not-be-used.invalid",
            "api_key": "secret",
            "model": "wrong-model",
            "voice": "wrong-voice",
            "speed": 1.25,
            "caption": "soft voice",
        }
    )
    assert managed["base_url"] == "http://127.0.0.1:8088"
    assert managed["api_key"] == ""
    assert managed["model"] == "irodori-tts"
    assert managed["voice"] == "none"
    assert managed["caption"] == "soft voice"
    assert managed["speed"] == pytest.approx(1.25)

    external = api._audiobook_generation_runtime(
        {
            "provider": "external",
            "url": "https://tts.example/v1/",
            "api_key": "key",
            "model": "tts-model",
            "voice": "speaker",
            "speed": 0.9,
        }
    )
    assert external["base_url"] == "https://tts.example/v1"
    assert external["api_key"] == "key"
    assert external["model"] == "tts-model"
    assert external["voice"] == "speaker"


def test_long_text_is_split_below_irodori_request_limit_and_counts_all_characters() -> None:
    text = ("これは長い文章です。" * 900) + " tail  "
    chunks = WebAppApi._split_audiobook_tts_text(text)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 3600 for chunk, _ in chunks)
    assert sum(consumed for _, consumed in chunks) == len(text)


class _Response:
    def __init__(self, *, status: int = 200, content: bytes = b"A" * 2048, text: str = "") -> None:
        self.status_code = status
        self.content = content
        self.text = text
        self.is_error = status >= 400

    def json(self):
        if not self.text:
            return {}
        raise ValueError


class _Client:
    def __init__(self, calls: list[dict], on_post=None, *args, **kwargs) -> None:
        self.calls = calls
        self.on_post = on_post

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, _url: str, *, headers: dict, json: dict):
        self.calls.append(dict(json))
        if self.on_post:
            self.on_post()
        return _Response()


def test_generation_progress_uses_characters_chunks_long_chapters_and_survives_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    api.light_novels.save_settings({"audiobook_generation_provider": "irodori"})
    text = "猫が好きです。" * 1000
    book_id = 77
    monkeypatch.setattr(
        api.light_novels,
        "tts_source",
        lambda _book_id: {"title": "Long Novel", "chapters": [{"title": "Chapter 1", "text": text}]},
    )
    monkeypatch.setattr(api, "test_irodori_tts", lambda *_a, **_k: {"ok": True})
    calls: list[dict] = []

    def prune_during_request() -> None:
        api.manager._prune_empty_library_dirs()
        generated = api.config.library.root_dir / "Generated Audiobooks" / f"Long Novel [Generated {book_id}]"
        assert generated.is_dir()
        assert (generated / ".pudge-audiobook-generating").is_file()

    monkeypatch.setattr(
        web_app_module.httpx,
        "Client",
        lambda *a, **k: _Client(calls, prune_during_request, *a, **k),
    )

    def fake_merge(parts: list[Path], output: Path) -> None:
        assert len(parts) == len(calls)
        output.write_bytes(b"M" * 4096)

    monkeypatch.setattr(api, "_merge_audiobook_parts", fake_merge)
    monkeypatch.setattr(api.audiobooks, "import_folder", lambda *_a, **_k: {"id": 901})
    monkeypatch.setattr(api.audiobooks, "link_light_novel", lambda *_a, **_k: {"ok": True})

    job_id = api.job_center.start("audiobook_generation", "test", total=len(text))
    with api._irodori_tts_lock:
        api._irodori_tts_job_ids[book_id] = job_id
    api._irodori_tts_worker(book_id, job_id)

    assert len(calls) >= 2
    assert all(len(body["input"]) <= 3600 for body in calls)
    job = api.job_center.get(job_id)
    assert job is not None
    assert job["state"] == "succeeded"
    assert job["total"] == pytest.approx(len(text))
    assert job["current"] == pytest.approx(len(text))


def test_422_error_body_is_preserved_and_managed_caption_has_compat_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    runtime = {
        "provider": "irodori",
        "base_url": "http://127.0.0.1:8088",
        "api_key": "",
        "model": "irodori-tts",
        "voice": "none",
        "caption": "calm",
        "speed": 1.0,
    }

    class CompatClient:
        def __init__(self):
            self.calls: list[dict] = []

        def post(self, _url, *, headers, json):
            self.calls.append(dict(json))
            if len(self.calls) == 1:
                return _Response(status=422, text='{"detail":"unsupported irodori field"}')
            return _Response()

    client = CompatClient()
    response = api._post_audiobook_speech(client, runtime, "本文", 1)
    assert response.status_code == 200
    assert "irodori" in client.calls[0]
    assert "irodori" not in client.calls[1]

    class RejectClient:
        def post(self, _url, *, headers, json):
            return _Response(status=422, text='input must be at most 4096 characters')

    with pytest.raises(RuntimeError, match="4096 characters"):
        api._post_audiobook_speech(RejectClient(), {**runtime, "caption": ""}, "本文", 2)


def test_failed_generation_state_remains_visible_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    job_id = api.job_center.start("audiobook_generation", "test", total=100)
    api.job_center.update(job_id, current=40, total=100)
    api.job_center.fail(job_id, RuntimeError("HTTP 422 — bad request"))
    with api._irodori_tts_lock:
        api._irodori_tts_job_ids[5] = job_id
    status = api._irodori_tts_status(5)
    assert status["active"] is False
    assert status["state"] == "failed"
    assert "422" in status["error"]
    assert status["current"] == pytest.approx(40)
    assert status["total"] == pytest.approx(100)


def test_install_status_only_marks_update_when_remote_revision_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    repo = api._irodori_repo_dir()
    (repo / ".git").mkdir(parents=True)
    python = api._irodori_managed_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("python", encoding="utf-8")

    local = "a" * 40
    monkeypatch.setattr(
        web_app_module.subprocess,
        "check_output",
        lambda command, **_kwargs: local + "\n" if "rev-parse" in command else local + "\tHEAD\n",
    )
    same = api.irodori_install_status(refresh=True)
    assert same["installed"] is True
    assert same["update_available"] is False

    remote = "b" * 40
    monkeypatch.setattr(
        web_app_module.subprocess,
        "check_output",
        lambda command, **_kwargs: local + "\n" if "rev-parse" in command else remote + "\tHEAD\n",
    )
    api._irodori_remote_revision_cache = ""
    different = api.irodori_install_status(refresh=True)
    assert different["update_available"] is True


def test_v91_ui_is_provider_neutral_and_patches_progress_in_place() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="s_ln_audiobook_provider"' in html
    assert "Audiobook generation" in html
    assert 'id="audiobookIrodoriFields"' in html
    assert 'id="audiobookExternalFields"' in html
    assert "Generate audiobook (Irodori)" not in html
    assert "Optional audiobook generation" not in html
    assert "Irodori is installed" not in html
    assert "patchLnAudiobookProgress(next)" in html
    signature = html.split("function lnStateRenderSignature", 1)[1].split("async function loadLightNovels", 1)[0]
    assert "irodori_tts" not in signature
    assert "slot.hidden=!(active||failed||resumable)" in html
    assert "update_available" in html
    assert "!!status.running||!status.installed||!!status.update_available" in html
    assert "Loading settings…" in html
