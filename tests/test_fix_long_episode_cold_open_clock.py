"""A long episode's cold open (more than ten lines, a few percent of the episode) without
text support in a small STT model's transcript follows the next clock instead of failing
the whole speech clock (and leaving the episode unaligned).  Invented data only."""

from __future__ import annotations

import random
from pathlib import Path

import pudge.syncing as syncing
from pudge.subtitle_formats import parse_srt, write_srt

_KANA = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"


def _lines(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(_KANA) for _ in range(rng.randint(10, 16))) for _ in range(count)]


def _garble(text: str, rng: random.Random, keep: float) -> str:
    return text if rng.random() < keep else "".join(rng.choice(_KANA) for _ in text)


def _plateau(tmp_path: Path, cold_open: int, count: int = 400, break_after: int = 250):
    texts = _lines(count, seed=4)
    times, start = [], 8.0
    for index in range(count):
        if index == break_after:
            start += 3.0
        times.append((start, start + 3.0))
        start += 4.0
    rng = random.Random(9)
    speech = []
    for index, ((a, b), text) in enumerate((zip(times, texts))):
        if index < cold_open:
            continue  # music over the cold open: nothing transcribed
        shift = 148.0 if index < break_after else 308.0
        speech.append((a + shift, b + shift, _garble(text, rng, 0.6)))
    reference = tmp_path / "reference.ja.srt"
    write_srt(speech, reference)
    mapped = [
        a + (153.0 if i < cold_open else 148.0 if i < break_after else 308.0)
        for i, (a, _b) in enumerate(times)
    ]
    rejected = tmp_path / "clock.srt"
    write_srt([(m, m + 2.0, t) for m, t in zip(mapped, texts)], rejected)
    source_cues = [(a, b, t) for (a, b), t in zip(times, texts)]
    return syncing._text_clock_plateau_output(source_cues, mapped, reference, rejected), times


def test_cold_open_of_eighteen_lines_in_a_long_episode_follows_its_neighbour(tmp_path: Path):
    plateau, times = _plateau(tmp_path, cold_open=18)
    assert plateau["accepted"] is True, plateau.get("gate_failures")
    folded = plateau["folded_edge_clocks"]
    assert folded and folded[0]["first_cue"] == 0 and abs(folded[0]["to_shift"] - 148.0) < 0.06
    cues = parse_srt(Path(plateau["output"]))
    assert all(abs(c[0] - a - 148.0) < 0.05 for c, (a, _b) in zip(cues[:250], times[:250]))
    assert all(abs(c[0] - a - 308.0) < 0.05 for c, (a, _b) in zip(cues[250:], times[250:]))


def test_a_long_unsupported_opening_part_still_fails_the_clock(tmp_path: Path):
    # Forty lines (10 %) without any text support are not a cold open to fold over.
    plateau, _times = _plateau(tmp_path, cold_open=40)
    assert plateau["accepted"] is False
    assert not plateau.get("folded_edge_clocks")
    assert any(str(f).startswith("clock_text_support:0-") for f in plateau["gate_failures"])
