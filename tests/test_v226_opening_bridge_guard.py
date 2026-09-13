from pudge.subtitles.timeline_alignment import (
    _WindowMatch,
    _anchor_opening_path_gap_boundary,
    _refine_opening_preclock_from_holdout,
    _segments,
    _stabilize_decreasing_boundaries,
    _suppress_weak_opening_bridge_excursion,
    _suppress_weak_opening_path_excursion,
)


def _slime_shape():
    # Synthetic timing shape from the real debug: dense pre-OP dialogue, a
    # ~106s subtitle silence for the OP, then dense post-OP dialogue.
    source_cues = [
        (float(start), float(start) + 2.0, f"pre-{start}")
        for start in range(20, 352, 6)
    ]
    source_cues.append((354.12, 356.122, "last-pre"))
    source_cues.extend(
        (float(start), float(start) + 2.0, f"post-{start}")
        for start in range(462, 1400, 6)
    )
    segments = [
        {
            "offset_seconds": -32.0,
            "support": 4,
            "mean_score": 3.2585,
            "mean_coverage": 0.8434,
            "kind": "stable",
        },
        {
            "offset_seconds": -92.0,
            "support": 2,
            "mean_score": 2.8022,
            "mean_coverage": 0.8056,
            "kind": "stable",
        },
        {
            "offset_seconds": -41.0,
            "support": 19,
            "mean_score": 3.3284,
            "mean_coverage": 0.9151,
            "kind": "stable",
        },
    ]
    boundaries = [255.5, 357.3]
    edge_hints = (-32.5, -38.069)
    return source_cues, segments, boundaries, edge_hints


def test_slime_style_false_middle_clock_is_collapsed_at_opening_gap() -> None:
    source_cues, segments, boundaries, edge_hints = _slime_shape()

    fixed_segments, fixed_boundaries, guard = _suppress_weak_opening_bridge_excursion(
        source_cues,
        segments,
        boundaries,
        edge_hints,
    )

    assert guard["applied"] is True
    assert guard["reason"] == "weak_opening_bridge_excursion"
    assert guard["removed_offset_seconds"] == -92.0
    assert [row["offset_seconds"] for row in fixed_segments] == [-32.0, -41.0]
    assert fixed_segments[-1]["kind"] == "post_opening_reacquire"
    assert len(fixed_boundaries) == 1
    assert 356.122 < fixed_boundaries[0] < 462.0

    # The real bug happened because the -92s excursion forced monotonic repair
    # to drag the boundary through many cues.  After collapsing the bridge the
    # -32 -> -41 switch sits inside the OP silence and needs no such repair.
    stable_segments, stable_boundaries, refinements = _stabilize_decreasing_boundaries(
        source_cues,
        fixed_segments,
        fixed_boundaries,
    )
    assert stable_segments == fixed_segments
    assert stable_boundaries == fixed_boundaries
    assert refinements == []


def test_opening_bridge_guard_keeps_middle_clock_with_real_support() -> None:
    source_cues, segments, boundaries, edge_hints = _slime_shape()
    segments[1] = dict(segments[1], support=4, mean_score=3.31, mean_coverage=0.90)

    fixed_segments, fixed_boundaries, guard = _suppress_weak_opening_bridge_excursion(
        source_cues,
        segments,
        boundaries,
        edge_hints,
    )

    assert guard["applied"] is False
    assert fixed_segments == segments
    assert fixed_boundaries == boundaries


def test_opening_bridge_guard_keeps_middle_clock_when_edge_hint_supports_it() -> None:
    source_cues, segments, boundaries, _edge_hints = _slime_shape()

    fixed_segments, fixed_boundaries, guard = _suppress_weak_opening_bridge_excursion(
        source_cues,
        segments,
        boundaries,
        (-92.0, -41.0),
    )

    assert guard["applied"] is False
    assert fixed_segments == segments
    assert fixed_boundaries == boundaries


