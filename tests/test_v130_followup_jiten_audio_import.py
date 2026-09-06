from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.database import Database

ROOT = Path(__file__).parents[1]


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "library.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        ffmpeg="ffmpeg",
        cache_dir=tmp_path / "cache",
        cover_cache_dir=tmp_path / "covers",
    )


def test_study_card_select_layer_and_text_selection_are_above_app_defaults() -> None:
    select_css = (ROOT / "pudge/web/pudge_select.css").read_text(encoding="utf-8")
    card_css = (ROOT / "pudge/web/reading_tools.css").read_text(encoding="utf-8")

    assert ".pudge-select-menu" in select_css and "z-index:14060" in select_css
    assert ".pudge-study-card,.pudge-study-card *{-webkit-user-select:text!important;user-select:text!important}" in card_css
    assert ".pudge-study-card button,.pudge-study-card select" in card_css
    assert "-webkit-user-select:none!important;user-select:none!important" in card_css


def test_cover_preview_zooms_at_pointer_and_pans_after_open() -> None:
    js = (ROOT / "pudge/web/cover_preview.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/cover_preview.css").read_text(encoding="utf-8")

    assert "previewAnchor(clientX,clientY)" in js
    assert "previewPanX=anchor.x-(anchor.x-previewPanX)*(next/oldScale)" in js
    assert "previewPanY=anchor.y-(anchor.y-previewPanY)*(next/oldScale)" in js
    assert "panPreview(-Number(event.deltaX||0),-Number(event.deltaY||0))" in js
    assert "pointermove" in js and "previewPan" in js
    assert "touch-action:none" in css
    assert "cursor:grab" in css and "cursor:grabbing" in css


def test_play_from_here_is_green_left_of_read_and_forces_playback() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/reading_tools.css").read_text(encoding="utf-8")
    audio = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")

    action_line = next(line for line in html.splitlines() if "actions=[...(canSeek?" in line)
    assert action_line.index("paired-audio-here") < action_line.index("bookmark-here")
    assert "tone:'listen'" in action_line
    assert 'data-pudge-study-action-tone="${' in js
    assert '.pudge-study-header-action[data-pudge-study-action-tone="listen"]' in css
    seek_line = next(line for line in html.splitlines() if line.startswith("async function seekLnPairedToOffset"))
    assert "light_novel_play_paired_at_offset" in seek_line
    assert "playing:true,paused:false" in seek_line
    assert "self.set_paused(audiobook_id, False)" in audio
    assert "forceReaderStart" in html and "lnPairedStartFromReaderGeneration" in html


def test_folder_import_preserves_embedded_m4b_chapters_with_global_offsets(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Book"
    folder.mkdir()
    first = folder / "01.m4b"
    second = folder / "02.m4b"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    def probe(path: Path):
        if path.name == "01.m4b":
            return 30.0, [
                {"index": 0, "title": "Intro", "start": 0.0, "end": 10.0},
                {"index": 1, "title": "One", "start": 10.0, "end": 30.0},
            ]
        return 20.0, [
            {"index": 0, "title": "Two", "start": 0.0, "end": 8.0},
            {"index": 1, "title": "Three", "start": 8.0, "end": 20.0},
        ]

    monkeypatch.setattr(service, "_probe", probe)
    monkeypatch.setattr(service, "_queue_embedded_cover", lambda _path: None)
    book = service.import_folder(folder, auto_link=False, prepare_transcription=False)

    assert [row["title"] for row in book["chapters"]] == ["Intro", "One", "Two", "Three"]
    assert [row["start"] for row in book["chapters"]] == [0.0, 10.0, 30.0, 38.0]
    assert [row["end"] for row in book["chapters"]] == [10.0, 30.0, 38.0, 50.0]


def test_embedded_m4b_cover_is_extracted_into_shared_cover_cache(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append([str(value) for value in command])
        target = Path(command[-1])
        target.write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    url = service._embedded_cover_url(source)

    assert url.startswith("covers/audiobook-") and url.endswith(".jpg")
    assert (tmp_path / "covers" / Path(url).name).read_bytes() == b"jpeg"
    assert "-map" in calls[0] and "0:v:0" in calls[0]
    assert "-frames:v" in calls[0] and "1" in calls[0]


def test_transcription_queue_is_bounded_and_reports_positions(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_ensure_transcription_dispatcher", lambda: None)
    ids = []
    for index in range(2):
        source = tmp_path / f"book-{index}.mp3"
        source.write_bytes(b"audio")
        book = service._upsert(
            path=source,
            title=f"Book {index}",
            duration=100.0,
            files=[{"index": 0, "path": str(source), "title": source.stem, "duration": 100.0, "start": 0.0, "end": 100.0}],
            chapters=[{"index": 0, "title": source.stem, "start": 0.0, "end": 100.0}],
        )
        ids.append(int(book["id"]))

    service.prepare_transcription(ids[0])
    service.prepare_transcription(ids[1])

    first = service.transcription_status(ids[0])
    second = service.transcription_status(ids[1])
    assert first["status"] == second["status"] == "queued"
    assert first["queue_position"] == 1 and second["queue_position"] == 2
    assert first["queue_size"] == second["queue_size"] == 2
    source = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")
    assert 'name="audiobook-stt-dispatcher"' in source
    assert 'name=f"audiobook-stt-{audiobook_id}"' not in source


def test_audiobook_bulk_delete_api_and_ui_exist(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "stop", lambda _book_id: {"ok": True})
    ids = []
    for index in range(2):
        source = tmp_path / f"bulk-{index}.mp3"
        source.write_bytes(b"audio")
        book = service._upsert(
            path=source,
            title=source.stem,
            duration=1.0,
            files=[{"index": 0, "path": str(source), "title": source.stem, "duration": 1.0, "start": 0.0, "end": 1.0}],
            chapters=[{"index": 0, "title": source.stem, "start": 0.0, "end": 1.0}],
        )
        ids.append(int(book["id"]))

    result = service.delete_many(ids)
    assert result == {"ok": True, "removed": ids, "errors": []}
    assert service.state()["books"] == []

    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    web_app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "window.PudgeAudiobookSelection" in media
    assert "selectedBookIds" in media
    assert "deleteSelected" in media
    assert "audiobook_delete_many(ids)" in media
    assert "def audiobook_delete_many" in web_app
