from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import _prune_rejoining_local_rate_outliers


def _spice_wolf_v1_clock() -> list[dict[str, float]]:
    return [
        {"offset": 8.0, "time": 311.228},
        {"offset": 19.999, "time": 312.528},
        {"offset": 20.0, "time": 313.028},
        {"offset": 24.999, "time": 313.728},
        {"offset": 25.0, "time": 314.548},
        {"offset": 32.999, "time": 315.308},
        {"offset": 33.0, "time": 315.868},
        {"offset": 44.0, "time": 319.928},
    ]


def test_real_v1_clock_drops_only_transient_rate_outliers() -> None:
    rows, debug = _prune_rejoining_local_rate_outliers(
        _spice_wolf_v1_clock(), max_rate=7.512
    )

    assert [(float(row["offset"]), float(row["time"])) for row in rows] == [
        (8.0, 311.228),
        (20.0, 313.028),
        (24.999, 313.728),
        (25.0, 314.548),
        (33.0, 315.868),
        (44.0, 319.928),
    ]
    assert debug["dropped"] == 2
    assert debug["max_rate"] == pytest.approx(7.512)
    assert [row["offset"] for row in debug["anchors"]] == [19.999, 32.999]
    assert debug["anchors"][0]["rate"] == pytest.approx(9.23, abs=0.001)
    assert debug["anchors"][1]["rate"] == pytest.approx(10.525, abs=0.001)


def test_rate_guard_preserves_unsupported_tail_instead_of_inventing_clock() -> None:
    rows = [
        {"offset": 8.0, "time": 10.0},
        {"offset": 30.0, "time": 11.0},
        {"offset": 50.0, "time": 12.0},
    ]

    repaired, debug = _prune_rejoining_local_rate_outliers(rows, max_rate=7.0)

    assert repaired == rows
    assert debug["dropped"] == 0


def test_rate_guard_keeps_already_plausible_dense_clock_byte_for_byte() -> None:
    rows = [
        {"offset": 8.0, "time": 10.0},
        {"offset": 14.0, "time": 11.0},
        {"offset": 20.0, "time": 12.2},
        {"offset": 31.0, "time": 14.5},
    ]

    repaired, debug = _prune_rejoining_local_rate_outliers(rows, max_rate=7.0)

    assert repaired == rows
    assert debug["dropped"] == 0


def test_rate_guard_preserves_non_monotonic_tail_for_existing_safeguards() -> None:
    rows = [
        {"offset": 8.0, "time": 10.0},
        {"offset": 14.0, "time": 11.0},
        {"offset": 13.0, "time": 12.0},
        {"offset": 20.0, "time": 13.0},
    ]

    repaired, debug = _prune_rejoining_local_rate_outliers(rows, max_rate=7.0)

    assert repaired == rows
    assert debug["dropped"] == 0


def test_local_rate_guard_bumps_persisted_alignment_revision() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "pudge" / "audiobooks.py").read_text(
        encoding="utf-8"
    )
    assert '_READING_AUDIO_ALIGNMENT_REVISION = "reading-audio-v3-leading-prefix-v22"' in source

    fingerprint = source.split("def _alignment_fingerprint", 1)[1].split(
        "def _alignment_path", 1
    )[0]
    assert "_READING_AUDIO_ALIGNMENT_REVISION" in fingerprint

    migration = source.split("def _migrate_compatible_alignment", 1)[1].split(
        "def alignment_status", 1
    )[0]
    assert "revision != _READING_AUDIO_ALIGNMENT_REVISION" in migration


def test_alignment_status_exposes_algorithm_revision_for_real_trace_diagnostics() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "pudge" / "audiobooks.py").read_text(
        encoding="utf-8"
    )
    status = source.split("def alignment_status", 1)[1].split(
        "def _shift_transcription_segments", 1
    )[0]
    assert '"algorithm_revision"' in status
    assert 'processing.get("alignment_algorithm_revision")' in status


