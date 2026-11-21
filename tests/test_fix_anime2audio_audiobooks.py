"""An anime episode (or the whole anime) made into an audiobook (pudge/anime_audiobooks.py).

Invented data only: synthetic videos made with FFmpeg (two audio tracks, English first,
Japanese second), invented subtitle lines, an invented title.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from media_tools import paired_ffmpeg, requires_ffmpeg

from pudge import anime_audiobooks as aa
from pudge import library_overrides
from pudge.audiobooks import AudiobookService
from pudge.bridge_methods import PAGE_METHODS
from pudge.database import Database
from pudge.i18n import catalog

ROOT = Path(__file__).resolve().parents[1]
TITLE = "Tanuki Lantern Club"


# ---------------------------------------------------------------- pure parts


def test_the_japanese_track_is_chosen() -> None:
    streams = [
        {"codec_type": "video", "codec_name": "h264"},
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "tags": {"language": "eng"},
            "disposition": {"default": 1},
        },
        {"codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn", "title": "Commentary"}},
        {"codec_type": "audio", "codec_name": "opus", "tags": {"language": "jpn"}},
    ]
    best = aa.pick_audio_stream(streams)
    assert best["audio_index"] == 2 and best["codec_name"] == "opus"
    # No language tags: the default track; a title saying Japanese wins over it.
    plain = [{"codec_type": "audio"}, {"codec_type": "audio", "disposition": {"default": 1}}]
    assert aa.pick_audio_stream(plain)["audio_index"] == 1
    titled = [
        {"codec_type": "audio", "disposition": {"default": 1}},
        {"codec_type": "audio", "tags": {"title": "Japanese 2.0"}},
    ]
    assert aa.pick_audio_stream(titled)["audio_index"] == 1
    assert aa.pick_audio_stream([{"codec_type": "video"}]) is None


def test_cut_times_and_subtitles_follow_the_cut() -> None:
    spans = aa.merge_spans([(60, 150), (0.2, 0.5), (140, 155), (1300, 1500)], 1400)
    assert spans == [(60.0, 155.0), (1300.0, 1400.0)]
    assert aa.remap_time(30, spans) == 30
    assert aa.remap_time(100, spans) == 60  # inside the opening: its start
    assert aa.remap_time(200, spans) == 105
    cues = [(10, 12, "まえ"), (70, 80, "うた"), (150, 160, "あと"), (200, 202, "つぎ")]
    remapped = aa.remap_cues(cues, spans, delay=1.0)
    # The song inside the cut goes; a line crossing its end starts where the cut ends.
    assert [text for _a, _b, text in remapped] == ["まえ", "あと", "つぎ"]
    assert remapped[0][:2] == (11.0, 13.0)
    assert remapped[1][:2] == pytest.approx((60.0, 66.0))
    assert remapped[2][:2] == pytest.approx((106.0, 108.0))


def test_read_along_chapter_is_monotonic_and_keeps_the_line_lit_between_lines() -> None:
    cues = [(1.0, 3.0, "ねこが\nいる。"), (2.5, 4.0, "いぬ！"), (6.0, 7.0, "…")]
    chapter = aa.read_along_chapter(cues, index=0, title="Episode 1", duration=8.0)
    assert [s["text"] for s in chapter["sentences"]] == ["ねこが いる。", "いぬ！", "…"]
    assert [s["p"] for s in chapter["sentences"]] == [0, 1, 2]
    starts = [s["start"] for s in chapter["sentences"]]
    assert starts == [0, 6, 9]  # 5 spoken characters + one silent gap
    anchors = chapter["anchors"]
    assert anchors == sorted(anchors) and all(b[1] >= a[1] for a, b in zip(anchors, anchors[1:]))
    assert chapter["endMs"] == 8000
    assert aa.read_along_chapter([], index=0, title="", duration=1) is None


def test_strings_and_bridge_methods_exist() -> None:
    for lang in ("en", "ru"):
        strings = catalog(lang)
        for key in ("action.makeAudiobook", "action.makeAudiobookAll", "animeAudiobook.episodeChapter"):
            assert strings[key]
    assert {"anime_audiobook_status", "anime_audiobook_start", "anime_audiobook_cancel"} <= PAGE_METHODS


def _anime(**values):
    return {
        "media_id": 77,
        "title": TITLE,
        "episodes": 3,
        "progress": 1,
        "media_status": "FINISHED",
        "site_url": "https://anilist.co/anime/77",
        "local": None,
        **values,
    }


MENU = r"""
showAnimeMenu(77, false, 10, 10);
return [...document.querySelectorAll('#contextMenu button')].map(b => b.dataset.contextAction);
"""


def test_menu_offers_the_whole_anime_only_when_every_episode_is_local(page) -> None:
    none = page.run(MENU, state={"current": [_anime(local_count=0)]}).value
    assert "anime-audiobook" not in none
    some = page.run(MENU, state={"current": [_anime(local_count=2)]}).value
    assert "anime-audiobook" in some and "anime-audiobook-all" not in some
    every = page.run(MENU, state={"current": [_anime(local_count=3)]}).value
    assert "anime-audiobook" in every and "anime-audiobook-all" in every


STATUS = {
    "media_id": 77,
    "title": TITLE,
    "released": 3,
    "episodes": [
        {"episode": 1, "episode_end": None, "local": False, "subtitles": False, "audiobook_id": 5},
        {"episode": 2, "episode_end": None, "local": True, "subtitles": True, "audiobook_id": None},
    ],
    "all_local": False,
    "whole_audiobook_id": None,
    "job": None,
    "busy": False,
}


def test_dialog_lists_episodes_and_starts_one(page) -> None:
    result = page.run(
        r"""
