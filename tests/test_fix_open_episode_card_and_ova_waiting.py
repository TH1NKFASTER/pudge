"""Clicking the card of an episode that is already open shows its mpv; an OVA file matched
to a scheduled series without an episode number no longer replaces its "Caught up" card.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api

EPISODE = "/lib/Show - 03.mkv"


def test_card_of_an_open_episode_brings_its_mpv_to_the_front(page) -> None:
    result = page.run(
        f"""
        const path = {json.dumps(EPISODE)};
        ui.playStates.set(path, 'running');
        await startPlay(path); await __tick(20);
        const focused = __toasts();
        __api.focus_playback = async () => ({{ok: true, status: 'running', focused: false}});
        await startPlay(path); await __tick(20);
        return {{focused, notFocused: __toasts()}};
        """,
        api={"focus_playback": {"ok": True, "status": "running", "focused": True}},
    )
    assert result.calls_of("focus_playback") == [[EPISODE], [EPISODE]]
    assert result.calls_of("play") == []
    # mpv came to the front: no toast. When it cannot, the toast says the video is open.
    assert result.value["focused"] == []
    assert result.value["notFocused"] == ["Video is already open"]


def test_backend_focuses_the_mpv_of_a_running_playback(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    focused: list[int] = []
    monkeypatch.setattr("pudge.window_focus.bring_process_to_front", lambda pid: focused.append(pid) or True)
    video = tmp_path / "Show - 03.mkv"
    video.write_bytes(b"video")
    key = str(video.resolve())
    # The launcher and the mpv it reported in its start-up acknowledgement.
    mpv = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ack = tmp_path / "ack.json"
    ack.write_text(json.dumps({"pid": mpv.pid, "started_at": time.time()}), encoding="utf-8")
    try:
        api._play_processes[key] = mpv
        api._play_started_at[key] = time.time()
        api._play_registry[key] = {"ack_file": str(ack)}
        assert api.focus_playback(str(video)) == {"ok": True, "status": "running", "focused": True}
        assert focused == [mpv.pid]
    finally:
        mpv.kill()
        mpv.wait()
    api._play_processes.pop(key, None)
    api._play_registry.pop(key, None)
    assert api.focus_playback(str(video))["focused"] is False
    assert focused == [mpv.pid]


def test_ova_matched_without_episode_keeps_the_scheduled_series_caught_up(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            media_id=105228,
            title="Dorohedoro",
            status="CURRENT",
            episodes=3,
            media_status="FINISHED",
            progress=2,
            format="TV",
        )
    )
    folder = tmp_path / "Dorohedoro"
    folder.mkdir()
    for number in (1, 2, 3):
        video = folder / f"Dorohedoro - {number:02d}.mkv"
        video.write_bytes(b"video")
        subtitle = folder / f"Dorohedoro - {number:02d}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nドロヘドロ\n", encoding="utf-8")
        db.upsert_episode(
            LibraryEpisode(
                media_id=105228,
                title="Dorohedoro",
                episode=number,
                video_path=video.resolve(),
                subtitle_path=subtitle.resolve(),
                subtitle_origin="jimaku",
                state="watched" if number < 3 else "ready",
            )
        )
    # The OVA file matched to the series, without an episode number, still waiting for subtitles.
    ova = folder / "Dorohedoro - OVA.mkv"
    ova.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(
            media_id=105228, title="Dorohedoro", episode=None, video_path=ova.resolve(), state="local"
        )
    )
    # The personal schedule continues with episode 3, which opens tomorrow.
    start = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(time.time() + 86400))
    saved = api.personal_schedule_save(105228, start, "UTC", "days", 3, 0)
    assert saved.get("ok"), saved

    home = api.get_state()["home"]

    def titles(section: str) -> list[str]:
        return [card.get("title") for card in home.get(section) or []]

    assert titles("waiting") == []
    assert titles("caught_up") == ["Dorohedoro"]
    assert home["caught_up"][0]["personal_schedule"]["next_episode"] == 3


def test_mpv_is_activated_by_handing_over_activation(monkeypatch) -> None:
    # macOS 14+: the active Pudge hands activation to mpv (all its windows: its Space/screen).
    from types import SimpleNamespace

    from pudge import window_focus

    calls = []

    class RunningApp:
        def __init__(self, pid):
            self.pid = pid

        def activateFromApplication_options_(self, source, options):
            calls.append(("from", self.pid, source.pid, options))
            return True

    appkit = SimpleNamespace(
        NSApplicationActivateAllWindows=1,
        NSApplicationActivateIgnoringOtherApps=2,
        NSRunningApplication=SimpleNamespace(
            runningApplicationWithProcessIdentifier_=lambda pid: RunningApp(pid) if pid == 4242 else None,
            currentApplication=lambda: RunningApp(100),
        ),
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(window_focus.sys, "platform", "darwin")
    assert window_focus.bring_process_to_front(4242) is True
    assert calls == [("from", 4242, 100, 1)]
    assert window_focus.bring_process_to_front(5555) is False  # mpv has quit
