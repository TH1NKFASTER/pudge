"""TV captures (minutes of pre-roll, a kept commercial break): speech text clock as a few
constant clocks, last-resort speech after every candidate failed, and a bilingual file
whose audio "success" the Japanese-only files contradict.  Invented data only."""

from __future__ import annotations

import random
from pathlib import Path

import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles.text_clock_plateaus import fit_text_clock_plateaus

_KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"


def _lines(count: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(_KANA) for _ in range(rng.randint(9, 16))) for _ in range(count)]


def _source_timeline(count: int, break_after: int, break_gap: float) -> list[tuple[float, float]]:
    times = []
    start = 8.0
    for index in range(count):
        if index == break_after:
            start += break_gap - 2.0
        times.append((start, start + 3.0))
        start += 5.0
    return times


# --- fit -----------------------------------------------------------------------------


def _fit_input(break_gap: float = 3.0, noise: bool = True):
    times = _source_timeline(60, 30, break_gap)
    shifts: list[float | None] = []
    for index in range(60):
        shift = 130.0 if index < 30 else 290.0
        shifts.append(shift + (0.05 if index % 2 else -0.05))
    if noise:
        shifts[10] = 124.0  # one line pulled towards earlier speech
        shifts[29] = 210.0  # the line before the break ramps into it
        shifts[30] = 250.0
    return [a for a, _ in times], [b for _, b in times], shifts


def test_fit_reads_pre_roll_and_break_as_two_clocks_with_the_jump_at_the_pause():
    starts, ends, shifts = _fit_input()
    fit = fit_text_clock_plateaus(starts, ends, shifts)
    assert fit["accepted"] is True, fit
    assert [round(c["shift_seconds"]) for c in fit["clocks"]] == [130, 290]
    (jump,) = fit["jumps"]
    assert jump["kind"] == "inserted_time"
    assert round(jump["jump_seconds"]) == 160
    assert jump["source_time"] == starts[30]  # the real dialogue gap, not mid-ramp
    repaired = fit["shifts"]
    assert abs(repaired[10] - 130.0) < 0.2  # noisy single cue takes its clock
    assert abs(repaired[29] - 130.0) < 0.2 and abs(repaired[30] - 290.0) < 0.2
    assert abs(repaired[5] - 130.0) < 0.06  # every cue takes its clock (median of agreeing cues)


def test_fit_refuses_a_forward_jump_without_a_dialogue_gap():
    starts, _ends, shifts = _fit_input(break_gap=0.2)
    ends = [start + 4.8 for start in starts]  # continuous dialogue: no pause anywhere
    fit = fit_text_clock_plateaus(starts, ends, shifts)
    assert fit["accepted"] is False
    assert fit["reason"] == "forward_jump_without_dialogue_gap"


def test_fit_refuses_a_drifting_clock():
    starts = [8.0 + 5.0 * i for i in range(80)]
    ends = [s + 3.0 for s in starts]
    shifts = [130.0 - 4.5 * i for i in range(80)]  # a compressed, wrong text map
    fit = fit_text_clock_plateaus(starts, ends, shifts)
    assert fit["accepted"] is False
    assert fit["reason"] in {"too_many_clocks", "too_few_agreeing_cues", "weak_clock_beside_jump"}


def test_fit_keeps_squeezed_lines_of_missing_source_content():
    starts = [8.0 + 5.0 * i for i in range(40)]
    ends = [s + 3.0 for s in starts]
    shifts = [130.0] * 18 + [126.0, 122.0] + [118.0] * 20  # 12 s of the source cut
    fit = fit_text_clock_plateaus(starts, ends, shifts)
    assert fit["accepted"] is True, fit
    assert fit["jumps"][0]["kind"] == "missing_source_content"
    assert fit["shifts"][18] == 126.0 and fit["shifts"][19] == 122.0


# --- text clock -----------------------------------------------------------------------


