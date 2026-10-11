"""Audit decisions, patch D2: performance and energy of background work (behaviour; audit 71Б
rewrite)."""

from __future__ import annotations

import http.client
import http.server
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.database import Database
from t71g03_support import MANGA, READER

ROOT = Path(__file__).resolve().parents[1]


def test_subtitle_traces_are_kept_for_the_last_20_videos(tmp_path):
    from pudge.debug_snapshot import prune_subtitle_debug

    root = tmp_path / "subtitle-debug"
    timeline = tmp_path / "subtitle-timeline-debug"
    root.mkdir()
    timeline.mkdir()
    now = time.time()
    for number in range(25):
        digest = f"{number:040x}"
        for path in (
            root / f"{digest}.json",
            root / f"{digest}-g1-owner.jsonl",
            timeline / f"{digest}.jsonl",
        ):
            path.write_text("{}")
            os.utime(path, (now - 1000 + number, now - 1000 + number))
    (root / "notes.txt").write_text("kept")
    removed = prune_subtitle_debug(tmp_path)
    assert removed == 5 * 3
    kept = {path.name[:40] for path in root.glob("*.json")}
    assert kept == {f"{number:040x}" for number in range(5, 25)}
    assert len(list(timeline.iterdir())) == 20
    assert (root / "notes.txt").exists()


def _subtitle_job(tmp_path: Path, monkeypatch):
    """A manager with one queued episode whose preparation (a separate process) reports ready."""
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)
    monkeypatch.setattr(manager.work_scheduler, "background_wait_reason", lambda *a, **k: None)
    video, subtitle = tmp_path / "Show - 01.mkv", tmp_path / "Show - 01.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=5, title="Show", episode=1, video_path=video.resolve(), state="waiting_subtitles"
        )
    )
    environments: list[dict] = []

    class Preparation:
        def __init__(self, command, **kwargs):
            environments.append(dict(kwargs.get("env") or {}))
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, *args, **kwargs):
            return f"PREPARED_SUBTITLE={subtitle}\nPREPARE_STATUS=ready\n", ""

    class Other(Preparation):
        def __init__(self, command, **kwargs):  # not recorded, never run
            self.returncode = 0

        def communicate(self, *args, **kwargs):
            return "", ""

    def popen(command, **kwargs):
        # Only the subtitle preparation counts; on a Mac other helpers start too (ps, ...).
        return Preparation(command, **kwargs) if "--prepare-only" in command else Other(command, **kwargs)

    monkeypatch.setattr("pudge.manager.subprocess.Popen", popen)
    return manager, video.resolve(), environments


def test_a_finished_subtitle_job_prunes_old_traces_and_queue_work_runs_at_low_priority(tmp_path, monkeypatch):
    manager, video, environments = _subtitle_job(tmp_path, monkeypatch)
    root = tmp_path / "cache" / "subtitle-debug"
    root.mkdir(parents=True)
    now = time.time()
    for number in range(25):  # traces of 25 earlier videos
        trace = root / f"{number:040x}.json"
        trace.write_text("{}")
        os.utime(trace, (now - 1000 + number, now - 1000 + number))

    manager.db.queue_subtitle_job(video, 5, 1)
    manager.process_subtitle_jobs(limit=1)  # queue work nobody waits for
    manager.db.queue_subtitle_job(video, 5, 1)
    manager.process_subtitle_jobs(
        limit=1, preferred_paths=[video], wait_for_slot=True
    )  # the user asked for it

    assert [env.get("PUDGE_BACKGROUND_PRIORITY") for env in environments] == ["1", None]
    kept = {path.name[:40] for path in root.glob("*.json")}
    # The newest 20 videos keep their traces: this episode and the 19 most recent others.
    assert len(kept) == 20 and {f"{number:040x}" for number in range(6, 25)} <= kept


def test_background_workers_lower_themselves_to_nice_15():
    code = (
        "import os; os.environ['PUDGE_BACKGROUND_PRIORITY']='1';"
        "from pudge.process_priority import lower_if_requested;"
        "print(lower_if_requested(), os.getpriority(os.PRIO_PROCESS, 0))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT
    )
    assert result.stdout.split() == ["True", "15"], result.stderr
    from pudge.process_priority import lower_if_requested

    assert lower_if_requested({}) is False  # work the user waits for stays at normal priority


