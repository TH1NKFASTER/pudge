from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def merge_activity_regions(
    regions: Iterable[dict[str, Any]],
    *,
    bridge_seconds: float = 0.12,
) -> list[dict[str, float]]:
    """Normalize and merge neighboring speech regions."""

    normalized: list[tuple[float, float]] = []
    for row in regions:
        if not isinstance(row, dict):
            continue
        try:
            start = max(0.0, float(row.get("start") or 0.0))
            end = max(start, float(row.get("end") or start))
        except (TypeError, ValueError):
            continue
        if end - start >= 0.02:
            normalized.append((start, end))
    normalized.sort()
    merged: list[list[float]] = []
    for start, end in normalized:
        if merged and start <= merged[-1][1] + max(0.0, float(bridge_seconds)):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [
        {"start": round(start, 3), "end": round(end, 3)}
        for start, end in merged
    ]


def gate_activity_regions(
    regions: Iterable[dict[str, Any]],
    speech_hints: Iterable[dict[str, Any]],
    *,
    padding_seconds: float = 0.30,
) -> list[dict[str, float]]:
    """Discard energetic music/noise that is not near an STT speech hint."""

    padded: list[dict[str, float]] = []
    for row in speech_hints:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start") or 0.0)
            end = float(row.get("end") or start)
        except (TypeError, ValueError):
            continue
        padded.append(
            {
                "start": max(0.0, start - padding_seconds),
                "end": end + padding_seconds,
            }
        )
    gates = merge_activity_regions(padded, bridge_seconds=0.0)
    if not gates:
        return merge_activity_regions(regions, bridge_seconds=0.0)
    gated: list[dict[str, float]] = []
    for region in regions:
        try:
            region_start = float(region.get("start") or 0.0)
            region_end = float(region.get("end") or region_start)
        except (AttributeError, TypeError, ValueError):
            continue
        for gate in gates:
            start = max(region_start, float(gate["start"]))
            end = min(region_end, float(gate["end"]))
            if end - start >= 0.02:
                gated.append({"start": start, "end": end})
    return merge_activity_regions(gated, bridge_seconds=0.0)


def activity_regions_from_features(
    energy_db: Any,
    spectral_flux: Any,
    *,
    frame_seconds: float = 0.02,
) -> tuple[list[dict[str, float]], dict[str, float]]:
    """Turn short-time FFT features into stable speech regions.

    Energy carries ordinary voiced speech. Positive spectral flux recovers soft
    consonant onsets that sit just below the adaptive energy threshold. Short
    gaps are bridged inside a phrase, while sentence-sized pauses remain gaps.
    """

    import numpy as np

    energy = np.asarray(energy_db, dtype=np.float32).reshape(-1)
    flux = np.asarray(spectral_flux, dtype=np.float32).reshape(-1)
    count = min(int(energy.size), int(flux.size))
    if count < 2:
        return [], {"noise_floor_db": -80.0, "speech_threshold_db": -40.0}
    energy = np.nan_to_num(energy[:count], nan=-120.0, neginf=-120.0, posinf=0.0)
    flux = np.nan_to_num(flux[:count], nan=0.0, neginf=0.0, posinf=0.0)

    noise_floor = float(np.percentile(energy, 18))
    speech_level = float(np.percentile(energy, 78))
    spread = max(0.0, speech_level - noise_floor)
    margin = max(3.0, min(12.0, spread * 0.38))
    threshold = min(speech_level - 1.0, noise_floor + margin)
    threshold = max(-62.0, min(-24.0, threshold))
    flux_threshold = float(np.percentile(flux, 84))

    active = energy >= threshold
    if flux_threshold > 0:
        onset = (flux >= flux_threshold) & (energy >= threshold - 6.0)
        # Include one frame before and two frames after a spectral onset.
        active |= np.convolve(onset.astype(np.int8), np.ones(4, dtype=np.int8), mode="same") > 0

    # Close only sub-word gaps. A sentence pause must remain visible so the
    # reader highlight can wait for the next real utterance.
    max_gap = max(1, round(0.10 / max(0.005, float(frame_seconds))))
    cursor = 0
    while cursor < count:
        if active[cursor]:
            cursor += 1
            continue
        gap_start = cursor
        while cursor < count and not active[cursor]:
            cursor += 1
        if gap_start > 0 and cursor < count and cursor - gap_start <= max_gap:
            active[gap_start:cursor] = True

    minimum_frames = max(2, round(0.08 / max(0.005, float(frame_seconds))))
    regions: list[dict[str, float]] = []
    cursor = 0
    while cursor < count:
        if not active[cursor]:
            cursor += 1
            continue
        start = cursor
        while cursor < count and active[cursor]:
            cursor += 1
        if cursor - start < minimum_frames:
            continue
        regions.append(
            {
                "start": round(max(0.0, (start - 1) * frame_seconds), 3),
                "end": round((cursor + 1) * frame_seconds, 3),
            }
        )
    return merge_activity_regions(regions, bridge_seconds=0.0), {
        "noise_floor_db": round(noise_floor, 3),
        "speech_level_db": round(speech_level, 3),
        "speech_threshold_db": round(threshold, 3),
        "spectral_flux_threshold": round(flux_threshold, 6),
    }


