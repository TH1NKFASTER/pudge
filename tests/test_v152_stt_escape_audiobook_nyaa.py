from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import audiobook_torrent_files_from_payload, audiobook_torrent_pack_plan
from pudge.subtitle_formats import parse_srt, write_srt

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"


def _bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return _bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        rows = []
        for key in sorted(value):
            rows.append(_bencode(key))
            rows.append(_bencode(value[key]))
        return b"d" + b"".join(rows) + b"e"
    raise TypeError(type(value))


def test_stt_text_clock_preserves_source_cue_duration(monkeypatch, tmp_path: Path) -> None:
    import pudge.syncing as syncing

    source = tmp_path / "source.srt"
    reference = tmp_path / "reference.srt"
    cues = []
    for i in range(8):
        start = i * 4.0
        cues.append((start, start + 1.25, f"これは十分に長い字幕テキストです{i}番です"))
    write_srt(cues, source)
    write_srt([(s + 5.0, e + 5.0, t) for s, e, t in cues], reference)

    monkeypatch.setattr(syncing, "align_light_novel_to_transcript", lambda *a, **k: {
        "alignment_method": "test", "matched_anchor_count": 8, "anchor_count": 8,
        "confidence": 0.9, "frontier": None, "frontier_reason": "test",
    })
    # Deliberately make text-offset interpolation absurdly stretched.  Cue end
    # must still follow the original subtitle duration rather than this curve.
    monkeypatch.setattr(syncing, "audio_position_for_light_novel_offset", lambda alignment, chapter, offset: 5.0 + float(offset) * 0.8)
    monkeypatch.setattr(syncing, "_stt_alass_transition_safety", lambda *a, **k: {"accepted": True, "reason": "ok"})
    monkeypatch.setattr(syncing, "_subtitle_stt_text_score", lambda *a, **k: {"available": True, "score": 0.9, "coverage": 1.0})
    monkeypatch.setattr(syncing, "compare_timing_activity", lambda *a, **k: {"available": True, "weighted": 0.8})

    output, diagnostics = syncing._stt_text_clock_candidate(source, reference, tmp_path / "cache", model="tiny")
    assert output is not None, diagnostics
    retimed = parse_srt(output)
    assert len(retimed) == len(cues)
    for before, after in zip(cues, retimed):
        assert abs((after[1] - after[0]) - (before[1] - before[0])) < 0.02
        assert after[1] - after[0] < 2.0


def test_audiobook_torrent_collection_detects_numeric_volume_directory() -> None:
    payload = _bencode({
        b"info": {
            b"name": b"Series Collection",
            b"files": [
                {b"length": 100, b"path": [b"01", b"book.m4b"]},
                {b"length": 200, b"path": [b"02", b"book.m4b"]},
                {b"length": 3, b"path": [b"cover.jpg"]},
            ],
        }
    })
    files = audiobook_torrent_files_from_payload(payload)
    assert [row["name"] for row in files] == ["01/book.m4b", "02/book.m4b", "cover.jpg"]
    plan = audiobook_torrent_pack_plan(files)
    assert plan["selective"] is True
    assert [row["volume"] for row in plan["volumes"]] == [1, 2]
    assert plan["volumes"][1]["file_ids"] == [1]


def test_audiobook_nyaa_volume_selection_chooses_only_requested_files() -> None:
    from pudge.web_app import WebAppApi

    class FakeLightNovels:
        @staticmethod
        def _release_volume_match(title: str, target: int):
            return True, None, False

    api = object.__new__(WebAppApi)
    api.light_novels = FakeLightNovels()
    files = [
        {"index": 0, "name": "Series/01/book.m4b", "size": 100},
        {"index": 1, "name": "Series/02/book.m4b", "size": 200},
    ]
    selected, plan = api._audiobook_nyaa_volume_selection(files, 2, "Series Collection")
    assert plan["selective"] is True
    assert [row["file_id"] for row in selected] == [1]


def test_escape_card_selection_has_window_capture_fallback() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "function isEscapeKey(event)" in html
    assert "event?.key==='Esc'" in html
    assert "window.addEventListener('keydown',captureEscapeCardSelection,true)" in html
    assert "window.addEventListener('keyup',captureEscapeCardSelection,true)" in html


def test_light_novel_context_menu_has_selective_nyaa_audiobook_search() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert 'data-ln-context-action="find-audiobook-nyaa"' in html
    assert "light_novel_search_audiobook_nyaa" in html
    assert "light_novel_download_audiobook_nyaa" in html
    assert "Inspecting releases and collection contents" in html
