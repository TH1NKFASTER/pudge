"""mpv line keys (replay / previous / next / auto-pause) and a subtitle delay carried
across the episodes of one release.

All names and subtitle text here are invented.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from pudge import subtitle_delay
from pudge.config import (
    MPV_LINE_SHORTCUTS,
    AppConfig,
    ShortcutsConfig,
    apply_mpv_line_shortcut_settings,
    load_config,
    mpv_line_shortcut_env,
    mpv_line_shortcut_settings,
    write_config,
)
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.pipeline_cache import save_final_pipeline_result
from pudge.playback_save import save_playback
from pudge.subtitle_delay import (
    agreed_delay,
    carried_delay_suggestion,
    name_family,
    release_identity,
    remember_carried_delay,
    remember_subtitle_delay,
    remembered_subtitle_delay,
)
from tests.i18n_support import catalog, en, ru
from tests.lua_harness import ANILIST, SCRIPTS, needs_lua, run_mpv_scenario

ROOT = Path(__file__).resolve().parents[1]
LINE_KEYS = SCRIPTS / "pudge_line_keys.lua"
RELEASE = "group:kumo|external:[kumo] hoshi no niwa - #.ja.srt"


def _srt(episode: int) -> str:
    return (
        f"1\n00:00:01,000 --> 00:00:02,000\nてすと{episode}\n"
        + "\n2\n00:00:03,000 --> 00:00:04,000\nあ\n" * episode
    )


def _library(tmp_path: Path, episodes: int = 5, group: str = "Kumo"):
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=7, title="Hoshi no Niwa", status="CURRENT"))
    rows = []
    for number in range(1, episodes + 1):
        video = tmp_path / f"[{group}] Hoshi no Niwa - {number:02d} [1080p].mkv"
        video.write_bytes(b"video")
        subtitle = tmp_path / "prepared" / f"hoshi-{number:02d}.srt"
        subtitle.parent.mkdir(exist_ok=True)
        subtitle.write_text(_srt(number), encoding="utf-8")
        db.upsert_episode(
            LibraryEpisode(7, "Hoshi no Niwa", number, video.resolve(), state="ready", subtitle_path=subtitle)
        )
        rows.append(db.episode_by_path(video.resolve()))
    return db, rows


def _set(db, row, seconds: float) -> None:
    remember_subtitle_delay(db, row.video_path, seconds, subtitle=row.subtitle_path)


def _suggest(db, rows, target, releases: dict[int, str] | None = None):
    releases = releases or {}
    return carried_delay_suggestion(
        db, target, rows, release_of=lambda row: releases.get(row.media_episode, RELEASE)
    )


@pytest.fixture
def results(monkeypatch):
    lines: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        subtitle_delay, "_log_result", lambda video, outcome, **fields: lines.append((outcome, fields))
    )
    return lines


# --- the rule -------------------------------------------------------------------------


def test_agreed_delay_rule() -> None:
    assert agreed_delay([0.35, 0.40]) == 0.375
    assert agreed_delay([0.30, 0.35, 0.45]) == 0.35  # the median, spread exactly 0.15
    assert agreed_delay([-1.2, -1.25]) == -1.225
    assert agreed_delay([0.35]) is None  # one episode only
    assert agreed_delay([]) is None
    assert agreed_delay([0.30, 0.50]) is None  # 0.2 apart
    assert agreed_delay([0.35, 0.35, 0.0]) is None  # an explicit zero disagrees
    assert agreed_delay([0.0, 0.0]) is None  # nothing to carry


def test_two_agreeing_episodes_are_carried_to_the_others(tmp_path: Path) -> None:
    db, rows = _library(tmp_path)
    assert _suggest(db, rows, rows[2]) is None  # nobody set anything
    _set(db, rows[0], 0.35)
    assert _suggest(db, rows, rows[2]) is None  # one episode only
    _set(db, rows[1], 0.40)
    assert _suggest(db, rows, rows[2]) == (0.375, 2, RELEASE)
    assert _suggest(db, rows, rows[4]) == (0.375, 2, RELEASE)
    # The episodes that have their own delay keep it: nothing is suggested over it.
    assert _suggest(db, rows, rows[0]) is None
    assert remembered_subtitle_delay(db, rows[0].video_path, subtitle=rows[0].subtitle_path) == 0.35


def test_disagreeing_delays_are_not_carried(tmp_path: Path) -> None:
    db, rows = _library(tmp_path)
    _set(db, rows[0], 0.35)
    _set(db, rows[1], 0.60)
    assert _suggest(db, rows, rows[2]) is None
    _set(db, rows[3], 0.36)  # two agree, the third does not: still no agreement
    assert _suggest(db, rows, rows[2]) is None


def test_another_release_is_not_evidence(tmp_path: Path) -> None:
    db, rows = _library(tmp_path)
    _set(db, rows[0], 0.35)
    _set(db, rows[1], 0.35)
    other = "group:other|external:hoshi.s#e#.ja.srt"
    # The target is of another release (other video group or other subtitle family).
    assert _suggest(db, rows, rows[2], {3: other}) is None
    # One of the two samples is of another release: one episode is left.
    assert _suggest(db, rows, rows[2], {2: other}) is None
    # An unknown release is never matched.
    assert _suggest(db, rows, rows[2], {1: "", 2: "", 3: ""}) is None
    assert _suggest(db, rows, rows[2]) == (0.35, 2, RELEASE)


def test_a_delay_of_a_replaced_subtitle_is_not_evidence(tmp_path: Path) -> None:
    db, rows = _library(tmp_path)
    _set(db, rows[0], 0.35)
    _set(db, rows[1], 0.35)
    # Episode 2 was prepared again (other cues): its old delay no longer describes it.
    rows[1].subtitle_path.write_text(_srt(9), encoding="utf-8")
    assert _suggest(db, rows, rows[2]) is None


def test_a_remembered_delay_is_never_overridden(tmp_path: Path) -> None:
    db, rows = _library(tmp_path)
    for row in rows[:3]:
        _set(db, row, 0.35)
    _set(db, rows[3], -2.0)  # this episode is off differently, by the viewer's own hand
    assert _suggest(db, rows, rows[3]) is None
    assert remembered_subtitle_delay(db, rows[3].video_path, subtitle=rows[3].subtitle_path) == -2.0
    # ...and its odd delay stops the carry for the rest of the release.
    assert _suggest(db, rows, rows[4]) is None


def test_carried_then_reset_remembers_zero(tmp_path: Path, results) -> None:
    db, rows = _library(tmp_path)
    _set(db, rows[0], 0.35)
    _set(db, rows[1], 0.35)
    target = rows[2]
    seconds, episodes, release = _suggest(db, rows, target)
    assert remember_carried_delay(
        db, target.video_path, seconds, subtitle=target.subtitle_path, episodes=episodes, release=release
    )
    assert results[-1] == ("carried", {"seconds": 0.35, "episodes": 2, "release": RELEASE})
    # It plays with the carried delay (also on the next launch) and heartbeats keep the mark.
    assert remembered_subtitle_delay(db, target.video_path, subtitle=target.subtitle_path) == 0.35
    save_playback(
        db, target.video_path, position=30, duration=1400, sub_delay=0.35, subtitle=str(target.subtitle_path)
    )
    stored = json.loads(db.get_state(subtitle_delay.state_key(target.video_path)))
    assert stored["origin"] == "carried" and "kept" not in stored
    # A carried delay is not evidence: episode 4 is still carried from the two manual ones.
    assert _suggest(db, rows, rows[3]) == (0.35, 2, RELEASE)
    # The reset key sends zero: remembered as zero, logged, never suggested again.
    save_playback(
        db, target.video_path, position=40, duration=1400, sub_delay=0.0, subtitle=str(target.subtitle_path)
    )
    assert results[-1] == ("reset", {"previous": 0.35, "seconds": 0.0})
    assert remembered_subtitle_delay(db, target.video_path, subtitle=target.subtitle_path) is None
    assert _suggest(db, rows, target) is None
    save_playback(
        db, target.video_path, position=50, duration=1400, sub_delay=0.0, subtitle=str(target.subtitle_path)
    )
    assert json.loads(db.get_state(subtitle_delay.state_key(target.video_path)))["origin"] == "carry_reset"
    assert len([row for row in results if row[0] == "reset"]) == 1
    # The remembered zero is evidence against the release-wide delay.
    assert _suggest(db, rows, rows[3]) is None


def test_carried_delay_kept_and_adjusted_are_logged(tmp_path: Path, results, monkeypatch) -> None:
    db, rows = _library(tmp_path)
    _set(db, rows[0], 0.35)
    _set(db, rows[1], 0.35)
    kept, adjusted = rows[2], rows[3]
    for row in (kept, adjusted):
        remember_carried_delay(
            db, row.video_path, 0.35, subtitle=row.subtitle_path, episodes=2, release=RELEASE
        )
    results.clear()
    _set(db, kept, 0.35)
    assert results == []  # too early to call it kept
    now = subtitle_delay.time.time()
    monkeypatch.setattr(subtitle_delay.time, "time", lambda: now + 600)
    _set(db, kept, 0.35)
    _set(db, kept, 0.35)
    assert results == [("kept", {"seconds": 0.35})]  # once
    assert json.loads(db.get_state(subtitle_delay.state_key(kept.video_path)))["origin"] == "carried"
    # z/x on a carried delay: the viewer's own delay from now on (and evidence).
    _set(db, adjusted, 0.45)
    assert results[-1] == ("adjusted", {"previous": 0.35, "seconds": 0.45})
    assert "origin" not in json.loads(db.get_state(subtitle_delay.state_key(adjusted.video_path)))
    assert _suggest(db, rows, rows[4]) == (0.35, 3, RELEASE)


def test_result_line_is_written_to_the_log(tmp_path: Path, monkeypatch) -> None:
    lines: list[str] = []

    class Log:
        def info(self, message, *args):
            lines.append(message % args)

    monkeypatch.setattr("pudge.logging_utils.configure_logging", lambda *a, **k: Log())
    subtitle_delay._log_result(tmp_path / "Hoshi - 03.mkv", "carried", seconds=0.35, episodes=2, release="r")
    assert lines == [
        "RESULT step=mpv.sub_delay_carry video=Hoshi - 03.mkv outcome=carried seconds=0.350 episodes=2 release='r'"
    ]


def test_release_identity() -> None:
    assert name_family("[Kumo] Hoshi no Niwa - 03 [1A2B3C4D].ja.srt") == name_family(
        "[Kumo] Hoshi no Niwa - 11 [99887766].ja.srt"
    )
    assert name_family("Hoshi.S01E03.WEB.ja.srt") != name_family("[Kumo] Hoshi no Niwa - 03.ja.srt")

    def identity(
        video="[Kumo] Hoshi - 03.mkv", group="Kumo", source="external", raw="Hoshi.S01E03.ja.srt", sid=None
    ):
        return release_identity(
            video, release_group=group, subtitle_source=source, raw_subtitle_name=raw, embedded_sid=sid
        )

    base = identity()
    assert base and base == identity(video="[kumo] Hoshi - 04.mkv", group="KUMO", raw="Hoshi.S01E04.ja.srt")
    assert base != identity(group="Ame")  # another video release group
    assert base != identity(raw="[Tsuki] Hoshi - 03.srt")  # another subtitle family
    assert base != identity(source="embedded", sid=2) != identity(source="embedded", sid=3)
    assert identity(source="embedded", sid=2) == identity(
        video="[Kumo] Hoshi - 09.mkv", source="embedded", sid=2
    )
    # No group in the name: the name family stands in for it.
    assert identity(video="Hoshi.S01E03.1080p.mkv", group="") == identity(
        video="Hoshi.S01E04.1080p.mkv", group=""
    )
    # Unknown subtitle source (a manually chosen file, nothing recorded): no identity.
    assert identity(source="") == "" and identity(raw="") == "" and identity(source="manual") == ""


# --- the launch -----------------------------------------------------------------------


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    return cfg


def _manifests(cfg: AppConfig, rows, raw_name: str = "[Kumo] Hoshi no Niwa - {n:02d}.ja.srt") -> None:
    for row in rows:
        raw = cfg.paths.cache_dir / "downloads" / raw_name.format(n=row.media_episode)
        save_final_pipeline_result(
            row.video_path,
            cfg,
            subtitle=row.subtitle_path,
            subtitle_id=None,
            source="external",
            raw_source=raw,
        )


def test_launch_helper_carries_within_one_release(tmp_path: Path, results) -> None:
    from pudge.cli import _carried_subtitle_delay_for_launch, _episode_release_identity

    db, rows = _library(tmp_path)
    cfg = _config(tmp_path)
    _manifests(cfg, rows[:4])
    # Episode 5 has subtitles of another family.
    _manifests(cfg, rows[4:], raw_name="Hoshi.S01E{n:02d}.WEB.ja.srt")
    assert _episode_release_identity(cfg, rows[0]) == _episode_release_identity(cfg, rows[3]) != ""
    assert _episode_release_identity(cfg, rows[4]) not in {"", _episode_release_identity(cfg, rows[0])}
    _set(db, rows[0], 0.35)
    _set(db, rows[1], 0.35)

    def launch(row):
        return _carried_subtitle_delay_for_launch(cfg, row.video_path, row.subtitle_path, None)

    assert launch(rows[4]) is None  # another subtitle family
    assert launch(rows[2]) == 0.35
    assert results[-1][0] == "carried" and results[-1][1]["episodes"] == 2
    stored = json.loads(db.get_state(subtitle_delay.state_key(rows[2].video_path)))
    assert stored["origin"] == "carried" and stored["seconds"] == 0.35
    # mpv plays some other subtitle than the library's: nothing is carried to it.
    stray = tmp_path / "stray.srt"
    stray.write_text(_srt(1), encoding="utf-8")
    assert _carried_subtitle_delay_for_launch(cfg, rows[3].video_path, stray, None) is None
    # A manifest that describes an older subtitle of the row gives no identity.
    replaced = dataclasses.replace(rows[3], subtitle_path=stray)
    assert _episode_release_identity(cfg, replaced) == ""


def test_process_video_starts_mpv_with_the_carried_delay(tmp_path: Path, monkeypatch, results) -> None:
    from pudge.cli import build_parser, process_video

    db, rows = _library(tmp_path)
    cfg = _config(tmp_path)
    _manifests(cfg, rows)
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *_a, **_k: [])
    launches: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        "pudge.cli.run_mpv",
        lambda command, **kwargs: (
            launches.append((list(command), dict(kwargs.get("env_overrides") or {}))) or 0
        ),
    )

    def launch(row, *extra: str):
        args = build_parser().parse_args(
            [str(row.video_path), "--fast-play", "--no-sync", "--sub", str(row.subtitle_path), *extra]
        )
        args.config = cfg.config_path
        assert process_video(row.video_path, args, cfg, None) == 0
        return launches[-1]

    command, env = launch(rows[0])
    assert not [arg for arg in command if arg.startswith("--sub-delay")]
    assert "PUDGE_SUBTITLE_DELAY_CARRIED" not in env
    # The line keys script is loaded and gets its keys; unbound ones are passed empty.
    assert any(arg.endswith("pudge_line_keys.lua") for arg in command)
    assert env["PUDGE_SHORTCUT_REPLAY_LINE"] == "Ctrl+r" and env["PUDGE_SHORTCUT_NEXT_LINE"] == ""
    assert env["PUDGE_SHORTCUT_RESET_CARRIED_DELAY"] == "Ctrl+z"
    for row in rows[:2]:
        save_playback(
            db,
            row.video_path,
            position=300,
            duration=1400,
            sub_delay=0.35,
            subtitle=launch(row)[1]["PUDGE_SUBTITLE_PATH"],
            cache_dir=cfg.paths.cache_dir,
        )
    command, env = launch(rows[0])
    assert "--sub-delay=0.350" in command and "PUDGE_SUBTITLE_DELAY_CARRIED" not in env  # its own
    # A dry run carries (and stores) nothing.
    launch(rows[2], "--dry-run")
    assert db.get_state(subtitle_delay.state_key(rows[2].video_path), "") == ""
    command, env = launch(rows[2])
    assert "--sub-delay=0.350" in command and env["PUDGE_SUBTITLE_DELAY_CARRIED"] == "0.350"
    played = env["PUDGE_SUBTITLE_PATH"]
    # Declined in mpv: the next launch starts at zero and says nothing.
    save_playback(
        db,
        rows[2].video_path,
        position=20,
        duration=1400,
        sub_delay=0.0,
        subtitle=played,
        cache_dir=cfg.paths.cache_dir,
    )
    assert results[-1][0] == "reset"
    command, env = launch(rows[2])
    assert not [arg for arg in command if arg.startswith("--sub-delay")]
    assert "PUDGE_SUBTITLE_DELAY_CARRIED" not in env
    # The user's own --sub-delay in the mpv arguments wins over a carry.
    cfg.tools.mpv_extra_args = ["--sub-delay=0.2"]
    command, env = launch(rows[3])
    assert [arg for arg in command if arg.startswith("--sub-delay")] == ["--sub-delay=0.2"]
    assert "PUDGE_SUBTITLE_DELAY_CARRIED" not in env


# --- config and settings --------------------------------------------------------------


def test_line_shortcuts_defaults_and_config_round_trip(tmp_path: Path) -> None:
    shortcuts = ShortcutsConfig()
    # Only replay (and the carried-delay reset) are bound out of the box.
    assert mpv_line_shortcut_env(shortcuts) == {
        "PUDGE_SHORTCUT_REPLAY_LINE": "Ctrl+r",
        "PUDGE_SHORTCUT_PREVIOUS_LINE": "",
        "PUDGE_SHORTCUT_NEXT_LINE": "",
        "PUDGE_SHORTCUT_AUTO_PAUSE": "",
        "PUDGE_SHORTCUT_RESET_CARRIED_DELAY": "Ctrl+z",
    }
    fields = {field.name for field in dataclasses.fields(ShortcutsConfig)}
    assert {field for field, _env in MPV_LINE_SHORTCUTS.values()} <= fields
    used = {shortcuts.mpv_mark_watched, shortcuts.mpv_translate_subtitle, shortcuts.mpv_explain_subtitle}
    assert not used & {shortcuts.mpv_replay_line, shortcuts.mpv_reset_carried_delay}
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.shortcuts.mpv_previous_line = "Ctrl+ArrowLeft"
    cfg.shortcuts.mpv_auto_pause = "Ctrl+p"
    cfg.shortcuts.mpv_replay_line = ""
    write_config(cfg, cfg.config_path)
    loaded = load_config(cfg.config_path, resolve_secrets=False).shortcuts
    assert (loaded.mpv_previous_line, loaded.mpv_auto_pause, loaded.mpv_replay_line) == (
        "Ctrl+ArrowLeft",
        "Ctrl+p",
        "",
    )
    assert loaded.mpv_next_line == "" and loaded.mpv_reset_carried_delay == "Ctrl+z"
    for example in (ROOT / "config.example.toml", ROOT / "pudge" / "config.example.toml"):
        assert (
            load_config(example, resolve_secrets=False, apply_language=False).shortcuts == ShortcutsConfig()
        )


def test_settings_values_are_applied() -> None:
    shortcuts = ShortcutsConfig()
    apply_mpv_line_shortcut_settings(
        shortcuts, {"shortcut_mpv_next_line": " Ctrl+ArrowRight ", "shortcut_mpv_replay_line": ""}
    )
    assert shortcuts.mpv_next_line == "Ctrl+ArrowRight" and shortcuts.mpv_replay_line == ""
    assert shortcuts.mpv_reset_carried_delay == "Ctrl+z"  # not sent: unchanged
    # A key Pudge already uses in mpv, or the same key twice, is left unbound.
    apply_mpv_line_shortcut_settings(
        shortcuts,
        {
            "shortcut_mpv_replay_line": "ctrl+a",
            "shortcut_mpv_previous_line": "Ctrl+b",
            "shortcut_mpv_auto_pause": "Ctrl+B",
        },
    )
    assert shortcuts.mpv_replay_line == "" and shortcuts.mpv_previous_line == "Ctrl+b"
    assert shortcuts.mpv_auto_pause == ""
    assert mpv_line_shortcut_settings(shortcuts)["shortcut_mpv_previous_line"] == "Ctrl+b"


def test_settings_window_round_trip(tmp_path: Path) -> None:
    from pudge.web_app import WebAppApi

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    settings = api.save_settings(
        {"shortcut_mpv_next_line": "Ctrl+ArrowRight", "shortcut_mpv_auto_pause": "Ctrl+a"}
    )["settings"]
    assert (
        settings["shortcut_mpv_replay_line"] == "Ctrl+r"
        and settings["shortcut_mpv_next_line"] == "Ctrl+ArrowRight"
    )
    assert settings["shortcut_mpv_auto_pause"] == ""  # Ctrl+A counts the episode
    assert settings["shortcut_mpv_reset_carried_delay"] == "Ctrl+z"
    saved = load_config(cfg.config_path, resolve_secrets=False).shortcuts
    assert saved.mpv_next_line == "Ctrl+ArrowRight" and saved.mpv_auto_pause == ""


def test_line_keys_are_in_settings(page) -> None:
    labels = {
        "replay_line": "settings.shortcutMpvReplayLine",
        "previous_line": "settings.shortcutMpvPreviousLine",
        "next_line": "settings.shortcutMpvNextLine",
        "auto_pause": "settings.shortcutMpvAutoPause",
        "reset_carried_delay": "settings.shortcutMpvResetCarriedDelay",
    }
    assert {f"shortcut_mpv_{name}" for name in labels} == set(MPV_LINE_SHORTCUTS)
    for lang, text in (("en", en), ("ru", ru)):
        result = page.run(
            """
            setPage('settings'); await __tick(20);
            const out = {};
            for (const name of %s) {
                const el = $('s_shortcut_mpv_' + name);
                out[name] = el ? [el.dataset.shortcutValue, el.getAttribute('aria-label')] : null;
            }
            await __click('#s_shortcut_mpv_previous_line');
            await __key('ArrowLeft', {code: 'ArrowLeft', ctrlKey: true}, '#s_shortcut_mpv_previous_line');
            out.recorded = $('s_shortcut_mpv_previous_line').dataset.shortcutValue;
            await __click('#saveSettings'); await __tick(10);
            return out;
            """
            % json.dumps(list(labels)),
            lang=lang,
            state={
                "settings": {
                    "shortcut_mpv_replay_line": "Ctrl+r",
                    "shortcut_mpv_reset_carried_delay": "Ctrl+z",
                }
            },
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        out = result.value
        assert out["replay_line"] == ["Ctrl+r", text(labels["replay_line"])]
        assert out["reset_carried_delay"] == ["Ctrl+z", text(labels["reset_carried_delay"])]
        for name in ("previous_line", "next_line", "auto_pause"):
            assert out[name] == ["", text(labels[name])]  # unbound until the viewer picks a key
        assert out["recorded"] == "Ctrl+ArrowLeft"
        [[values]] = result.calls_of("save_settings")
        assert values["shortcut_mpv_previous_line"] == "Ctrl+ArrowLeft"
        assert values["shortcut_mpv_replay_line"] == "Ctrl+r" and values["shortcut_mpv_next_line"] == ""
    assert en(labels["next_line"]) != ru(labels["next_line"])
    assert (
        "{delay}" in catalog("ru")["osd.subtitleDelayCarried"]
        and "{key}" in catalog("ru")["osd.subtitleDelayCarried"]
    )


# --- the mpv scripts: behaviour (needs a Lua interpreter) -------------------------------

LINE_PRELUDE = """
sim.set('path', '/media/Hoshi - 03.mkv')
sim.set('pause', false)
sim.set('sub-delay', 0)
sim.set('time-pos', 0)
-- One subtitle state at a time, as mpv reports it.
function show(text, s, e)
    sim.props['sub-start'] = s; sim.props['sub-end'] = e
    sim.set('sub-text', text)
