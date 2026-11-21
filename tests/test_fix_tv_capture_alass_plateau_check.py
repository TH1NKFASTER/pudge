"""TV-timed Japanese file on a WEB video: an accepted speech ALASS map vs the text-clock plateaus.

A TV capture keeps a few seconds after the cold open (and around the breaks) that the
WEB video lacks. The text clock reads it as constant clocks (plateaus); the finest
ALASS map at the configured split penalty passed its own checks and used to win:
one constant shift (the cold open ~5 s off) that also sat ~0.2-0.3 s early (interval
overlap with long TV cues), or a map that dragged the cold open onto 0 s (then the
output guard refused everything and the episode had no subtitles). The plateau map now
wins when the two disagree; an ALASS map that agrees, or one whose moved lines read
their text no better at the plateau times, is kept. Invented data only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge import syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import write_srt
from pudge.subtitles.text_clock_plateaus import fit_text_clock_plateaus

KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
COLD_OPEN = 8
COUNT = 70
COLD_SHIFT = 1.6
MAIN_SHIFT = -3.4


def _text(index: int) -> str:
    return "".join(KANA[(index * 7 + j * 5) % len(KANA)] for j in range(8))


def _shift_for(index: int) -> float:
    return COLD_SHIFT if index < COLD_OPEN else MAIN_SHIFT


@pytest.fixture
def episode(tmp_path: Path):
    # A pause after the cold open: the TV file's extra seconds sit there.
    starts = [10.0 + 4.0 * i + (8.0 if i >= COLD_OPEN else 0.0) for i in range(COUNT)]
    source_cues = [(start, start + 2.5, _text(i)) for i, start in enumerate(starts)]
    source = tmp_path / "source.srt"
    write_srt(source_cues, source)
    # The transcript says the right times: the cold open +1.6 s, the rest -3.4 s.
    reference = tmp_path / "reference.ja.srt"
    write_srt(
        [(s + _shift_for(i), e + _shift_for(i), t) for i, (s, e, t) in enumerate(source_cues)], reference
    )
    plateau = tmp_path / "rejected-plateaus.srt"
    write_srt([(s + _shift_for(i), e + _shift_for(i), t) for i, (s, e, t) in enumerate(source_cues)], plateau)
    text_alignment = {
        "accepted": False,
        "reason": "text_clock_safety_gate_failed",
        "plateau_clock": {
            "accepted": True,
            "output": str(plateau),
            "activity": {"weighted": 0.8},
            "clock_ranges": [
                {"first_cue": 0, "last_cue": COLD_OPEN - 1, "shift_seconds": COLD_SHIFT},
                {"first_cue": COLD_OPEN, "last_cue": COUNT - 1, "shift_seconds": MAIN_SHIFT},
            ],
        },
    }

    def alass_output(name: str, shift_of) -> Path:
        out = tmp_path / name
        write_srt([(s + shift_of(i), e + shift_of(i), t) for i, (s, e, t) in enumerate(source_cues)], out)
        return out

    return source, reference, plateau, text_alignment, alass_output


def test_constant_alass_shift_that_misses_the_cold_open_and_reads_early_disagrees(episode) -> None:
    source, reference, _plateau, text_alignment, alass_output = episode
    aligned = alass_output("alass.srt", lambda _i: MAIN_SHIFT - 0.25)
    check = syncing._alass_plateau_disagreement(source, aligned, reference, text_alignment)
    assert check["disagrees"] is True
    assert check["missed_cues"] == COLD_OPEN
    assert set(check["reasons"]) == {"alass_moves_plateau_clock", "alass_offset_from_plateau_onsets"}
    assert check["median_offset_seconds"] == pytest.approx(-0.25, abs=0.01)


def test_constant_offset_alone_is_a_disagreement(episode) -> None:
    source, reference, _plateau, text_alignment, alass_output = episode
    aligned = alass_output("alass.srt", lambda i: _shift_for(i) - 0.3)
    check = syncing._alass_plateau_disagreement(source, aligned, reference, text_alignment)
    assert check["reasons"] == ["alass_offset_from_plateau_onsets"]


def test_an_agreeing_alass_map_is_no_disagreement(episode) -> None:
    source, reference, _plateau, text_alignment, alass_output = episode
    aligned = alass_output("alass.srt", lambda i: _shift_for(i) + 0.05)
    check = syncing._alass_plateau_disagreement(source, aligned, reference, text_alignment)
    assert check["available"] is True
    assert check["disagrees"] is False


def test_moved_lines_whose_text_is_no_better_at_the_plateau_times_keep_alass(episode, tmp_path: Path) -> None:
    source, _reference, _plateau, text_alignment, alass_output = episode
    # This transcript has the cold open where ALASS put it (main clock): the text cannot
    # prefer the plateau times for those lines, so the moved piece is not held against ALASS.
    source_cues = syncing.parse_srt(source)
    reference = tmp_path / "reference-main.ja.srt"
    write_srt([(s + MAIN_SHIFT, e + MAIN_SHIFT, t) for s, e, t in source_cues], reference)
    aligned = alass_output("alass.srt", lambda _i: MAIN_SHIFT)
    check = syncing._alass_plateau_disagreement(source, aligned, reference, text_alignment)
    assert check["missed_cues"] == COLD_OPEN
    assert "alass_moves_plateau_clock" not in check["reasons"]
    assert check["disagrees"] is False


def test_without_clock_ranges_there_is_no_check(episode) -> None:
    source, reference, _plateau, text_alignment, alass_output = episode
    del text_alignment["plateau_clock"]["clock_ranges"]
    aligned = alass_output("alass.srt", lambda _i: MAIN_SHIFT)
    check = syncing._alass_plateau_disagreement(source, aligned, reference, text_alignment)
    assert check["available"] is False


def _run_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, episode, shift_of):
    source, reference, plateau, text_alignment, alass_output = episode
    monkeypatch.setattr(syncing, "prepare_japanese_stt_reference", lambda *_a, **_k: (reference, {}))
    monkeypatch.setattr(syncing, "_stt_text_clock_candidate", lambda *_a, **_k: (None, text_alignment))
    calls: list[float] = []

    def fake_alass(_reference, _subtitle, _cache, config, **_kwargs):
        calls.append(float(config.alass_split_penalty))
        out = alass_output(f"alass-{config.alass_split_penalty}.srt", shift_of)
        return out, {"reason": "applied", "sync_was_successful": True, "alass_blocks": 1}

    monkeypatch.setattr(syncing, "synchronize_with_alass", fake_alass)
    monkeypatch.setattr(
        syncing, "compare_timing_activity", lambda *_a, **_k: {"available": True, "weighted": 0.9}
    )
    monkeypatch.setattr(syncing, "_stt_alass_transition_safety", lambda *_a, **_k: {"accepted": True})
    monkeypatch.setattr(syncing, "_stt_alass_map_safe", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(
        syncing, "_refine_stt_opening_plateaus", lambda _s, aligned, *_a, **_k: (aligned, {"applied": False})
    )
    output, result = syncing._try_japanese_stt_fallback(
        tmp_path / "video.mkv",
        source,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    return output, result, plateau, calls


def test_plateau_map_wins_over_a_disagreeing_finest_alass_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, episode
) -> None:
    output, result, plateau, calls = _run_fallback(
        tmp_path, monkeypatch, episode, lambda _i: MAIN_SHIFT - 0.25
    )
    assert output == plateau
    assert result["engine"] == "japanese-stt+text-clock-plateaus"
    assert result["sync_was_successful"] is True
    assert calls == [7.0]
    attempt = result["stt_alass_attempts"][0]
    assert attempt["superseded_by"] == "stt_text_clock_plateaus"
    assert attempt["plateau_check"]["disagrees"] is True


def test_an_agreeing_finest_alass_map_is_still_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, episode
) -> None:
    output, result, plateau, _calls = _run_fallback(
        tmp_path, monkeypatch, episode, lambda i: _shift_for(i) + 0.05
    )
    assert output != plateau
    assert result["engine"] == "japanese-stt+alass"
    assert result["stt_alass_attempts"][0]["plateau_check"]["disagrees"] is False


def test_squeezed_lines_far_outside_both_clocks_take_the_nearer_clock() -> None:
    # Two clocks with a backward change (the source has 5 s the video lacks after a cold
    # open); between them a few lines whose transcript match is garbled (OP lyrics): their
    # text-mapped onsets lie 10-80 s away. They used to keep those onsets.
    starts = [5.0 * i for i in range(40)]
    ends = [value + 2.0 for value in starts]
    shifts: list[float | None] = [1.6] * 10 + [-12.0, -40.0, -75.0, 0.2] + [-3.4] * 26
    fit = fit_text_clock_plateaus(starts, ends, shifts)
    assert fit["accepted"] is True
    repaired = fit["shifts"]
    assert repaired[:10] == [pytest.approx(1.6)] * 10
    assert repaired[10:13] == [pytest.approx(-3.4)] * 3  # far below both clocks: the lower one
    assert repaired[13] == pytest.approx(0.2)  # between the clocks: kept as mapped
    assert repaired[14:] == [pytest.approx(-3.4)] * 26


def test_accepted_plateau_clock_reports_its_final_clock_ranges(tmp_path: Path) -> None:
    from tests.test_fix_tv_capture_text_clock import _write_episode

    source, reference = _write_episode(tmp_path)
    _output, diagnostics = syncing._stt_text_clock_candidate(source, reference, tmp_path, model="test")
    plateau = diagnostics["plateau_clock"]
    assert plateau["accepted"] is True
    ranges = plateau["clock_ranges"]
    assert [(r["first_cue"], r["last_cue"]) for r in ranges] == [(0, 29), (30, 59)]
    assert ranges[0]["shift_seconds"] == pytest.approx(130.0, abs=0.35)
    assert ranges[1]["shift_seconds"] == pytest.approx(290.0, abs=0.35)
