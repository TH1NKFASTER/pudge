"""OP/ED fingerprints of a title survive the deletion of its episode files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from pudge import intro_library, intro_skipper
from pudge.audio_fingerprints import SAMPLE_RATE
from pudge.intro_library import TitleLibrary, episodes_expected, prune_libraries
from pudge.intro_skipper import (
    EpisodeFile,
    IntroSkipperCache,
    analyze_title,
    run_background_pass,
    segments_for_video,
)
from tests.test_intro_skipper import ED, OP, OP2, FakeScheduler, _episode

SR = SAMPLE_RATE
OP_AT = {1: 0.0, 2: 95.0, 3: 30.0, 4: 12.0, 5: 60.0, 6: 5.0, 7: 40.0, 8: 20.0}
ED_AT = {1: 700.0, 2: 740.0, 3: 768.0, 4: 720.0, 5: 705.0, 6: 730.0, 7: 710.0, 8: 715.0}


@pytest.fixture
def media(tmp_path, monkeypatch):
    """Synthetic episodes decoded by a fake FFmpeg (the real fingerprint cache code runs)."""
    tracks: dict[str, np.ndarray] = {}

    def probe(path, _ffprobe, **_k):
        return tracks[Path(path).name].size / SR, None

    def decode(path, *, ffmpeg, start_seconds, duration_seconds, stream_index=None, cancel_check=None, **_k):
        track = tracks[Path(path).name]
        return track[int(start_seconds * SR) : int((start_seconds + duration_seconds) * SR)]

    monkeypatch.setattr(intro_skipper, "probe_audio", probe)
    monkeypatch.setattr(intro_skipper, "decode_region", decode)

    def make(n: int, *, op=OP, folder: str = "Show") -> EpisodeFile:
        directory = tmp_path / folder
        directory.mkdir(exist_ok=True)
        path = directory / f"Show - {n:02d}.mkv"
        path.write_bytes(b"episode %d %d" % (n, id(op)))
        tracks[path.name] = _episode(5000 + n, op_at=OP_AT[n], ed_at=ED_AT[n], op=op)
        return EpisodeFile(path=path, episode=n, identity=intro_skipper.identity_of(path))

    return SimpleNamespace(make=make, cache_dir=tmp_path / "cache", root=tmp_path / "cache" / "intro-skipper")


def _analyse(media, files, library):
    return analyze_title(
        files,
        cache=IntroSkipperCache(media.cache_dir),
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        library=library,
        max_new_fingerprints=10,
    )


def _segments(media, item) -> dict:
    return {s.kind: s for s in segments_for_video(media.cache_dir, item.path)}


def _close(segment, start: float, length: float) -> bool:
    return abs(segment.start_seconds - start) <= 2 and abs(segment.end_seconds - (start + length)) <= 2


def _library_files(media, media_id: int = 77) -> list[str]:
    folder = media.root / "library" / str(media_id)
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def test_lone_new_episode_is_matched_after_the_old_files_are_deleted(media) -> None:
    library = TitleLibrary(media.root, 77)
    first = [media.make(n) for n in (1, 2, 3)]
    _analyse(media, first, library)
    for item in first:
        item.path.unlink()  # watched episodes are cleaned up
    fourth = media.make(4)
    report = _analyse(media, [fourth], TitleLibrary(media.root, 77))
    assert report.found == 1
    found = _segments(media, fourth)
    assert _close(found["intro"], OP_AT[4], 85.0), found
    assert _close(found["outro"], ED_AT[4], 72.0), found
    assert found["intro"].confidence >= 0.8


def test_confirmed_variant_keeps_only_segment_fingerprints(media) -> None:
    library = TitleLibrary(media.root, 77)
    _analyse(media, [media.make(n) for n in (1, 2, 3)], library)
    stored = TitleLibrary(media.root, 77)
    rows = stored.data["variants"].values()
    assert sorted((row["kind"], row["confirmed"], row["episodes"]) for row in rows) == [
        ("intro", True, [1, 2, 3]),
        ("outro", True, [1, 2, 3]),
    ]
    # Per-episode head/tail data is gone; only the two segment fingerprints stay.
    names = _library_files(media)
    assert [n for n in names if n.startswith("e")] == []
    assert len([n for n in names if n.startswith("v-")]) == 2
    assert stored.size_bytes() < 64 * 1024


def test_changed_opening_mid_season_becomes_a_second_variant(media) -> None:
    first = [media.make(n) for n in (1, 2, 3)]
    _analyse(media, first, TitleLibrary(media.root, 77))
    for item in first:
        item.path.unlink()
    # Episode 4: a new OP that nothing shares yet; the ED is still known.
    four = media.make(4, op=OP2)
    _analyse(media, [four], TitleLibrary(media.root, 77))
    found = _segments(media, four)
    assert "intro" not in found and _close(found["outro"], ED_AT[4], 72.0)
    assert "e4-head.npz" in _library_files(media)  # kept for the next episode
    four.path.unlink()
    # Episode 5 alone: compared with the kept head of episode 4.
    five = media.make(5, op=OP2)
    _analyse(media, [five], TitleLibrary(media.root, 77))
    found = _segments(media, five)
    assert _close(found["intro"], OP_AT[5], 88.0), found
    intros = [row for row in TitleLibrary(media.root, 77).data["variants"].values() if row["kind"] == "intro"]
    assert sorted(row["episodes"] for row in intros) == [[1, 2, 3], [4, 5]]
    five.path.unlink()
    six = media.make(6, op=OP2)
    _analyse(media, [six], TitleLibrary(media.root, 77))
    assert _close(_segments(media, six)["intro"], OP_AT[6], 88.0)
    library = TitleLibrary(media.root, 77)
    assert all(row["confirmed"] for row in library.data["variants"].values())
    assert [n for n in _library_files(media) if n.startswith("e")] == []
    # The old OP (e.g. a special episode) is still recognised.
    six.path.unlink()
    seven = media.make(7, op=OP)
    _analyse(media, [seven], TitleLibrary(media.root, 77))
    assert _close(_segments(media, seven)["intro"], OP_AT[7], 85.0)


def test_raw_episode_data_and_variants_are_bounded(media, monkeypatch) -> None:
    from tests.test_intro_skipper import speech

    library = TitleLibrary(media.root, 77)
    settings = intro_skipper.DetectorSettings()
    for n in range(1, 11):  # a title without OP/ED: nothing explains the episodes
        head = intro_skipper.fingerprint_samples(speech(n, 360.0))
        tail = intro_skipper.fingerprint_samples(speech(100 + n, 420.0), start_seconds=420.0)
        library.add_episode(n, f"id{n}", (head, tail, 840.0))
    library.reconcile(settings)
    library.save()
    names = _library_files(media)
    assert len([n for n in names if n.endswith("-head.npz")]) == intro_library.MAX_RAW_EPISODES
    assert "e10-head.npz" in names and "e1-head.npz" not in names  # the newest are kept
    # Over the total cap the raw data goes first; variants stay.
    piece = intro_skipper.fingerprint_samples(OP)
    library._new_variant("intro", piece, 10)
    library.save()
    animes = [SimpleNamespace(media_id=77, status="CURRENT", media_status="RELEASING")]
    prune_libraries(media.root, animes, max_bytes=16 * 1024)
    names = _library_files(media)
    assert [n for n in names if n.startswith("e")] == [] and "v-1.npz" in names


@pytest.mark.parametrize(
    ("status", "media_status", "kept"),
    [
        ("CURRENT", "FINISHED", True),
        ("PLANNING", "NOT_YET_RELEASED", True),
        ("REPEATING", "FINISHED", True),
        ("PAUSED", "RELEASING", True),
        ("COMPLETED", "RELEASING", True),
        ("COMPLETED", "FINISHED", False),
        ("DROPPED", "RELEASING", False),
    ],
)
def test_library_is_deleted_when_no_more_episodes_are_expected(media, status, media_status, kept) -> None:
    library = TitleLibrary(media.root, 77)
    library._new_variant("intro", intro_skipper.fingerprint_samples(OP), 1)
    library.save()
    anime = SimpleNamespace(media_id=77, status=status, media_status=media_status)
    assert episodes_expected(anime) is kept
    prune_libraries(media.root, [anime])
    assert (media.root / "library" / "77").is_dir() is kept


def test_library_of_a_title_removed_from_the_list_is_deleted(media) -> None:
    library = TitleLibrary(media.root, 77)
    library._new_variant("intro", intro_skipper.fingerprint_samples(OP), 1)
    library.save()
    prune_libraries(media.root, [])
    assert not (media.root / "library" / "77").exists()


def _db(files_by_media: dict[int, list[EpisodeFile]], status: str = "CURRENT"):
    animes = [
        SimpleNamespace(media_id=mid, title=f"Show {mid}", status=status, media_status="RELEASING")
        for mid in files_by_media
    ]

    def episodes(media_id=None):
        return [
            SimpleNamespace(media_id=mid, episode=f.episode, video_path=f.path, state="ready")
            for mid, items in files_by_media.items()
            if media_id in (None, mid)
            for f in items
            if f.path.exists()
        ]

    return SimpleNamespace(anime_list=lambda *a: animes, episodes=episodes)


def _pass(media, db) -> dict:
    return run_background_pass(
        db=db,
        cache_dir=media.cache_dir,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        scheduler=FakeScheduler(),
        max_new_fingerprints=10,
    )


def test_background_pass_analyses_a_lone_episode_and_migrates_old_caches(media, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    monkeypatch.setattr(intro_skipper, "_register_and_enforce", lambda *a: None)
    old = [media.make(n) for n in (1, 2)]
    # An older Pudge analysed episodes 1–2 (per-file cache only, no library).
    analyze_title(old, cache=IntroSkipperCache(media.cache_dir), ffmpeg="f", ffprobe="p")
    assert not (media.root / "library").exists()
    for item in old:
        item.path.unlink()
    three = media.make(3)
    result = _pass(media, _db({77: [three]}))
    assert result["status"] == "ran" and result["found"] == 1, result
    found = _segments(media, three)
    assert _close(found["intro"], OP_AT[3], 85.0) and _close(found["outro"], ED_AT[3], 72.0)
    library = TitleLibrary(media.root, 77)
    assert library.migrated
    assert all(row["confirmed"] for row in library.data["variants"].values())
    assert _pass(media, _db({77: [three]}))["status"] == "idle"
    # Dropped: the next pass deletes the title's library.
    _pass(media, _db({77: [three]}, status="DROPPED"))
    assert not (media.root / "library" / "77").exists()
