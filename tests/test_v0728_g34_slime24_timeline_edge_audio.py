from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.subtitles.timeline_alignment import (
    _WindowMatch,
    _activity_bins,
    _early_edit_audio_verification_risk,
    _score_window,
    _suppress_nonmonotonic_transient_path_excursion,
)


def _match(center: float, offset: float, *, score: float = 3.0, coverage: float = .9) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=score,
        matched=10,
        source_count=11,
        reference_count=11,
        onset_coverage=coverage,
        onset_f1=.9,
        activity_f1=.9,
        mean_error=.25,
        rank_delta=.01,
        gap_fingerprint=.8,
        edge_hint_distance=None,
    )


def test_slime24_style_tail_hint_is_not_free_middle_window_anchor() -> None:
    onsets = [float(value) for value in range(0, 1001, 10)]
    bins = _activity_bins([(value, value + 2.0) for value in onsets])
    row = _score_window(
        center=500.0,
        offset=-11.0,
        source_onsets=onsets,
        reference_onsets=onsets,
        source_bins=bins,
        reference_bins=bins,
        edge_hints=(0.5, -11.0),
    )
    assert row.edge_hint_distance is None


def test_slime24_style_huge_two_window_alias_is_removed_without_matching_gap() -> None:
    path = [
        _match(325.0, -5.0),
        _match(361.0, -4.75),
        _match(397.0, -5.0),
        _match(433.0, -110.5),
        _match(469.0, -110.75),
        _match(505.0, -4.75),
        _match(541.0, -5.0),
        _match(577.0, -4.5),
    ]
    cues = [
        (300.0 + index * 4.0, 302.0 + index * 4.0, str(index))
        for index in range(75)
    ]
    filtered, diagnostics = _suppress_nonmonotonic_transient_path_excursion(cues, path)
    assert diagnostics["applied"] is True
    assert diagnostics["removed_window_count"] == 2
    assert diagnostics["removed_offsets"] == [-110.5, -110.75]
    assert all(abs(item.offset) < 20.0 for item in filtered)


def test_slime24_style_opening_edge_disagreement_requires_audio_without_gap() -> None:
    path = [
        _match(37.6, -5.0, score=2.58, coverage=.75),
        _match(109.6, -5.0, score=2.62, coverage=.80),
        _match(145.6, -5.0),
        _match(217.6, -5.0),
    ]
    risk = _early_edit_audio_verification_risk(
        path,
        {
            "applied": False,
            "reason": "no_early_long_gap",
            "base_offset_seconds": -4.75,
            "hint_offset_seconds": 0.55,
            "delta_seconds": 5.30,
        },
        [],
    )
    assert risk["required"] is True
    assert "opening_edge_clock_disagreement" in risk["reasons"]
    assert risk["cold_start_gap_seconds"] == 0.0


def test_g34_requeues_v617_unverified_opening_edge_mismatch(monkeypatch) -> None:
    from pudge.manager import AnimeManager

    video = Path("/tmp/slime-s4e24.mkv")
    episode = SimpleNamespace(
        subtitle_path=Path("/tmp/prepared.srt"),
        video_path=video,
        media_id=182205,
        episode=24,
    )

    class FakeDB:
        def __init__(self) -> None:
            self.state: dict[str, str] = {}
            self.invalidated: list[tuple[object, ...]] = []

        def get_state(self, key: str, default: str = "") -> str:
            return self.state.get(key, default)

        def set_state(self, key: str, value: str) -> None:
            self.state[key] = value

        def episodes(self):
            return [episode]

        def latest_selected_subtitle(self, _video):
            return {
                "source": "jimaku",
                "details": {
                    "alignment": {
                        "timeline_algorithm": "timeline-v6.17-decreasing-gap-anchor",
                        "timeline_early_edit_audio_verification": {
                            "required": False,
                            "cold_start_delta_seconds": 8.55,
                            "cold_start_gap_seconds": 0.0,
                        },
                    }
                },
            }

        def invalidate_subtitle(self, *args):
            self.invalidated.append(args)

    invalidated_pipeline: list[Path] = []
    monkeypatch.setattr(
        "pudge.manager.invalidate_final_pipeline_result",
        lambda path, _config: invalidated_pipeline.append(path),
    )
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = FakeDB()
    manager.config = SimpleNamespace()
    manager.log = lambda _message: None

    assert manager._requeue_local_edge_timeline_upgrade() == 1
    assert invalidated_pipeline == [video]
    assert manager.db.invalidated
    assert manager._requeue_local_edge_timeline_upgrade() == 0
