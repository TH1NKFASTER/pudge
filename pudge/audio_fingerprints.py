"""Sequential audio fingerprints for OP/ED detection (PUDGE_IMPLEMENTATION task 6).

Engine: a Philips/Haitsma–Kalker style sub-fingerprint — the same family as
Chromaprint — computed with numpy from PCM that FFmpeg decodes:

* mono 11025 Hz, 4096-sample Hann frames (0.37 s), hop 1102 samples (~0.1 s);
* 33 logarithmic bands 300–2000 Hz; bit ``m`` of frame ``n`` is the sign of
  ``(E[n,m]-E[n,m+1]) - (E[n-1,m]-E[n-1,m+1])``;
* one 32-bit code per hop, with the real time of every code kept.

Why not Chromaprint itself: ``fpcalc`` is not installed with Pudge and the
FFmpeg ``chromaprint`` muxer needs an FFmpeg built with ``--enable-chromaprint``.
numpy is already installed with Pudge's subtitle stack; FFmpeg is the one the
app already uses.  Codes are compared by Hamming distance, so the fingerprint
survives re-encoding, volume changes and small time shifts; it is *not* a
content hash.  Silent frames are flagged and never count as a match.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENGINE_VERSION = "pudge-hk-1"
SAMPLE_RATE = 11025
FRAME_SIZE = 4096
HOP_SIZE = 1102
HOP_SECONDS = HOP_SIZE / SAMPLE_RATE
BAND_COUNT = 33
BAND_LOW_HZ = 300.0
BAND_HIGH_HZ = 2000.0
SILENCE_DB = -55.0


class FingerprintError(RuntimeError):
    pass


def numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


@dataclass(frozen=True)
class AudioFingerprint:
    """Codes of one audio region; ``times[i]`` is the absolute time of code ``i``."""

    codes: Any  # np.ndarray[uint32]
    silent: Any  # np.ndarray[bool]
    start_seconds: float
    duration_seconds: float

    @property
    def size(self) -> int:
        return int(self.codes.shape[0])

    def time_of(self, index: float) -> float:
        # Code i compares frames i and i+1; its centre is frame i+1's centre.
        return self.start_seconds + ((float(index) + 1.0) * HOP_SIZE + FRAME_SIZE / 2.0) / SAMPLE_RATE

    def index_at(self, seconds: float) -> int:
        value = (float(seconds) - self.start_seconds) * SAMPLE_RATE - FRAME_SIZE / 2.0
        return int(round(value / HOP_SIZE - 1.0))


def _band_edges(np: Any) -> Any:
    edges_hz = np.geomspace(BAND_LOW_HZ, BAND_HIGH_HZ, BAND_COUNT + 1)
    return np.clip(np.round(edges_hz * FRAME_SIZE / SAMPLE_RATE).astype(int), 1, FRAME_SIZE // 2)


def fingerprint_samples(samples: Any, *, start_seconds: float = 0.0) -> AudioFingerprint:
    """PCM float samples (mono, SAMPLE_RATE) → fingerprint."""
    import numpy as np

    pcm = np.asarray(samples, dtype=np.float32).reshape(-1)
    frame_count = 0 if pcm.size < FRAME_SIZE else 1 + (pcm.size - FRAME_SIZE) // HOP_SIZE
    if frame_count < 3:
        return AudioFingerprint(
            np.zeros(0, dtype=np.uint32), np.zeros(0, dtype=bool), float(start_seconds),
            pcm.size / SAMPLE_RATE,
        )
    window = np.hanning(FRAME_SIZE).astype(np.float32)
    edges = _band_edges(np)
    energies = np.empty((frame_count, BAND_COUNT), dtype=np.float64)
    loudness = np.empty(frame_count, dtype=np.float64)
    chunk = 512
    for first in range(0, frame_count, chunk):
        last = min(frame_count, first + chunk)
        idx = (np.arange(first, last) * HOP_SIZE)[:, None] + np.arange(FRAME_SIZE)[None, :]
        frames = pcm[idx]
        rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
        loudness[first:last] = 20.0 * np.log10(np.maximum(rms, 1e-9))
        power = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
        cumulative = np.concatenate([np.zeros((power.shape[0], 1)), np.cumsum(power, axis=1)], axis=1)
        energies[first:last] = cumulative[:, edges[1:]] - cumulative[:, edges[:-1]]
    band_diff = energies[:, :-1] - energies[:, 1:]  # (n, 32)
    bits = (band_diff[1:] - band_diff[:-1]) > 0  # (n-1, 32)
    weights = (np.uint32(1) << np.arange(32, dtype=np.uint32)).astype(np.uint32)
    codes = (bits.astype(np.uint32) * weights).sum(axis=1).astype(np.uint32)
    silent = np.maximum(loudness[1:], loudness[:-1]) < SILENCE_DB
    return AudioFingerprint(codes, silent, float(start_seconds), pcm.size / SAMPLE_RATE)


def decode_region(
    video: Path,
    *,
    ffmpeg: str,
    start_seconds: float,
    duration_seconds: float,
    stream_index: int | None = None,
    timeout: float = 180.0,
    cancel_check: Any | None = None,
) -> Any:
    """Decode one region of the audio track to mono float PCM via FFmpeg."""
    import numpy as np

    command = [
        str(ffmpeg), "-nostdin", "-v", "error",
        "-ss", f"{max(0.0, float(start_seconds)):.3f}",
        "-t", f"{max(0.1, float(duration_seconds)):.3f}",
        "-i", str(video),
        "-map", f"0:{int(stream_index)}" if stream_index is not None else "0:a:0",
        "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise FingerprintError(f"ffmpeg not runnable: {exc}") from exc
    try:
        stdout, stderr = _communicate(process, timeout, cancel_check)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if process.returncode != 0:
        raise FingerprintError((stderr or b"").decode("utf-8", "replace").strip()[-500:] or "ffmpeg failed")
    return np.frombuffer(stdout, dtype="<i2").astype(np.float32) / 32768.0


def _communicate(process: subprocess.Popen, timeout: float, cancel_check: Any | None) -> tuple[bytes, bytes]:
    import threading
    import time

    out: dict[str, bytes] = {}

    def reader(name: str, stream: Any) -> None:
        out[name] = stream.read() if stream is not None else b""

    threads = [
        threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + float(timeout)
    while process.poll() is None:
        if callable(cancel_check) and cancel_check():
            process.kill()
            raise FingerprintError("cancelled")
        if time.monotonic() > deadline:
            process.kill()
            raise FingerprintError("ffmpeg timeout")
        time.sleep(0.05)
    for thread in threads:
        thread.join(5)
    return out.get("stdout", b""), out.get("stderr", b"")


def _bit_matrix(np: Any, fp: AudioFingerprint) -> Any:
    bits = ((fp.codes[:, None] >> np.arange(32, dtype=np.uint32)[None, :]) & 1).astype(np.float32)
    signed = bits * 2.0 - 1.0
    signed[fp.silent] = 0.0
    return signed


def candidate_offsets(a: AudioFingerprint, b: AudioFingerprint, *, limit: int = 4) -> list[int]:
    """Frame offsets ``k`` (b index = a index + k) ranked by bit agreement (FFT xcorr)."""
    import numpy as np

    if a.size < 8 or b.size < 8:
        return []
    ma, mb = _bit_matrix(np, a), _bit_matrix(np, b)
    n = 1 << int(np.ceil(np.log2(a.size + b.size)))
    fa = np.fft.rfft(ma, n=n, axis=0)
    fb = np.fft.rfft(mb, n=n, axis=0)
    corr = np.fft.irfft(np.conj(fa) * fb, n=n, axis=0).sum(axis=1)
    # corr[k] = sum_i a[i]·b[i+k] for k>=0; negative k wraps to the end.
    lags = np.concatenate([np.arange(0, b.size), np.arange(-(a.size - 1), 0)])
    values = np.concatenate([corr[: b.size], corr[n - (a.size - 1):]])
    order = np.argsort(values)[::-1]
    picked: list[int] = []
    for position in order:
        lag = int(lags[position])
        if values[position] <= 0:
            break
        if all(abs(lag - other) > 20 for other in picked):  # ~2 s apart
            picked.append(lag)
        if len(picked) >= limit:
            break
    return picked


@dataclass(frozen=True)
class MatchRun:
    a_start: float
    a_end: float
    b_start: float
    b_end: float
    bit_error_rate: float

    @property
    def duration(self) -> float:
        return self.a_end - self.a_start


def matching_runs(
    a: AudioFingerprint,
    b: AudioFingerprint,
    offset: int,
    *,
    max_bit_error: float = 0.33,
    window_seconds: float = 2.0,
    bridge_seconds: float = 1.5,
    min_seconds: float = 5.0,
) -> list[MatchRun]:
    """Stretches where ``a`` and ``b`` (aligned by ``offset``) carry the same audio."""
    import numpy as np

    first_a = max(0, -offset)
    last_a = min(a.size, b.size - offset)
    if last_a - first_a < 8:
        return []
    codes_a = a.codes[first_a:last_a]
    codes_b = b.codes[first_a + offset:last_a + offset]
    xor = np.bitwise_xor(codes_a, codes_b)
    distance = np.zeros(xor.shape[0], dtype=np.float64)
    for shift in range(32):
        distance += ((xor >> np.uint32(shift)) & np.uint32(1)).astype(np.float64)
    ber = distance / 32.0
    valid = ~(a.silent[first_a:last_a] | b.silent[first_a + offset:last_a + offset])
    width = max(3, int(round(window_seconds / HOP_SECONDS)))
    kernel = np.ones(width)
    sums = np.convolve(np.where(valid, ber, 0.0), kernel, mode="same")
    counts = np.convolve(valid.astype(np.float64), kernel, mode="same")
    smooth = np.where(counts >= width * 0.5, sums / np.maximum(counts, 1.0), 1.0)
    good = smooth <= max_bit_error
    # Silence between two good stretches (e.g. a pause in the OP) is bridged.
    bridge = int(round(bridge_seconds / HOP_SECONDS))
    runs: list[list[int]] = []
    cursor = 0
    size = good.shape[0]
    while cursor < size:
        if not good[cursor]:
            cursor += 1
            continue
        start = cursor
        while cursor < size and good[cursor]:
            cursor += 1
        if runs and start - runs[-1][1] <= bridge:
            runs[-1][1] = cursor
        else:
            runs.append([start, cursor])
    result: list[MatchRun] = []
    for start, end in runs:
        # Tighten edges to the first/last individually matching frame, so the
        # smoothing window does not widen the segment by ±1 s.
        local = np.where(valid[start:end] & (ber[start:end] <= max_bit_error + 0.05))[0]
        if local.size == 0:
            continue
        start, end = start + int(local[0]), start + int(local[-1]) + 1
        if (end - start) * HOP_SECONDS < min_seconds:
            continue
        rate = float(np.mean(ber[start:end][valid[start:end]])) if valid[start:end].any() else 1.0
        ia, ib = first_a + start, first_a + end - 1
        result.append(
            MatchRun(
                a_start=a.time_of(ia) - HOP_SECONDS / 2,
                a_end=a.time_of(ib) + HOP_SECONDS / 2,
                b_start=b.time_of(ia + offset) - HOP_SECONDS / 2,
                b_end=b.time_of(ib + offset) + HOP_SECONDS / 2,
                bit_error_rate=round(rate, 4),
            )
        )
    return result


def best_shared_run(
    a: AudioFingerprint,
    b: AudioFingerprint,
    *,
    min_seconds: float,
    max_seconds: float,
    max_bit_error: float = 0.33,
) -> MatchRun | None:
    """Longest shared stretch of a plausible OP/ED length (None when there is none)."""
    best: MatchRun | None = None
    for offset in candidate_offsets(a, b):
        for run in matching_runs(a, b, offset, max_bit_error=max_bit_error, min_seconds=min_seconds):
            if not (min_seconds <= run.duration <= max_seconds):
                continue
            if best is None or run.duration > best.duration or (
                abs(run.duration - best.duration) < 0.5 and run.bit_error_rate < best.bit_error_rate
            ):
                best = run
    return best


def save_fingerprint(path: Path, fp: AudioFingerprint, *, meta: dict[str, Any]) -> None:
    import json
    import os
    import tempfile

    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fp.", suffix=".npz", dir=str(path.parent))
    os.close(fd)
    try:
        np.savez_compressed(
            tmp, codes=fp.codes, silent=fp.silent,
            header=np.array([fp.start_seconds, fp.duration_seconds], dtype=np.float64),
            meta=np.array(json.dumps({**meta, "engine": ENGINE_VERSION}, sort_keys=True)),
        )
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_fingerprint(path: Path) -> tuple[AudioFingerprint, dict[str, Any]] | None:
    import json

    import numpy as np

    try:
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"]))
            if meta.get("engine") != ENGINE_VERSION:
                return None
            header = data["header"]
            return (
                AudioFingerprint(
                    data["codes"].astype(np.uint32), data["silent"].astype(bool),
                    float(header[0]), float(header[1]),
                ),
                meta,
            )
    except (OSError, ValueError, KeyError, TypeError):
        return None
