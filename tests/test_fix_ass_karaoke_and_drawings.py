"""ASS karaoke effects and vector drawings in ASS -> SRT conversion (review SYNC-6).

A karaoke template emits one positioned event per syllable (Effect "fx"): the
geometry path grouped events only by exact (start, end), so every syllable
became its own overlapping cue. The ffmpeg path kept ``{\\p1}`` drawings as
cues such as "m 0 0 l 100 0 100 100 0 100". Both are now removed before either
conversion path (and the Python fallback) sees the script.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pudge.subtitle_formats import _sanitize_ass_events, convert_to_plain_srt, parse_srt

HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: OP,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,8,10,10,10,1
Style: Sign,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,8,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
DIALOGUE = [
    f"Dialogue: 0,0:00:{30 + k * 3:05.2f},0:00:{32 + k * 3:05.2f},Default,,0,0,0,,おはようございます{k}"
    for k in range(10)
]
EXPECTED = [(30.0 + k * 3, 32.0 + k * 3, f"おはようございます{k}") for k in range(10)]


def _karaoke_events() -> list[str]:
    events = []
    syllables = ["き", "み", "の", "こ", "え", "が"]
    for line in range(4):
        base = 1.0 + line * 4
        events.append(
            f"Comment: 0,0:00:{base:05.2f},0:00:{base + 4:05.2f},OP,,0,0,0,karaoke,"
            + "".join(f"{{\\k30}}{s}" for s in syllables)
        )
        for k, syllable in enumerate(syllables):
            start = base + k * 0.3
            x = 800 + k * 50
            events.append(
                f"Dialogue: 1,0:00:{start:05.2f},0:00:{start + 0.3:05.2f},OP,,0,0,0,fx,"
                f"{{\\an5\\pos({x},80)\\t(\\fscx120)}}{syllable}"
            )
            events.append(
                f"Dialogue: 0,0:00:{base:05.2f},0:00:{start:05.2f},OP,,0,0,0,fx,{{\\an5\\pos({x},80)}}{syllable}"
            )
    return events


DRAWINGS = [
    "Dialogue: 0,0:00:20.00,0:00:24.00,Sign,,0,0,0,,{\\p1}m 0 0 l 100 0 100 100 0 100{\\p0}",
    "Dialogue: 0,0:00:25.00,0:00:28.00,Sign,,0,0,0,,{\\an7\\p2}m 0 0 b 10 10 20 20 30 30",
]


def _write(tmp_path: Path, name: str, events: list[str]) -> Path:
    path = tmp_path / name
    path.write_text(HEAD + "\n".join(events) + "\n", encoding="utf-8")
    return path


def test_karaoke_fx_rows_do_not_become_syllable_cues(tmp_path: Path) -> None:
    # Positioned fx events select the geometry path.
    subtitle = _write(tmp_path, "karaoke.ja.ass", _karaoke_events() + DRAWINGS[1:] + DIALOGUE)
    output, info = convert_to_plain_srt(subtitle, tmp_path / "cache", force=True)
    assert info["reason"] == "converted"
    assert parse_srt(output) == EXPECTED


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_drawings_do_not_reach_the_ffmpeg_conversion(tmp_path: Path) -> None:
    subtitle = _write(tmp_path, "drawings.ja.ass", DRAWINGS + DIALOGUE)
    output, info = convert_to_plain_srt(subtitle, tmp_path / "cache", force=True)
    assert info["method"] == "ffmpeg"
    assert parse_srt(output) == EXPECTED


def test_drawings_do_not_reach_the_python_fallback(tmp_path: Path) -> None:
    subtitle = _write(tmp_path, "drawings.ja.ass", DRAWINGS + _karaoke_events() + DIALOGUE)
    output, info = convert_to_plain_srt(
        subtitle, tmp_path / "cache", ffmpeg_path=str(tmp_path / "no-ffmpeg"), force=True
    )
    assert info["reason"] == "converted"
    assert parse_srt(output) == EXPECTED


def test_text_after_a_drawing_is_kept() -> None:
    text = HEAD + "Dialogue: 0,0:00:01.00,0:00:02.00,Sign,,0,0,0,,{\\p1}m 0 0 l 1 1{\\p0}看板です\n"
    sanitized, removed = _sanitize_ass_events(text)
    assert removed == {"fx_events_removed": 0, "drawings_removed": 1}
    assert sanitized.splitlines()[-1] == "Dialogue: 0,0:00:01.00,0:00:02.00,Sign,,0,0,0,,{}{}看板です"
    # \pos and \pbo are not drawing tags; ordinary karaoke lines (no fx) stay.
    keep = HEAD + "Dialogue: 0,0:00:01.00,0:00:05.00,OP,,0,0,0,,{\\pos(10,10)\\pbo2\\k30}き{\\k30}み\n"
    assert _sanitize_ass_events(keep) == (keep, {"fx_events_removed": 0, "drawings_removed": 0})
