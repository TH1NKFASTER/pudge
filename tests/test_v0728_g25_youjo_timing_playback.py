from pathlib import Path

from pudge.subtitle_formats import clean_srt_for_playback
from pudge.subtitles.timeline_alignment import _stabilize_decreasing_boundaries


def test_youjo_style_decreasing_clock_reanchors_to_real_source_gap() -> None:
    cues = [
        (71.772, 74.274, "目撃者は最小限に抑え"),
        (74.274, 77.678, "不幸な事故のタイミングを狙わねば"),
        (94.561, 101.401, "（悲鳴）"),
        (101.401, 104.571, "西から東"),
        (104.571, 106.740, "そして また西へ"),
        (106.740, 110.577, "命令とはいえ"),
    ]
    segments = [
        {
            "first_center": 0.0,
            "last_center": 101.5,
            "offset_seconds": -37.0,
            "support": 1,
            "mean_score": 2.829,
            "mean_coverage": 0.7692,
            "windows": [],
            "kind": "stable",
        },
        {
            "first_center": 101.5,
            "last_center": 999.0,
            "offset_seconds": -47.0,
            "support": 22,
            "mean_score": 3.3269,
            "mean_coverage": 0.9131,
            "windows": [],
            "kind": "stable",
        },
    ]

    _segments, boundaries, diagnostics = _stabilize_decreasing_boundaries(
        cues, segments, [101.5]
    )

    assert boundaries == [(77.678 + 94.561) / 2.0]
    assert diagnostics == [
        {
            "applied": True,
            "reason": "decreasing_boundary_reanchored_to_source_gap",
            "boundary_index": 0,
            "old_source_time": 101.5,
            "new_source_time": round((77.678 + 94.561) / 2.0, 3),
            "left_offset_seconds": -37.0,
            "right_offset_seconds": -47.0,
            "cue_index": 3,
            "gap_seconds": round(94.561 - 77.678, 3),
            "gap_start_seconds": 77.678,
            "gap_end_seconds": 94.561,
            "previous_mapped_start": round(94.561 - 37.0, 3),
            "current_mapped_start": round(101.401 - 47.0, 3),
        }
    ]

    # The scream and all following dialogue now share the post-edit clock.
    assert 94.561 - 47.0 < 101.401 - 47.0
    assert round(101.401 - 47.0, 3) == 54.401


def test_playback_cleaner_keeps_japanese_han_only_lines(tmp_path: Path) -> None:
    source = tmp_path / "youjo-like.srt"
    normal = "".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},500\n普通の日本語です。\n\n"
        for index in range(1, 11)
    )
    source.write_text(
        normal
        + "11\n00:02:04,405 --> 00:02:08,409\n"
        "出発前\nルーデルドルフ閣下が　こちらを。\n\n"
        "12\n00:03:29,590 --> 00:03:32,660\n"
        "敵爆撃機より\n魔導反応感知！\n\n",
        encoding="utf-8",
    )

    cleaned, result = clean_srt_for_playback(source, tmp_path / "cache", force=True)
    payload = cleaned.read_text(encoding="utf-8")

    assert "出発前" in payload
    assert "ルーデルドルフ閣下が" in payload
    assert "敵爆撃機より" in payload
    assert "魔導反応感知！" in payload
    assert result["bilingual_profile"]["removed_inline_chinese_lines"] == 0
    assert result["bilingual_cjk"] is False


def test_playback_cleaner_still_removes_obvious_inline_chinese(tmp_path: Path) -> None:
    source = tmp_path / "bilingual.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n"
        "これは日本語の台詞です\n这是中文翻译台词\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n"
        "次の日本語です\n我们继续说话\n",
        encoding="utf-8",
    )

    cleaned, result = clean_srt_for_playback(source, tmp_path / "cache", force=True)
    payload = cleaned.read_text(encoding="utf-8")

    assert "这是中文翻译台词" not in payload
    assert "我们继续说话" not in payload
    assert "これは日本語の台詞です" in payload
    assert "次の日本語です" in payload
    assert result["bilingual_profile"]["removed_inline_chinese_lines"] == 2


def test_g25_migration_requeues_old_dragged_decreasing_boundary(monkeypatch) -> None:
    from types import SimpleNamespace

    from pudge.manager import AnimeManager

    video = Path("/tmp/youjo-s2e12.mkv")
    episode = SimpleNamespace(
        subtitle_path=Path("/tmp/prepared.srt"),
        video_path=video,
        media_id=135865,
        episode=12,
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
                        "timeline_algorithm": "timeline-v6.16-tail-clock-duration-anchors",
                        "timeline_monotonic_refinements": [
                            {
                                "applied": True,
                                "reason": "decreasing_boundary_extended_for_monotonicity",
                                "boundary_index": 0,
                                "old_source_time": 101.5,
                                "new_source_time": 102.987,
                                "left_offset_seconds": -37.0,
                                "right_offset_seconds": -47.0,
                            },
                            {
                                "applied": True,
                                "reason": "decreasing_boundary_extended_for_monotonicity",
                                "boundary_index": 0,
                                "old_source_time": 146.864,
                                "new_source_time": 151.386,
                                "left_offset_seconds": -37.0,
                                "right_offset_seconds": -47.0,
                            },
                        ],
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

    assert manager._requeue_decreasing_gap_timeline_upgrade() == 1
    assert invalidated_pipeline == [video]
    assert manager.db.invalidated
    assert manager._requeue_decreasing_gap_timeline_upgrade() == 0
