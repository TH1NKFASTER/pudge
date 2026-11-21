"""Audio-derived times are playback times when the audio track starts late or early.

Synthetic releases are made with FFmpeg in the test: tone bursts are the "speech",
``-itsoffset`` moves the audio (+1.0 s: a DUAL/remux audio delay) or the video (+0.5 s:
the audio starts first), in MKV, MP4, MPEG-TS (its own 1.4 s start) and without video.
A subtitle file that matches the bursts in playback time (container time minus the
file's start time, which is what mpv shows as 0) must come out of the real pipeline
functions within 0.05 s: the segment windows, the shared ffsubsync speech file, ffsubsync
on the whole file, ALASS on the video, the Japanese STT clock, the OP/ED fingerprints and
the double-episode parts. ffsubsync, ALASS and the MLX worker are not installed here:
small stand-ins decode the given file with FFmpeg exactly as they do (raw PCM from the
track's first sample), so the time base under test is FFmpeg's.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import types
import wave
from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from pudge import audio_timeline, intro_skipper, subtitle_migrations, syncing  # noqa: E402
from pudge.config import SyncConfig  # noqa: E402
from pudge.double_episode import cut_audio_part  # noqa: E402
from pudge.subtitle_formats import parse_srt, write_srt  # noqa: E402
from pudge.subtitles import stt  # noqa: E402
from tests.media_tools import paired_ffmpeg, requires_ffmpeg  # noqa: E402

pytestmark = requires_ffmpeg

FFMPEG, FFPROBE = paired_ffmpeg()
SR = 48_000
LENGTH = 21.0
# (start, duration) in the audio's own time: uneven, so one shift fits.
BURSTS = [
    (1.5, 0.5),
    (3.2, 0.25),
    (4.0, 0.9),
    (6.1, 0.4),
    (7.3, 0.7),
    (9.0, 0.3),
    (10.2, 1.0),
    (12.4, 0.35),
    (13.5, 0.6),
    (15.3, 0.45),
    (16.6, 0.8),
    (18.5, 0.3),
    (19.4, 0.55),
]
TOLERANCE = 0.05


# --------------------------------------------------------------------------- media ---


def _bursts_wav(path: Path, *, length: float = LENGTH, shift: float = 0.0, silent: bool = False) -> Path:
    t = np.arange(int(length * SR)) / SR
    signal = np.zeros_like(t)
    for start, duration in [] if silent else BURSTS:
        a, b = int((start + shift) * SR), int((start + shift + duration) * SR)
        tone = 0.45 * np.sin(2 * np.pi * 700 * t[a:b]) + 0.2 * np.sin(2 * np.pi * 1400 * t[a:b])
        ramp = min(240, (b - a) // 2)
        tone[:ramp] *= np.linspace(0, 1, ramp)
        tone[-ramp:] *= np.linspace(1, 0, ramp)
        signal[a:b] = tone
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes((np.clip(signal, -1, 1) * 32000).astype("<i2").tobytes())
    return path


def _probe(path: Path) -> dict:
    completed = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=start_time:stream=codec_type,start_time",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


class Release:
    def __init__(
        self, path: Path, origin: float, *, shift: float = 0.0, gap_at: float | None = None, gap: float = 0.0
    ) -> None:
        self.path = path
        payload = _probe(path)
        self.format_start = float(payload["format"]["start_time"])
        # Playback time (mpv: container time minus the file start) of the WAV's sample 0.
        self.audio_zero = origin - self.format_start
        self.shift, self.gap_at, self.gap = shift, gap_at, gap

    def playback_bursts(self) -> list[tuple[float, float]]:
        rows = []
        for start, duration in BURSTS:
            at = start + self.shift
            moved = self.gap if self.gap_at is not None and at >= self.gap_at else 0.0
            rows.append((at + moved + self.audio_zero, at + duration + moved + self.audio_zero))
        return rows

    def subtitle(self, folder: Path, name: str = "matching.ja.srt") -> Path:
        """A Japanese subtitle that matches the bursts on the playback timeline."""
        cues = [(start, end, f"台詞{index}") for index, (start, end) in enumerate(self.playback_bursts(), 1)]
        return write_srt(cues, folder / name)


def _make(
    folder: Path,
    name: str,
    *,
    audio_offset: float = 0.0,
    video_offset: float | None = 0.0,
    subtitle_track: list[tuple[float, float, str]] | None = None,
    audio_codec: tuple[str, ...] = ("-c:a", "aac", "-b:a", "96k"),
    shift: float = 0.0,
    length: float = LENGTH,
    gap_at: float | None = None,
    gap: float = 0.0,
) -> Release:
    """``gap_at``/``gap``: the track's timestamps jump by ``gap`` there (WAV time), as at a remux seam."""
    wav = folder / (f"bursts-{shift:g}-{length:g}.wav" if shift or length != LENGTH else "bursts.wav")
    if not wav.exists():
        _bursts_wav(wav, length=length, shift=shift)
    path = folder / name
    command = [FFMPEG, "-v", "error", "-y"]
    maps: list[str] = []
    count = 0
    if video_offset is not None:
        if video_offset:
            command += ["-itsoffset", f"{video_offset}"]
        rate = 10 if length <= 60 else 2
        command += ["-f", "lavfi", "-i", f"color=c=black:s=32x32:r={rate}:d={length + gap + 2}"]
        maps += ["-map", f"{count}:v"]
        count += 1
    if audio_offset:
        command += ["-itsoffset", f"{audio_offset}"]
    command += ["-i", str(wav)]
    maps += ["-map", f"{count}:a"]
    count += 1
    if subtitle_track is not None:
        track = write_srt(subtitle_track, folder / f"{path.stem}.track.srt")
        command += ["-i", str(track)]
        maps += ["-map", f"{count}:s", "-metadata:s:s:0", "language=eng"]
    command += maps
    if video_offset is not None:
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-bf", "0"]
    command += list(audio_codec)
    if gap_at is not None:
        # asetpts sees container time (the -itsoffset included).
        command += ["-af", f"asetpts='if(gte(T,{gap_at + audio_offset}),PTS+{gap}/TB,PTS)'"]
    if subtitle_track is not None:
        command += ["-c:s", "srt" if path.suffix != ".mp4" else "mov_text"]
    command.append(str(path))
    subprocess.run(command, check=True)
    payload = _probe(path)
    starts = {row["codec_type"]: float(row.get("start_time") or 0.0) for row in payload["streams"]}
    # A muxer that cannot store a negative start shifts every stream: the video (first
    # frame at its offset, no B-frames) shows how far. Without video only positive offsets.
    muxer_shift = starts["video"] - float(video_offset or 0.0) if video_offset is not None else 0.0
    return Release(path, audio_offset + muxer_shift, shift=shift, gap_at=gap_at, gap=gap)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("releases")
    return {
        "mkv_late": _make(folder, "late.mkv", audio_offset=1.0),
        "mp4_late": _make(folder, "late.mp4", audio_offset=1.0),
        "ts_late": _make(folder, "late.ts", audio_offset=1.0),
        "mka_late": _make(folder, "late.mka", audio_offset=1.0, video_offset=None),
        "mkv_early": _make(folder, "early.mkv", audio_offset=-0.5),
        "mp4_early": _make(folder, "early.mp4", audio_offset=-0.5),
        "mkv_video_late": _make(folder, "video-late.mkv", video_offset=0.5),
        "ts_plain": _make(folder, "plain.ts"),
        "mkv_plain": _make(folder, "plain.mkv"),
    }


