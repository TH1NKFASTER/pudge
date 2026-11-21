"""ALASS against a subtitle reference keeps the framerate (no 25/23.976 stretch).

A TV-timed Japanese subtitle aligned to a noisy Japanese STT transcript of a web release:
ALASS's framerate guess picked 25/23.976, a 4 % stretch (+8 s at the start, +64 s at the
end) that Pudge reported as one block with scale 1.0 and accepted as the speech
verification of an early edit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pudge.config import SyncConfig
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import _fingerprint, synchronize_with_alass

FAKE_ALASS = """
import json, shutil, sys
from pathlib import Path
args = sys.argv[1:]
Path({log!r}).write_text(json.dumps(args), encoding="utf-8")
reference, source, output = args[0], args[1], args[2]
shutil.copyfile(source, output)
print("shifted block of 3 subtitles with length 0:00:10.000 by 0:00:00.000")
"""


def _fake_alass(folder: Path) -> tuple[Path, Path]:
    log = folder / "argv.json"
    script = folder / "alass"
    script.write_text(f"#!{sys.executable}\n" + FAKE_ALASS.format(log=str(log)), encoding="utf-8")
    script.chmod(0o755)
    return script, log


def _cues(starts: list[float], prefix: str) -> list[tuple[float, float, str]]:
    return [(start, start + 1.5, f"{prefix}{index}") for index, start in enumerate(starts)]


def test_subtitle_reference_alass_runs_without_framerate_guessing(tmp_path: Path) -> None:
    script, log = _fake_alass(tmp_path)
    reference = tmp_path / "reference.ja.srt"
    source = tmp_path / "source.srt"
    write_srt(_cues([5.0, 12.0, 20.0], "r"), reference)
    write_srt(_cues([4.0, 11.0, 19.0], "s"), source)

    output, result = synchronize_with_alass(
        reference,
        source,
        tmp_path / "cache",
        SyncConfig(),
        alass_path=str(script),
    )

    assert result["sync_was_successful"] is True, result
    argv = json.loads(log.read_text(encoding="utf-8"))
    assert "--disable-fps-guessing" in argv
    assert float(result["framerate_scale_factor"]) == 1.0
    assert [cue[0] for cue in parse_srt(output)] == [4.0, 11.0, 19.0]


def test_cached_framerate_guessing_output_is_not_reused(tmp_path: Path) -> None:
    """An ALASS output cached by a build that let ALASS guess the framerate is not served."""
    script, _log = _fake_alass(tmp_path)
    reference = tmp_path / "reference.ja.srt"
    source = tmp_path / "source.srt"
    write_srt(_cues([5.0, 12.0, 20.0], "r"), reference)
    write_srt(_cues([4.0, 11.0, 19.0], "s"), source)
    config = SyncConfig()
    stale = (
        tmp_path / "cache" / "alass" / f"{_fingerprint(reference, source, config, tag='alass-direct-v2')}.srt"
    )
    stale.parent.mkdir(parents=True)
    write_srt(_cues([40.0, 50.0, 60.0], "s"), stale)

    output, result = synchronize_with_alass(
        reference, source, tmp_path / "cache", config, alass_path=str(script)
    )

    assert output != stale and result.get("cached") is not True
    assert [cue[0] for cue in parse_srt(output)] == [4.0, 11.0, 19.0]
