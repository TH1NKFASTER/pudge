"""Two tests that failed only on GitHub's macOS runners (the v0.8.1 CI run).

- Background FFmpeg runs with nice 15 on top of Pudge's own niceness. The runner started the
  tests below 0, so FFmpeg there ran at 5 and a test that wanted 15 or more failed; the tests
  compare with Pudge's own niceness now (tests/test_fix_intro_detection_paused_mpv.py).
- The phone's manga spreads equal the Mac's apart from the alignment, which a background pass
  works on meanwhile: its pending count can drop between two answers
  (tests/test_fix_manga_two_page_view.py).
"""

from __future__ import annotations

import os
from pathlib import Path

from pudge import audio_fingerprints


def test_background_ffmpeg_is_fifteen_nicer_than_pudge(tmp_path: Path) -> None:
    out = tmp_path / "nice.txt"
    ffmpeg = tmp_path / "ffmpeg"
    # nice execs FFmpeg in place, so FFmpeg's parent is Pudge itself.
    ffmpeg.write_text(f'#!/bin/sh\nps -o nice= -p $$ > "{out}"\nps -o nice= -p $PPID >> "{out}"\n')
    ffmpeg.chmod(0o755)
    samples = audio_fingerprints.decode_region(
        tmp_path / "x.mkv", ffmpeg=str(ffmpeg), start_seconds=0, duration_seconds=1, nice=15
    )
    assert len(samples) == 0
    ffmpeg_nice, pudge_nice = (int(line) for line in out.read_text().split())
    assert pudge_nice == os.getpriority(os.PRIO_PROCESS, 0)
    assert ffmpeg_nice == pudge_nice + 15 or ffmpeg_nice >= 19, (ffmpeg_nice, pudge_nice)