LATE = ["mkv_late", "mp4_late", "ts_late"]
EARLY = ["mkv_early", "mp4_early", "mkv_video_late"]
ALL = [*LATE, "mka_late", *EARLY, "ts_plain", "mkv_plain"]


def _onsets(path: Path) -> list[float]:
    raw = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return _onsets_of(np.frombuffer(raw, dtype="<i2").astype(float) / 32768.0, 16000)


def _onsets_of(samples, rate: int) -> list[float]:
    frame = rate // 200  # 5 ms
    count = len(samples) // frame
    energy = np.sqrt((np.asarray(samples[: count * frame]).reshape(count, frame) ** 2).mean(axis=1))
    active = energy > 0.08
    # Rising edges only: a burst already sounding when the audio starts is not an onset.
    return [index * frame / rate for index in range(1, count) if active[index] and not active[index - 1]]


def _assert_matches(found: list[float], expected: list[float], *, tolerance: float = TOLERANCE) -> None:
    assert len(found) == len(expected), (found, expected)
    worst = max(abs(a - b) for a, b in zip(found, expected))
    assert worst < tolerance, (worst, found, expected)


def _starts_within(release: Release, start: float, end: float) -> list[float]:
    return [a for a, _b in release.playback_bursts() if start + 0.02 < a < end - 0.05]


# ------------------------------------------------------------- stand-in ffsubsync ---


def _speech_of_samples(samples, rate: int) -> object:
    frame = rate // 100
    count = len(samples) // frame
    energy = np.sqrt((np.asarray(samples[: count * frame]).reshape(count, frame) ** 2).mean(axis=1))
    return (energy > 0.08).astype(float)


def _speech_of_cues(cues, length: int | None = None) -> object:
    end = max((cue[1] for cue in cues), default=0.0)
    size = max(int(np.ceil(end * 100)) + 1, length or 0)
    speech = np.zeros(size)
    for start, stop, _text in cues:
        speech[max(0, int(round(start * 100))) : max(0, int(round(stop * 100)))] = 1.0
    return speech


