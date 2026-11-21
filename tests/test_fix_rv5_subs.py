"""Subtitle pipeline review (rv5): the "not fixed" list of the round-4 review.

Invented data only.

* A line without readable text takes the clock of its nearer mapped neighbour (plateau
  and cut clocks), not its source time or always the earlier clock.
* ASS events that end before they start are dropped on the ffmpeg path too.
* Negative and 100+ hour SRT times; ruby with several readings in SRT/ASS.
* The prepared-result manifest sees an edited raw subtitle, new step versions and another
  ALASS; old manifests stay valid until their inputs change. The ALASS cache key names
  the binary.
* TS/TX/EX/CX rank a file as TV-timed only as a release tag.
* A mark at 0 s does not refuse the constant speech clock.
* A moved cold open does not run into the song marks after it.
* A stray line far past the end does not stretch the timeline grid.
* write_srt numbers the cues it writes without gaps; an ASS file is read once.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import time
from pathlib import Path

import pytest

from pudge import pipeline_cache, syncing
from pudge.config import AppConfig, SyncConfig
from pudge.subtitle_formats import (
    _parse_srt_text,
    _sanitize_ass_events,
    convert_to_plain_srt,
    parse_srt,
    plain_subtitle_text,
    srt_timestamp,
    write_exact_srt,
    write_srt,
)
from pudge.subtitles import candidate_choice, cold_open, timeline_alignment
from pudge.subtitles.cut_clocks import fit_text_clock_cuts
from pudge.subtitles.speech_recovery import recover_constant_speech_clock

# --- 1. plateau clock: a line without text -----------------------------------------------------


def test_a_line_without_text_takes_the_nearer_clock_of_the_plateau_map() -> None:
    cues = [
        (10.0, 12.0, "まえのせりふ"),
        (12.5, 13.0, "…"),  # 0.5 s after the earlier line, 20 s before the later one
        (40.0, 41.0, "！？"),  # 26 s after "…", 0.5 s before the later line
        (41.5, 43.0, "あとのせりふ"),
        (90.0, 90.5, "……"),  # after the last mapped line
    ]
    shifts = [120.0, None, None, 150.0, None]
    filled = syncing._plateau_unmapped_shifts(cues, shifts)
    # Before: both kept their source time (12.5 s and 40 s) while their scenes moved by
    # two minutes and more.
    assert filled == [120.0, 120.0, 150.0, 150.0, 150.0]


def test_a_line_without_text_is_not_put_before_the_file_start() -> None:
    cues = [(1.0, 2.0, "…"), (3.0, 4.0, "せりふです"), (30.0, 31.0, "つぎのせりふ")]
    # The nearer clock (-60 s) would put "…" before 0 s; there is no other neighbour.
    assert syncing._plateau_unmapped_shifts(cues, [None, -60.0, -50.0]) == [None, -60.0, -50.0]
    cues = [(10.0, 11.0, "まえ"), (12.0, 13.0, "…"), (40.0, 41.0, "あと")]
    # The earlier neighbour is nearer but -20 s puts "…" before 0 s: the later one's +5 s.
    assert syncing._plateau_unmapped_shifts(cues, [-20.0, None, 5.0]) == [-20.0, 5.0, 5.0]


# --- 2. cut clocks: an unmapped line right after an inserted break --------------------------------


def _cut_episode() -> tuple[list[float], list[float], list[float | None], int]:
    starts: list[float] = []
    ends: list[float] = []
    shifts: list[float | None] = []
    t = 5.0
    unmapped = -1
    for block, shift in enumerate((0.0, 30.0, 60.0)):
        if block:
            t += 12.0  # the dialogue pause where the break was inserted
        if block == 1:
            # a "…" right before the first line after the break
            starts.append(t - 1.0)
            ends.append(t - 0.6)
            shifts.append(None)
            unmapped = len(starts) - 1
        for _line in range(30):
            starts.append(t)
            ends.append(t + 2.0)
            shifts.append(shift)
            t += 3.0
    return starts, ends, shifts, unmapped


def test_an_unmapped_line_after_an_inserted_break_takes_the_later_clock() -> None:
    starts, ends, shifts, unmapped = _cut_episode()
    fit = fit_text_clock_cuts(starts, ends, shifts)
    assert fit["accepted"] is True
    # Before: the earlier clock (0 s), 30 s early, shown before the break.
    assert fit["shifts"][unmapped] == 30.0


# --- 3. ASS: End before Start --------------------------------------------------------------------

ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,いちばんめのせりふ
Dialogue: 0,0:00:10.00,0:00:08.00,Default,,0,0,0,,さかさまのせりふ
Dialogue: 0,0:00:20.00,0:00:20.00,Default,,0,0,0,,ながさゼロのせりふ
Dialogue: 0,0:01:00.00,0:01:02.00,Default,,0,0,0,,さいごのせりふ
"""


