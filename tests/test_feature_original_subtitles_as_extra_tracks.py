"""mpv starts with the prepared subtitles and also lists every original file, unchanged.

User: "let all the subtitles of this episode go into the video: start with the computed ones,
but just in case give every variant there is (without changes, just the originals)".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from lua_harness import ANILIST, needs_lua, run_mpv_scenario
from pudge.config import AppConfig
from pudge.models import SubtitleCandidate
from pudge.subtitles.originals import list_originals, originals_root, save_originals
from test_skip_segments_playback import _launch

SRT = "1\n00:00:01,000 --> 00:00:02,000\n{text}\n"


def _file(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(SRT.format(text=text), encoding="utf-8")
    return path


def test_the_originals_are_kept_unchanged_best_first(tmp_path: Path) -> None:
    root = tmp_path / "originals"
    video = tmp_path / "Show - 03.mkv"
    jimaku = tmp_path / "jimaku"
    best = _file(jimaku / "a", "[NanakoRaws] Show - 03.srt", "一")
    ass = _file(jimaku / "b", "[NanakoRaws] Show - 03.ass", "二")
    same = _file(jimaku / "c", "copy of best.srt", "一")  # byte-identical: once
    image = jimaku / "d" / "Show - 03.sup"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"PG")

    kept = save_originals(root, video, [best, ass, same, image])
    assert [path.name for path in kept] == ["01 [NanakoRaws] Show - 03.srt", "02 [NanakoRaws] Show - 03.ass"]
    assert kept[0].read_bytes() == best.read_bytes() and kept[1].read_bytes() == ass.read_bytes()

    stamp = kept[0].stat().st_mtime_ns
    assert save_originals(root, video, [best, ass]) == kept  # unchanged set: nothing rewritten
    assert kept[0].stat().st_mtime_ns == stamp
    newer = _file(jimaku / "e", "[Other] Show - 03.srt", "三")
    assert [path.name for path in save_originals(root, video, [newer])] == ["01 [Other] Show - 03.srt"]
    assert list_originals(root, tmp_path / "Other - 01.mkv") == []


def test_preparation_keeps_the_candidates_it_found(tmp_path: Path) -> None:
    from pudge.cli import _keep_original_subtitles

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "data" / "library.sqlite3"
    video = tmp_path / "Show - 03.mkv"
    low = _file(tmp_path / "j1", "low.srt", "低")
    high = _file(tmp_path / "j2", "high.ass", "高")
    candidates = [
        SubtitleCandidate(path=low, source="jimaku", score=40.0, name=low.name),
        SubtitleCandidate(path=high, source="jimaku", score=90.0, name=high.name),
    ]
    _keep_original_subtitles(video, candidates, cfg, logging.getLogger("test"))
    root = originals_root(cfg.library.database_path)
    assert root == (tmp_path / "data" / "prepared-subtitles" / "originals").resolve()
    assert [path.name for path in list_originals(root, video)] == ["01 high.ass", "02 low.srt"]


def test_playback_passes_the_originals_to_mpv(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "run"
    folder.mkdir()
    original = _file(tmp_path / "jimaku", "[NanakoRaws] Anime - 05.srt", "原文")
    save_originals(originals_root(folder / "library.sqlite3"), folder / "Anime - 05.mkv", [original])
    command, env, _existed = _launch(folder, monkeypatch, exclusive=False, segments=False)
    [passed] = json.loads(env["PUDGE_SUBTITLE_ORIGINALS"])
    assert Path(passed).name == "01 [NanakoRaws] Anime - 05.srt"
    # The prepared subtitles are still the one loaded with the video (and selected).
    assert [arg for arg in command if arg.startswith("--sub-file=")] and Path(passed).name not in " ".join(
        command
    )

    command, env, _existed = _launch(tmp_path / "plain", monkeypatch, exclusive=False, segments=False)
    assert "PUDGE_SUBTITLE_ORIGINALS" not in env


@needs_lua
def test_mpv_adds_them_as_tracks_that_are_not_selected(tmp_path: Path) -> None:
    paths = ["/subs/01 [NanakoRaws] Show - 03.srt", "/subs/02 [NanakoRaws] Show - 03.ass"]
    run_mpv_scenario(
        tmp_path,
        """
        sim.set('track-list', {{type = 'sub', id = 1, external = true, ['external-filename'] = '/prepared.srt'}})
        sim.fire('file-loaded')
        local added = sim.commands('^sub%-add')
        assert(#added == 2, 'two originals: ' .. #added)
        assert(added[1] == 'sub-add /subs/01 [NanakoRaws] Show - 03.srt auto Оригинал: [NanakoRaws] Show - 03.srt', added[1])
        assert(added[2]:find(' auto Оригинал: [NanakoRaws] Show - 03.ass', 1, true), added[2])
        """,
        scripts=[ANILIST],
        env={"PUDGE_SUBTITLE_ORIGINALS": json.dumps(paths, ensure_ascii=False)},
        language="ru",
    )