class _VideoSpeechTransformer:
    """ffsubsync's speech step: embedded text subtitles first, else the decoded audio."""

    sample_rate = 100
    non_speech_label = 0.0

    def __init__(self, vad: str, ref_stream: str | None, ffmpeg: str) -> None:
        self.vad, self.ref_stream, self.ffmpeg = vad, ref_stream, ffmpeg

    def try_fit_using_embedded_subs(self, fname: str) -> None:
        completed = subprocess.run(
            [
                self.ffmpeg,
                "-loglevel",
                "fatal",
                "-nostdin",
                "-i",
                fname,
                "-map",
                self.ref_stream or "0:s:0",
                "-f",
                "srt",
                "-",
            ],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or "-->" not in completed.stdout:
            raise ValueError("Video file appears to lack subtitle stream")
        folder = Path(fname).parent
        temporary = folder / ".embedded-reference.srt"
        temporary.write_text(completed.stdout, encoding="utf-8")
        self.video_speech_results_ = _speech_of_cues(parse_srt(temporary))
        temporary.unlink()

    def fit(self, fname: str, *_args) -> "_VideoSpeechTransformer":
        if "subs" in self.vad and (self.ref_stream is None or self.ref_stream.startswith("0:s:")):
            try:
                self.try_fit_using_embedded_subs(fname)
                return self
            except Exception:  # noqa: BLE001 - as ffsubsync: fall back to the audio
                pass
        command = [self.ffmpeg, "-loglevel", "fatal", "-nostdin", "-i", fname]
        if self.ref_stream and self.ref_stream.startswith("0:a:"):
            command += ["-map", self.ref_stream]
        command += [
            "-f",
            "s16le",
            "-ac",
            "1",
            "-acodec",
            "pcm_s16le",
            "-af",
            "aresample=async=1",
            "-ar",
            "16000",
            "-",
        ]
        raw = subprocess.run(command, capture_output=True, check=True).stdout
        self.video_speech_results_ = _speech_of_samples(
            np.frombuffer(raw, dtype="<i2").astype(float) / 32768.0, 16000
        )
        return self

    def transform(self, *_args):
        return self.video_speech_results_


class _Pipeline:
    def __init__(self, step) -> None:
        self.steps = [("speech_extract", step)]

    def fit(self, fname: str):
        self.steps[0][1].fit(fname)
        return self

    def transform(self, fname: str):
        return self.steps[0][1].transform(fname)


class FakeFfsubsync:
    def __init__(self) -> None:
        self.runs: list[list[str]] = []

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        parser.add_argument("reference")
        parser.add_argument("-i", dest="srtin")
        parser.add_argument("-o", dest="srtout")
        parser.add_argument("--max-offset-seconds", type=float, default=60.0)
        parser.add_argument("--vad", default="subs_then_webrtc")
        parser.add_argument("--reference-stream", default=None)
        parser.add_argument("--ffmpeg-path", default=FFMPEG)
        parser.add_argument("--no-fix-framerate", action="store_true")
        parser.add_argument("--gss", action="store_true")
        return parser

    def _reference_pipe(self, args):
        return _Pipeline(_VideoSpeechTransformer(args.vad, args.reference_stream, args.ffmpeg_path or FFMPEG))

    def _run(self, args) -> dict:
        self.runs.append(sorted(vars(args).items()))
        if str(args.reference).endswith(".npz"):
            reference = np.load(args.reference)["speech"]
        else:
            pipe = self._reference_pipe(args)
            pipe.fit(args.reference)
            reference = pipe.transform(args.reference)
        cues = parse_srt(Path(args.srtin))
        subtitle = _speech_of_cues(cues)
        limit = int(round(args.max_offset_seconds * 100))
        padded = np.concatenate(
            [np.zeros(limit), np.asarray(reference, dtype=float), np.zeros(limit + subtitle.size)]
        )
        best, best_score = 0, -1.0
        for shift in range(-limit, limit + 1):
            window = padded[limit + shift : limit + shift + subtitle.size]
            score = float(np.dot(window, subtitle)) - 0.001 * abs(shift)
            if score > best_score:
                best, best_score = shift, score
        offset = best / 100.0
        write_srt([(a + offset, b + offset, text) for a, b, text in cues], Path(args.srtout))
        return {"sync_was_successful": True, "offset_seconds": offset, "framerate_scale_factor": 1.0}

    def install(self, monkeypatch) -> None:
        module = types.ModuleType("ffsubsync.ffsubsync")
        module.make_parser = self._parser
        module.make_reference_pipe = self._reference_pipe
        module.run = self._run
        package = types.ModuleType("ffsubsync")
        package.ffsubsync = module
        monkeypatch.setitem(sys.modules, "ffsubsync", package)
        monkeypatch.setitem(sys.modules, "ffsubsync.ffsubsync", module)


@pytest.fixture
def ffsubsync(monkeypatch):
    fake = FakeFfsubsync()
    fake.install(monkeypatch)
    return fake


def _config(**changes) -> SyncConfig:
    return replace(SyncConfig(), **changes)


# --------------------------------------------------------- the probe and its cache ---


@pytest.mark.parametrize("key", ALL)
def test_the_probe_reports_where_the_track_starts_on_the_playback_timeline(media, key: str) -> None:
    release = media[key]
    index, details = stt._select_japanese_audio_stream(release.path, FFPROBE)
    assert index is not None
    expected = release.audio_zero if release.audio_zero >= audio_timeline.MIN_OFFSET_SECONDS else 0.0
    assert abs(float(details["start_offset_seconds"]) - expected) < TOLERANCE, (key, details, expected)
    # The same probe filled the cache: no second ffprobe for the timeline.
    assert audio_timeline.timeline(release.path, None) is not None


# --------------------------------------------------------------- segment windows ---


@pytest.mark.parametrize("key", ALL)
@pytest.mark.parametrize("start", [0.0, 6.5, 12.0])
def test_segment_windows_are_on_the_playback_timeline(media, tmp_path: Path, key: str, start: float) -> None:
    release = media[key]
    length = 8.0
    audio, error = syncing._extract_audio_segment(
        release.path, start, length, tmp_path / "cache", ffmpeg_path=FFMPEG, force=False
    )
    assert audio is not None, error
    found = [start + value for value in _onsets(audio)]
    _assert_matches(found, _starts_within(release, start, start + length))


@pytest.mark.parametrize("key", ["mkv_late", "ts_late", "mkv_early", "mkv_plain"])
def test_the_segment_check_finds_no_offset_for_a_matching_subtitle(
    media, tmp_path: Path, ffsubsync, key: str
) -> None:
    release = media[key]
    config = _config(segment_count=3, segment_window_seconds=8.0, segment_max_offset_seconds=4.0)
    result = syncing.evaluate_segment_alignment(
        release.path,
        release.subtitle(tmp_path),
        tmp_path / "cache",
        config,
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
    )
    offsets = {item["label"]: item.get("offset_seconds") for item in result["segments"]}
    assert offsets and all(value is not None for value in offsets.values()), result
    assert max(abs(value) for value in offsets.values()) < TOLERANCE, offsets


# ------------------------------------------------- ffsubsync on the whole episode ---


@pytest.mark.parametrize("key", ["mkv_late", "mp4_late", "ts_late", "mka_late", "mkv_early", "mp4_early"])
def test_the_shared_speech_file_is_on_the_playback_timeline(
    media, tmp_path: Path, ffsubsync, key: str
) -> None:
    release = media[key]
    reference, result = syncing.prepare_speech_reference(
        release.path, tmp_path / "cache", _config(), ffmpeg_path=FFMPEG
    )
    assert reference is not None, result
    speech = np.load(reference)["speech"]
    found = [index / 100 for index in range(1, len(speech)) if speech[index] and not speech[index - 1]]
    _assert_matches(found, [a for a, _b in release.playback_bursts()])


@pytest.mark.parametrize("key", ["mkv_late", "mp4_late", "ts_late", "mkv_early", "mkv_plain"])
def test_ffsubsync_on_the_video_keeps_a_matching_subtitle(media, tmp_path: Path, ffsubsync, key: str) -> None:
    release = media[key]
    subtitle = release.subtitle(tmp_path)
    output, result = syncing.synchronize_subtitle(
        release.path, subtitle, tmp_path / "cache", _config(max_offset_seconds=5.0), ffmpeg_path=FFMPEG
    )
    assert result["sync_was_successful"] is True, result
    assert abs(float(result["offset_seconds"])) < TOLERANCE, result
    _assert_matches([cue[0] for cue in parse_srt(output)], [a for a, _b in release.playback_bursts()])


def test_with_the_real_ffsubsync_the_speech_file_of_a_late_track_is_padded(media, tmp_path: Path) -> None:
    """On a Mac with ffsubsync installed: its own decoding and speech step, padded by Pudge."""
    real = pytest.importorskip("ffsubsync.ffsubsync")
    args = real.make_parser().parse_args([str(media["mkv_late"].path)])
    assert syncing._watch_embedded_subtitle_reference(real.make_reference_pipe(args)) is not None
    arrays = {}
    for key in ("mkv_late", "mkv_plain"):
        reference, result = syncing.prepare_speech_reference(
            media[key].path, tmp_path / key, _config(vad="webrtc"), ffmpeg_path=FFMPEG
        )
        assert reference is not None, result
        arrays[key] = np.asarray(np.load(reference)["speech"], dtype=float).reshape(-1)
    late, plain = arrays["mkv_late"], arrays["mkv_plain"]
    if not late.any() or not plain.any():
        pytest.skip("webrtcvad heard no speech in the tone bursts")
    size = min(late.size, plain.size)
    lags = range(0, 200)
    best = max(lags, key=lambda lag: float(np.dot(late[lag:size], plain[: size - lag])))
    expected = media["mkv_late"].audio_zero - media["mkv_plain"].audio_zero
    assert abs(best / 100 - expected) < TOLERANCE, (best, expected)


def test_an_embedded_subtitle_reference_is_not_moved(tmp_path: Path, ffsubsync) -> None:
    # The English track is on the playback timeline already: no padding for it.
    late = _make(tmp_path, "late-with-subs.mkv", audio_offset=1.0)
    english = [(a, b, f"line {n}") for n, (a, b) in enumerate(late.playback_bursts(), 1)]
    release = _make(tmp_path, "late-subs.mkv", audio_offset=1.0, subtitle_track=english)
    reference, result = syncing.prepare_speech_reference(
        release.path, tmp_path / "cache", _config(), ffmpeg_path=FFMPEG
    )
    assert reference is not None, result
    speech = np.load(reference)["speech"]
    found = [index / 100 for index in range(1, len(speech)) if speech[index] and not speech[index - 1]]
    _assert_matches(found, [a for a, _b in release.playback_bursts()])


def test_results_from_before_the_padding_are_not_reused(media, tmp_path: Path, ffsubsync) -> None:
    release = media["mkv_late"]
    config = _config()
    _path, result = syncing.prepare_speech_reference(
        release.path, tmp_path / "cache", config, ffmpeg_path=FFMPEG
    )
    assert result.get("audio_start_offset_seconds") == pytest.approx(release.audio_zero, abs=TOLERANCE)
    subtitle = release.subtitle(tmp_path)
    output, _result = syncing.synchronize_subtitle(
        release.path, subtitle, tmp_path / "cache", config, ffmpeg_path=FFMPEG
    )
    # The cache name of an output made before (no delay in its key) is not the one used now.
    assert output.name != f"{syncing._fingerprint(release.path, subtitle, config, tag='ffsubsync')}.srt"


# ------------------------------------------------------------- ALASS on the video ---


FAKE_ALASS = r"""
import os, subprocess, sys
import numpy as np
sys.path.insert(0, {root!r})
from pudge.subtitle_formats import parse_srt, write_srt
from pathlib import Path

reference, source, output = sys.argv[1:4]
# ALASS decodes a media reference into raw PCM with FFmpeg (no timestamps), FFmpeg's track.
raw = subprocess.run(
    [os.environ.get("ALASS_FFMPEG_PATH", "ffmpeg"), "-v", "error", "-i", reference,
     "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
samples = np.frombuffer(raw, dtype="<i2").astype(float) / 32768.0
count = len(samples) // 80
speech = (np.sqrt((samples[: count * 80].reshape(count, 80) ** 2).mean(axis=1)) > 0.08).astype(float)
cues = parse_srt(Path(source))
end = int(max(cue[1] for cue in cues) * 100) + 1
subtitle = np.zeros(end)
for a, b, _text in cues:
    subtitle[int(round(a * 100)):int(round(b * 100))] = 1.0
limit = 500
padded = np.concatenate([np.zeros(limit), speech, np.zeros(limit + end)])
best = max(range(-limit, limit + 1), key=lambda s: float(np.dot(padded[limit + s: limit + s + end], subtitle)) - 0.001 * abs(s))
write_srt([(a + best / 100, b + best / 100, t) for a, b, t in cues], Path(output))
print(f"shifted block of {{len(cues)}} subtitles with length 0:00:20.000 by {{best / 100:.3f}}")
"""


@pytest.mark.parametrize("key", ["mkv_late", "ts_late", "mkv_early", "mkv_plain"])
def test_alass_on_the_video_keeps_a_matching_subtitle(media, tmp_path: Path, key: str) -> None:
    release = media[key]
    script = tmp_path / "alass"
    script.write_text(
        f"#!{sys.executable}\n" + FAKE_ALASS.format(root=str(Path(__file__).resolve().parents[1])),
        encoding="utf-8",
    )
    script.chmod(0o755)
    output, result = syncing.synchronize_with_alass(
        release.path,
        release.subtitle(tmp_path),
        tmp_path / "cache",
        _config(),
        alass_path=str(script),
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
    )
    assert result["sync_was_successful"] is True, result
    _assert_matches([cue[0] for cue in parse_srt(output)], [a for a, _b in release.playback_bursts()])


# ------------------------------------------------------------- Japanese STT clock ---


FAKE_WORKER = r"""
import json, os, subprocess, sys
import numpy as np

if "--check" in sys.argv:
    sys.exit(0)
audio, result = sys.argv[sys.argv.index("--words") + 1 : sys.argv.index("--words") + 3]
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", audio, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                     capture_output=True, check=True).stdout
samples = np.frombuffer(raw, dtype="<i2").astype(float) / 32768.0
count = len(samples) // 80
active = np.sqrt((samples[: count * 80].reshape(count, 80) ** 2).mean(axis=1)) > 0.08
segments, start = [], None
for index, value in enumerate(list(active) + [False]):
    if value and start is None:
        start = index
    elif not value and start is not None:
        segments.append({"start": start * 0.005, "end": index * 0.005, "text": "こんにちは"})
        start = None
json.dump({"segments": segments}, open(result, "w", encoding="utf-8"))
"""


@pytest.mark.parametrize("key", ["mkv_late", "ts_late", "mkv_early", "mkv_plain"])
def test_the_stt_clock_is_on_the_playback_timeline(media, tmp_path: Path, monkeypatch, key: str) -> None:
    release = media[key]
    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{worker}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setenv("PUDGE_PYTHON", str(python))
    reference, info = stt.prepare_japanese_stt_reference(
        release.path,
        tmp_path / "cache",
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
        model="fake",
        timeout_seconds=60,
    )
    assert reference is not None, info
    _assert_matches([cue[0] for cue in parse_srt(reference)], [a for a, _b in release.playback_bursts()])


# ------------------------------------------------------------ OP/ED fingerprints ---


@pytest.mark.parametrize("delay", [1.0, 0.0])
def test_opening_fingerprints_are_on_the_playback_timeline(tmp_path: Path, monkeypatch, delay: float) -> None:
    # OP/ED detection needs 2 minutes: bursts at the start, silence after.
    wav = _bursts_wav(tmp_path / "long.wav", length=125.0)
    path = tmp_path / "Show - 01.mkv"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=2:d=127",
            *(["-itsoffset", str(delay)] if delay else []),
            "-i",
            str(wav),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-bf",
            "0",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )
    regions: list[tuple[float, object]] = []
    real = intro_skipper.decode_region

    def recording(video, **kwargs):
        samples = real(video, **kwargs)
        regions.append((float(kwargs["start_seconds"]), samples))
        return samples

    monkeypatch.setattr(intro_skipper, "decode_region", recording)
    item = intro_skipper.EpisodeFile(path=path, episode=1, identity=intro_skipper.identity_of(path))
    cache = intro_skipper.IntroSkipperCache(tmp_path / "cache")
    fps = intro_skipper._fingerprints_for(
        item, cache, ffmpeg=FFMPEG, ffprobe=FFPROBE, allow_compute=True, cancel_check=None
    )
    assert fps is not None
    head_start, head = regions[0]
    from pudge.audio_fingerprints import SAMPLE_RATE

    _assert_matches(
        [head_start + value for value in _onsets_of(head, SAMPLE_RATE)], [a + delay for a, _b in BURSTS]
    )


