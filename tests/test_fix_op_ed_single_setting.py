"""Openings and endings: one setting (the skip button); detection follows it; no automatic skipping."""

from __future__ import annotations

import json
from pathlib import Path

from lua_harness import SKIP_SEGMENTS, needs_lua, run_mpv_scenario

from pudge import intro_skipper
from pudge.config import AppConfig, load_config, write_config

OLD_PLAYBACK = """
[playback]
enabled = true
intro_detection_enabled = false
skip_segments_enabled = {button}
auto_skip_intro = true
auto_skip_outro = true
"""


def _old_config(tmp_path: Path, *, button: bool) -> Path:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    write_config(cfg, cfg.config_path)
    text = cfg.config_path.read_text(encoding="utf-8")
    start = text.index("[playback]")
    end = text.index("\n[", start + 1)
    text = (
        text[:start] + OLD_PLAYBACK.strip().format(button="true" if button else "false") + "\n" + text[end:]
    )
    cfg.config_path.write_text(text, encoding="utf-8")
    return cfg.config_path


def test_old_detect_and_auto_skip_keys_are_ignored_and_dropped_on_save(tmp_path: Path) -> None:
    path = _old_config(tmp_path, button=True)
    loaded = load_config(path, resolve_secrets=False)
    assert loaded.playback.skip_segments_enabled is True
    write_config(loaded, path)
    text = path.read_text(encoding="utf-8")
    for key in ("intro_detection_enabled", "auto_skip_intro", "auto_skip_outro"):
        assert key not in text
    assert "skip_segments_enabled = true" in text


def _detection_enabled(tmp_path: Path, monkeypatch, *, button: bool) -> bool:
    from pudge.manager import AnimeManager

    cfg = load_config(_old_config(tmp_path, button=button), resolve_secrets=False)
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg)
    seen: list[bool] = []
    monkeypatch.setattr(
        intro_skipper,
        "run_background_pass",
        lambda **k: seen.append(k["enabled"]) or {"analysed": 0},
    )
    manager.run_intro_detection()
    return seen[0]


def test_detection_follows_the_skip_button(tmp_path: Path, monkeypatch) -> None:
    # An existing user with detection off but the button on: detection now runs.
    assert _detection_enabled(tmp_path / "on", monkeypatch, button=True) is True
    assert _detection_enabled(tmp_path / "off", monkeypatch, button=False) is False


@needs_lua
def test_mpv_never_skips_automatically(tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "abc",
                "video": "/media/Show - 05.mkv",
                "video_absolute": "/media/Show - 05.mkv",
                "fingerprint": "f",
                "segments": [
                    {"kind": "intro", "start": 30, "end": 120},
                    {"kind": "outro", "start": 1300, "end": 1390},
                ],
            }
        ),
        encoding="utf-8",
    )
    run_mpv_scenario(
        tmp_path,
        """
        sim.set('duration', 1440)
        sim.set('time-pos', 28)
        sim.fire('file-loaded')
        sim.play(4)
        assert(sim.props['time-pos'] > 31.5 and sim.props['time-pos'] < 32.5, 'the OP plays')
        assert(sim.overlay():find('Skip opening', 1, true), 'button shown')
        sim.seek(1298)
        sim.play(4)
        assert(sim.props['time-pos'] > 1301.5 and sim.props['time-pos'] < 1302.5, 'the ED plays')
        assert(#sim.commands('^seek') == 1, 'only the scenario seek, no automatic skip')
    """,
        scripts=[SKIP_SEGMENTS],
        env={"PUDGE_SEGMENTS_FILE": str(session), "PUDGE_AUTO_SKIP_INTRO": "1", "PUDGE_AUTO_SKIP_OUTRO": "1"},
    )