def analyze_audio_activity(
    source: Path,
    *,
    ffmpeg: str,
    sample_rate: int = 8_000,
) -> dict[str, Any]:
    """Decode a low-rate waveform and build an energy/FFT speech clock."""

    import numpy as np

    frame_size = 256
    hop_size = 160
    frame_seconds = hop_size / float(sample_rate)
    command = [
        str(ffmpeg),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(Path(source)),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("ffmpeg did not expose the audiobook waveform")

    window = np.hanning(frame_size).astype(np.float32)
    buffer = np.empty(0, dtype=np.float32)
    energies: list[Any] = []
    fluxes: list[Any] = []
    previous_spectrum: Any = None
    pending_byte = b""
    try:
        while True:
            raw = process.stdout.read(1024 * 1024)
            if not raw:
                break
            raw = pending_byte + raw
            if len(raw) % 2:
                pending_byte, raw = raw[-1:], raw[:-1]
            else:
                pending_byte = b""
            if not raw:
                continue
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            buffer = np.concatenate((buffer, samples))
            while buffer.size >= frame_size:
                available = 1 + (int(buffer.size) - frame_size) // hop_size
                take = min(4096, available)
                frames = np.lib.stride_tricks.sliding_window_view(buffer, frame_size)[
                    : take * hop_size : hop_size
                ]
                rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
                energies.append((20.0 * np.log10(rms + 1e-9)).astype(np.float32))
                spectrum = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
                prior = np.vstack((previous_spectrum, spectrum[:-1])) if previous_spectrum is not None else np.vstack((spectrum[:1], spectrum[:-1]))
                positive_change = np.maximum(0.0, spectrum - prior)
                flux = positive_change.sum(axis=1) / (spectrum.sum(axis=1) + 1e-6)
                fluxes.append(flux.astype(np.float32))
                previous_spectrum = spectrum[-1:]
                buffer = buffer[take * hop_size :]
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        returncode = process.wait(timeout=30)
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise
    if returncode != 0:
        raise RuntimeError(stderr.strip()[-1200:] or "ffmpeg could not analyze audiobook audio")
    if not energies:
        raise ValueError("Audiobook waveform contained no readable audio")

    energy = np.concatenate(energies)
    flux = np.concatenate(fluxes)
    regions, diagnostics = activity_regions_from_features(
        energy,
        flux,
        frame_seconds=frame_seconds,
    )
    return {
        "schema": "audio-activity-v1",
        "sample_rate": int(sample_rate),
        "frame_seconds": frame_seconds,
        "frame_count": int(energy.size),
        "regions": regions,
        **diagnostics,
    }