def test_fingerprints_of_a_late_track_made_before_are_made_again(tmp_path: Path) -> None:
    release = _make(tmp_path, "Show - 02.mkv", audio_offset=1.0)
    item = intro_skipper.EpisodeFile(
        path=release.path, episode=2, identity=intro_skipper.identity_of(release.path)
    )
    cache = intro_skipper.IntroSkipperCache(tmp_path / "cache")
    meta = intro_skipper._meta_path(cache, item.identity)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps({"engine": "pudge-hk-1", "stream": None, "duration": 23.0, "identity": item.identity})
    )
    cache.write_result(item.identity, {"status": "found", "segments": []})
    assert intro_skipper._early_legacy_fingerprints(cache, item, FFPROBE) is True
    assert cache.read_result(item.identity) is None
    plain = _make(tmp_path, "Show - 03.mkv")
    other = intro_skipper.EpisodeFile(
        path=plain.path, episode=3, identity=intro_skipper.identity_of(plain.path)
    )
    other_meta = intro_skipper._meta_path(cache, other.identity)
    other_meta.write_text(json.dumps({"engine": "pudge-hk-1", "stream": None, "duration": 23.0}))
    assert intro_skipper._early_legacy_fingerprints(cache, other, FFPROBE) is False
    # Checked once: the next process does not probe it again.
    assert json.loads(other_meta.read_text())["timeline"] == audio_timeline.VERSION
    # Audio that starts before the video: the opening region lost that lead.
    first = _make(tmp_path, "Show - 04.mkv", video_offset=0.5)
    early = intro_skipper.EpisodeFile(
        path=first.path, episode=4, identity=intro_skipper.identity_of(first.path)
    )
    intro_skipper._meta_path(cache, early.identity).write_text(
        json.dumps({"engine": "pudge-hk-1", "stream": None})
    )
    assert intro_skipper._early_legacy_fingerprints(cache, early, FFPROBE) is True


