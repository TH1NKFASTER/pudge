from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.metadata_cache import MetadataCache
from pudge.web_app import WebAppApi
from tests.i18n_support import en


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _api(tmp_path: Path) -> WebAppApi:
    config = _config(tmp_path)
    write_config(config, config.config_path)
    return WebAppApi(config.config_path)


def test_metadata_cache_clear_removes_only_namespace(tmp_path: Path) -> None:
    first = MetadataCache(tmp_path, "first", schema="v1")
    second = MetadataCache(tmp_path, "second", schema="v1")
    first.put("a", 1)
    first.put("b", 2)
    second.put("c", 3)

    assert first.clear() == 2
    assert first.get("a", ttl_seconds=60) is None
    assert second.get("c", ttl_seconds=60) == 3
    assert first.clear() == 0


def test_irodori_settings_are_optional_and_roundtrip(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    defaults = service.settings_payload()
    assert defaults["irodori_tts_enabled"] is False
    assert defaults["irodori_tts_auto_generate"] is False
    assert defaults["irodori_tts_url"] == "http://127.0.0.1:8088"

    saved = service.save_settings(
        {
            "irodori_tts_enabled": True,
            "irodori_tts_auto_generate": True,
            "irodori_tts_url": "http://localhost:9999/",
            "irodori_tts_api_key": "secret",
            "irodori_tts_voice": "narrator",
            "irodori_tts_caption": "落ち着いた声",
            "irodori_tts_speed": 1.15,
        }
    )
    assert saved["irodori_tts_enabled"] is True
    assert saved["irodori_tts_auto_generate"] is True
    assert saved["irodori_tts_url"] == "http://localhost:9999"
    assert saved["irodori_tts_voice"] == "narrator"
    assert saved["irodori_tts_speed"] == pytest.approx(1.15)


def test_jiten_manual_refresh_invalidates_metadata_and_only_parses_existing_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path)
    novel = tmp_path / "Novel.txt"
    novel.write_text("第一章\n猫が好きです。\n\n第二章\n犬も好きです。", encoding="utf-8")
    book = api.light_novels.import_file(novel)
    api.light_novels.save_settings({"jiten_api_key": "key"})

    now = time.time()
    with api.manager.db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(tmp_path / "manga.cbz"), "Manga", 1, 0, "rtl", now, now),
        )
        manga_id = int(conn.execute("SELECT id FROM manga_books WHERE title='Manga'").fetchone()[0])
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (manga_id, 0, "region-1", "既にOCRされた漫画本文", now),
        )

    api.light_novels._jiten_media_cache.put({"media": 1}, {"coverage": 90})
    parsed: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        api.light_novels,
        "jiten_preparse",
        lambda text, digest=None: parsed.append((str(text), digest)) or {"tokens": []},
    )
    # If manual refresh accidentally launches OCR, this test must fail.  It should
    # consume only manga_ocr_cache rows that already exist.
    monkeypatch.setattr(
        api.manga, "ocr_page", lambda *_a, **_k: pytest.fail("OCR must stay lazy"), raising=False
    )

    result = api.refresh_jiten_data()
    assert result["started"] is True
    assert result["invalidated"] == 1
    thread = api._jiten_refresh_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert any("猫が好き" in text or "犬も好き" in text for text, _ in parsed)
    assert any(text == "既にOCRされた漫画本文" for text, _ in parsed)
    job = api.job_center.get(result["job_id"])
    assert job is not None and job["state"] == "succeeded"
    assert int(book["id"]) > 0


def test_jiten_manual_refresh_requires_key(tmp_path: Path) -> None:
    api = _api(tmp_path)
    with pytest.raises(LightNovelError):
        api.refresh_jiten_data()


class _AudioResponse:
    content = b"I" * 2048

    def raise_for_status(self) -> None:
        return None


class _FakeIrodoriClient:
    def __init__(self, calls: list[tuple[str, dict]], *args, **kwargs) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url: str, *, headers: dict, json: dict):
        self.calls.append((url, json))
        return _AudioResponse()