showAnimeMenu(77, false, 10, 10);
await __click('[data-context-action="anime-audiobook"]');
await __flush();
const text = __text('#modalBody');
await __click('[data-anime-audiobook-episode="2"]');
await __flush();
return {text, title: __text('#modalTitle'), buttons: document.querySelectorAll('[data-anime-audiobook-episode]').length, toasts: __toasts()};
""",
        state={"current": [_anime(local_count=1)]},
        api={
            "anime_audiobook_status": STATUS,
            "anime_audiobook_start": {
                **STATUS,
                "busy": True,
                "job": {"state": "running", "progress": 0.5, "message": "x"},
            },
        },
    )
    value = result.value
    assert TITLE in value["title"]
    assert "video deleted, audiobook kept" in value["text"] and "with subtitles" in value["text"]
    assert "needs every released episode downloaded (1 of 3" in value["text"]
    assert value["buttons"] == 1  # the deleted episode has no button
    assert result.calls_of("anime_audiobook_start") == [[77, 2, False, True]]


# ---------------------------------------------------------------- with FFmpeg


def _ffmpeg(*args: str) -> None:
    ffmpeg, _ = paired_ffmpeg()
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", *args], check=True, timeout=120)


def _video(path: Path, seconds: float = 20.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size=96x64:rate=5:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={seconds}",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "48k",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=jpn",
        str(path),
    )  # fmt: skip
    return path


def _srt(path: Path, cues: list[tuple[str, str, str]]) -> Path:
    blocks = [f"{index}\n{start} --> {end}\n{text}\n" for index, (start, end, text) in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _probe(path: Path) -> dict:
    _, ffprobe = paired_ffmpeg()
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


@pytest.fixture()
def world(tmp_path):
    ffmpeg, ffprobe = paired_ffmpeg()
    db = Database(tmp_path / "pudge.sqlite3")
    books = AudiobookService(db, ffprobe=ffprobe, mpv="mpv", cache_dir=tmp_path / "cache", ffmpeg=ffmpeg)
    maker = aa.AnimeAudiobookMaker(
        audiobooks=books,
        root=tmp_path / "library" / aa.ROOT_NAME,
        cache_dir=tmp_path / "cache",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    cover = tmp_path / "cover.png"
    _ffmpeg("-f", "lavfi", "-i", "color=blue:size=40x60", "-frames:v", "1", str(cover))
    videos = tmp_path / "library" / TITLE
    one = aa.EpisodeSource(
        episode=1,
        video=_video(videos / "ep01.mkv"),
        subtitle=_srt(
            videos / "ep01.ja.srt",
            [("00:00:01,000", "00:00:03,000", "こんにちは。"), ("00:00:06,000", "00:00:08,000", "うたのじかん"),
             ("00:00:12,000", "00:00:14,000", "さようなら！")],
        ),
        segments=[(5.0, 10.0)],
    )  # fmt: skip
    two = aa.EpisodeSource(episode=2, video=_video(videos / "ep02.mkv", 12.0))
    yield SimpleNamespace(db=db, books=books, maker=maker, cover=cover, one=one, two=two, tmp=tmp_path)
    books.close()


def _book(world, book_id: int) -> dict:
    return world.books.book(book_id, include_transcription=False)


@requires_ffmpeg
def test_an_episode_becomes_an_audiobook_with_its_subtitles_and_without_the_opening(world) -> None:
    result = world.maker.make(media_id=77, title=TITLE, sources=[world.one], whole=False, cover=world.cover)
    folder = aa.title_folder(world.maker.root, 77, TITLE)
    output = folder / aa.episode_file_name(1)
    probe = _probe(output)
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1 and audio[0]["codec_name"] == "aac"
    assert any(s.get("disposition", {}).get("attached_pic") for s in probe["streams"])  # the anime cover
    assert float(probe["format"]["duration"]) == pytest.approx(15.0, abs=0.3)  # 20 s - the 5 s opening
    entry = world.maker.manifest(folder)["episodes"]["1"]
    assert entry["stream"] == 1 and entry["language"] == "jpn" and entry["cuts"] == [[5.0, 10.0]]
    assert not list(folder.glob(".partial-*"))

    book = _book(world, result["audiobook_id"])
    assert book["series_title"] == TITLE and book["volume"] == 1
    assert book["title"] == f"{TITLE} — Episode 1"
    assert [c["title"] for c in book["chapters"]] == ["Episode 1"]
    assert book["chapters"][0]["end"] == pytest.approx(15.0, abs=0.3)

    document = aa.read_along_document(world.db, result["audiobook_id"])
    sentences = document["chapters"][0]["sentences"]
    assert [s["text"] for s in sentences] == ["こんにちは。", "さようなら！"]  # the line inside the cut goes
    anchors = document["chapters"][0]["anchors"]
    assert anchors[0] == [1000, 0]
    assert [7000, sentences[1]["start"]] in anchors  # 12 s in the video, 7 s in the audiobook

    # Idempotent: a second run reuses the file and the book.
    stamp = output.stat().st_mtime_ns
    again = world.maker.make(media_id=77, title=TITLE, sources=[world.one], whole=False, cover=world.cover)
    assert again["audiobook_id"] == result["audiobook_id"] and output.stat().st_mtime_ns == stamp
    # Never linked to a light novel by title.
    assert world.books.auto_link_audiobook(result["audiobook_id"]) is None


@requires_ffmpeg
def test_whole_anime_is_one_book_of_episode_chapters_on_the_same_shelf(world) -> None:
    first = world.maker.make(media_id=77, title=TITLE, sources=[world.one], whole=False)
    whole = world.maker.make(media_id=77, title=TITLE, sources=[world.two, world.one], whole=True)
    folder = aa.title_folder(world.maker.root, 77, TITLE)
    # Episode 2 kept every second and its AAC as is (no cut known: stream copy).
    assert world.maker.manifest(folder)["episodes"]["2"]["codec"] == "copy"
    book = _book(world, whole["audiobook_id"])
    assert [c["title"] for c in book["chapters"]] == ["Episode 1", "Episode 2"]
    assert book["chapters"][1]["start"] == pytest.approx(15.0, abs=0.3)
    assert book["duration"] == pytest.approx(27.0, abs=0.5)
    assert book["series_key"] == _book(world, first["audiobook_id"])["series_key"]
    with world.db.connect() as conn:
        paths = [
            r["path"]
            for r in conn.execute(
                "SELECT path FROM audiobook_files WHERE book_id=? ORDER BY file_index",
                (whole["audiobook_id"],),
            )
        ]
        assert library_overrides.load(conn, "audiobook")[whole["audiobook_id"]]["series_title"] == TITLE
    assert paths == [str(folder / aa.episode_file_name(1)), str(folder / aa.episode_file_name(2))]
    document = aa.read_along_document(world.db, whole["audiobook_id"])
    assert len(document["chapters"]) == 1  # episode 2 has no subtitles: audio only
    assert world.maker.made_episodes(77, TITLE) == {1: first["audiobook_id"]}
    assert world.maker.whole_book(77, TITLE) == whole["audiobook_id"]

    # The videos go: the audiobooks stay and are reused as made.
    world.one.video.unlink()
    world.two.video.unlink()
    again = world.maker.make(media_id=77, title=TITLE, sources=[world.one, world.two], whole=True)
    assert again["audiobook_id"] == whole["audiobook_id"]

    # Deleting the episode book keeps the file the whole book still plays; then both go.
    world.books.delete(first["audiobook_id"])
    assert world.maker.prune_unreferenced(paths) == []
    world.books.delete(whole["audiobook_id"])
    assert sorted(world.maker.prune_unreferenced(paths)) == sorted(Path(p) for p in paths)
    assert not folder.exists()


@requires_ffmpeg
def test_cancel_and_failure_leave_no_partial_files(world) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(aa.Cancelled):
        world.maker.make(media_id=78, title="Other", sources=[world.two], whole=False, cancel=cancel)
    assert not aa.title_folder(world.maker.root, 78, "Other").exists()
    missing = aa.EpisodeSource(episode=3, video=world.tmp / "gone.mkv")
    with pytest.raises(ValueError):
        world.maker.make(media_id=79, title="Third", sources=[missing], whole=False)
    assert not aa.title_folder(world.maker.root, 79, "Third").exists()


@requires_ffmpeg
def test_background_job_reports_progress_and_the_space_check_refuses(world, monkeypatch) -> None:
    status = world.maker.start(media_id=80, title=TITLE, sources=[world.two], whole=False)
    assert status["state"] == "running"
    world.maker.wait(80, timeout=120)
    job = world.maker.job(80)
    assert job["state"] == "succeeded" and job["progress"] == 1.0 and job["audiobook_id"]

    monkeypatch.setattr(aa.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1024))
    world.maker.start(media_id=81, title="Tiny disk", sources=[world.two], whole=False)
    world.maker.wait(81, timeout=120)
    job = world.maker.job(81)
    assert job["state"] == "failed" and "disk space" in job["error"]
    assert not aa.title_folder(world.maker.root, 81, "Tiny disk").exists()


@requires_ffmpeg
def test_the_iphone_gets_the_subtitles_as_read_along(world) -> None:
    from pudge.iphone_service import IPhoneService

    result = world.maker.make(media_id=77, title=TITLE, sources=[world.one], whole=False)
    phone = IPhoneService(world.db, world.tmp / "iphone", audiobooks=world.books)
    read_along = phone._read_along(result["audiobook_id"])
    assert read_along["fileName"] == "readalong.json"
    stored = json.loads(
        (world.tmp / "iphone" / "objects" / f"readalong-{read_along['sha256']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["schema"] == 1 and stored["chapters"][0]["sentences"][0]["text"] == "こんにちは。"
    assert phone._series_info(result["audiobook_id"]) == {"seriesTitle": TITLE, "volume": 1}


@requires_ffmpeg
def test_window_api_lists_episodes_and_makes_the_whole_anime(world) -> None:
    import logging

    from pudge.manager_models import LibraryAnime, LibraryEpisode
    from pudge.web_app import WebAppApi

    world.db.upsert_anime(
        LibraryAnime(media_id=77, title=TITLE, episodes=2, media_status="FINISHED", status="CURRENT")
    )
    for source in (world.one, world.two):
        world.db.upsert_episode(
            LibraryEpisode(media_id=77, title=TITLE, episode=source.episode, video_path=source.video)
        )
    world.db.set_subtitle_ready(world.one.video, world.one.subtitle, None, origin="prepared")
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=world.db, cached_cover_path=lambda anime: world.cover)
    api.config = SimpleNamespace(
        library=SimpleNamespace(root_dir=world.tmp / "library"),
        paths=SimpleNamespace(cache_dir=world.tmp / "cache"),
        tools=SimpleNamespace(ffmpeg=world.maker.ffmpeg, ffprobe=world.maker.ffprobe),
    )
    api.audiobooks = world.books
    api.logger = logging.getLogger("test")
    api.job_center = None

    status = api.anime_audiobook_status(77)
    assert status["all_local"] is True and status["released"] == 2
    assert [(row["episode"], row["subtitles"]) for row in status["episodes"]] == [(1, True), (2, False)]
    api.anime_audiobook_start(77, None, True, True)
    api._anime_audiobook_maker().wait(77, timeout=120)
    status = api.anime_audiobook_status(77)
    assert status["job"]["state"] == "succeeded", status["job"]
    assert status["whole_audiobook_id"] == status["job"]["audiobook_id"]
    with pytest.raises(ValueError):
        api.anime_audiobook_start(77, 5)
    # Deleting the episode video keeps the audiobook; deleting the audiobook keeps the video.
    world.two.video.unlink()
    assert api.anime_audiobook_status(77)["whole_audiobook_id"] == status["whole_audiobook_id"]
    api.consumption = SimpleNamespace(detach_library_media=lambda **_kwargs: None)
    api.audiobook_delete(status["whole_audiobook_id"])
    assert world.one.video.is_file()
    assert not aa.title_folder(world.maker.root, 77, TITLE).exists()


@requires_ffmpeg
def test_an_episode_made_again_updates_the_whole_book(world) -> None:
    whole = world.maker.make(media_id=77, title=TITLE, sources=[world.one, world.two], whole=True)
    assert _book(world, whole["audiobook_id"])["chapters"][0]["end"] == pytest.approx(15.0, abs=0.3)
    # Made again with the opening kept: the episode's file is new, and the whole book follows.
    world.maker.make(media_id=77, title=TITLE, sources=[world.one], whole=False, skip_op_ed=False)
    chapters = _book(world, whole["audiobook_id"])["chapters"]
    assert chapters[0]["end"] == pytest.approx(20.0, abs=0.3)
    assert chapters[1]["start"] == pytest.approx(chapters[0]["end"])
    document = aa.read_along_document(world.db, whole["audiobook_id"])
    assert [s["text"] for s in document["chapters"][0]["sentences"]] == [
        "こんにちは。",
        "うたのじかん",
        "さようなら！",
    ]
