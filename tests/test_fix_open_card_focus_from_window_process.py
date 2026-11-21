"""The card of an episode that is already open brings its mpv to the front again.

With the window in its own process (normal and Low RAM mode) the main process is an accessory
while the window is clicked, and macOS 14+ lets only the active app hand activation over: its
own attempt was refused (`EVENT play.focus ... focused=False` on every click). The window
process, which is the active app then, now does it; an in-process window still does it here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from test_web_app import make_api

from pudge import window_process


class _Host:
    def __init__(self, answer):
        self.answer = answer
        self.requests: list[tuple[str, object]] = []

    def request(self, op, args=None, *, timeout=None):
        self.requests.append((op, args))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_the_window_process_activates_the_mpv_it_is_asked_for(monkeypatch) -> None:
    asked: list[int] = []
    monkeypatch.setattr("pudge.window_focus.bring_process_to_front", lambda pid: asked.append(pid) or True)
    child = SimpleNamespace(window=None)  # no window needed: it is about another app
    assert window_process._ChildWindow._handle_request(child, "focus_process", {"pid": 4242}) is True
    assert asked == [4242]
    assert "focus_process" in window_process.WINDOW_REQUESTS


def test_the_remote_window_asks_the_window_process_and_says_when_it_could_not() -> None:
    host = _Host(True)
    assert window_process.RemoteWindow(host).focus_process(4242) is True
    assert host.requests == [("focus_process", {"pid": 4242})]
    assert window_process.RemoteWindow(_Host(False)).focus_process(1) is False
    assert window_process.RemoteWindow(_Host(window_process.ChannelClosed("gone"))).focus_process(1) is None
    assert window_process.RemoteWindow(_Host(TimeoutError("slow"))).focus_process(1) is None


def _running(api, tmp_path: Path):
    video = tmp_path / "Show - 03.mkv"
    video.write_bytes(b"video")
    key = str(video.resolve())
    mpv = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ack = tmp_path / "ack.json"
    ack.write_text(json.dumps({"pid": mpv.pid, "started_at": time.time()}), encoding="utf-8")
    api._play_processes[key] = mpv
    api._play_started_at[key] = time.time()
    api._play_registry[key] = {"ack_file": str(ack)}
    return video, mpv


def test_with_a_window_process_the_card_focuses_mpv_through_it(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    # This process is an accessory: its own attempt fails, as on the Mac.
    monkeypatch.setattr("pudge.window_focus.bring_process_to_front", lambda pid: False)
    asked: list[int] = []
    api.window = SimpleNamespace(focus_process=lambda pid: asked.append(pid) or True)
    video, mpv = _running(api, tmp_path)
    try:
        assert api.focus_playback(str(video)) == {"ok": True, "status": "running", "focused": True}
        assert asked == [mpv.pid]
        # The window process could not be asked (closing): this process tries itself.
        api.window = SimpleNamespace(focus_process=lambda pid: None)
        mine: list[int] = []
        monkeypatch.setattr("pudge.window_focus.bring_process_to_front", lambda pid: mine.append(pid) or True)
        assert api.focus_playback(str(video))["focused"] is True
        assert mine == [mpv.pid]
    finally:
        mpv.kill()
        mpv.wait()
