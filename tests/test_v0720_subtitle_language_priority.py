# pudge-subtitle-language-priority-v1
from __future__ import annotations

from pathlib import Path

from pudge.models import JimakuFile, SubtitleCandidate, VideoIdentity
from pudge.providers.jimaku import JimakuClient
from pudge.subtitle_formats import (
    clean_srt_for_playback,
    convert_to_plain_srt,
    parse_srt,
    subtitle_filename_language_profile,
)
from pudge.syncing import _rank_embedded_reference_candidates


def _ts(value: float) -> str:
    total_ms = round(value * 1000)
    hours, total_ms = divmod(total_ms, 3_600_000)
    minutes, total_ms = divmod(total_ms, 60_000)
    seconds, millis = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def test_filename_language_profile_distinguishes_clean_and_mixed_japanese() -> None:
    assert subtitle_filename_language_profile(
        "さよならララ.S01E01.人魚姫ララ.WEBRip.ABEMA.ja[cc].srt"
    )["purity"] == "japanese_only"
    assert subtitle_filename_language_profile(
        "[KitaujiSub] Sayonara Lara [01][JPN].ass"
    )["purity"] == "japanese_only"
    assert subtitle_filename_language_profile(
        "[Studio] Sayonara Lara [01][CHS, JPN].ass"
    )["purity"] == "mixed_japanese_chinese"


def test_jimaku_filename_ranking_penalizes_mixed_track() -> None:
    client = object.__new__(JimakuClient)
    files = [
        JimakuFile(
            url="https://example.invalid/mixed",
            name="[Studio] Sayonara Lara - 01 [CHS, JPN].ass",
            size=1,
            last_modified="",
        ),
        JimakuFile(
            url="https://example.invalid/srt",
            name="さよならララ.S01E01.人魚姫ララ.WEBRip.ABEMA.ja[cc].srt",
            size=1,
            last_modified="",
        ),
    ]
    ranked = client.rank_files(
        files,
        VideoIdentity(title="Sayonara Lara", episode=1),
        Path("Sayonara Lara - 01.mkv"),
        prefer_srt=True,
    )
    assert ranked[0].name.endswith("ja[cc].srt")
    assert ranked[0].details["language_purity"] == "japanese_only"
    assert ranked[1].details["language_purity"] == "mixed_japanese_chinese"
    assert ranked[0].score > ranked[1].score + 100


def test_cleaner_removes_normal_duration_parallel_chinese_track(tmp_path: Path) -> None:
    blocks: list[str] = []
    for number in range(24):
        start = 1.0 + number * 1.5
        end = start + 1.2
        index = number * 2 + 1
        blocks.append(
            f"{index}\n{_ts(start)} --> {_ts(end)}\nこれは日本語の台詞です"
        )
        blocks.append(
            f"{index + 1}\n{_ts(start)} --> {_ts(end)}\n这是中文翻译台词"
        )
    source = tmp_path / "parallel.srt"
    source.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

    cleaned, result = clean_srt_for_playback(source, tmp_path / "cache")

    assert result["bilingual_cjk"] is True
    assert result["bilingual_removed"] == 24
    payload = cleaned.read_text(encoding="utf-8")
    assert "这是中文翻译台词" not in payload
    assert payload.count("これは日本語の台詞です") == 24


def test_manual_ass_conversion_filters_parallel_chinese_before_merging(tmp_path: Path) -> None:
    events = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for number in range(24):
        start = number * 1.5
        end = start + 1.2
        events.append(
            f"Dialogue: 0,0:00:{start:05.2f},0:00:{end:05.2f},Default,,0,0,0,,これは日本語です"
        )
        events.append(
            f"Dialogue: 0,0:00:{start:05.2f},0:00:{end:05.2f},Default,,0,0,0,,这是中文翻译"
        )
    source = tmp_path / "episode.[CHS, JPN].ass"
    source.write_text("\n".join(events) + "\n", encoding="utf-8")

    converted, result = convert_to_plain_srt(
        source,
        tmp_path / "cache",
        ffmpeg_path="/definitely/missing/ffmpeg",
    )

    assert result["converted"] is True
    payload = converted.read_text(encoding="utf-8")
    assert "这是中文翻译" not in payload
    assert "これは日本語です" in payload
    assert len(parse_srt(converted)) == 24


def test_clean_srt_beats_better_activity_mixed_ass_when_clock_is_acceptable(tmp_path: Path) -> None:
    def item(
        name: str,
        suffix: str,
        purity: str,
        activity: float,
        score: float,
    ) -> tuple[tuple[float, ...], SubtitleCandidate, Path, dict[str, object], dict[str, object], dict[str, object]]:
        candidate = SubtitleCandidate(
            path=tmp_path / f"candidate{suffix}",
            source="jimaku",
            score=score,
            name=name,
            details={"language_purity": purity},
        )
        return (
            (1.0, activity, 0.0, score),
            candidate,
            tmp_path / f"aligned-{name[:4]}.srt",
            {"timeline_alignment_reliable": True},
            {"available": True, "weighted": activity},
            {"reason": "ok"},
        )

    mixed = item("mixed ASS", ".ass", "mixed_japanese_chinese", 0.9526, 103.0)
    pure_ass = item("pure ASS", ".ass", "japanese_only", 0.9465, 113.0)
    pure_srt = item("ABEMA ja[cc] SRT", ".srt", "japanese_only", 0.9011, 123.0)

    ranked, meta = _rank_embedded_reference_candidates(
        [mixed, pure_ass, pure_srt],
        prefer_srt=True,
    )

    assert ranked[0][1].name == "ABEMA ja[cc] SRT"
    assert meta["format_preference_applied"] is True
    assert meta["selected_language_purity"] == "japanese_only"
    assert meta["raw_best_language_purity"] == "mixed_japanese_chinese"
