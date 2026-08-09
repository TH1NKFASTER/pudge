from anime_mpv.subtitle_formats import format_preference_bonus


def test_native_srt_gets_stronger_general_format_bonus() -> None:
    assert format_preference_bonus('episode.srt', True) == 16.0
    assert format_preference_bonus('episode.ass', True) == 6.0
    assert format_preference_bonus('episode.ssa', True) == 5.0
    assert format_preference_bonus('episode.srt', True) - format_preference_bonus('episode.ass', True) == 10.0


def test_format_bonus_remains_disabled_when_preference_is_off() -> None:
    assert format_preference_bonus('episode.srt', False) == 0.0
    assert format_preference_bonus('episode.ass', False) == 0.0
