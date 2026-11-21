"""A timing far ahead of the voice moves later, to the usual Japanese lead.

Gold Japanese tracks lead their speech onsets by 0.08-0.27 s; outputs synced on an English
track sometimes led by 0.3-0.45 s and the onset-lead step only ever moved lines earlier.
Invented data only."""

from __future__ import annotations

from tests.test_fix_onset_lead import _cues, _speech, _voice_onsets

from pudge.subtitles import onset_lead


def _measure(lead: float) -> dict[str, object]:
    onsets = _voice_onsets()
    frames = _speech(onsets, 700)
    cues = _cues(onsets, lead=lead)
    return onset_lead.onset_lead([c[0] for c in cues], onset_lead.speech_onsets(frames))


def test_a_lead_beyond_any_gold_track_moves_later() -> None:
    shift = onset_lead.lead_correction(_measure(0.42))
    assert shift == -onset_lead.MAX_CORRECTION_SECONDS
    moved = onset_lead.shift_earlier([(10.0, 12.0, "a")], shift)
    assert moved == [
        (10.0 + onset_lead.MAX_CORRECTION_SECONDS, 12.0 + onset_lead.MAX_CORRECTION_SECONDS, "a")
    ]


def test_a_lead_just_over_the_limit_moves_to_the_target() -> None:
    measurement = _measure(0.31)
    shift = onset_lead.lead_correction(measurement)
    assert shift < 0.0
    assert abs(-shift - (float(measurement["lead_seconds"]) - onset_lead.TARGET_LEAD_SECONDS)) < 0.002


def test_leads_within_the_gold_range_are_kept() -> None:
    for lead in (0.12, 0.2, 0.26):
        assert onset_lead.lead_correction(_measure(lead)) == 0.0
