"""The STT text clock's plateaus win over an ALASS map that only a coarse split penalty made safe.

Japanese speech checks an early edit: the finest ALASS map (the configured split penalty)
is unsafe, the coarser one passes as a compromise between the episode's clocks, while the
text clock found constant clocks whose jumps the source's pauses and the text support.
The plateau map is used (before: the coarse ALASS map, 76 % of lines within 0.3 s instead
of all). A safe map at the configured penalty is still used as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge import syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import write_srt


@pytest.fixture
def fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reference = tmp_path / "reference.ja.srt"
    source = tmp_path / "source.srt"
    plateau = tmp_path / "plateaus.srt"
    for path in (reference, source, plateau):
        write_srt([(float(i * 5 + 1), float(i * 5 + 2), f"l{i}") for i in range(12)], path)
    monkeypatch.setattr(syncing, "prepare_japanese_stt_reference", lambda *_a, **_k: (reference, {}))
    monkeypatch.setattr(
        syncing,
        "_stt_text_clock_candidate",
        lambda *_a, **_k: (
            None,
            {
                "accepted": False,
                "reason": "text_clock_safety_gate_failed",
                "plateau_clock": {"accepted": True, "output": str(plateau), "activity": {"weighted": 0.8}},
            },
        ),
    )
    penalties: list[float] = []

    def fake_alass(_reference, subtitle, _cache, config, **_kwargs):
        penalties.append(float(config.alass_split_penalty))
        out = tmp_path / f"alass-{config.alass_split_penalty}.srt"
        write_srt([(1.0, 2.0, "x"), (6.0, 7.0, "y")], out)
        return out, {"reason": "applied", "sync_was_successful": True, "split": config.alass_split_penalty}

    monkeypatch.setattr(syncing, "synchronize_with_alass", fake_alass)
    monkeypatch.setattr(
        syncing, "compare_timing_activity", lambda *_a, **_k: {"available": True, "weighted": 0.9}
    )
    monkeypatch.setattr(syncing, "_stt_alass_transition_safety", lambda *_a, **_k: {"accepted": True})
    monkeypatch.setattr(
        syncing, "_refine_stt_opening_plateaus", lambda _s, aligned, *_a, **_k: (aligned, {"applied": False})
    )

    def run(safe_penalties: set[float]):
        monkeypatch.setattr(
            syncing,
            "_stt_alass_map_safe",
            lambda result, _safety: (
                (True, "ok")
                if float(result["split"]) in safe_penalties
                else (False, "stt_alass_fragmented_large_edit")
            ),
        )
        penalties.clear()
        output, result = syncing._try_japanese_stt_fallback(
            tmp_path / "video.mkv",
            source,
            tmp_path / "cache",
            SyncConfig(),
            ffmpeg_path="ffmpeg",
            ffprobe_path="ffprobe",
            alass_path="alass",
            verbose=False,
        )
        return output, result, list(penalties)

    return run, plateau


def test_plateau_clock_beats_a_map_only_a_coarse_split_penalty_made_safe(fallback) -> None:
    run, plateau = fallback
    output, result, penalties = run({18.0, 35.0})
    assert output == plateau
    assert result["engine"] == "japanese-stt+text-clock-plateaus"
    assert result["sync_was_successful"] is True
    assert penalties == [7.0, 18.0]  # stops at the coarse map, no third ALASS run
    assert result["stt_alass_attempts"][-1]["superseded_by"] == "stt_text_clock_plateaus"


def test_safe_map_at_the_configured_penalty_is_still_used(fallback) -> None:
    run, plateau = fallback
    output, result, penalties = run({7.0})
    assert output != plateau
    assert result["engine"] == "japanese-stt+alass"
    assert penalties == [7.0]