def test_worker_entry_points_lower_themselves_when_started_for_background_work(tmp_path, monkeypatch):
    from pudge import cli, process_priority

    lowered: list[int] = []
    monkeypatch.setattr(
        process_priority, "lower_current_process", lambda nice=15: lowered.append(nice) or True
    )
    runs = {
        "cli": lambda: cli.main(["--init-config", "--config", str(tmp_path / "config.toml")]),
    }
    for environment, expected in (({}, []), ({"PUDGE_BACKGROUND_PRIORITY": "1"}, [15])):
        for name, run in runs.items():
            lowered.clear()
            monkeypatch.delenv("PUDGE_BACKGROUND_PRIORITY", raising=False)
            for key, value in environment.items():
                monkeypatch.setenv(key, value)
            run()
            assert lowered == expected, name
    # The speech worker and audiobook transcription: tests/test_patch_d3_structural.py.


def test_background_mokuro_worker_runs_at_nice_15(tmp_path, monkeypatch):
    from pudge import manga_mokuro_session
    from pudge.manga import MangaService

    monkeypatch.delenv("PUDGE_BACKGROUND_PRIORITY", raising=False)
    service = MangaService(
        Database(tmp_path / "db.sqlite3"),
        cache_dir=tmp_path / "cache",
        mokuro_python=sys.executable,
    )
    own = os.getpriority(os.PRIO_PROCESS, 0)
    try:
        nice = {}
        for background in (False, True):
            session = service._mokuro_session(background=background)
            # Mokuro's own interpreter cannot import pudge: its worker lowers itself.
            assert session.request({"op": "ping"}, timeout=30)["ok"] is True
            nice[background] = os.getpriority(os.PRIO_PROCESS, session._process.pid)
        assert nice == {False: own, True: max(own, 15)}
    finally:
        manga_mokuro_session.close_all()


def test_aria2_writes_less_and_talks_to_fewer_peers(tmp_path):
    from pudge.providers.aria2 import Aria2Client

    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    try:
        options = client._launch_options()
        assert "--file-allocation=none" in options and "--bt-max-peers=30" in options
    finally:
        client.close()


def test_aria2_idle_means_nothing_active_or_queued(tmp_path, monkeypatch):
    from pudge.providers.aria2 import Aria2Client

    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    replies = {
        "aria2.getGlobalStat": {"numActive": "0", "numWaiting": "1"},
        "aria2.tellWaiting": [{"gid": "a", "status": "paused"}],
    }
    monkeypatch.setattr(client, "_probe", lambda: True)
    monkeypatch.setattr(client, "_rpc_raw", lambda method, params=None: replies.get(method))
    try:
        assert client.is_idle() is True  # paused items survive in the session file
        replies["aria2.tellWaiting"] = [{"gid": "a", "status": "waiting"}]
        assert client.is_idle() is False
        replies["aria2.getGlobalStat"] = {"numActive": "1", "numWaiting": "0"}
        assert client.is_idle() is False
        replies["aria2.getGlobalStat"] = {"numActive": "0", "numWaiting": "0"}
        monkeypatch.setattr(client, "shutdown", lambda save_session=True: True)
        assert client.stop_when_idle() is True
        assert (tmp_path / "idle-stopped").is_file()
        monkeypatch.setattr(client, "_probe", lambda: False)
        assert client.idle_stopped() is True
    finally:
        client.close()


def test_starting_aria2_again_forgets_that_it_was_stopped_for_idleness(tmp_path, monkeypatch):
    from pudge.providers import aria2
    from pudge.providers.aria2 import Aria2Client

    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    (tmp_path / "idle-stopped").write_text("idle")
    running = {"now": False}
    monkeypatch.setattr(client, "_probe", lambda: running["now"])
    monkeypatch.setattr(client, "_binary_path", lambda: "/usr/bin/aria2c")
    monkeypatch.setattr(client, "_apply_runtime_options", lambda: None)
    monkeypatch.setattr(
        aria2.subprocess,
        "Popen",
        lambda *a, **k: running.update(now=True) or SimpleNamespace(poll=lambda: None),
    )
    monkeypatch.setattr(aria2.time, "sleep", lambda _s: None)
    try:
        assert client.idle_stopped() is True
        client._start()
        running["now"] = False  # later it is not running for another reason (crash, quit)
        assert client.idle_stopped() is False
    finally:
        client.close()


def test_manager_stops_aria2_after_ten_idle_minutes(monkeypatch):
    from pudge import manager as manager_module
    from pudge.manager import AnimeManager

    manager = AnimeManager.__new__(AnimeManager)
    manager.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    clock = [1000.0]
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: clock[0])
    stops = []
    client = SimpleNamespace(is_idle=lambda: True, stop_when_idle=lambda: stops.append(1) or True)
    assert manager._stop_aria2_when_idle(client) is False  # first idle observation
    clock[0] += 599
    assert manager._stop_aria2_when_idle(client) is False
    clock[0] += 2
    assert manager._stop_aria2_when_idle(client) is True and stops == [1]
    busy = SimpleNamespace(is_idle=lambda: False, stop_when_idle=lambda: stops.append(2) or True)
    assert manager._stop_aria2_when_idle(busy) is False and manager._aria2_idle_since is None
    assert AnimeManager._aria2_idle_stopped(SimpleNamespace(idle_stopped=lambda: True)) is True


