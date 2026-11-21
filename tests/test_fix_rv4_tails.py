"""Review round 4, the tails: anime audiobooks, read-along, the window server, restore.

1. A "whole anime" run that failed half way: the made episodes are listed as made, reused
   by the next run, and removed when the dialog is closed, the run is cancelled or the
   title's audiobooks are deleted.
2. The free space is checked for the whole run before the first episode is made.
3. An episode made again with another cut: the saved place stays on the same content.
4. Anime audiobooks stand on a shelf of their own (named apart only on a collision).
5. Two subtitle lines shown at once start together.
6. The player's lines are kept per book *and* revision.
7. Made files are removed by their folder's marker, not by where the library is now.
8. An audio track that starts later than the video: the lines follow.
9. iPhone covers and anime read-along are not rebuilt per request.
10. A non-ASCII token is answered 403.
11. A crafted archive member name fails the restore before anything is staged.

Invented data only (synthetic FFmpeg audio/video, invented titles and lines).
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import subprocess
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from media_tools import paired_ffmpeg, requires_ffmpeg

from pudge import anime_audiobooks as aa
from pudge import backup, library_overrides, read_along
from pudge.audiobooks import AudiobookService
from pudge.bridge_methods import PAGE_METHODS
from pudge.database import Database
from pudge.i18n import catalog

ROOT = Path(__file__).resolve().parents[1]
TITLE = "Kettle Harbor Patrol"


# ---------------------------------------------------------------- pure parts


def test_a_time_of_the_cut_audio_goes_back_to_the_video() -> None:
    spans = [(5.0, 10.0), (20.0, 23.0)]
    for video in (0.0, 4.9, 10.0, 12.5, 19.9, 23.0, 40.0):
        assert aa.unmap_time(aa.remap_time(video, spans), spans) == pytest.approx(video)
    assert aa.unmap_time(7.0, []) == 7.0


def test_lines_follow_an_audio_track_that_starts_late() -> None:
    cues = [(0.5, 1.5, "まだ音がない"), (3.0, 4.0, "はじめ"), (12.0, 14.0, "つぎ")]
    # No cut: the made audio starts at the track's first sample (2 s into the video).
    assert aa.remap_cues(cues, [], audio_start=2.0) == [(1.0, 2.0, "はじめ"), (10.0, 12.0, "つぎ")]
    # With the opening (5..10) cut as well.
    assert aa.remap_cues(cues, [(5.0, 10.0)], audio_start=2.0) == [(1.0, 2.0, "はじめ"), (5.0, 7.0, "つぎ")]
    # The track starts inside the cut: the audio begins where the cut ends.
    assert aa.remap_cues(cues, [(0.0, 10.0)], audio_start=2.0) == [(2.0, 4.0, "つぎ")]
    assert aa.audio_offset({"start_time": "10.0"}, {"start_time": "12.0"}) == 2.0
    assert aa.audio_offset({"start_time": "0"}, {"start_time": "0.021"}) == 0.0  # encoder priming
    assert aa.audio_offset({}, {"start_time": "N/A"}) == 0.0


def test_two_lines_shown_at_once_start_together() -> None:
    cues = [(1.0, 3.0, "みぎだ"), (1.0, 3.0, "ひだりだ"), (6.0, 8.0, "おわり")]
    chapter = aa.read_along_chapter(cues, index=0, title="Ep", duration=10)
    assert [sentence["t"] for sentence in chapter["sentences"]] == [[1000, 3000], [1000, 3000], [6000, 8000]]
    # The phone's clock stays monotonic, in the order of the lines.
    anchors = chapter["anchors"]
    assert anchors == sorted(anchors) and all(a[1] <= b[1] for a, b in zip(anchors, anchors[1:]))
    assert [sentence["start"] for sentence in chapter["sentences"]] == sorted(
        sentence["start"] for sentence in chapter["sentences"]
    )
    lines = read_along.transcript({"chapters": [chapter]})["lines"]
    assert [(line["start"], line["end"], line["text"]) for line in lines] == [
        (1.0, 3.0, "みぎだ"),
        (1.0, 3.0, "ひだりだ"),
        (6.0, 8.0, "おわり"),
    ]
    shifted = aa.shift_chapter(chapter, index=1, title="Ep 2", offset_ms=10_000)
    assert shifted["sentences"][1]["t"] == [11000, 13000] and chapter["sentences"][1]["t"] == [1000, 3000]
    assert read_along.transcript({"chapters": [shifted]})["lines"][1]["start"] == 11.0
    # A chapter written before (no own times) is still timed by its anchors.
    old = {**chapter, "sentences": [{k: v for k, v in s.items() if k != "t"} for s in chapter["sentences"]]}
    assert read_along.transcript({"chapters": [old]})["lines"][1]["start"] == 3.0


def test_strings_and_the_bridge_method_exist() -> None:
    assert "anime_audiobook_discard" in PAGE_METHODS
    for language in ("en", "ru"):
        strings = catalog(language)
        for key in (
            "animeAudiobook.madeNoBook",
            "animeAudiobook.unfinishedHint",
            "error.animeAudiobook.noSpaceRun",
            "error.backup.invalidArchiveMember",
        ):
            assert strings.get(key), (language, key)


# ---------------------------------------------------------------- 4: the shelf


def _plain_book(db: Database, path: Path, title: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,60,1,1)",
                (str(path), title),
            ).lastrowid
        )


def _made_folder(root: Path, name: str, episodes: int = 2, *, media_id: int = 31) -> tuple[Path, list[Path]]:
    folder = root / f"{name} [{media_id}]"
    folder.mkdir(parents=True)
    files = []
    manifest = {"schema": aa.SCHEMA, "media_id": media_id, "title": name, "episodes": {}}
    for number in range(1, episodes + 1):
        audio = folder / aa.episode_file_name(number)
        audio.write_bytes(b"\0" * 16)
        manifest["episodes"][str(number)] = {"file": audio.name, "episode": number, "duration": 6.0}
        files.append(audio)
    (folder / aa.MARKER).write_text(json.dumps(manifest), encoding="utf-8")
    return folder, files


@pytest.fixture()
def library(tmp_path):
    db = Database(tmp_path / "pudge.sqlite3")
    books = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache", ffmpeg="ffmpeg")
    maker = aa.AnimeAudiobookMaker(
        audiobooks=books, root=tmp_path / "library" / aa.ROOT_NAME, cache_dir=tmp_path / "cache"
    )
    yield SimpleNamespace(db=db, books=books, maker=maker, tmp=tmp_path)
    books.close()


def _shelf(library, book_id: int) -> tuple[str, str, int]:
    book = library.books.book(book_id, include_transcription=False)
    return book["series_key"], book["series_title"], book["volume"]


def test_anime_audiobooks_do_not_join_the_light_novels_shelf(library) -> None:
    folder, files = _made_folder(library.maker.root, TITLE)
    episode = _plain_book(library.db, files[0], f"{TITLE} — Episode 1")
    with library.db.connect() as conn:
        library_overrides.store(conn, "audiobook", episode, volume=1, series_title=TITLE)
    key, title, volume = _shelf(library, episode)
    assert key.startswith(library_overrides.ANIME_SHELF_PREFIX) and title == TITLE and volume == 1

    # The light novel's audiobook series of the same name arrives: another shelf, and now
    # the anime's one is named apart (the iPhone groups by the name).
    novel = _plain_book(library.db, library.tmp / "novels" / f"{TITLE} 第1巻.m4b", f"{TITLE} 第1巻")
    novel_key, novel_title, novel_volume = _shelf(library, novel)
    assert (novel_title, novel_volume) == (TITLE, 1) and novel_key and novel_key != key
    assert _shelf(library, episode) == (key, f"{TITLE} (anime)", 1)
    catalog_rows = {row["id"]: row for row in library.books.search_catalog()}
    assert catalog_rows[episode]["series_key"] == key and catalog_rows[novel]["series_key"] == novel_key
    assert catalog_rows[episode]["series_title"] == f"{TITLE} (anime)"
    # What the user typed stays what the edit dialog shows.
    assert library.books.book(episode, include_transcription=False)["manual_series_title"] == TITLE

    # A hand-named ordinary book keeps joining the shelf of that name.
    other = _plain_book(library.db, library.tmp / "novels" / "side story.m4b", "Side story")
    library.books.set_metadata_override(other, volume=2, series_title=TITLE)
    assert _shelf(library, other)[0] == novel_key
    # The novel goes away: the plain name comes back.
    with library.db.connect() as conn:
        conn.execute("DELETE FROM audiobooks WHERE id IN (?,?)", (novel, other))
        library_overrides.forget(conn, "audiobook", [other])
    library.books._overrides = None
    assert _shelf(library, episode) == (key, TITLE, 1)


def test_the_player_gets_a_revision_of_the_read_along_text(library) -> None:
    folder, files = _made_folder(library.maker.root, TITLE)
    book = _plain_book(library.db, files[0], "Episode")
    first = library.books.book(book, include_transcription=False)["read_along_revision"]
    assert first.startswith("anime:")
    assert library.books.book(book, include_transcription=False)["read_along_revision"] == first
    marker = folder / aa.MARKER
    stat = marker.stat()
    import os

    os.utime(marker, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # made again
    assert library.books.book(book, include_transcription=False)["read_along_revision"] != first
    plain = _plain_book(library.db, library.tmp / "plain.m4b", "Plain")
    assert library.books.book(plain, include_transcription=False)["read_along_revision"] == ""


# ---------------------------------------------------------------- 1, 7: files nobody uses


def test_unfinished_episodes_are_listed_and_discarded(library) -> None:
    folder, files = _made_folder(library.maker.root, TITLE, 3)
    book = _plain_book(library.db, files[0], "Episode 1")
    with library.db.connect() as conn:
        conn.execute(
            "INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,?,6,0,6)",
            (book, str(files[0]), "Episode 1"),
        )
    assert library.maker.unfinished_episodes(31, TITLE) == {2: files[1], 3: files[2]}
    assert library.maker.made_episodes(31, TITLE) == {1: book}
    assert sorted(library.maker.discard_unfinished(31, TITLE)) == files[1:]
    assert files[0].is_file() and not files[1].exists() and not files[2].exists()
    assert set(library.maker.manifest(folder)["episodes"]) == {"1"}
    assert library.maker.unfinished_episodes(31, TITLE) == {}
    assert library.maker.discard_unfinished(31, TITLE) == []


def test_deleting_a_titles_audiobook_takes_the_unfinished_files_too(library) -> None:
    folder, files = _made_folder(library.maker.root, TITLE, 3)
    # The episode book was deleted: its file and the two nobody lists go; the folder too.
    assert sorted(library.maker.prune_unreferenced([files[0]])) == files
    assert not folder.exists()


def test_files_under_an_earlier_library_folder_are_removed_by_their_marker(library) -> None:
    old_root = library.tmp / "old library" / aa.ROOT_NAME
    folder, files = _made_folder(old_root, TITLE, 2)
    assert old_root != library.maker.root
    # Not Pudge's: a folder without the marker, a marker that is no manifest, a link.
    stranger = library.tmp / "music"
    stranger.mkdir()
    (stranger / "Episode 001.m4a").write_bytes(b"x")
    fake = library.tmp / "fake"
    fake.mkdir()
    (fake / aa.MARKER).write_text("[]", encoding="utf-8")
    (fake / "Episode 001.m4a").write_bytes(b"x")
    link = folder / "Episode 009.m4a"
    link.symlink_to(stranger / "Episode 001.m4a")
    removed = library.maker.prune_unreferenced(
        [files[0], stranger / "Episode 001.m4a", fake / "Episode 001.m4a", link, folder / ".cover.jpg"]
    )
    assert sorted(removed) == files
    assert (stranger / "Episode 001.m4a").is_file() and (fake / "Episode 001.m4a").is_file()


def test_a_file_a_book_still_plays_is_kept_wherever_it_is(library) -> None:
    folder, files = _made_folder(library.tmp / "old" / aa.ROOT_NAME, TITLE, 2)
    whole = _plain_book(library.db, folder, "Whole")
    with library.db.connect() as conn:
        for index, audio in enumerate(files):
            conn.execute(
                "INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) "
                "VALUES(?,?,?,?,6,?,?)",
                (whole, index, str(audio), audio.stem, index * 6.0, index * 6.0 + 6),
            )
    assert library.maker.prune_unreferenced(files) == []
    assert all(audio.is_file() for audio in files)


# ---------------------------------------------------------------- 3: the saved place


def test_the_saved_place_follows_its_episode(library) -> None:
    folder, files = _made_folder(library.maker.root, TITLE, 2)
    one = aa.EpisodeSource(episode=1, video=library.tmp / "one.mkv")
    two = aa.EpisodeSource(episode=2, video=library.tmp / "two.mkv")
    whole = library.maker._add_whole_book(folder, TITLE, [(one, files[0], 15.0), (two, files[1], 12.0)])
    library.books.set_position(whole, 15.0 + 4.0)  # four seconds into episode 2
    # Episode 1 made again with its opening kept (5..10 was cut): 5 s longer.
    changes = {str(files[0]): ([(5.0, 10.0)], [])}
    again = library.maker._add_whole_book(
        folder, TITLE, [(one, files[0], 20.0), (two, files[1], 12.0)], changes
    )
    assert again == whole
    assert library.books.book(whole, include_transcription=False)["position"] == pytest.approx(24.0)

    # The listener was inside the episode that was cut otherwise: 7 s of the cut audio was
    # 12 s of the video, which is 12 s of the uncut one.
    library.books.set_position(whole, 7.0)
    library.maker._add_whole_book(folder, TITLE, [(one, files[0], 15.0), (two, files[1], 12.0)])
    library.books.set_position(whole, 7.0)
    library.maker._add_whole_book(folder, TITLE, [(one, files[0], 20.0), (two, files[1], 12.0)], changes)
    assert library.books.book(whole, include_transcription=False)["position"] == pytest.approx(12.0)
    # And back: the opening is cut again; a place inside it lands where it was cut.
    library.books.set_position(whole, 8.0)
    library.maker._add_whole_book(
        folder,
        TITLE,
        [(one, files[0], 15.0), (two, files[1], 12.0)],
        {str(files[0]): ([], [(5.0, 10.0)])},
    )
    assert library.books.book(whole, include_transcription=False)["position"] == pytest.approx(5.0)
    # Nothing changed: the place is left alone.
    library.maker._add_whole_book(folder, TITLE, [(one, files[0], 15.0), (two, files[1], 12.0)])
    assert library.books.book(whole, include_transcription=False)["position"] == pytest.approx(5.0)


# ---------------------------------------------------------------- 9: the iPhone


def test_anime_read_along_is_built_once_for_the_iphone(library, monkeypatch) -> None:
    from pudge.iphone_service import IPhoneService

    folder, files = _made_folder(library.maker.root, TITLE, 1)
    chapter = aa.read_along_chapter([(1.0, 2.0, "はじめ。")], index=0, title="Ep 1", duration=6)
    sidecar = aa.read_along_sidecar(files[0])
    sidecar.write_text(json.dumps(chapter, ensure_ascii=False), encoding="utf-8")
    book = _plain_book(library.db, files[0], "Episode 1")
    with library.db.connect() as conn:
        conn.execute(
            "INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,?,6,0,6)",
            (book, str(files[0]), "Episode 1"),
        )
    phone = IPhoneService(library.db, library.tmp / "iphone", audiobooks=library.books)
    built = []
    real = aa.read_along_document
    monkeypatch.setattr(
        aa, "read_along_document", lambda db, book_id: built.append(book_id) or real(db, book_id)
    )
    first = phone._anime_read_along(book)
    assert first["fileName"] == "readalong.json" and built == [book]
    assert phone._anime_read_along(book) == first and built == [book]
    # The subtitles were prepared again: another text, built once more.
    chapter = aa.read_along_chapter([(1.0, 2.5, "べつの文。")], index=0, title="Ep 1", duration=6)
    sidecar.write_text(json.dumps(chapter, ensure_ascii=False), encoding="utf-8")
    second = phone._anime_read_along(book)
    assert second["sha256"] != first["sha256"] and built == [book, book]
    assert phone._anime_read_along(book) == second and built == [book, book]
    # No text: remembered too, and an ordinary book is none of this.
    sidecar.unlink()
    assert phone._anime_read_along(book) is None and phone._anime_read_along(book) is None
    assert built == [book, book, book]
    plain = _plain_book(library.db, library.tmp / "plain.m4b", "Plain")
    assert phone._anime_read_along(plain) is None and built == [book, book, book]


def test_iphone_covers_are_scaled_once_per_source_file(tmp_path, monkeypatch) -> None:
    from PIL import Image

    from pudge.iphone_media import IPhoneMedia

    db = Database(tmp_path / "pudge.sqlite3")
    covers = tmp_path / "covers"
    covers.mkdir()
    Image.new("RGB", (600, 900), (20, 80, 160)).save(covers / "show.jpg", "JPEG")
    from pudge.manager_models import LibraryAnime

    db.upsert_anime(LibraryAnime(media_id=5, title="Show", episodes=1, cover_url="covers/show.jpg"))
    media = IPhoneMedia(SimpleNamespace(api=None, root=tmp_path, db=db, cover_dirs=[covers]))
    scaled = []
    real = IPhoneMedia._thumbnail
    monkeypatch.setattr(
        IPhoneMedia, "_thumbnail", staticmethod(lambda data, width=360: scaled.append(1) or real(data, width))
    )
    first, kind = media.cover("anime", 5)
    assert kind == "image/jpeg" and first[:2] == b"\xff\xd8" and scaled == [1]
    assert media.cover("anime", 5) == (first, kind) and scaled == [1]
    Image.new("RGB", (500, 700), (200, 30, 30)).save(covers / "show.jpg", "JPEG")  # another cover
    second, _ = media.cover("anime", 5)
    assert second != first and scaled == [1, 1]
    assert media.cover("anime", 5)[0] == second and scaled == [1, 1]


# ---------------------------------------------------------------- 10: the window server


def test_a_non_ascii_token_is_refused_not_dropped(tmp_path, monkeypatch) -> None:
    from test_web_app import make_api

    from pudge.web_app import _start_asset_server

    api = make_api(tmp_path)
    switched: list[bool] = []
    monkeypatch.setattr(
        api, "set_torrents_enabled", lambda desired: switched.append(desired) or {"enabled": desired}
    )
    server, _base = _start_asset_server(api)
    try:
        port = server.server_address[1]

        def post(token: bytes) -> int:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.putrequest("POST", "/api/torrents/enabled")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", "17")
                connection.putheader("X-Pudge-Token", token)  # bytes go out as they are
                connection.endheaders(b'{"enabled": true}')
                return connection.getresponse().status
            finally:
                connection.close()

        assert post("ключ".encode("utf-8")) == 403
        assert post(b"\xff\xfe") == 403
        assert switched == []
        assert post(api._http_session_token.encode()) == 200 and switched == [True]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 11: restore


@pytest.mark.parametrize(
    "member",
    [
        "prepared-subtitles/..",
        "cached-subtitles/.",
        "cached-subtitles/",
        "..",
        "a/../b.srt",
        "a\\b.srt",
        "a/b\x00.srt",
    ],
)
def test_a_crafted_member_name_is_refused(member: str) -> None:
    with pytest.raises(ValueError):
        backup._cached_member_name(member)


def test_ordinary_member_names_pass() -> None:
    assert backup._cached_member_name("cached-subtitles/0001-ep01.ja.srt") == "0001-ep01.ja.srt"
    assert backup._cached_member_name("prepared-subtitles/話 1.srt") == "話 1.srt"


def test_restore_fails_before_staging_a_crafted_archive(tmp_path) -> None:
    archive = tmp_path / "crafted.zip"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps(
                {
                    "app": backup.BACKUP_APP_ID,
                    "format": backup.BACKUP_FORMAT,
                    "cached_files": [
                        {
                            "archive": "prepared-subtitles/good.srt",
                            "original": "/x/good.srt",
                            "storage": "durable",
                        },
                        {"archive": "prepared-subtitles/..", "original": "/x/bad.srt", "storage": "durable"},
                    ],
                }
            ),
        )
        bundle.writestr("library.sqlite3", b"not a database")
        bundle.writestr("prepared-subtitles/good.srt", "1\n00:00:01,000 --> 00:00:02,000\nline\n")
        bundle.writestr("prepared-subtitles/..", "x")
    archive.write_bytes(data.getvalue())
    home = tmp_path / "home"
    home.mkdir()
    database = home / "library.sqlite3"
    database.write_bytes(b"live")
    with pytest.raises(ValueError) as caught:
        backup.restore_backup(
            archive_path=archive,
            config_path=home / "config.toml",
            database_path=database,
            cache_dir=tmp_path / "cache",
        )
    assert "invalidArchiveMember" in str(caught.value) or "invalid name" in str(caught.value)
    assert sorted(item.name for item in home.iterdir()) == ["library.sqlite3"]
    assert database.read_bytes() == b"live" and not (tmp_path / "cache").exists()


# ---------------------------------------------------------------- the pages

STATUS = {
    "media_id": 77,
    "title": TITLE,
    "released": 3,
    "episodes": [
        {"episode": 1, "local": True, "subtitles": True, "audiobook_id": None, "made": True},
        {"episode": 2, "local": True, "subtitles": True, "audiobook_id": None, "made": True},
        {"episode": 3, "local": True, "subtitles": False, "audiobook_id": None, "made": False},
    ],
    "all_local": True,
    "whole_audiobook_id": None,
    "unfinished": [1, 2],
    "job": {"state": "failed", "message": "Could not make the audiobook", "error": "boom", "updated_at": 1},
    "busy": False,
}
ANIME = {
    "media_id": 77,
    "title": TITLE,
    "episodes": 3,
    "progress": 1,
    "media_status": "FINISHED",
    "site_url": "https://anilist.co/anime/77",
    "local": None,
    "local_count": 3,
}
OPEN = r"""
showAnimeMenu(77, false, 10, 10);
await __click('[data-context-action="anime-audiobook"]');
await __flush();
"""


def test_closing_the_dialog_removes_what_a_failed_run_made(page) -> None:
    result = page.run(
        OPEN
        + r"""
