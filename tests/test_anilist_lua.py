from importlib.resources import files


def _lua_source() -> str:
    return files("anime_mpv").joinpath("mpv_scripts/anime_mpv_anilist.lua").read_text(
        encoding="utf-8"
    )


def test_lua_tracker_has_separate_auto_update_switch():
    source = _lua_source()

    assert "ANIME_MPV_ANILIST_AUTO_UPDATE" in source
    assert "if not auto_update or triggered" in source


def test_lua_tracker_keeps_manual_hotkeys_when_auto_update_is_off():
    source = _lua_source()

    assert "AniList: ручной режим" in source
    assert "ANIME_MPV_SHORTCUT_MARK_WATCHED" in source
    assert "add_reliable_binding(shortcut_mark_watched" in source
    assert "ANIME_MPV_SHORTCUT_CORRECT_MATCH" in source
    assert "mp.add_key_binding(shortcut_correct_match" in source



def test_lua_tracker_cleans_first_external_text_subtitle_after_its_end():
    source = _lua_source()

    assert "mp.observe_property('sub-text', 'string', on_subtitle_text)" in source
    assert "mp.get_property_number('sub-end/full')" in source
    assert "cue_end - current_time + 0.08" in source
    assert "refresh_after_first_subtitle('scheduled-end')" in source
    assert "selected.external ~= true or selected.image == true" in source
    assert "mp.set_property_bool('sub-visibility', false)" in source
    assert "mp.set_property('secondary-sid', 'no')" in source
    assert "mp.set_property_bool('secondary-sub-visibility', false)" in source
    assert "alternate_subtitle_track" not in source
    assert "mp.set_property_number('sid', alternate_sid)" not in source
    assert "{'sub-reload', tostring(selected_sid)}" in source
    assert "'absolute+exact'" in source
    assert "Proactively reload before the first near-zero cue is painted" not in source