def _write_episode(tmp_path: Path, break_gap: float = 3.0) -> tuple[Path, Path]:
    texts = _lines(60)
    times = _source_timeline(60, 30, break_gap)
    source = tmp_path / "source.ja.srt"
    write_srt([(a, b, t) for (a, b), t in zip(times, texts)], source)
    speech = []
    for index, ((a, b), text) in enumerate(zip(times, texts)):
        shift = 130.0 if index < 30 else 290.0
        speech.append((a + shift, b + shift, text))
    reference = tmp_path / "reference.ja.srt"
    write_srt(speech, reference)
    return source, reference


def test_text_clock_of_a_tv_capture_offers_the_constant_clock_reading(tmp_path: Path):
    source, reference = _write_episode(tmp_path)
    output, diagnostics = syncing._stt_text_clock_candidate(source, reference, tmp_path, model="test")
    # The ALASS-style check still rejects the raw clock (a 160 s jump at a 3 s pause) ...
    assert output is None
    assert str(diagnostics["reject_reason"]).startswith("transition_safety:")
    # ... but the constant-clock reading is accepted, with its jump at the pause.
    plateau = diagnostics["plateau_clock"]
    assert plateau["accepted"] is True, plateau
    cues = parse_srt(Path(plateau["output"]))
    raw = parse_srt(source)
    assert all(abs(c[0] - r[0] - 130.0) < 0.35 for c, r in zip(cues[:30], raw[:30]))
    assert all(abs(c[0] - r[0] - 290.0) < 0.35 for c, r in zip(cues[30:], raw[30:]))


def test_text_clock_without_a_pause_at_the_break_stays_rejected(tmp_path: Path):
    source, reference = _write_episode(tmp_path, break_gap=0.3)
    output, diagnostics = syncing._stt_text_clock_candidate(source, reference, tmp_path, model="test")
    assert output is None
    assert diagnostics["plateau_clock"]["accepted"] is False


