"""How a subtitle job retries, pauses and stops.

- Playback starting (or a timeout, a network error) cleared the playable fallback of the
  job's episode: the OCR SRT of an episode waiting for text subtitles, the raw subtitle of
  a "couldn't sync" one. A run without a verdict now leaves the episode alone.
- A "fresh subtitles" request was killed at once under energy saving.
- A job taken over by another worker kept its child running; the result was thrown away.
- Retries never ended and never slowed down: a file that hangs ffmpeg cost 20 minutes
  every half hour, a file without a title asked Jimaku every half hour forever, and
  postponements nobody tried counted as attempts.
- Cancelling a prepare left ffmpeg, alass and the candidate pool running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pudge.manager import (
    SUBTITLE_FAILURE_STREAK_CAP,
    SUBTITLE_RETRY_EXHAUSTED,
    _stop_prepare_child,
)
from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api

SRT = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n"


class _Child:
    """A prepare worker that runs until stopped, or exits at once with ``result``."""

    instances: list["_Child"] = []
    result: tuple[int, str, str] | None = None
    on_poll = None

    def __init__(self, *_args, **kwargs):
        self.kwargs = kwargs
        self.terminated = False
        self.polls = 0
        self.returncode = None if _Child.result is None else _Child.result[0]
        _Child.instances.append(self)

    def poll(self):
        self.polls += 1
        if _Child.on_poll is not None:
            _Child.on_poll(self)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self):
        if _Child.result is None:
            return ("", "")
        return (_Child.result[1], _Child.result[2])


@pytest.fixture()
def api(tmp_path: Path):
    return make_api(tmp_path)


@pytest.fixture()
def child(monkeypatch, api):
    # After the window's API exists: its start runs real commands.
    _Child.instances = []
    _Child.result = None
    _Child.on_poll = None
    monkeypatch.setattr("pudge.manager.subprocess.Popen", _Child)
    monkeypatch.setattr("pudge.manager.time.sleep", lambda *_a: None)
    return _Child


def _episode(api, tmp_path: Path, **fields) -> Path:
    video = (tmp_path / "Show - 05.mkv").resolve()
    video.write_bytes(b"video")
    values = {"media_id": 7, "title": "Show", "episode": 5, "video_path": video, "state": "waiting_subtitles"}
    values.update(fields)
    raw = values.pop("raw", None)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=7, title="Show", status="CURRENT", episodes=12, media_status="RELEASING", progress=4
        )
    )
    api.manager.db.upsert_episode(LibraryEpisode(**values))
    if raw is not None:  # "couldn't sync": the raw subtitle stays playable
        api.manager.db.set_couldnt_sync_subtitle(video, raw, origin="raw_unsynced")
    api.manager.db.queue_subtitle_job(video, values["media_id"], values["episode"])
    return video


def _job(api, video: Path):
    return next(row for row in api.manager.db.subtitle_jobs() if Path(row["video_path"]) == video)


def _due_again(api, video: Path) -> None:
    with api.manager.db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET next_check=0 WHERE video_path=?", (str(video),))


def _run(api, video: Path) -> None:
    api.manager.process_subtitle_jobs(limit=1, preferred_paths=[video], wait_for_slot=True)


def _cached_subtitle(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "cache" / "fallback" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SRT, encoding="utf-8")
    return path


def test_playback_pause_keeps_the_ocr_fallback_and_counts_no_attempt(
    tmp_path: Path, api, child, monkeypatch
) -> None:
    ocr = _cached_subtitle(tmp_path, "ep5.srt")
    video = _episode(api, tmp_path, state="waiting_text_subtitles", subtitle_path=ocr, subtitle_origin="ocr")
    monkeypatch.setattr(
        api.manager.work_scheduler,
        "background_wait_reason",
        lambda *_a, **_k: "foreground" if child.instances else None,
    )
    api.manager.process_subtitle_jobs(limit=1)
    assert child.instances and child.instances[0].terminated
    row = api.manager.db.episode_by_path(video)
    assert (row.state, row.subtitle_path, row.subtitle_origin) == ("waiting_text_subtitles", ocr, "ocr")
    job = _job(api, video)
    assert (job["state"], int(job["attempts"])) == ("pending", 0)


def test_fresh_subtitles_request_is_not_killed_by_energy_saving(
    tmp_path: Path, api, child, monkeypatch
) -> None:
    video = _episode(api, tmp_path)
    monkeypatch.setattr(
        api.manager.work_scheduler.power_policy, "background_block_reason", lambda **_k: "energy_saving"
    )
    child.on_poll = lambda process: setattr(process, "returncode", 4) if process.polls > 3 else None
    _run(api, video)
    assert child.instances and not child.instances[0].terminated


def test_a_child_whose_job_was_taken_over_is_stopped(tmp_path: Path, api, child, monkeypatch) -> None:
    video = _episode(api, tmp_path)
    real_monotonic = time.monotonic
    clock = {"offset": 0.0}
    monkeypatch.setattr("pudge.manager.time.monotonic", lambda: real_monotonic() + clock["offset"])

    def taken_over(process):
        if process.polls == 1:
            # The lease ran out and another window claimed the job.
            with api.manager.db.connect() as conn:
                conn.execute(
                    "UPDATE subtitle_jobs SET owner_token='other-worker' WHERE video_path=?", (str(video),)
                )
        clock["offset"] += 16.0

    child.on_poll = taken_over
    _run(api, video)
    assert child.instances[0].terminated
    job = _job(api, video)
    assert (job["state"], job["owner_token"]) == ("processing", "other-worker")


def _timeout_clock(child, monkeypatch) -> None:
    real_monotonic = time.monotonic
    state = {"offset": 0.0}
    monkeypatch.setattr("pudge.manager.time.monotonic", lambda: real_monotonic() + state["offset"])

    def advance(_process):
        state["offset"] += 11 * 60.0

    child.on_poll = advance


def test_timeouts_keep_the_raw_subtitle_back_off_and_end_in_action_required(
    tmp_path: Path, api, child, monkeypatch
) -> None:
    raw = _cached_subtitle(tmp_path, "raw.srt")
    video = _episode(api, tmp_path, raw=raw)
    assert api.manager.db.episode_by_path(video).state == "couldnt_sync"
    _timeout_clock(child, monkeypatch)
    delays = []
    for _ in range(SUBTITLE_FAILURE_STREAK_CAP):
        _due_again(api, video)
        started = time.time()
        _run(api, video)
        job = _job(api, video)
        delays.append(round((float(job["next_check"]) - started) / 60) if job["state"] == "pending" else None)
        row = api.manager.db.episode_by_path(video)
        assert (row.state, row.subtitle_path) == ("couldnt_sync", raw)  # still playable as before
    # 30 minutes (the configured poll), then twice as long each time, then the person decides.
    assert delays == [30, 60, 120, 240, None]
    job = _job(api, video)
    assert (job["state"], job["action_code"], int(job["attempts"])) == (
        "needs_action",
        SUBTITLE_RETRY_EXHAUSTED,
        SUBTITLE_FAILURE_STREAK_CAP,
    )
    [card] = api.get_state()["home"]["needs_action"]
    assert card["action_code"] == SUBTITLE_RETRY_EXHAUSTED and card["local"]["video_path"] == str(video)
    # Refresh and launch leave it alone: another 20 minutes would end the same way.
    api.manager.db.force_requeue_unresolved_subtitle_jobs(priority=200)
    assert _job(api, video)["state"] == "needs_action"
    # "Retry preparation" on the card runs it again; the raw subtitle stays a fallback,
    # it never becomes Ready when that run ends the same way.
    api.retry_episode_preparation(str(video))
    for task in list(api.task_supervisor._tasks.values()):  # it runs off the bridge call (WEBFLOW-12)
        if "manual-subtitle-retry" in task.name:
            task.thread.join(timeout=60)
    row = api.manager.db.episode_by_path(video)
    assert (row.state, row.subtitle_path) == ("couldnt_sync", raw)
    assert (_job(api, video)["state"], _job(api, video)["action_code"]) == (
        "needs_action",
        SUBTITLE_RETRY_EXHAUSTED,
    )
    assert not api.manager.db.get_state(api.manager._subtitle_upgrade_state_key(video), "")


def test_the_same_error_backs_off_and_ends_in_action_required(tmp_path: Path, api, child) -> None:
    video = _episode(api, tmp_path)
    child.result = (
        1,
        "",
        "Traceback (most recent call last):\nValueError: broken container at 0x1234 frame 99\n",
    )
    delays = []
    for run in range(SUBTITLE_FAILURE_STREAK_CAP):
        # The error text varies in numbers only: still the same failure.
        child.result = (1, "", f"Traceback:\nValueError: broken container at 0x{run} frame {run}\n")
        _due_again(api, video)
        started = time.time()
        _run(api, video)
        job = _job(api, video)
        delays.append(round((float(job["next_check"]) - started) / 60) if job["state"] == "pending" else None)
    assert delays == [30, 60, 120, 240, None]
    assert _job(api, video)["action_code"] == SUBTITLE_RETRY_EXHAUSTED


def test_a_different_failure_starts_counting_again(tmp_path: Path, api, child) -> None:
    video = _episode(api, tmp_path)
    delays = []
    for error in ("ValueError: one", "ValueError: one", "KeyError: other", "KeyError: other"):
        child.result = (1, "", error)
        _due_again(api, video)
        started = time.time()
        _run(api, video)
        delays.append(round((float(_job(api, video)["next_check"]) - started) / 60))
    assert delays == [30, 60, 30, 60]


def test_waiting_for_an_upload_keeps_the_poll_of_an_airing_title(tmp_path: Path, api, child) -> None:
    video = _episode(api, tmp_path)
    child.result = (4, "Japanese subtitles not found yet\nPREPARE_STATUS=waiting_subtitles\n", "")
    for _ in range(SUBTITLE_FAILURE_STREAK_CAP + 2):
        _due_again(api, video)
        started = time.time()
        _run(api, video)
        job = _job(api, video)
        assert job["state"] == "pending"
        assert round((float(job["next_check"]) - started) / 60) == 30


def test_a_file_without_a_title_is_retried_daily_after_two_runs(tmp_path: Path, api, child) -> None:
    video = _episode(api, tmp_path, media_id=None, title="Dorohedoro - OVA", episode=None)
    child.result = (4, "Jimaku: no matching anime found\nPREPARE_STATUS=waiting_subtitles\n", "")
    delays = []
    for _ in range(3):
        _due_again(api, video)
        started = time.time()
        _run(api, video)
        delays.append(float(_job(api, video)["next_check"]) - started)
    assert delays[0] < 3600
    assert all(delay >= 86400 - 5 for delay in delays[1:])


def test_a_network_error_keeps_the_raw_subtitle(tmp_path: Path, api, child) -> None:
    raw = _cached_subtitle(tmp_path, "raw.srt")
    video = _episode(api, tmp_path, raw=raw)
    child.result = (1, "", "Jimaku API error: [Errno 8] nodename nor servname provided, or not known\n")
    _run(api, video)
    row = api.manager.db.episode_by_path(video)
    assert (row.state, row.subtitle_path) == ("couldnt_sync", raw)
    job = _job(api, video)
    assert (job["state"], int(job["attempts"])) == ("pending", 1)


def test_the_worker_runs_in_its_own_process_group(tmp_path: Path, api, child) -> None:
    video = _episode(api, tmp_path)
    child.result = (4, "PREPARE_STATUS=waiting_subtitles\n", "")
    _run(api, video)
    assert child.instances[0].kwargs.get("start_new_session") is True


def _process_state(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return "gone"


@pytest.mark.skipif(not Path("/proc/self/stat").exists() and sys.platform != "darwin", reason="needs ps")
def test_stopping_a_worker_stops_what_it_started(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        print(grandchild.pid, flush=True)
        time.sleep(60)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    grandchild = int(process.stdout.readline())
    try:
        _stop_prepare_child(process)
        deadline = time.time() + 10
        while time.time() < deadline:
            if Path("/proc").is_dir():
                if _process_state(grandchild) in {"gone", "Z", "X"}:
                    break
            else:
                result = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(grandchild)], capture_output=True, text=True
                )
                if not result.stdout.strip() or result.stdout.strip().startswith("Z"):
                    break
            time.sleep(0.1)
        else:
            pytest.fail("the worker's child kept running")
        assert process.poll() is not None
    finally:
        for pid in (grandchild, process.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        process.stdout.close()


def test_background_prepare_cleans_up_on_sigterm(tmp_path: Path) -> None:
    marker = tmp_path / "cleaned"
    script = textwrap.dedent(
        f"""
        import time
        from pudge.cli import _stop_on_sigterm
        _stop_on_sigterm()
        try:
            print("ready", flush=True)
            time.sleep(60)
        finally:
            open({str(marker)!r}, "w").write("done")
        """
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")]
        ),
    }
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert process.stdout.readline().strip() == "ready"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 128 + signal.SIGTERM
        assert marker.read_text() == "done"
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()


EXHAUSTED_CARD = {
    "media_id": 7,
    "title": "Show",
    "action_code": SUBTITLE_RETRY_EXHAUSTED,
    "action_error": "subtitle prepare timed out",
    "local": {"episode": 5, "video_path": "/videos/Show - 05.mkv", "state": "couldnt_sync"},
}

EXHAUSTED_SCRIPT = r"""
renderCurrent();
const card = document.querySelector('.retry-exhausted-card');
const out = {note: __text('.retry-exhausted-card .airing-state'),
             buttons: [...card.querySelectorAll('.needs-action-button')].map(b => [b.dataset.action, b.dataset.path, b.textContent]),
             expected: [PudgeI18n.t('ui.subtitleRetryExhausted'), PudgeI18n.t('action.retryPreparation')]};
await __click('[data-action="retry-needs-action"]'); await __tick(20);
out.page = document.body.dataset.page || '';
return out;
"""


def test_home_card_offers_retry_for_an_exhausted_job(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            EXHAUSTED_SCRIPT,
            lang=lang,
            state={"home": {"needs_action": [EXHAUSTED_CARD]}},
            api={"retry_episode_preparation": {"ok": True, "state": {"home": {}}}},
        )
        out = result.value
        note, label = out["expected"]
        assert out["note"] == note
        assert out["buttons"] == [["retry-needs-action", "/videos/Show - 05.mkv", label]]
        assert result.calls_of("retry_episode_preparation") == [["/videos/Show - 05.mkv"]]
