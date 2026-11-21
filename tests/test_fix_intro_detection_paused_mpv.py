# ruff: noqa: F811 - the "media" fixture is imported
"""OP/ED detection may run while the video mpv is paused (maintainer: "на паузе можно
раскошелиться по нагрузке чуть"); the user keeps mpv open, paused, for hours.

Only the intro-skipper pass opts in (``foreground_paused_ok=True``): playing mpv, a pause
shorter than 30 s, battery power, a silent or missing mpv socket keep it deferred, and
every other background job still waits for the whole foreground session. Resumed playback
kills the running FFmpeg within ~2 s; finished files keep their fingerprints.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from pudge import audio_fingerprints, foreground, intro_skipper, work_scheduler
from pudge.foreground import clear_foreground, foreground_playback, mark_foreground
from pudge.intro_skipper import IntroSkipperCache, cached_fingerprints, run_background_pass
from pudge.power_policy import PowerPolicy
from pudge.work_scheduler import WorkScheduler
from tests.test_fix_intro_detection_starvation import media  # noqa: F401 - fixture


class FakeMpv:
    """A tiny mpv JSON IPC server: get_property of pause / idle-active / time-pos."""

    def __init__(self, *, pause: bool = True, position: float = 612.5, answer: bool = True) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="mpv", dir="/tmp"))
        self.path = self.dir / "s.sock"
        self.state = {"pause": pause, "idle-active": False, "time-pos": position}
        self.answer = answer
        self.queries = 0
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(8)
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(client,), daemon=True).start()

    def _client(self, client: socket.socket) -> None:
        with client:
            buffer = b""
            try:
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        return
                    buffer += chunk
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        request = json.loads(line)
                        self.queries += 1
                        if not self.answer:
                            continue  # a hung player: never answers
                        name = request["command"][1]
                        reply = {
                            "request_id": request["request_id"],
                            "error": "success",
                            "data": self.state[name],
                        }
                        client.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                return

    def close(self) -> None:
        self._stop = True
        self._server.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def _power(source: str) -> PowerPolicy:
    return PowerPolicy(
        platform="darwin",
        run_command=lambda args, **_k: SimpleNamespace(
            stdout=(
                f"Now drawing from '{source}'\n -InternalBattery-0 (id=1)\t80%; charged;"
                if args[:3] == ["pmset", "-g", "batt"]
                else ""
            )
        ),
        native_probe=lambda: (False, False),
    )


@pytest.fixture
def player(tmp_path, monkeypatch):
    """A paused mpv as the foreground; PAUSE_SETTLE_SECONDS shortened for the tests."""
    monkeypatch.setattr(foreground, "PAUSE_SETTLE_SECONDS", 0.4)
    monkeypatch.setattr(work_scheduler, "PAUSE_SETTLE_SECONDS", 0.4)
    monkeypatch.setattr(foreground, "_PLAYBACK_CACHE_SECONDS", 0.3)
    foreground._playback_cache.clear()
    intro_skipper._deferral_logged.clear()
    servers: list[FakeMpv] = []
    cache_dir = tmp_path / "cache"

    def start(video: Path | None = None, **kwargs) -> FakeMpv:
        mpv = FakeMpv(**kwargs)
        servers.append(mpv)
        mark_foreground(cache_dir, video=video or tmp_path / "x.mkv", mpv_ipc=mpv.path)
        return mpv

    yield SimpleNamespace(start=start, cache_dir=cache_dir)
    clear_foreground(cache_dir)
    for mpv in servers:
        mpv.close()
    foreground._playback_cache.clear()


def _scheduler(cache_dir: Path, source: str = "AC Power") -> WorkScheduler:
    return WorkScheduler(cache_dir, power_policy=_power(source))


def _paused_long_enough(scheduler: WorkScheduler) -> None:
    """The pause counts from when Pudge first saw it; wait out PAUSE_SETTLE_SECONDS."""
    scheduler.background_wait_reason(foreground_paused_ok=True)
    time.sleep(0.45)
    foreground._playback_cache.clear()


def _db(files_by_media: dict[int, list]):
    animes = [
        SimpleNamespace(media_id=mid, title=title, status="CURRENT", media_status="RELEASING")
        for mid, (title, _files) in files_by_media.items()
    ]

    def episodes(media_id=None):
        return [
            SimpleNamespace(media_id=mid, episode=f.episode, video_path=f.path, state="ready")
            for mid, (_title, items) in files_by_media.items()
            if media_id in (None, mid)
            for f in items
            if f.path.exists()
        ]

    return SimpleNamespace(anime_list=lambda *a: animes, episodes=episodes)


def _files(media, folder: str, seed: int, count: int = 2):
    return [
        media.make(folder, f"{folder} - {n:02d}.mkv", n, seed=seed + n, op_at=10.0 * n, ed_at=700.0)
        for n in range(1, count + 1)
    ]


def _pass(media, db, scheduler, *, paused_ok: bool = True) -> dict:
    return run_background_pass(
        db=db,
        cache_dir=media.cache_dir,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        scheduler=scheduler,
        max_new_fingerprints=10,
        paused_ok=paused_ok,  # the agent's own step after the maintenance lock
    )


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    monkeypatch.setattr(intro_skipper, "_register_and_enforce", lambda *a: None)


def test_paused_mpv_on_ac_runs_the_pass_and_the_open_title_goes_first(media, player, tools) -> None:
    first = _files(media, "Aaa Show", 10)
    open_title = _files(media, "Zzz Show", 40)
    player.start(video=open_title[0].path, pause=True)
    scheduler = _scheduler(player.cache_dir)
    db = _db({1: ("Aaa Show", first), 2: ("Zzz Show", open_title)})
    # A pause younger than PAUSE_SETTLE_SECONDS: deferred at once (nothing waits in the pass).
    started = time.monotonic()
    assert _pass(media, db, scheduler) == {"status": "deferred", "reason": "paused_recently"}
    assert time.monotonic() - started < 0.5
    time.sleep(0.45)
    foreground._playback_cache.clear()
    result = _pass(media, db, scheduler)
    assert result["status"] == "ran" and result["media_id"] == 2 and result["found"] == 2, result
    assert _pass(media, db, scheduler)["media_id"] == 1


def test_maintenance_pass_and_refresh_never_run_beside_a_paused_player(media, player, tools) -> None:
    files = _files(media, "Aaa Show", 10)
    player.start(video=files[0].path, pause=True)
    scheduler = _scheduler(player.cache_dir)
    _paused_long_enough(scheduler)
    db = _db({1: ("Aaa Show", files)})
    assert _pass(media, db, scheduler, paused_ok=False) == {"status": "deferred", "reason": "foreground"}


def test_playing_mpv_defers_the_pass(media, player, tools, caplog) -> None:
    files = _files(media, "Aaa Show", 10)
    player.start(pause=False)
    scheduler = _scheduler(player.cache_dir)
    with caplog.at_level("INFO"):
        for _ in range(3):
            assert _pass(media, _db({1: ("Aaa Show", files)}), scheduler) == {
                "status": "deferred",
                "reason": "playing",
            }
    assert caplog.text.count("reason=deferred wait=playing") == 1  # at most once an hour


def test_paused_on_battery_defers_the_pass(media, player, tools) -> None:
    files = _files(media, "Aaa Show", 10)
    player.start(pause=True)
    scheduler = _scheduler(player.cache_dir, "Battery Power")
    _paused_long_enough(scheduler)
    assert _pass(media, _db({1: ("Aaa Show", files)}), scheduler) == {
        "status": "deferred",
        "reason": "battery",
    }


def test_other_power_blocks_still_apply_while_paused(player) -> None:
    player.start(pause=True)
    policy = _power("AC Power")
    policy.manual_enabled = True  # energy saving
    scheduler = WorkScheduler(player.cache_dir, power_policy=policy)
    _paused_long_enough(scheduler)
    assert scheduler.background_wait_reason(foreground_paused_ok=True) == "energy_saving"


def test_missing_or_silent_mpv_socket_counts_as_playing(player, tmp_path) -> None:
    mpv = player.start(pause=True, answer=False)
    scheduler = _scheduler(player.cache_dir)
    started = time.monotonic()
    assert scheduler.background_wait_reason(foreground_paused_ok=True) == "playing"
    assert time.monotonic() - started < 0.6  # never blocks on a hung player
    assert mpv.queries >= 1
    # No socket at all (mpv gone, or a preparation the user waits for).
    mark_foreground(player.cache_dir, video=tmp_path / "x.mkv", mpv_ipc=tmp_path / "gone.sock")
    foreground._playback_cache.clear()
    assert scheduler.background_wait_reason(foreground_paused_ok=True) == "playing"
    mark_foreground(player.cache_dir, video=tmp_path / "x.mkv")
    foreground._playback_cache.clear()
    assert scheduler.background_wait_reason(foreground_paused_ok=True) == "playing"


def test_other_background_work_still_waits_while_mpv_is_paused(player) -> None:
    player.start(pause=True)
    scheduler = _scheduler(player.cache_dir)
    _paused_long_enough(scheduler)
    assert scheduler.background_wait_reason(foreground_paused_ok=True) is None
    assert scheduler.background_wait_reason() == "foreground"
    assert not scheduler.background_allowed()
    assert scheduler.acquire_heavy("subtitle-rebuild", blocking=False) is None
    lease = scheduler.acquire_heavy("intro-skipper", blocking=False, foreground_paused_ok=True)
    assert lease is not None
    lease.release()


def test_pause_answer_is_cached_between_frequent_checks(player) -> None:
    mpv = player.start(pause=True)
    scheduler = _scheduler(player.cache_dir)
    for _ in range(10):
        scheduler.background_wait_reason(foreground_paused_ok=True)
    assert mpv.queries <= 3  # three properties, one connection
    # A pause lasts across agent processes: the same position keeps its start time.
    state, paused_for = foreground_playback(player.cache_dir)
    assert state == "paused"
    time.sleep(0.35)
    foreground._playback_cache.clear()
    assert foreground_playback(player.cache_dir)[1] >= paused_for + 0.3


def test_resumed_playback_stops_the_pass_quickly_and_keeps_finished_files(
    media, player, tools, monkeypatch, tmp_path
) -> None:
    files = _files(media, "Aaa Show", 10, count=3)
    mpv = player.start(video=files[0].path, pause=True)
    nice_file = tmp_path / "nice.txt"
    slow = tmp_path / "slow-ffmpeg"
    pid_file = tmp_path / "pid.txt"
    slow.write_text(f'#!/bin/sh\necho $$ > "{pid_file}"\nps -o nice= -p $$ > "{nice_file}"\nexec sleep 30\n')
    slow.chmod(0o755)
    fake_decode = intro_skipper.decode_region
    resumed: dict[str, float] = {}

    def decode(path, **kwargs):
        if Path(path) != files[2].path:
            return fake_decode(path, **kwargs)

        # The third file takes long: playback resumes while FFmpeg works on it.
        def resume() -> None:
            time.sleep(0.3)
            mpv.state["pause"] = False
            resumed["at"] = time.monotonic()

        threading.Thread(target=resume, daemon=True).start()
        kwargs["ffmpeg"] = str(slow)
        return audio_fingerprints.decode_region(path, **kwargs)

    monkeypatch.setattr(intro_skipper, "decode_region", decode)
    scheduler = _scheduler(player.cache_dir)
    _paused_long_enough(scheduler)
    result = _pass(media, _db({1: ("Aaa Show", files)}), scheduler)
    stopped_after = time.monotonic() - resumed["at"]
    assert result["status"] == "stopped" and result["stopped"] == "playing", result
    assert stopped_after <= 2.0, stopped_after
    cache = IntroSkipperCache(media.cache_dir)
    assert cached_fingerprints(cache, files[0].identity) is not None
    assert cached_fingerprints(cache, files[1].identity) is not None
    assert cached_fingerprints(cache, files[2].identity) is None
    # FFmpeg ran at low CPU priority (nice) and was killed, not left running.
    assert int(nice_file.read_text().strip()) >= 15
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text().strip()), 0)
