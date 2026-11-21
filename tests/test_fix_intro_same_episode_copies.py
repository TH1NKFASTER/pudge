# ruff: noqa: F811 - the "media" fixture is imported
"""Two copies of one episode are not partners for finding its opening.

User (Kusuriya no Hitorigoto S3 #3): "it offered the opening much later". An upgrade had left
AnoZu's and Erai-raws' copies of episode 3 side by side, and they were each other's only
partners: they share every second, so "the opening" was an arbitrary 114 s at 4:05-6:00 (and
two library variants were made of it).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from pudge import intro_skipper
from pudge.intro_library import TitleLibrary
from pudge.intro_skipper import EpisodeFile, IntroSkipperCache, analyze_title, segments_for_video
from tests.test_fix_intro_library import media  # noqa: F401 - fixture


def _two_copies(media, tmp_path):
    first = media.make(3)
    track_dir = first.path.parent
    second_path = track_dir / "Show - 03 (other group).mkv"
    second_path.write_bytes(b"another release of episode 3")
    # The same audio: one episode, two releases.
    import pudge.intro_skipper as module

    probe, decode = module.probe_audio, module.decode_region

    def probe2(path, ffprobe, **k):
        return probe(first.path if Path(path) == second_path else path, ffprobe, **k)

    def decode2(path, **k):
        return decode(first.path if Path(path) == second_path else path, **k)

    return (
        first,
        EpisodeFile(path=second_path, episode=3, identity=intro_skipper.identity_of(second_path)),
        probe2,
        decode2,
    )


def _analyse(media, files, library):
    return analyze_title(
        files,
        cache=IntroSkipperCache(media.cache_dir),
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        library=library,
        max_new_fingerprints=10,
    )


def test_two_copies_of_one_episode_find_no_opening(media, tmp_path, monkeypatch) -> None:
    first, second, probe2, decode2 = _two_copies(media, tmp_path)
    monkeypatch.setattr(intro_skipper, "probe_audio", probe2)
    monkeypatch.setattr(intro_skipper, "decode_region", decode2)
    report = _analyse(media, [first, second], TitleLibrary(media.root, 77))
    assert report.analysed == 0
    assert segments_for_video(media.cache_dir, first.path) == []
    assert segments_for_video(media.cache_dir, second.path) == []


def test_what_two_copies_found_before_is_forgotten(media, tmp_path, monkeypatch, caplog) -> None:
    first, second, probe2, decode2 = _two_copies(media, tmp_path)
    monkeypatch.setattr(intro_skipper, "probe_audio", probe2)
    monkeypatch.setattr(intro_skipper, "decode_region", decode2)
    cache = IntroSkipperCache(media.cache_dir)
    _analyse(media, [first, second], TitleLibrary(media.root, 77))  # fingerprints, nothing found
    # What the old partner choice wrote: each copy the other's partner, a 114 s "opening",
    # and two library variants made of it at the same moment.
    checked = 1_792_164_731.0
    for item, twin in ((first, second), (second, first)):
        cache.write_result(
            item.identity,
            {
                "algorithm": intro_skipper.ALGORITHM_VERSION,
                "status": "found",
                "segments": [
                    {
                        "kind": "intro",
                        "start_seconds": 245.72,
                        "end_seconds": 359.77,
                        "confidence": 0.6,
                        "source": "local-fingerprint",
                        "media_fingerprint": item.identity,
                    },
                ],
                "partners": sorted([twin.identity, "variant:1"]),
                "checked_at": checked,
                "retry_after": 0,
                "path": str(item.path),
                "episode": 3,
                "settings": {},
            },
        )
    library = TitleLibrary(media.root, 77)
    fps = intro_skipper._fingerprints_for(
        first, cache, ffmpeg="f", ffprobe="p", allow_compute=True, cancel_check=lambda: False
    )
    from pudge.intro_library import slice_fingerprint

    library._new_variant("intro", slice_fingerprint(fps[0], 245.72, 359.77), 3)
    library.data["variants"]["1"]["created_at"] = checked + 5.0
    library.save()
    assert segments_for_video(media.cache_dir, first.path)  # the false "opening"
    # The background pass looks at the title again although nothing else changed.
    assert intro_skipper.title_needs_analysis(
        [first], cache, intro_skipper.DetectorSettings(), checked + 10, TitleLibrary(media.root, 77)
    )

    with caplog.at_level(logging.INFO):
        _analyse(media, [first], TitleLibrary(media.root, 77))  # the other copy was deleted since
    assert segments_for_video(media.cache_dir, first.path) == []
    assert TitleLibrary(media.root, 77).data["variants"] == {}
    assert "same_episode_pair" in caplog.text

    # Real partners still find the real opening.
    fourth = media.make(4)
    _analyse(media, [first, fourth], TitleLibrary(media.root, 77))
    [intro] = [s for s in segments_for_video(media.cache_dir, fourth.path) if s.kind == "intro"]
    assert abs(intro.start_seconds - 12.0) <= 2
