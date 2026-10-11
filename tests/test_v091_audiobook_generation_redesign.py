from __future__ import annotations

import json
from pathlib import Path

import pytest

import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi
from tests.i18n_support import en, has_text, ru


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
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
            return _Response(status=422, text="input must be at most 4096 characters")

    with pytest.raises(RuntimeError, match="4096 characters"):
        api._post_audiobook_speech(RejectClient(), {**runtime, "caption": ""}, "本文", 2)


def test_failed_generation_state_remains_visible_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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
    monkeypatch.setattr(api.light_novels, "book", lambda book_id: {"id": book_id})
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


GENERATION_SETTINGS = r"""
setPage('settings', true); await __tick(300);
await __click('button[data-settings-category="advanced"]');
const block = document.getElementById('s_ln_audiobook_provider').closest('.setting-block');
const install = () => __visible('#installIrodoriTts') ? [__text('#installIrodoriTts'), document.getElementById('installIrodoriTts').disabled] : null;
const view = () => ({irodori: __visible('#audiobookIrodoriFields'), external: __visible('#audiobookExternalFields'), install: install()});
"""


def test_audiobook_generation_settings_are_provider_neutral(page) -> None:
    assert (en("ui.audiobookGeneration"), ru("ui.audiobookGeneration")) == (
        "Audiobook generation",
        "Создание аудиокниг",
    )
    for text in ("Generate audiobook (Irodori)", "Optional audiobook generation", "Irodori is installed"):
        assert not has_text(text)
    result = page.run(
        GENERATION_SETTINGS
        + r"""
        const out = {heading: block.querySelector('h3').innerText, shown: block.checkVisibility(),
                     options: [...block.querySelectorAll('#s_ln_audiobook_provider option')].map(o => o.value), start: view()};
        for (const provider of ['external', 'off', 'irodori']) { await __input('#s_ln_audiobook_provider', provider); await __tick(50); out[provider] = view(); }
        out.texts = {heading: t('ui.audiobookGeneration'), install: t('ui.installIrodori')};
        out.page = document.getElementById('settingsContent').innerText;
        return out;
        """,
        state={"settings": {"light_novels": {"audiobook_generation_provider": "irodori"}}},
        api={"irodori_install_status": {"installed": False, "running": False}},
    )
    out = result.value
    assert out["heading"] == out["texts"]["heading"] and out["shown"] is True
    assert out["options"] == ["off", "irodori", "external"]
    # Only the chosen provider's fields are shown; Irodori offers its installer while it is missing.
    assert (
        out["start"]
        == out["irodori"]
        == {"irodori": True, "external": False, "install": [out["texts"]["install"], False]}
    )
    assert out["external"] == {"irodori": False, "external": True, "install": None}
    assert out["off"] == {"irodori": False, "external": False, "install": None}
    for text in ("Generate audiobook (Irodori)", "Optional audiobook generation", "Irodori is installed"):
        assert text not in out["page"]


def test_irodori_button_installs_when_missing_and_updates_when_installed(page) -> None:
    # An installed Irodori always offers Update (it says itself when main has nothing new).
    cases = {
        "missing": ({"installed": False, "running": False}, "ui.installIrodori", False),
        "update": (
            {"installed": True, "running": False, "update_available": True},
            "ui.updateIrodori",
            False,
        ),
        "current": (
            {"installed": True, "running": False, "update_available": False},
            "ui.updateIrodori",
            False,
        ),
        "running": ({"installed": False, "running": True}, "ui.installing", True),
    }
    for name, (status, label, disabled) in cases.items():
        result = page.run(
            GENERATION_SETTINGS
            + f"return {{install: install(), label: {json.dumps(label)} && t({json.dumps(label)})}};",
            state={"settings": {"light_novels": {"audiobook_generation_provider": "irodori"}}},
            api={"irodori_install_status": status},
        )
        out = result.value
        assert out["install"] == (None if label is None else [out["label"], disabled]), name


def test_settings_page_says_loading_before_the_state_arrives(page) -> None:
    # Before the first state the language is not known yet: the page speaks English.
    result = page.run(
        r"""
        await __click('.nav button[data-page="settings"]'); await __tick(50);
        const loading = __text('#settingsContent');
        window.releaseState(); await __tick(500);
        return {loading, loaded: Boolean(document.getElementById('s_language'))};
        """,
        # The backend is slow to answer the first state request.
        before="""
        const state = __api.get_state_fast;
        let release; const answered = new Promise(resolve => { release = resolve; });
        window.releaseState = release;
        __api.get_state_fast = async (...args) => { await answered; return state(...args); };
        __api.get_state = __api.get_state_fast;
        """,
    )
    assert result.value == {"loading": en("uiB.settings.loading"), "loaded": True}
    assert en("uiB.settings.loading") == "Loading settings…"


# The LN library while book 6 generates its audiobook: window.nextLn is what the next poll returns.
LN_GENERATION = """
const book = tts => ({id: 6, title: 'Novel', volume: 1, character_count: 1000, irodori_tts: tts});
window.lnState = tts => ({version: 1, settings: {audiobook_generation_provider: 'irodori'}, irodori_tts_active: !!tts.active, books: [book(tts)]});
window.nextLn = window.lnState({active: true, state: 'running', current: 10, total: 100});
__api.light_novel_state = async () => window.nextLn;
"""


def test_generation_progress_is_patched_in_place_without_redrawing_the_library(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels', true); await __tick(300);
        const card = () => document.querySelector('[data-ln-book="6"]');
        const slot = () => card().querySelector('[data-ln-tts-status]');
        const view = () => ({shown: slot().checkVisibility(), percent: slot().querySelector('[data-ln-tts-percent]').innerText,
                             failed: slot().classList.contains('danger'), same: card() === first});
        const first = card();
        const out = {start: view()};
        window.nextLn = lnState({active: true, state: 'running', current: 40, total: 100}); await __tick(1000);
        out.progress = view();
        window.nextLn = lnState({active: false, state: 'failed', current: 55, total: 100, error: 'HTTP 422'}); await __tick(1000);
        out.failed = view();
        return out;
        """,
        before=LN_GENERATION,
    )
    out = result.value
    assert out["start"] == {"shown": True, "percent": "10%", "failed": False, "same": True}
    assert out["progress"] == {"shown": True, "percent": "40%", "failed": False, "same": True}
    # A failed job stays visible (until retried) — and the card is still not redrawn.
    assert out["failed"] == {"shown": True, "percent": "55%", "failed": True, "same": True}