def test_opening_fingerprints_of_audio_that_starts_first_keep_the_lead(tmp_path: Path, monkeypatch) -> None:
    wav = _bursts_wav(tmp_path / "long.wav", length=125.0)
    path = tmp_path / "Show - 06.mkv"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-itsoffset",
            "0.5",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=2:d=127",
            "-i",
            str(wav),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-bf",
            "0",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )
    start = float(_probe(path)["format"]["start_time"])
    regions: list[tuple[float, object]] = []
    real = intro_skipper.decode_region

    def recording(video, **kwargs):
        samples = real(video, **kwargs)
        regions.append((float(kwargs["start_seconds"]), samples))
        return samples

    monkeypatch.setattr(intro_skipper, "decode_region", recording)
    item = intro_skipper.EpisodeFile(path=path, episode=6, identity=intro_skipper.identity_of(path))
    assert intro_skipper._fingerprints_for(
        item,
        intro_skipper.IntroSkipperCache(tmp_path / "cache"),
        ffmpeg=FFMPEG,
        ffprobe=FFPROBE,
        allow_compute=True,
        cancel_check=None,
    )
    from pudge.audio_fingerprints import SAMPLE_RATE

    _assert_matches(_onsets_of(regions[0][1], SAMPLE_RATE), [a - start for a, _b in BURSTS])


# ------------------------------------------------------------ double-episode parts ---


@pytest.mark.parametrize("key", ["mkv_late", "ts_late", "mkv_early", "mkv_plain"])
@pytest.mark.parametrize("start,end", [(0.0, 11.0), (8.0, None)])
def test_double_episode_parts_start_at_their_playback_time(
    media, tmp_path: Path, key: str, start: float, end: float | None
) -> None:
    release = media[key]
    part = cut_audio_part(FFMPEG, release.path, start, end, tmp_path / "part.mkv", ffprobe=FFPROBE)
    assert abs(float(_probe(part)["format"]["start_time"])) < 0.03  # the part's own 0 is its start
    stop = end if end is not None else LENGTH + 3
    _assert_matches([start + value for value in _onsets(part)], _starts_within(release, start, stop))


