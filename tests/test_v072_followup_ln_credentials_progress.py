from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.database import Database


ROOT = Path(__file__).parents[1]


def test_planning_suggestions_and_reader_controls_have_requested_order() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")

    assert "content.after(root)" in html
    assert "$('plannedContent')?.before(root)" not in html
    assert "z-index:9500" in html
    assert "tray.hidden=!ui.lnBook?.paired_audio" in html
    assert "toolbar.insertBefore(tray,$('lnReaderAppearanceToggle'))" in html
    assert "lnCharacterNames" not in html
    assert "node.hidden=!ready||!ui.lnPairedExpanded" in html
    assert "ui.lnPairedExpanded=true" in html
    assert "await showLnPopup(target.dataset.lnToken,target)" in html
    assert "Play from here" in html
    assert "data-pudge-study-extra-action" in (
        ROOT / "pudge/web/reading_tools.js"
    ).read_text(encoding="utf-8")
    assert "${ui.lang==='ru'?'Разбор аудио':'Audio analysis'} · ${percent}%" in html


def test_names_editor_is_in_ln_context_menu_and_credential_guides_are_available() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert 'data-ln-context-action="name-cues"' in html
    assert "if(action==='name-cues'){await showCharacterGlossaryEditor" in html
    assert "https://jimaku.cc/account" in html
    assert "https://jimaku.cc/profile" not in html
    assert "https://jimaku.cc/api/docs" not in html
    assert "https://anilist.co/api/v2/oauth/pin" in html
    assert "response_type=token" in html
    assert "settings?'anilistAuthorize':'onboardingAniListAuthorize'" in html
    assert "## Jimaku and AniList credentials" in readme


def test_audiobook_status_reports_live_background_progress(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    service._set_transcription_job(
        7,
        {
            "status": "transcribing",
            "ready": False,
            "started_at": 1.0,
            "progress_percent": 42.0,
            "processed_audio_seconds": 420.0,
            "remaining_audio_seconds": 580.0,
            "total_duration": 1000.0,
        },
    )

    status = service.transcription_status(7)

    assert status["background"] is True
    assert status["progress_percent"] == 42.0
    assert status["remaining_audio_seconds"] == 580.0
    assert status["elapsed_seconds"] > 0


def test_audiobook_state_resumes_an_interrupted_queued_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    book = service._upsert(
        path=source,
        title="Book",
        duration=3600.0,
        files=[
            {
                "index": 0,
                "path": str(source),
                "title": "Book",
                "duration": 3600.0,
                "start": 0.0,
                "end": 3600.0,
            }
        ],
        chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": 3600.0}],
    )
    resumed: list[int] = []
    monkeypatch.setattr(
        service,
        "prepare_transcription",
        lambda book_id, **_kwargs: resumed.append(int(book_id))
        or {"status": "queued", "ready": False, "background": True},
    )

    state = service.state()

    assert resumed == [book["id"]]
    assert state["books"][0]["transcription"]["background"] is True


def test_stt_worker_exports_machine_readable_progress() -> None:
    worker = (ROOT / "pudge/subtitles/stt_worker.py").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    assert "_install_progress_reporter" in worker
    assert '"percent": percent' in worker
    assert "Audio analysis" in media
    assert "Background audio analysis" not in media
    assert "remaining_audio_seconds" not in media


def test_audiobook_stt_receives_the_configured_ffmpeg_path() -> None:
    audiobooks = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")
    web_app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "self.ffmpeg = str(ffmpeg or \"ffmpeg\")" in audiobooks
    assert 'environment["PATH"] = os.pathsep.join' in audiobooks
    assert "env=environment" in audiobooks
    assert "ffmpeg=self.config.tools.ffmpeg" in web_app


def test_background_audio_refresh_keeps_chapter_list_open() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    assert "openChapterBooks=new Set" in media
    assert "data-book-id=\"${Number(book.id)}\"" in media
    assert "openChapterBooks.has(Number(book.id))?'open':''" in media


def test_web_app_resumes_audiobook_analysis_during_startup() -> None:
    web_app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "target=self.audiobooks.resume_pending_transcriptions" in web_app
    assert 'name="audiobook-stt-resume"' in web_app
