"""Second review of the subtitle-job fixes.

- A prepare worker runs in its own process group. launchd stops the agent at quit with
  SIGTERM; without a handler the agent died at once and left the worker (and its ffmpeg,
  alass, speech recognition) running. The window's own quit stops it by process tree.
- The "keeps failing" card was hidden when another file of the title came first.
- A Jimaku search was kept 6 h even while the title airs; a matched movie or one-episode
  special (no episode number) was retried only daily, like an unmatched file.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from pudge import cli
from pudge.config import AppConfig
from pudge.filename import parse_anime_filename
from pudge.manager import SUBTITLE_RETRY_EXHAUSTED
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.process_cleanup import OwnedProcessTree
from pudge.providers.jimaku import JimakuClient
import test_fix_subtitle_job_retries as retries
from test_fix_subtitle_job_retries import _due_again, _episode, _job, _run

api = retries.api  # fixtures
child = retries.child
ROOT = Path(__file__).resolve().parents[1]
WORKER = textwrap.dedent(
    """
    import os, subprocess, sys, time
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1] + ".tmp", "w") as handle:
        handle.write(f"{os.getpid()} {grandchild.pid}")
    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(120)
    """
)


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "tests"), os.environ.get("PYTHONPATH", "")]),
    }


def _gone(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state in {"Z", "X"}


def _wait_gone(pids: list[int], seconds: float = 15.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if all(_gone(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return False


def _pids(path: Path, seconds: float = 30.0) -> list[int]:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.is_file():
            return [int(value) for value in path.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("the worker did not start")


def _kill(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


needs_proc = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="reads process states from /proc"
)


@needs_proc
def test_agent_stopped_by_launchd_stops_the_prepare_worker_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    script = textwrap.dedent(
        f"""
        import subprocess, sys
        from pathlib import Path
        import pudge.manager as manager_module
        from pudge.agent import _exit_on_sigterm
        from pudge.config import AppConfig
        from pudge.manager import AnimeManager
        from pudge.manager_models import LibraryEpisode

        tmp = Path({str(tmp_path)!r})
        cfg = AppConfig()
        cfg.config_path = tmp / "config.toml"
        cfg.library.database_path = tmp / "library.sqlite3"
        cfg.library.root_dir = tmp / "library"
        cfg.paths.cache_dir = tmp / "cache"
        cfg.anilist.enabled = False
        manager = AnimeManager(cfg, log=lambda _message: None)
        video = (tmp / "Show - 05.mkv").resolve()
        video.write_bytes(b"video")
        manager.db.upsert_episode(
            LibraryEpisode(media_id=None, title="Show", episode=5, video_path=video, state="waiting_subtitles")
        )
        manager.db.queue_subtitle_job(video, None, 5)
        real = subprocess.Popen

        def popen(command, **kwargs):  # the real worker, as a long sleep with a child
            return real([sys.executable, "-c", {WORKER!r}, {str(pid_file)!r}], **kwargs)

        manager_module.subprocess.Popen = popen
        _exit_on_sigterm()
        manager.process_subtitle_jobs(limit=1, wait_for_slot=True)
        """
    )
    agent = subprocess.Popen([sys.executable, "-c", script], env=_env())
    pids: list[int] = []
    try:
        pids = _pids(pid_file)
        agent.send_signal(signal.SIGTERM)  # quit: launchd stops the agent
        assert agent.wait(timeout=30) == 128 + signal.SIGTERM
        assert _wait_gone(pids), "the worker or its child kept running after the agent stopped"
    finally:
        _kill([*pids, agent.pid])


def test_agent_main_installs_and_restores_the_sigterm_handler(monkeypatch) -> None:
    from pudge import agent

    seen = []
    monkeypatch.setattr(agent, "_main", lambda argv=None: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
    before = signal.getsignal(signal.SIGTERM)
    assert agent.main([]) == 0
    assert seen and seen[0] is not before and callable(seen[0])
    assert signal.getsignal(signal.SIGTERM) is before


@needs_proc
def test_window_quit_stops_a_worker_in_its_own_session(tmp_path: Path) -> None:
    pid_file = tmp_path / "pids"
    launcher = textwrap.dedent(
        f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", {WORKER!r}, {str(pid_file)!r}], start_new_session=True)
        time.sleep(120)
        """
    )
    window = subprocess.Popen([sys.executable, "-c", launcher])
    pids: list[int] = []
    try:
        pids = _pids(pid_file)
        tree = OwnedProcessTree(root=window.pid)  # what the window's quit cleanup uses
        assert tree.stop() == []
        assert _wait_gone(pids)
    finally:
        _kill([*pids, window.pid])
        window.wait(timeout=10)


