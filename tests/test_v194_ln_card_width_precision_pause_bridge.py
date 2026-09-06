from __future__ import annotations

from pathlib import Path

from pudge import reading_audio_alignment as raa


def _chapter() -> dict:
    return {
        "leading_prefix_debug": {
            "recovered": True,
            "chapter_marker": True,
            "story_start": 14054.365,
            "marker_start": 14051.515,
            "precision_window_start": 14030.0,
            "precision_window_end": 14102.0,
        },
        "_runtime_reading_hints": [{"offset_start": 1, "offset_end": 2, "reading": "あ"}],
        "_runtime_precision_segments": [{"start": 14050.0, "end": 14100.0, "text": "x"}],
        "leading_prefix_start": 14054.365,
    }


def test_high_rate_post_precision_bridge_uses_end_of_following_pause(monkeypatch) -> None:
    chapter = _chapter()
    coarse = [
        {"offset": 0, "time": 14054.365},
        {"offset": 11, "time": 14057.515},
        {"offset": 142, "time": 14089.015},
        {"offset": 154.999, "time": 14090.360},
        {"offset": 154.999, "time": 14090.899},
        {"offset": 155, "time": 14090.900},
        {"offset": 162, "time": 14094.755},
    ]

    monkeypatch.setattr(
        raa,
        "_precision_reading_word_clock",
        lambda *args, **kwargs: (
            [
                {"offset": 11, "time": 14057.515},
                {"offset": 133, "time": 14086.700},
                {"offset": 135, "time": 14087.395},
                {"offset": 137, "time": 14088.075},
                {"offset": 140, "time": 14088.815},
                {"offset": 142, "time": 14089.015},
            ],
            {
                "mode": "precision_word_reading_prefix",
                "verified_through_offset": 142,
            },
        ),
    )

    refined, _count, debug = raa._runtime_precision_reading_prefix(chapter, coarse)

    assert debug and debug["verified_through_offset"] == 142
    # The 142 -> 154.999 bridge is ~9.7 chars/s and is immediately followed by
    # a 539ms encoded pause hold.  Do not race to 154.999 and wait there.
    assert {"offset": 154.999, "time": 14090.36} not in refined
    assert {"offset": 154.999, "time": 14090.899} not in refined
    assert {"offset": 155, "time": 14090.9, "wall_clock_from_previous": True} in refined
    assert debug["wall_clock_pause_bridge"] is True
    assert {"offset": 162, "time": 14094.755} in refined


def test_normal_rate_bridge_keeps_original_coarse_anchor(monkeypatch) -> None:
    chapter = _chapter()
    coarse = [
        {"offset": 0, "time": 10.0},
        {"offset": 20, "time": 14.0},
        {"offset": 30, "time": 16.5},  # 4 chars/s from precision end below
        {"offset": 40, "time": 19.0},
    ]
    monkeypatch.setattr(raa, "_chapter_recovered_story_hold", lambda _chapter: (10.0, None))
    monkeypatch.setattr(
        raa,
        "_precision_reading_word_clock",
        lambda *args, **kwargs: (
            [
                {"offset": 5, "time": 11.0},
                {"offset": 10, "time": 12.0},
                {"offset": 20, "time": 14.0},
            ],
            {"mode": "precision_word_reading_prefix", "verified_through_offset": 20},
        ),
    )
    refined, _count, _debug = raa._runtime_precision_reading_prefix(chapter, coarse)
    assert {"offset": 30, "time": 16.5} in refined


def test_study_card_starts_at_old_width_and_can_expand_to_long_term_limit() -> None:
    css = Path("pudge/web/reading_tools.css").read_text(encoding="utf-8")
    js = Path("pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert (
        ".pudge-study-card{width:min(var(--pudge-study-width,440px),calc(100vw - 24px));"
        "max-width:min(620px,calc(100vw - 24px))"
    ) in css
    assert (
        ".pudge-study-term{min-width:0;flex:1;padding-top:0;font-size:25px;"
        "font-weight:700;line-height:1.15;white-space:nowrap;"
        "overflow-wrap:normal;word-break:keep-all}"
    ) in css
    assert "function sizeStudyCard(el)" in js
    assert "Math.max(440, Math.min(620, required))" in js
    assert "sizeStudyCard(pop);" in js
    # Small windows remain usable instead of overflowing off-screen.
    assert (
        "@media(max-width:520px){.pudge-study-card{width:calc(100vw - 16px);"
        "padding:12px}.pudge-study-head{gap:8px}.pudge-study-term{white-space:normal;"
    ) in css