def test_irodori_generation_is_chaptered_reusable_and_skips_whisper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path)
    novel = tmp_path / "Novel.txt"
    novel.write_text("第一章\n猫が好きです。\n\n第二章\n犬も好きです。", encoding="utf-8")
    book = api.light_novels.import_file(novel)
    api.light_novels.save_settings(
        {
            "irodori_tts_enabled": True,
            "irodori_tts_url": "http://127.0.0.1:8088",
            "irodori_tts_voice": "none",
            "irodori_tts_caption": "narration",
            "irodori_tts_speed": 1.1,
        }
    )

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(api, "test_irodori_tts", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        "pudge.web_app.httpx.Client",
        lambda *a, **k: _FakeIrodoriClient(calls, *a, **k),
    )
    imported: list[tuple[Path, dict]] = []
    linked: list[tuple[int, int, dict]] = []

    def fake_import(folder: Path, **kwargs):
        imported.append((Path(folder), kwargs))
        files = sorted(Path(folder).glob("*.mp3"))
        assert files and all(path.stat().st_size == 2048 for path in files)
        return {"id": 77}

    def fake_link(ln_book_id: int, audiobook_id: int, **kwargs):
        linked.append((ln_book_id, audiobook_id, kwargs))
        return {"ok": True}

    monkeypatch.setattr(api.audiobooks, "import_folder", fake_import)
    monkeypatch.setattr(api.audiobooks, "link_light_novel", fake_link)

    job_id = api.job_center.start("irodori_tts", "test", total=2)
    api._irodori_tts_worker(int(book["id"]), job_id)

    assert calls
    assert all(url.endswith("/v1/audio/speech") for url, _ in calls)
    assert all(body["model"] == "irodori-tts" for _, body in calls)
    assert all(body["response_format"] == "mp3" for _, body in calls)
    assert all(body["irodori"]["caption"] == "narration" for _, body in calls)
    assert imported[0][1] == {"auto_link": False, "prepare_transcription": False}
    assert linked == [(int(book["id"]), 77, {"prepare_alignment": False})]
    assert api.job_center.get(job_id)["state"] == "succeeded"

    # Retry: existing chapter files are reused, so no TTS requests are made.
    calls.clear()
    retry_job = api.job_center.start("irodori_tts", "retry", total=2)
    api._irodori_tts_worker(int(book["id"]), retry_job)
    assert calls == []
    assert api.job_center.get(retry_job)["state"] == "succeeded"