def test_the_keeps_failing_card_shows_although_another_file_comes_first(tmp_path: Path, api) -> None:
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            media_id=7, title="Show", status="CURRENT", episodes=12, media_status="RELEASING", progress=4
        )
    )
    for number in (5, 6):
        video = (tmp_path / f"Show - {number:02d}.mkv").resolve()
        video.write_bytes(b"video")
        db.upsert_episode(
            LibraryEpisode(
                media_id=7, title="Show", episode=number, video_path=video, state="waiting_subtitles"
            )
        )
        db.queue_subtitle_job(video, 7, number)
    failing = (tmp_path / "Show - 06.mkv").resolve()
    with db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='needs_action',action_code=? WHERE video_path=?",
            (SUBTITLE_RETRY_EXHAUSTED, str(failing)),
        )
    [card] = api.get_state()["home"]["needs_action"]
    assert card["action_code"] == SUBTITLE_RETRY_EXHAUSTED and card["local"]["video_path"] == str(failing)


def _ttl_for(monkeypatch, tmp_path: Path, status: str | None) -> float:
    seen: list[float] = []
    original = JimakuClient.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        seen.append(self.search_cache_ttl_seconds)
        self.client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=[])))

    monkeypatch.setattr(JimakuClient, "__init__", init)
    monkeypatch.setattr(JimakuClient, "_acquire_request_slot", lambda self, path: None)
    monkeypatch.setattr(cli, "_jimaku_episode_aliases", lambda *a, **k: ())
    args = cli.build_parser().parse_args(
        ["--media-id", "7", "--media-title", "Show", *(["--media-status", status] if status else []), "x.mkv"]
    )
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    config.jimaku.api_key = "key"
    video = tmp_path / "Show - 05.mkv"
    cli._find_online_subtitles(
        video,
        parse_anime_filename(video.name),
        config,
        None,
        False,
        anime_hint=cli._anime_hint_from_args(args),
        skip_airing_lookup=True,
    )
    return seen[-1]


def test_searches_of_an_airing_title_are_kept_an_hour_finished_ones_six(tmp_path: Path, monkeypatch) -> None:
    assert _ttl_for(monkeypatch, tmp_path, "FINISHED") == 6 * 3600
    assert _ttl_for(monkeypatch, tmp_path, "RELEASING") == 3600
    assert _ttl_for(monkeypatch, tmp_path, None) == 3600  # status unknown: like airing


def test_the_manager_passes_the_airing_status(tmp_path: Path, api, child, monkeypatch) -> None:
    video = _episode(api, tmp_path)
    child.result = (4, "PREPARE_STATUS=waiting_subtitles\n", "")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "pudge.manager.subprocess.Popen",
        lambda command, **kwargs: commands.append(command) or child(command, **kwargs),
    )
    _run(api, video)
    [command] = commands
    assert command[command.index("--media-status") + 1] == "RELEASING"


@pytest.mark.parametrize(
    ("episodes", "media_format"),
    [(1, "MOVIE"), (1, "SPECIAL"), (None, "MOVIE")],
)
def test_a_matched_movie_or_special_keeps_the_normal_retry(
    tmp_path: Path, api, child, episodes, media_format
) -> None:
    video = _episode(api, tmp_path, media_id=8, title="Movie", episode=None)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=8,
            title="Movie",
            status="CURRENT",
            episodes=episodes,
            media_status="RELEASING",
            format=media_format,
        )
    )
    child.result = (4, "Jimaku: no sufficiently confident file found\nPREPARE_STATUS=waiting_subtitles\n", "")
    for _ in range(3):
        _due_again(api, video)
        started = time.time()
        _run(api, video)
        assert float(_job(api, video)["next_check"]) - started < 3600