def test_resolved_opening_bridge_does_not_retrigger_stt_audio_verification() -> None:
    from pudge.subtitles.timeline_alignment import (
        _WindowMatch,
        _early_edit_audio_verification_risk,
    )

    def window(center: float, offset: float) -> _WindowMatch:
        return _WindowMatch(
            center=center,
            offset=offset,
            score=3.2,
            matched=12,
            source_count=14,
            reference_count=14,
            onset_coverage=0.86,
            onset_f1=0.86,
            activity_f1=0.90,
            mean_error=0.4,
            rank_delta=0.01,
            gap_fingerprint=0.8,
            edge_hint_distance=1.0,
        )

    path = [
        window(141.0, -32.0),
        window(249.0, -32.0),
        window(285.0, -92.0),
        window(357.0, -93.75),
        window(465.0, -41.0),
    ]
    unresolved = _early_edit_audio_verification_risk(path, {}, [])
    resolved = _early_edit_audio_verification_risk(
        path,
        {},
        [],
        resolved_by="weak_opening_bridge_excursion",
    )

    assert unresolved["required"] is True
    assert "early_path_clock_change" in unresolved["reasons"]
    assert resolved["required"] is False
    assert resolved["reasons"] == []
    assert "early_path_clock_change" in resolved["resolved_reasons"]
    assert resolved["resolved_by"] == "weak_opening_bridge_excursion"


def test_slime_style_bridge_collapses_without_clean_subtitle_gap() -> None:
    source_cues, segments, boundaries, edge_hints = _slime_shape()
    # Some subtitle releases keep OP/sign/title cues, so parse_srt no longer
    # exposes a >=45s clean silence even though the local onset path still has
    # the same weak -92s two-window excursion.
    source_cues.extend(
        (float(start), float(start) + 1.5, f"op-{start}")
        for start in range(360, 462, 12)
    )
    source_cues.sort(key=lambda row: row[0])

    fixed_segments, fixed_boundaries, guard = _suppress_weak_opening_bridge_excursion(
        source_cues,
        segments,
        boundaries,
        edge_hints,
    )

    assert guard["applied"] is True
    assert guard["reason"] == "weak_opening_bridge_excursion"
    assert guard["evidence"] == "structural_crossover"
    assert guard["clean_long_gap_available"] is False
    assert [row["offset_seconds"] for row in fixed_segments] == [-32.0, -41.0]
    assert fixed_segments[-1]["kind"] == "post_opening_reacquire"
    assert fixed_boundaries == [357.3]


def test_structural_fallback_requires_dominant_post_clock() -> None:
    source_cues, segments, boundaries, edge_hints = _slime_shape()
    source_cues.extend(
        (float(start), float(start) + 1.5, f"op-{start}")
        for start in range(360, 462, 12)
    )
    source_cues.sort(key=lambda row: row[0])
    segments[2] = dict(segments[2], support=8)

    fixed_segments, fixed_boundaries, guard = _suppress_weak_opening_bridge_excursion(
        source_cues,
        segments,
        boundaries,
        edge_hints,
    )

    assert guard["applied"] is False
    assert fixed_segments == segments
    assert fixed_boundaries == boundaries


def _path_window(center: float, offset: float, score: float = 3.3) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=score,
        matched=16,
        source_count=20,
        reference_count=20,
        onset_coverage=0.85,
        onset_f1=0.82,
        activity_f1=0.92,
        mean_error=0.35,
        rank_delta=0.01,
        gap_fingerprint=0.82,
        edge_hint_distance=1.0,
    )


def test_slime_real_path_sandwich_is_removed_before_segmentation() -> None:
    source_cues, _segments_unused, _boundaries, edge_hints = _slime_shape()
    path = [
        _path_window(69.3, -31.0),
        _path_window(141.3, -32.0),
        _path_window(177.3, -32.0),
        _path_window(249.3, -32.0),
        _path_window(285.3, -92.0, 2.98112),
        _path_window(357.3, -93.75, 2.62322),
        _path_window(465.3, -41.0),
        _path_window(501.3, -41.0),
        _path_window(573.3, -41.0),
        _path_window(609.3, -41.0),
        _path_window(681.3, -41.0),
        _path_window(717.3, -41.0),
        _path_window(789.3, -41.0),
        _path_window(825.3, -41.0),
    ]

    fixed_path, guard = _suppress_weak_opening_path_excursion(
        source_cues, path, edge_hints
    )

    assert guard["applied"] is True
    assert guard["reason"] == "weak_opening_path_excursion"
    assert guard["removed_offsets"] == [-92.0, -93.75]
    assert all(row.offset > -80.0 for row in fixed_path)
    clustered = _segments(fixed_path)
    assert [row["offset_seconds"] for row in clustered] == [-32.0, -41.0]

    from pudge.subtitles.timeline_alignment import _early_edit_audio_verification_risk

    risk = _early_edit_audio_verification_risk(fixed_path, {}, [])
    assert risk["required"] is False
    assert risk["reasons"] == []


