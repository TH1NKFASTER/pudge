"""Ctrl+A in mpv counts the episode on AniList (behaviour, mock mpv; audit 71Б rewrite)."""

from __future__ import annotations

from pathlib import Path

from test_anilist_lua import of, tracker
from tests.i18n_support import en, ru
from tests.lua_harness import needs_lua

PLAYBACK = {"PUDGE_PLAYBACK_ENABLED": "1", "PUDGE_PLAYBACK_VIDEO": "/media/Show - 05.mkv"}
PRESS = """
sim.set('duration', 1440); sim.set('time-pos', 600)
sim.fire('file-loaded')
sim.play(1)
mark('forced', select(2, sim.binding_for_key('Ctrl+a')).forced)
mark('taken', sim.press('Ctrl+a'))
"""


@needs_lua
def test_ctrl_a_is_a_forced_binding_that_counts_with_feedback_and_saves_position(tmp_path: Path) -> None:
    # A plain binding could be shadowed by input.conf or another script and Ctrl+A did nothing.
    for language, text in (("en", en), ("ru", ru)):
        env = {
            "PUDGE_ANILIST_TRACKING_FILE": str(tmp_path / "tracking.json"),
            "PUDGE_ANILIST_AUTO_UPDATE": "0",
            **PLAYBACK,
        }
        log = tracker(tmp_path / language, PRESS, env, language)
        assert "forced=true" in of(log, "mark") and "taken=true" in of(log, "mark")
        # Feedback at once, then the position is saved, then the helper counts the episode.
        rows = [(kind, value) for kind, value in log if kind in ("osd", "subprocess")]
        assert rows[0] == ("osd", text("osd.anilistManualMode"))
        assert rows[1] == ("osd", text("osd.anilistCountingEntry"))
        assert "--playback-save" in rows[2][1] and "--playback-position 601.000" in rows[2][1]
        assert "--anilist-action update" in rows[3][1] and rows[3][1].endswith("--manual")
        assert rows[4] == ("osd", text("osd.anilistUpdated"))
    assert ru("osd.anilistCountingEntry") == "AniList: засчитываю серию…"


@needs_lua
def test_missing_tracking_file_is_visible_instead_of_silent(tmp_path: Path) -> None:
    for language, text in (("en", en), ("ru", ru)):
        log = tracker(tmp_path / language, PRESS, {"PUDGE_ANILIST_TRACKING_FILE": "", **PLAYBACK}, language)
        assert of(log, "osd") == [text("osd.anilistCountingEntry"), text("osd.anilistTrackingUnavailable")]
        assert of(log, "subprocess", "--anilist-action") == []  # no helper is started
        assert of(log, "msg.warn") == ["AniList manual update unavailable: tracking file is empty"]
    assert en("osd.anilistTrackingUnavailable") == "AniList: tracking is unavailable for this file"
    assert ru("osd.anilistTrackingUnavailable") == "AniList: трекер недоступен для этого файла"