def test_reversed_ass_events_are_dropped_before_ffmpeg() -> None:
    text, removed = _sanitize_ass_events(ASS)
    assert removed["untimed_removed"] == 2
    assert "さかさま" not in text and "ながさゼロ" not in text and "さいご" in text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_reversed_ass_event_is_no_cue_until_the_next_line(tmp_path: Path) -> None:
    source = tmp_path / "Invented Show - 01.ja.ass"
    source.write_text(ASS, encoding="utf-8")
    output, info = convert_to_plain_srt(source, tmp_path / "cache")
    assert info["method"] == "ffmpeg"
    # Before: (10.0, 20.0, "さかさまのせりふ") - ffmpeg showed it until the next event.
    assert parse_srt(output) == [(1.0, 3.0, "いちばんめのせりふ"), (60.0, 62.0, "さいごのせりふ")]


# --- 4. timestamps -------------------------------------------------------------------------------


def test_negative_and_long_srt_times() -> None:
    cues = _parse_srt_text(
        "1\n-00:00:01,000 --> 00:00:02,000\nまえから\n\n"
        "2\n-00:00:09,000 --> -00:00:07,000\nみえない\n\n"
        "3\n100:00:01,000 --> 100:00:03,000\nとおい\n\n"
        "4\n00:00:05,000 --> 00:00:06,500\nふつう\n"
    )
    # Before: "-00:00:01" read as +1 s, the line before the file start kept, the 100-hour line lost.
    assert cues == [(0.0, 2.0, "まえから"), (360001.0, 360003.0, "とおい"), (5.0, 6.5, "ふつう")]
    assert srt_timestamp(360001.0) == "100:00:01,000"
    # Never past what the parser reads back.
    assert srt_timestamp(1e12) == "9999:59:59,999"
    assert _parse_srt_text(f"1\n{srt_timestamp(9999 * 3600.0)} --> {srt_timestamp(1e12)}\nはし\n") == [
        (9999 * 3600.0, 9999 * 3600.0 + 3599.999, "はし")
    ]


# --- 5. ruby --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "plain"),
    [
        ("<ruby>漢<rt>かん</rt>字<rt>じ</rt></ruby>を読む", "漢字を読む"),
        ("<ruby><rb>漢</rb><rt>かん</rt><rb>字</rb><rt>じ</rt></ruby>", "漢字"),
        ("<ruby>漢字<rp>(</rp><rt>かんじ</rt><rp>)</rp></ruby>です", "漢字です"),
        ("<ruby>漢<rt>かん</ruby>と<ruby>字<rt>じ</rt></ruby>", "漢と字"),
        ("<いつも朝が苦手だった>", "いつも朝が苦手だった"),
    ],
)
def test_ruby_with_several_readings_keeps_every_base(raw: str, plain: str) -> None:
    assert plain_subtitle_text(raw) == plain


# --- 6. the prepared-result manifest -------------------------------------------------------------