def _cbz(tmp_path: Path) -> Path:
    archive = tmp_path / "volume.cbz"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, size in (("1.png", (40, 60)), ("2.png", (90, 60))):
            image_path = tmp_path / name
            Image.new("RGB", size, "red").save(image_path)
            bundle.write(image_path, name)
    return archive


def test_manga_pages_and_covers_go_by_url_with_a_revision(tmp_path):
    from pudge.manga import MangaService

    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    book = service.import_file(_cbz(tmp_path))
    assert service.page(book["id"], 0)["data_uri"].startswith("data:image/png;base64,")
    service.asset_base = "http://127.0.0.1:9"
    page = service.page(book["id"], 1)
    assert "data_uri" not in page and page["spread"] is True
    prefix = f"http://127.0.0.1:9/api/manga/page/{book['id']}/1/"
    assert page["url"].startswith(prefix)
    revision = page["url"].removeprefix(prefix)
    body, content_type = service.page_asset(book["id"], 1, expected_revision=revision)
    assert body.startswith(b"\x89PNG") and content_type == "image/png"
    with pytest.raises(KeyError):
        service.page_asset(book["id"], 1, expected_revision="0" * 20)
    payload = service._payload(service._book(book["id"]))
    assert payload["cover_url"].startswith("http://127.0.0.1:9/api/manga/cover/")
    assert payload["local_cover_url"] == payload["cover_url"]
    name = payload["cover_url"].rsplit("/", 1)[1]
    assert service.cover_thumbnail_asset(name).is_file()
    with pytest.raises(KeyError):
        service.cover_thumbnail_asset("../db.sqlite3")


