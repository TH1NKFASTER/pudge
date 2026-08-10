from pathlib import Path


def _lua() -> str:
    return Path("pudge/mpv_scripts/pudge_anilist.lua").read_text(encoding="utf-8")


def test_ctrl_a_uses_forced_binding_with_old_mpv_fallback() -> None:
    source = _lua()
    assert "mp.add_forced_key_binding(key, name, callback" in source
    assert "add_reliable_binding(shortcut_mark_watched, 'pudge_anilist_update'" in source
    assert "PUDGE_SHORTCUT_MARK_WATCHED" in source
    assert "mp.add_key_binding(key, name, callback)" in source


def test_manual_ctrl_a_gives_immediate_feedback_and_saves_position() -> None:
    source = _lua()
    manual_block = source.split("local function update_anilist(manual)", 1)[1].split(
        "local function open_anilist", 1
    )[0]
    assert "AniList: засчитываю серию…" in manual_block
    assert "save_playback(true)" in manual_block
    assert "manual and {'--manual'}" in manual_block


def test_missing_tracking_file_is_visible_instead_of_silent() -> None:
    source = _lua()
    assert "AniList: трекер недоступен для этого файла" in source
    assert "tracking file is empty" in source
    assert "return false" in source
