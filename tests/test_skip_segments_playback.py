"""Task 7: OP/ED skip button in Pudge's mpv (session file, launch, Lua behaviour)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from pudge import intro_skipper
from pudge.config import AppConfig, load_config, write_config
from pudge.intro_skipper import ALGORITHM_VERSION, IntroSkipperCache, write_playback_session
from pudge.player import build_mpv_command

ROOT = Path(__file__).resolve().parents[1]
SKIP_LUA = ROOT / "pudge" / "mpv_scripts" / "pudge_skip_segments.lua"
TRACKER_LUA = ROOT / "pudge" / "mpv_scripts" / "pudge_anilist.lua"
MOCK = ROOT / "tests" / "lua" / "mpv_mock.lua"


def _lua() -> str | None:
    configured = os.environ.get("PUDGE_TEST_LUA", "").strip()
    if configured:
        return configured
    for name in ("luajit", "lua5.1", "lua", "lua5.4", "lua5.3"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in ("/opt/homebrew/bin/luajit", "/usr/local/bin/luajit"):
        if Path(candidate).is_file():
            return candidate
    return None


LUA = _lua()
needs_lua = pytest.mark.skipif(LUA is None, reason="no lua/luajit interpreter (brew install luajit)")


def _cache_with_segments(tmp_path: Path, video: Path, segments: list[dict]) -> Path:
    cache_dir = tmp_path / "cache"
    identity = intro_skipper.identity_of(video)
    IntroSkipperCache(cache_dir).write_result(identity, {
        "algorithm": ALGORITHM_VERSION, "status": "found", "partners": [], "segments": segments,
    })
    return cache_dir


def _segment(kind: str, start: float, end: float) -> dict:
    return {"kind": kind, "start_seconds": start, "end_seconds": end, "source": "local-fingerprint",
            "confidence": 0.8, "media_fingerprint": "x"}


# --------------------------------------------------------- session file ---

def test_session_file_contains_only_ready_segments(tmp_path: Path) -> None:
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"v")
    assert write_playback_session(tmp_path / "cache", video) is None  # nothing detected yet
    cache_dir = _cache_with_segments(tmp_path, video, [_segment("intro", 30, 120), _segment("outro", 1300, 1390)])
    path = write_playback_session(cache_dir, video)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["session_id"]) == 16
    assert data["video"] == str(video) and data["video_absolute"] == str(video.resolve())
    assert data["fingerprint"] == intro_skipper.identity_of(video)
    assert [(s["kind"], s["start"], s["end"]) for s in data["segments"]] == [
        ("intro", 30.0, 120.0), ("outro", 1300.0, 1390.0)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    other = write_playback_session(cache_dir, video)
    assert other != path  # random session id per launch


def test_build_mpv_command_loads_skip_script_with_load_scripts_no(tmp_path: Path) -> None:
    tracker = tmp_path / "pudge_anilist.lua"
    plugin = tmp_path / "jiten" / "main.lua"
    command = build_mpv_command(
        "mpv", Path("ep.mkv"), None, None, ["--load-scripts=no", f"--script={plugin}"],
        script=tracker, extra_scripts=[SKIP_LUA],
    )
    assert command.index("--load-scripts=no") < command.index(f"--script={SKIP_LUA}")
    assert f"--script={tracker}" in command and f"--script={plugin}" in command
    assert command.count(f"--script={SKIP_LUA}") == 1
    assert command[-2:] == ["--", "ep.mkv"]


def _launch(tmp_path: Path, monkeypatch, *, exclusive: bool, segments: bool, enabled: bool = True):
    from pudge.cli import build_parser, process_video
    from pudge.pipeline_cache import save_final_pipeline_result

    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    cfg.ui.language = "ru"
    cfg.playback.skip_segments_enabled = enabled
    video = tmp_path / "Anime - 05.mkv"
    subtitle = tmp_path / "cached.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    save_final_pipeline_result(video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle,
                               source="external")
    if segments:
        _cache_with_segments(tmp_path, video, [_segment("intro", 10, 100)])
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *_a, **_k: [])
    # Starting playback must never run the detector.
    monkeypatch.setattr(intro_skipper, "analyze_title", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(intro_skipper, "run_background_pass", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    import pudge.first_experience as fe

    monkeypatch.setattr(fe, "mpv_study_script_plan", lambda *a, **k: (
        {"exclusive": True, "selected": "jiten", "scripts": ["/x/jiten/main.lua"]} if exclusive
        else {"exclusive": False, "selected": "", "scripts": []}))
    launched: list[tuple[list[str], dict]] = []
    seen_files: list[bool] = []

    def fake_run(command, **kwargs):
        env = kwargs.get("env_overrides") or {}
        launched.append((command, env))
        seen_files.append(bool(env.get("PUDGE_SEGMENTS_FILE")) and Path(env["PUDGE_SEGMENTS_FILE"]).is_file())
        return 0

    monkeypatch.setattr("pudge.cli.run_mpv", fake_run)
    args = build_parser().parse_args([str(video)])
    args.config = tmp_path / "config.toml"
    assert process_video(video, args, cfg, None) == 0
    return launched[0][0], launched[0][1], seen_files[0]


def test_process_video_passes_session_and_script_then_deletes_file(tmp_path, monkeypatch) -> None:
    command, env, existed = _launch(tmp_path, monkeypatch, exclusive=True, segments=True)
    assert "--load-scripts=no" in command
    assert any(arg.endswith("pudge_skip_segments.lua") and arg.startswith("--script=") for arg in command)
    assert any(arg.endswith("pudge_anilist.lua") for arg in command)
    assert "--script=/x/jiten/main.lua" in command
    assert existed and env["PUDGE_UI_LANGUAGE"] == "ru" and env["PUDGE_SHORTCUT_SKIP_SEGMENT"] == "Tab"
    assert env["PUDGE_AUTO_SKIP_INTRO"] == "0" and env["PUDGE_AUTO_SKIP_OUTRO"] == "0"
    assert not Path(env["PUDGE_SEGMENTS_FILE"]).exists()  # removed after mpv exits


def test_process_video_without_segments_or_disabled(tmp_path, monkeypatch) -> None:
    command, env, _ = _launch(tmp_path / "a", monkeypatch, exclusive=False, segments=False)
    assert "PUDGE_SEGMENTS_FILE" not in env
    assert any(arg.endswith("pudge_skip_segments.lua") for arg in command)
    command, env, _ = _launch(tmp_path / "b", monkeypatch, exclusive=False, segments=True, enabled=False)
    assert "PUDGE_SEGMENTS_FILE" not in env
    assert not any(arg.endswith("pudge_skip_segments.lua") for arg in command)


def test_config_roundtrip_and_reserved_shortcut(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.playback.auto_skip_outro = True
    cfg.shortcuts.mpv_skip_segment = "Ctrl+Enter"
    write_config(cfg, cfg.config_path)
    loaded = load_config(cfg.config_path)
    assert loaded.playback.skip_segments_enabled is True and loaded.playback.auto_skip_outro is True
    assert loaded.playback.auto_skip_intro is False and loaded.shortcuts.mpv_skip_segment == "Ctrl+Enter"
    text = cfg.config_path.read_text(encoding="utf-8").replace('"Ctrl+Enter"', '"Ctrl+a"')
    cfg.config_path.write_text(text, encoding="utf-8")
    assert load_config(cfg.config_path).shortcuts.mpv_skip_segment == "Tab"


# ------------------------------------------------------------ Lua (mpv) ---

def _session(tmp_path: Path, segments: list[dict], *, video: str = "/media/Show - 05.mkv") -> Path:
    path = tmp_path / "session.json"
    path.write_text(json.dumps({
        "version": 1, "session_id": "abc", "video": video, "video_absolute": video,
        "fingerprint": "f", "segments": segments,
    }), encoding="utf-8")
    return path


def _run(tmp_path: Path, scenario: str, *, env: dict[str, str], scripts: list[Path] | None = None) -> str:
    path = tmp_path / "scenario.lua"
    prelude = textwrap.dedent("""
        sim.set('path', '/media/Show - 05.mkv')
        sim.set('working-directory', '/media')
        sim.set('osd-dimensions', {w = 1920, h = 1080})
        sim.set('mouse-pos', {x = 10, y = 10, hover = true})
        sim.set('pause', false)
    """)
    path.write_text(prelude + textwrap.dedent(scenario), encoding="utf-8")
    full_env = {"PATH": os.environ.get("PATH", ""), "PUDGE_UI_LANGUAGE": "en", **env}
    completed = subprocess.run(
        [LUA, str(MOCK), str(path), *[str(s) for s in (scripts or [SKIP_LUA])]],
        capture_output=True, text=True, env=full_env, timeout=60,
    )
    assert completed.returncode == 0 and "SCENARIO_OK" in completed.stdout, completed.stderr[-4000:]
    return completed.stdout


@needs_lua
def test_lua_button_only_inside_segment_click_seeks_to_end(tmp_path: Path) -> None:
    session = _session(tmp_path, [{"kind": "intro", "start": 30, "end": 120},
                                  {"kind": "outro", "start": 1300, "end": 1390}])
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(5)
        assert(sim.overlay() == '', 'no button before the OP')
        assert(sim.press('Tab') == false, 'key is not taken outside a segment')
        assert(sim.click(1700, 850) == false, 'clicks pass through outside a segment')
        sim.play(26)  -- now at ~31 s
        assert(sim.overlay():find('Skip opening', 1, true), 'button visible in OP')
        local x0, y0, x1, y1 = sim.button()
        assert(x0 and x1 > x0 and y1 > y0, 'button drawn')
        assert(x1 <= 1920 and y1 <= 1080 * 0.9, 'button inside the window, above the controls')
        assert(sim.click(100, 100) == false, 'click outside the button is not captured')
        assert(sim.props['time-pos'] < 40)
        assert(sim.click((x0 + x1) / 2, (y0 + y1) / 2) == true, 'click on the button is captured')
        assert(math.abs(sim.props['time-pos'] - 120) < 0.001, 'seek to the segment end')
        local seeks = sim.commands('^seek')
        assert(seeks[#seeks] == 'seek 120.000 absolute+exact', seeks[#seeks])
        assert(sim.overlay() == '', 'button hidden after skip')
        assert(sim.press('Tab') == false, 'key released after skip')
    """, env={"PUDGE_SEGMENTS_FILE": str(session)})


