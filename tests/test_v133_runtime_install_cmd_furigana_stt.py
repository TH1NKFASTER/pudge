from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.database import Database

ROOT = Path(__file__).resolve().parents[1]


def _audio_service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def test_patch_installer_can_force_build_from_current_release_checkout() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'PUDGE_BUILD_CURRENT_TREE' in installer
    assert 'if [[ -e "$PROJECT_DIR/.git" ]]' in installer
    assert 'pip wheel "$PROJECT_DIR"' in installer


def test_audiobook_card_background_toggles_without_select_mode() -> None:
    js = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    assert "v207: an audiobook volume card is itself a selection control" in js
    assert "audioSelectionSurface" in js
    assert "audioSelection.has(id)" in js
    assert "if(!modified&&!audioSelection.size)return" not in js
    assert "audioSelectionMode" not in js
    assert "data-audio-selection-mode" not in js
    assert "event.stopImmediatePropagation()" in js


def test_horizontal_furigana_is_plain_span_in_live_dom_not_webkit_ruby() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    marker = html.split("pudge-v133-ln-furigana-dom-overlay-v1", 1)[1]
    assert "function lnSyncFuriganaDom(vertical=false)" in html
    assert "document.createElement('span')" in html
    assert "ruby.replaceWith(span)" in html
    assert "document.createElement('ruby')" in html  # vertical mode is reversible
    assert "content:attr(data-ln-reading)" in marker
    assert "width:max-content" in marker
    assert "lnSyncFuriganaDom(!!(payload.settings||ui.lnState?.settings||{}).reader_vertical)" in html


def test_unknown_stt_duration_is_probed_before_direct_mlx_path(tmp_path: Path, monkeypatch) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "legacy-long.m4b"
    source.write_bytes(b"audio")
    calls = []

    def fake_probe(path: Path):
        calls.append(path)
        return 901.0, []

    monkeypatch.setattr(service, "_probe", fake_probe)
    plan = service._transcription_chunk_plan([
        {"file_index": 0, "path": str(source), "duration": 0.0, "start": 0.0, "end": 0.0}
    ])
    assert calls == [source]
    assert len(plan) == 4
    assert all(row["direct"] is False for row in plan)
    assert [round(row["duration"]) for row in plan] == [300, 300, 300, 1]
