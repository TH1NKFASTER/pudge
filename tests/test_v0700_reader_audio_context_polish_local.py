from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.database import Database

ROOT = Path(__file__).parents[1]


def test_manga_ocr_copy_is_user_facing_and_bubble_is_in_place() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")

    assert "Recognize" not in js
    assert "Распознать" not in js
    assert "OCR cache" not in js
    assert "OCR из кэша" not in js
    assert '>OCR</button>' in js
    assert "manga-v2-region-content" in js
    assert "background:rgba(250,248,242,.97)" in css
    assert "user-select:text" in css


def test_audiobook_seek_and_speed_service(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_probe", lambda _path: (300.0, []))
    book = service.import_file(source)
    service.set_position(book["id"], 100.0)

    result = service.seek(book["id"], -15.0)
    assert result["book"]["position"] == 85.0
    speed = service.set_speed(book["id"], 1.75)
    assert speed["speed"] == 1.75
    assert service.book(book["id"])["speed"] == 1.75


def test_audiobook_play_passes_speed_to_mpv(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_probe", lambda _path: (60.0, []))
    book = service.import_file(source)
    commands: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr("pudge.audiobooks.subprocess.Popen", lambda command, **_kwargs: commands.append(command) or FakeProcess())
    monkeypatch.setattr("pudge.audiobooks.threading.Thread", lambda **_kwargs: SimpleNamespace(start=lambda: None))

    service.play(book["id"], speed=1.5)
    assert any(item == "--speed=1.500" for item in commands[0])


def test_audiobook_ui_has_busy_state_speed_and_skip_controls() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/media.css").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "audioImportBusy='folder'" in media
    assert "Сканирую папку…" in media
    assert "audiobook-import-status" in media
    assert "data-audio-speed" in media
    assert "seek-audio" in media
    assert "-15" in media and "+15" in media
    assert "audiobook-card.playing" in css
    assert "def audiobook_set_speed" in app
    assert "def audiobook_seek" in app
    assert "const formatAudioTime = value =>" in media
    assert "formatTime(book.position)" not in media
    assert "formatTime(book.duration)" not in media


def test_anime_context_menu_has_dom_fallback_for_watching_cards() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "animeFromId(id)||{media_id:id" in html
    assert "fallback.siteUrl||`https://anilist.co/anime/${id}`" in html
    assert "function openAnimeContextFromPointer(e)" in html
    assert "document.addEventListener('contextmenu'" in html
    assert "function isSecondaryAnimeActivation(e)" not in html
    assert "document.addEventListener('auxclick'" not in html
    assert "showAnimeMenu(card.dataset.mediaId" in html


def test_continue_watching_has_explicit_resume_and_context_metadata() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert 'data-continue-card="1"' in html
    assert 'data-continue-card="1" data-media-id=' in html
    assert 'data-action="resume" data-path=' not in html
    assert "document.addEventListener('pointerup',async e=>" in html
    assert "if(e.button!==0||e.ctrlKey||e.defaultPrevented)return" in html
    assert "suppressContinueClickUntil" not in html
    assert ".airing-card.play-starting,.airing-card.play-running { cursor:context-menu; }" in html
    assert "e.stopImmediatePropagation()" in html
    assert "await startPlay(card.dataset.path,true)" in html
    assert '"site_url": anime.site_url if anime is not None else ""' in app
    assert '"media_status": anime.media_status if anime is not None else ""' in app
