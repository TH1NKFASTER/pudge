from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.manager import AnimeManager
from pudge.models import JimakuFile, VideoIdentity
from pudge.providers.jimaku import JimakuClient, explicit_season_hint
from pudge.subtitles.timeline_alignment import _suppress_false_tail_jump_from_duration_anchors


def test_g13_explicit_season_hint_handles_s2_and_roman_sequel() -> None:
    assert explicit_season_hint("暗殺教室.S02E01.WEBRip.ja.srt", "Ansatsu Kyoushitsu") == 2
    assert explicit_season_hint("Ansatsu Kyoushitsu S2 - E01 - [TV].srt", "Ansatsu Kyoushitsu") == 2
    assert explicit_season_hint("[Kamigami] Ansatsu Kyoushitsu II [01][BD][JPN].ass", "Ansatsu Kyoushitsu") == 2
    assert explicit_season_hint("[Kamigami] Ansatsu Kyoushitsu [01][BD][JPN].ass", "Ansatsu Kyoushitsu") is None


def test_g13_rank_files_marks_explicit_wrong_season_as_hard_reject() -> None:
    client = JimakuClient.__new__(JimakuClient)
    identity = VideoIdentity(title="Ansatsu Kyoushitsu", season=1, episode=1)
    correct = JimakuFile("ok", "[Kamigami] Ansatsu Kyoushitsu [01][BD][JPN].ass", 1, "")
    wrong_roman = JimakuFile("bad1", "[Kamigami] Ansatsu Kyoushitsu II [01][BD][JPN].ass", 1, "")
    wrong_tag = JimakuFile("bad2", "Ansatsu Kyoushitsu S2 - E01 - [TV].srt", 1, "")
    ranked = client.rank_files([wrong_roman, correct, wrong_tag], identity, Path("Assassination Classroom S01E01.mkv"))
    by_name = {row.name: row for row in ranked}
    assert "hard_reject_reason" not in by_name[correct.name].details
    for item in (wrong_roman, wrong_tag):
        assert by_name[item.name].details["season_match"] == "mismatch"
        assert by_name[item.name].details["hard_reject_reason"] == "season_mismatch"
        assert by_name[item.name].score < -700


def test_g13_duration_anchor_guard_catches_sparse_but_decisive_lara_shape() -> None:
    # 51 disputed source cues, but only 11 have a duration twin at the old clock.
    # This mirrors the real Sayonara Lara E04 shape that v6.15 missed at 11/51.
    source = [(1263.0 + i * 2.1, 1263.9 + i * 2.1 + (i % 3) * 0.1, f"cue {i}") for i in range(51)]
    reference = [
        (source[i][0] + 0.03, source[i][1] + 0.04, f"old {i}")
        for i in range(11)
    ]
    # Add unrelated reference activity so the denominator remains 51 while no
    # duration anchors support the proposed +82 second clock.
    reference += [(1270.0 + i * 1.7, 1270.3 + i * 1.7, f"noise {i}") for i in range(40)]
    segments = [
        {"offset_seconds": 0.0, "support": 23, "mean_score": 3.5, "mean_coverage": 0.92},
        {"offset_seconds": 82.0, "support": 2, "mean_score": 2.6, "mean_coverage": 0.84},
    ]
    result, boundaries, report = _suppress_false_tail_jump_from_duration_anchors(
        source, reference, segments, [1262.5]
    )
    assert report["old_clock_duration_anchors"] >= 8
    assert report["old_clock_duration_anchor_ratio"] >= 0.20
    assert report["proposed_clock_duration_anchors"] <= 2
    assert report["applied"] is True
    assert len(result) == 1
    assert boundaries == []


def test_g13_tail_cache_migration_requeues_only_weak_positive_tail(monkeypatch) -> None:
    video = Path("/tmp/episode.mkv")
    episode = SimpleNamespace(subtitle_path=Path("/tmp/final.srt"), video_path=video, media_id=177637, episode=4)

    class FakeDB:
        def __init__(self) -> None:
            self.state = {}
            self.invalidated = []

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
                        "timeline_segments": [
                            {"offset_seconds": 0.0, "support": 23},
                            {"offset_seconds": 58.5, "support": 2},
                        ],
                        "timeline_weak_tail_guard": {"boundary_ratio": 0.8859},
                    }
                },
            }

        def invalidate_subtitle(self, *args):
            self.invalidated.append(args)

    invalidated_pipeline = []
    monkeypatch.setattr("pudge.manager.invalidate_final_pipeline_result", lambda path, config: invalidated_pipeline.append(path))
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = FakeDB()
    manager.config = SimpleNamespace()
    messages = []
    manager.log = messages.append

    assert manager._requeue_false_positive_tail_clocks() == 1
    assert manager.db.invalidated
    assert invalidated_pipeline == [video]
    assert manager.db.state["subtitle_tail_clock_guard_generation"] == "1"
    # One-time migration.
    assert manager._requeue_false_positive_tail_clocks() == 0