# --------------------------------------------------- files that do not start at 0 ---


def test_a_file_that_starts_late_has_its_own_start_as_zero(tmp_path: Path) -> None:
    """Audio + subtitles only (a compact proxy), first packet at 1.0 s: mpv shows that as 0.

    Pudge's embedded subtitle extraction and audio both subtract the same start.
    """
    late = _make(tmp_path, "proxy-audio.mka", audio_offset=1.0, video_offset=None)
    track = [(a + 1.0, b + 1.0, f"line {n}") for n, (a, b) in enumerate(late.playback_bursts(), 1)]
    proxy = _make(tmp_path, "proxy.mka", audio_offset=1.0, video_offset=None, subtitle_track=track)
    assert proxy.format_start == pytest.approx(1.0, abs=0.07)
    reference, result = syncing.extract_embedded_timing_reference(
        proxy.path, tmp_path / "cache", ffmpeg_path=FFMPEG, ffprobe_path=FFPROBE
    )
    assert reference is not None, result
    starts = [cue[0] for cue in parse_srt(reference)]
    audio, error = syncing._extract_audio_segment(
        proxy.path, 0.0, 30.0, tmp_path / "cache", ffmpeg_path=FFMPEG, force=False
    )
    assert audio is not None, error
    # Subtitle and audio agree, both counted from the file's own start.
    _assert_matches(_onsets(audio), starts)


# ------------------------------------------------------------------ rebuild rule ---


def test_unwatched_episodes_of_a_late_track_are_prepared_again_once(tmp_path: Path, monkeypatch) -> None:
    from t71g08_support import subtitle_library

    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    manager.config.tools.ffprobe = FFPROBE
    late = _make(tmp_path, "source-late.mkv", audio_offset=1.0)
    plain = _make(tmp_path, "source-plain.mkv")
    shutil.copyfile(late.path, video)

    def episode(name: str, number: int, source: Release, state: str) -> Path:
        from pudge.manager_models import LibraryEpisode

        path = (manager.config.library.root_dir / name).resolve()
        shutil.copyfile(source.path, path)
        subtitle = tmp_path / f"{name}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n古い\n", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=5,
                title="Show",
                episode=number,
                video_path=path,
                subtitle_path=subtitle,
                subtitle_origin="jimaku",
                state=state,
            )
        )
        return path

    plain_video = episode("Show - 02.mkv", 2, plain, "ready")
    watched_video = episode("Show - 03.mkv", 3, late, "watched")
    for number, path in enumerate((video, plain_video, watched_video), 1):
        manager.db.record_subtitle_history(
            video_path=path,
            media_id=5,
            episode=number,
            source="jimaku",
            candidate_name="old.srt",
            candidate_path=manager.db.episode_by_path(path).subtitle_path,
            status="selected",
            details={"alignment": {"engine": "ffsubsync", "offset_seconds": -1.0}},
        )

    assert manager._requeue_late_audio_tracks() == 1
    queued = {Path(str(job["video_path"])).resolve() for job in manager.db.subtitle_jobs()}
    assert video in queued and plain_video not in queued and watched_video not in queued
    assert manager.db.get_state("subtitle_audio_playback_timeline_generation", "") == "1"
    assert manager._requeue_late_audio_tracks() == 0  # once


def test_the_rule_waits_while_ffprobe_cannot_be_found(tmp_path: Path, monkeypatch) -> None:
    from t71g08_support import subtitle_library

    manager, _video, _commands = subtitle_library(tmp_path, monkeypatch)
    monkeypatch.setattr(subtitle_migrations, "_configured_ffprobe", lambda _manager: None)
    counts = subtitle_migrations.run_pending_rules(manager)
    assert "late_audio_track" not in counts
    assert manager.db.get_state("subtitle_audio_playback_timeline_generation", "") == ""


# -------------------------------------------------------- review: more situations ---