def test_speech_fallback_uses_the_constant_clock_only_after_alass_found_no_safe_map(
    tmp_path: Path, monkeypatch
):
    source, reference = _write_episode(tmp_path)
    video = tmp_path / "capture.ts"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        syncing, "prepare_japanese_stt_reference", lambda *a, **k: (reference, {"model": "t"})
    )
    calls = []

    def no_safe_alass(reference_path, subtitle, cache_dir, config, **kwargs):
        calls.append(config.alass_split_penalty)
        return subtitle, {"sync_was_successful": False, "reason": "alass_failed"}

    monkeypatch.setattr(syncing, "synchronize_with_alass", no_safe_alass)
    out, result = syncing._try_japanese_stt_fallback(
        video,
        source,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    assert calls, "speech ALASS maps are tried first"
    assert out is not None
    assert result["engine"] == "japanese-stt+text-clock-plateaus"
    assert result["reference_alignment_reliable"] is True
    assert syncing.subtitle_quality_accepted(result)[0] is True


# --- candidates -----------------------------------------------------------------------


def _candidate(path: Path, purity: str, score: float = 100.0) -> SubtitleCandidate:
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nテスト\n", encoding="utf-8")
    return SubtitleCandidate(
        path=path,
        source="jimaku",
        score=score,
        name=path.name,
        details={"episode_match": "exact", "entry_anilist_match": True, "language_purity": purity},
    )


def test_bilingual_success_contradicted_by_japanese_files_at_the_offset_limit(tmp_path: Path):
    bilingual = _candidate(tmp_path / "ep.ja-en.ass", "mixed_japanese_english")
    japanese = [_candidate(tmp_path / f"ep-{i}.ja.srt", "japanese_only") for i in range(3)]
    evaluated = [
        (
            bilingual,
            bilingual.path,
            {"sync_was_successful": True, "alignment_score": 3600.0, "offset_seconds": -9.4},
        )
    ]
    for index, candidate in enumerate(japanese):
        evaluated.append(
            (
                candidate,
                candidate.path,
                {
                    "sync_was_successful": False,
                    "reason": "quality_offset_exceeded",
                    "alignment_score": 7000.0 + index,
                    "offset_seconds": 118.8 + index * 0.4,
                },
            )
        )
    assert syncing._weak_mixed_language_successes(evaluated) == {bilingual.path}
    # Agreeing offset or a single contradicting file: no demotion.
    agreeing = [(c, p, dict(r, offset_seconds=119.0) if c is bilingual else r) for c, p, r in evaluated]
    assert syncing._weak_mixed_language_successes(agreeing) == set()
    assert syncing._weak_mixed_language_successes(evaluated[:2]) == set()


def test_failed_audio_candidates_still_get_the_full_alignment(tmp_path: Path, monkeypatch):
    video = tmp_path / "capture.ts"
    video.write_bytes(b"video")
    bilingual = _candidate(tmp_path / "ep.ja-en.ass", "mixed_japanese_english", 120.0)
    japanese = [_candidate(tmp_path / f"ep-{i}.ja.srt", "japanese_only", 100.0 - i) for i in range(2)]
    monkeypatch.setattr(syncing, "prepare_speech_reference", lambda *a, **k: (None, {"reason": "test"}))

    def fake_evaluate(_video, subtitle, *_a, **_k):
        if subtitle == bilingual.path:
            return subtitle, {
                "sync_was_successful": True,
                "reason": "applied",
                "alignment_score": 3600.0,
                "offset_seconds": -9.4,
            }
        return subtitle, {
            "sync_was_successful": False,
            "reason": "quality_offset_exceeded",
            "alignment_score": 7000.0,
            "offset_seconds": 119.0,
        }

    optimized: list[Path] = []

    def fake_optimize(_video, subtitle, *_a, **_k):
        optimized.append(subtitle)
        if subtitle == japanese[0].path:
            return subtitle, {
                "sync_was_successful": True,
                "reason": "applied",
                "reference_alignment_reliable": True,
            }
        return subtitle, {"sync_was_successful": False, "reason": "local validation rejected"}

    monkeypatch.setattr(syncing, "synchronize_subtitle", fake_evaluate)
    monkeypatch.setattr(syncing, "optimize_subtitle", fake_optimize)
    chosen, _output, _result = syncing._optimize_candidates_unguarded(
        video, [bilingual, *japanese], tmp_path / "cache", SyncConfig()
    )
    assert chosen is japanese[0]
    assert optimized[0] == japanese[0].path  # the contradicted bilingual file is not tried first


def test_speech_clock_is_the_last_resort_after_every_candidate_failed(tmp_path: Path, monkeypatch):
    video = tmp_path / "capture.ts"
    video.write_bytes(b"video")
    bilingual = _candidate(tmp_path / "ep.ja-en.ass", "mixed_japanese_english", 150.0)
    japanese = _candidate(tmp_path / "ep.ja.srt", "japanese_only", 90.0)
    episode = tmp_path / "episode"
    episode.mkdir()
    source, _reference = _write_episode(episode)
    japanese.path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    shifted = tmp_path / "shifted.srt"
    write_srt([(a + 130.0, b + 130.0, t) for a, b, t in parse_srt(japanese.path)], shifted)

    monkeypatch.setattr(
        syncing,
        "_optimize_candidates_unguarded",
        lambda *a, **k: (
            None,
            None,
            {"sync_was_successful": False, "reason": "all_candidate_quality_checks_failed"},
        ),
    )
    tried: list[Path] = []

    def fake_speech(_video, subtitle, *_a, **_k):
        tried.append(subtitle)
        return shifted, {
            "sync_was_successful": True,
            "reason": "applied",
            "engine": "japanese-stt+text-clock-plateaus",
            "reference_alignment_reliable": True,
        }

    monkeypatch.setattr(syncing, "_try_japanese_stt_fallback", fake_speech)
    chosen, output, result = syncing.optimize_candidates(
        video, [bilingual, japanese], tmp_path / "cache", SyncConfig()
    )
    assert chosen is japanese
    assert output == shifted
    assert tried == [japanese.path]  # Japanese-only first
    assert result["selection_reason"] == "last_resort_stt_after_all_candidates_failed"

    tried.clear()
    chosen, output, _ = syncing.optimize_candidates(
        video, [bilingual, japanese], tmp_path / "cache", SyncConfig(japanese_stt_fallback=False)
    )
    assert chosen is None and output is None and tried == []
