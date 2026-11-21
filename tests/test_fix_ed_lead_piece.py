"""An ending (or opening) whose first seconds are broken by an episode's own line starts on time.

User (Sora yori mo Tooi Basho): "the ending starts being detected a bit late (~5 s)". E01's ED
matched the other episodes for its first ~3 s, then ~6 s of dialogue over the ED's intro
differed, then the rest matched. The 3-s lead piece was too short to be joined, so the ED was
offered from ~9 s late. Invented audio only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from pudge import intro_skipper
from pudge.audio_fingerprints import MatchRun, _extend_run, best_shared_run, fingerprint_samples
from pudge.intro_library import TitleLibrary, VariantRef, empty_fingerprint, slice_fingerprint
from tests.test_intro_skipper import music, place, speech

ED = music(42, 85.0)
TAIL_START = 1100.0


def _tail(seed: int, ed_at: float, *, line: tuple[float, float] | None = None, extra=None) -> np.ndarray:
    track = speech(seed, 300.0)
    place(track, ED, ed_at)
    if line is not None:  # this episode's own line over the ED's intro (others do not have it)
        place(track, speech(seed + 500, line[1] - line[0]), ed_at + line[0], gain=1.5, mix=True)
    if extra is not None:
        clip, at = extra
        place(track, clip, at)
    return track


def _fp(track: np.ndarray):
    return fingerprint_samples(track, start_seconds=TAIL_START)


def test_sora_like_ending_extends_to_its_short_lead_piece() -> None:
    first = _fp(_tail(1, 86.0, line=(3.0, 9.0)))  # ED really starts at 1186.0
    for seed, at in ((2, 120.0), (3, 105.0), (4, 110.0)):
        run = best_shared_run(first, _fp(_tail(seed, at)), min_seconds=30, max_seconds=180)
        assert run is not None
        # Before the fix: start ~1195.2 (the lead piece 1186-1189 was dropped).
        assert abs(run.a_start - 1186.0) <= 1.0, run
        assert abs(run.a_end - 1271.0) <= 1.0, run


def test_a_short_coincidence_far_from_the_stretch_does_not_extend_it() -> None:
    jingle = music(77, 3.0)
    # Same 3-s jingle at the same alignment, 20 s before the ED: too far to be the ED's lead.
    a = _fp(_tail(5, 86.0, extra=(jingle, 63.0)))
    b = _fp(_tail(6, 120.0, extra=(jingle, 97.0)))
    run = best_shared_run(a, b, min_seconds=30, max_seconds=180)
    assert run is not None and abs(run.a_start - 1186.0) <= 1.0, run


def _run(start: float, end: float, ber: float = 0.1, voiced: float = 1.0) -> MatchRun:
    return MatchRun(start, end, start + 30.0, end + 30.0, ber, voiced)


def test_extension_rules() -> None:
    main = _run(100.0, 170.0)
    # Lead piece 4 s before, then a trailing piece 5 s after, then a chained one 6 s further.
    grown = _extend_run(main, [_run(92.0, 96.0), _run(175.0, 178.0), _run(184.0, 186.5)], max_seconds=180)
    assert (grown.a_start, grown.a_end, grown.b_start, grown.b_end) == (92.0, 186.5, 122.0, 216.5)
    # Too far, too short, too noisy, mostly silent: none extends.
    rejected = [
        _run(80.0, 85.0),  # 15 s gap
        _run(97.0, 98.5),  # 1.5 s piece
        _run(94.0, 97.0, ber=0.3),  # above the strict threshold
        _run(94.0, 97.0, voiced=0.5),  # mostly silence
    ]
    assert _extend_run(main, rejected, max_seconds=180) == main
    # Never beyond max_seconds.
    assert _extend_run(main, [_run(92.0, 96.0)], max_seconds=75.0) == main


def _ef(n: int) -> intro_skipper.EpisodeFile:
    return intro_skipper.EpisodeFile(path=Path(f"/x/{n}.mkv"), episode=n, identity=f"id{n}")


def test_confirmed_late_variant_grows_from_partners_and_is_recut(tmp_path) -> None:
    head = empty_fingerprint(0.0)
    first = _fp(_tail(1, 86.0, line=(3.0, 9.0)))
    second = _fp(_tail(2, 120.0))
    third = _fp(_tail(3, 105.0))
    settings = intro_skipper.DetectorSettings()
    library = TitleLibrary(tmp_path, 9)
    # What an older detector stored: the ED cut from E02 from ~9 s late, confirmed by E02/E03.
    late = slice_fingerprint(second, 1229.2, 1305.0)
    vid = library._new_variant("outro", late, 2)
    library._credit(vid, 3)
    assert library.data["variants"][vid]["confirmed"]
    [ref] = library.variants("outro")
    found = intro_skipper._detect(
        _ef(1),
        (head, first, 1400.0),
        [(_ef(3), (head, third, 1400.0))],
        settings,
        {"intro": [], "outro": [ref]},
    )
    [(segment, variant)] = found
    assert variant == vid and abs(segment.start_seconds - 1186.0) <= 1.0, segment
    library.observe(
        1,
        "id1",
        (head, first, 1400.0),
        [(s.kind, s.start_seconds, s.end_seconds, v) for s, v in found],
        settings,
    )
    library.save()
    regrown = TitleLibrary(tmp_path, 9)
    assert regrown.data["variants"][vid]["duration"] >= 84.0
    # A later lone episode (no partners) now gets the full ED from the library alone.
    lone = _fp(_tail(8, 130.0))
    [(alone, used)] = intro_skipper._detect(
        _ef(8), (head, lone, 1400.0), [], settings, {"intro": [], "outro": regrown.variants("outro")}
    )
    assert used == vid and abs(alone.start_seconds - 1230.0) <= 1.0, alone


def test_confirmed_variant_without_partners_is_kept_as_is() -> None:
    head = empty_fingerprint(0.0)
    second = _fp(_tail(2, 120.0))
    late = slice_fingerprint(second, 1229.2, 1305.0)
    lone = _fp(_tail(8, 130.0))
    [(segment, variant)] = intro_skipper._detect(
        _ef(8),
        (head, lone, 1400.0),
        [],
        intro_skipper.DetectorSettings(),
        {"intro": [], "outro": [VariantRef("1", "outro", late, True, 2)]},
    )
    assert variant == "1" and abs(segment.start_seconds - 1239.2) <= 1.0, segment


def test_results_of_the_old_detector_are_recomputed_from_cached_fingerprints(tmp_path) -> None:
    assert intro_skipper.ALGORITHM_VERSION.endswith("+intro-3")
    old = {"algorithm": intro_skipper.ALGORITHM_VERSION.replace("intro-3", "intro-2"), "status": "found"}
    assert intro_skipper._needs_analysis(old, set(), 0.0)
    cache = intro_skipper.IntroSkipperCache(tmp_path)
    before = cache.fingerprint_path("id1", None, "tail")
    intro_skipper.DETECTOR_VERSION, saved = "intro-x", intro_skipper.DETECTOR_VERSION
    try:
        assert cache.fingerprint_path("id1", None, "tail") == before  # keyed by ENGINE_VERSION only
    finally:
        intro_skipper.DETECTOR_VERSION = saved