def test_opening_path_sandwich_requires_real_long_gap() -> None:
    source_cues, _segments_unused, _boundaries, edge_hints = _slime_shape()
    source_cues = [
        (float(start), float(start) + 2.0, f"cue-{start}")
        for start in range(20, 1400, 6)
    ]
    path = [
        _path_window(69.3, -31.0),
        _path_window(141.3, -32.0),
        _path_window(177.3, -32.0),
        _path_window(249.3, -32.0),
        _path_window(285.3, -92.0, 2.98112),
        _path_window(357.3, -93.75, 2.62322),
        _path_window(465.3, -41.0),
        _path_window(501.3, -41.0),
        _path_window(573.3, -41.0),
        _path_window(609.3, -41.0),
        _path_window(681.3, -41.0),
        _path_window(717.3, -41.0),
        _path_window(789.3, -41.0),
        _path_window(825.3, -41.0),
    ]

    fixed_path, guard = _suppress_weak_opening_path_excursion(
        source_cues, path, edge_hints
    )

    assert guard["applied"] is False
    assert guard["reason"] == "no_long_early_gap"
    assert fixed_path == path


def test_repaired_opening_path_anchors_clock_switch_inside_the_same_long_gap() -> None:
    source_cues, _segments_unused, _boundaries, edge_hints = _slime_shape()
    path = [
        _path_window(69.3, -31.0),
        _path_window(141.3, -32.0),
        _path_window(177.3, -32.0),
        _path_window(249.3, -32.0),
        _path_window(285.3, -92.0, 2.98112),
        _path_window(357.3, -93.75, 2.62322),
        _path_window(465.3, -41.0),
        _path_window(501.3, -41.0),
        _path_window(573.3, -41.0),
        _path_window(609.3, -41.0),
        _path_window(681.3, -41.0),
        _path_window(717.3, -41.0),
        _path_window(789.3, -41.0),
        _path_window(825.3, -41.0),
    ]

    fixed_path, path_guard = _suppress_weak_opening_path_excursion(
        source_cues, path, edge_hints
    )
    assert path_guard["applied"] is True

    clustered = _segments(fixed_path)
    # This is the bad boundary shape from the v8.10.7 real debug: generic
    # fixed-offset crossover chose an early point, then monotonic repair pushed
    # it only as far as ~298s.  The actual OP subtitle silence is ~356..462s.
    anchored_segments, anchored_boundaries, anchor = (
        _anchor_opening_path_gap_boundary(clustered, [190.0], path_guard)
    )

    assert anchor["applied"] is True
    assert anchor["reason"] == "opening_path_gap_boundary_anchor"
    assert 356.0 < anchored_boundaries[0] < 462.0
    assert anchored_boundaries[0] == path_guard["gap_midpoint_seconds"]

    stable_segments, stable_boundaries, refinements = _stabilize_decreasing_boundaries(
        source_cues, anchored_segments, anchored_boundaries
    )
    assert stable_segments == anchored_segments
    assert stable_boundaries == anchored_boundaries
    assert refinements == []

    from pudge.subtitles.timeline_alignment import _early_edit_audio_verification_risk

    risk = _early_edit_audio_verification_risk(fixed_path, {}, refinements)
    assert risk["required"] is False
    assert risk["reasons"] == []

    # User-verified clock model: pre-OP ~-31/-32s, post-OP ~-41s.
    from pudge.subtitles.timeline_alignment import _offset_for_time

    assert _offset_for_time(354.12, stable_segments, stable_boundaries) == -32.0
    assert _offset_for_time(462.262, stable_segments, stable_boundaries) == -41.0