const text = __text('#modalBody');
closeModal();
await __flush();
closeModal();
await __flush();
return {text};
""",
        state={"current": [ANIME]},
        api={"anime_audiobook_status": STATUS, "anime_audiobook_discard": {"ok": True, "removed": 2}},
    )
    assert "audio made, not in a book yet" in result.value["text"]
    assert "Episodes made so far: 2" in result.value["text"]
    assert result.calls_of("anime_audiobook_discard") == [[77]]


def test_making_again_or_nothing_unfinished_discards_nothing(page) -> None:
    again = page.run(
        OPEN
        + r"""
await __click('[data-anime-audiobook-all]');
await __flush();
closeModal();
await __flush();
return true;
""",
        state={"current": [ANIME]},
        api={
            "anime_audiobook_status": STATUS,
            "anime_audiobook_start": {**STATUS, "busy": True, "job": {"state": "running", "progress": 0.1}},
            "anime_audiobook_discard": {"ok": True, "removed": 0},
        },
    )
    assert again.called("anime_audiobook_start") and not again.called("anime_audiobook_discard")
    clean = page.run(
        OPEN + "closeModal(); await __flush(); return true;",
        state={"current": [ANIME]},
        api={
            "anime_audiobook_status": {**STATUS, "unfinished": [], "job": None},
            "anime_audiobook_discard": {"ok": True, "removed": 0},
        },
    )
    assert not clean.called("anime_audiobook_discard")


def test_the_players_lines_are_kept_per_revision(page) -> None:
    book = {
        "id": 7,
        "title": "Show",
        "duration": 60,
        "position": 1.2,
        "speed": 1,
        "playing": False,
        "player_running": True,
        "read_along": True,
        "read_along_revision": "anime:1",
        "chapters": [],
    }
    before = (
        "window.floatBooks = BOOKS; window.readAlongCalls = 0;"
        "__api.sidebar_audio_state = async () => ({books: window.floatBooks});"
        "__api.audiobook_read_along = async id => { window.readAlongCalls += 1; return {ok: true, book_id: id,"
        " source: 'anime', chapters: [], lines: [{start: 1, end: 2, text: 'made ' + window.readAlongCalls,"
        " chapter: 0}]}; };"
    ).replace("BOOKS", json.dumps([book]))
    result = page.run(
        r"""
        const seen = () => [window.readAlongCalls, __text('.abt-line .abt-text')];
        const poll = async () => { await __tick(1500); await __flush(); await __tick(50); await __flush(); };
        await __tick(1500);
        await __click('[data-sc-audio="text"]'); await __tick(50); await __flush(); await __tick(50);
        const opened = seen();
        await poll(); await poll();
        const same = seen();
        // The episode was made again (another cut): the same book, other lines.
        window.floatBooks = window.floatBooks.map(b => ({...b, read_along_revision: 'anime:2', duration: 55}));
        await poll(); await poll();
        const remade = seen();
        await poll();
        return {opened, same, remade, after: seen()};
        """,
        name="audiobook_float.html",
        api={"audiobook_float_settings": {"language": "en"}},
        before=before,
    )
    value = result.value
    assert value["opened"] == [1, "made 1"] and value["same"] == [1, "made 1"]
    assert value["remade"] == [2, "made 2"] and value["after"] == [2, "made 2"]


# ---------------------------------------------------------------- with FFmpeg


def _ffmpeg(*args: str) -> None:
    ffmpeg, _ = paired_ffmpeg()
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", *args], check=True, timeout=120)


def _video(path: Path, seconds: float = 20.0, *, audio_delay: float = 0.0, codec: str = "aac") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    delay = ["-itsoffset", str(audio_delay)] if audio_delay else []
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc=size=96x64:rate=5:duration={seconds}",
        *delay, "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds - audio_delay}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", codec, "-b:a", "48k",
        "-metadata:s:a:0", "language=jpn",
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
    yield SimpleNamespace(db=db, books=books, maker=maker, tmp=tmp_path, videos=tmp_path / "library" / TITLE)
    books.close()


CUES = [("00:00:03,000", "00:00:04,000", "はじめ。"), ("00:00:12,000", "00:00:14,000", "つぎ。")]


@requires_ffmpeg
@pytest.mark.parametrize(
    ("codec", "segments", "expected"),
    [
        ("aac", [], [(1.0, 2.0), (10.0, 12.0)]),  # copied
        ("libmp3lame", [], [(1.0, 2.0), (10.0, 12.0)]),  # converted
        ("aac", [(5.0, 10.0)], [(1.0, 2.0), (5.0, 7.0)]),  # cut
    ],
)
def test_a_late_audio_track_keeps_its_lines_on_the_audio(world, codec, segments, expected) -> None:
    video = _video(world.videos / "ep01.mkv", audio_delay=2.0, codec=codec)
    probed = _probe(video)
    track = next(s for s in probed["streams"] if s["codec_type"] == "audio")
    assert float(track["start_time"]) - float(probed["format"]["start_time"]) == pytest.approx(2.0, abs=0.1)
    source = aa.EpisodeSource(
        episode=1, video=video, subtitle=_srt(world.videos / "ep01.ja.srt", CUES), segments=segments
    )
    result = world.maker.make(media_id=77, title=TITLE, sources=[source], whole=False)
    folder = Path(result["folder"])
    made = _probe(folder / aa.episode_file_name(1))
    # The made audio starts at its own first sample (no delay kept as an empty edit).
    assert float(made["format"].get("start_time") or 0.0) == pytest.approx(0.0, abs=0.05)
    cut = sum(end - start for start, end in segments)
    assert float(made["format"]["duration"]) == pytest.approx(18.0 - cut, abs=0.3)
    entry = world.maker.manifest(folder)["episodes"]["1"]
    assert entry["audio_offset"] == pytest.approx(2.0, abs=0.1)
    lines = read_along.transcript(aa.read_along_document(world.db, result["audiobook_id"]))["lines"]
    assert [(line["start"], line["end"]) for line in lines] == [
        (pytest.approx(a, abs=0.1), pytest.approx(b, abs=0.1)) for a, b in expected
    ]


@requires_ffmpeg
def test_a_failed_whole_run_is_listed_resumed_and_cleaned(world) -> None:
    one = aa.EpisodeSource(episode=1, video=_video(world.videos / "ep01.mkv", 8.0))
    two = aa.EpisodeSource(episode=2, video=world.videos / "ep02.mkv")  # not there yet
    with pytest.raises(ValueError):
        world.maker.make(media_id=77, title=TITLE, sources=[one, two], whole=True)
    folder = aa.title_folder(world.maker.root, 77, TITLE)
    first = folder / aa.episode_file_name(1)
    assert first.is_file() and world.maker.whole_book(77, TITLE) is None
    assert world.maker.unfinished_episodes(77, TITLE) == {1: first}

    # "Make" again: the made episode is used as it is, and the book comes.
    _video(two.video, 6.0)
    stamp = first.stat().st_mtime_ns
    whole = world.maker.make(media_id=77, title=TITLE, sources=[one, two], whole=True)
    assert first.stat().st_mtime_ns == stamp and whole["audiobook_id"]
    assert world.maker.unfinished_episodes(77, TITLE) == {}
    assert world.maker.discard_unfinished(77, TITLE) == [] and first.is_file()

    # Another title fails and its dialog is closed: nothing of it stays.
    with pytest.raises(ValueError):
        world.maker.make(
            media_id=78,
            title="Other",
            sources=[
                aa.EpisodeSource(episode=1, video=one.video),
                aa.EpisodeSource(episode=2, video=world.tmp / "gone.mkv"),
            ],
            whole=True,
        )
    other = aa.title_folder(world.maker.root, 78, "Other")
    assert (other / aa.episode_file_name(1)).is_file()
    assert len(world.maker.discard_unfinished(78, "Other")) == 1 and not other.exists()

    # A failed run, then a cancelled one: the cancel leaves nothing either.
    with pytest.raises(ValueError):
        world.maker.make(
            media_id=79,
            title="Third",
            sources=[
                aa.EpisodeSource(episode=1, video=one.video),
                aa.EpisodeSource(episode=2, video=world.tmp / "gone.mkv"),
            ],
            whole=True,
        )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(aa.Cancelled):
        world.maker.make(
            media_id=79,
            title="Third",
            sources=[aa.EpisodeSource(episode=1, video=one.video)],
            whole=True,
            cancel=cancel,
        )
    assert not aa.title_folder(world.maker.root, 79, "Third").exists()


@requires_ffmpeg
def test_the_window_lists_unfinished_episodes_and_discards_them(world) -> None:
    from pudge.manager_models import LibraryAnime, LibraryEpisode
    from pudge.web_app import WebAppApi

    one = _video(world.videos / "ep01.mkv", 6.0)
    world.db.upsert_anime(
        LibraryAnime(media_id=77, title=TITLE, episodes=2, media_status="FINISHED", status="CURRENT")
    )
    world.db.upsert_episode(LibraryEpisode(media_id=77, title=TITLE, episode=1, video_path=one))
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=world.db, cached_cover_path=lambda anime: None)
    api.config = SimpleNamespace(
        library=SimpleNamespace(root_dir=world.tmp / "library"),
        paths=SimpleNamespace(cache_dir=world.tmp / "cache"),
        tools=SimpleNamespace(ffmpeg=world.maker.ffmpeg, ffprobe=world.maker.ffprobe),
    )
    api.audiobooks = world.books
    api.logger = logging.getLogger("test")
    api.job_center = None
    maker = api._anime_audiobook_maker()
    with pytest.raises(ValueError):
        maker.make(
            media_id=77,
            title=TITLE,
            sources=[
                aa.EpisodeSource(episode=1, video=one),
                aa.EpisodeSource(episode=2, video=world.tmp / "gone.mkv"),
            ],
            whole=True,
        )
    status = api.anime_audiobook_status(77)
    assert status["unfinished"] == [1]
    assert [(row["episode"], row["made"], row["audiobook_id"]) for row in status["episodes"]] == [
        (1, True, None)
    ]
    assert api.anime_audiobook_discard(77) == {"ok": True, "removed": 1}
    status = api.anime_audiobook_status(77)
    assert status["unfinished"] == [] and status["episodes"][0]["made"] is False
    assert not aa.title_folder(maker.root, 77, TITLE).exists()
    assert api.anime_audiobook_discard(404) == {"ok": True, "removed": 0}


@requires_ffmpeg
def test_the_whole_run_is_refused_before_the_first_episode(world, monkeypatch) -> None:
    sources = [
        aa.EpisodeSource(episode=number, video=_video(world.videos / f"ep{number:02d}.mkv", 6.0))
        for number in (1, 2, 3)
    ]
    # Room for one episode (as each is checked alone), not for what the three need.
    one_episode = aa.FREE_SPACE_MARGIN_BYTES + int(aa.AnimeAudiobookMaker._expected_bytes({}, 6.0) * 1.5)
    assert all(source.video.stat().st_size > 2048 for source in sources)
    free = {"bytes": aa.FREE_SPACE_MARGIN_BYTES + 2048}
    monkeypatch.setattr(aa.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free["bytes"]))
    started = []
    real = world.maker._run_ffmpeg
    monkeypatch.setattr(
        world.maker, "_run_ffmpeg", lambda *args, **kwargs: started.append(1) or real(*args, **kwargs)
    )
    with pytest.raises(ValueError) as caught:
        world.maker.make(media_id=77, title=TITLE, sources=sources, whole=True)
    assert "3 episodes" in str(caught.value) and "disk space" in str(caught.value)
    assert started == [] and not aa.title_folder(world.maker.root, 77, TITLE).exists()
    # Episodes already made are not counted again: with two of the three made, the run fits.
    free["bytes"] = 10 * 1024**3
    world.maker.make(media_id=77, title=TITLE, sources=sources[:2], whole=False)
    free["bytes"] = one_episode
    whole = world.maker.make(media_id=77, title=TITLE, sources=sources, whole=True)
    assert whole["audiobook_id"] and len(started) == 3


@requires_ffmpeg
def test_an_episode_recut_keeps_the_listeners_place_in_the_whole_book(world) -> None:
    one = aa.EpisodeSource(episode=1, video=_video(world.videos / "ep01.mkv", 20.0), segments=[(5.0, 10.0)])
    two = aa.EpisodeSource(episode=2, video=_video(world.videos / "ep02.mkv", 12.0))
    whole = world.maker.make(media_id=77, title=TITLE, sources=[one, two], whole=True)["audiobook_id"]
    chapters = world.books.book(whole, include_transcription=False)["chapters"]
    assert chapters[1]["start"] == pytest.approx(15.0, abs=0.3)
    world.books.set_position(whole, chapters[1]["start"] + 4.0)
    # Episode 1 again, the opening kept: the whole book is 5 s longer before episode 2.
    world.maker.make(media_id=77, title=TITLE, sources=[one], whole=False, skip_op_ed=False)
    book = world.books.book(whole, include_transcription=False)
    assert book["chapters"][1]["start"] == pytest.approx(20.0, abs=0.3)
    assert book["position"] == pytest.approx(book["chapters"][1]["start"] + 4.0, abs=0.05)
    # Inside the recut episode itself: 12 s of the uncut audio is 7 s of the cut one.
    world.books.set_position(whole, 12.0)
    world.maker.make(media_id=77, title=TITLE, sources=[one], whole=False, skip_op_ed=True)
    assert world.books.book(whole, include_transcription=False)["position"] == pytest.approx(7.0, abs=0.05)
