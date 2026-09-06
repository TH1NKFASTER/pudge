from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database


ROOT = Path(__file__).parents[1]


def _audio_service(tmp_path: Path) -> AudiobookService:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return AudiobookService(
        Database(config.library.database_path),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=config.paths.cache_dir,
    )


def test_generated_audiobook_state_exposes_tts_marker(tmp_path: Path) -> None:
    service = _audio_service(tmp_path)
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / ".pudge-audiobook-profile.json").write_text("{}", encoding="utf-8")
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()

    tts = service._upsert(path=generated, title="Generated", duration=10.0, files=[], chapters=[])
    normal = service._upsert(path=ordinary, title="Ordinary", duration=10.0, files=[], chapters=[])

    assert tts["tts_generated"] is True
    assert normal["tts_generated"] is False


def test_audiobook_library_shows_robot_only_for_tts_generated_books() -> None:
    media = (ROOT / "pudge" / "web" / "media.js").read_text(encoding="utf-8")
    assert "book.tts_generated" in media
    assert 'class="audiobook-tts-badge"' in media
    assert ">🤖</span>" in media


def test_settings_save_does_not_force_navigation_back_to_settings() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    save_block = html.split("if(e.target.id==='saveSettings')", 1)[1].split("if(e.target.id==='presetNyaaSocks')", 1)[0]
    assert "await pywebview.api.save_settings(values)" in save_block
    assert "renderAll();toast(t('toast.settingsSaved'))" in save_block
    assert "setPage('settings')" not in save_block


def test_ln_context_menus_expose_speaker_markup_export_import() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    menus = html.split("function showLnBookMenu", 1)[1].split("async function showLnAudiobookLink", 1)[0]
    assert 'data-ln-context-action="export-speaker-markup"' in menus
    assert 'data-ln-context-action="import-speaker-markup"' in menus
    # Keep the backend workflow available for compatibility/debugging even though it is no longer in the LN UI.
    assert "export_light_novel_speaker_markup" in html
    assert "choose_light_novel_speaker_markup" in html