def _fake_alass(folder: Path) -> Path:
    script = folder / "alass"
    script.write_text(
        f"#!{sys.executable}\n" + FAKE_ALASS.format(root=str(Path(__file__).resolve().parents[1])),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _make_tracks(folder: Path, name: str, tracks: list[dict]) -> tuple[Path, float]:
    """A video at 0 with several AAC tracks: wav, offset (>= 0), language, channels, default."""
    path = folder / name
    command = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=32x32:r=10:d={LENGTH + 3}"]
    for track in tracks:
        if track.get("offset"):
            command += ["-itsoffset", str(track["offset"])]
        command += ["-i", str(track["wav"])]
    command += ["-map", "0:v"]
    for number in range(len(tracks)):
        command += ["-map", f"{number + 1}:a"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-bf", "0", "-c:a", "aac", "-b:a", "96k"]
    for number, track in enumerate(tracks):
        command += [f"-ac:a:{number}", str(track.get("channels", 2))]
        command += [f"-disposition:a:{number}", "default" if track.get("default") else "0"]
        if track.get("language"):
            command += [f"-metadata:s:a:{number}", f"language={track['language']}"]
    command.append(str(path))
    subprocess.run(command, check=True)
    return path, float(_probe(path)["format"]["start_time"])


@pytest.fixture(scope="module")
def wavs(tmp_path_factory):
    folder = tmp_path_factory.mktemp("wavs")
    return {
        "bursts": _bursts_wav(folder / "bursts.wav"),
        # The same speech 1 s later in the file: in sync with a track delayed by 1 s.
        "bursts+1": _bursts_wav(folder / "bursts-plus1.wav", length=LENGTH + 1, shift=1.0),
        "silence": _bursts_wav(folder / "silence.wav", silent=True),
    }


def _expected_with_delay(format_start: float, delay: float) -> list[float]:
    return [start + delay - format_start for start, _duration in BURSTS]


def test_ffmpeg_default_track_is_the_default_flagged_one(tmp_path: Path, wavs, ffsubsync) -> None:
    # 6ch without the default flag, 2ch default delayed 1 s; no language tags (no
    # Japanese track to name): ffsubsync decodes FFmpeg's choice, the default one.
    path, start = _make_tracks(
        tmp_path,
        "untagged.mkv",
        [
            {"wav": wavs["silence"], "channels": 6},
            {"wav": wavs["bursts"], "offset": 1.0, "channels": 2, "default": True},
        ],
    )
    found = audio_timeline.timeline(path, FFPROBE)
    assert found is not None and found.default_position() == 1
    assert syncing._ffsubsync_audio_delay(path, FFMPEG, None) == pytest.approx(1.0, abs=TOLERANCE)
    reference, result = syncing.prepare_speech_reference(
        path, tmp_path / "cache", _config(), ffmpeg_path=FFMPEG
    )
    assert reference is not None, result
    speech = np.load(reference)["speech"]
    onsets = [index / 100 for index in range(1, len(speech)) if speech[index] and not speech[index - 1]]
    _assert_matches(onsets, _expected_with_delay(start, 1.0))


def test_a_delayed_japanese_track_that_is_not_first(tmp_path: Path, wavs) -> None:
    path, start = _make_tracks(
        tmp_path,
        "dual.mkv",
        [
            {"wav": wavs["bursts+1"], "language": "eng", "channels": 2, "default": True},
            {"wav": wavs["bursts"], "offset": 1.0, "language": "jpn", "channels": 2},
        ],
    )
    assert syncing._japanese_audio_stream_spec(path, FFMPEG) == "0:a:1"
    index, details = stt._select_japanese_audio_stream(path, FFPROBE)
    assert details["audio_position"] == 1 and details["start_offset_seconds"] == pytest.approx(
        1.0, abs=TOLERANCE
    )
    for window in (0.0, 9.0):
        audio, error = syncing._extract_audio_segment(
            path, window, 8.0, tmp_path / "cache", ffmpeg_path=FFMPEG, force=False
        )
        assert audio is not None, error
        expected = [
            value for value in _expected_with_delay(start, 1.0) if window + 0.02 < value < window + 7.95
        ]
        _assert_matches([window + value for value in _onsets(audio)], expected)


def test_alass_never_reads_a_delayed_track_of_its_own_choice(tmp_path: Path, wavs) -> None:
    # The Japanese track starts with the video; ALASS's own (FFmpeg's default) track
    # is an English one delayed by 1 s. Both are in sync with the picture.
    path, start = _make_tracks(
        tmp_path,
        "dub-delayed.mkv",
        [
            {"wav": wavs["bursts+1"], "language": "jpn", "channels": 2},
            {"wav": wavs["bursts"], "offset": 1.0, "language": "eng", "channels": 6, "default": True},
        ],
    )
    expected = _expected_with_delay(start, 1.0)
    subtitle = write_srt(
        [(value, value + 0.3, f"台詞{n}") for n, value in enumerate(expected, 1)], tmp_path / "match.ja.srt"
    )
    output, result = syncing.synchronize_with_alass(
        path,
        subtitle,
        tmp_path / "cache",
        _config(),
        alass_path=str(_fake_alass(tmp_path)),
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
    )
    assert result["sync_was_successful"] is True, result
    _assert_matches([cue[0] for cue in parse_srt(output)], expected)


def test_a_failed_speech_file_is_not_replaced_by_unpadded_decoding(
    media, tmp_path: Path, ffsubsync, monkeypatch
) -> None:
    release = media["mkv_late"]
    monkeypatch.setattr(
        syncing, "prepare_speech_reference", lambda *_a, **_k: (None, {"reason": "speech_cache_error"})
    )
    output, result = syncing.synchronize_subtitle(
        release.path, release.subtitle(tmp_path), tmp_path / "cache", _config(), ffmpeg_path=FFMPEG
    )
    assert result["sync_was_successful"] is False and result["reason"] == "speech_reference_unavailable"
    assert ffsubsync.runs == []
    assert not list((tmp_path / "cache" / "synced").glob("*.srt"))
    assert output.name == "matching.ja.srt"


# --- a gap inside the track (a remux seam) -------------------------------------------


@pytest.fixture(scope="module")
def gapped(tmp_path_factory):
    folder = tmp_path_factory.mktemp("gapped")
    flac = ("-c:a", "flac")
    return {
        "delayed": _make(folder, "gap-late.mkv", audio_offset=1.0, gap_at=9.6, gap=1.5, audio_codec=flac),
        "plain": _make(folder, "gap-plain.mkv", gap_at=9.6, gap=1.5, audio_codec=flac),
    }


@pytest.mark.parametrize(
    "key,start",
    # An ordinary file keeps its byte-identical -ss windows: one that spans the seam
    # (8 s here) still loses the gap, as before; windows before or after it are exact.
    [("delayed", 0.0), ("delayed", 8.0), ("delayed", 12.0), ("plain", 0.0), ("plain", 12.0)],
)
def test_windows_after_a_gap_inside_the_track(gapped, tmp_path: Path, key: str, start: float) -> None:
    release = gapped[key]
    audio, error = syncing._extract_audio_segment(
        release.path, start, 8.0, tmp_path / "cache", ffmpeg_path=FFMPEG, force=False
    )
    assert audio is not None, error
    _assert_matches([start + value for value in _onsets(audio)], _starts_within(release, start, start + 8.0))


def test_double_episode_part_b_after_a_gap(gapped, tmp_path: Path) -> None:
    release = gapped["delayed"]
    part = cut_audio_part(FFMPEG, release.path, 8.0, None, tmp_path / "part-b.mkv", ffprobe=FFPROBE)
    _assert_matches([8.0 + value for value in _onsets(part)], _starts_within(release, 8.0, LENGTH + 4))


def test_the_ending_fingerprints_after_a_gap_are_on_the_playback_timeline(
    tmp_path: Path, monkeypatch
) -> None:
    # Over 7 minutes, so the ending region does not start at 0; the speech sits in it,
    # after a 1.5 s seam at 100 s, in a track delayed by 1 s.
    release = _make(
        tmp_path,
        "Show - 05.mkv",
        audio_offset=1.0,
        shift=410.0,
        length=432.0,
        gap_at=100.0,
        gap=1.5,
        audio_codec=("-c:a", "flac"),
    )
    regions: list[tuple[float, object]] = []
    real = intro_skipper.decode_region

    def recording(video, **kwargs):
        samples = real(video, **kwargs)
        regions.append((float(kwargs["start_seconds"]), samples))
        return samples

    monkeypatch.setattr(intro_skipper, "decode_region", recording)
    item = intro_skipper.EpisodeFile(
        path=release.path, episode=5, identity=intro_skipper.identity_of(release.path)
    )
    fps = intro_skipper._fingerprints_for(
        item,
        intro_skipper.IntroSkipperCache(tmp_path / "cache"),
        ffmpeg=FFMPEG,
        ffprobe=FFPROBE,
        allow_compute=True,
        cancel_check=None,
    )
    assert fps is not None
    tail_start, tail = regions[1]
    assert tail_start > 0
    from pudge.audio_fingerprints import SAMPLE_RATE

    _assert_matches(
        [tail_start + value for value in _onsets_of(tail, SAMPLE_RATE)],
        [a for a, _b in release.playback_bursts()],
    )


# --- ordinary files keep their exact command and cache -------------------------------


def test_an_ordinary_file_keeps_its_window_command_and_cache_name(media, tmp_path: Path, monkeypatch) -> None:
    import hashlib

    from pudge import audio_fingerprints

    release = media["mkv_plain"]  # AAC encoder delay only: audio 21 ms before the video
    found = audio_timeline.timeline(release.path, FFPROBE)
    assert found is not None and not found.max_delay() and not found.video_lead()
    commands: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(command, *args, **kwargs):
        commands.append([str(part) for part in command])
        return real_run(command, *args, **kwargs)

    def popen(command, *args, **kwargs):
        commands.append([str(part) for part in command])
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    audio, error = syncing._extract_audio_segment(
        release.path, 0.0, 8.0, tmp_path / "cache", ffmpeg_path=FFMPEG, force=False
    )
    assert audio is not None, error
    stat = release.path.stat()
    old = hashlib.sha1(
        f"{release.path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:0.000:8.000:0.000:mono-16k-flac-v3".encode()
    ).hexdigest()[:20]
    assert audio.name == f"{old}.flac"
    # (run() records the command and its Popen once more)
    extract = next(command for command in commands if "-map" in command and command[-1].endswith(".tmp"))
    assert extract[extract.index("-ss") + 1] == "0.000" and "-af" not in extract
    audio_fingerprints.decode_region(release.path, ffmpeg=FFMPEG, start_seconds=0.0, duration_seconds=5.0)
    assert commands[-1][commands[-1].index("-ss") + 1] == "0.000" and "-af" not in commands[-1]


# --- the rebuild rule ------------------------------------------------------------------


def test_the_rule_covers_audio_first_files_and_skips_manual_unaligned_and_new_results(
    media, tmp_path: Path, monkeypatch
) -> None:
    from t71g08_support import subtitle_library

    from pudge.manager_models import LibraryEpisode

    manager, video, _commands = subtitle_library(tmp_path, monkeypatch)
    manager.config.tools.ffprobe = FFPROBE
    shutil.copyfile(media["mkv_video_late"].path, video)  # audio 0.5 s before the video
    rows = {
        video: ("jimaku", {"engine": "ffsubsync"}),
        "Show - 02.mkv": ("manual", {"engine": "ffsubsync"}),
        "Show - 03.mkv": ("jimaku", {}),
        "Show - 04.mkv": ("jimaku", {"engine": "ffsubsync", "audio_timeline": audio_timeline.VERSION}),
    }
    paths = []
    for number, (name, (source, alignment)) in enumerate(rows.items(), 1):
        path = name if isinstance(name, Path) else (manager.config.library.root_dir / name).resolve()
        if not isinstance(name, Path):
            shutil.copyfile(media["mkv_late"].path, path)
            subtitle = tmp_path / f"{number}.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n古い\n", encoding="utf-8")
            manager.db.upsert_episode(
                LibraryEpisode(
                    media_id=5,
                    title="Show",
                    episode=number,
                    video_path=path,
                    subtitle_path=subtitle,
                    subtitle_origin=source,
                    state="ready",
                )
            )
        manager.db.record_subtitle_history(
            video_path=path,
            media_id=5,
            episode=number,
            source=source,
            candidate_name="old.srt",
            candidate_path=manager.db.episode_by_path(path).subtitle_path,
            status="selected",
            details={"alignment": alignment},
        )
        paths.append(path)

    assert manager._requeue_late_audio_tracks() == 1
    queued = {Path(str(job["video_path"])).resolve() for job in manager.db.subtitle_jobs()}
    assert queued == {video}


def test_new_alignment_results_carry_the_timeline_mark(media, tmp_path: Path, ffsubsync) -> None:
    release = media["mkv_plain"]
    _path, result = syncing.optimize_subtitle(
        release.path,
        release.subtitle(tmp_path),
        tmp_path / "cache",
        _config(engine="ffsubsync", compare_engines=False, segment_validation=False, piecewise_repair=False),
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
    )
    assert result.get("audio_timeline") == audio_timeline.VERSION, result


def test_a_result_made_without_a_probe_carries_no_timeline_mark(
    media, tmp_path: Path, ffsubsync, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ffprobe next to ffmpeg: nothing could be compensated, so the late-audio rule must
    # still look at this episode once ffprobe is available.
    monkeypatch.setattr(syncing, "_media_timeline", lambda video, ffmpeg_path: None)
    release = media["mkv_plain"]
    _path, result = syncing.optimize_subtitle(
        release.path,
        release.subtitle(tmp_path),
        tmp_path / "cache",
        _config(engine="ffsubsync", compare_engines=False, segment_validation=False, piecewise_repair=False),
        ffmpeg_path=FFMPEG,
        ffprobe_path=FFPROBE,
    )
    assert "audio_timeline" not in result, result
