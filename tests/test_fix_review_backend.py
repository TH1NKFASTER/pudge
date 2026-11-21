"""External review: verified findings in the library, torrents, iPhone server and subtitle code.

Each test fails on the code before the fix. Invented data only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import types
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_fix_iphone_video_stream import _episode, _prepared, service  # noqa: F401 - fixtures
from test_iphone_audio_repository import allowed, archived, audio_manifest, checkpoint  # noqa: F401


# ---------------------------------------------------------------------- manager
def test_unknown_placeholder_rows_ask_the_worker_for_every_entrys_media() -> None:
    from pudge.manager import AnimeManager

    def broken():
        raise sqlite3.OperationalError("database is locked")

    fake = SimpleNamespace(db=SimpleNamespace(anime_list=broken))
    # None (JSON null): the worker keeps every entry's media; [] kept none of it.
    assert AnimeManager._placeholder_media_ids(fake) is None


def test_a_download_still_being_written_is_registered_later_not_completed_now(tmp_path: Path) -> None:
    from pudge.manager import AnimeManager

    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"x")
    calls: list = []
    asked: list = []

    def unfinished(path, *, roots, finished):
        asked.append((finished("A" * 40), finished("b" * 40)))
        return True  # another torrent's control file next to it

    fake = SimpleNamespace(
        _completed_download_video_files=lambda item: [video],
        _library_path_is_ignored=lambda path: False,
        _library_scan_roots=lambda: (tmp_path,),
        _finished_torrent_check=lambda: lambda info_hash: False,
        unfinished_library_video=unfinished,
        _complete_download_intent=lambda item: calls.append("completed"),
        logger=logging.getLogger("t"),
    )
    item = SimpleNamespace(torrent_hash="a" * 40, media_id=1)
    assert AnimeManager._register_completed_download(fake, item) == 0
    assert calls == []  # the intent stays: a later pass registers the file
    assert asked == [(True, False)]  # the download registered here counts as finished


def test_torrent_on_with_no_client_to_ask_keeps_the_pause_records(tmp_path, monkeypatch) -> None:
    from test_fix_packdel import HASH, FakeClient, _manager
    from test_fix_torrenton import _api

    manager = _manager(tmp_path, monkeypatch, FakeClient({}, state="stoppedUP"))
    manager.note_torrents_paused_by_pudge([HASH], reason="quiet_toggle_off")
    api = _api(manager, monkeypatch)
    assert api._resume_torrents_paused_by_pudge(lambda: iter(())) == 0
    assert manager.torrent_paused_by_pudge(HASH).startswith("quiet_toggle_off|")


def test_torrent_off_does_not_claim_a_torrent_the_user_had_stopped(tmp_path, monkeypatch) -> None:
    from pudge import web_app
    from test_fix_packdel import HASH, FakeClient, _manager, _pack
    from test_fix_torrenton import _api

    client = FakeClient({}, state="pausedUP")
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, _videos = _pack(manager)
    client.payloads[HASH] = folder

    class Quiet:
        def __init__(self):
            self.paused: list = []

        def torrents(self, *, category=""):
            return [SimpleNamespace(torrent_hash=HASH, state="pausedUP", category="", tags="")]

        def pause(self, hashes):
            self.paused.append(hashes)

        def close(self):
            pass

    quiet = Quiet()
    api = _api(manager, monkeypatch, quiet)
    web_app.WebAppApi._quiesce_qbittorrent(api, reason="toggle_off")
    assert quiet.paused == [HASH]  # stopped (again) all the same
    assert manager.torrent_paused_by_pudge(HASH) == ""  # but Torrent On will not start it


# ---------------------------------------------------------------------- final sequel cache
def test_two_warm_ups_at_once_never_start_one_thread_twice(tmp_path) -> None:
    from pudge import final_sequel_cache

    errors: list = []
    cache: list = []

    class Log:
        def info(self, text, *args):
            if cache and len(errors) < 1 and "warmup_queued" in text and args[0] == 1:
                try:
                    cache[0].warm(2)  # a second warm() right between the lock and start()
                except RuntimeError as exc:
                    errors.append(exc)

        warning = debug = exception = info

    busy = threading.Event()
    instance = final_sequel_cache.FinalSequelCache(
        lambda media_id, background=False: busy.wait(5) and {"ok": True}, logger=Log()
    )
    cache.append(instance)
    try:
        instance.warm(1)
    except RuntimeError as exc:
        errors.append(exc)
    finally:
        busy.set()
    assert errors == []


# ---------------------------------------------------------------------- iPhone finale
def test_a_score_the_mac_could_not_save_now_is_sent_again_not_dropped() -> None:
    from pudge.iphone_finale import IPhoneFinale
    from pudge.iphone_media import MediaTemporaryError

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    finale = IPhoneFinale(
        SimpleNamespace(api=SimpleNamespace(set_anilist_score=locked), _log=lambda *a: None)
    )
    with pytest.raises(MediaTemporaryError):
        finale.score(1, {"score": 8, "episode": 12})


# ---------------------------------------------------------------------- iPhone HTTP
def test_a_get_answer_is_encoded_once_for_its_etag_and_body(monkeypatch) -> None:
    from pudge import iphone_http

    counted: list = []
    real = iphone_http.encoded
    monkeypatch.setattr(iphone_http, "encoded", lambda value: counted.append(1) or real(value))
    sent: list = []
    handler = object.__new__(iphone_http.IPhoneHandler)
    handler.command = "GET"
    handler.headers = {}
    handler.send_response = lambda code: sent.append(code)
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    handler.wfile = SimpleNamespace(write=lambda data: sent.append(json.loads(data)))
    value = {"cues": [{"text": "invented", "start": 1.0}] * 50}
    body = iphone_http.encoded(value).encode()
    counted.clear()
    handler.json(200, value, etag='"x"', body=body)
    assert counted == [] and sent == [200, value]


# ---------------------------------------------------------------------- iPhone media
def test_an_episode_whose_copy_is_gone_is_not_ready_without_a_video(service, tmp_path, monkeypatch) -> None:
    episode, state = _prepared(service, tmp_path, monkeypatch, b"copy")
    Path(json.loads(_prepared_row(service))["path"]).unlink()
    answer = service.media.episode(episode)
    assert answer["state"] == "idle" and "video" not in answer


def _prepared_row(service) -> str:
    with service.db.connect() as conn:
        return conn.execute("SELECT result FROM iphone_prepared").fetchone()["result"]


def test_subtitles_are_tokenized_before_the_copy_counts_as_prepared(service, tmp_path, monkeypatch) -> None:
    episode = _episode(service, tmp_path)
    seen: list = []

    def fake_prepare(source, folder, **_kwargs):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "copy.mp4"
        target.write_bytes(b"copy")
        return {"path": target, "durationMs": 1440000}

    monkeypatch.setattr("pudge.iphone_media.prepare_video", fake_prepare)
    monkeypatch.setattr(
        service.media, "episode_subtitles", lambda _id: seen.append(service.media._prepared_video(_id))
    )
    service.media._prepare_worker(episode)
    assert seen == [None]  # the phone saw no "ready" before the subtitles were cached
    assert service.media.episode(episode)["state"] == "ready"


def test_a_copy_that_could_not_be_pruned_leaves_the_preparation_ready(service, tmp_path, monkeypatch) -> None:
    def refuse():
        raise PermissionError("external volume")

    monkeypatch.setattr(service.media, "_prune_videos", refuse)
    episode, _state = _prepared(service, tmp_path, monkeypatch, b"copy")
    assert service.media.video_jobs[service.media._job_key(episode, "original")]["state"] == "ready"


def test_a_file_deleted_between_lookup_and_stat_is_no_copy(service, tmp_path, monkeypatch) -> None:
    gone = tmp_path / "gone.mkv"
    monkeypatch.setattr(service.media, "_episode_row", lambda *a, **k: {"video_path": str(gone)})
    assert service.media._prepared_video(1) is None


def test_ffmpeg_4s_progress_lines_move_the_bar(tmp_path) -> None:
    from pudge import iphone_media

    script = tmp_path / "ffmpeg"
    script.write_text(
        f"#!{sys.executable}\nimport sys\nprint('out_time_ms=12000000', flush=True)\n"
        "open(sys.argv[-1], 'wb').write(b'x')\n"
    )
    script.chmod(0o755)
    seen: list = []
    temporary, target = tmp_path / "x.part.mp4", tmp_path / "x.mp4"
    iphone_media._run_prepare([str(script), str(temporary)], temporary, target, 24.0, seen.append)
    assert seen and abs(seen[0] - 0.5) < 0.01


def test_a_watch_union_aniList_cannot_take_is_not_retried_on_every_send(
    service, tmp_path, monkeypatch
) -> None:
    from pudge import watch_coverage

    episode = _episode(service, tmp_path)
    calls: list = []

    def unreachable(episode_id, value):
        calls.append(episode_id)
        raise OSError("AniList unreachable")

    monkeypatch.setattr(service.media, "episode_watched", unreachable)
    monkeypatch.setattr(watch_coverage, "reaches_end", lambda *a: True)
    for _send in range(3):
        service.media._add_watched_parts(episode, 1400, 1440, [[0, 1400]])
    assert calls == [episode]


# ---------------------------------------------------------------------- iPhone service
def test_one_odd_book_never_ends_the_catalog_walk(service, monkeypatch) -> None:
    with service.db.connect() as conn:
        for book_id, title in ((1, "Older Book"), (2, "Newer Book")):
            conn.execute(
                "INSERT INTO audiobooks(id,path,title,duration,position,created_at,updated_at) "
                "VALUES(?,?,?,60,0,0,0)",
                (book_id, f"/nowhere/{book_id}", title),
            )
    seen: list = []

    def series(book_id):
        seen.append(book_id)
        if book_id == 2:
            raise KeyError("originalSHA256")  # an old cached row
        return {}

    monkeypatch.setattr(service, "_series_info", series)
    result = service._refresh_catalog_now(lazy=True)
    assert seen == [2, 1]  # the walk went on
    assert [error["error"] for error in result["errors"]] == ["KeyError"]


def test_an_offline_mac_does_not_wait_for_the_same_cover_on_every_walk(service, monkeypatch) -> None:
    import httpx

    calls: list = []

    def offline(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", offline)
    assert service._remote_cover("https://covers.invalid/a.jpg") is None
    assert service._remote_cover("https://covers.invalid/a.jpg") is None
    assert calls == ["https://covers.invalid/a.jpg"]


def test_a_bookmark_sent_before_under_the_earlier_device_id_gets_a_final_receipt(service) -> None:
    from test_fix_iphone_checkpoint_device_change import _second_device

    device, manifest = archived(service)
    bookmark = {
        "bookmarkId": str(uuid.uuid4()),
        "eventId": str(uuid.uuid4()),
        "assetId": manifest["assetId"],
        "editionId": manifest["editionId"],
        "positionMs": 1000,
        "title": "One",
        "deleted": False,
        "baseRevision": 0,
    }
    first = service.record_bookmark(device, bookmark)
    new_device = _second_device(service)
    again = service.record_bookmark(new_device, bookmark)
    assert again["status"] == "rejected" and again["reason"] == "device_changed"
    assert service.record_bookmark(device, bookmark) == first


def test_resolving_a_conflict_while_a_mac_session_starts_does_not_fail(service, monkeypatch) -> None:
    device, manifest = archived(service)
    asset = manifest["assetId"]

    class Growing(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "base":
                service.mac_sessions[(asset, str(uuid.uuid4()))] = {"base": 0}  # another thread

    service.mac_sessions[(asset, "s")] = Growing(base=0)
    monkeypatch.setattr(service, "asset_for_book", lambda book_id: asset)
    monkeypatch.setattr(service, "resolve_progress", lambda *a: {"revision": 3})
    monkeypatch.setattr(service, "progress", lambda a: {"selected": {"positionMs": 5000}})
    assert service.use_phone_position(1, "c", 2) == 5.0


def test_an_empty_server_secret_left_by_a_crash_is_replaced(tmp_path) -> None:
    from pudge.database import Database
    from pudge.iphone_service import IPhoneService

    root = tmp_path / "mobile"
    root.mkdir()
    (root / "server-secret").write_bytes(b"")
    service = IPhoneService(Database(tmp_path / "db.sqlite"), root)
    assert len(service.secret) == 32


def test_a_manifest_with_one_part_twice_or_one_resource_for_two_files_is_refused() -> None:
    from pudge.iphone_service import validate_manifest

    manifest = audio_manifest()
    part = manifest["parts"][0]
    twice = dict(
        manifest,
        parts=[
            dict(part, durationMs=30000),
            dict(part, partId=part["partId"].upper(), offsetMs=30000, durationMs=30000),
        ],
    )
    with pytest.raises(ValueError, match="Duplicate part"):
        validate_manifest(twice)
    other = dict(part, partId=str(uuid.uuid4()), sha256="0" * 64, offsetMs=30000, durationMs=30000)
    shared = dict(manifest, parts=[dict(part, durationMs=30000), other])
    with pytest.raises(ValueError, match="reused"):
        validate_manifest(shared)


# ---------------------------------------------------------------------- subtitles
def _candidate(path: Path, purity: str):
    from pudge.models import SubtitleCandidate

    return SubtitleCandidate(
        path=path, name=path.name, source="jimaku", score=1.0, details={"language_purity": purity}
    )


def test_a_mixed_language_file_never_replaces_a_japanese_only_one_on_onsets(tmp_path, monkeypatch) -> None:
    from pudge import syncing
    from pudge.config import SyncConfig

    first = _candidate(tmp_path / "ja.srt", "japanese_only")
    mixed = _candidate(tmp_path / "mixed.srt", "mixed_japanese_chinese")
    same = _candidate(tmp_path / "ja2.srt", "japanese_only")
    out = tmp_path / "out.srt"
    for path in (first.path, mixed.path, same.path, out):
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
    offered: list = []
    monkeypatch.setattr(
        syncing,
        "_optimize_candidates_unguarded",
        lambda video, items, *a, **k: (items[0], out, {"sync_was_successful": True}),
    )
    monkeypatch.setattr(syncing, "_guard_alignment_result", lambda *a: None)
    monkeypatch.setattr(
        syncing, "_recover_rejected_clock", lambda video, src, path, result, *a, **k: (path, result)
    )
    monkeypatch.setattr(syncing, "subtitle_quality_accepted", lambda result: (True, ""))
    monkeypatch.setattr(
        syncing,
        "_second_candidate_check",
        lambda video, selected, path, result, others, *a, **k: (
            offered.append([c.name for c in others]) or (selected, path, result)
        ),
    )
    monkeypatch.setattr(syncing, "_apply_onset_lead", lambda video, path, *a, **k: path)
    monkeypatch.setattr(syncing, "_media_timeline", lambda *a: None)
    syncing.optimize_candidates(tmp_path / "v.mkv", [first, mixed, same], tmp_path, SyncConfig())
    assert offered == [["ja2.srt"]]


def test_a_failing_second_opinion_is_kept_and_logged(tmp_path, monkeypatch) -> None:
    from pudge import syncing
    from pudge.config import SyncConfig
    from pudge.subtitles import candidate_choice

    first = _candidate(tmp_path / "ja.srt", "japanese_only")
    other = _candidate(tmp_path / "ja2.srt", "japanese_only")

    def broken(*args, **kwargs):
        raise TypeError("unexpected keyword")

    monkeypatch.setattr(syncing, "_episode_speech_onsets", lambda *a, **k: [1.0])
    monkeypatch.setattr(
        syncing, "_output_onset_signal", lambda *a: {"available": True, "matched_fraction": 0.5}
    )
    monkeypatch.setattr(candidate_choice, "needs_second", lambda signal: (True, "low"))
    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded", broken)
    result: dict = {}
    syncing._second_candidate_check(
        tmp_path / "v.mkv", first, tmp_path / "o.srt", result, [other], tmp_path, SyncConfig()
    )
    assert "unexpected keyword" in result["candidate_onset_selection"]["second_error"]


def test_the_onset_lead_is_measured_once_per_result(tmp_path, monkeypatch) -> None:
    from pudge import syncing
    from pudge.config import SyncConfig

    calls: list = []
    monkeypatch.setattr(syncing, "_correct_onset_lead", lambda *a, **k: calls.append(1) or a[1])
    result = {"onset_lead_correction": {"applied": True, "shift_seconds": -0.15}}
    syncing._apply_onset_lead(tmp_path / "v.mkv", tmp_path / "o.srt", result, tmp_path, SyncConfig())
    assert calls == []


def test_a_single_file_given_as_text_paths_gets_the_onset_correction(tmp_path, monkeypatch) -> None:
    from pudge.subtitles import pipeline

    seen: list = []
    monkeypatch.setattr(pipeline, "_optimize_subtitle", lambda *a, **k: (tmp_path / "o.srt", {}))
    monkeypatch.setattr(
        pipeline,
        "_finalize_single_subtitle",
        lambda video, subtitle, path, *a, **k: seen.append(type(video)) or path,
    )
    pipeline.SubtitleAlignmentPipeline().optimize_subtitle(
        str(tmp_path / "v.mkv"), str(tmp_path / "s.srt"), str(tmp_path), None
    )
    assert seen == [type(tmp_path)]


def test_a_stray_cue_hours_away_does_not_build_a_huge_bit_set() -> None:
    from pudge.subtitles import timeline_alignment

    bins = set(range(0, 4000)) | {720_000 * 10}  # one cue at ~1000 h
    assert timeline_alignment._bit_set(bins) is None
    window = list(range(100, 140))
    assert timeline_alignment._shifted_overlap(window, bins, 0) == 40


def test_the_alignment_memo_is_read_once(tmp_path, monkeypatch) -> None:
    from pudge.subtitles import timeline_alignment

    class Racing(dict):
        def __contains__(self, key):  # a clear() by another alignment right after "in"
            return True

    monkeypatch.setattr(timeline_alignment, "_ALIGNMENT_MEMO", Racing())
    monkeypatch.setattr(
        timeline_alignment, "_align_subtitle_timelines", lambda *a, **k: (Path(a[0]), {"ok": 1})
    )
    source, reference = tmp_path / "a.srt", tmp_path / "b.srt"
    source.write_text("x"), reference.write_text("y")
    assert timeline_alignment.align_subtitle_timelines(source, reference, tmp_path)[1] == {"ok": 1}


@pytest.mark.parametrize("name", ["Kaiju-No-8-JPN.ass", "Danganronpa-3-JPN.srt"])
def test_a_titles_own_single_digit_glued_with_dashes_is_no_episode(name) -> None:
    from pudge.filename import parse_anime_filename

    assert parse_anime_filename(name).episode is None


def test_every_titles_plain_episode_query_comes_before_the_scene_forms() -> None:
    from pudge.manager_models import LibraryAnime
    from pudge.providers import nyaa

    anime = LibraryAnime(
        media_id=1,
        title="Kono Subarashii Sekai ni Shukufuku wo",
        titles=("KonoSuba: God's Blessing on This Wonderful World",),
        synonyms=("Konosuba", "KonoSuba Blessing", "Subarashii Sekai"),
        status="CURRENT",
        progress=5,
    )
    plan = nyaa.release_search_queries(anime, episode=6, batch=False, max_queries=5)
    plain = [query for query in plan if query.endswith(" 06") and "S01E" not in query]
    assert any(query.startswith("Subarashii Sekai") for query in plain), plan


# ---------------------------------------------------------------------- intro skipper
def test_a_failed_recompute_keeps_the_segments_found_before(tmp_path, monkeypatch) -> None:
    from pudge import intro_skipper

    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"x" * 64)
    cache = intro_skipper.IntroSkipperCache(tmp_path)
    identity = intro_skipper.identity_of(video)
    old = next(iter(intro_skipper.PLAYBACK_FALLBACK_VERSIONS))
    segments = [{"kind": "intro", "start_seconds": 60.0, "end_seconds": 150.0, "confidence": 0.9}]
    cache.write_result(identity, {"algorithm": old, "status": "found", "segments": segments})

    def busy(*args, **kwargs):
        raise intro_skipper.FingerprintError("file busy")

    monkeypatch.setattr(intro_skipper, "_fingerprints_for", busy)
    intro_skipper._analyze_title(
        [intro_skipper.EpisodeFile(path=video, episode=1, identity=identity)],
        report=intro_skipper.AnalysisReport(),
        cache=cache,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        settings=intro_skipper.DetectorSettings(),
        should_stop=lambda: None,
        max_new_fingerprints=1,
        log=logging.getLogger("t"),
        stamp=1000.0,
        library=None,
    )
    assert cache.read_result(identity)["status"] == "error"  # retried after its back-off
    found = intro_skipper.segments_for_video(tmp_path, video)
    assert [(s.kind, s.start_seconds) for s in found] == [("intro", 60.0)]


# ---------------------------------------------------------------------- aria2 (second batch)
def test_aria2_puts_the_nearest_back_in_front_after_the_farther_one_gave_its_slot_up() -> None:
    from test_fix_ios3_download_priority import _aria

    client, rpc = _aria({"near": "waiting", "far": "active"})
    assert client.prioritize_queue(["near", "far"]) is True
    # aria2 puts a paused active download at the front of the waiting queue.
    after = rpc[[method for method, _ in rpc].index("aria2.unpause") + 1 :]
    assert after == [("aria2.changePosition", ["near", 0, "POS_SET"])]


def test_a_hung_vpn_command_fails_the_add_like_a_refused_one(monkeypatch) -> None:
    import subprocess

    from pudge.providers.aria2 import Aria2Client, Aria2Error

    def hung(*args, **kwargs):
        raise subprocess.TimeoutExpired("vpn-up", 90)

    monkeypatch.setattr(subprocess, "run", hung)
    client = object.__new__(Aria2Client)
    client.pre_download_command = "vpn-up"
    with pytest.raises(Aria2Error):
        client._run_hook()


# ---------------------------------------------------------------------- intro skipper (second batch)
from test_fix_intro_detection_starvation import background, media  # noqa: E402,F401 - fixtures


def test_one_title_that_raises_does_not_stop_the_pass_for_the_others(media, background, monkeypatch) -> None:
    from pudge import intro_skipper
    from test_fix_intro_detection_starvation import BACCANO, EDGERUNNERS, _baccano, _db, _edgerunners, _pass

    baccano, edgerunners = _baccano(media), _edgerunners(media)
    broken = {item.path for item in baccano}
    real = intro_skipper.analyze_title

    def analyze(files, **kwargs):
        if {item.path for item in files} & broken:
            raise ValueError("damaged library.json")
        return real(files, **kwargs)

    monkeypatch.setattr(intro_skipper, "analyze_title", analyze)
    first = _pass(media, _db({BACCANO: baccano, EDGERUNNERS: edgerunners}))
    assert first["media_id"] == EDGERUNNERS and first["found"] == 2, first