def test_reader_shows_pages_from_their_url_and_falls_back_to_inline_data(page):
    result = page.run(
        READER
        + r"""
        __api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 6, name: `p${index}.png`, spread: false,
          ...(index === 0 ? {url: `http://127.0.0.1:9/api/manga/page/${id}/0/rev0`} : {data_uri: 'data:image/png;base64,AAAA'})});
        await open(3);
        const first = pages()[0].src;
        await __key('ArrowLeft', {}, 'body'); await __tick(50);
        return {first, second: pages()[0].src};
        """,
        before=MANGA,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    assert result.value == {
        "first": "http://127.0.0.1:9/api/manga/page/3/0/rev0",
        "second": "data:image/png;base64,AAAA",
    }


def test_asset_server_serves_pages_only_to_its_own_origin(tmp_path):
    from pudge.manga import MangaService
    from pudge.web_app import _asset_handler_for_api

    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    book = service.import_file(_cbz(tmp_path))
    api = SimpleNamespace(
        manga=service, logger=SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    )
    web_root = tmp_path / "web"
    web_root.mkdir()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _asset_handler_for_api(api, web_root))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    service.asset_base = f"http://127.0.0.1:{port}"
    try:
        path = service.page(book["id"], 0)["url"].split(str(port), 1)[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 200 and response.read().startswith(b"\x89PNG")
        assert "immutable" in response.getheader("Cache-Control")
        connection.request("GET", path, headers={"Host": "evil.example"})
        response = connection.getresponse()
        response.read()
        assert response.status == 403
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def _srt(path: Path, cues: list[tuple[str, str, str]]) -> Path:
    path.write_text(
        "\n".join(
            f"{number}\n{start} --> {end}\n{text}\n" for number, (start, end, text) in enumerate(cues, 1)
        ),
        encoding="utf-8",
    )
    return path


def test_same_cues_in_two_files_are_recognized(tmp_path):
    from pudge.syncing import _candidate_cue_set, _same_cue_set

    first = _srt(
        tmp_path / "a.srt",
        [("00:00:01,234", "00:00:02,000", "こんにちは"), ("00:00:03,000", "00:00:04,500", "元気？")],
    )
    rounded = _srt(
        tmp_path / "b.srt",
        [("00:00:01,230", "00:00:02,000", "こんにちは"), ("00:00:03,000", "00:00:04,500", "元気？")],
    )
    retimed = _srt(
        tmp_path / "c.srt",
        [("00:00:02,234", "00:00:03,000", "こんにちは"), ("00:00:04,000", "00:00:05,500", "元気？")],
    )
    other = _srt(
        tmp_path / "d.srt",
        [("00:00:01,234", "00:00:02,000", "さようなら"), ("00:00:03,000", "00:00:04,500", "元気？")],
    )
    assert _same_cue_set(_candidate_cue_set(first), _candidate_cue_set(rounded))
    assert not _same_cue_set(_candidate_cue_set(first), _candidate_cue_set(retimed))
    assert not _same_cue_set(_candidate_cue_set(first), _candidate_cue_set(other))


def _candidate(path: Path, score: float = 80.0):
    from pudge.syncing import SubtitleCandidate

    return SubtitleCandidate(path=path, name=path.name, source="jimaku", score=score, details={})


def test_embedded_reference_aligns_a_reuploaded_file_once(tmp_path, monkeypatch):
    from pudge import syncing
    from pudge.config import SyncConfig

    cues = [("00:00:01,000", "00:00:02,000", "一"), ("00:00:03,000", "00:00:04,000", "二")]
    files = [
        _srt(tmp_path / "a.srt", cues),
        _srt(tmp_path / "a-copy.srt", cues),
        _srt(tmp_path / "b.srt", [cues[0]]),
    ]
    aligned_calls = []
    captured = {}

    class Stop(Exception):
        pass

    monkeypatch.setattr(syncing, "probe_container_edit_points", lambda *a, **k: [])
    monkeypatch.setattr(syncing, "extract_embedded_timing_reference", lambda *a, **k: (files[0], {}))
    monkeypatch.setattr(syncing, "_exact_release_zero_offset_result", lambda *a, **k: None)
    monkeypatch.setattr(
        syncing,
        "align_subtitle_timelines",
        lambda source, *a, **k: (
            aligned_calls.append(source.name)
            or (
                source,
                {"timeline_alignment_reliable": True, "sync_was_successful": True, "offset_seconds": 0.0},
            )
        ),
    )
    monkeypatch.setattr(syncing, "_validate_embedded_reference_output", lambda *a, **k: (True, "ok", {}))
    monkeypatch.setattr(
        syncing, "compare_timing_activity", lambda *a, **k: {"available": True, "weighted": 0.9}
    )

    def rank(items, **kwargs):
        captured["items"] = list(items)
        raise Stop

    monkeypatch.setattr(syncing, "_rank_embedded_reference_candidates", rank)
    with pytest.raises(Stop):
        syncing._optimize_candidates_unguarded(
            tmp_path / "video.mkv",
            [_candidate(path) for path in files],
            tmp_path / "cache",
            SyncConfig(),
        )
    assert aligned_calls == ["a.srt", "b.srt"]
    items = captured["items"]
    assert [item[1].name for item in items] == ["a.srt", "a-copy.srt", "b.srt"]
    assert items[1][3]["alignment_reused_from"] == "a.srt"


def test_audio_evaluation_skips_an_identical_srt(tmp_path, monkeypatch):
    from pudge import syncing
    from pudge.config import SyncConfig

    cues = [("00:00:01,000", "00:00:02,000", "一"), ("00:00:03,000", "00:00:04,000", "二")]
    files = [_srt(tmp_path / "a.srt", cues), _srt(tmp_path / "a-copy.srt", cues)]
    calls = []
    captured = {}
    monkeypatch.setattr(syncing, "prepare_speech_reference", lambda *a, **k: (None, {}))
    monkeypatch.setattr(
        syncing,
        "synchronize_subtitle",
        lambda video, subtitle, *a, **k: (
            calls.append(subtitle.name) or (subtitle, {"sync_was_successful": True, "alignment_score": 10})
        ),
    )

    def consensus(evaluated, **kwargs):
        captured["evaluated"] = list(evaluated)
        return evaluated[0], {"offsets_seconds": [], "offset_spread_seconds": 0}

    monkeypatch.setattr(syncing, "_exact_jimaku_audio_clock_consensus", consensus)
    syncing._optimize_candidates_unguarded(
        tmp_path / "video.mkv",
        [_candidate(path) for path in files],
        tmp_path / "cache",
        SyncConfig(use_container_chapters=False),
    )
    assert calls == ["a.srt"]
    assert captured["evaluated"][1][2]["alignment_reused_from"] == "a.srt"


def test_anchor_chain_is_computed_once_for_the_same_texts(monkeypatch):
    from pudge import reading_audio_alignment as ralign

    calls = []
    original = ralign._best_dense_anchor_chain
    monkeypatch.setattr(ralign, "_best_dense_anchor_chain", lambda n, t: calls.append(1) or original(n, t))
    novel = "".join(f"今日は{index}番目の文章を読みます。" for index in range(40))
    first = ralign._global_anchor_chain(novel, novel)
    second = ralign._global_anchor_chain(novel, novel)
    assert first == second and len(calls) == 1 and first[2] == "exact-ngram"
    ralign._global_anchor_chain(novel + "終わり", novel)
    ralign._global_anchor_chain(novel + "終わり。", novel)
    assert len(ralign._ANCHOR_CHAIN_CACHE) == 2
