import os
import subprocess

from pudge import player
from pudge.player import mpv_supports_option as real_probe  # captured before any fixture patch


def _fake_mpv(tmp_path, counter):
    script = tmp_path / "mpv"
    script.write_text(f"#!/bin/sh\necho x >> '{counter}'\necho ' --macos-app-activation-policy  Choices: regular accessory'\necho ' --pause  Flag'\n")
    script.chmod(0o755)
    return str(script)


def test_probe_runs_once_per_executable_identity(tmp_path):
    player._MPV_OPTION_CACHE.clear()
    counter = tmp_path / "calls"
    mpv = _fake_mpv(tmp_path, counter)
    assert real_probe(mpv, "macos-app-activation-policy") is True
    assert real_probe(mpv, "pause") is True
    assert real_probe(mpv, "missing-option") is False
    assert counter.read_text().count("x") == 1
    stat = os.stat(mpv)
    os.utime(mpv, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))  # replaced binary → re-probe
    assert real_probe(mpv, "pause") is True
    assert counter.read_text().count("x") == 2


def test_probe_failure_never_breaks_playback(tmp_path, monkeypatch):
    player._MPV_OPTION_CACHE.clear()
    mpv = _fake_mpv(tmp_path, tmp_path / "calls")

    class NotAContextManager:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr(subprocess, "Popen", NotAContextManager)
    assert real_probe(mpv, "pause") is False
    assert real_probe(str(tmp_path / "missing-mpv"), "pause") is False


def test_cross_file_window_uses_concat_filter_not_list_demuxer():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "pudge" / "audiobooks.py").read_text(encoding="utf-8")
    body = source.split("def _extract_global_audio_window(", 1)[1].split("\n    def ", 1)[0]
    assert "concat=n={len(parts)}:v=0:a=1[out]" in body
    assert '"-f", "concat"' not in body and "concat.txt" not in body
