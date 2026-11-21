"""A weekly show watched one episode at a time gets an intro skip from its second episode.

User: "watching Reincarnated as a Sword and the opening skip didn't show up, although we
started remembering the audio of earlier episodes". The title library keeps OP/ED
fingerprints of deleted episodes, but a lone episode on disk with nothing stored yet was
never even fingerprinted ("one episode alone proves nothing"). When every episode is
deleted after watching and the next arrives a week later, there are never two files at
once, so nothing was ever stored and no episode got a skip. A lone episode is now
fingerprinted into the library (once), and the next lone episode is matched against it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")

from pudge import intro_skipper
from pudge.intro_library import TitleLibrary
from pudge.intro_skipper import DetectorSettings, IntroSkipperCache
from tests.test_fix_intro_library import ED_AT, OP_AT, _analyse, _close, _segments, media  # noqa: F401


def _needs(media, files, library) -> bool:
    return intro_skipper.title_needs_analysis(
        files, IntroSkipperCache(media.cache_dir), DetectorSettings(), 0.0, library
    )


def test_one_episode_at_a_time_gets_the_skip_from_the_second(media) -> None:  # noqa: F811
    first = media.make(1)
    library = TitleLibrary(media.root, 77)
    assert _needs(media, [first], library)  # stored for the next episode
    report = _analyse(media, [first], library)
    assert report.fingerprinted == 1 and report.analysed == 0
    assert not _needs(media, [first], TitleLibrary(media.root, 77))  # done once, no loop
    first.path.unlink()  # watched and deleted before episode 2 arrives

    second = media.make(2)
    assert _needs(media, [second], TitleLibrary(media.root, 77))
    _analyse(media, [second], TitleLibrary(media.root, 77))
    found = _segments(media, second)
    assert _close(found["intro"], OP_AT[2], 85.0), found
    assert _close(found["outro"], ED_AT[2], 72.0), found
    second.path.unlink()

    third = media.make(3)
    _analyse(media, [third], TitleLibrary(media.root, 77))
    found = _segments(media, third)
    assert _close(found["intro"], OP_AT[3], 85.0), found


def test_lone_episode_without_a_library_is_still_left_alone(media) -> None:  # noqa: F811
    # A finished, dropped title keeps no library: one file still proves nothing.
    assert not _needs(media, [media.make(1)], None)
