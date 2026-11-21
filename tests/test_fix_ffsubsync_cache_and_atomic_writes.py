"""One ffsubsync run per candidate, and cached outputs written only when accepted.

SYNC-12: on the audio path the candidate check (tag "candidate-eval", engine
"ffsubsync") and the later full run (tag "ffsubsync", engine "auto") ran ffsubsync
twice with identical arguments; their cache keys differed only in fields that do
not reach ffsubsync. They now share one entry (also within a forced run).

SYNC-13: ``retime_sup`` wrote straight to the PGS cache path before the
acceptance check (any file there is later served as a cached success), and
``stabilize_music_marker_pairs`` rewrote cached outputs in place before the
guard's verdict. Both now write a side file and move it into place atomically,
and only once accepted.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from pudge import pgs, syncing
from pudge.alignment_guard import stabilize_music_marker_pairs
from pudge.config import SyncConfig
from pudge.subtitle_formats import parse_srt
from tests.test_fix_reference_extraction_and_audio_streams import _FakeFfsubsync

SRT = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n"


def test_candidate_check_and_full_run_share_one_ffsubsync_result(tmp_path: Path, monkeypatch) -> None:
    fake = _FakeFfsubsync()
    fake.install(monkeypatch)
    monkeypatch.setattr(syncing, "_japanese_audio_stream_spec", lambda *_a, **_k: None)
    monkeypatch.setattr(syncing, "_SYNC_OUTPUTS_FROM_THIS_PROCESS", set())
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"v")
    subtitle = tmp_path / "Show - 05.ja.srt"
    subtitle.write_text(SRT, encoding="utf-8")
    reference = tmp_path / "speech.npz"
    reference.write_bytes(b"npz")
    cache = tmp_path / "cache"
    config = SyncConfig()
    assert config.engine == "auto" and config.compare_engines is True
    evaluation_config = replace(config, engine="ffsubsync", compare_engines=False)

    first, first_result = syncing.synchronize_subtitle(
        video, subtitle, cache, evaluation_config, tag="candidate-eval", reference=reference
    )
    second, second_result = syncing.synchronize_subtitle(
        video, subtitle, cache, config, tag="ffsubsync", reference=reference
    )
    assert len(fake.runs) == 1
    assert first == second and second_result["reason"] == "cached"
    assert second_result["offset_seconds"] == first_result["offset_seconds"]

    # A forced (Fresh) preparation in a new process: recomputed once, then shared.
    monkeypatch.setattr(syncing, "_SYNC_OUTPUTS_FROM_THIS_PROCESS", set())
    syncing.synchronize_subtitle(
        video, subtitle, cache, evaluation_config, tag="candidate-eval", reference=reference, force=True
    )
    syncing.synchronize_subtitle(
        video, subtitle, cache, config, tag="ffsubsync", reference=reference, force=True
    )
    assert len(fake.runs) == 2
    # Other ffsubsync steps keep their own entries.
    syncing.synchronize_subtitle(video, subtitle, cache, config, tag="alass-finish", reference=reference)
    assert len(fake.runs) == 3


def _sup(path: Path, starts: list[float]) -> None:
    data = bytearray()
    for start in starts:
        pts = int(start * 90_000)
        data += (
            b"PG" + pts.to_bytes(4, "big") + (0).to_bytes(4, "big") + bytes([0x16]) + (0).to_bytes(2, "big")
        )
    path.write_bytes(bytes(data))


@pytest.mark.parametrize("accepted", [False, True])
def test_retimed_sup_reaches_the_cache_only_when_accepted(
    tmp_path: Path, monkeypatch, accepted: bool
) -> None:
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"v")
    subtitle = tmp_path / "Show - 05.ja.sup"
    starts = [10.0 + 5 * k for k in range(10)]
    _sup(subtitle, starts)
    reference = tmp_path / "reference.srt"
    reference.write_text(
        "\n\n".join(
            f"{k + 1}\n00:00:{11 + 5 * k:02d},000 --> 00:00:{12 + 5 * k:02d},000\nLine" for k in range(10)
        )
        + "\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        syncing, "extract_embedded_timing_reference", lambda *_a, **_k: (reference, {"reason": "applied"})
    )
    monkeypatch.setattr(syncing, "parse_pgs_cues", lambda _path: [(s, s + 1.0) for s in starts])
    monkeypatch.setattr(syncing, "onset_times", lambda cues: [start for start, _end in cues])

    def alass(reference_pulses, source_pulses, *_a, **_k):
        aligned = tmp_path / "aligned.srt"
        aligned.write_text(
            "\n\n".join(
                f"{k + 1}\n00:00:{11 + 5 * k:02d},000 --> 00:00:{11 + 5 * k:02d},400\nPGS" for k in range(10)
            )
            + "\n",
            encoding="utf-8",
        )
        return aligned, {"sync_was_successful": True}

    monkeypatch.setattr(syncing, "synchronize_with_alass", alass)
    seen_at_check: list[list[str]] = []

    def onset_score(_starts, _reference, *, tolerance):
        visible = sorted(
            path.name for path in (cache / "pgs-onset").glob("*.sup") if not path.name.startswith(".")
        )
        seen_at_check.append(visible)
        return {"coverage": 0.9 if accepted and len(seen_at_check) == 2 else 0.1}

    monkeypatch.setattr(syncing, "onset_match_score", onset_score)
    output, result = syncing.synchronize_pgs_with_embedded_reference(video, subtitle, cache, SyncConfig())
    # Nothing sat at the cached path while the result was being judged.
    assert seen_at_check == [[], []]
    leftovers = sorted(path.name for path in (cache / "pgs-onset").glob("*.sup"))
    if accepted:
        assert result["reason"] == "applied" and output.parent == cache / "pgs-onset"
        assert leftovers == [output.name]
        first = next(iter(pgs.iter_pgs_segments(output.read_bytes())))
        assert first.pts / 90_000 == pytest.approx(11.0, abs=0.01)
    else:
        assert result["reason"] == "pgs_onset_no_improvement" and output == subtitle
        assert leftovers == []


def test_retime_sup_replaces_the_destination_atomically(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "in.sup"
    _sup(source, [1.0, 2.0])
    destination = tmp_path / "out.sup"
    destination.write_bytes(b"previous")

    def broken_replace(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(pgs.os, "replace", broken_replace)
    with pytest.raises(OSError):
        pgs.retime_sup(source, destination, lambda value: value + 1.0)
    assert destination.read_bytes() == b"previous"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["in.sup", "out.sup"]


RAW = (
    "1\n00:00:01,418 --> 00:00:03,420\n♪～\n\n"
    "2\n00:01:27,587 --> 00:01:29,589\n～♪\n\n"
    "3\n00:01:31,591 --> 00:01:33,468\nここから先は通さない\n\n"
    "4\n00:01:34,000 --> 00:01:35,000\n次の台詞\n"
)
FINAL = (
    "1\n00:00:11,230 --> 00:00:13,232\n♪～\n\n"
    "2\n00:01:27,485 --> 00:01:29,487\n～♪\n\n"
    "3\n00:01:31,489 --> 00:01:33,366\nここから先は通さない\n\n"
    "4\n00:01:33,898 --> 00:01:34,898\n次の台詞\n"
)


def test_music_marker_repair_can_go_to_a_side_file(tmp_path: Path) -> None:
    raw, final, side = tmp_path / "raw.srt", tmp_path / "final.srt", tmp_path / "side.srt"
    raw.write_text(RAW, encoding="utf-8")
    final.write_text(FINAL, encoding="utf-8")
    result = stabilize_music_marker_pairs(raw, final, cache_dir=tmp_path, destination=side)
    assert result["applied"] is True
    assert final.read_text(encoding="utf-8") == FINAL
    assert parse_srt(side)[0][0] == pytest.approx(1.316, abs=0.001)


@pytest.mark.parametrize("status", ["rejected", "accepted"])
def test_guard_replaces_a_cached_output_only_after_its_verdict(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    raw, output = tmp_path / "raw.srt", tmp_path / "cache" / "synced" / "abc.srt"
    output.parent.mkdir(parents=True)
    raw.write_text(RAW, encoding="utf-8")
    output.write_text(FINAL, encoding="utf-8")
    checked: list[Path] = []

    def verdict(_raw, final, *, cache_dir=None):
        checked.append(Path(final))
        assert Path(final) != output and parse_srt(Path(final))[0][0] == pytest.approx(1.316, abs=0.001)
        return {"status": status, "reason": "test", "final": str(final)}

    monkeypatch.setattr(syncing, "validate_alignment_output", verdict)
    result = syncing._guard_alignment_result(raw, output, {"sync_was_successful": True}, tmp_path / "cache")
    assert len(checked) == 1
    assert result["alignment_output_guard"]["final"] == str(output)
    assert sorted(os.listdir(output.parent)) == ["abc.srt"]
    if status == "rejected":
        assert result["sync_was_successful"] is False
        assert output.read_text(encoding="utf-8") == FINAL
    else:
        assert result["sync_was_successful"] is True
        assert parse_srt(output)[0][0] == pytest.approx(1.316, abs=0.001)
