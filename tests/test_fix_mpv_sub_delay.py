"""The subtitle delay set in mpv with z/x is remembered (review SCEN-9).

No script observed ``sub-delay`` and no launch passed ``--sub-delay``, so the
same correction was needed on every launch. The mpv script now sends the delay
with the playback save; it is stored per video for the subtitle it was set for,
and the next launch of that same subtitle starts with it. A re-prepared
subtitle is a new file: its delay starts at zero again.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.playback_save import main as playback_save_main
from pudge.playback_save import save_playback
from pudge.subtitle_delay import remember_subtitle_delay, remembered_subtitle_delay
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario

SRT = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n"


def _library(tmp_path: Path):
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "cache" / "playback-srt" / "v17-abc.srt"
    subtitle.parent.mkdir(parents=True)
    subtitle.write_text(SRT, encoding="utf-8")
    db.upsert_anime(LibraryAnime(media_id=5, title="Show", status="CURRENT"))
    db.upsert_episode(LibraryEpisode(5, "Show", 5, video, state="ready", subtitle_path=subtitle))
    return db, video.resolve(), subtitle


def test_delay_is_kept_for_the_same_subtitle_only(tmp_path: Path) -> None:
    db, video, subtitle = _library(tmp_path)
    remember_subtitle_delay(db, video, 1.25, subtitle=subtitle)
    assert remembered_subtitle_delay(db, video, subtitle=subtitle) == 1.25
    # Another subtitle (or none) for the video: no delay.
    other = subtitle.with_name("v17-other.srt")
    other.write_text(SRT, encoding="utf-8")
    assert remembered_subtitle_delay(db, video, subtitle=other) is None
    assert remembered_subtitle_delay(db, video) is None
    # The same playback copy made again (same name and size): still the same subtitle.
    later = time.time() + 5
    os.utime(subtitle, (later, later))
    assert remembered_subtitle_delay(db, video, subtitle=subtitle) == 1.25
    # Re-prepared into the same path with other cues: the delay starts over.
    subtitle.write_text(SRT + "\n2\n00:00:03,000 --> 00:00:04,000\nまた\n", encoding="utf-8")
    assert remembered_subtitle_delay(db, video, subtitle=subtitle) is None
    # Back to zero forgets it; embedded tracks are keyed by their number.
    remember_subtitle_delay(db, video, -0.4, embedded_sid=3)
    assert remembered_subtitle_delay(db, video, embedded_sid=3) == -0.4
    assert remembered_subtitle_delay(db, video, embedded_sid=2) is None
    remember_subtitle_delay(db, video, 0.0, embedded_sid=3)
    assert remembered_subtitle_delay(db, video, embedded_sid=3) is None


def test_playback_save_stores_the_delay(tmp_path: Path) -> None:
    db, video, subtitle = _library(tmp_path)
    save_playback(db, video, position=100.0, duration=1440.0, sub_delay=2.5, subtitle=str(subtitle))
    assert remembered_subtitle_delay(db, video, subtitle=subtitle) == 2.5
    assert db.episode_by_path(video).playback_position == 100.0
    # An embedded-track playback (no external file) uses the episode's track.
    db.upsert_episode(LibraryEpisode(5, "Show", 5, video, state="ready", embedded_subtitle_id=4))
    save_playback(db, video, position=110.0, duration=1440.0, sub_delay=-1.0, subtitle="")
    assert remembered_subtitle_delay(db, video, embedded_sid=4) == -1.0
    # Older scripts send no delay: nothing changes.
    save_playback(db, video, position=120.0, duration=1440.0)
    assert remembered_subtitle_delay(db, video, embedded_sid=4) == -1.0
    # The script names the launch track; zero for another subtitle keeps this one.
    save_playback(db, video, position=125.0, duration=1440.0, sub_delay=0.5, subtitle="", subtitle_sid=2)
    assert remembered_subtitle_delay(db, video, embedded_sid=2) == 0.5
    save_playback(db, video, position=126.0, duration=1440.0, sub_delay=0.0, subtitle=str(subtitle))
    assert remembered_subtitle_delay(db, video, embedded_sid=2) == 0.5
    save_playback(db, video, position=127.0, duration=1440.0, sub_delay=0.0, subtitle="", subtitle_sid=2)
    assert remembered_subtitle_delay(db, video, embedded_sid=2) is None


def test_a_late_older_save_does_not_change_the_delay(tmp_path: Path) -> None:
    db, video, subtitle = _library(tmp_path)
    save_playback(
        db,
        video,
        position=200.0,
        duration=1440.0,
        session="s",
        sequence=5,
        sub_delay=1.5,
        subtitle=str(subtitle),
    )
    # The HTTP save of sequence 4 arrives after the fallback of sequence 5.
    save_playback(
        db,
        video,
        position=190.0,
        duration=1440.0,
        session="s",
        sequence=4,
        sub_delay=0.0,
        subtitle=str(subtitle),
    )
    assert remembered_subtitle_delay(db, video, subtitle=subtitle) == 1.5


def test_playback_save_command_line_takes_the_delay(tmp_path: Path) -> None:
    db, video, subtitle = _library(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(f'[library]\ndatabase_path = "{tmp_path / "library.sqlite3"}"\n', encoding="utf-8")
    code = playback_save_main(
        [
            "--config",
            str(config),
            "--playback-save",
            "--playback-video",
            str(video),
            "--playback-position",
            "30",
            "--playback-duration",
            "1440",
            "--playback-sub-delay",
            "0.750",
            "--playback-subtitle",
            str(subtitle),
        ]
    )
    assert code == 0
    assert remembered_subtitle_delay(db, video, subtitle=subtitle) == 0.75


def test_web_playback_save_passes_the_delay(tmp_path: Path, monkeypatch) -> None:
    import threading

    from pudge import playback_save
    from pudge.web_app import WebAppApi

    db, video, subtitle = _library(tmp_path)
    api = WebAppApi.__new__(WebAppApi)
    api._play_lock = threading.Lock()

    class Process:
        def poll(self):
            return None

    api._play_processes = {str(video): Process()}
    api._play_key = lambda value: str(Path(value).expanduser().resolve())
    api.manager = type("M", (), {"db": db})()
    api.consumption = None
    captured: dict = {}
    monkeypatch.setattr(playback_save, "save_playback", lambda *_a, **kwargs: captured.update(kwargs))
    api.mpv_playback_save(
        {"video": str(video), "position": 5, "duration": 10, "sub_delay": 1.5, "subtitle": str(subtitle)}
    )
    assert captured["sub_delay"] == 1.5 and captured["subtitle"] == str(subtitle)
    api.mpv_playback_save({"video": str(video), "position": 6, "duration": 10, "sub_delay": "x"})
    assert captured["sub_delay"] is None


def test_next_launch_passes_the_remembered_delay(tmp_path: Path, monkeypatch) -> None:
    from pudge.cli import build_parser, process_video
    from pudge.config import AppConfig

    db, video, subtitle = _library(tmp_path)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *_a, **_k: [])
    launches: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        "pudge.cli.run_mpv",
        lambda command, **kwargs: (
            launches.append((list(command), dict(kwargs.get("env_overrides") or {}))) or 0
        ),
    )

    def launch() -> tuple[list[str], dict[str, str]]:
        args = build_parser().parse_args([str(video), "--fast-play", "--no-sync", "--sub", str(subtitle)])
        args.config = tmp_path / "config.toml"
        assert process_video(video, args, cfg, None) == 0
        return launches[-1]

    command, env = launch()
    assert not [arg for arg in command if arg.startswith("--sub-delay")]
    # What the mpv script reports for this playback (PUDGE_SUBTITLE_PATH is the file mpv shows).
    played = env["PUDGE_SUBTITLE_PATH"]
    assert env["PUDGE_SUBTITLE_SID"] == ""  # an external file, not an embedded track
    assert f"--sub-file={played}" in command
    save_playback(db, video, position=300.0, duration=1440.0, sub_delay=1.5, subtitle=played)
    command, _env = launch()
    assert "--sub-delay=1.500" in command
    # An explicit value in the user's mpv arguments wins.
    cfg.tools.mpv_extra_args = ["--sub-delay=0.2"]
    command, _env = launch()
    assert [arg for arg in command if arg.startswith("--sub-delay")] == ["--sub-delay=0.2"]


@needs_lua
def test_mpv_script_sends_the_delay_with_the_playback_save(tmp_path: Path) -> None:
    run_mpv_scenario(
        tmp_path,
        """
        sim.set('duration', 1440)
        sim.set('time-pos', 100)
        sim.set('current-tracks/sub/external-filename', '/subs/Show - 05.srt')
        sim.set('sub-delay', 0)        -- mpv reports the launch value first
        sim.fire('file-loaded')
        sim.play(11)
        local first = sim.commands('%-%-playback%-save')
        assert(#first >= 1, 'periodic save')
        assert(first[#first]:find('--playback-sub-delay 0.000 --playback-subtitle /subs/Show - 05.srt', 1, true), first[#first])
        sim.set('pause', true)
        sim.set('sub-delay', 1.5)      -- z/x while paused: saved although the position is unchanged
        sim.play(10)
        local saves = sim.commands('%-%-playback%-save')
        assert(#saves == #first + 1, 'a delay change is saved: ' .. #saves)
        assert(saves[#saves]:find('--playback-sub-delay 1.500', 1, true), saves[#saves])
        sim.play(10)
        assert(#sim.commands('%-%-playback%-save') == #saves, 'no repeated saves while paused')
        -- The viewer switches to "Original: ..." and corrects that track: not saved for the prepared one.
        sim.set('current-tracks/sub/external-filename', '/media/Show - 05.ja.srt')
        sim.set('sub-delay', -2.0)
        sim.play(10)
        local other = sim.commands('%-%-playback%-save')
        assert(not other[#other]:find('--playback-sub-delay', 1, true), other[#other])
        sim.set('current-tracks/sub/external-filename', '/subs/Show - 05.srt')
        sim.set('sub-delay', 1.5)
        sim.fire('end-file')
        local final = sim.commands('%-%-playback%-save')
        assert(final[#final]:find('--playback-sub-delay 1.500', 1, true), final[#final])
        """,
        env={
            "PUDGE_PLAYBACK_ENABLED": "1",
            "PUDGE_PLAYBACK_VIDEO": "/media/Show - 05.mkv",
            "PUDGE_PLAYBACK_INTERVAL": "10",
            "PUDGE_SUBTITLE_PATH": "/subs/Show - 05.srt",
        },
        scripts=[ANILIST],
    )


@needs_lua
def test_mpv_script_sends_the_delay_of_the_launch_embedded_track_only(tmp_path: Path) -> None:
    run_mpv_scenario(
        tmp_path,
        """
        sim.set('duration', 1440)
        sim.set('time-pos', 100)
        sim.set('current-tracks/sub/id', 3)
        sim.set('sub-delay', 0)
        sim.fire('file-loaded')
        sim.set('sub-delay', 0.8)
        sim.play(11)
        local saves = sim.commands('%-%-playback%-save')
        assert(saves[#saves]:find('--playback-sub-delay 0.800 --playback-subtitle  --playback-subtitle-sid 3', 1, true), saves[#saves])
        sim.set('current-tracks/sub/id', 1)   -- embedded English
        sim.set('sub-delay', 2.0)
        sim.play(10)
        saves = sim.commands('%-%-playback%-save')
        assert(not saves[#saves]:find('--playback-sub-delay', 1, true), saves[#saves])
        """,
        env={
            "PUDGE_PLAYBACK_ENABLED": "1",
            "PUDGE_PLAYBACK_VIDEO": "/media/Show - 05.mkv",
            "PUDGE_PLAYBACK_INTERVAL": "10",
            "PUDGE_SUBTITLE_SID": "3",
        },
        scripts=[ANILIST],
    )
