"""A freshly downloaded episode gets its OP/ED skip without waiting for the maintenance pass.

User log: JoJo ep 7 finished downloading at 15:50:29 and was played at 15:53:58 without a
Skip button; the only intro pass (the last step of the maintenance pass) ran at 15:45:43.

* completion: the foreground poll asks for detection of the completed videos at once (a
  background thread with the usual pass rules), and the pass takes those files first;
* playback: a video without segments gets a *pending* session; a background step for it
  runs beside mpv, and the skip script picks segments up while playing;
* selection: a new episode of an analysed season is matched in one step (one fingerprint),
  before other titles that need work.

Invented titles and synthetic audio only.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from pudge import intro_skipper  # noqa: E402
from pudge.intro_skipper import (  # noqa: E402
    EpisodeFile,
    IntroSkipperCache,
    PlaybackDetection,
    refresh_playback_session,
    request_analysis,
    run_background_pass,
    segments_for_video,
    write_playback_session,
)
from tests.lua_harness import SKIP_SEGMENTS, needs_lua, run_mpv_scenario  # noqa: E402
from tests.test_intro_skipper import FakeLease, _episode, _fps  # noqa: E402

pytestmark = pytest.mark.usefixtures("_detection_on")


@pytest.fixture
def _detection_on(monkeypatch):
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    monkeypatch.setattr(intro_skipper, "_register_and_enforce", lambda *a: None)


# ------------------------------------------------------------------ fixtures ---


class Scheduler:
    """Work scheduler stand-in: a playing video, power rules, the heavy lease."""

    def __init__(self, *, wait_reason=None, playing: Path | None = None, power=None) -> None:
        self.wait_reason = wait_reason
        self.playing = playing
        self.power_policy = SimpleNamespace(background_block_reason=lambda **_k: power)
        self.acquired: list[dict] = []
        self.released = 0

    def background_wait_reason(self, **_kwargs):
        return self.wait_reason

    def should_yield_to_higher_priority(self, _priority) -> bool:
        return False

    def foreground_video(self):
        return self.playing

    def acquire_heavy(self, name, **kwargs):
        self.acquired.append({"name": name, **kwargs})
        return FakeLease(self)


def _title(tmp_path: Path, name: str, count: int) -> list[EpisodeFile]:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for n in range(1, count + 1):
        path = folder / f"{name} - {n:02d}.mkv"
        path.write_bytes(f"{name} video {n}".encode())
        files.append(EpisodeFile(path=path, episode=n, identity=intro_skipper.identity_of(path)))
    return files


def _db(titles: dict[int, tuple[str, list[EpisodeFile]]]):
    animes = [
        SimpleNamespace(media_id=media_id, title=title, status="CURRENT", episodes=None)
        for media_id, (title, _files) in titles.items()
    ]
    rows = {
        media_id: [
            SimpleNamespace(media_id=media_id, episode=f.episode, video_path=f.path, state="ready")
            for f in files
        ]
        for media_id, (_title, files) in titles.items()
    }

    def episodes(media_id=None):
        if media_id is None:
            return [row for group in rows.values() for row in group]
        return rows.get(media_id, [])

    def episode_by_path(path):
        return next((row for row in episodes() if Path(row.video_path) == Path(path)), None)

    return SimpleNamespace(anime_list=lambda *a: animes, episodes=episodes, episode_by_path=episode_by_path)


_TRACKS: dict[tuple, tuple] = {}


def _track(seed: int, *, op_at: float | None, ed_at: float | None) -> tuple:
    """Fingerprints (head, tail, duration) of one synthetic episode, made once per session."""
    key = (seed, op_at, ed_at)
    if key not in _TRACKS:
        _TRACKS[key] = _fps(_episode(seed, op_at=op_at, ed_at=ed_at))
    return _TRACKS[key]


def _fake_fingerprints(monkeypatch, tracks: dict[str, tuple], cached: set[str], calls: list[str]):
    """Files in ``cached`` have fingerprints on disk already; the others cost a computation."""

    def fake(item, cache, *, ffmpeg, ffprobe, allow_compute, cancel_check):
        if item.identity in cached:
            return (*tracks[item.identity], False)
        if not allow_compute:
            return None
        calls.append(item.identity)
        cached.add(item.identity)
        return (*tracks[item.identity], True)

    monkeypatch.setattr(intro_skipper, "_fingerprints_for", fake)


def _season(tmp_path: Path, monkeypatch, *, new_cached: bool = False):
    """'Steel Run' 1-6 analysed (fingerprints cached), 7 just downloaded; 'Aaa Show' (first
    in the usual order) has three files that were never analysed."""
    steel = _title(tmp_path / "lib", "Steel Run", 7)
    other = _title(tmp_path / "lib", "Aaa Show", 3)
    tracks = {f.identity: _track(3000 + f.episode, op_at=10.0 + f.episode, ed_at=700.0) for f in steel}
    tracks.update({f.identity: _track(4000 + f.episode, op_at=30.0, ed_at=None) for f in other})
    cached = {f.identity for f in steel[:6]}
    if new_cached:
        cached.add(steel[6].identity)
    calls: list[str] = []
    _fake_fingerprints(monkeypatch, tracks, cached, calls)
    db = _db({1: ("Aaa Show", other), 2: ("Steel Run", steel)})
    cache_dir = tmp_path / "cache"
    # Episodes 1-6 were analysed by an earlier pass.
    intro_skipper.analyze_title(
        steel[:6], cache=IntroSkipperCache(cache_dir), ffmpeg="f", ffprobe="p", max_new_fingerprints=0
    )
    assert all(segments_for_video(cache_dir, f.path) for f in steel[:6])
    assert segments_for_video(cache_dir, steel[6].path) == []
    return db, cache_dir, steel, other, calls


# ------------------------------------------------- selection (goal 3) ---


def test_without_a_preference_the_new_episode_waits_behind_other_titles(tmp_path, monkeypatch) -> None:
    db, cache_dir, steel, other, calls = _season(tmp_path, monkeypatch)
    result = run_background_pass(db=db, cache_dir=cache_dir, ffmpeg="f", ffprobe="p", scheduler=Scheduler())
    assert result["media_id"] == 1  # the old order: "Aaa Show" first
    assert segments_for_video(cache_dir, steel[6].path) == []


def test_a_completed_download_is_analysed_first_with_one_fingerprint(tmp_path, monkeypatch) -> None:
    db, cache_dir, steel, other, calls = _season(tmp_path, monkeypatch)
    assert request_analysis(cache_dir, [steel[6].path]) == 1
    result = run_background_pass(db=db, cache_dir=cache_dir, ffmpeg="f", ffprobe="p", scheduler=Scheduler())
    assert result["status"] == "ran" and result["media_id"] == 2
    assert calls == [steel[6].identity]  # matched against the cached fingerprints of 1-6
    segments = segments_for_video(cache_dir, steel[6].path)
    assert [s.kind for s in segments] == ["intro", "outro"]
    assert abs(segments[0].start_seconds - 17.0) <= 2
    # Done: the wanted entry is gone, the next pass goes on with the other title.
    assert not (cache_dir / "intro-skipper" / intro_skipper.WANTED_FILE).exists()
    assert (
        run_background_pass(db=db, cache_dir=cache_dir, ffmpeg="f", ffprobe="p", scheduler=Scheduler())[
            "media_id"
        ]
        == 1
    )


def test_preferred_file_gets_the_fingerprint_budget_first(tmp_path, monkeypatch) -> None:
    files = _title(tmp_path, "Budget Show", 5)
    tracks = {f.identity: _track(5000 + f.episode, op_at=12.0, ed_at=700.0) for f in files}
    calls: list[str] = []
    _fake_fingerprints(monkeypatch, tracks, set(), calls)
    intro_skipper.analyze_title(
        files,
        cache=IntroSkipperCache(tmp_path / "cache"),
        ffmpeg="f",
        ffprobe="p",
        max_new_fingerprints=2,
        prefer=[files[4].identity],
    )
    assert calls == [files[4].identity, files[0].identity]


# --------------------------------------------- completion trigger (goal 1) ---


def _manager(tmp_path: Path):
    from tests.test_manager import _upgrade_manager

    manager = _upgrade_manager(tmp_path)
    manager.config.paths.cache_dir = tmp_path / "cache"
    manager.INTRO_REQUEST_RETRY_SECONDS = 0.01
    return manager


def _wait(predicate, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_completed_download_starts_detection_in_the_background(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    video = tmp_path / "Steel Run - 07.mkv"
    video.write_bytes(b"new episode")
    release = threading.Event()
    calls: list[dict] = []
    waits = iter(["heavy_busy", None])  # first deferred, then admitted
    monkeypatch.setattr(manager.work_scheduler, "background_wait_reason", lambda **_k: next(waits, None))

    def fake_pass(*, paused_ok, prefer_paths=()):
        calls.append({"paused_ok": paused_ok, "prefer": list(prefer_paths)})
        release.wait(5)
        IntroSkipperCache(manager.config.paths.cache_dir).write_result(
            intro_skipper.identity_of(video),
            {"algorithm": intro_skipper.ALGORITHM_VERSION, "status": "empty", "segments": []},
        )
        return {"status": "ran", "analysed": 1}

    monkeypatch.setattr(manager, "_intro_detection_pass", fake_pass)
    started = time.monotonic()
    assert manager.request_intro_detection([video]) is True
    assert time.monotonic() - started < 1.0  # the poll is never held up by the pass
    # Remembered for whichever pass comes first (agent, worker).
    wanted = json.loads((tmp_path / "cache" / "intro-skipper" / "wanted.json").read_text())
    assert str(video) in wanted
    assert _wait(lambda: calls)
    assert calls == [{"paused_ok": False, "prefer": [video]}]
    # A second completion while the step runs joins the same thread.
    assert manager.request_intro_detection([video]) is True
    release.set()
    assert _wait(lambda: manager._intro_request_thread is None)
    assert len(calls) == 1


def test_completion_trigger_is_off_with_the_skip_button(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.config.playback.skip_segments_enabled = False
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"x")
    monkeypatch.setattr(
        manager, "_intro_detection_pass", lambda **_k: (_ for _ in ()).throw(AssertionError("no pass"))
    )
    assert manager.request_intro_detection([video]) is False
    assert manager._intro_request_thread is None
    assert not (tmp_path / "cache" / "intro-skipper" / "wanted.json").exists()


def test_completion_trigger_passes_preferred_files_to_the_worker(tmp_path, monkeypatch) -> None:
    from pudge import low_ram

    manager = _manager(tmp_path)
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"x")
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(low_ram, "worker_allowed", lambda *_a: True)

    def worker(task, payload, inline, **_k):
        seen.append((task, payload))
        return {"status": "idle"}

    monkeypatch.setattr(low_ram, "worker_or_inline", worker)
    assert manager._intro_detection_pass(paused_ok=False, prefer_paths=[video]) == {"status": "idle"}
    assert seen[0][0] == "intro_pass" and seen[0][1]["prefer_paths"] == [str(video)]


def test_foreground_poll_asks_for_detection_of_completed_videos(tmp_path, monkeypatch) -> None:
    from tests.test_v203_refresh_foreground_queue import make_api

    api = make_api(tmp_path)
    video = tmp_path / "Steel Run - 07.mkv"
    video.write_bytes(b"x")

    def sync() -> int:
        api.manager._last_completed_video_paths = (video,)
        return 1

    asked: list[tuple] = []
    monkeypatch.setattr(api.manager, "sync_downloads", sync)
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: {"requeued": 0})
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: {})
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", lambda *a, **k: 0)
    monkeypatch.setattr(
        api.manager, "request_intro_detection", lambda paths, **k: asked.append((tuple(paths), k)) or True
    )
    api._poll_downloads_and_subtitles_sync()
    assert asked == [((video,), {"reason": "download_complete"})]


# ----------------------------------------------- play-time trigger (goal 2) ---


def test_playback_step_runs_beside_its_own_video_and_fills_the_session(tmp_path, monkeypatch) -> None:
    db, cache_dir, steel, other, calls = _season(tmp_path, monkeypatch)
    video = steel[6].path
    session = write_playback_session(cache_dir, video, pending=True)
    data = json.loads(session.read_text())
    assert data["pending"] is True and data["segments"] == []
    # Another video playing: the step waits.
    busy = Scheduler(wait_reason="foreground", playing=other[0].path)
    assert run_background_pass(
        db=db, cache_dir=cache_dir, ffmpeg="f", ffprobe="p", scheduler=busy, playback_video=video
    ) == {"status": "deferred", "reason": "foreground"}
    # Energy saving still wins.
    saving = Scheduler(wait_reason="foreground", playing=video, power="energy_saving")
    assert (
        run_background_pass(
            db=db, cache_dir=cache_dir, ffmpeg="f", ffprobe="p", scheduler=saving, playback_video=video
        )["status"]
        == "deferred"
    )
    assert calls == []
    # Its own video is the foreground: only its title, one fingerprint, the session gets segments.
    own = Scheduler(wait_reason="foreground", playing=video)
    result = run_background_pass(
        db=db, cache_dir=cache_dir, ffmpeg="f", ffprobe="p", scheduler=own, playback_video=video
    )
    assert result["status"] == "ran" and result["media_id"] == 2
    assert calls == [steel[6].identity]
    assert own.acquired[0]["foreground_sensitive"] is False and own.released == 1
    data = json.loads(session.read_text())
    assert data["pending"] is False and [row["kind"] for row in data["segments"]] == ["intro", "outro"]
    assert (session.stat().st_mode & 0o777) == 0o600


def test_playback_step_for_a_file_outside_the_library_is_idle(tmp_path, monkeypatch) -> None:
    db, cache_dir, steel, other, calls = _season(tmp_path, monkeypatch)
    loose = tmp_path / "Loose - 01.mkv"
    loose.write_bytes(b"x")
    result = run_background_pass(
        db=db,
        cache_dir=cache_dir,
        ffmpeg="f",
        ffprobe="p",
        scheduler=Scheduler(wait_reason="foreground", playing=loose),
        playback_video=loose,
    )
    assert result == {"status": "idle"} and calls == []


def _pending(tmp_path: Path) -> tuple[Path, Path, Path]:
    cache_dir = tmp_path / "cache"
    video = tmp_path / "Steel Run - 07.mkv"
    video.write_bytes(b"x")
    session = write_playback_session(cache_dir, video, pending=True)
    return cache_dir, video, session


def _found(cache_dir: Path, video: Path, *, confidence: float = 0.9) -> None:
    IntroSkipperCache(cache_dir).write_result(
        intro_skipper.identity_of(video),
        {
            "algorithm": intro_skipper.ALGORITHM_VERSION,
            "status": "found",
            "segments": [
                {"kind": "intro", "start_seconds": 20.0, "end_seconds": 110.0, "confidence": confidence},
            ],
        },
    )


def test_playback_detection_retries_a_deferred_step_then_feeds_mpv(tmp_path) -> None:
    cache_dir, video, session = _pending(tmp_path)
    answers = iter([{"status": "deferred", "reason": "heavy_busy"}, None])
    steps: list[int] = []

    def run_pass():
        steps.append(1)
        answer = next(answers)
        if answer is None:
            _found(cache_dir, video)
            return {"status": "ran"}
        return answer

    runner = PlaybackDetection(
        cache_dir=cache_dir, video=video, session=session, run_pass=run_pass, retry_seconds=0.01
    )
    assert runner.run() == "segments"
    assert len(steps) == 2
    data = json.loads(session.read_text())
    assert data["pending"] is False and data["segments"][0]["end"] == 110.0


@pytest.mark.parametrize(
    "run_pass",
    [
        lambda: (_ for _ in ()).throw(RuntimeError("worker crashed")),
        lambda: {"status": "tool_missing"},
        lambda: {"status": "idle"},
    ],
)
def test_playback_detection_failure_ends_pending_without_segments(tmp_path, run_pass) -> None:
    cache_dir, video, session = _pending(tmp_path)
    runner = PlaybackDetection(
        cache_dir=cache_dir, video=video, session=session, run_pass=run_pass, retry_seconds=0.01
    )
    runner.run()  # never raises
    data = json.loads(session.read_text())
    assert data == {**data, "pending": False, "segments": []}


def test_playback_detection_stops_with_mpv_and_never_recreates_the_session(tmp_path) -> None:
    cache_dir, video, session = _pending(tmp_path)
    runner = PlaybackDetection(
        cache_dir=cache_dir,
        video=video,
        session=session,
        run_pass=lambda: {"status": "deferred", "reason": "heavy_busy"},
        retry_seconds=30.0,
    ).start()
    assert _wait(lambda: runner._thread is not None)
    session.unlink()  # mpv exited
    runner.stop()
    runner.join(5)
    assert runner.outcome == "stopped" and not session.exists()
    # A late result is not written into a closed session either.
    _found(cache_dir, video)
    assert refresh_playback_session(cache_dir, session) is None and not session.exists()


def test_session_that_is_not_pending_is_never_changed(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"x")
    assert write_playback_session(cache_dir, video) is None  # unchanged default: no segments, no file
    _found(cache_dir, video)
    session = write_playback_session(cache_dir, video, pending=True)
    data = json.loads(session.read_text())
    assert "pending" not in data and len(data["segments"]) == 1
    IntroSkipperCache(cache_dir).write_result(
        intro_skipper.identity_of(video),
        {"algorithm": intro_skipper.ALGORITHM_VERSION, "status": "empty", "segments": []},
    )
    assert refresh_playback_session(cache_dir, session, final=True) == 1
    assert json.loads(session.read_text()) == data


def test_launch_without_segments_passes_a_pending_session_and_starts_detection(tmp_path, monkeypatch) -> None:
    from pudge import cli
    from tests.test_skip_segments_playback import _launch

    started: list[tuple] = []
    stopped: list[bool] = []

    def fake_start(config, video, session):
        started.append((video, json.loads(Path(session).read_text())))
        return SimpleNamespace(stop=lambda: stopped.append(True))

    monkeypatch.setattr(cli, "_start_playback_intro_detection", fake_start)
    command, env, existed = _launch(tmp_path, monkeypatch, exclusive=False, segments=False)
    assert existed and env["PUDGE_SEGMENTS_FILE"]
    assert len(started) == 1 and started[0][1]["pending"] is True and started[0][1]["segments"] == []
    assert stopped == [True] and not Path(env["PUDGE_SEGMENTS_FILE"]).exists()
    # Ready segments: an ordinary session, nothing started.
    started.clear()
    _command, env, existed = _launch(tmp_path / "ready", monkeypatch, exclusive=False, segments=True)
    assert existed and started == []


def test_launch_with_detection_off_has_no_pending_session(tmp_path, monkeypatch) -> None:
    from pudge import cli
    from tests.test_skip_segments_playback import _launch

    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "0")
    monkeypatch.setattr(
        cli, "_start_playback_intro_detection", lambda *a: (_ for _ in ()).throw(AssertionError("started"))
    )
    _command, env, _existed = _launch(tmp_path, monkeypatch, exclusive=False, segments=False)
    assert "PUDGE_SEGMENTS_FILE" not in env


# ------------------------------------------------------- Lua pickup (goal 2) ---

READ_LOG = """
local function logged(text)
    for _, row in ipairs(sim.log) do
        if tostring(row.value):find(text, 1, true) then return true end
    end
    return false
