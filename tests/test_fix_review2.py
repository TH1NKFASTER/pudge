"""External review, round 2 (and round-1 items deferred then): verified findings, each failing
on the code before its fix. Invented data only."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem


def _download(**fields) -> DownloadItem:
    values = dict(
        torrent_hash="a" * 40,
        name="[Group] Show (01-12)",
        state="downloading",
        progress=0.3,
        save_path="",
        content_path="",
        media_id=7,
        episode=None,
        is_batch=True,
    )
    values.update(fields)
    return DownloadItem(
        **{key: value for key, value in values.items() if key in DownloadItem.__dataclass_fields__}
    )


# ---------------------------------------------------------------------- manager
def test_a_season_pack_without_its_own_folder_does_not_hide_the_titles_folder(tmp_path) -> None:
    root = (tmp_path / "Library").resolve()
    title = root / "Show"
    title.mkdir(parents=True)
    flat = _download(save_path=str(title), content_path=str(title))
    folder = _download(
        torrent_hash="b" * 40, save_path=str(title), content_path=str(title / "[Group] Show S2")
    )
    fake = SimpleNamespace(
        db=SimpleNamespace(downloads=lambda: [flat, folder]),
        _download_is_complete=lambda item: False,
        _download_content_path=AnimeManager._download_content_path,
        _library_scan_roots=lambda: (root,),
    )
    paths = AnimeManager.incomplete_download_paths(fake)
    # The flat pack's "content" is the title folder every other episode is in: never excluded.
    assert paths == (title / "[Group] Show S2",)


def test_two_season_packs_of_one_entry_are_not_duplicates() -> None:
    first = _download(name="[Group] Show (01-12) [1080p]")
    second = _download(torrent_hash="b" * 40, name="[Group] Show (13-24) [1080p]")
    fake = SimpleNamespace(
        _download_is_managed=lambda item: True,
        _torrent_tags=lambda item: set(),
        _normalized_title=lambda title: title,
        db=SimpleNamespace(get_anime=lambda media_id: SimpleNamespace(format="TV", title="Show")),
    )
    one = AnimeManager._duplicate_identity(fake, first)
    two = AnimeManager._duplicate_identity(fake, second)
    assert one != two and one[1] == "batch:1-12" and two[1] == "batch:13-24"


def test_a_stall_replacement_that_started_nothing_leaves_the_request_waiting() -> None:
    store = {"state": "downloading", "selected_hash": "a" * 40, "backend": "aria2"}
    updates: list = []

    class Intents:
        def get(self, media_id, episode, batch):
            return dict(store)

        def update(self, media_id, episode, batch, **fields):
            updates.append(fields)
            store.update(fields)

    fake = SimpleNamespace(download_intents=Intents(), logger=logging.getLogger("t"))
    AnimeManager._stall_replace_left_nothing(fake, 7, None, True, "a" * 40, "no_candidate_started")
    assert store["state"] == "waiting" and "no replacement" in updates[0]["detail"]
    # A replacement that took over is left alone.
    store.update(state="downloading", selected_hash="c" * 40)
    updates.clear()
    AnimeManager._stall_replace_left_nothing(fake, 7, None, True, "a" * 40, "x")
    assert updates == []


def test_a_race_for_a_title_removed_meanwhile_fails_like_a_manager_error(tmp_path) -> None:
    from pudge.manager import ManagerError

    fake = SimpleNamespace(db=SimpleNamespace(get_anime=lambda media_id: None))
    with pytest.raises(ManagerError):
        AnimeManager._add_journaled_single_candidate.__wrapped__(
            fake,
            7,
            SimpleNamespace(info_hash="a" * 40),
            episode=1,
            batch=False,
            backend="aria2",
            intent_revision=1,
        )


# ---------------------------------------------------------------------- web app
def test_cancelling_a_generation_never_deletes_a_folder_pudge_did_not_make(tmp_path, monkeypatch) -> None:
    from pudge import web_app

    api = object.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(library=SimpleNamespace(root_dir=tmp_path / "library"))
    api.logger = logging.getLogger("t")
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "a.txt").write_text("x")
    monkeypatch.chdir(keep)
    assert api._remove_generation_output(Path("")) is False  # a manifest without output_dir
    assert api._remove_generation_output(keep) is False
    assert (keep / "a.txt").exists()
    made = tmp_path / "library" / "Generated Audiobooks" / "Book"
    made.mkdir(parents=True)
    (made / ".pudge-audiobook-generation.json").write_text("{}")
    assert api._remove_generation_output(made) is True and not made.exists()


def test_the_one_screen_recording_request_is_kept_when_macos_was_not_asked(tmp_path, monkeypatch) -> None:
    from pudge import web_app

    api = object.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path))
    api.logger = logging.getLogger("t")
    api.visual_novels = SimpleNamespace(_screen_recording_access=lambda: False)
    api.open_screen_recording_settings = lambda: None
    monkeypatch.setitem(sys.modules, "Quartz", ModuleType("Quartz"))  # no CGRequestScreenCaptureAccess
    monkeypatch.setattr(web_app, "_quartz_function", lambda module, name: None)
    api.screen_recording_request_access()
    assert not (tmp_path / "screen-recording-requested").exists()


def test_torrent_off_is_confirmed_when_qbittorrent_on_this_mac_is_not_running(monkeypatch) -> None:
    import httpx

    from pudge import web_app

    class Closed:
        unreachable = True

        def __init__(self, *args, **kwargs):
            pass

        def torrents(self, category=""):
            raise httpx.ConnectError("connection refused")

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", Closed)
    api = object.__new__(web_app.WebAppApi)
    api.logger = logging.getLogger("t")
    api.manager = SimpleNamespace(db=SimpleNamespace(downloads=lambda: []))
    api.config = SimpleNamespace(
        qbittorrent=SimpleNamespace(
            enabled=True,
            base_url="http://127.0.0.1:8080",
            username="",
            password="",
            api_key="",
            verify_tls=True,
            category="anime",
        )
    )
    assert api._observe_qbittorrent_quiet() == (True, 0)
    api.config.qbittorrent.base_url = "http://nas.local:8080"  # another machine: still unknown
    assert api._observe_qbittorrent_quiet() == (False, None)


# ---------------------------------------------------------------------- audiobooks
@pytest.mark.real_process_scan
def test_an_orphaned_mpv_with_the_current_socket_name_is_ended(tmp_path, monkeypatch) -> None:
    from pudge import audiobooks

    ipc = (tmp_path / "audiobook-ipc").resolve()
    ipc.mkdir()
    gone_owner = 999_991
    socket_path = ipc / f"book-3-{gone_owner}-{time.monotonic_ns():x}-{12345:x}.sock"
    line = f"{os.getuid()} 4242 1 mpv --input-ipc-server={socket_path} /books/x.m4b"
    monkeypatch.setattr(
        audiobooks.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=line + "\n")
    )
    killed: list = []

    def kill(pid, sig):
        if pid == gone_owner:
            raise ProcessLookupError
        if sig == 0:
            raise ProcessLookupError  # ended at once
        killed.append(pid)

    monkeypatch.setattr(audiobooks.os, "kill", kill)
    assert audiobooks.terminate_orphaned_audiobook_players(tmp_path, grace_seconds=0) == [4242]


def test_a_missing_alignment_is_looked_for_once_until_the_folder_changes(tmp_path) -> None:
    from pudge.audiobooks import AudiobookService

    service = object.__new__(AudiobookService)
    service.cache_dir = tmp_path
    root = tmp_path / "reading-audio-alignment"
    root.mkdir()
    scans: list = []
    service._scan_compatible_alignment = lambda *a: scans.append(1)
    destination = root / "abc.json"
    for _poll in range(5):
        assert service._migrate_compatible_alignment(1, 2, destination) is None
    assert scans == [1]
    time.sleep(0.01)
    (root / "other.json").write_text("{}")  # a new alignment arrived: looked at again
    service._migrate_compatible_alignment(1, 2, destination)
    assert scans == [1, 1]


# ---------------------------------------------------------------------- light novels
def test_a_short_jiten_batch_answer_is_not_kept_and_is_asked_again(tmp_path, monkeypatch) -> None:
    from pudge.light_novels import LightNovelError, LightNovelService
    from tests.test_v0661_light_novels import cfg

    service = LightNovelService(cfg(tmp_path))
    service.save_settings({"jiten_api_key": "token"})
    monkeypatch.setattr(service, "_reserve_parse_request_start", lambda: None)
    paragraphs = ["あ" * 9000, "い" * 9000]  # two batches (> 14000 characters together)
    answers = {"short": True, "calls": 0}

    def fake(action, payload=None):
        answers["calls"] += 1
        texts = list(payload["text"])
        if answers["short"] and texts[0].startswith("あ"):
            texts = texts[:-1] if len(texts) > 1 else []
        tokens = [[{"wordId": 1, "readingIndex": 0, "start": 0, "end": 1, "length": 1}] for _ in texts]
        return {"tokens": tokens, "vocabulary": [], "parseVersion": 3}

    monkeypatch.setattr(service, "_jiten_request", fake)
    text = "\n".join(paragraphs)
    with pytest.raises(LightNovelError):
        service._parse_text_jiten(text, "hash-1", force=True)
    answers["short"] = False
    result = service._parse_text_jiten(text, "hash-1", force=True)
    assert result  # the bad answer was not reused from the cache


def test_jiten_overview_without_a_version_is_no_version(tmp_path, monkeypatch) -> None:
    from pudge.light_novels import LightNovelService
    from tests.test_v0661_light_novels import cfg

    service = LightNovelService(cfg(tmp_path))
    service.save_settings({"jiten_api_key": "token"})
    from pudge import jiten_client

    monkeypatch.setattr(
        jiten_client,
        "request",
        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {"other": 1}, content=b"{}"),
    )
    assert service.check_jiten_overview_version("token", "account") is None
    extras = service._live_state_extras()
    assert "None" not in (extras.get("versions") or {}).values()


# ---------------------------------------------------------------------- providers
def test_a_missing_external_volume_is_not_created_by_a_scan(tmp_path) -> None:
    from pudge.database import Database
    from pudge.library import scan_library

    root = Path("/Volumes/PudgeReviewNoSuchDisk/Anime")
    try:
        assert scan_library(root, Database(tmp_path / "db.sqlite")) == []
        assert not Path("/Volumes/PudgeReviewNoSuchDisk").exists()
    finally:
        if Path("/Volumes/PudgeReviewNoSuchDisk").exists():
            import shutil

            shutil.rmtree("/Volumes/PudgeReviewNoSuchDisk")


def test_a_hollow_aria2_payload_is_not_a_finished_download(tmp_path) -> None:
    from pudge.providers.aria2 import Aria2Client
    from test_fix_batch_finished_while_aria2_stopped import _bencode

    import hashlib

    name = "[Group] Show - 01 [1080p].mkv"
    length = 64 * 1024 * 1024
    info = {"name": name, "piece length": 16384, "pieces": b"\0" * 20, "length": length}
    info_hash = hashlib.sha1(_bencode(info)).hexdigest()
    (tmp_path / f"{info_hash}.torrent").write_bytes(_bencode({"info": info}))
    video = tmp_path / name
    with open(video, "wb") as handle:  # only the tail pieces are there: the rest is a hole
        handle.truncate(length)
        handle.seek(length - 4096)
        handle.write(b"t" * 4096)
    if os.stat(video).st_blocks * 512 >= length // 2:
        pytest.skip("this file system does not keep sparse files")
    assert Aria2Client._completed_payload_on_disk(tmp_path, info_hash) is None
    with open(video, "r+b") as handle:  # downloaded completely
        handle.write(b"v" * (length - 4096))
    assert Aria2Client._completed_payload_on_disk(tmp_path, info_hash) is not None


def test_anilists_404_is_the_gone_error_callers_handle() -> None:
    from pudge.providers.anilist import AniListClient, AniListHTTPError, AniListNotFoundError

    client = object.__new__(AniListClient)
    client._token_hash = ""
    client.endpoint = "https://graphql.anilist.co"
    response = SimpleNamespace(
        status_code=404,
        text='{"errors":[{"message":"Not Found."}]}',
        headers={},
        json=lambda: {"errors": [{"message": "Not Found."}], "data": {"Media": None}},
    )
    error = client._http_failure(response)
    assert isinstance(error, AniListNotFoundError) and isinstance(error, AniListHTTPError)
    assert error.status_code == 404


def test_jimaku_files_without_a_size_or_name_do_not_fail_the_list(monkeypatch) -> None:
    from pudge.providers.jimaku import JimakuClient

    client = object.__new__(JimakuClient)
    monkeypatch.setattr(
        client,
        "_get_json",
        lambda path, params=None: (
            [{"url": "https://jimaku.invalid/f", "name": "x.srt", "size": None}]
            if path.endswith("/files")
            else [{"id": 1, "name": None}]
        ),
        raising=False,
    )
    assert client.files(1, None)[0].size == 0
    assert client.search_entries(query="x")[0].name == ""


# ---------------------------------------------------------------------- subtitles (round 1: mod-syncing-1)
def _fake_ffsubsync(monkeypatch, seen: list) -> None:
    module = ModuleType("ffsubsync.ffsubsync")

    class Parser:
        def parse_args(self, arguments):
            return arguments

    def run(arguments):
        target = Path(arguments[arguments.index("-o") + 1])
        seen.append(target)
        target.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
        return {"sync_was_successful": True, "offset_seconds": 0.5}

    module.make_parser = lambda: Parser()
    module.run = run
    module.logger = logging.getLogger("fake-ffsubsync")
    package = ModuleType("ffsubsync")
    package.ffsubsync = module
    monkeypatch.setitem(sys.modules, "ffsubsync", package)
    monkeypatch.setitem(sys.modules, "ffsubsync.ffsubsync", module)


def test_ffsubsync_output_is_published_complete_and_keyed_on_its_reference(tmp_path, monkeypatch) -> None:
    from pudge import syncing
    from pudge.config import SyncConfig

    seen: list = []
    _fake_ffsubsync(monkeypatch, seen)
    monkeypatch.setattr(syncing, "_media_timeline", lambda *a: None)
    monkeypatch.setattr(syncing, "_ffsubsync_audio_stream", lambda *a: None)
    video, subtitle = tmp_path / "v.mkv", tmp_path / "s.srt"
    video.write_bytes(b"v")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
    first_ref, second_ref = tmp_path / "a.srt", tmp_path / "b.srt"
    first_ref.write_text("a"), second_ref.write_text("bb")
    config = SyncConfig()
    one, result = syncing.synchronize_subtitle(
        video, subtitle, tmp_path / "cache", config, reference=first_ref
    )
    # ffsubsync wrote a temporary sibling; the result appeared under its name with its metadata.
    assert seen[0] != one and not seen[0].exists() and one.exists()
    assert json.loads(syncing._metadata_path(one).read_text())["offset_seconds"] == 0.5
    two, _ = syncing.synchronize_subtitle(video, subtitle, tmp_path / "cache", config, reference=second_ref)
    assert two != one  # another reference is another result, not the cached one


def test_alass_reads_a_relative_video_path(tmp_path, monkeypatch) -> None:
    from pudge import syncing
    from pudge.config import SyncConfig

    alass = tmp_path / "alass"
    alass.write_text(
        f"#!{sys.executable}\nimport os, shutil, sys\n"
        "ref, src, out = sys.argv[1:4]\n"
        "sys.exit(3) if not os.path.exists(ref) else shutil.copy(src, out)\n"
    )
    alass.chmod(0o755)
    monkeypatch.setattr(syncing, "_media_timeline", lambda *a: None)
    work = tmp_path / "work"
    work.mkdir()
    (work / "ep.mkv").write_bytes(b"video")
    subtitle = work / "s.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
    monkeypatch.chdir(work)
    path, result = syncing.synchronize_with_alass(
        Path("ep.mkv"), subtitle, tmp_path / "cache", SyncConfig(), alass_path=str(alass)
    )
    assert result["reason"] == "applied", result
    assert path.exists() and json.loads(syncing._metadata_path(path).read_text())["reason"] == "applied"
