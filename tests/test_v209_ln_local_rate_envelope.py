"""Listen together: the local reading-rate guard of the alignment clock (behaviour; the
revision and pipeline checks rewritten in audit 71Б)."""

from __future__ import annotations

import pytest

from pudge import audiobooks
from pudge import reading_audio_alignment as raa
from pudge.audiobooks import _READING_AUDIO_ALIGNMENT_REVISION
from pudge.reading_audio_alignment import _prune_rejoining_local_rate_outliers, light_novel_position_for_audio
from t71g23_support import paired_book, write_alignment


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
    rows, debug = _prune_rejoining_local_rate_outliers(_spice_wolf_v1_clock(), max_rate=7.512)

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


V21 = "reading-audio-v3-leading-prefix-v21"


def test_alignment_made_before_the_local_rate_guard_is_rebuilt_and_reports_its_revision(
    tmp_path, monkeypatch
) -> None:
    # The guard changed the clock (v22): an alignment cached by the v21 algorithm is not
    # used — neither at the cache path v21 had nor through the migration of compatible
    # caches — so the book is aligned again. Status and the paired state name the
    # algorithm revision of the alignment in use (for diagnosing real traces).
    audio, ln_id, audio_id = paired_book(
        tmp_path,
        [("一", "吾輩は猫である。名前はまだない。"), ("二", "どこで生まれたか見当がつかぬ。")],
        duration=20.0,
    )
    chapters = [
        {
            "chapter_index": index,
            "title": title,
            "normalized_length": length,
            "confidence": 0.9,
            "leading_prefix_debug": {"recovered": True, "chapter": index},
            "anchors": [{"offset": 0, "time": 10.0 * index}, {"offset": length, "time": 10.0 * index + 9.0}],
        }
        for index, (title, length) in enumerate([("一", 14), ("二", 14)])
    ]
    current_path = audio._alignment_path(ln_id, audio_id)
    with monkeypatch.context() as patch:
        patch.setattr(audiobooks, "_READING_AUDIO_ALIGNMENT_REVISION", V21)
        audio._fingerprint_cache.clear()
        v21_path = audio._alignment_path(ln_id, audio_id)
    audio._fingerprint_cache.clear()
    assert v21_path != current_path
    write_alignment(audio, audio_id, v21_path, chapters, V21)
    assert audio.alignment_status(ln_id) == {"status": "not_prepared", "ready": False}
    assert audio.paired_state(ln_id)["alignment"] == {"status": "not_prepared", "ready": False}

    write_alignment(audio, audio_id, current_path, chapters, _READING_AUDIO_ALIGNMENT_REVISION)
    status = audio.alignment_status(ln_id)
    assert (
        status["ready"] is True and status["algorithm_revision"] == _READING_AUDIO_ALIGNMENT_REVISION != V21
    )
    audio.set_position(audio_id, 13.0)
    paired = audio.paired_state(ln_id)
    assert paired["ln_chapter_index"] == 1
    assert paired["alignment"]["algorithm_revision"] == _READING_AUDIO_ALIGNMENT_REVISION
    assert paired["alignment"]["chapter_start_debug"] == {"recovered": True, "chapter": 1}


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


# The Spice and Wolf v1 prefix after punctuation pause holds were added.
_ENRICHED_PREFIX = [
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


def test_alignment_prunes_the_fast_edges_that_punctuation_pauses_create(monkeypatch) -> None:
    # Punctuation/VAD enrichment can itself create transient fast edges inside the recovered
    # prose prefix (real Spice and Wolf v1: 8 -> 19.999 and 25 -> 32.999). The finished
    # alignment must not contain them: the rate guard runs after the enrichment, at the
    # chapter's verified reading rate, and only before the verified end of the prefix.
    text = "吾輩は猫である。名前はまだない。どこで生まれたか見当がつかぬ。何でも薄暗いじめじめした所で泣いていたことだけは記憶している。"
    leading = {
        "attempted": True,
        "recovered": True,
        "verified_through_offset": 102,
        "verified_rate": 4.173,
        "max_bridge_rate": 7.512,
    }

    def recovered_prefix(_text, _segments, _clock, **_kwargs):
        return (
            [{"offset": 0, "time": 309.0}, {"offset": 8, "time": 311.228}, {"offset": 44, "time": 319.928}],
            309.0,
            dict(leading),
        )

    def enriched(_anchors, _boundaries, _regions):
        # What the enrichment makes of this prefix: pause holds that rejoin too fast.
        rows = [
            {"offset": 0, "time": 309.0},
            *[dict(row) for row in _ENRICHED_PREFIX],
            {"offset": 120, "time": 340.0},
        ]
        return rows, 3

    monkeypatch.setattr(raa, "_recover_leading_prefix_clock", recovered_prefix)
    monkeypatch.setattr(raa, "_inject_punctuation_pause_anchors", enriched)
    normalized = raa.normalize_reading_text(text)
    alignment = raa.align_light_novel_to_transcript(
        [{"chapter_index": 0, "title": "第一章", "text": text}],
        [{"start": 309.0, "end": 340.0, "text": normalized}],
        duration=400.0,
        model="test-model",
        speech_regions=[{"start": 309.0, "end": 340.0}],
    )
    chapter = alignment["chapters"][0]
    assert [(float(row["offset"]), float(row["time"])) for row in chapter["anchors"]] == [
        (0.0, 309.0),
        (8.0, 311.228),
        (44.0, 319.928),
        (120.0, 340.0),
    ]
    prune = chapter["leading_prefix_debug"]["local_rate_prune"]
    assert prune["dropped"] == 9 and prune["preferred_rate"] == pytest.approx(4.173)


def test_rate_prune_rejoin_uses_wall_clock_so_vad_cannot_restore_runaway() -> None:
    rows = [dict(row) for row in _ENRICHED_PREFIX]
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
