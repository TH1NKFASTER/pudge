"""No trustworthy time map, exact episode: raw subtitles "not aligned" instead of nothing.

BLUE REFLECTION RAY E07 (sync benchmark): TV-cut subtitles for a BD video; every ALASS map was
an unconfirmed discontinuity, raw fallback was refused for all, and the episode had no
subtitles at all ("waiting_subtitles").
"""

from __future__ import annotations

from pathlib import Path

from pudge import syncing
from pudge.cli import _confident_raw_unsynced_candidate
from pudge.models import SubtitleCandidate


def _candidate(tmp_path: Path, name: str, episode_match: str) -> SubtitleCandidate:
    path = tmp_path / name
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\n台詞\n", encoding="utf-8")
    return SubtitleCandidate(
        path=path,
        source="jimaku",
        score=103.0,
        name=name,
        episode=7,
        verified_japanese=True,
        details={"episode_match": episode_match, "entry_anilist_match": True},
    )


def _result(candidate: SubtitleCandidate, result: dict) -> dict:
    return {
        "quality_fallback_attempts": [
            {
                "path": str(candidate.path),
                "raw_fallback_disqualified": syncing.raw_fallback_disqualified(result),
                "raw_fallback_needs_exact_identity": syncing.raw_fallback_needs_exact_identity(result),
            }
        ]
    }


def test_rejected_discontinuous_map_still_offers_the_exact_episode_raw(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "[ASW] Show - 07v2 [Retimed From TV].srt", "exact")
    rejected = {"reference_discontinuity_rejected": True, "reason": "embedded_reference_output_invalid"}
    chosen = _confident_raw_unsynced_candidate(
        [candidate], expected_episode=7, manually_selected=False, minimum_score=50.0,
        alignment_result=_result(candidate, rejected),
    )  # fmt: skip
    assert chosen is candidate


def test_without_an_exact_identity_the_raw_file_is_still_withheld(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "Show - 07.srt", "absolute")
    rejected = {"reference_discontinuity_rejected": True}
    chosen = _confident_raw_unsynced_candidate(
        [candidate], expected_episode=7, manually_selected=False, minimum_score=50.0,
        alignment_result=_result(candidate, rejected),
    )  # fmt: skip
    assert chosen is None


def test_a_destroyed_map_still_withholds_the_raw_file(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "Show - 07.srt", "exact")
    rejected = {"reference_discontinuity_rejected": True, "alignment_output_guard": {"status": "rejected"}}
    chosen = _confident_raw_unsynced_candidate(
        [candidate], expected_episode=7, manually_selected=False, minimum_score=50.0,
        alignment_result=_result(candidate, rejected),
    )  # fmt: skip
    assert chosen is None


def test_the_phone_is_told_the_subtitles_are_not_aligned(tmp_path: Path) -> None:
    from pudge.iphone_media import IPhoneMedia

    media = object.__new__(IPhoneMedia)
    media.video_jobs = {}
    media._prepared_video = lambda *a, **k: None  # type: ignore[method-assign]
    row = {
        "id": 3, "media_id": 9, "number": 7, "number_end": None, "anime_title": "Show", "title": "",
        "state": "couldnt_sync", "playback_position": 0, "playback_duration": 0,
        "subtitle_path": str(tmp_path / "raw.srt"), "updated_at": 0,
    }  # fmt: skip
    assert media._episode_item(row)["subtitleNotAligned"] is True
    assert media._episode_item({**row, "state": "ready"})["subtitleNotAligned"] is False


def _timed_srt(path: Path, starts: list[float], shift: float) -> Path:
    blocks = []
    for index, start in enumerate(starts, 1):
        begin, end = start + shift, start + shift + 1.2
        stamp = lambda value: (
            "%02d:%02d:%02d,%03d"
            % (  # noqa: E731
                value // 3600,
                value % 3600 // 60,
                value % 60,
                round(value % 1 * 1000) % 1000,
            )
        )
        blocks.append(f"{index}\n{stamp(begin)} --> {stamp(end)}\n台詞{index}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _speech_starts() -> list[float]:
    starts, value, seed = [], 20.0, 7
    while value < 1300.0:
        seed = (seed * 1103515245 + 12345) % 2**31
        value += 1.6 + (seed % 1000) / 1000.0 * 5.5
        starts.append(round(value, 2))
    return starts


def _exact_pair(
    tmp_path: Path, far_shift: float, close_shift: float
) -> tuple[SubtitleCandidate, SubtitleCandidate, dict]:
    """Two exact-episode files whose maps were both rejected; the first listed is far off."""
    starts = _speech_starts()
    reference = _timed_srt(tmp_path / "reference.srt", starts, 0.0)
    far = _candidate(tmp_path, "[TV] Show - 07.srt", "exact")
    close = _candidate(tmp_path, "[ASW] Show - 07 [Retimed From TV].srt", "exact")
    _timed_srt(far.path, starts, far_shift)
    _timed_srt(close.path, starts, close_shift)
    rejected = {"reference_discontinuity_rejected": True, "reason": "embedded_reference_output_invalid"}
    attempts = (
        _result(far, rejected)["quality_fallback_attempts"]
        + _result(close, rejected)["quality_fallback_attempts"]
    )
    return far, close, {"timing_reference": str(reference), "quality_fallback_attempts": attempts}


def test_the_raw_fallback_prefers_the_exact_file_whose_timing_is_close(tmp_path: Path) -> None:
    far, close, result = _exact_pair(tmp_path, far_shift=93.8, close_shift=0.4)
    chosen = _confident_raw_unsynced_candidate(
        [far, close], expected_episode=7, manually_selected=False, minimum_score=50.0, alignment_result=result,
    )  # fmt: skip
    assert chosen is close


def test_without_timing_evidence_the_raw_fallback_keeps_the_listed_order(tmp_path: Path) -> None:
    far, close, result = _exact_pair(tmp_path, far_shift=93.8, close_shift=0.4)
    result.pop("timing_reference")
    chosen = _confident_raw_unsynced_candidate(
        [far, close], expected_episode=7, manually_selected=False, minimum_score=50.0, alignment_result=result,
    )  # fmt: skip
    assert chosen is far