end
local function copy(from, to)
    local source = assert(io.open(from, 'r'))
    local content = source:read('*a')
    source:close()
    local target = assert(io.open(to, 'w'))
    target:write(content)
    target:close()
end
"""


def _sessions_for_lua(tmp_path: Path, *, final_without_segments: bool = False) -> tuple[Path, Path, str]:
    """The pending session as Python writes it at launch and its later version (a copy)."""
    cache_dir = tmp_path / "cache"
    video = tmp_path / "Steel Run - 07.mkv"
    video.write_bytes(b"x")
    session = write_playback_session(cache_dir, video, pending=True)
    later = tmp_path / "later.json"
    later.write_bytes(session.read_bytes())
    if not final_without_segments:
        _found(cache_dir, video)
    refresh_playback_session(cache_dir, later, final=True)
    return session, later, str(video)


@needs_lua
def test_lua_picks_up_segments_that_arrive_during_playback(tmp_path: Path) -> None:
    session, later, video = _sessions_for_lua(tmp_path)
    run_mpv_scenario(
        tmp_path,
        READ_LOG
        + f"""
        sim.set('path', {json.dumps(video)})
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        assert(logged('segments pending'), 'polling started')
        sim.play(12)
        assert(sim.overlay() == '', 'nothing to show yet')
        assert(sim.press('Tab') == false)
        copy({json.dumps(str(later))}, {json.dumps(str(session))})  -- Python rewrote the session
        sim.play(6)
        assert(logged('loaded during playback'), 'segments picked up')
        assert(sim.overlay() == '', 'no button outside the opening')
        sim.play(3)  -- ~21 s: two seconds into the opening
        assert(sim.overlay():find('Skip opening', 1, true), 'button in the opening')
        assert(sim.press('Tab') == true)
        assert(math.abs(sim.props['time-pos'] - 110) < 0.001, 'seek to the end of the opening')
        """,
        scripts=[SKIP_SEGMENTS],
        env={"PUDGE_SEGMENTS_FILE": str(session)},
    )


@needs_lua
def test_lua_late_segments_never_skip_or_show_past_the_first_seconds(tmp_path: Path) -> None:
    session, later, video = _sessions_for_lua(tmp_path)
    run_mpv_scenario(
        tmp_path,
        READ_LOG
        + f"""
        sim.set('path', {json.dumps(video)})
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(40)  -- in the opening, past its first seconds
        copy({json.dumps(str(later))}, {json.dumps(str(session))})
        sim.play(10)
        assert(logged('loaded during playback'))
        assert(sim.overlay() == '', 'no button 30 s into the opening')
        assert(sim.press('Tab') == false, 'key not taken')
        assert(#sim.commands('^seek') == 0, 'never skips by itself')
        sim.seek(20)
        sim.play(1)
        assert(sim.overlay():find('Skip opening', 1, true), 'seeking back shows it as usual')
        """,
        scripts=[SKIP_SEGMENTS],
        env={"PUDGE_SEGMENTS_FILE": str(session)},
    )


@needs_lua
def test_lua_stops_polling_when_detection_ends_without_segments(tmp_path: Path) -> None:
    session, later, video = _sessions_for_lua(tmp_path, final_without_segments=True)
    run_mpv_scenario(
        tmp_path,
        READ_LOG
        + f"""
        sim.set('path', {json.dumps(video)})
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        copy({json.dumps(str(later))}, {json.dumps(str(session))})
        sim.play(6)
        assert(logged('no segments for this file'), 'polling ended')
        -- A later (bogus) session with segments is not read any more.
        local handle = assert(io.open({json.dumps(str(session))}, 'w'))
        handle:write('{{"version": 1, "video": {json.dumps(video)}, "video_absolute": {json.dumps(video)}, '
            .. '"segments": [{{"kind": "intro", "start": 20, "end": 110}}]}}')
        handle:close()
        sim.play(20)
        assert(sim.overlay() == '', 'nothing shown')
        assert(not logged('loaded during playback'))
        """,
        scripts=[SKIP_SEGMENTS],
        env={"PUDGE_SEGMENTS_FILE": str(session)},
    )


@needs_lua
def test_lua_ignores_a_rewritten_session_of_another_file_and_end_file_stops_polling(tmp_path: Path) -> None:
    session, later, video = _sessions_for_lua(tmp_path)
    run_mpv_scenario(
        tmp_path,
        READ_LOG
        + f"""
        sim.set('path', '/media/Another - 01.mkv')
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        assert(logged('another file'), 'a session of another file is ignored')
        assert(not logged('segments pending'))
        sim.set('path', {json.dumps(video)})
        sim.fire('file-loaded')
        assert(logged('segments pending'))
        sim.fire('end-file')
        copy({json.dumps(str(later))}, {json.dumps(str(session))})
        sim.set('time-pos', 19)
        sim.play(10)
        assert(not logged('loaded during playback'), 'polling ended with the file')
        assert(sim.overlay() == '')
        """,
        scripts=[SKIP_SEGMENTS],
        env={"PUDGE_SEGMENTS_FILE": str(session)},
    )