def _prepared(tmp_path: Path) -> tuple[AppConfig, Path, Path, Path]:
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    alass = tmp_path / "bin" / "alass"
    alass.parent.mkdir(parents=True)
    alass.write_text("#!/bin/sh\n", encoding="utf-8")
    alass.chmod(0o755)
    config.tools.alass = str(alass)
    video = tmp_path / "Invented Show - 01.mkv"
    video.write_bytes(b"video")
    raw = tmp_path / "raw.srt"
    write_exact_srt([(1.0, 2.0, "もとのせりふ")], raw)
    prepared = tmp_path / "prepared.srt"
    write_exact_srt([(1.5, 2.5, "もとのせりふ")], prepared)
    pipeline_cache.save_final_pipeline_result(
        video,
        config,
        subtitle=prepared,
        subtitle_id=None,
        dependency=prepared,
        source="external",
        raw_source=raw,
    )
    return config, video, raw, prepared


def test_a_raw_subtitle_edited_in_place_is_prepared_again(tmp_path: Path) -> None:
    config, video, raw, _prepared_path = _prepared(tmp_path)
    assert pipeline_cache.load_final_pipeline_result(video, config) is not None
    write_exact_srt([(1.0, 2.0, "なおしたせりふ")], raw)
    os.utime(raw, ns=(time.time_ns() + 10**9,) * 2)
    assert pipeline_cache.load_final_pipeline_result(video, config) is None


def test_a_touched_raw_subtitle_or_a_pruned_one_keeps_the_result(tmp_path: Path) -> None:
    config, video, raw, _prepared_path = _prepared(tmp_path)
    os.utime(raw, ns=(time.time_ns() + 10**9,) * 2)  # same bytes, new time
    assert pipeline_cache.load_final_pipeline_result(video, config) is not None
    raw.unlink()  # the download cache was pruned: the prepared file is still right
    assert pipeline_cache.load_final_pipeline_result(video, config) is not None


def test_new_step_versions_or_another_alass_prepare_again(tmp_path: Path, monkeypatch) -> None:
    config, video, _raw, _prepared_path = _prepared(tmp_path)
    monkeypatch.setattr(pipeline_cache, "pipeline_steps_version", lambda: "clock-blocks-99")
    assert pipeline_cache.load_final_pipeline_result(video, config) is None
    monkeypatch.undo()

    config, video, _raw, _prepared_path = _prepared(tmp_path / "second")
    alass = Path(config.tools.alass)
    alass.write_text("#!/bin/sh\n# another build\n", encoding="utf-8")
    assert pipeline_cache.load_final_pipeline_result(video, config) is None


def test_an_old_manifest_stays_valid_and_is_completed(tmp_path: Path) -> None:
    config, video, raw, _prepared_path = _prepared(tmp_path)
    manifest = pipeline_cache._manifest_path(video, config)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for key in ("raw_source_snapshot", "pipeline_steps", "alass_identity"):
        payload.pop(key)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert pipeline_cache.load_final_pipeline_result(video, config) is not None
    completed = json.loads(manifest.read_text(encoding="utf-8"))
    assert completed["pipeline_steps"] == pipeline_cache.pipeline_steps_version()
    assert completed["raw_source_snapshot"]["path"] == str(raw.resolve())
    # From now on its inputs are watched.
    write_exact_srt([(1.0, 2.0, "べつのせりふです")], raw)
    assert pipeline_cache.load_final_pipeline_result(video, config) is None


def test_the_alass_cache_key_names_the_binary(tmp_path: Path, monkeypatch) -> None:
    alass = tmp_path / "alass"
    alass.write_text("one", encoding="utf-8")
    first = pipeline_cache.tool_identity_key(str(alass))
    assert pipeline_cache.tool_identity_key(str(tmp_path / "missing")) == "missing"
    reference, source = tmp_path / "reference.srt", tmp_path / "source.srt"
    write_exact_srt([(1.0, 2.0, "せりふ")], reference)
    write_exact_srt([(2.0, 3.0, "せりふ")], source)
    tags: list[str] = []

    class Keyed(Exception):
        pass

    def fingerprint(_video, _subtitle, _config, *, tag: str = "") -> str:
        tags.append(tag)
        raise Keyed

    monkeypatch.setattr(syncing, "_fingerprint", fingerprint)
    for _build in range(2):
        with pytest.raises(Keyed):
            syncing.synchronize_with_alass(
                reference, source, tmp_path / "cache", SyncConfig(), alass_path=str(alass)
            )
        alass.write_text("another build", encoding="utf-8")
    # Before: one key for every ALASS build.
    assert tags[0].endswith(":binary=" + first) and tags[0] != tags[1]


