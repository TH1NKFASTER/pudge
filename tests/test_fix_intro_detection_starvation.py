"""A title whose OP/ED analysis does nothing no longer starves every title after it.

User: Cyberpunk: Edgerunners 2 (10 local episodes) never offered the OP/ED skip. For days every
background pass logged ``media_id=2251 files=16 fingerprinted=0 analysed=0`` (Baccano!): its
three "S00E01 - Episode 14 - OVA 1" files were filed as episodes 1-3 next to S01E01-E03, the
title library keeps one file per episode, ``title_needs_analysis`` wanted the OVA copies
observed there, ``analyze_title`` could never do it, and the pass always stopped at Baccano.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from pudge import intro_skipper
from pudge.audio_fingerprints import SAMPLE_RATE
from pudge.intro_library import TitleLibrary
from pudge.intro_skipper import (
    DetectorSettings,
    EpisodeFile,
    IntroSkipperCache,
    analyze_title,
    run_background_pass,
    segments_for_video,
    title_needs_analysis,
)
from tests.test_intro_skipper import OP, OP2, FakeScheduler, _episode

BACCANO, EDGERUNNERS = 2251, 195539
OVA_OP = OP2


@pytest.fixture
def media(tmp_path, monkeypatch):
    """Synthetic files decoded by a fake FFmpeg (the real fingerprint cache code runs)."""
    tracks: dict[str, np.ndarray] = {}

    def probe(path, _ffprobe, **_k):
        return tracks[Path(path).name].size / SAMPLE_RATE, None

    def decode(path, *, ffmpeg, start_seconds, duration_seconds, stream_index=None, cancel_check=None, **_k):
        track = tracks[Path(path).name]
        return track[int(start_seconds * SAMPLE_RATE) : int((start_seconds + duration_seconds) * SAMPLE_RATE)]

    monkeypatch.setattr(intro_skipper, "probe_audio", probe)
    monkeypatch.setattr(intro_skipper, "decode_region", decode)

    def make(folder: str, name: str, episode: int, *, seed: int, op_at: float, ed_at: float, op=OP):
        directory = tmp_path / folder
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(name.encode())
        tracks[name] = _episode(seed, op_at=op_at, ed_at=ed_at, op=op)
        return EpisodeFile(path=path, episode=episode, identity=intro_skipper.identity_of(path))

    return SimpleNamespace(make=make, cache_dir=tmp_path / "cache", root=tmp_path / "cache" / "intro-skipper")


def _baccano(media) -> list[EpisodeFile]:
    folder = "[DeeVeeDee] Baccano! [DVD 480p HEVC Opus Dual-Audio]"
    tv = [
        media.make(
            folder,
            f"[DeeVeeDee] Baccano - S01E{n:02d} [DVD].mkv",
            n,
            seed=100 + n,
            op_at=10.0 * n,
            ed_at=700.0,
        )
        for n in (1, 2, 3)
    ]
    # The OVA as the old scan filed it: "episode 1", other audio.
    ova = media.make(
        folder,
        "[DeeVeeDee] Baccano - S00E01 - Episode 14 - OVA 1 [DVD].mkv",
        1,
        seed=190,
        op_at=40.0,
        ed_at=710.0,
        op=OVA_OP,
    )
    return [*tv, ova]


def _edgerunners(media) -> list[EpisodeFile]:
    return [
        media.make(
            "Cyberpunk Edgerunners 2",
            f"[Erai-raws] Cyberpunk Edgerunners 2 - {n:02d} [1080p].mkv",
            n,
            seed=300 + n,
            op_at=20.0 + 15.0 * n,
            ed_at=720.0,
        )
        for n in (1, 2)
    ]


def _analyse(media, files, media_id):
    return analyze_title(
        files,
        cache=IntroSkipperCache(media.cache_dir),
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        library=TitleLibrary(media.root, media_id),
        max_new_fingerprints=10,
    )


def _settle(media, files, media_id) -> None:
    """Analyse until a pass does nothing (the OVA, analysed before the title's first
    variants existed, is compared with them once more)."""
    for _ in range(4):
        report = _analyse(media, files, media_id)
        if not (report.fingerprinted or report.analysed or report.errors):
            return
    raise AssertionError("the title never settles")


def _db(files_by_media: dict[int, list[EpisodeFile]]):
    titles = {BACCANO: "Baccano!", EDGERUNNERS: "Cyberpunk: Edgerunners 2"}
    animes = [
        SimpleNamespace(
            media_id=mid, title=titles.get(mid, f"Show {mid}"), status="PLANNING", media_status="FINISHED"
        )
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


def _pass(media, db, scheduler=None, logger=None) -> dict:
    return run_background_pass(
        db=db,
        cache_dir=media.cache_dir,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        scheduler=scheduler or FakeScheduler(),
        max_new_fingerprints=10,
        logger=logger,
    )


@pytest.fixture
def background(monkeypatch):
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    monkeypatch.setattr(intro_skipper, "_register_and_enforce", lambda *a: None)
    intro_skipper._deferral_logged.clear()


def test_two_files_of_one_episode_number_do_not_keep_the_title_needing_analysis(media) -> None:
    files = _baccano(media)
    assert _analyse(media, files, BACCANO).analysed == 4
    _settle(media, files, BACCANO)
    # Both functions agree on which copy of "episode 1" the title library keeps: once a pass
    # does nothing, the title no longer "needs analysis" (it did forever: the OVA copy).
    assert not title_needs_analysis(
        files,
        IntroSkipperCache(media.cache_dir),
        DetectorSettings(),
        time.time(),
        TitleLibrary(media.root, BACCANO),
    )
    library = TitleLibrary(media.root, BACCANO).data
    tv1 = next(f for f in files if "S01E01" in f.path.name)
    assert library["episodes"]["1"]["identity"] == tv1.identity and library["episodes"]["1"]["observed"]
    # Every file, the OVA too, still has its own segments (the OVA shares only the ED).
    for item in files:
        kinds = {s.kind for s in segments_for_video(media.cache_dir, item.path)}
        assert kinds == ({"outro"} if "OVA" in item.path.name else {"intro", "outro"}), item.path.name


def test_baccano_then_edgerunners_get_analysed_in_the_following_passes(media, background) -> None:
    baccano, edgerunners = _baccano(media), _edgerunners(media)
    db = _db({BACCANO: baccano, EDGERUNNERS: edgerunners})
    first = _pass(media, db)
    assert first["media_id"] == BACCANO and first["analysed"] == 4
    picked = [_pass(media, db) for _ in range(3)]
    assert [r.get("media_id") for r in picked] == [BACCANO, EDGERUNNERS, None], picked
    assert picked[1]["found"] == 2 and picked[2]["status"] == "idle"
    assert segments_for_video(media.cache_dir, edgerunners[0].path)


def test_title_whose_pass_does_nothing_does_not_starve_the_next_one(
    media, background, monkeypatch, caplog
) -> None:
    baccano, edgerunners = _baccano(media), _edgerunners(media)
    _settle(media, baccano, BACCANO)  # everything of Baccano is known
    stuck_paths = {item.path for item in baccano}
    real = intro_skipper.title_needs_analysis
    analysed: list[int] = []
    real_analyze = intro_skipper.analyze_title

    def always_for_baccano(files, *args, **kwargs):
        return bool({f.path for f in files} & stuck_paths) or real(files, *args, **kwargs)

    def counting(files, **kwargs):
        analysed.append(BACCANO if {f.path for f in files} & stuck_paths else EDGERUNNERS)
        return real_analyze(files, **kwargs)

    # Any future disagreement between the two functions: "needs analysis", nothing to do.
    monkeypatch.setattr(intro_skipper, "title_needs_analysis", always_for_baccano)
    monkeypatch.setattr(intro_skipper, "analyze_title", counting)
    db = _db({BACCANO: baccano, EDGERUNNERS: edgerunners})
    with caplog.at_level(logging.INFO):
        first = _pass(media, db, logger=logging.getLogger("pudge.test"))
    # The same pass goes on to Cyberpunk.
    assert first["media_id"] == EDGERUNNERS and first["found"] == 2, first
    assert first["no_progress"] == [BACCANO]
    assert analysed == [BACCANO, EDGERUNNERS]
    assert "reason=no_progress" in caplog.text
    # Baccano is left alone while nothing about it changes.
    assert _pass(media, db)["status"] == "idle"
    assert analysed == [BACCANO, EDGERUNNERS]
    # A new file changes it: looked at again (and fingerprinted).
    baccano.append(
        media.make(
            baccano[0].path.parent.name,
            "[DeeVeeDee] Baccano - S01E04 [DVD].mkv",
            4,
            seed=104,
            op_at=5.0,
            ed_at=700.0,
        )
    )
    third = _pass(media, db)
    assert third["media_id"] == BACCANO and third["fingerprinted"] == 1
    assert analysed[-1] == BACCANO


def test_title_that_did_nothing_is_looked_at_again_after_its_recheck_time(
    media, background, monkeypatch
) -> None:
    baccano = _baccano(media)
    _settle(media, baccano, BACCANO)
    monkeypatch.setattr(intro_skipper, "title_needs_analysis", lambda *_a, **_k: True)
    db = _db({BACCANO: baccano})
    assert _pass(media, db)["no_progress"] == [BACCANO]
    assert _pass(media, db)["status"] == "idle"
    memo_path = media.root / intro_skipper.NO_PROGRESS_FILE
    memo = json.loads(memo_path.read_text())
    memo[str(BACCANO)]["until"] = 1.0  # a day later (or an empty result's retry is due)
    memo_path.write_text(json.dumps(memo))
    assert _pass(media, db)["no_progress"] == [BACCANO]


def test_deferred_pass_says_why_at_most_once_an_hour(media, background, caplog) -> None:
    db = _db({EDGERUNNERS: _edgerunners(media)})
    playing = FakeScheduler(wait_reason="foreground")
    log = logging.getLogger("pudge.test")
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert _pass(media, db, playing, log) == {"status": "deferred", "reason": "foreground"}
    assert caplog.text.count("reason=deferred wait=foreground") == 1
    intro_skipper._deferral_logged["foreground"] -= intro_skipper.DEFERRAL_LOG_SECONDS + 1
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _pass(media, db, playing, log)
    assert caplog.text.count("reason=deferred wait=foreground") == 1
    assert playing.acquired == []