end
function hide() show('', nil, nil) end
function at(t) sim.props['time-pos'] = t end
function resume() sim.set('pause', false) end
function paused() return sim.props['pause'] == true end
"""

LINE_ENV = {
    "PUDGE_SHORTCUT_REPLAY_LINE": "Ctrl+r",
    "PUDGE_SHORTCUT_PREVIOUS_LINE": "Ctrl+ArrowLeft",
    "PUDGE_SHORTCUT_NEXT_LINE": "Ctrl+ArrowRight",
    "PUDGE_SHORTCUT_AUTO_PAUSE": "Ctrl+p",
}


def _lines(tmp_path: Path, scenario: str, env: dict[str, str] | None = None, language: str = "en") -> None:
    run_mpv_scenario(
        tmp_path,
        scenario,
        scripts=[LINE_KEYS],
        env=LINE_ENV if env is None else env,
        prelude=LINE_PRELUDE,
        language=language,
    )


@needs_lua
def test_replay_previous_next_keys(tmp_path: Path) -> None:
    _lines(
        tmp_path,
        """
        -- Named keys from Settings become mpv's names.
        assert(sim.binding_for_key('Ctrl+LEFT') == 'pudge-previous-line')
        assert(sim.binding_for_key('Ctrl+RIGHT') == 'pudge-next-line')
        at(10); show('いち', 10, 12)
        sim.play(1.5)
        assert(sim.press('Ctrl+r'))
        local seeks = sim.commands('^seek ')
        assert(seeks[#seeks] == 'seek 10.000 absolute+exact', seeks[#seeks])
        -- Between lines: the last line shown.
        at(12.4); hide()
        sim.set('pause', true)
        sim.press('Ctrl+r')
        seeks = sim.commands('^seek ')
        assert(#seeks == 2 and seeks[2] == 'seek 10.000 absolute+exact')
        assert(not paused(), 'replay plays the line')
        -- The delay set with z/x moves the line with it.
        sim.set('sub-delay', 0.5)
        at(20.5); show('に', 20, 22)
        sim.press('Ctrl+r')
        seeks = sim.commands('^seek ')
        assert(seeks[#seeks] == 'seek 20.500 absolute+exact', seeks[#seeks])
        -- After the viewer's seek into a gap the old line is not the one to replay.
        sim.fire('seek'); at(300); hide(); sim.fire('playback-restart')
        sim.press('Ctrl+r')
        local steps = sim.commands('^sub%-seek')
        assert(#steps == 1 and steps[1] == 'sub-seek -1', tostring(steps[1]))
        sim.press('Ctrl+LEFT'); sim.press('Ctrl+RIGHT')
        steps = sim.commands('^sub%-seek')
        assert(steps[2] == 'sub-seek -1' and steps[3] == 'sub-seek 1')
        """,
    )


@needs_lua
def test_unbound_keys_and_default(tmp_path: Path) -> None:
    _lines(
        tmp_path,
        """
        local name, binding = sim.binding_for_key('Ctrl+r')
        assert(name == 'pudge-replay-line' and binding.forced)
        for _, unbound in ipairs({'pudge-previous-line', 'pudge-next-line', 'pudge-auto-pause'}) do
            assert(sim.bindings[unbound] and sim.bindings[unbound].key == nil, unbound)
        end
        """,
        env={},
    )
    _lines(
        tmp_path,
        "assert(sim.bindings['pudge-replay-line'].key == nil)",
        env={"PUDGE_SHORTCUT_REPLAY_LINE": ""},
    )


@needs_lua
def test_auto_pause_at_line_end(tmp_path: Path) -> None:
    _lines(
        tmp_path,
        """
        local function osd() return sim.log[#sim.log].value end
        -- Off by default: a line ends, nothing pauses.
        at(10); show('いち', 10, 12); sim.play(2.5); hide()
        assert(not paused(), 'off by default')
        sim.press('Ctrl+p'); assert(osd() == 'Auto-pause at line end: on', osd())
        at(20); show('に', 20, 22)
        sim.play(1.8); assert(not paused(), 'mid-line')
        sim.play(0.2)
        assert(paused(), 'paused just before the end')
        assert(sim.props['time-pos'] < 22 and sim.props['sub-text'] == 'に', 'the line is still on screen')
        -- Resuming plays on: the same line is not paused for twice.
        resume(); sim.play(0.3); hide(); sim.play(0.5)
        assert(not paused(), 'no second pause')
        -- The viewer's own pause is left alone, and so is its end.
        at(30); show('さん', 30, 32); sim.play(1)
        sim.set('pause', true); sim.play(3)
        resume(); sim.play(0.95)
        assert(paused(), 'still pauses at the end after a manual pause')
        resume()
        -- A seek out of a line does not pause; the line it lands in does, at its end.
        at(40); show('よん', 40, 43); sim.play(1)
        sim.fire('seek'); at(42.95); sim.play(0.1); assert(not paused(), 'seeking')
        at(50); show('ご', 50, 52); sim.fire('playback-restart')
        sim.play(1.95); assert(paused(), 'the line after the seek')
        -- Replay of a line already paused for: paused again at its end.
        sim.press('Ctrl+r'); assert(not paused())
        sim.fire('seek'); at(50); sim.fire('playback-restart')
        sim.play(1.95); assert(paused(), 'replayed line')
        -- Next line while paused by auto-pause resumes; off resumes too and stops pausing.
        sim.press('Ctrl+RIGHT'); assert(not paused(), 'next resumes our pause')
        at(60); show('ろく', 60, 62); sim.play(1.95); assert(paused())
        sim.press('Ctrl+p'); assert(osd() == 'Auto-pause at line end: off', osd())
        assert(not paused(), 'turning it off resumes our pause')
        at(70); show('なな', 70, 72); sim.play(2.5); assert(not paused())
        """,
    )


@needs_lua
def test_auto_pause_ignores_overlaps_and_tiny_gaps(tmp_path: Path) -> None:
    _lines(
        tmp_path,
        """
        sim.press('Ctrl+p')
        -- A second line joins the first (same event times, more text), then outlives it:
        -- one pause, at the end of the last one.
        at(10); show('いち', 10, 12); sim.play(1)
        show('いち\\nに', 10, 12)
        sim.play(1.2); assert(not paused(), 'the first of two overlapping lines')
        show('に', 11, 14)
        sim.play(1.5); assert(not paused())
        sim.play(0.3); assert(paused(), 'the end of the overlapping group')
        sim.press('Ctrl+r')
        local seeks = sim.commands('^seek ')
        assert(seeks[#seeks] == 'seek 10.000 absolute+exact', 'the group replays from its first line')
        sim.fire('seek'); sim.fire('playback-restart')
        -- A short interjection over a long line that ends first: the long line still pauses.
        at(20); show('ながい', 20, 24); sim.play(1)
        show('ながい\\nえっ', 20, 24); sim.play(1); show('ながい', 20, 24)
        sim.play(1.95); assert(paused(), 'the long line')
        resume(); sim.play(0.2); hide()
        -- The same text again after a tiny gap is one line: no second pause.
        at(30); show('おなじ', 30, 31); sim.play(0.95); assert(paused())
        resume(); sim.play(0.1); hide()
        at(31.08); show('おなじ', 31.08, 32)
        sim.play(1.2); assert(not paused(), 'continued line')
        hide()
        -- Another text right after is its own line.
        at(40); show('まえ', 40, 41); sim.play(0.95); assert(paused()); resume(); sim.play(0.1)
        at(41.05); show('あと', 41.05, 42); sim.play(0.9); assert(paused(), 'next line')
        resume()
        -- A missed end (no usable sub-end): paused when the line goes away, not after a seek.
        at(50); show('おわり', 50, nil); sim.play(1); at(51.5); hide()
        assert(paused(), 'paused when the line disappeared'); resume()
        at(60); show('とぶ', 60, 62); sim.play(0.5); sim.fire('seek'); at(90); hide()
        assert(not paused(), 'a seek away is not a line end')
        """,
    )


@needs_lua
def test_auto_pause_with_a_subtitle_delay_and_russian_osd(tmp_path: Path) -> None:
    _lines(
        tmp_path,
        """
        sim.set('sub-delay', 2)
        sim.press('Ctrl+p')
        assert(sim.log[#sim.log].value:find('Автопауза', 1, true))
        -- mpv reports subtitle times; the line is on screen 2 s later.
        at(12); show('いち', 10, 13)
        sim.play(1.5); assert(not paused(), 'the raw end is not the end')
        sim.play(1.4); assert(paused())
        assert(math.abs(sim.props['time-pos'] - 14.9) < 0.11, tostring(sim.props['time-pos']))
        """,
        language="ru",
    )


CARRY_ENV = {
    "PUDGE_PLAYBACK_ENABLED": "1",
    "PUDGE_PLAYBACK_VIDEO": "/media/Hoshi - 03.mkv",
    "PUDGE_PLAYBACK_INTERVAL": "10",
    "PUDGE_SUBTITLE_PATH": "/subs/Hoshi - 03.srt",
    "PUDGE_SUBTITLE_DELAY_CARRIED": "0.350",
    "PUDGE_SHORTCUT_RESET_CARRIED_DELAY": "Ctrl+z",
}
CARRY_START = """
sim.set('duration', 1440)
sim.set('time-pos', 100)
sim.set('current-tracks/sub/external-filename', '/subs/Hoshi - 03.srt')
sim.set('sub-delay', 0.35)
sim.fire('file-loaded')
local function osds()
    local out = {}
    for _, row in ipairs(sim.log) do if row.kind == 'osd' then table.insert(out, row.value) end end
    return out
end
"""


@needs_lua
def test_carried_delay_notice_and_reset_key(tmp_path: Path) -> None:
    run_mpv_scenario(
        tmp_path,
        CARRY_START
        + """
        sim.play(2)
        local shown = osds()
        assert(#shown == 1 and shown[1] ==
            'Subtitle delay +0.35 s carried from other episodes — press Ctrl+z to reset', shown[1])
        local before = #sim.commands('%-%-playback%-save')
        assert(sim.press('Ctrl+z'))
        assert(sim.props['sub-delay'] == 0)
        shown = osds()
        assert(shown[#shown] == 'Subtitle delay reset to 0')
        local saves = sim.commands('%-%-playback%-save')
        assert(#saves == before + 1 and saves[#saves]:find('--playback-sub-delay 0.000', 1, true), saves[#saves])
        assert(not sim.press('Ctrl+z'), 'the key is gone after the reset')
        sim.fire('file-loaded'); sim.play(5)
        assert(#osds() == #shown, 'said once')
        """,
        env=CARRY_ENV,
        scripts=[ANILIST],
    )


@needs_lua
def test_carried_delay_changed_by_hand_is_no_longer_reset(tmp_path: Path) -> None:
    run_mpv_scenario(
        tmp_path,
        CARRY_START
        + """
        sim.play(2)
        sim.set('sub-delay', 0.45)   -- x in mpv
        assert(not sim.press('Ctrl+z'), 'the delay is the viewer own now')
        assert(sim.props['sub-delay'] == 0.45)
        """,
        env=CARRY_ENV,
        scripts=[ANILIST],
    )
    # Nothing carried: no notice, no key. No reset key: the notice does not name one.
    run_mpv_scenario(
        tmp_path,
        CARRY_START + "sim.play(5); assert(#osds() == 0); assert(not sim.binding_for_key('Ctrl+z'))",
        env={**CARRY_ENV, "PUDGE_SUBTITLE_DELAY_CARRIED": ""},
        scripts=[ANILIST],
    )
    run_mpv_scenario(
        tmp_path,
        CARRY_START
        + """
        sim.play(2)
        local shown = osds()
        assert(shown[1] == 'Subtitle delay +0.35 s carried from other episodes', shown[1])
        """,
        env={**CARRY_ENV, "PUDGE_SHORTCUT_RESET_CARRIED_DELAY": ""},
        scripts=[ANILIST],
    )