def test_rate_guard_can_be_scoped_to_verified_prefix_without_touching_tail() -> None:
    rows = [
        {"offset": 8.0, "time": 311.228},
        {"offset": 19.999, "time": 312.528},
        {"offset": 20.0, "time": 313.028},
        {"offset": 24.999, "time": 313.728},
        {"offset": 25.0, "time": 314.548},
        {"offset": 32.999, "time": 315.308},
        {"offset": 33.0, "time": 315.868},
        {"offset": 44.0, "time": 319.928},
        # Deliberately too fast, but beyond the verified prefix. It must stay.
        {"offset": 102.0, "time": 332.328},
        {"offset": 125.0, "time": 338.608},
    ]

    repaired, debug = _prune_rejoining_local_rate_outliers(
        rows,
        max_rate=7.512,
        max_offset_exclusive=102.0,
    )

    assert [(float(row["offset"]), float(row["time"])) for row in repaired] == [
        (8.0, 311.228),
        (20.0, 313.028),
        (24.999, 313.728),
        (25.0, 314.548),
        (33.0, 315.868),
        (44.0, 319.928),
        (102.0, 332.328),
        (125.0, 338.608),
    ]
    assert debug["dropped"] == 2
    assert [row["offset"] for row in debug["anchors"]] == [19.999, 32.999]
    assert debug["max_offset_exclusive"] == pytest.approx(102.0)


def test_alignment_pipeline_prunes_after_punctuation_injection() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "pudge" / "reading_audio_alignment.py").read_text(
        encoding="utf-8"
    )
    pipeline = source.split("for chapter in aligned:", 1)[1].split(
        "return {", 1
    )[0]

    inject_index = pipeline.index(
        'chapter["anchors"], pause_count = _inject_punctuation_pause_anchors'
    )
    scoped_prune_index = pipeline.index(
        'max_offset_exclusive=verified_through_offset'
    )
    preferred_rate_index = pipeline.index(
        'preferred_rate=float(leading_debug.get("verified_rate") or 0.0)'
    )
    assert scoped_prune_index > inject_index
    assert preferred_rate_index > inject_index


def test_ready_alignment_trace_exposes_current_algorithm_revision() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "pudge" / "audiobooks.py").read_text(
        encoding="utf-8"
    )
    marker = '"chapter_start_debug": chapter_start_debug'
    marker_index = source.index(marker)
    paired_state = source[max(0, marker_index - 1200) : marker_index + len(marker)]
    assert '"algorithm_revision": _READING_AUDIO_ALIGNMENT_REVISION' in paired_state


def test_rate_prune_rejoin_uses_wall_clock_so_vad_cannot_restore_runaway() -> None:
    from pudge.reading_audio_alignment import light_novel_position_for_audio

    rows = [
        {"offset": 8.0, "time": 311.228},
        {"offset": 19.999, "time": 312.528},
        {"offset": 19.999, "time": 313.027},
        {"offset": 20.0, "time": 313.028},
        {"offset": 24.999, "time": 313.728},
        {"offset": 24.999, "time": 314.547},
        {"offset": 25.0, "time": 314.548},
        {"offset": 32.999, "time": 315.308},
        {"offset": 32.999, "time": 315.867},
        {"offset": 33.0, "time": 315.868},
        {"offset": 44.0, "time": 319.928},
    ]
    repaired, debug = _prune_rejoining_local_rate_outliers(
        rows,
        max_rate=7.512,
        preferred_rate=4.173,
        max_offset_exclusive=102.0,
    )

    # The hard ceiling (7.512 chars/s) would rejoin at 19.999 and leave the
    # visible prefix racing at ~6.67 chars/s.  Prefer the chapter's verified
    # 4.173 chars/s rate instead: the first stable existing rejoin is
    # 44@319.928, whose cumulative rate from 8@311.228 is ~4.138 chars/s.
    assert debug["dropped"] == 9
    assert debug["preferred_rate"] == pytest.approx(4.173)
    assert debug["preferred_max_rate"] == pytest.approx(5.008, abs=0.001)
    assert [(float(row["offset"]), float(row["time"])) for row in repaired] == [
        (8.0, 311.228),
        (44.0, 319.928),
    ]
    rejoin_44 = repaired[-1]
    assert rejoin_44["wall_clock_from_previous"] is True

    alignment = {
        "chapters": [
            {
                "chapter_index": 1,
                "start": 311.228,
                "end": 319.928,
                "normalized_length": 44,
                "anchors": repaired,
                "punctuation_pause_count": 1,
                "speech_regions": [
                    {"start": 311.228, "end": 312.528},
                    {"start": 313.028, "end": 313.728},
                    {"start": 314.548, "end": 315.308},
                    {"start": 315.868, "end": 319.928},
                ],
            }
        ]
    }
    state = light_novel_position_for_audio(alignment, 312.118)
    assert state is not None
    # Stable wall-time interpolation is ~11.68 here.  v21 still returned to
    # 19.999@313.027 and was already around 15 chars at the same audio time.
    assert state["chapter_char_offset_exact"] == pytest.approx(11.683, abs=0.02)
    assert state["anchor_window"]["right_time"] == pytest.approx(319.928)
