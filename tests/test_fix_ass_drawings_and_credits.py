"""ASS vector drawings, credits and romaji leaked into prepared Japanese subtitles.

A BD fansub .ass draws lyric boxes with ``{\\p1}m 0 0 l 670 0 ...{\\p0}`` events, shows
release/staff credits in their own styles and romaji lyrics beside the Japanese ones.
Prepared SRTs showed ``m 0 0 l 670 0 l 670 60 ...`` and ``BDRip.....Group`` as cues.
All content below is invented.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pudge import subtitle_migrations
from pudge.subtitle_formats import (
    clean_srt_for_playback,
    convert_to_plain_srt,
    merge_layered_cues,
    parse_srt,
    plain_subtitle_text,
    srt_has_non_dialogue_lines,
)
from subtitle_rebuild_support import assert_not_prepared_again, assert_rebuilding_with_subtitles_kept
from t71g08_support import subtitle_library

_HEADER = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1
Style: OP-JP,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,10,1
Style: OP-Romaji,Arial,30,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,10,1
Style: Credit,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,9,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_JAPANESE_EVENTS = r"""Dialogue: 0,0:00:01.00,0:00:03.00,Credit,,0,0,0,,Encode.....Example-Raws
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\an7\p1}m 0 0 l 670 0 l 670 60 l 0 60 l 0 0{\p0}
Dialogue: 1,0:00:05.00,0:00:08.00,OP-JP,,0,0,0,,{\p1}m 0 0 l 670 0 l 670 60 l 0 60{\p0}\N空の向こうへ飛んでいく
Dialogue: 2,0:00:05.00,0:00:08.20,OP-JP,,0,0,0,,{\blur3}空の向こうへ飛んでいく
Dialogue: 0,0:00:05.00,0:00:08.00,OP-Romaji,,0,0,0,,{\k20}so{\k20}ra
Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,m 0 0 l 100 0 l 100 20 l 0 20
Dialogue: 0,0:00:13.00,0:00:15.00,Default,,0,0,0,,BDRip.....Example-Studio x265
Dialogue: 0,0:00:20.00,0:00:22.00,Default,,0,0,0,,おはようございます
Dialogue: 0,0:00:23.00,0:00:25.00,Default,,0,0,0,,今日はいい天気だね
Dialogue: 0,0:00:26.00,0:00:28.00,Default,,0,0,0,,OK
Dialogue: 0,0:00:29.00,0:00:31.00,Default,,0,0,0,,行ってきます
"""


def _write(tmp_path: Path, events: str, name: str = "show.jp.ass") -> Path:
    path = tmp_path / name
    path.write_text(_HEADER + events, encoding="utf-8")
    return path


_FFMPEG = shutil.which("ffmpeg")


@pytest.mark.parametrize("ffmpeg", ["/nonexistent/ffmpeg", _FFMPEG or "/nonexistent/ffmpeg"])
def test_ass_conversion_drops_drawings_credits_romaji_and_layer_duplicates(tmp_path, ffmpeg):
    source = _write(tmp_path, _JAPANESE_EVENTS)
    output, result = convert_to_plain_srt(source, tmp_path / "cache", ffmpeg_path=ffmpeg)
    assert result["converted"] is True
    cues = parse_srt(output)
    texts = [text for _start, _end, text in cues]
    assert texts == [
        "空の向こうへ飛んでいく",
        "おはようございます",
        "今日はいい天気だね",
        "OK",
        "行ってきます",
    ]
    raw = output.read_text(encoding="utf-8")
    assert "m 0 0" not in raw and "Raws" not in raw and "BDRip" not in raw and "sora" not in raw
    # The two lyric layers became one cue spanning both.
    assert cues[0][0] == pytest.approx(5.0) and cues[0][1] == pytest.approx(8.2)


def test_latin_song_lines_are_kept_in_a_non_japanese_script(tmp_path):
    events = (
        "".join(
            f"Dialogue: 0,0:00:{10 + 3 * i:02d}.00,0:00:{12 + 3 * i:02d}.00,Default,,0,0,0,,Line number {i} here\n"
            for i in range(6)
        )
        + "Dialogue: 0,0:00:40.00,0:00:42.00,OP-Romaji,,0,0,0,,{\\k20}Fly beyond the sky\n"
    )
    output, _result = convert_to_plain_srt(
        _write(tmp_path, events, "show.en.ass"), tmp_path / "cache", ffmpeg_path="/nonexistent/ffmpeg"
    )
    assert "Fly beyond the sky" in [text for *_t, text in parse_srt(output)]


def test_plain_text_drops_only_drawing_and_release_credit_lines():
    assert plain_subtitle_text("m 0 0 l 670 0 l 670 60 l 0 60 l 0 0\n空へ") == "空へ"
    assert plain_subtitle_text("m 10 5 b 20 5 30 15 30 25 l 0 25 c") == ""
    assert plain_subtitle_text("BDRip.....VCB-Studio") == ""
    assert plain_subtitle_text("Encoded by someone") == ""
    # Dialogue that only looks similar stays.
    for text in ("m-maybe", "Room 101", "m 1 2", "We rip the BD box open", "BDRipの話", "OK"):
        assert plain_subtitle_text(text) == text


def test_merge_layered_cues_keeps_different_or_sequential_lines():
    cues = [
        (1.0, 3.0, "同じ歌詞の行"),
        (1.0, 3.5, "同じ歌詞の行"),
        (4.0, 5.0, "同じ歌詞の行"),
        (1.0, 3.0, "別の行です"),
    ]
    merged, count = merge_layered_cues(cues)
    assert count == 1
    assert merged == [(1.0, 3.5, "同じ歌詞の行"), (1.0, 3.0, "別の行です"), (4.0, 5.0, "同じ歌詞の行")]


_OLD_PREPARED = (
    "1\n00:00:05,000 --> 00:00:08,000\nm 0 0 l 670 0 l 670 60 l 0 60 l 0 0\n空の向こうへ飛んでいく\n\n"
    "2\n00:00:05,000 --> 00:00:08,200\n空の向こうへ飛んでいく\n\n"
    "3\n00:00:09,000 --> 00:00:10,000\nm 0 0 l 670 0 l 670 60 l 0 60\n\n"
    "4\n00:00:11,000 --> 00:00:12,000\nBDRip.....Example-Studio\n\n"
    "5\n00:00:20,123 --> 00:00:22,456\nおはようございます\n"
)


def test_already_prepared_playback_srt_is_cleaned_when_served(tmp_path):
    cache = tmp_path / "cache"
    prepared = cache / "playback-srt" / "v16-0123456789abcdef0123.srt"
    prepared.parent.mkdir(parents=True)
    prepared.write_text(_OLD_PREPARED, encoding="utf-8")
    assert srt_has_non_dialogue_lines(prepared)
    served, result = clean_srt_for_playback(prepared, cache)
    assert result["reason"] == "non_dialogue_removed" and served != prepared
    assert served.name.startswith("v16-") and served.parent == prepared.parent
    assert parse_srt(served) == [(5.0, 8.2, "空の向こうへ飛んでいく"), (20.123, 22.456, "おはようございます")]
    assert prepared.read_text(encoding="utf-8") == _OLD_PREPARED  # never rewritten in place
    # The cleaned copy is clean: served as is.
    assert clean_srt_for_playback(served, cache) == (served, {"reason": "already_clean", "cleaned": False})
    again, again_result = clean_srt_for_playback(prepared, cache)
    assert again == served and again_result["reason"] == "non_dialogue_removed"


def test_clean_prepared_srt_is_served_unchanged(tmp_path):
    cache = tmp_path / "cache"
    prepared = cache / "playback-srt" / "v16-aaaaaaaaaaaaaaaaaaaa.srt"
    prepared.parent.mkdir(parents=True)
    prepared.write_text("1\n00:00:01,000 --> 00:00:02,000\n同じ行\n", encoding="utf-8")
    assert not srt_has_non_dialogue_lines(prepared)
    assert clean_srt_for_playback(prepared, cache) == (
        prepared,
        {"reason": "already_clean", "cleaned": False},
    )


def test_stale_cached_playback_copy_is_cleaned(tmp_path):
    cache = tmp_path / "cache"
    aligned = tmp_path / "aligned.srt"
    aligned.write_text(_OLD_PREPARED, encoding="utf-8")
    fresh, fresh_result = clean_srt_for_playback(aligned, cache)
    assert fresh_result["reason"] == "cleaned"
    assert [text for *_t, text in parse_srt(fresh)] == ["空の向こうへ飛んでいく", "おはようございます"]
    # A copy written by the old cleaner (drawings kept) under the same cache name.
    fresh.write_text(_OLD_PREPARED, encoding="utf-8")
    served, result = clean_srt_for_playback(aligned, cache)
    assert result["reason"] == "non_dialogue_removed"
    assert [text for *_t, text in parse_srt(served)] == ["空の向こうへ飛んでいく", "おはようございます"]


def test_rebuild_rule_prepares_unwatched_episodes_showing_drawings_again(tmp_path, monkeypatch):
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    rule = subtitle_migrations.RULES_BY_ID["non_dialogue_text"]
    assert rule.scope == "unwatched" and rule.kind == "generation"
    row = manager.db.episode_by_path(video)
    # A clean prepared file is left alone.
    assert subtitle_migrations.run_rule(manager, "non_dialogue_text") == 0
    assert_not_prepared_again(manager, video)
    manager.db.set_state(rule.progress_key, "")
    row.subtitle_path.write_text(_OLD_PREPARED, encoding="utf-8")
    assert subtitle_migrations.run_rule(manager, "non_dialogue_text") == 1
    assert_rebuilding_with_subtitles_kept(manager, video)
    assert subtitle_migrations.run_rule(manager, "non_dialogue_text") == 0  # once


def test_rebuild_rule_skips_manual_and_watched_selections(tmp_path, monkeypatch):
    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    row = manager.db.episode_by_path(video)
    row.subtitle_path.write_text(_OLD_PREPARED, encoding="utf-8")
    manual = subtitle_migrations.Selection(row, {"id": 1, "source": "manual", "details": {}})
    assert not subtitle_migrations._non_dialogue_text_shown(manual)
    row.state = "watched"
    manager.db.upsert_episode(row)
    assert subtitle_migrations.run_rule(manager, "non_dialogue_text") == 0
    assert_not_prepared_again(manager, video)
