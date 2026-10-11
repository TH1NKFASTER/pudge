"""v0.8.0 batch: shared identity tables, character-name overrides, dense audio alignment,
idle visual-novel capture, audiobook import queueing, and the window side of the batch
(behaviour; audit 71Б rewrite).

Elsewhere: a word click seeks listen-together audio (light_novel_play_paired_at_offset)
tests/test_v072_followup_ln_credentials_progress.py; AniList identity search
(media_identity_search) tests/test_v0662_light_novel_followups.py and
tests/test_v0700_reading_library_polish_local.py; the Visual Novels page
tests/test_v227_r1_escape_vn.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.database import LATEST_SCHEMA_VERSION, Database
from pudge.light_novels import LightNovelService
from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel_offset,
)
from pudge.visual_novels import VisualNovelService
from t71g05_support import READER, READER_SETTINGS, reader_before, vertical_segments
from t71g20_support import LN_PAIRED, PAIRED_VIEW


def test_latest_schema_has_shared_identity_and_name_overrides(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == LATEST_SCHEMA_VERSION
    assert LATEST_SCHEMA_VERSION >= 7
    assert {"character_name_overrides", "media_identities"} <= tables


def test_user_character_name_overrides_win_without_anilist(tmp_path: Path) -> None:
    config = SimpleNamespace(
        library=SimpleNamespace(
            database_path=tmp_path / "library.sqlite3",
            root_dir=tmp_path,
            cover_cache_dir=tmp_path / "covers",
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        anilist=SimpleNamespace(enabled=False, access_token=""),
        ui=SimpleNamespace(language="en"),
    )
    Database(config.library.database_path)
    service = LightNovelService(config)
    rows = service.save_character_glossary_override(123, "雪乃", "Yukino")
    assert rows == [{"source": "雪乃", "preferred": "Yukino", "user_override": True}]
    assert service.delete_character_glossary_override(123, "雪乃") == []


def test_dense_alignment_supports_word_click_seek() -> None:
    text = "".join(f"第{index}節では主人公が静かに歩きました。" for index in range(80))
    normalized = "".join(f"第{index}節では主人公が静かに歩きました" for index in range(80))
    words = [
        {"word": character, "start": index * 0.08, "end": (index + 1) * 0.08}
        for index, character in enumerate(normalized)
    ]
    alignment = align_light_novel_to_transcript(
        [{"chapter_index": 0, "title": "One", "text": text}],
        [{"start": 0, "end": len(words) * 0.08, "words": words}],
        duration=len(words) * 0.08,
        model="test",
    )
    anchors = alignment["chapters"][0]["anchors"]
    assert len(anchors) > 50
    position = audio_position_for_light_novel_offset(alignment, 0, 120)
    assert position is not None and position > 0


def test_visual_novel_reader_is_idle_until_explicit_start() -> None:
    service = VisualNovelService()
    state = service.state()
    assert state["running"] is False
    assert state["status"] == "idle"
    assert state["transcript"] == []


def test_audiobook_import_queues_transcription_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    monkeypatch.setattr(service, "_probe", lambda _path: (120.0, []))
    queued: list[int] = []
    monkeypatch.setattr(
        service,
        "prepare_transcription",
        lambda book_id, **_kwargs: queued.append(int(book_id)) or {"status": "queued"},
    )
    book = service.import_file(source)
    assert queued == [int(book["id"])]


def test_listen_together_has_no_manual_alignment_button(page) -> None:
    # Alignment runs by itself; the old "Align" button (light_novel_prepare_audio_alignment) is gone.
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        await __click('#lnPairedAudio'); await __tick(500);
        return {align: document.getElementById('lnPairedAlign') !== null, playing: highlight().words.length > 0};
        """,
        before=LN_PAIRED,
    )
    assert result.value == {"align": False, "playing": True}
    assert not result.called("light_novel_prepare_audio_alignment")


# Two vertical bubbles on one row, sent left one first: the reader reads right to left.
RIGHT = {
    "text": "猫が好き",
    "x": 0.7,
    "y": 0.5,
    "width": 0.1,
    "height": 0.3,
    "orientation": "vertical",
    "source": "vision",
    "segments": vertical_segments("猫が好き", 0.7, 0.8, 0.1, 0.075),
}
LEFT = {
    "text": "犬も",
    "x": 0.2,
    "y": 0.65,
    "width": 0.1,
    "height": 0.15,
    "orientation": "vertical",
    "source": "vision",
    "segments": vertical_segments("犬も", 0.2, 0.8, 0.1, 0.075),
}


