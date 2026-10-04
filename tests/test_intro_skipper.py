"""Task 6: local OP/ED detection from sequential audio fingerprints."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from pudge import intro_skipper
from pudge.audio_fingerprints import (
    ENGINE_VERSION,
    SAMPLE_RATE,
    FingerprintError,
    best_shared_run,
    fingerprint_samples,
    load_fingerprint,
    save_fingerprint,
)
from pudge.intro_skipper import (
    ALGORITHM_VERSION,
    DetectorSettings,
    EpisodeFile,
    IntroSkipperCache,
    analyze_title,
    detect_segments,
    run_background_pass,
    segments_for_video,
)

SR = SAMPLE_RATE


# ------------------------------------------------------------ synthesis ---

def music(seed: int, seconds: float, sr: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = np.zeros(int(seconds * sr), np.float32)
    t = 0
    while t < out.size:
        dur = int(rng.uniform(0.2, 0.6) * sr)
        f0 = 110 * 2 ** (rng.integers(0, 36) / 12)
        n = np.arange(min(dur, out.size - t))
        env = np.minimum(1, n / 200) * np.exp(-n / sr * 2)
        tone = sum(np.sin(2 * np.pi * f0 * h * n / sr) / h for h in (1, 2, 3, 4))
        out[t:t + n.size] += (0.3 * env * tone).astype(np.float32)
        t += dur
    return out + 0.01 * rng.standard_normal(out.size).astype(np.float32)


def speech(seed: int, seconds: float, sr: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(int(seconds * sr)).astype(np.float32)
    smooth = np.cumsum(noise)
    smooth = (smooth[32:] - smooth[:-32]) / 32.0
    smooth = np.concatenate([smooth, np.zeros(32, np.float32)])
    rate = rng.uniform(3, 5)
    env = (np.sin(2 * np.pi * rate * np.arange(noise.size) / sr) > 0).astype(np.float32)
    return (smooth * env * 0.6).astype(np.float32)


def place(track: np.ndarray, clip: np.ndarray, at: float, gain: float = 1.0, *, mix: bool = False) -> None:
    start = int(at * SR)
    end = min(track.size, start + clip.size)
    if mix:
        track[start:end] += clip[: end - start] * gain
    else:
        track[start:end] = clip[: end - start] * gain


OP = music(1, 85.0)
OP2 = music(9, 88.0)
ED = music(2, 72.0)


# ------------------------------------------------------ fingerprint unit ---

def test_fingerprint_finds_shifted_shared_music() -> None:
    a = speech(11, 360.0)
    b = speech(12, 360.0)
    place(a, OP, 30.0)
    place(b, OP, 95.0, gain=0.7)  # other time, other volume
    run = best_shared_run(fingerprint_samples(a), fingerprint_samples(b), min_seconds=30, max_seconds=180)
    assert run is not None
    assert abs(run.a_start - 30.0) < 1.0 and abs(run.a_end - 115.0) < 1.0
    assert abs(run.b_start - 95.0) < 1.0 and abs(run.b_end - 180.0) < 1.0


def test_unrelated_audio_silence_and_short_logo_do_not_match() -> None:
    a = speech(21, 360.0)
    b = speech(22, 360.0)
    logo = music(5, 6.0)
    place(a, logo, 0.0)
    place(b, logo, 3.0)
    a[int(100 * SR):int(160 * SR)] = 0.0  # a minute of silence in both
    b[int(200 * SR):int(260 * SR)] = 0.0
    fa, fb = fingerprint_samples(a), fingerprint_samples(b)
    assert best_shared_run(fa, fb, min_seconds=30, max_seconds=180) is None
    # The logo is found as a short match, but it is below the OP length.
    assert best_shared_run(fa, fb, min_seconds=3, max_seconds=180) is not None


def test_fingerprint_roundtrip(tmp_path: Path) -> None:
    fp = fingerprint_samples(speech(1, 20.0), start_seconds=400.0)
    save_fingerprint(tmp_path / "x.npz", fp, meta={"identity": "i"})
    loaded, meta = load_fingerprint(tmp_path / "x.npz")
    assert meta["engine"] == ENGINE_VERSION and meta["identity"] == "i"
    assert np.array_equal(loaded.codes, fp.codes) and loaded.start_seconds == 400.0


# ---------------------------------------------------- detector (no ffmpeg) ---

def _episode(seed: int, *, op_at: float | None, ed_at: float | None, op=OP, total: float = 840.0,
             op_dialogue: bool = False) -> np.ndarray:
    track = speech(seed, total)
    if op_at is not None:
        place(track, op, op_at)
        if op_dialogue:  # a character talks over the first 20 s of the OP
            place(track, speech(seed + 500, 20.0), op_at, gain=0.35, mix=True)
    if ed_at is not None:
        place(track, ED, ed_at)
    return track


def _fps(track: np.ndarray) -> tuple:
    duration = track.size / SR
    tail_start = duration - intro_skipper.TAIL_SECONDS
    head = fingerprint_samples(track[: int(intro_skipper.HEAD_SECONDS * SR)], start_seconds=0.0)
    tail = fingerprint_samples(track[int(tail_start * SR):], start_seconds=tail_start)
    return head, tail, duration


def _ef(n: int) -> EpisodeFile:
    return EpisodeFile(path=Path(f"/x/ep{n}.mkv"), episode=n, identity=f"id{n}")


def test_detector_per_file_boundaries_and_confidence() -> None:
    eps = {
        1: _fps(_episode(101, op_at=0.0, ed_at=700.0)),  # post-credit scene 772–840
        2: _fps(_episode(102, op_at=95.0, ed_at=740.0)),  # cold open
        3: _fps(_episode(103, op_at=30.0, ed_at=768.0, op_dialogue=True)),  # ED runs to EOF
    }
    settings = DetectorSettings()
    seg1 = detect_segments(_ef(1), eps[1], [(_ef(2), eps[2]), (_ef(3), eps[3])], settings)
    by_kind = {s.kind: s for s in seg1}
    assert abs(by_kind["intro"].start_seconds - 0.0) <= 2 and abs(by_kind["intro"].end_seconds - 85.0) <= 2
    assert abs(by_kind["outro"].start_seconds - 700.0) <= 2 and abs(by_kind["outro"].end_seconds - 772.0) <= 2
    assert by_kind["intro"].confidence >= 0.8  # confirmed by two partners
    seg2 = {s.kind: s for s in detect_segments(_ef(2), eps[2], [(_ef(1), eps[1])], settings)}
    assert abs(seg2["intro"].start_seconds - 95.0) <= 2 and abs(seg2["intro"].end_seconds - 180.0) <= 2
    assert seg2["intro"].confidence == 0.6  # only one partner
    seg3 = {s.kind: s for s in detect_segments(_ef(3), eps[3], [(_ef(1), eps[1]), (_ef(2), eps[2])], settings)}
    assert abs(seg3["intro"].start_seconds - 30.0) <= 2 and abs(seg3["intro"].end_seconds - 115.0) <= 2
    assert abs(seg3["outro"].end_seconds - 840.0) <= 2
    assert all(s.media_fingerprint == "id3" for s in seg3.values())


def test_changed_op_and_recap_give_no_false_intro() -> None:
    new_op = _fps(_episode(201, op_at=10.0, ed_at=700.0, op=OP2))
    old_a = _fps(_episode(202, op_at=10.0, ed_at=700.0))
    old_b = _fps(_episode(203, op_at=40.0, ed_at=710.0))
    segments = {s.kind: s for s in detect_segments(
        _ef(9), new_op, [(_ef(1), old_a), (_ef(2), old_b)], DetectorSettings())}
    assert "intro" not in segments  # nobody shares the new OP
    assert "outro" in segments  # the ED is still the same


def test_negative_titles_have_no_segments() -> None:
    a = _fps(_episode(301, op_at=None, ed_at=None))
    b = _fps(_episode(302, op_at=None, ed_at=None))
    assert detect_segments(_ef(1), a, [(_ef(2), b)], DetectorSettings()) == []


# ---------------------------------------------- analyze_title with a cache ---

def _patch_fingerprints(monkeypatch, tracks: dict[str, np.ndarray], calls: list[str], *, fail: set[str] = frozenset()):
    def fake(item, cache, *, ffmpeg, ffprobe, allow_compute, cancel_check):
        if not allow_compute:
            return None
        calls.append(item.identity)
        if item.identity in fail:
            raise FingerprintError("ffprobe duration failed: broken file")
        head, tail, duration = _fps(tracks[item.identity])
        return head, tail, duration, True

    monkeypatch.setattr(intro_skipper, "_fingerprints_for", fake)


def _files(tmp_path: Path, count: int) -> list[EpisodeFile]:
    files = []
    for n in range(1, count + 1):
        path = tmp_path / f"Show - {n:02d}.mkv"
        path.write_bytes(b"video %d" % n)
        files.append(EpisodeFile(path=path, episode=n, identity=intro_skipper.identity_of(path)))
    return files


def test_analyze_title_writes_results_and_playback_reads_them(tmp_path, monkeypatch) -> None:
    files = _files(tmp_path, 3)
    tracks = {f.identity: _episode(400 + i, op_at=5.0 * i, ed_at=700.0) for i, f in enumerate(files)}
    calls: list[str] = []
    _patch_fingerprints(monkeypatch, tracks, calls)
    cache = IntroSkipperCache(tmp_path / "cache")
    report = analyze_title(files, cache=cache, ffmpeg="ffmpeg", ffprobe="ffprobe")
    assert report.analysed == 3 and report.found == 3 and report.fingerprinted == 3
    segments = segments_for_video(tmp_path / "cache", files[1].path)
    assert [s.kind for s in segments] == ["intro", "outro"]
    assert abs(segments[0].start_seconds - 5.0) <= 2
    # Nothing new: a second pass does not re-analyse.
    assert analyze_title(files, cache=cache, ffmpeg="ffmpeg", ffprobe="ffprobe").analysed == 0


def test_replaced_file_invalidates_segments(tmp_path, monkeypatch) -> None:
    files = _files(tmp_path, 2)
    tracks = {f.identity: _episode(500 + i, op_at=20.0, ed_at=700.0) for i, f in enumerate(files)}
    _patch_fingerprints(monkeypatch, tracks, [])
    analyze_title(files, cache=IntroSkipperCache(tmp_path / "cache"), ffmpeg="f", ffprobe="p")
    assert segments_for_video(tmp_path / "cache", files[0].path)
    files[0].path.write_bytes(b"a different, longer release of episode one")
    assert segments_for_video(tmp_path / "cache", files[0].path) == []


def test_single_episode_is_not_analysed_and_empty_vs_error_states(tmp_path, monkeypatch) -> None:
    files = _files(tmp_path, 3)
    tracks = {f.identity: _episode(600 + i, op_at=None, ed_at=None) for i, f in enumerate(files)}
    _patch_fingerprints(monkeypatch, tracks, [], fail={files[2].identity})
    cache = IntroSkipperCache(tmp_path / "cache")
    only_one = analyze_title(files[:1], cache=cache, ffmpeg="f", ffprobe="p")
    assert only_one.analysed == 0 and cache.read_result(files[0].identity) is None
    report = analyze_title(files, cache=cache, ffmpeg="f", ffprobe="p", now=lambda: 1000.0)
    empty = cache.read_result(files[0].identity)
    error = cache.read_result(files[2].identity)
    assert report.empty == 2 and report.errors == 1
    assert empty["status"] == "empty" and empty["retry_after"] == 1000.0 + intro_skipper.EMPTY_RETRY_SECONDS
    assert error["status"] == "error" and error["retry_after"] == 1000.0 + intro_skipper.ERROR_RETRY_SECONDS
    assert segments_for_video(tmp_path / "cache", files[0].path) == []


def test_new_partner_triggers_recheck_of_empty_result(tmp_path, monkeypatch) -> None:
    files = _files(tmp_path, 3)
    cache = IntroSkipperCache(tmp_path / "cache")
    settings = DetectorSettings()
    cache.write_result(files[0].identity, {
        "algorithm": ALGORITHM_VERSION, "status": "empty", "segments": [],
        "partners": [files[1].identity], "retry_after": 10**12,
    })
    cache.write_result(files[1].identity, {
        "algorithm": ALGORITHM_VERSION, "status": "empty", "segments": [],
        "partners": [files[0].identity], "retry_after": 10**12,
    })
    assert not intro_skipper.title_needs_analysis(files[:2], cache, settings, 0.0)
    assert intro_skipper.title_needs_analysis(files, cache, settings, 0.0)


def test_stop_between_files(tmp_path, monkeypatch) -> None:
    files = _files(tmp_path, 3)
    tracks = {f.identity: _episode(700 + i, op_at=0.0, ed_at=700.0) for i, f in enumerate(files)}
    calls: list[str] = []
    _patch_fingerprints(monkeypatch, tracks, calls)
    state = {"n": 0}

    def should_stop():
        state["n"] += 1
        return "foreground" if len(calls) >= 1 else None

    report = analyze_title(files, cache=IntroSkipperCache(tmp_path / "c"), ffmpeg="f", ffprobe="p",
                           should_stop=should_stop)
    assert report.stopped == "foreground" and len(calls) == 1 and report.analysed == 0


def test_fingerprint_budget_per_pass(tmp_path, monkeypatch) -> None:
    files = _files(tmp_path, 4)
    tracks = {f.identity: _episode(800 + i, op_at=0.0, ed_at=700.0) for i, f in enumerate(files)}
    calls: list[str] = []
    _patch_fingerprints(monkeypatch, tracks, calls)
    report = analyze_title(files, cache=IntroSkipperCache(tmp_path / "c"), ffmpeg="f", ffprobe="p",
                           max_new_fingerprints=2)
    assert len(calls) == 2 and report.fingerprinted == 2 and report.analysed == 2


# ------------------------------------------------------- background pass ---

class FakeLease:
    def __init__(self, scheduler) -> None:
        self.scheduler = scheduler

    def release(self) -> None:
        self.scheduler.released += 1


class FakeScheduler:
    def __init__(self, *, wait_reason=None, lease=True) -> None:
        self.wait_reason = wait_reason
        self.lease = lease
        self.acquired: list[dict] = []
        self.released = 0

    def background_wait_reason(self, **_kwargs):
        return self.wait_reason

    def should_yield_to_higher_priority(self, _priority) -> bool:
        return False

    def acquire_heavy(self, name, **kwargs):
        self.acquired.append({"name": name, **kwargs})
        return FakeLease(self) if self.lease else None


def _fake_db(tmp_path: Path, files: list[EpisodeFile], *, state: str = "ready"):
    anime = SimpleNamespace(media_id=77, title="Show", status="CURRENT")
    rows = [SimpleNamespace(episode=f.episode, video_path=f.path, state=state) for f in files]
    db = SimpleNamespace(anime_list=lambda *a: [anime], episodes=lambda media_id: rows if media_id == 77 else [])
    return db


def test_background_pass_uses_background_lease_and_releases_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    monkeypatch.setattr(intro_skipper, "_register_and_enforce", lambda *a: None)
    files = _files(tmp_path, 2)
    tracks = {f.identity: _episode(900 + i, op_at=0.0, ed_at=700.0) for i, f in enumerate(files)}
    _patch_fingerprints(monkeypatch, tracks, [])
    scheduler = FakeScheduler()
    result = run_background_pass(db=_fake_db(tmp_path, files), cache_dir=tmp_path / "cache", ffmpeg="ffmpeg",
                                 ffprobe="ffprobe", scheduler=scheduler)
    assert result["status"] == "ran" and result["found"] == 2 and result["media_id"] == 77
    from pudge.work_scheduler import WorkPriority

    assert scheduler.acquired[0]["priority"] == WorkPriority.BACKGROUND
    assert scheduler.acquired[0]["foreground_sensitive"] is True
    assert scheduler.released == 1
    # Everything analysed: the next pass is idle and takes no lease.
    again = run_background_pass(db=_fake_db(tmp_path, files), cache_dir=tmp_path / "cache", ffmpeg="ffmpeg",
                                ffprobe="ffprobe", scheduler=scheduler)
    assert again["status"] == "idle" and len(scheduler.acquired) == 1


def test_background_pass_releases_lease_on_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    files = _files(tmp_path, 2)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(intro_skipper, "analyze_title", boom)
    scheduler = FakeScheduler()
    with pytest.raises(RuntimeError):
        run_background_pass(db=_fake_db(tmp_path, files), cache_dir=tmp_path / "c", ffmpeg="f", ffprobe="p",
                            scheduler=scheduler)
    assert scheduler.released == 1


def test_background_pass_defers_during_playback_and_without_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    files = _files(tmp_path, 2)
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    playing = FakeScheduler(wait_reason="foreground")
    assert run_background_pass(db=_fake_db(tmp_path, files), cache_dir=tmp_path, ffmpeg="f", ffprobe="p",
                               scheduler=playing) == {"status": "deferred", "reason": "foreground"}
    assert playing.acquired == []
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: ["ffprobe"])
    assert run_background_pass(db=_fake_db(tmp_path, files), cache_dir=tmp_path, ffmpeg="f", ffprobe="p",
                               scheduler=FakeScheduler())["status"] == "tool_missing"
    assert run_background_pass(db=_fake_db(tmp_path, files), cache_dir=tmp_path, ffmpeg="f", ffprobe="p",
                               scheduler=FakeScheduler(), enabled=False) == {"status": "disabled"}


def test_downloading_files_are_not_analysed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    files = _files(tmp_path, 2)
    scheduler = FakeScheduler()
    result = run_background_pass(db=_fake_db(tmp_path, files, state="downloading"), cache_dir=tmp_path / "c",
                                 ffmpeg="f", ffprobe="p", scheduler=scheduler)
    assert result["status"] == "idle"
    result = run_background_pass(db=_fake_db(tmp_path, files), cache_dir=tmp_path / "c", ffmpeg="f",
                                 ffprobe="p", scheduler=scheduler, incomplete_paths=[tmp_path])
    assert result["status"] == "idle" and scheduler.acquired == []


def test_cache_quota_registered(tmp_path, monkeypatch) -> None:
    from pudge.cache_registry import CacheRegistry
    from pudge.database import Database

    db = Database(tmp_path / "lib.sqlite3")
    cache_dir = tmp_path / "cache"
    old = cache_dir / "intro-skipper" / "results" / "old.json"
    old.parent.mkdir(parents=True)
    old.write_text("x" * 1000)
    new = cache_dir / "intro-skipper" / "results" / "new.json"
    new.write_text("y" * 1000)
    registry = CacheRegistry(db, cache_dir)
    registry.register(intro_skipper.CACHE_CATEGORY, old)
    monkeypatch.setattr(intro_skipper, "CACHE_MAX_BYTES", 1500)
    import time as _time

    _time.sleep(0.01)
    intro_skipper._register_and_enforce(db, cache_dir, [new], intro_skipper.logging.getLogger("t"))
    assert new.exists() and not old.exists()


def test_manager_pass_is_isolated_from_failures(tmp_path, monkeypatch) -> None:
    from tests.test_manager import _upgrade_manager

    manager = _upgrade_manager(tmp_path)
    monkeypatch.setattr(intro_skipper, "run_background_pass", lambda **_k: (_ for _ in ()).throw(OSError("x")))
    assert manager.run_intro_detection() == 0
    monkeypatch.setattr(intro_skipper, "run_background_pass", lambda **k: {"status": "ran", "analysed": 2,
                                                                          "enabled": k["enabled"]})
    assert manager.run_intro_detection() == 2


# ------------------------------------------------ end to end with FFmpeg ---

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _write_wav(path: Path, track: np.ndarray) -> None:
    pcm = (np.clip(track, -1, 1) * 32000).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(pcm.tobytes())


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")
def test_end_to_end_reencoded_corpus(tmp_path) -> None:
    """Different codecs/bitrates/volumes/rates; boundaries within 2 s; a negative title gets nothing."""
    specs = [
        # Native FFmpeg encoders only (present in every build); lossy, other
        # sample rates, volume and channel layout.
        (1, 0.0, 700.0, ["-c:a", "aac", "-aac_coder", "fast", "-b:a", "64k"]),
        (2, 95.0, 740.0, ["-c:a", "mp2", "-b:a", "96k", "-ar", "22050", "-af", "volume=0.6"]),
        (3, 30.0, 768.0, ["-c:a", "mp2", "-b:a", "128k", "-ac", "2", "-ar", "32000"]),
    ]
    files = []
    for n, op_at, ed_at, codec in specs:
        wav = tmp_path / f"ep{n}.wav"
        _write_wav(wav, _episode(1000 + n, op_at=op_at, ed_at=ed_at))
        mkv = tmp_path / f"Show - {n:02d}.mkv"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(wav), "-metadata:s:a:0", "language=jpn",
                        *codec, str(mkv)], check=True)
        wav.unlink()
        files.append(EpisodeFile(path=mkv, episode=n, identity=intro_skipper.identity_of(mkv)))
    cache = IntroSkipperCache(tmp_path / "cache")
    report = analyze_title(files, cache=cache, ffmpeg=FFMPEG, ffprobe=FFPROBE)
    assert report.found == 3, report
    expected = {1: (0.0, 85.0, 700.0, 772.0), 2: (95.0, 180.0, 740.0, 812.0), 3: (30.0, 115.0, 768.0, 840.0)}
    for item in files:
        segments = {s.kind: s for s in segments_for_video(tmp_path / "cache", item.path)}
        op_start, op_end, ed_start, ed_end = expected[item.episode]
        assert abs(segments["intro"].start_seconds - op_start) <= 2, (item.episode, segments)
        assert abs(segments["intro"].end_seconds - op_end) <= 2, (item.episode, segments)
        assert abs(segments["outro"].start_seconds - ed_start) <= 2, (item.episode, segments)
        assert abs(segments["outro"].end_seconds - min(ed_end, 840.0)) <= 2, (item.episode, segments)

    negative = []
    for n in (1, 2):
        wav = tmp_path / f"neg{n}.wav"
        _write_wav(wav, _episode(2000 + n, op_at=None, ed_at=None, total=600.0))
        mkv = tmp_path / f"Other - {n:02d}.mkv"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(wav), "-c:a", "mp2", str(mkv)], check=True)
        negative.append(EpisodeFile(path=mkv, episode=n, identity=intro_skipper.identity_of(mkv)))
    neg_report = analyze_title(negative, cache=cache, ffmpeg=FFMPEG, ffprobe=FFPROBE)
    assert neg_report.empty == 2 and neg_report.found == 0
    assert all(segments_for_video(tmp_path / "cache", item.path) == [] for item in negative)