# --- 7. TV tags ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "rank"),
    [
        ("Hoshi no Michi EX - 01.srt", "?"),
        ("[Group][Kumo TS][01][1080p].ass", "?"),
        ("Natsu CX 1080p.srt", "?"),
        ("Katanagatari - 01 (CX).srt", "TV"),
        ("[SomeRaws] Kanpeki - 08 (TX 1920x1080 x265 AAC).srt", "TV"),
        ("Show - 03 [1080p TX].srt", "TV"),
        ("Show - 05 (テレ東 TX).srt", "TV"),
        ("Show.S01E03.ts.srt", "TV"),
        ("Show - 02 [EX].srt", "TV"),
        ("Show - 02 (AT-X).srt", "TV"),
    ],
)
def test_short_tv_tags_count_only_as_release_tags(name: str, rank: str) -> None:
    assert candidate_choice.source_rank(name) == rank


# --- 8. constant speech clock: a mark at 0 s --------------------------------------------------------

_SYLLABLES = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわ"


def _line(index: int) -> str:
    rng = random.Random(index)
    return "".join(rng.choice(_SYLLABLES) for _ in range(8))


def test_a_mark_at_the_file_start_does_not_refuse_the_speech_clock(tmp_path: Path) -> None:
    cues = [(0.0, 0.1, "♪")]
    segments = []
    for index in range(40):
        start = 20.0 + index * 30.0
        text = _line(index)
        cues.append((start, start + 2.0, text))
        segments.append(
            {
                "start": start - 0.5,
                "end": start + 1.5,
                "words": [{"word": text, "start": start - 0.5, "end": start, "probability": 0.95}],
            }
        )
    source = tmp_path / "source.srt"
    write_exact_srt(cues, source)
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
    output, meta = recover_constant_speech_clock(source, transcript, tmp_path / "cache")
    # Before: "constant_shift_would_clamp_source" for the 0.1 s mark.
    assert meta["reason"] == "distributed_exact_word_clock" and meta["shift_seconds"] == -0.5
    shifted = parse_srt(output)
    assert shifted[0] == (19.5, 21.5, _line(0)) and len(shifted) == 40


# --- 9. cold open: moved ends ----------------------------------------------------------------------


COLD_TRUTH = [3.0, 7.0, 11.0, 15.0, 19.0]


def _cold_text_score(cues):
    gaps = [abs(start - true) for (start, _end, _text), true in zip(cues, COLD_TRUTH)]
    return sum(max(0.0, 1.0 - gap) for gap in gaps) / len(gaps)


def _cold_open_episode(after_block):
    output = [(t - 5.0, t - 5.0 + 4.0, f"せりふ{i}ばんめです") for i, t in enumerate(COLD_TRUTH)]
    output += after_block
    output += [(100.0 + 3.0 * i, 102.0 + 3.0 * i, f"ほんぺん{i}のせりふ") for i in range(20)]
    onsets = [t + 0.1 for t in COLD_TRUTH] + [100.1 + 3.0 * i for i in range(20)]
    return output, onsets


def test_a_moved_cold_open_does_not_run_into_the_song_marks() -> None:
    # The opening's marks are on the old clock like the block was (The Fable-like shape).
    output, onsets = _cold_open_episode([(20.2, 35.0, "♪～"), (35.0, 90.0, "♪～")])
    moved, info = cold_open.check_cold_open(output, onsets, _cold_text_score)
    assert info["applied"] is True and info["shift_seconds"] == 5.0
    assert info["version"] == "cold-open-2" and info["song_marks_moved"] == 1
    # The lines keep their length; before, the last one ran over the song mark from 20.2 s.
    assert [cue[:2] for cue in moved[:5]] == [(t, t + 4.0) for t in COLD_TRUTH]
    assert moved[5] == (pytest.approx(23.1), 35.0, "♪～")
    assert moved[6:] == output[6:]


