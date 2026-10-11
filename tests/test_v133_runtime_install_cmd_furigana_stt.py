"""Patch installs from a release folder, audiobook card selection, horizontal furigana and
STT duration probing (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

from pathlib import Path

from css_support import css_rules
from t71g01_support import AUDIOBOOKS, AUDIO_SELECTION, release_history, source_history, wheel_files

from pudge.audiobooks import AudiobookService
from pudge.database import Database


def _audio_service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def test_patch_installer_can_force_a_build_from_the_release_folder() -> None:
    # A patch applied to an unpacked release (no .git) must be what gets installed, not the
    # wheel that came with the release: PUDGE_BUILD_CURRENT_TREE=1 builds from the folder.
    history = source_history()
    patched, folder = history.steps["patched"], history.projects["patched"]
    assert patched.result.returncode == 0, patched.result.output
    [wheel] = [call for call in patched.result.calls("pip") if call[0] == "wheel"]
    assert wheel[:4] == ["wheel", str(folder), "--no-deps", "--no-build-isolation"]
    assert patched.runtime_after["pudge/web/app.js"] == b"// Pudge 9.8.8 + patch\n"
    assert (
        patched.runtime_after["pudge/web/app.js"]
        != wheel_files(folder / "pudge-9.8.8-py3-none-any.whl")["pudge/web/app.js"]
    )
    # A git checkout always builds from its tree; a plain release uses its wheel.
    checkout = history.steps["install"].result
    assert [call for call in checkout.calls("pip") if call[0] == "wheel"][0][1] == str(
        history.projects["checkout"]
    )
    assert not [call for call in release_history().steps["install"].result.calls("pip") if call[0] == "wheel"]


def test_audiobook_card_background_toggles_selection_without_a_select_mode(page) -> None:
    result = page.run(
        AUDIO_SELECTION
        + r"""
        const out = {modes: document.querySelectorAll('[data-audio-selection-mode]').length};
        const calls = __apiCalls.length;
        await __click(`${card(5)} .audiobook-main`); await __tick(10); out.first = selected();
        await __click(`${card(5)} .audiobook-title-row`); await __tick(10); out.second = selected();
        out.calls = __apiCalls.slice(calls).map(call => call.name);
        return out;
        """,
        api=AUDIOBOOKS,
    )
    # Nothing selected yet and no mode to switch on: the first plain click selects, the next one
    # deselects, and the click does nothing else (no playback, no backend call).
    assert result.value == {"modes": 0, "first": [5], "second": [], "calls": []}


LN_FURIGANA = r"""
__api.light_novel_state = async () => ({version: 1, settings: window.LN_SETTINGS, books: [{id: 6, title: 'Novel', volume: 1}]});
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, settings: window.LN_SETTINGS, paragraphs: ['猫が好き'],
  tokens: [[{start: 0, end: 1, surface: '猫', rubies: [{start: 0, end: 1, text: 'ねこ'}]}, {start: 1, end: 2, surface: 'が'},
            {start: 2, end: 4, surface: '好き', rubies: [{start: 2, end: 3, text: 'す'}]}]], vocabulary: []});
"""
FURIGANA_SHAPE = r"""
const reader = $('lnReader');
const shape = () => ({ruby: reader.querySelectorAll('ruby,rt').length,
  spans: [...reader.querySelectorAll('span.ln-furigana-ruby')].map(s => [s.textContent, s.dataset.lnReading]), text: reader.innerText.trim()});
setPage('lightnovels'); await __tick(100);
await __settle(openLightNovel(6)); await __tick(100);
"""


def test_horizontal_furigana_is_a_plain_span_and_vertical_mode_gets_ruby_back(page) -> None:
    # WebKit's native ruby changed word widths and line heights in horizontal text; there the
    # reading is a painted overlay of a plain span (and never part of the text).
    no_gate = {"settings": {"review_gate_ln_enabled": False}}
    result = page.run(
        FURIGANA_SHAPE
        + r"""
        const out = {horizontal: shape()};
        applyLnReaderSettings({reader_vertical: true}); out.vertical = shape();
        applyLnReaderSettings({reader_vertical: false}); out.back = shape();
        return out;
        """,
        before="window.LN_SETTINGS = {};" + LN_FURIGANA,
        state=no_gate,
    )
    spans = {"ruby": 0, "spans": [["猫", "ねこ"], ["好", "す"]], "text": "猫が好き"}
    assert result.value == {
        "horizontal": spans,
        "vertical": {"ruby": 4, "spans": [], "text": "猫ねこが好すき"},
        "back": spans,
    }
    # A chapter opened in vertical mode keeps native ruby.
    vertical = page.run(
        FURIGANA_SHAPE + "return shape();",
        before="window.LN_SETTINGS = {reader_vertical: true};" + LN_FURIGANA,
        state=no_gate,
    )
    assert vertical.value["ruby"] == 4 and vertical.value["spans"] == []
    overlay = css_rules("index.html").declarations("#lnReader:not(.vertical) .ln-furigana-ruby::after")
    assert (overlay["content"], overlay["position"], overlay["width"], overlay["white-space"]) == (
        "attr(data-ln-reading)",
        "absolute",
        "max-content",
        "nowrap",
    )


def test_unknown_stt_duration_is_probed_before_direct_mlx_path(tmp_path: Path, monkeypatch) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "legacy-long.m4b"
    source.write_bytes(b"audio")
    calls = []

    def fake_probe(path: Path):
        calls.append(path)
        return 901.0, []

    monkeypatch.setattr(service, "_probe", fake_probe)
    plan = service._transcription_chunk_plan(
        [{"file_index": 0, "path": str(source), "duration": 0.0, "start": 0.0, "end": 0.0}]
    )
    assert calls == [source]
    assert len(plan) == 4
    assert all(row["direct"] is False for row in plan)
    assert [round(row["duration"]) for row in plan] == [300, 300, 300, 1]
