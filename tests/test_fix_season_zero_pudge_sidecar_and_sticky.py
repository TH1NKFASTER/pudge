"""Reviewer repro (second review of the season-0 rule), kept as a regression test."""

from __future__ import annotations

from pathlib import Path

from test_fix_season_zero_specials import SPECIALS, TV, _library, _ova_name, _rows, _tv_name


def test_pudge_written_sidecar_in_the_title_folder_disables_the_rule(tmp_path: Path) -> None:
    # Adapted after the third review: a title-named folder alone is no proof that Pudge wrote
    # the sidecar (the person may have pinned it). Pudge records the sidecars it writes.
    from pudge import extra_files

    api, folder = _library(tmp_path)
    (folder.parent / ".anilist.id").write_text(str(TV), encoding="utf-8")
    api.manager.db.set_state(extra_files.pudge_sidecar_state_key(folder.parent), str(TV))
    api.manager.scan_library()
    rows = _rows(api, folder)
    assert rows[_ova_name(1)] == (SPECIALS, 1), rows[_ova_name(1)]


def test_unproven_sidecar_in_the_title_folder_keeps_the_title_but_no_duplicate(tmp_path: Path) -> None:
    api, folder = _library(tmp_path)
    (folder.parent / ".anilist.id").write_text(str(TV), encoding="utf-8")  # whose? unknown
    api.manager.scan_library()
    rows = _rows(api, folder)
    assert [rows[_ova_name(n)] for n in (1, 2, 3)] == [(TV, None)] * 3
    assert [rows[_tv_name(n)] for n in (1, 2, 3)] == [(TV, 1), (TV, 2), (TV, 3)]


def test_extra_flips_back_to_tv_episode_when_the_regular_file_goes(tmp_path: Path) -> None:
    api, folder = _library(tmp_path, relation=False)
    api.manager.scan_library()
    rows = _rows(api, folder)
    print("first:", rows[_ova_name(1)])
    assert rows[_ova_name(1)] == (TV, None)
    # Watched and cleaned up: the regular episode 1 file is gone.
    (folder / _tv_name(1)).unlink()
    (folder / _tv_name(1)).with_suffix(".ja.srt").unlink()
    api.manager.scan_library()
    rows = _rows(api, folder)
    print("after:", rows[_ova_name(1)])
    assert rows[_ova_name(1)] == (TV, None), rows[_ova_name(1)]