def test_a_moved_cold_open_ends_before_the_next_dialogue_line() -> None:
    output, onsets = _cold_open_episode([(19.5, 19.8, "♪"), (20.0, 22.0, "つづきのせりふです")])
    moved, info = cold_open.check_cold_open(output, onsets, _cold_text_score)
    assert info["applied"] is True and info["shift_seconds"] == 5.0
    # Before: the last line (19-23 s) covered the next line from 20 s.
    assert moved[4][0] == pytest.approx(19.0) and moved[4][1] == pytest.approx(19.9)
    assert moved[5] == (19.5, 19.8, "♪")  # too short to start after the block: kept
    assert moved[6:] == output[6:]


# --- 10. timeline: a stray line far past the end ----------------------------------------------------


def _timeline_pair(stray: bool) -> tuple[list, list]:
    rng = random.Random(7)
    t = 5.0
    reference = []
    for index in range(200):
        duration = rng.uniform(1.0, 3.5)
        reference.append((t, t + duration, f"せりふ{index}"))
        t += duration + rng.uniform(0.2, 6.0)
    source = [(s - 3.2, e - 3.2, x) for s, e, x in reference if s > 4]
    if stray:
        source.append((99999.0, 100001.0, "おまけ"))
    return source, reference


def test_a_stray_line_far_past_the_end_does_not_stretch_the_grid(tmp_path: Path, monkeypatch) -> None:
    results = {}
    for stray in (False, True):
        source, reference = _timeline_pair(stray)
        folder = tmp_path / str(stray)
        write_exact_srt(source, folder / "s.srt")
        write_exact_srt(reference, folder / "r.srt")
        calls = []
        original = timeline_alignment._window_candidates
        monkeypatch.setattr(
            timeline_alignment,
            "_window_candidates",
            lambda **kwargs: calls.append(kwargs["center"]) or original(**kwargs),
        )
        path, result = timeline_alignment._align_subtitle_timelines(
            folder / "s.srt", folder / "r.srt", folder / "cache"
        )
        monkeypatch.undo()
        results[stray] = (len(calls), parse_srt(Path(path)), result)
    plain_windows, plain_cues, plain = results[False]
    stray_windows, stray_cues, stray_result = results[True]
    # Before: ~2800 windows up to 100000 s instead of ~30.
    assert stray_windows == plain_windows
    assert stray_result["accepted"] is True and stray_result["timeline_stray_tail_cues"] == 1
    assert stray_cues[:-1] == plain_cues and stray_cues[-1][2] == "おまけ"
    assert "timeline_stray_tail_cues" not in plain


def test_a_late_scene_after_the_reference_is_no_stray_tail() -> None:
    reference = [(float(t), t + 1.0, "x") for t in range(10, 1400, 10)]
    near = [(1500.0, 1501.0, "a")]  # within the offset search and margin
    keep, dropped = timeline_alignment._without_stray_tail(reference + near, reference, 120.0)
    assert dropped == 0 and keep[-1] == near[0]


# --- write_srt numbering, one read of an ASS file --------------------------------------------------


def test_write_srt_numbers_the_cues_it_writes(tmp_path: Path) -> None:
    path = write_srt([(1.0, 2.0, "あ"), (3.0, 4.0, "{\\an8}"), (5.0, 6.0, "い")], tmp_path / "n.srt")
    # Before: "1 ... 3": the cue left without text left a gap.
    assert path.read_text(encoding="utf-8") == (
        "1\n00:00:01,000 --> 00:00:02,000\nあ\n\n2\n00:00:05,000 --> 00:00:06,000\nい\n"
    )


def test_an_ass_file_is_read_once_when_converted(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Invented Show - 01.ja.ass"
    source.write_text(ASS, encoding="utf-8")
    reads = []
    original = Path.read_bytes

    def counted(self: Path) -> bytes:
        if self == source:
            reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", counted)
    output, info = convert_to_plain_srt(source, tmp_path / "cache", ffmpeg_path=str(tmp_path / "no-ffmpeg"))
    assert info["converted"] is True
    assert len(reads) == 1