@needs_lua
def test_lua_key_skips_ending_but_keeps_post_credit_scene_and_russian_label(tmp_path: Path) -> None:
    session = _session(tmp_path, [{"kind": "outro", "start": 1300, "end": 1390}])
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 1299)
        sim.fire('file-loaded')
        sim.play(2)
        assert(sim.overlay():find('Пропустить эндинг', 1, true), 'russian label')
        assert(sim.press('Tab') == true)
        assert(math.abs(sim.props['time-pos'] - 1390) < 0.001, 'ED ends at end_seconds, not EOF')
        sim.play(3)
        assert(sim.props['time-pos'] > 1390 and sim.props['time-pos'] < 1440, 'post-credit scene plays')
    """, env={"PUDGE_SEGMENTS_FILE": str(session), "PUDGE_UI_LANGUAGE": "ru"})


@needs_lua
def test_lua_rejects_invalid_segments_and_foreign_session(tmp_path: Path) -> None:
    session = _session(tmp_path, [
        {"kind": "intro", "start": -1, "end": 50},
        {"kind": "intro", "start": 60, "end": 60},
        {"kind": "outro", "start": 1400, "end": 1500},  # beyond duration
        {"kind": "credits", "start": 100, "end": 200},
        {"kind": "intro", "start": "10", "end": 90},
    ])
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(150)
        sim.seek(1420)
        sim.play(2)
        assert(sim.overlay() == '', 'no button for invalid segments')
    """, env={"PUDGE_SEGMENTS_FILE": str(session)})
    foreign = _session(tmp_path, [{"kind": "intro", "start": 0, "end": 90}], video="/media/Other - 01.mkv")
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(5)
        assert(sim.overlay() == '', 'session of another file is ignored')
    """, env={"PUDGE_SEGMENTS_FILE": str(foreign)})
    relative = _session(tmp_path, [{"kind": "intro", "start": 0, "end": 90}])
    _run(tmp_path, """
        sim.set('path', 'Show - 05.mkv')  -- mpv started with a relative path
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(1)
        assert(sim.overlay() ~= '', 'relative path resolved against working-directory')
    """, env={"PUDGE_SEGMENTS_FILE": str(relative)})


@needs_lua
def test_lua_auto_skip_is_opt_in_once_and_allows_rewatch(tmp_path: Path) -> None:
    session = _session(tmp_path, [{"kind": "intro", "start": 30, "end": 120}])
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 25)
        sim.fire('file-loaded')
        sim.play(6)
        assert(sim.props['time-pos'] >= 120 and sim.props['time-pos'] < 121.5, 'auto skipped when playback ran into the OP')
        sim.seek(40)  -- the user goes back to watch the OP
        sim.play(5)
        assert(sim.props['time-pos'] > 44 and sim.props['time-pos'] < 46, 'no second auto skip (no loop)')
        assert(sim.overlay():find('Skip opening', 1, true), 'manual button still offered')
    """, env={"PUDGE_SEGMENTS_FILE": str(session), "PUDGE_AUTO_SKIP_INTRO": "1"})
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 25)
        sim.fire('file-loaded')
        sim.play(10)
        assert(sim.props['time-pos'] > 34 and sim.props['time-pos'] < 36, 'manual by default')
    """, env={"PUDGE_SEGMENTS_FILE": str(session)})


@needs_lua
def test_lua_resets_on_end_file_and_resize(tmp_path: Path) -> None:
    session = _session(tmp_path, [{"kind": "intro", "start": 0, "end": 90}])
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(1)
        local _, _, small_x1 = sim.button()
        sim.set('osd-dimensions', {w = 3840, h = 2160})  -- fullscreen on a large display
        local _, _, big_x1 = sim.button()
        assert(big_x1 > small_x1, 'button follows the window size')
        sim.fire('end-file')
        assert(sim.overlay() == '', 'overlay cleared on end-file')
        assert(sim.press('Tab') == false, 'binding removed on end-file')
    """, env={"PUDGE_SEGMENTS_FILE": str(session)})