def test_generated_folder_can_skip_autolink_and_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "generated"
    folder.mkdir()
    (folder / "0001.mp3").write_bytes(b"audio")
    service = AudiobookService(
        Database(tmp_path / "audio.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache-audio"
    )
    monkeypatch.setattr(service, "_probe", lambda _path: (12.0, []))
    monkeypatch.setattr(
        service, "auto_link_audiobook", lambda *_a, **_k: pytest.fail("auto-link must be skipped")
    )
    monkeypatch.setattr(
        service, "prepare_transcription", lambda *_a, **_k: pytest.fail("STT must be skipped")
    )

    book = service.import_folder(folder, auto_link=False, prepare_transcription=False)

    assert book["multi_file"] is True
    assert book["duration"] == pytest.approx(12.0)


def test_irodori_generation_disabled_has_no_side_effects(tmp_path: Path) -> None:
    api = _api(tmp_path)
    novel = tmp_path / "Novel.txt"
    novel.write_text("本文", encoding="utf-8")
    book = api.light_novels.import_file(novel)
    with pytest.raises(LightNovelError):
        api.generate_light_novel_tts(int(book["id"]))
    assert api._irodori_tts_threads == {}


def test_sidebar_refresh_menu_updates_jiten_data(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await __fire('#refreshAll', 'contextmenu', {clientX: 20, clientY: 20}, 'MouseEvent');
            const item = document.querySelector('#contextMenu [data-sidebar-context-action=jiten]');
            const label = item.innerText;
            item.click(); await __tick(10);
            return {label, toast: __text('#toast'), texts: [t('ui.updateJitenData'), t('ui.jitenRefreshIsRunningLn', {light_novels: 2, manga_regions: 5})]};
            """,
            lang=lang,
            api={"refresh_jiten_data": {"started": True, "light_novels": 2, "manga_regions": 5}},
        )
        out = result.value
        assert out["label"] == out["texts"][0]
        assert out["toast"] == out["texts"][1]
        assert result.calls_of("refresh_jiten_data") == [[]]


def test_audiobook_generation_is_optional_and_lives_in_advanced_settings(page, tmp_path: Path) -> None:
    assert en("ui.audiobookGeneration") == "Audiobook generation"
    api = _api(tmp_path)
    state = json.loads(json.dumps(api.get_state(), default=str))
    result = page.run(
        r"""
        setPage('settings'); await __tick(50);
        const fields = () => ({provider: $('s_ln_audiobook_provider').value, shown: __visible('#s_ln_audiobook_provider'),
                               irodori: __visible('#audiobookIrodoriFields'), external: __visible('#audiobookExternalFields'),
                               auto: __visible('#audiobookAutoRow')});
        const essential = fields();
        await __click('#settingsContent [role=tab][data-settings-category=advanced]');
        const advanced = {...fields(), heading: $('s_ln_audiobook_provider').closest('.setting-block').querySelector('h3').innerText,
                          expected: t('ui.audiobookGeneration')};
        await __input('#s_ln_audiobook_provider', 'irodori');
        const irodori = fields();
        await __input('#s_ln_irodori_auto', true);
        await __click('#testAudiobookGeneration'); await __tick(10);
        const tested = {toasts: __toasts(), ready: t('ui.audiobookGenerationIsReady')};
        await __click('#saveSettings'); await __tick(10);
        return {essential, advanced, irodori, tested};
        """,
        state=state,
        api={"test_audiobook_generation": {"ok": True}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    out = result.value
    # Off by default, and not among the essential settings.
    assert out["essential"]["provider"] == "off" and out["essential"]["shown"] is False
    assert out["advanced"]["shown"] is True and out["advanced"]["heading"] == out["advanced"]["expected"]
    assert (out["advanced"]["irodori"], out["advanced"]["external"], out["advanced"]["auto"]) == (
        False,
        False,
        False,
    )
    assert (out["irodori"]["irodori"], out["irodori"]["external"], out["irodori"]["auto"]) == (
        True,
        False,
        True,
    )
    [[test_values]] = result.calls_of("test_audiobook_generation")
    assert test_values["provider"] == "irodori"
    assert out["tested"]["ready"] in out["tested"]["toasts"]
    [[values]] = result.calls_of("save_settings")
    api.save_settings(values)
    saved = api.light_novels.settings_payload()
    assert (saved["audiobook_generation_provider"], saved["irodori_tts_auto_generate"]) == ("irodori", True)


def test_light_novel_menu_offers_audiobook_generation_only_with_a_provider(page) -> None:
    book = {"id": 9, "title": "Novel", "volume": 1, "character_count": 1000}
    code = r"""
        await __fire('#lightNovelsContent [data-ln-book="9"]', 'contextmenu', {clientX: 20, clientY: 20}, 'MouseEvent');
        const item = document.querySelector('#contextMenu [data-ln-context-action=generate-audiobook]');
        const label = item?.innerText ?? null;
        item?.click(); await __tick(10);
        return {label, toast: __text('#toast'), texts: [t('ui.generateAudiobook'), t('ui.audiobookGenerationStarted')]};
    """
    off = page.run(code, api={"light_novel_state": {"version": 1, "settings": {}, "books": [book]}})
    assert off.value["label"] is None
    on = page.run(
        code,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {"audiobook_generation_provider": "irodori"},
                "books": [book],
            },
            "generate_light_novel_tts": {"ok": True, "job": {"state": "queued"}},
        },
    )
    assert on.value["label"] == on.value["texts"][0]
    assert on.calls_of("generate_light_novel_tts") == [[9]]
    assert on.value["toast"] == on.value["texts"][1]
