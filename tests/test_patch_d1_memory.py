"""Audit decisions, patch D1: memory and small energy items (behaviour; window parts run in
the page harness, audit 71Б rewrite)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from css_support import css_rules
from pudge.database import Database
from study_page_support import LN_BOOK
from t71g03_support import MANGA, READER

ROOT = Path(__file__).resolve().parents[1]


def test_only_two_alignments_stay_in_memory(tmp_path):
    from pudge.audiobooks import AudiobookService

    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    root = tmp_path / "cache" / "reading-audio-alignment"
    root.mkdir(parents=True)
    service._attach_runtime_alignment_context = lambda *a, **k: None
    for book in (1, 2, 3):
        (root / f"fp{book}.json").write_text(json.dumps({"schema": "reading-audio-v3", "chapters": []}))
    service._alignment_path = lambda ln, audio: root / f"fp{ln}.json"
    for book in (1, 2, 1, 3):
        assert service._load_alignment(book, 9) is not None
    assert [key[0] for key in service._alignment_payload_cache] == [1, 3]


def test_manga_page_text_reuses_the_parsed_artifact(tmp_path, monkeypatch):
    from pudge import manga as manga_module
    from pudge.manga import MangaService

    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    reads = []
    monkeypatch.setattr(manga_module, "read_artifact", lambda path: reads.append(path) or {"pages": []})
    for _ in range(5):
        assert service._read_artifact_cached(artifact) == {"pages": []}
    assert len(reads) == 1
    time.sleep(0.01)
    artifact.write_text('{"changed": true}')
    service._read_artifact_cached(artifact)
    assert len(reads) == 2


def test_playback_wrapper_does_not_import_the_alignment_stack():
    import subprocess

    code = "import sys, pudge.cli; print(int('pudge.syncing' in sys.modules), int('pudge.subtitles.timeline_alignment' in sys.modules))"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT
    )
    assert result.stdout.split() == ["0", "0"], result.stderr[-2000:]


def test_release_name_cache_is_bounded():
    from pudge.release_parser import parse_release_name

    assert parse_release_name.cache_info().maxsize == 1024


def test_closing_the_light_novel_reader_frees_its_word_map_and_scroll_trace(page):
    result = page.run(
        r"""
        await __settle(openLightNovel(6)); await __tick(300);
        lnScrollPerfTrace('scroll_start', {session_id: 1});
        const open = {words: ui.lnTokenMap.size, trace: ui.lnScrollPerfTrace.length};
        await __click('#lnReaderClose'); await __tick(50);
        return {open, closed: {words: ui.lnTokenMap.size, trace: ui.lnScrollPerfTrace.length}};
        """,
        before=LN_BOOK,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    assert result.value["open"]["words"] > 0 and result.value["open"]["trace"] > 0
    assert result.value["closed"] == {"words": 0, "trace": 0}


def test_manga_debug_log_keeps_a_summary_of_the_ocr_regions(page):
    result = page.run(
        READER
        + r"""
        window.MANGA.regions = () => [{id: 9, text: '猫が好き', x: .5, y: .5, width: .2, height: .2, orientation: 'vertical'}];
        await open(3);
        await __key('L', {metaKey: true, shiftKey: true, code: 'KeyL'}, 'body'); await __tick(50);
        """,
        before=MANGA
        + "__api.manga_export_ocr_debug = async () => ({ok: true, path: '/tmp/manga-ocr.json'});",
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    [[payload]] = result.calls_of("manga_export_ocr_debug")
    [event] = [event for event in payload["events"] if event["event"] == "ocr_result"]
    # The ring of 600 events keeps a small summary per region, not a copy of every region.
    assert event["regions"] == [{"order": 0, "id": 9, "chars": 4}]


def test_audio_word_attributes_only_with_paired_audio(page):
    paired = """
    const plainOpen = __api.light_novel_open;
    __api.light_novel_open = async id => ({...await plainOpen(id), paired_audio: {ln_book_id: id, alignment: {ready: false}, book: {id: 75}}});
    """
    counts = {}
    for name, before in (("plain", LN_BOOK), ("paired", LN_BOOK + paired)):
        result = page.run(
            r"""
            await __settle(openLightNovel(6)); await __tick(300);
            return {words: document.querySelectorAll('#lnReader [data-ln-token]').length,
                    audio: document.querySelectorAll('#lnReader [data-ln-audio-weights]').length};
            """,
            before=before,
            state={"settings": {"review_gate_ln_enabled": False}},
        )
        counts[name] = result.value
    # Per-word timing data is only rendered for a book read together with its audiobook.
    assert counts["plain"]["words"] > 0 and counts["plain"]["audio"] == 0
    assert counts["paired"]["audio"] > 0


def test_kept_home_snapshot_interns_repeated_strings():
    from pudge.web_state import UIStateSnapshotCache

    title_a = "".join(["Long anime ", "title"])
    title_b = "".join(["Long anime ", "title"])
    assert title_a is not title_b
    payload = {"episodes": [{"title": title_a, "path": "/x" * 100}, {"title": title_b}]}
    cache = UIStateSnapshotCache()
    stored = cache.store("1", payload)
    assert stored["episodes"][0]["title"] is stored["episodes"][1]["title"]
    big = {
        "episodes": [
            {"title": f"Show {n % 50}", "state": "ready", "source": "jimaku", "n": n} for n in range(50000)
        ]
    }
    started = time.perf_counter()
    cache.store("2", big)
    assert time.perf_counter() - started < 1.0


def test_stt_worker_is_checked_once_and_the_temporary_flac_is_light(monkeypatch):
    from pudge.subtitles import stt

    calls = []
    import subprocess

    monkeypatch.setattr(
        stt.subprocess,
        "run",
        lambda *a, **k: calls.append(a[0]) or subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    for _ in range(3):
        assert stt.japanese_stt_available()
    assert len(calls) == 1
    environment = stt._stt_worker_environment()
    assert int(environment["PUDGE_MLX_CACHE_LIMIT_BYTES"]) == 512 * 1024 * 1024


def test_speech_reference_audio_is_written_without_flac_compression(tmp_path, monkeypatch):
    from pudge.subtitles import stt

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    commands = []

    def run(command, **kwargs):
        commands.append(list(command))
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"fLaC")
        return subprocess.CompletedProcess(command, 0 if command[0] == "ffmpeg" else 1, "", "stop here")

    monkeypatch.setattr(stt.subprocess, "run", run)
    monkeypatch.setattr(stt, "_worker_availability", lambda _python: {"available": True})
    stt.prepare_japanese_stt_reference(
        video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="tiny", timeout_seconds=60
    )
    [extract] = [command for command in commands if command[0] == "ffmpeg"]
    # The FLAC lives only until the transcription ends: the cheapest compression level.
    assert extract[extract.index("-compression_level") + 1] == "0"
    assert not list((tmp_path / "cache").rglob("*.flac"))


def test_monitoring_banner_pulses_instead_of_spinning():
    # A one-step pulse every 2 s instead of a continuously animated spinner.
    animation = css_rules("index.html").get(".startup-banner .spinner", "animation")
    assert animation == "pudge-banner-pulse 2s steps(1,end) infinite"


def test_hidden_float_is_destroyed_after_ten_minutes(monkeypatch):
    from pudge import audiobook_float

    timers = []

    class Timer:
        def __init__(self, delay, function, args=()):
            self.delay, self.function, self.args, self.daemon = delay, function, args, False
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(audiobook_float.threading, "Timer", Timer)
    destroyed = []
    window = SimpleNamespace(hide=lambda: None, show=lambda: None, destroy=lambda: destroyed.append(True))
    panel = audiobook_float.AudiobookFloatWindow(SimpleNamespace(logger=None))
    panel._window = window
    panel.hide()
    assert timers[-1].delay == 600
    panel.show()  # shown again before the timer: nothing is destroyed
    timers[-1].function(*timers[-1].args)
    assert destroyed == []
    panel.hide()
    timers[-1].function(*timers[-1].args)
    assert destroyed == [True]


class _Player:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _paused_minutes_until_closed(tmp_path, monkeypatch, *, paused: bool) -> float | None:
    from pudge import audiobooks
    from pudge.audiobooks import AudiobookService

    service = AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    monkeypatch.setattr(service, "_probe", lambda _path: (3600.0, []))
    monkeypatch.setattr(
        service, "prepare_transcription", lambda *_a, **_k: {"status": "queued", "ready": False}
    )
    monkeypatch.setattr(service, "_tracked_thread", lambda **_k: None)
    folder = tmp_path / "book"
    folder.mkdir()
    (folder / "01.mp3").write_bytes(b"audio")
    book_id = service.import_folder(folder)["id"]
    process, ipc_path = _Player(), tmp_path / "book.sock"
    service._players[book_id], service._ipc_paths[book_id], service._playback_sessions[book_id] = (
        process,
        ipc_path,
        "s",
    )
    clock = [1000.0]
    service._last_motion_at[book_id] = clock[0]  # the player last moved now
    sent: list[list] = []
    monkeypatch.setattr(audiobooks.time, "monotonic", lambda: clock[0])

    def sleep(_seconds):
        clock[0] += 20.0
        if clock[0] > 1000.0 + 3600:
            process.returncode = 0  # still open an hour later: stop the test

    monkeypatch.setattr(audiobooks.time, "sleep", sleep)
    monkeypatch.setattr(service, "_global_position", lambda *_a: 20.0)
    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: paused if name == "pause" else None)
    monkeypatch.setattr(
        service, "_ipc_commands_no_wait", lambda _path, commands: sent.append([clock[0], commands]) or True
    )
    service._monitor(book_id, process, ipc_path, "s")
    quits = [at for at, commands in sent if ["quit"] in commands]
    return (quits[0] - 1000.0) / 60 if quits else None


def test_long_paused_audiobook_player_is_closed(tmp_path, monkeypatch):
    # A paused mpv (30-60 MB) is closed after half an hour; a playing one is left alone.
    minutes = _paused_minutes_until_closed(tmp_path / "paused", monkeypatch, paused=True)
    assert minutes is not None and 30 <= minutes <= 31.5
    assert _paused_minutes_until_closed(tmp_path / "playing", monkeypatch, paused=False) is None


def test_anime_list_covers_use_a_small_copy(tmp_path):
    from PIL import Image

    from pudge.web_app import WebAppApi

    covers = tmp_path / "covers"
    covers.mkdir()
    full = covers / "7.jpg"
    Image.new("RGB", (460, 650), (200, 10, 10)).save(full)
    api = WebAppApi.__new__(WebAppApi)
    api.asset_base = "http://127.0.0.1:1"
    api.logger = SimpleNamespace(info=lambda *a, **k: None)
    api.manager = SimpleNamespace(cached_cover_path=lambda anime: full)
    assert api._cover_uri(SimpleNamespace(media_id=7)).endswith("/covers/7.jpg")
    deadline = time.time() + 5
    thumb = covers / "7.jpg.list.jpg"
    while not thumb.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert Image.open(thumb).size[0] == 240
    assert api._cover_uri(SimpleNamespace(media_id=7)).endswith("/covers/7.jpg.list.jpg")


def test_cover_preview_opens_the_full_cover_behind_a_small_list_copy(page):
    def show(media_id, cover):
        return {
            "media_id": media_id,
            "title": f"Show {media_id}",
            "episode": 3,
            "video_path": f"/library/Show {media_id} - 03.mkv",
            "cover": cover,
        }

    watching = [
        show(7, "http://127.0.0.1:1/covers/7.jpg.list.jpg"),
        show(8, "https://s4.anilist.co/file/anilistcdn/media/anime/cover/medium/bx8.jpg"),
    ]
    result = page.run(
        r"""
        const pinch = async id => {
          const selector = `#currentContent [data-media-id="${id}"] .cover-shell img`;
          await __fire(selector, 'gesturestart', {});
          const change = new Event('gesturechange', {bubbles: true, cancelable: true}); change.scale = 1.4;
          document.querySelector(selector).dispatchEvent(change);
          await __fire(selector, 'gestureend', {}); await __tick(50);
          const src = document.querySelector('.pudge-cover-preview img')?.getAttribute('src') ?? null;
          PudgeCoverPreview.close();
          return src;
        };
        await __tick(100);
        return {local: await pinch(7), anilist: await pinch(8)};
        """,
        state={"home": {"continue_watching": watching}},
    )
    # A local list copy opens the original file; an AniList "medium" cover opens the large size.
    assert result.value == {
        "local": "http://127.0.0.1:1/covers/7.jpg",
        "anilist": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx8.jpg",
    }


def test_light_novel_library_uses_320x480_covers(tmp_path):
    from PIL import Image
    import io

    from pudge.config import AppConfig
    from pudge.light_novels import LightNovelService

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    for directory in (cfg.library.root_dir, cfg.paths.cache_dir):
        directory.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(cfg)
    raw = io.BytesIO()
    Image.new("RGB", (1200, 1800), (10, 10, 200)).save(raw, "JPEG")
    stored = service._store_cover_bytes(raw.getvalue(), ".jpg")
    thumb = tmp_path / "covers" / (Path(stored).name + ".list.jpg")
    assert Image.open(thumb).size == (320, 480)
    assert service._list_cover_url(stored) == f"covers/{thumb.name}"


def test_planning_is_only_rebuilt_when_it_is_shown(page):
    planned = {"media_id": 21, "title": "Planned One", "status": "PLANNING", "media_kind": "anime"}
    result = page.run(
        r"""
        const titles = () => [...document.querySelectorAll('#plannedContent .anime-card')].map(card => card.dataset.title);
        await __tick(100);
        const out = {start: titles()};
        setPage('planned', true); await __tick(100);
        out.shown = titles();
        setPage('current', true); await __tick(100);
        ui.state.planned = [{...ui.state.planned[0], title: 'Planned Two'}];
        buildPlannedOnce();                      // new data while Planning is hidden
        out.hiddenRefresh = titles();
        setPage('planned', true); await __tick(100);
        out.back = titles();
        return out;
        """,
        state={"planned": [planned]},
    )
    assert result.value == {
        "start": [],
        "shown": ["Planned One"],
        "hiddenRefresh": ["Planned One"],
        "back": ["Planned Two"],
    }


def test_vertical_manga_keeps_only_nearby_pages_decoded(page):
    result = page.run(
        READER
        + r"""
        await open(3);
        // The reader watches its pages twice: which page is on screen (several thresholds) and which are near it.
        const watching = __observers.intersection.filter(o => !o.disconnected && o.targets.length === 6);
        const visibility = watching.find(o => o.thresholds.length > 1), preload = watching.find(o => o !== visibility);
        const frame = index => document.querySelector(`#mangaV2Pages [data-page-index="${index}"]`);
        const loaded = () => [...document.querySelectorAll('#mangaV2Pages .manga-v2-page-frame')].filter(f => f.querySelector('img')).map(f => Number(f.dataset.pageIndex));
        preload.__fire([0, 1, 2].map(i => ({target: frame(i)})));
        visibility.__fire([{target: frame(0), intersectionRatio: 1}]);
        await __tick(300);
        const out = {start: loaded(), margin: preload.rootMargin};
        // Scrolled down to page 3: pages 2-4 are near the screen, 0 and 1 have left it.
        preload.__fire([0, 1].map(i => ({target: frame(i), isIntersecting: false, intersectionRatio: 0}))
          .concat([2, 3, 4].map(i => ({target: frame(i)}))));
        visibility.__fire([{target: frame(0), isIntersecting: false, intersectionRatio: 0}, {target: frame(3), intersectionRatio: 1}]);
        await __tick(300);
        out.scrolled = loaded();
        out.placeholder = __text('#mangaV2Pages [data-page-index="0"]');
        return out;
        """,
        before=MANGA,
        state={"settings": {"review_gate_manga_enabled": False}},
        localStorage={"pudge.manga.reader.v2": json.dumps({"mode": "vertical"})},
    )
    out = result.value
    assert out["start"] == [0, 1, 2]
    # Pages are prepared 60 % of a screen ahead, and only pages within one of the current page
    # (or near the screen) stay decoded: page 0 goes back to its placeholder.
    assert out["margin"] == "60% 0px"
    assert out["scrolled"] == [2, 3, 4] and out["placeholder"] == "1"