def test_manga_text_is_always_on_the_page_in_reading_order_and_the_word_card_stays_by_its_word(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const bubbles = () => [...document.querySelectorAll('.manga-v2-text-region')].map(node => ({
          text: node.getAttribute('aria-label'), active: node.classList.contains('active'),
          words: [...node.querySelectorAll('[data-pudge-study-token]')].map(n => n.textContent)}));
        const out = {shown: bubbles()};
        // Hovering a bubble used to build its words and leaving it removed them again.
        await __fire('.manga-v2-text-region', 'pointerover', {}, 'PointerEvent');
        await __fire('.manga-v2-text-region', 'pointerout', {}, 'PointerEvent');
        out.afterHover = bubbles();
        // Words are drawn at (140, 60) on screen, 20x22 px; a word no longer in the page has no box.
        const proto = Object.getPrototypeOf(document.createElement('span'));
        const box = proto.getBoundingClientRect;
        proto.getBoundingClientRect = function () {
          if (this.dataset?.pudgeStudyToken === undefined) return box.call(this);
          const [x, y, width, height] = this.isConnected ? [140, 60, 20, 22] : [0, 0, 0, 0];
          return {x, y, left: x, top: y, width, height, right: x + width, bottom: y + height};
        };
        const surface = document.querySelector('.manga-v2-text-region .manga-v2-selection-content');
        surface.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: .75 * W, clientY: .24 * H, detail: 1}));
        await __tick(50);
        const card = document.getElementById('pudgeStudyCard');
        const where = () => [card.classList.contains('open'), card.style.left, card.style.top];
        out.opened = where();
        // The page redraws its text layer while the card waits for the deck list.
        for (const layer of document.querySelectorAll('.manga-v2-text-layer')) layer.replaceChildren();
        window.releaseDecks([{id: '12', name: 'Mining'}]); await __tick(50);
        out.decks = where();
        window.dispatchEvent(new Event('resize')); await __tick(50);
        out.resized = where();
        return out;
        """,
        before=reader_before(
            {0: [LEFT, RIGHT]},
            {"猫が好き": ["猫", "が", "好き"], "犬も": ["犬", "も"]},
            extra="__api.study_decks = () => new Promise(resolve => { window.releaseDecks = resolve; });",
        ),
        state=READER_SETTINGS,
    )
    out = result.value
    expected = [
        {"text": "猫が好き", "active": False, "words": ["猫", "が", "好き"]},
        {"text": "犬も", "active": False, "words": ["犬", "も"]},
    ]
    assert out["shown"] == expected
    assert out["afterHover"] == expected
    # Under the word: left at its centre (the card has no width here), 10 px below it.
    assert out["opened"] == [True, "150px", "92px"]
    assert out["decks"] == out["opened"] and out["resized"] == out["opened"]


def test_character_name_editor_saves_and_deletes_overrides(page) -> None:
    result = page.run(
        r"""
        void showCharacterGlossaryEditor(123, 'Oregairu'); await __tick(20);
        const rows = () => [...document.querySelectorAll('#modalBody .ln-nyaa-row')].map(row => [...row.children].map(n => n.textContent));
        const out = {title: __text('#modalTitle'), before: rows()};
        await __input('#glossarySource', '雪乃'); await __input('#glossaryPreferred', 'Yukino');
        await __click('#glossarySave'); await __tick(20);
        out.saved = rows();
        await __click('[data-glossary-delete="雪乃"]'); await __tick(20);
        out.deleted = rows();
        out.expected = {title: `${t('ui.characterNames')} · Oregairu`, custom: t('ui.custom')};
        return out;
        """,
        before=r"""
        window.ROWS = [{source: '八幡', preferred: 'Hachiman', user_override: false}];
        __api.character_glossary = async () => window.ROWS;
        __api.save_character_glossary_override = async (id, source, preferred) => (window.ROWS = [...window.ROWS, {source, preferred, user_override: true}]);
        __api.delete_character_glossary_override = async (id, source) => (window.ROWS = window.ROWS.filter(row => row.source !== source));
        """,
    )
    out = result.value
    assert out["title"] == out["expected"]["title"]
    assert out["before"] == [["八幡", "Hachiman", "AniList", ""]]
    assert out["saved"] == [
        ["八幡", "Hachiman", "AniList", ""],
        ["雪乃", "Yukino", out["expected"]["custom"], "×"],
    ]
    assert out["deleted"] == out["before"]
    assert result.calls_of("save_character_glossary_override") == [[123, "雪乃", "Yukino"]]
    assert result.calls_of("delete_character_glossary_override") == [[123, "雪乃"]]
