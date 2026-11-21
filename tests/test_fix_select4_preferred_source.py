"""SELECT4 (onset-select-3): the file timed to a WEB release (then BD, TV), an SRT, is the preferred start.

When Pudge's accepted file is not the preferred one, the preferred file is synced instead of Pudge's
own second choice and kept unless the accepted file matches more speech onsets; failures keep the
accepted file. Invented names and timings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.models import SubtitleCandidate
from pudge.subtitles import candidate_choice as cc

WEB = "Show.S01E05.WEBRip.Netflix.ja[cc].srt"
BD = "[Grp] Show - 05 [BD 1080p].ass"
TV = "Show - 05 (TBS 1920x1080 x264 AAC).srt"
UNKNOWN = "[Grp] Show - 05.srt"


def _cand(name: str, score: float = 100.0) -> SubtitleCandidate:
    return SubtitleCandidate(path=Path("/x") / name, source="jimaku", score=score, name=name)


def test_source_ranks_from_the_file_name():
    assert cc.source_rank(WEB) == "WEB" and cc.source_rank("Show.S01E05.Amazon.ja-jp[sdh].srt") == "WEB"
    assert cc.source_rank(BD) == "BD" and cc.source_rank(TV) == "TV" and cc.source_rank(UNKNOWN) == "?"
    assert cc.source_rank("Show - 05 [BD][Retimed from TV].srt") == "BD"


def test_the_preferred_file_is_web_then_bd_then_tv_then_srt_then_score():
    assert cc.preferred_candidate([_cand(TV, 130), _cand(BD, 120), _cand(WEB, 90)]).name == WEB
    assert cc.preferred_candidate([_cand(TV, 130), _cand(BD, 90)]).name == BD
    ass = _cand("Show.S01E05.WEBRip.Netflix.ja.ass", 140)
    assert cc.preferred_candidate([ass, _cand(WEB, 90)]).name == WEB  # SRT before score
    first, second = _cand(UNKNOWN, 100), _cand("[Other] Show - 05.srt", 100)
    assert cc.preferred_candidate([first, second]) is first  # equals keep Pudge's order


def _sig(fraction, matched=100):
    return {"matched_fraction": fraction, "matched_cues": matched}


def test_the_preferred_file_stays_unless_the_accepted_one_matches_more_onsets():
    assert cc.choose_preferred(_sig(0.70, 300), _sig(0.70, 300)) == (True, "preferred_source")
    assert cc.choose_preferred(_sig(0.70, 300), _sig(0.80, 310)) == (False, "accepted_matches_more_onsets")
    # more onsets on fewer lines is not more: the preferred file stays
    assert cc.choose_preferred(_sig(0.70, 300), _sig(0.80, 250))[0] is True
    assert cc.choose_preferred(None, _sig(0.5)) == (False, "preferred_unavailable")
    assert cc.choose_preferred(_sig(None), _sig(0.5)) == (False, "preferred_unavailable")


# ---------------------------------------------------------------------------- integration

from test_fix_select_onset_choice import world  # noqa: E402,F401 - the same fakes


def test_the_preferred_file_is_synced_instead_of_the_next_choice(world):  # noqa: F811
    run, state, onset_lead_paths = world
    selected, path, result = run((TV, 40), (UNKNOWN, 60), (WEB, 40))
    assert selected.name == WEB and path == state["outputs"][WEB] and onset_lead_paths == [path]
    names, kwargs = state["passes"][1]
    assert names == [WEB] and kwargs["quality_attempt_limit"] == 1
    record = result["candidate_onset_selection"]
    assert record["version"] == "onset-select-3" and record["mode"] == "preferred_source"
    assert record["decision"] == "second" and record["reason"] == "preferred_source"
    assert record["first"]["source"] == "TV" and record["second"]["source"] == "WEB"


def test_the_accepted_file_stays_when_it_matches_more_onsets(world):  # noqa: F811
    run, state, _ = world
    selected, _path, result = run((TV, 50), (WEB, 30))
    assert selected.name == TV
    assert result["candidate_onset_selection"]["reason"] == "accepted_matches_more_onsets"


def test_a_rejected_preferred_file_keeps_the_accepted_one(world):  # noqa: F811
    run, state, _ = world
    state["rejected"].add(WEB)
    selected, _path, result = run((TV, 30), (WEB, 60))
    assert selected.name == TV
    assert result["candidate_onset_selection"]["reason"] == "preferred_unavailable"


def test_an_accepted_preferred_file_keeps_the_next_choice_check(world):  # noqa: F811
    run, state, _ = world
    selected, _path, result = run((WEB, 30), (TV, 54))
    record = result["candidate_onset_selection"]
    assert record["mode"] == "next_choice" and selected.name == TV
    assert record["reason"] == "second_matches_more_onsets"


@pytest.mark.parametrize("onsets", [58, 60])
def test_the_preferred_file_is_synced_even_when_the_accepted_one_looks_perfect(world, onsets):  # noqa: F811
    run, state, _ = world
    selected, _path, result = run((TV, onsets), (WEB, 30))
    assert len(state["passes"]) == 2 and selected.name == TV
