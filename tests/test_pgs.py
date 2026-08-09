from __future__ import annotations

from pathlib import Path

import pytest

from anime_mpv.pgs import (
    PGS_CLOCK,
    build_time_mapper,
    onset_match_score,
    onset_times,
    parse_pgs_cues,
    retime_sup,
)


def _packet(seconds: float, segment_type: int, payload: bytes) -> bytes:
    pts = int(round(seconds * PGS_CLOCK))
    return (
        b"PG"
        + pts.to_bytes(4, "big")
        + pts.to_bytes(4, "big")
        + bytes([segment_type])
        + len(payload).to_bytes(2, "big")
        + payload
    )


def _pcs(seconds: float, visible: bool) -> bytes:
    payload = bytearray(11)
    payload[0:2] = (1920).to_bytes(2, "big")
    payload[2:4] = (1080).to_bytes(2, "big")
    payload[10] = 1 if visible else 0
    return _packet(seconds, 0x16, bytes(payload))


def _sup(path: Path, starts: list[float], duration: float = 1.0) -> Path:
    chunks: list[bytes] = []
    for start in starts:
        chunks.append(_pcs(start, True))
        chunks.append(_pcs(start + duration, False))
    path.write_bytes(b"".join(chunks))
    return path


def test_parse_pgs_cues_and_onsets(tmp_path: Path) -> None:
    source = _sup(tmp_path / "input.sup", [10.0, 20.0, 30.0])
    cues = parse_pgs_cues(source)
    assert [(cue.start, cue.end) for cue in cues] == [
        (10.0, 11.0),
        (20.0, 21.0),
        (30.0, 31.0),
    ]
    assert onset_times(cues) == [10.0, 20.0, 30.0]


def test_retime_sup_with_piecewise_mapper(tmp_path: Path) -> None:
    source = _sup(tmp_path / "input.sup", [10.0, 20.0, 30.0])
    destination = tmp_path / "output.sup"
    mapper = build_time_mapper([(10.0, 15.0), (20.0, 25.0), (30.0, 37.0)])
    retime_sup(source, destination, mapper)
    starts = onset_times(parse_pgs_cues(destination))
    assert starts == pytest.approx([15.0, 25.0, 37.0], abs=0.001)


def test_onset_match_score_tolerates_extra_pgs_events() -> None:
    result = onset_match_score(
        [10.1, 15.0, 20.2, 30.0, 40.1],
        [10.0, 20.0, 30.0, 40.0],
        tolerance=0.3,
    )
    assert result["matched"] == 4
    assert result["coverage"] == 1.0


def test_sync_pgs_against_embedded_reference(monkeypatch, tmp_path: Path) -> None:
    from anime_mpv.config import SyncConfig
    from anime_mpv import syncing
    from anime_mpv.subtitle_formats import parse_srt, write_srt

    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")
    source = _sup(tmp_path / "input.sup", [10, 20, 30, 40, 50, 60, 70, 80])
    reference = tmp_path / "english.srt"
    write_srt(
        [(start, start + 1.0, f"line-{index}") for index, start in enumerate([15, 25, 35, 45, 55, 65, 75, 85])],
        reference,
    )

    monkeypatch.setattr(
        syncing,
        "extract_embedded_timing_reference",
        lambda *args, **kwargs: (
            reference,
            {"reason": "applied", "language": "eng", "title": "CR"},
        ),
    )

    def fake_alass(ref, pulse_source, cache_dir, config, **kwargs):
        cues = parse_srt(pulse_source)
        output = tmp_path / "aligned.srt"
        write_srt([(start + 5, end + 5, text) for start, end, text in cues], output)
        return output, {"reason": "applied", "sync_was_successful": True}

    monkeypatch.setattr(syncing, "synchronize_with_alass", fake_alass)
    output, result = syncing.synchronize_pgs_with_embedded_reference(
        video,
        source,
        tmp_path / "cache",
        SyncConfig(),
        force=True,
    )
    assert result["sync_was_successful"] is True
    assert result["engine"] == "pgs-onset+alass"
    assert onset_times(parse_pgs_cues(output)) == pytest.approx(
        [15, 25, 35, 45, 55, 65, 75, 85], abs=0.001
    )
