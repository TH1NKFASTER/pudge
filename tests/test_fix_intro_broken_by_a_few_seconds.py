"""An opening broken by a few different seconds is still one opening.

User (Assassination Classroom E07): "it did not offer to skip the opening here". The OP ran
0:38-2:05, but a few seconds in the middle (1:13-1:16) differed from the other episodes, so
the shared audio came in two pieces and only the second, 1:16-2:05, was offered (the user was
at 0:44). Episode 8 likewise kept a 48 s piece as its "OP" in the title library.
"""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from pudge import intro_skipper
from pudge.audio_fingerprints import best_shared_run, fingerprint_samples
from pudge.intro_library import VariantRef, empty_fingerprint, slice_fingerprint
from tests.test_intro_skipper import OP, place, speech


def _episode(seed: int, op_at: float, *, broken_at: float | None = None) -> np.ndarray:
    track = speech(seed, 360.0)
    place(track, OP, op_at)
    if broken_at is not None:  # 3.5 s over the OP that no other episode has
        place(track, speech(seed + 900, 3.5), op_at + broken_at, gain=1.5)
    return track


def test_a_few_different_seconds_do_not_cut_the_opening_in_two() -> None:
    seventh = fingerprint_samples(_episode(7, 38.0, broken_at=35.0))
    sixth = fingerprint_samples(_episode(6, 94.0))
    run = best_shared_run(seventh, sixth, min_seconds=30, max_seconds=180)
    assert run is not None
    assert abs(run.a_start - 38.0) <= 1.5 and abs(run.a_end - 123.0) <= 1.5, run


def test_two_different_stretches_are_not_joined() -> None:
    a = speech(21, 360.0)
    b = speech(22, 360.0)
    place(a, OP[: OP.size // 2], 30.0)
    place(b, OP[: OP.size // 2], 100.0)
    place(a, OP[OP.size // 2 :], 120.0)  # the second half 50 s later in one, right after in the other
    place(b, OP[OP.size // 2 :], 142.5)
    run = best_shared_run(fingerprint_samples(a), fingerprint_samples(b), min_seconds=30, max_seconds=180)
    assert run is not None and run.duration < 50.0


def _ef(n: int) -> intro_skipper.EpisodeFile:
    return intro_skipper.EpisodeFile(path=Path(f"/x/{n}.mkv"), episode=n, identity=f"id{n}")


def test_a_longer_shared_opening_wins_over_an_unconfirmed_piece() -> None:
    eighth = fingerprint_samples(_episode(8, 36.0))
    seventh = fingerprint_samples(_episode(7, 38.0))
    piece = slice_fingerprint(eighth, 74.0, 121.0)  # what the library kept of episode 8
    tail = empty_fingerprint(1000.0)
    found = intro_skipper._detect(
        _ef(8),
        (eighth, tail, 1400.0),
        [(_ef(7), (seventh, tail, 1400.0))],
        intro_skipper.DetectorSettings(),
        {"intro": [VariantRef("3", "intro", piece, False, 1)], "outro": []},
    )
    [(segment, variant)] = found
    assert (
        variant is None
        and abs(segment.start_seconds - 36.0) <= 1.5
        and abs(segment.end_seconds - 121.0) <= 1.5
    )
