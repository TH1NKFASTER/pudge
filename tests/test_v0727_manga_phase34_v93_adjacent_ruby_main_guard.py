from pudge import manga_ocr_worker as worker


def _proposal(*, x, y, width, height, black, count, coverage=0.95, detector=None):
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "detector": detector or worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "component_count": count,
            "component_coverage": coverage,
            "black_ratio": black,
        },
    }


def test_v93_rejects_p31_ruby_lane_next_to_same_height_main_kanji_column():
    ruby = _proposal(x=0.376132, y=0.052333, width=0.018789, height=0.050333, black=0.1082, count=3)
    main = _proposal(x=0.334026, y=0.053167, width=0.043789, height=0.050333, black=0.2580, count=2)

    assert worker._ruby_like_layout_proposal(ruby, [ruby, main]) is True


def test_v93_rejects_p32_ruby_lane_when_stronger_raw_main_lane_sits_beside_it():
    ruby = _proposal(x=0.098500, y=0.814833, width=0.016158, height=0.065333, black=0.0574, count=5, coverage=0.7105)
    main = _proposal(
        x=0.126316,
        y=0.842500,
        width=0.043421,
        height=0.056667,
        black=0.1698,
        count=4,
        detector=worker._RAW_LAYOUT_DETECTOR,
    )

    assert worker._ruby_like_layout_proposal(ruby, [ruby, main]) is True
    assert worker._ruby_like_layout_proposal(main, [ruby, main]) is False


def test_v93_rejects_p36_low_density_ruby_continuation_beside_large_main_glyphs():
    ruby = _proposal(x=0.915605, y=0.182333, width=0.017474, height=0.053667, black=0.0692, count=3, coverage=0.5323)
    main = _proposal(x=0.856395, y=0.284833, width=0.062211, height=0.076167, black=0.3170, count=2)

    assert worker._ruby_like_layout_proposal(ruby, [ruby, main]) is True


def test_v93_keeps_narrow_normal_dialogue_without_a_much_stronger_main_peer():
    candidate = _proposal(x=0.51, y=0.42, width=0.020, height=0.070, black=0.105, count=5, coverage=0.84)
    peer = _proposal(x=0.47, y=0.42, width=0.025, height=0.075, black=0.135, count=5, coverage=0.86)

    assert worker._ruby_like_layout_proposal(candidate, [candidate, peer]) is False


def test_v93_keeps_low_density_lane_when_peer_is_too_far_away_vertically():
    candidate = _proposal(x=0.51, y=0.10, width=0.018, height=0.055, black=0.070, count=3, coverage=0.60)
    peer = _proposal(x=0.47, y=0.30, width=0.060, height=0.080, black=0.30, count=2)

    assert worker._ruby_like_layout_proposal(candidate, [candidate, peer]) is False
