"""The mpv AniList tracker (pudge_anilist.lua) under the mock mpv (behaviour; audit 71Б rewrite):
automatic vs manual counting, its hotkeys, and the cleanup of the first external subtitle cue."""

from __future__ import annotations

from pathlib import Path

from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario

# Marks go into the mock's log, so they are ordered with OSD messages and commands.
PRELUDE = r"""
function mark(name, value) table.insert(sim.log, {kind = 'mark', value = name .. '=' .. tostring(value)}) end
"""
DUMP = r"""
for _, row in ipairs(sim.log) do print('LOG\t' .. row.kind .. '\t' .. (tostring(row.value):gsub('\n', '\\n'))) end
"""


def tracker(
    tmp_path: Path, scenario: str, env: dict[str, str], language: str = "en"
) -> list[tuple[str, str]]:
    """Runs the tracker; returns its log: (kind, value) rows: osd, command, command_native,
    subprocess, msg.*, mark."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = run_mpv_scenario(tmp_path, PRELUDE + scenario + DUMP, scripts=[ANILIST], env=env, language=language)
    rows = [line.split("\t", 2) for line in out.splitlines() if line.startswith("LOG\t")]
    return [(kind, value) for _log, kind, value in rows]


def of(log: list[tuple[str, str]], kind: str, needle: str = "") -> list[str]:
    return [value for k, value in log if k == kind and needle in value]


def tracking_env(tmp_path: Path, *, auto: bool) -> dict[str, str]:
    return {
        "PUDGE_ANILIST_TRACKING_FILE": str(tmp_path / "tracking.json"),
        "PUDGE_ANILIST_AUTO_UPDATE": "1" if auto else "0",
    }


# 90 % of a 24-minute episode watched, 2 min 20 s left: past the automatic threshold.
NEAR_END = """
sim.set('duration', 1440); sim.set('time-pos', 1300)
sim.fire('file-loaded')
"""


@needs_lua
def test_tracker_counts_automatically_only_when_auto_update_is_on(tmp_path: Path) -> None:
    auto = tracker(tmp_path / "auto", NEAR_END + "sim.play(25)", tracking_env(tmp_path, auto=True))
    updates = of(auto, "subprocess", "--anilist-action update")
    assert len(updates) == 1 and "--manual" not in updates[0]  # counted once, not on every tick
    assert of(auto, "osd") == [en("osd.anilistUpdated")]

    for language, text in (("en", en), ("ru", ru)):
        manual = tracker(
            tmp_path / language, NEAR_END + "sim.play(30)", tracking_env(tmp_path, auto=False), language
        )
        assert of(manual, "subprocess", "--anilist-action") == []
        assert of(manual, "osd") == [text("osd.anilistManualMode")]
    assert ru("osd.anilistManualMode").startswith("AniList: ручной режим")


@needs_lua
def test_manual_hotkeys_work_when_auto_update_is_off(tmp_path: Path) -> None:
    # Configured keys replace the defaults; the removed "correct match" (c) and
    # "open AniList" (Ctrl+b) hotkeys no longer take keys away from mpv.
    env = {
        **tracking_env(tmp_path, auto=False),
        "PUDGE_SHORTCUT_MARK_WATCHED": "Ctrl+Shift+w",
        "PUDGE_SHORTCUT_TRANSLATE_SUBTITLE": "Alt+t",
        "PUDGE_SHORTCUT_CORRECT_MATCH": "c",
        "PUDGE_SHORTCUT_OPEN_ANILIST": "Ctrl+b",
    }
    log = tracker(
        tmp_path,
        r"""
        sim.set('duration', 1440); sim.set('time-pos', 300)
        sim.fire('file-loaded')
        local _, binding = sim.binding_for_key('Ctrl+Shift+w')
        mark('mark_forced', binding and binding.forced)
        mark('default_free', sim.binding_for_key('Ctrl+a') == nil)
        mark('c', sim.press('c')); mark('ctrl_b', sim.press('Ctrl+b'))
        sim.press('Ctrl+Shift+w')
        sim.set('sub-text', 'こんにちは')
        sim.press('Alt+t')
    """,
        env,
    )
    marks = of(log, "mark")
    assert "mark_forced=true" in marks and "default_free=true" in marks
    assert "c=false" in marks and "ctrl_b=false" in marks  # passed through to mpv
    [update] = of(log, "subprocess", "--anilist-action update")
    assert update.endswith("--manual")
    [translate] = of(log, "subprocess", "--subtitle-translate")
    assert "--subtitle-study-text こんにちは" in translate


SUBTITLE_ENV = {"PUDGE_ANILIST_TRACKING_FILE": ""}


def first_cue(track: str) -> str:
    """The first cue (0.1–3.0 s) of the selected track; marks before and after its end."""
    return r"""
        sim.set('track-list', TRACK)
        sim.set('sid', 1)
        sim.set('secondary-sid', 2); sim.set('secondary-sub-visibility', true)
        sim.set('sub-visibility', true)
        sim.set('time-pos', 0)
        sim.fire('file-loaded')
        mark('secondary', tostring(sim.props['secondary-sid']) .. '/' .. tostring(sim.props['secondary-sub-visibility']))
        sim.play(0.1)
        sim.set('sub-end/full', 3.0); sim.set('sub-text', 'first line')
        sim.play(2.5)
        mark('cue_shown_visible', sim.props['sub-visibility'])
        sim.play(0.5)
        mark('after_end_visible', sim.props['sub-visibility'])
        sim.play(0.2)
        mark('restored_visible', sim.props['sub-visibility'])
        sim.set('sub-end/full', 6.0); sim.set('sub-text', 'second line')
        sim.play(4)
        mark('sid', sim.props['sid'])
    """.replace("TRACK", track)


@needs_lua
def test_first_external_text_cue_is_cleaned_once_just_after_its_end(tmp_path: Path) -> None:
    # mpv/libass can leave the first external text cue painted after it ends: the track
    # is reloaded once, after (not before) that end, hidden meanwhile, and re-seeked.
    log = tracker(
        tmp_path, first_cue("{{type = 'sub', id = 1, external = true, image = false}}"), SUBTITLE_ENV
    )
    rows = [f"{kind}:{value}" for kind, value in log if kind in ("mark", "command_native", "command")]
    reload = rows.index("command_native:sub-reload 1")
    assert rows.index("mark:cue_shown_visible=true") < reload < rows.index("mark:after_end_visible=false")
    seek = rows[reload + 1].split()
    assert seek[0] == "command:seek" and seek[2] == "absolute+exact"
    assert 3.0 < float(seek[1]) < 3.2  # the current position, after the cue
    assert "mark:restored_visible=true" in rows
    assert of(log, "command_native").count("sub-reload 1") == 1  # the second cue is not reloaded
    assert "sid=1" in of(log, "mark")  # never switches to another track
    assert "secondary=no/false" in of(log, "mark")  # secondary subtitles are off


@needs_lua
def test_embedded_or_bitmap_subtitles_are_never_reloaded(tmp_path: Path) -> None:
    for name, track in (
        ("embedded", "{{type = 'sub', id = 1, external = false}}"),
        ("bitmap", "{{type = 'sub', id = 1, external = true, image = true}}"),
    ):
        log = tracker(tmp_path / name, first_cue(track), SUBTITLE_ENV)
        assert of(log, "command_native") == [], name
        assert of(log, "command", "seek") == [], name
        assert "after_end_visible=true" in of(log, "mark") and "sid=1" in of(log, "mark")