@needs_lua
def test_lua_without_session_does_nothing(tmp_path: Path) -> None:
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(3)
        assert(sim.overlay() == '')
        assert(sim.press('Tab') == false)
    """, env={})


@needs_lua
def test_open_near_ending_skip_does_not_count_the_jump_as_watched(tmp_path: Path) -> None:
    """Both scripts together: the skipped ED adds no active seconds."""
    session = _session(tmp_path, [{"kind": "outro", "start": 1310, "end": 1400}])
    _run(tmp_path, """
        sim.set('duration', 1440)
        sim.set('time-pos', 1290)
        sim.fire('file-loaded')
        sim.play(25)  -- 25 s really watched, now inside the ED
        assert(sim.press('Tab') == true)
        assert(math.abs(sim.props['time-pos'] - 1400) < 0.001)
        sim.play(5)
        sim.fire('end-file')
        local total = 0
        for _, cmd in ipairs(sim.commands('%-%-playback%-save')) do
            total = total + tonumber(cmd:match('%-%-playback%-active%-seconds (%S+)'))
        end
        assert(total > 25 and total < 35, 'active seconds are real time only: ' .. total)
        local skipped = false
        for _, row in ipairs(sim.log) do
            if row.kind == 'msg.info' and row.value:find('pudge segment skip: outro', 1, true) then skipped = true end
        end
        assert(skipped, 'tracker saw the skip event')
    """, env={
        "PUDGE_SEGMENTS_FILE": str(session),
        "PUDGE_PLAYBACK_ENABLED": "1", "PUDGE_PLAYBACK_VIDEO": "/media/Show - 05.mkv",
        "PUDGE_PLAYBACK_INTERVAL": "10",
        "PUDGE_ANILIST_TRACKING_FILE": str(tmp_path / "tracking.json"), "PUDGE_ANILIST_AUTO_UPDATE": "1",
    }, scripts=[TRACKER_LUA, SKIP_LUA])


def test_after_skip_near_ending_episode_is_not_counted(tmp_path: Path, monkeypatch, capsys) -> None:
    """Python side of the same regression: position at the end + 30 s active → deferred."""
    from pudge import cli
    from pudge.anilist_tracking import TrackingPayload, create_tracking_file
    from pudge.database import Database
    from pudge.manager_models import LibraryAnime, LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.access_token = "token"
    cfg.playback.enabled = True
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"v")
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=5, title="Show", status="CURRENT"))
    db.upsert_episode(LibraryEpisode(5, "Show", 5, video, state="ready"))
    db.record_playback(video, 1405, 1440, active_seconds=30)  # opened at the ED, skipped it

    class Client:
        def __init__(self, *_a, **_k):
            pass

        def update_progress(self, *_a, **_k):
            raise AssertionError("a skip must not count the episode")

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Client)
    tracking = create_tracking_file(cfg.paths.cache_dir, TrackingPayload(
        video=str(video), title="Show", media_id=5, episode=5, total_episodes=12, threshold=5 / 6,
        mapping_key="show"))
    from types import SimpleNamespace

    result = cli._run_anilist_action(
        SimpleNamespace(tracking_file=tracking, anilist_action="update", anilist_id=None, manual=False), cfg)
    assert result == 3 and "ANILIST_DEFERRED:1" in capsys.readouterr().out
    assert db.episode_by_path(video).state == "ready"
