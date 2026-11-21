"""Review round 4 (Mac app): anime audiobooks survive a renamed anime, a watched library
folder and a cancelled "whole anime"; WebVTT furigana is not read as text.

Invented data only: synthetic audio made with FFmpeg, invented titles and lines.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from media_tools import paired_ffmpeg, requires_ffmpeg

from pudge import anime_audiobooks as aa
from pudge import library_overrides
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.subtitle_formats import convert_to_plain_srt, parse_srt, parse_webvtt

TITLE = "Lantern Tanuki Club"
RENAMED = "Chouchin Tanuki Bu"


def _video(path: Path, seconds: float = 6.0) -> Path:
    ffmpeg, _ = paired_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:a", "aac", "-b:a", "48k", "-metadata:s:a:0", "language=jpn", str(path),
        ],
        check=True,
        timeout=120,
    )  # fmt: skip
    return path


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
    videos = tmp_path / "videos"
    sources = [aa.EpisodeSource(episode=n, video=_video(videos / f"ep{n:02d}.mka")) for n in (1, 2, 3)]
    yield SimpleNamespace(db=db, books=books, maker=maker, sources=sources, tmp=tmp_path)
    books.close()


def _titles(world) -> dict[int, str]:
    with world.db.connect() as conn:
        return {int(r["id"]): str(r["title"]) for r in conn.execute("SELECT id,title FROM audiobooks")}


def _series(world) -> dict[int, str]:
    with world.db.connect() as conn:
        return {k: v["series_title"] for k, v in library_overrides.load(conn, "audiobook").items()}


# ---------------------------------------------------------------- a renamed anime


@requires_ffmpeg
def test_a_renamed_anime_keeps_its_folder_books_and_shelf(world) -> None:
    one, two, _three = world.sources
    first = world.maker.make(media_id=77, title=TITLE, sources=[one], whole=False)
    folder = aa.title_folder(world.maker.root, 77, TITLE)
    assert world.maker.folder(77, TITLE) == folder
    # AniList gave the anime another title: the episode made before is still listed as made
    # (a second folder made every file and every book again).
    assert world.maker.folder(77, RENAMED) == folder
    assert world.maker.made_episodes(77, RENAMED) == {1: first["audiobook_id"]}
    stamp = (folder / aa.episode_file_name(1)).stat().st_mtime_ns
    second = world.maker.make(media_id=77, title=RENAMED, sources=[one, two], whole=False)
    assert (folder / aa.episode_file_name(1)).stat().st_mtime_ns == stamp
    assert (folder / aa.episode_file_name(2)).is_file()
    assert [item.name for item in world.maker.root.iterdir()] == [folder.name]
    assert first["audiobook_id"] in second["audiobook_ids"] and len(_titles(world)) == 2
    # One shelf under the new name.
    assert set(_series(world).values()) == {RENAMED}
    assert world.maker.manifest(folder)["title"] == RENAMED
    # Another anime whose folder name merely ends alike is not taken.
    other = world.maker.root / "Somebody else [177]"
    other.mkdir()
    (other / aa.MARKER).write_text('{"schema":1,"media_id":177,"episodes":{}}', encoding="utf-8")
    assert world.maker.folder(177, "New name") == other
    assert world.maker.folder(7, "Seven") == aa.title_folder(world.maker.root, 7, "Seven")


@requires_ffmpeg
def test_a_series_name_typed_by_hand_survives_the_rename(world) -> None:
    one, two, _three = world.sources
    first = world.maker.make(media_id=77, title=TITLE, sources=[one], whole=False)
    with world.db.connect() as conn:
        library_overrides.store(conn, "audiobook", first["audiobook_id"], volume=1, series_title="My shelf")
    second = world.maker.make(media_id=77, title=RENAMED, sources=[two], whole=False)
    series = _series(world)
    assert series[first["audiobook_id"]] == "My shelf"
    assert series[second["audiobook_id"]] == RENAMED


# ---------------------------------------------------------------- imported again


@requires_ffmpeg
def test_importing_a_made_audiobook_again_changes_nothing(world) -> None:
    one, two, _three = world.sources
    made = world.maker.make(media_id=77, title=TITLE, sources=[one, two], whole=True)
    episode = world.maker.make(media_id=77, title=TITLE, sources=[one], whole=False)
    folder = aa.title_folder(world.maker.root, 77, TITLE)
    before = _titles(world)
    chapters = world.books.book(made["audiobook_id"], include_transcription=False)["chapters"]
    # A drop of the file or the folder, or a watched folder that holds the library.
    again = world.books.import_file(folder / aa.episode_file_name(1), prepare_transcription=False)
    assert again["id"] == episode["audiobook_id"]
    whole = world.books.import_folder(folder, prepare_transcription=False)
    assert whole["id"] == made["audiobook_id"]
    assert _titles(world) == before
    assert before[episode["audiobook_id"]] == f"{TITLE} — Episode 1"
    assert world.books.book(made["audiobook_id"], include_transcription=False)["chapters"] == chapters
    assert [c["title"] for c in chapters] == ["Episode 1", "Episode 2"]


@requires_ffmpeg
def test_a_watched_library_folder_adds_no_book_for_the_made_episodes(world) -> None:
    from pudge.web_app import WebAppApi

    one, two, _three = world.sources
    world.maker.make(media_id=77, title=TITLE, sources=[one, two], whole=False)  # two episode books
    plain = _video(world.tmp / "library" / "Plain Book" / "part.m4a", 2.0)
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        paths=SimpleNamespace(download_dirs=[world.tmp / "library"], max_scanned_files=8000),
        library=SimpleNamespace(recursive=True),
    )
    api.manager = SimpleNamespace(db=world.db)
    api.audiobooks = world.books
    api.light_novels = api.manga = None
    api.logger = logging.getLogger("test")
    before = _titles(world)
    stats = api.scan_watched_media_folders()
    after = _titles(world)
    # Only the ordinary audio file became a book; the two episode files' folder did not
    # become a third "Lantern Tanuki Club [77]" book and no title turned into "Episode 001".
    assert stats["audiobooks"] == 1
    assert {k: v for k, v in after.items() if k in before} == before
    assert sorted(set(after.values()) - set(before.values())) == [plain.stem]


# ---------------------------------------------------------------- cancel and deletion


@requires_ffmpeg
def test_a_cancelled_whole_anime_leaves_no_episode_files_behind(world, monkeypatch) -> None:
    one, two, three = world.sources
    kept = world.maker.make(media_id=77, title=TITLE, sources=[one], whole=False)
    folder = aa.title_folder(world.maker.root, 77, TITLE)
    cancel = threading.Event()
    real = world.maker._episode_audio

    def cancel_after_second(folder_, manifest, source, **kwargs):
        result = real(folder_, manifest, source, **kwargs)
        if source.episode == 2:
            cancel.set()
        return result

    monkeypatch.setattr(world.maker, "_episode_audio", cancel_after_second)
    with pytest.raises(aa.Cancelled):
        world.maker.make(media_id=77, title=TITLE, sources=[one, two, three], whole=True, cancel=cancel)
    # Episode 1 is a book and stays; episode 2 was made for the cancelled book only.
    assert sorted(item.name for item in folder.glob("*.m4a")) == [aa.episode_file_name(1)]
    assert set(world.maker.manifest(folder)["episodes"]) == {"1"}
    assert world.maker.made_episodes(77, TITLE) == {1: kept["audiobook_id"]}

    # A title cancelled before any book existed leaves nothing at all.
    cancel.clear()
    with pytest.raises(aa.Cancelled):
        world.maker.make(media_id=78, title="Other", sources=[two, three], whole=True, cancel=cancel)
    assert not aa.title_folder(world.maker.root, 78, "Other").exists()


@requires_ffmpeg
def test_a_failure_keeps_the_episodes_made_so_far_for_the_next_try(world) -> None:
    one, two, _three = world.sources
    missing = aa.EpisodeSource(episode=3, video=world.tmp / "gone.mka")
    with pytest.raises(ValueError):
        world.maker.make(media_id=77, title=TITLE, sources=[one, two, missing], whole=True)
    folder = aa.title_folder(world.maker.root, 77, TITLE)
    assert len(list(folder.glob("*.m4a"))) == 2


@requires_ffmpeg
def test_deleted_books_files_go_while_another_title_is_being_made(world, monkeypatch) -> None:
    one, two, _three = world.sources
    made = world.maker.make(media_id=77, title=TITLE, sources=[one], whole=False)
    output = aa.title_folder(world.maker.root, 77, TITLE) / aa.episode_file_name(1)
    busy: set[int] = {99}
    monkeypatch.setattr(world.maker, "busy", lambda media_id: int(media_id) in busy)
    world.maker._jobs[99] = {"media_id": 99}
    world.books.delete(made["audiobook_id"])
    # Title 99 is being made: that kept every other title's deleted files for good.
    assert world.maker.prune_unreferenced([output]) == [output]
    assert not output.exists()
    # The title's own job still protects its files.
    again = world.maker.make(media_id=77, title=TITLE, sources=[two], whole=False)
    second = aa.title_folder(world.maker.root, 77, TITLE) / aa.episode_file_name(2)
    world.books.delete(again["audiobook_id"])
    busy.add(77)
    assert world.maker.prune_unreferenced([second]) == []
    assert second.is_file()


def test_the_window_uses_one_maker_and_follows_the_library_folder(tmp_path) -> None:
    from pudge.web_app import WebAppApi

    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        library=SimpleNamespace(root_dir=tmp_path / "one"),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.audiobooks = SimpleNamespace(db=None)
    api.job_center = None
    made: list[object] = []
    barrier = threading.Barrier(8)

    def ask() -> None:
        barrier.wait(timeout=10)
        made.append(api._anime_audiobook_maker())

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(made) == 8 and len({id(maker) for maker in made}) == 1
    maker = api._anime_audiobook_maker()
    assert maker.root == tmp_path / "one" / aa.ROOT_NAME
    api.config.library.root_dir = tmp_path / "two"  # changed in Settings
    assert api._anime_audiobook_maker().root == tmp_path / "two" / aa.ROOT_NAME


# ---------------------------------------------------------------- WebVTT


def test_webvtt_furigana_and_direction_marks_are_not_text(tmp_path) -> None:
    text = (
        "WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:03.000 line:90%\n"
        "&lrm;<c.japanese><ruby>提灯<rt>ちょうちん</rt></ruby>を<ruby>持<rp>(</rp><rt>も</rt><rp>)</rp></ruby>って</c>\n\n"
        "00:04.000 --> 00:06.500\n"
        "<v Tanuki>たぬき &amp; きつね\n"
        "<i></i>\n"
        "\u200eつぎ\n"
    )
    cues = parse_webvtt(text)
    # The reading of a word is not spoken text (it read 提灯ちょうちんを持(も)って), a line of
    # tags alone leaves no empty line inside the cue.
    assert cues == [(1.0, 3.0, "提灯を持って"), (4.0, 6.5, "たぬき &amp; きつね\nつぎ")]
    source = tmp_path / "Invented - 01.ja.vtt"
    source.write_text(text, encoding="utf-8")
    output, result = convert_to_plain_srt(source, tmp_path / "cache")
    assert result["converted"] is True
    assert [cue[2] for cue in parse_srt(output)] == ["提灯を持って", "たぬき & きつね\nつぎ"]


# ---------------------------------------------------------------- the dialog


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


def test_a_failed_job_s_error_is_shown_as_text_not_markup(page) -> None:
    # FFmpeg's message names the video file, and a release can be named anything.
    error = "Invalid data: /library/<img src=x onerror=boom()>&<b>ep</b>.mkv"
    status = {
        "media_id": 77,
        "title": TITLE,
        "released": 3,
        "episodes": [
            {"episode": 1, "episode_end": None, "local": True, "subtitles": False, "audiobook_id": None}
        ],
        "all_local": False,
        "whole_audiobook_id": None,
        "job": {"state": "failed", "progress": 0.2, "message": "Could not make it", "error": error},
        "busy": False,
    }
    result = page.run(
        r"""
showAnimeMenu(77, false, 10, 10);
await __click('[data-context-action="anime-audiobook"]');
await __flush();
const body = document.querySelector('#modalBody');
return {text: __text('#modalBody'), html: body.innerHTML,
        injected: body.querySelectorAll('img, b').length};
""",
        state={"current": [_anime(local_count=1)]},
        api={"anime_audiobook_status": status},
    )
    value = result.value
    assert value["injected"] == 0
    assert "<img" not in value["html"] and "&lt;img src=x" in value["html"]
    assert "<img src=x onerror=boom()>&<b>ep</b>.mkv" in value["text"]
