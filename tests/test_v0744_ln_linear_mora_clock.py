from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pudge.reading_audio_alignment import (
    audio_position_for_light_novel,
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def _alignment() -> dict[str, object]:
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "normalized_length": 100,
                "start": 10.0,
                "end": 20.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 40, "time": 14.0},
                    {"offset": 100, "time": 20.0},
                ],
                "speech_regions": [
                    {"start": 10.0, "end": 10.5},
                    {"start": 13.5, "end": 14.0},
                    {"start": 19.0, "end": 20.0},
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (10.0, 0.0),
        (11.0, 10.0),
        (12.0, 20.0),
        (13.0, 30.0),
        (14.0, 40.0),
        (17.0, 70.0),
        (20.0, 100.0),
    ],
)
def test_audio_to_text_is_linear_between_anchors(
    position: float,
    expected: float,
) -> None:
    result = light_novel_position_for_audio(_alignment(), position)

    assert result is not None
    assert result["chapter_char_offset_exact"] == expected


@pytest.mark.parametrize("offset", [0, 10, 20, 30, 40, 55, 70, 100])
def test_text_to_audio_is_inverse_of_linear_anchor_clock(offset: int) -> None:
    alignment = _alignment()
    position = audio_position_for_light_novel_offset(
        alignment,
        1,
        offset,
    )
    assert position is not None

    restored = light_novel_position_for_audio(alignment, position)
    assert restored is not None
    assert restored["chapter_char_offset_exact"] == pytest.approx(
        float(offset),
        abs=0.0001,
    )


def test_progress_seek_uses_the_same_linear_clock() -> None:
    assert audio_position_for_light_novel(_alignment(), 1, 0.2) == 12.0
    assert audio_position_for_light_novel(_alignment(), 1, 0.7) == 17.0


def test_frontend_uses_wall_clock_ratio_and_immediate_word_switch() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedAnchorPath(anchor)" in source
    assert "for(let index=0;index<path.length-1;index++)" in source
    assert "speechActive=lnPairedSpeechActive(anchor,estimatedTime)" not in source
    assert "speechActive=lnPairedSpeechActive(anchor,position)" not in source
    assert "until:performance.now()+34" not in source
    assert "ui.lnPairedResumeTarget!==null" in source
    assert "ui.lnPairedResumeTarget!==undefined" in source


def test_inline_javascript_parses() -> None:
    source = HTML.read_text(encoding="utf-8")
    scripts = [
        match.group(2)
        for match in re.finditer(
            r"<script([^>]*)>(.*?)</script>",
            source,
            flags=re.I | re.S,
        )
        if not re.search(r"\bsrc\s*=", match.group(1), flags=re.I)
    ]
    assert scripts

    for index, script in enumerate(scripts):
        path = ROOT / f".pudge-v0744-linear-clock-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(
                ["node", "--check", str(path)],
                cwd=ROOT,
                check=True,
            )
        finally:
            path.unlink(missing_ok=True)
