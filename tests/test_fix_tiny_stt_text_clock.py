"""TV captures with a small STT model (whisper-tiny): the transcript is worse, so a right
text clock scores below the fixed text score; the constant-clock reading is judged
against the same lines displaced (chance) instead.  Invented data only."""

from __future__ import annotations

import random
from pathlib import Path

import pudge.syncing as syncing
from pudge.subtitle_formats import parse_srt, write_srt

_KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"


def _lines(count: int, seed: int = 11) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(_KANA) for _ in range(rng.randint(10, 16))) for _ in range(count)]


def _garble(text: str, rng: random.Random, keep: float) -> str:
    """A poor transcription: about ``keep`` of the lines heard right, the rest misheard."""
    if rng.random() < keep:
        return text
    return "".join(rng.choice(_KANA) for _ in text)


def _timeline(count: int, break_after: int) -> list[tuple[float, float]]:
    times, start = [], 8.0
    for index in range(count):
        if index == break_after:
            start += 3.0
        times.append((start, start + 3.0))
        start += 5.0
    return times


def _episode(tmp_path: Path, *, keep: float, same_text: bool = True) -> tuple[Path, Path]:
    texts = _lines(70)
    times = _timeline(70, 35)
    source = tmp_path / "source.ja.srt"
    write_srt([(a, b, t) for (a, b), t in zip(times, texts)], source)
    rng = random.Random(5)
    spoken = texts if same_text else _lines(70, seed=99)
    speech = []
    for index, ((a, b), text) in enumerate(zip(times, spoken)):
        shift = 130.0 if index < 35 else 290.0
        speech.append((a + shift, b + shift, _garble(text, rng, keep)))
    reference = tmp_path / "reference.ja.srt"
    write_srt(speech, reference)
    return source, reference


def test_poor_transcript_of_a_tv_capture_still_reads_as_constant_clocks(tmp_path: Path):
    source, reference = _episode(tmp_path, keep=0.45)
    output, diagnostics = syncing._stt_text_clock_candidate(source, reference, tmp_path, model="tiny")
    assert output is None
    # The whole raw clock fails both the jump check and the fixed text score ...
    failures = diagnostics["gate_failures"]
    assert failures[0].startswith("transition_safety:")
    assert any(failure.startswith("text_score:") for failure in failures[1:]), failures
    # ... but its constant-clock reading is far above displaced chance and is accepted.
    plateau = diagnostics["plateau_clock"]
    assert plateau["accepted"] is True, plateau
    support = plateau["text_support"]
    assert support["score"] < 0.62 and support["score"] > 2 * support["chance_mean"]
    cues = parse_srt(Path(plateau["output"]))
    raw = parse_srt(source)
    errors = [c[0] - r[0] - (130.0 if i < 35 else 290.0) for i, (c, r) in enumerate(zip(cues, raw))]
    # Every line takes its clock (the misheard ones too, never the break): the per-line
    # text map of a poor transcript is noisy, the clock is not.
    assert all(abs(e) < 0.1 for e in errors), errors


def test_transcript_of_other_speech_is_not_a_clock(tmp_path: Path):
    source, reference = _episode(tmp_path, keep=0.45, same_text=False)
    output, diagnostics = syncing._stt_text_clock_candidate(source, reference, tmp_path, model="tiny")
    assert output is None
    plateau = diagnostics.get("plateau_clock")
    assert plateau is None or plateau["accepted"] is False


def test_unsupported_short_cold_open_clock_follows_its_neighbour(tmp_path: Path):
    texts = _lines(80, seed=3)
    times = _timeline(80, 50)
    source_cues = [(a, b, t) for (a, b), t in zip(times, texts)]
    rng = random.Random(8)
    speech = []
    for index, ((a, b), text) in enumerate(zip(times, texts)):
        if index < 6:
            continue  # the cold open is music: the transcript has nothing there
        shift = 148.0 if index < 50 else 308.0
        speech.append((a + shift, b + shift, _garble(text, rng, 0.6)))
    reference = tmp_path / "reference.ja.srt"
    write_srt(speech, reference)
    # The text map pulled the six cold-open lines 5 s off (no speech of their own).
    mapped = [a + (153.0 if i < 6 else 148.0 if i < 50 else 308.0) for i, (a, _b) in enumerate(times)]
    mapped[6] = times[6][0] + 156.5  # a line between both clocks, pulled the other way
    rejected = tmp_path / "clock.srt"
    write_srt([(m, m + 2.0, t) for m, t in zip(mapped, texts)], rejected)
    plateau = syncing._text_clock_plateau_output(source_cues, mapped, reference, rejected)
    assert plateau["accepted"] is True, plateau
    assert plateau["folded_edge_clocks"] and abs(plateau["folded_edge_clocks"][0]["to_shift"] - 148.0) < 0.06
    cues = parse_srt(Path(plateau["output"]))
    assert all(abs(c[0] - a - 148.0) < 0.05 for c, (a, _b) in zip(cues[:50], times[:50]))
