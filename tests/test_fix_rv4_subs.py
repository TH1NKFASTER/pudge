"""Subtitle pipeline review (rv4): decision paths read end to end.

Invented data only.

* A WebVTT candidate reaches the sync engines as SRT (they refused every .vtt file).
* WebVTT ruby readings leave with their tags; machine-translated names with underscores.
* The cold-open block does not end at a short spoken line; the speech-moved cold open is
  seen through a music mark over the opening; the epilogue through a line after a later gap.
* The output guard sees lines piled where Pudge's own writer puts them (0.1 s).
* A collapsed map does not divide by zero in the stretch check; a failed rename keeps the
  previous original subtitles; a failed ALASS publish is an ALASS failure; an endless
  container duration is no duration.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

from pudge import alignment_guard, cli, syncing
from pudge.config import SyncConfig
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import (
    convert_to_plain_srt,
    parse_srt,
    parse_webvtt,
    subtitle_name_is_machine_translated,
    write_srt,
)
from pudge.subtitles import cold_open, originals, scale_check
from pudge.subtitles.discovery import deduplicate_candidates

VTT = (
    "WEBVTT\n\n"
    "1\n00:00:05.000 --> 00:00:07.000\nあさのそらはあおい\n\n"
    "2\n00:00:09.000 --> 00:00:11.000 line:0\n<v Aoi>きのうのよるはさむかった</v>\n\n"
    "3\n00:00:13.000 --> 00:00:15.000\nねこがやねのうえでねている\n"
)


def _candidate(path: Path, score: float = 80.0) -> SubtitleCandidate:
    return SubtitleCandidate(
        path=path,
        source="jimaku",
        score=score,
        name=path.name,
        episode=1,
        verified_japanese=True,
        details={"episode_match": "exact"},
    )


# --- WebVTT candidates ---------------------------------------------------------------------------


def test_a_webvtt_file_reaches_the_engines_as_srt(tmp_path: Path, monkeypatch) -> None:
    vtt = tmp_path / "Invented Show - 01.ja.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    seen: list[Path] = []

    def engines(video, subtitle, cache_dir, config, **_kwargs):
        seen.append(Path(subtitle))
        return subtitle, {"reason": "alignment_failed", "sync_was_successful": False}

    monkeypatch.setattr(syncing, "_optimize_subtitle_unguarded", engines)
    monkeypatch.setattr(syncing, "_media_timeline", lambda *_a, **_k: None)
    config = SyncConfig()
    config.japanese_stt_fallback = False
    syncing.optimize_subtitle(tmp_path / "video.mkv", vtt, tmp_path / "cache", config)
    assert [path.suffix for path in seen] == [".srt"]
    assert [text for _s, _e, text in parse_srt(seen[0])] == [
        "あさのそらはあおい",
        "きのうのよるはさむかった",
        "ねこがやねのうえでねている",
    ]


def test_webvtt_candidates_are_compared_as_srt(tmp_path: Path, monkeypatch) -> None:
    vtt = tmp_path / "Invented Show - 01.ja.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    srt = tmp_path / "Invented Show - 01.srt"
    write_srt([(1.0, 2.0, "べつのふぁいるのせりふ")], srt)
    seen: list[SubtitleCandidate] = []

    def compare(video, candidates, cache_dir, config, **_kwargs):
        seen.extend(candidates)
        return None, None, {"reason": "all_candidate_quality_checks_failed", "sync_was_successful": False}

    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded", compare)
    monkeypatch.setattr(syncing, "_media_timeline", lambda *_a, **_k: None)
    config = SyncConfig()
    config.japanese_stt_fallback = False
    syncing.optimize_candidates(
        tmp_path / "video.mkv", [_candidate(vtt, 90.0), _candidate(srt, 70.0)], tmp_path / "cache", config
    )
    assert [candidate.path.suffix for candidate in seen] == [".srt", ".srt"]
    # The name (release tags, the machine-translation mark), score and details stay.
    assert seen[0].name == vtt.name and seen[0].score == 90.0
    assert seen[0].details == {"episode_match": "exact"}
    assert seen[1].path == srt


def test_the_last_resort_speech_clock_gets_a_webvtt_file_as_srt(tmp_path: Path, monkeypatch) -> None:
    vtt = tmp_path / "Invented Show - 01.ja.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    seen: list[Path] = []

    def last_resort(video, candidates, cache_dir, config, **_kwargs):
        seen.extend(candidate.path for candidate in candidates)
        return None

    monkeypatch.setattr(syncing, "_last_resort_stt_clock", last_resort)
    rejected = {"reason": "alass_unsupported_format", "sync_was_successful": False}
    assert (
        syncing.rescue_single_candidate(
            tmp_path / "video.mkv", _candidate(vtt), rejected, tmp_path / "cache", SyncConfig()
        )
        is None
    )
    assert [path.suffix for path in seen] == [".srt"]


def test_the_cli_hands_webvtt_candidates_over_as_srt(tmp_path: Path) -> None:
    vtt = tmp_path / "Invented Show - 01.ja.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    empty = tmp_path / "Invented Show - 01.empty.vtt"
    empty.write_text("WEBVTT\n\nNOTE nothing here\n", encoding="utf-8")
    srt = tmp_path / "Invented Show - 01.srt"
    write_srt([(1.0, 2.0, "べつのふぁいるのせりふ")], srt)
    given = [_candidate(vtt), _candidate(empty), _candidate(srt)]
    converted = cli._webvtt_candidates_as_srt(given, tmp_path / "cache")
    assert converted[0].path.suffix == ".srt" and converted[0].name == vtt.name
    assert len(parse_srt(converted[0].path)) == 3
    # One that cannot be converted stays as it is; the others are the same objects.
    assert converted[1].path == empty
    assert converted[2] is given[2]
    assert given[0].path == vtt


def test_a_webvtt_copy_of_an_srt_is_one_candidate(tmp_path: Path) -> None:
    vtt = tmp_path / "Invented Show - 01.ja.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    converted, _result = convert_to_plain_srt(vtt, tmp_path / "cache")
    srt = tmp_path / "Invented Show - 01.ja.srt"
    srt.write_bytes(converted.read_bytes())
    kept, removed = deduplicate_candidates(
        [_candidate(vtt, 90.0), _candidate(srt, 70.0)], tmp_path / "cache", ffmpeg_path="ffmpeg"
    )
    assert removed == 1 and [candidate.path for candidate in kept] == [srt]


def test_webvtt_ruby_readings_leave_with_their_tags() -> None:
    text = (
        "WEBVTT\n\n"
        "00:01.000 --> 00:03.000\n"
        "<ruby>東<rt>とう</rt>京<rt>きょう</rt></ruby>へ<ruby>行<rp>(</rp><rt>い</rt><rp>)</rp></ruby>く\n\n"
        "00:04.000 --> 00:06.000\n"
        "<c.yellow><ruby>猫<rt>ねこ</ruby>がいる</c>\n"
    )
    assert parse_webvtt(text) == [(1.0, 3.0, "東京へ行く"), (4.0, 6.0, "猫がいる")]


# --- machine-translated names -------------------------------------------------------------------


def test_machine_translated_names_with_underscores_are_seen() -> None:
    for name in (
        "Invented_Show_01_AI_Translated.srt",
        "Invented_Show_01_MTL.ass",
        "Invented_Show_01_translated_from_english.srt",
        "Invented_Show_01_machine_translation.srt",
        "Invented_Show_01_DeepL_TL.srt",
        "[Group] Invented Show - 01 [JPN] (AI Translated From English).ass",
    ):
        assert subtitle_name_is_machine_translated(name), name
    for name in (
        "Invented Show - 01.srt",
        "Kizuna AI - 01.srt",
        "Smart_Translation_Club_01.srt",
        "Invented.Show.01.HDTV.srt",
        "retranslated from english.srt",
    ):
        assert not subtitle_name_is_machine_translated(name), name


# --- the cold-open block ----------------------------------------------------------------------------

COLD_OPEN = [6.0, 9.5, 13.0, 16.5, 20.0, 24.0]
COLD_LINES = [
    "あさのそらはあおい",
    "きのうのよるはさむかった",
    "うん",
    "はやくがっこうへいこう",
    "え？",
    "かわのみずはつめたい",
]


def _episode(cold_shift: float) -> list[tuple[float, float, str]]:
    cues = [(t + cold_shift, t + cold_shift + 2.0, COLD_LINES[i]) for i, t in enumerate(COLD_OPEN)]
    cues += [(30.0 + 15.0 * k, 44.0 + 15.0 * k, "♪～") for k in range(5)]
    t = 110.0
    for i in range(120):
        cues.append((t, t + 2.0, f"本編の台詞{i}"))
        t += 4.0
    return cues


def test_a_short_spoken_line_does_not_end_the_cold_open() -> None:
    output = _episode(cold_shift=-5.0)
    head, following = cold_open.head_block(output)
    assert head == [0, 1, 2, 3, 4, 5] and following == 11

    rng = random.Random(3)
    onsets = sorted(
        [t + 0.1 for t in COLD_OPEN]
        + [cue[0] + 0.1 for cue in output[11:]]
        + [rng.uniform(0.0, 600.0) for _ in range(120)]
    )

    def text_score(cues):
        gaps = [abs(start - true) for (start, _end, _text), true in zip(cues, COLD_OPEN)]
        return sum(1.0 if gap <= 1.0 else 0.3 for gap in gaps) / len(gaps)

    moved, info = cold_open.check_cold_open(output, onsets, text_score)
    assert info["applied"] is True and info["shift_seconds"] == 5.0
    assert info["moved_cues"] == [0, 5]
    assert [round(cue[0], 3) for cue in moved[:6]] == COLD_OPEN


def test_marks_without_a_letter_are_still_no_dialogue() -> None:
    assert cold_open._song_line((10.0, 12.0, "…"))
    assert cold_open._song_line((10.0, 12.0, "♪♪"))
    assert cold_open._song_line((10.0, 12.0, "♪～"))
    assert not cold_open._song_line((10.0, 12.0, "はい"))
    assert not cold_open._song_line((10.0, 12.0, "え？"))


def _stable_episode() -> list[tuple[float, float, str]]:
    cues = [(10.0 + 5.0 * i, 12.0 + 5.0 * i, f"ぼうとうのせりふ{i}") for i in range(6)]
    cues += [(140.0 + 4.0 * i, 142.0 + 4.0 * i, f"ほんぺんのせりふ{i}") for i in range(60)]
    return cues


def test_a_music_mark_over_the_opening_does_not_hide_the_moved_cold_open() -> None:
    plain = _stable_episode()
    # The same file with a mark over the whole opening song between the two blocks.
    marked = [*plain[:6], (40.0, 135.0, "♪～"), *plain[6:]]
    reference = [(start, end, f"line {index}") for index, (start, end, _text) in enumerate(plain)]
    for timeline in (plain, marked):
        speech = [
            (start + 72.0, end + 72.0, text) if text.startswith("ぼうとう") else (start, end, text)
            for start, end, text in timeline
        ]
        moved, meta = syncing._cold_open_block_moved(timeline, speech, reference, {})
        assert moved is True, meta
        assert meta["reason"] == "speech_moved_cold_open_off_the_reference"
        assert meta["cold_open_lines"] == 6
        # Speech that keeps the cold open is left alone either way.
        kept, meta = syncing._cold_open_block_moved(timeline, list(timeline), reference, {})
        assert kept is False and meta["reason"] == "speech_keeps_the_cold_open_clock"


def test_a_line_after_a_later_gap_does_not_hide_the_moved_epilogue() -> None:
    body = [(20.0 + 4.0 * i, 22.0 + 4.0 * i, f"ほんぺんのせりふ{i}") for i in range(60)]
    epilogue = [(360.0 + 4.0 * i, 362.0 + 4.0 * i, f"おわりのせりふ{i}") for i in range(6)]
    preview = [(440.0, 442.0, "じかいのだいめい")]
    reference = [(start, end, "line") for start, end, _text in [*body, *epilogue, *preview]]
    for timeline in ([*body, *epilogue], [*body, *epilogue, *preview]):
        speech = [
            (start - 36.0, end - 36.0, text) if text.startswith("おわり") else (start, end, text)
            for start, end, text in timeline
        ]
        moved, meta = syncing._speech_moves_epilogue_off_the_reference(timeline, speech, reference)
        assert moved is True, meta
        assert meta["reason"] == "speech_moved_epilogue_off_the_reference"
        kept, meta = syncing._speech_moves_epilogue_off_the_reference(timeline, list(timeline), reference)
        assert kept is False and meta["reason"] == "speech_keeps_the_epilogue_clock"


# --- the output guard -------------------------------------------------------------------------------


def test_lines_piled_at_the_writers_first_start_are_seen(tmp_path: Path) -> None:
    raw_cues = [(100.0 + 3.0 * i, 102.0 + 3.0 * i, f"せりふ{i}です") for i in range(100)]
    raw, final = tmp_path / "raw.srt", tmp_path / "final.srt"
    write_srt(raw_cues, raw)
    # A map 250 s too early: Pudge's writer puts the first fifty lines at 0.1 s, not 0.0 s.
    write_srt([(s - 250.0, e - 250.0, t) for s, e, t in raw_cues], final, preserve_order=True)
    assert parse_srt(final)[0][0] == 0.1
    standalone = alignment_guard.validate_final_standalone(final)
    assert standalone["status"] == "rejected" and standalone["reason"] == "lines_piled_at_zero"
    verdict = alignment_guard.validate_alignment_output(raw, final)
    assert verdict["status"] == "rejected" and "clamped_starts" in verdict["reason"]
    assert verdict["diagnostics"]["new_clamped_starts"] >= 40

    # A good file with its first line at the very start stays accepted.
    good = tmp_path / "good.srt"
    write_srt([(0.0, 2.0, "さいしょのせりふ"), *[(s - 95.0, e - 95.0, t) for s, e, t in raw_cues]], good)
    assert alignment_guard.validate_final_standalone(good)["status"] == "accepted"
    shifted = tmp_path / "shifted.srt"
    write_srt([(s - 95.0, e - 95.0, t) for s, e, t in raw_cues], shifted)
    verdict = alignment_guard.validate_alignment_output(raw, shifted)
    assert verdict["status"] == "accepted" and verdict["diagnostics"]["new_clamped_starts"] == 0


# --- smaller ones -----------------------------------------------------------------------------------


def test_a_collapsed_map_is_no_stretch() -> None:
    output = [(100.0, 101.0, f"せりふ{i}") for i in range(60)]
    pairs = [(i, i * 20.0) for i in range(60)]
    cues, info = scale_check.check_stretch(output, pairs, [100.0 + i for i in range(50)])
    assert info["applied"] is False and info["reason"] == "collapsed_map"
    assert cues == output


def test_a_failed_rename_keeps_the_previous_originals(tmp_path: Path, monkeypatch) -> None:
    root, video = tmp_path / "originals", tmp_path / "video.mkv"
    first = tmp_path / "first.srt"
    write_srt([(1.0, 2.0, "まえのふぁいる")], first)
    kept = originals.save_originals(root, video, [first])
    assert [path.name for path in kept] == ["01 first.srt"]
    second = tmp_path / "second.srt"
    write_srt([(1.0, 2.0, "あたらしいふぁいる")], second)
    real_replace = originals.os.replace

    def replace(source, target):
        if str(source).endswith(".tmp"):
            raise OSError("Directory not empty")
        return real_replace(source, target)

    monkeypatch.setattr(originals.os, "replace", replace)
    after = originals.save_originals(root, video, [second])
    assert [path.name for path in after] == ["01 first.srt"]
    assert after[0].read_bytes() == first.read_bytes()
    assert [path.name for path in root.iterdir() if path.name.endswith(".tmp")] == []

    monkeypatch.undo()
    assert [path.name for path in originals.save_originals(root, video, [second])] == ["01 second.srt"]
    assert [path.name for path in root.iterdir()] == [originals.originals_dir(root, video).name]


def test_a_failed_alass_rename_is_an_alass_failure(tmp_path: Path, monkeypatch) -> None:
    import stat
    import sys

    script = tmp_path / "alass"
    script.write_text(f"#!{sys.executable}\nimport shutil, sys\nshutil.copyfile(sys.argv[2], sys.argv[3])\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    reference, source = tmp_path / "reference.srt", tmp_path / "source.srt"
    write_srt([(7.0 + 3.0 * i, 9.0 + 3.0 * i, f"せりふ{i}") for i in range(20)], reference)
    write_srt([(8.0 + 3.0 * i, 10.0 + 3.0 * i, f"せりふ{i}") for i in range(20)], source)
    cache = tmp_path / "cache"
    real_replace = syncing.os.replace

    def replace(staged, target):
        if Path(target).parent.name == "alass" and Path(target).suffix == ".srt":
            raise OSError("No space left on device")
        return real_replace(staged, target)

    monkeypatch.setattr(syncing.os, "replace", replace)
    path, result = syncing.synchronize_with_alass(
        reference, source, cache, SyncConfig(), alass_path=str(script)
    )
    assert path == source and result["reason"] == "alass_error" and result["sync_was_successful"] is False
    # Neither a hidden copy nor metadata of an output that does not exist.
    assert list((cache / "alass").iterdir()) == []


def test_an_endless_container_duration_is_no_duration(tmp_path: Path, monkeypatch) -> None:
    def probe(answer: str):
        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=answer, stderr="")

        return run

    for answer, expected in (("inf\n", None), ("nan\n", None), ("N/A\n", None), ("1420.5\n", 1420.5)):
        monkeypatch.setattr(syncing.subprocess, "run", probe(answer))
        assert syncing._run_duration_probe(tmp_path / "video.mkv", "ffprobe") == expected


def test_a_double_episode_drops_machine_translations_too(tmp_path: Path, monkeypatch) -> None:
    import argparse
    import logging

    from pudge import double_episode
    from pudge.config import AppConfig
    from pudge.filename import parse_anime_filename

    def files(episode: int) -> list[SubtitleCandidate]:
        real = tmp_path / f"Invented Show - {episode:02d}.srt"
        machine = tmp_path / f"Invented_Show_{episode:02d}_AI_Translated.srt"
        for path in (real, machine):
            write_srt([(1.0, 2.0, "あさのそらはあおい")], path)
        return [_candidate(machine, 95.0), _candidate(real, 80.0)]

    monkeypatch.setattr(
        cli, "_double_episode_candidates", lambda video, identity, episode, *_a, **_k: files(episode)
    )
    monkeypatch.setattr(syncing, "_probe_duration", lambda *_a, **_k: 2800.0)
    given: list[list[str]] = []

    def prepare(video, first, second, **_kwargs):
        given.extend([[c.name for c in first], [c.name for c in second]])
        return double_episode.DoubleEpisodeResult(None, reason="stopped_by_the_test")

    monkeypatch.setattr(double_episode, "prepare_double_episode", prepare)
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    args = argparse.Namespace(
        prepare_only=False, force_search=False, resync=False, no_sync=False, verbose=False
    )
    video = tmp_path / "Invented Show - 01-02.mkv"
    video.write_bytes(b"not a real video")
    outcome = cli._prepare_double_episode_subtitles(
        video, parse_anime_filename(video.name), (1, 2), args, config, None, None, logging.getLogger("test")
    )
    assert outcome[0] is None and outcome[1]["reason"] == "stopped_by_the_test"
    assert given == [["Invented Show - 01.srt"], ["Invented Show - 02.srt"]]
