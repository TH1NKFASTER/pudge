from __future__ import annotations

import pytest

from pudge.syncing import subtitle_quality_accepted
from pudge.i18n import message


def _result(diagnostics):
    return {
        "sync_was_successful": True,
        "segment_diagnostics": {
            "available": True,
            "reliable": False,
            "quality_reason": "too_many_boundary_hits",
            **diagnostics,
        },
        "candidate_context": {
            "source": "jimaku",
            "filename_score": 128.0,
            "episode_match": "exact",
            "title_similarity": 0.0,
            "entry_anilist_match": True,
        },
    }


def test_wrong_episode_with_exact_name_is_not_accepted_as_broadcast_edges():
    # Real Seihantai S2E13 log: the "S02E13" file was the sequel's first episode.
    accepted, reason = subtitle_quality_accepted(
        _result(
            {
                "attempted_segments": 20,
                "successful_segments": 7,
                "successful_ratio": 0.35,
                "edge_hit_ratio": 0.8571,
                "spans_full_search": True,
                "mean_abs_offset_seconds": 492.77,
            }
        )
    )
    assert accepted is False
    assert reason == "too_many_boundary_hits"


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"successful_ratio": 0.4},
        {"edge_hit_ratio": 0.6},
        {"spans_full_search": True},
    ],
)
def test_each_global_failure_signal_blocks_the_identity_exception(diagnostics):
    assert subtitle_quality_accepted(_result(diagnostics))[0] is False


def test_real_broadcast_edge_mismatch_still_accepted():
    accepted, reason = subtitle_quality_accepted(
        _result(
            {
                "successful_ratio": 0.85,
                "edge_hit_ratio": 0.2,
                "spans_full_search": False,
            }
        )
    )
    assert accepted is True
    # Audit 68Б: the reason is English (error.*); the window shows it in its language.
    assert reason == "exact Jimaku episode; only broadcast boundaries differ"
    assert message(reason, "en") == reason
    assert message(reason, "ru") == "точная серия Jimaku; различаются только границы эфира"


# Reasons stored in Russian by older versions (audit 68Б: new ones are English).
OLD_REASONS = """
__api.anime_debug_snapshot = async mediaId => ({
  anime: {media_id: mediaId, title: 'Show', episodes: 12, format: 'TV'}, selected_episode: 3,
  summary: {diagnosis: {reason: 'тайминг разрушен при выравнивании: ok'}},
  subtitle_selection: {selected: {quality_reason: 'точная серия Jimaku; различаются только границы эфира'},
                       latest_history: {reason: 'тайминг разрушен при выравнивании: ok'}},
});
"""


def test_debug_window_shows_quality_reasons_in_the_interface_language(page) -> None:
    # Audit 68Б: ui_localization.js was replaced by the catalogs + PudgeI18n.message.
    shown = {}
    for lang in ("en", "ru"):
        result = page.run(
            """
            void PudgeDebug.open({media_id: 7, title: 'Show'});
            await __tick(10);
            const summary = __text('#pudgeDebugBody');
            await __click('[data-debug-tab=subtitles]');
            return {summary, subtitles: __text('#pudgeDebugBody')};
            """,
            lang=lang,
            before=OLD_REASONS,
            api={"startup_maintenance": {"running": False}, "ui_state_versions": {"ready": "1", "ui": "1"}},
        )
        shown[lang] = result.value
    en, ru = shown["en"], shown["ru"]
    assert "timing was broken by alignment: ok" in en["summary"]  # the diagnosis (JSON block)
    assert "exact Jimaku episode; only broadcast boundaries differ" in en["subtitles"]  # a key/value row
    assert "timing was broken by alignment: ok" in en["subtitles"]
    assert "тайминг" not in en["summary"] + en["subtitles"] and "точная" not in en["subtitles"]
    assert "точная серия Jimaku; различаются только границы эфира" in ru["subtitles"]
    assert "тайминг разрушен при выравнивании: ok" in ru["summary"]