def test_opening_gap_anchor_does_not_move_an_already_safe_boundary() -> None:
    source_cues, _segments_unused, _boundaries, edge_hints = _slime_shape()
    path = [
        _path_window(69.3, -31.0),
        _path_window(141.3, -32.0),
        _path_window(177.3, -32.0),
        _path_window(249.3, -32.0),
        _path_window(285.3, -92.0, 2.98112),
        _path_window(357.3, -93.75, 2.62322),
        _path_window(465.3, -41.0),
        _path_window(501.3, -41.0),
        _path_window(573.3, -41.0),
        _path_window(609.3, -41.0),
        _path_window(681.3, -41.0),
        _path_window(717.3, -41.0),
        _path_window(789.3, -41.0),
        _path_window(825.3, -41.0),
    ]
    fixed_path, path_guard = _suppress_weak_opening_path_excursion(
        source_cues, path, edge_hints
    )
    clustered = _segments(fixed_path)
    existing = float(path_guard["gap_midpoint_seconds"])

    same_segments, same_boundaries, anchor = _anchor_opening_path_gap_boundary(
        clustered, [existing], path_guard
    )

    assert anchor["applied"] is False
    assert anchor["reason"] == "already_inside_proven_opening_gap"
    assert same_boundaries == [existing]
    assert same_segments == clustered


def test_opening_preclock_holdout_recenters_only_preopening_plateau() -> None:
    segments = [
        {
            "offset_seconds": -32.0,
            "support": 4,
            "mean_score": 3.2585,
            "mean_coverage": 0.8434,
            "kind": "stable",
        },
        {
            "offset_seconds": -41.0,
            "support": 19,
            "mean_score": 3.3284,
            "mean_coverage": 0.9151,
            "kind": "stable",
        },
    ]
    boundaries = [409.192]
    holdout = {
        "windows": [
            {
                "center": 105.3,
                "expected_offset_seconds": -32.0,
                "best_offset_seconds": -31.25,
                "residual_seconds": 0.75,
                "score": 3.36725,
                "coverage": 0.8571,
                "matched": 12,
            },
            {
                "center": 213.3,
                "expected_offset_seconds": -32.0,
                "best_offset_seconds": -31.25,
                "residual_seconds": 0.75,
                "score": 3.42865,
                "coverage": 0.8636,
                "matched": 19,
            },
            # Real debug also had a weaker pre-OP holdout.  It must not dilute
            # two strong independent windows.
            {
                "center": 321.3,
                "expected_offset_seconds": -32.0,
                "best_offset_seconds": -31.5,
                "residual_seconds": 0.5,
                "score": 2.45764,
                "coverage": 0.6154,
                "matched": 8,
            },
            {
                "center": 537.3,
                "expected_offset_seconds": -41.0,
                "best_offset_seconds": -40.25,
                "residual_seconds": 0.75,
                "score": 3.31321,
                "coverage": 0.9444,
                "matched": 17,
            },
        ]
    }
    anchor = {
        "applied": True,
        "boundary_index": 0,
        "new_source_time": 409.192,
    }

    refined, diagnostics = _refine_opening_preclock_from_holdout(
        segments, boundaries, holdout, anchor
    )

    assert diagnostics["applied"] is True
    assert diagnostics["reason"] == "opening_preclock_holdout_recenter"
    assert diagnostics["correction_seconds"] == 1.0
    assert diagnostics["holdout_best_offsets"] == [-31.25, -31.25]
    assert refined[0]["offset_seconds"] == -31.0
    assert refined[1]["offset_seconds"] == -41.0
    assert boundaries == [409.192]


def test_opening_preclock_holdout_does_not_move_when_independent_windows_disagree() -> None:
    segments = [
        {
            "offset_seconds": -32.0,
            "support": 4,
            "mean_score": 3.2,
            "mean_coverage": 0.84,
            "kind": "stable",
        },
        {
            "offset_seconds": -41.0,
            "support": 19,
            "mean_score": 3.3,
            "mean_coverage": 0.91,
            "kind": "stable",
        },
    ]
    holdout = {
        "windows": [
            {
                "center": 105.0,
                "expected_offset_seconds": -32.0,
                "best_offset_seconds": -31.25,
                "score": 3.3,
                "coverage": 0.86,
                "matched": 14,
            },
            {
                "center": 213.0,
                "expected_offset_seconds": -32.0,
                "best_offset_seconds": -32.75,
                "score": 3.3,
                "coverage": 0.86,
                "matched": 14,
            },
        ]
    }

    refined, diagnostics = _refine_opening_preclock_from_holdout(
        segments,
        [409.192],
        holdout,
        {"applied": True, "boundary_index": 0},
    )

    assert diagnostics["applied"] is False
    assert refined == segments
