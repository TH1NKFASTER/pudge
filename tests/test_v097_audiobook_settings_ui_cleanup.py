"""Generated audiobooks are marked, saving settings keeps the current page, LN voice markup
actions (behaviour; audit 71Б rewrite: page harness instead of source text)."""

from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database


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


def test_audiobook_library_shows_robot_only_for_tts_generated_books(page) -> None:
    books = [
        {
            "id": 1,
            "title": "Generated",
            "duration": 60,
            "position": 0,
            "speed": 1,
            "chapters": [],
            "tts_generated": True,
        },
        {
            "id": 2,
            "title": "Ordinary",
            "duration": 60,
            "position": 0,
            "speed": 1,
            "chapters": [],
            "tts_generated": False,
        },
    ]
    result = page.run(
        r"""
        setPage('audiobooks', true); await __tick(300);
        const badge = id => { const node = document.querySelector(`[data-audiobook-id="${id}"] .audiobook-title-row .audiobook-tts-badge`);
          return node ? {text: node.textContent, title: node.title, visible: node.checkVisibility()} : null; };
        return {generated: badge(1), ordinary: badge(2), label: t('media.ttsGenerated')};
        """,
        api={"audiobook_state": {"books": books}},
    )
    out = result.value
    assert out["generated"] == {"text": "🤖", "title": out["label"], "visible": True}
    assert out["ordinary"] is None


def test_settings_save_does_not_force_navigation_back_to_settings(page) -> None:
    # The user saves and moves on before the backend answers: the answer must not pull
    # the window back to Settings.
    result = page.run(
        r"""
        let release;
        __api.save_settings = values => new Promise(resolve => { release = () => resolve({settings: {...ui.state.settings}}); });
        setPage('settings', true); await __tick(300);
        await __click('#saveSettings');
        await __click('.nav button[data-page="current"]'); await __tick(50);
        release(); await __tick(300);
        return {page: ui.page, toasts: __toasts(), saved: t('toast.settingsSaved'), button: document.getElementById('saveSettings')?.disabled};
        """,
    )
    out = result.value
    assert result.called("save_settings")
    assert out["page"] == "current"
    assert out["saved"] in out["toasts"]
    assert out["button"] is False


LN_MENU = r"""
const lnMenu = async id => {
  setPage('lightnovels', true); await __tick(100);
  await __fire(`[data-ln-book="${id}"]`, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
  return [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].filter(b => b.checkVisibility()).map(b => b.dataset.lnContextAction);
};
"""


def test_ln_context_menus_expose_speaker_markup_export_import(page) -> None:
    books = [
        {"id": 6, "title": "Novel", "volume": 1},
        {
            "id": 7,
            "title": "Paired",
            "volume": 2,
            "irodori_tts": {"paired_kind": "ordinary"},
            "paired_audio": {"audiobook_id": 9, "book": {"id": 9}},
        },
    ]
    result = page.run(
        LN_MENU
        + r"""
        const out = {plain: await lnMenu(6)};
        await __click('[data-ln-context-action="export-speaker-markup"]'); await __tick(50);
        await lnMenu(6);
        await __click('[data-ln-context-action="import-speaker-markup"]'); await __tick(50);
        out.ordinary = await lnMenu(7);
        out.labels = [...document.querySelectorAll('#contextMenu button')].map(b => b.innerText);
        out.texts = [t('ui.exportCharacterVoiceMarkup'), t('ui.importCharacterVoiceMarkup')];
        return out;
        """,
        api={
            "light_novel_state": {"version": 1, "settings": {}, "books": books},
            "export_light_novel_speaker_markup": {"book_count": 1, "chapter_count": 3},
            "choose_light_novel_speaker_markup": {"cancelled": True},
        },
    )
    out = result.value
    assert {"export-speaker-markup", "import-speaker-markup"} <= set(out["plain"])
    assert result.calls_of("export_light_novel_speaker_markup") == [[6]]
    assert result.calls_of("choose_light_novel_speaker_markup") == [[6]]
    # A book paired with an ordinary (not generated) audiobook has no voice markup to edit.
    assert not {"export-speaker-markup", "import-speaker-markup"} & set(out["ordinary"])
    assert not set(out["texts"]) & set(out["labels"])
