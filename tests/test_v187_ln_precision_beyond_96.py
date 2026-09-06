from __future__ import annotations

from pudge.reading_audio_alignment import _precision_reading_word_clock


def test_precision_word_clock_extends_through_mazu_and_sotogawa() -> None:
    hints = [
        {"offset_start": 100, "offset_end": 103, "surface": "荷馬車", "reading": "にばしゃ"},
        {"offset_start": 128, "offset_end": 130, "surface": "膨大", "reading": "ぼうだい"},
        {"offset_start": 131, "offset_end": 133, "surface": "ため", "reading": "ため"},
        {"offset_start": 133, "offset_end": 135, "surface": "まず", "reading": "まず"},
        {"offset_start": 135, "offset_end": 136, "surface": "町", "reading": "まち"},
        {"offset_start": 137, "offset_end": 139, "surface": "外側", "reading": "そとがわ"},
        {"offset_start": 142, "offset_end": 144, "surface": "検問", "reading": "けんもん"},
        {"offset_start": 147, "offset_end": 150, "surface": "通行証", "reading": "つうこうしょう"},
        {"offset_start": 151, "offset_end": 155, "surface": "もらって", "reading": "もらって"},
    ]
    segments = [
        {"start": 14070.0, "end": 14070.4, "words": [{"start": 14070.0, "end": 14070.4, "word": "まず"}]},
        {"start": 14079.70, "end": 14090.30, "words": [
            {"start": 14079.72, "end": 14080.15, "word": "にばしゃ"},
            {"start": 14084.20, "end": 14084.58, "word": "ぼうだい"},
            {"start": 14084.92, "end": 14085.18, "word": "ため"},
            {"start": 14085.32, "end": 14085.60, "word": "まず"},
            {"start": 14085.61, "end": 14085.84, "word": "まち"},
            {"start": 14086.70, "end": 14087.10, "word": "そとがわ"},
            {"start": 14087.72, "end": 14088.12, "word": "けんもん"},
            {"start": 14088.92, "end": 14089.42, "word": "つうこうしょう"},
            {"start": 14089.72, "end": 14090.20, "word": "もらって"},
        ]},
    ]
    coarse = [
        {"offset": 100, "time": 14079.72},
        {"offset": 136, "time": 14085.935},
        {"offset": 154.999, "time": 14090.36},
    ]

    clock, debug = _precision_reading_word_clock(
        hints,
        segments,
        search_start=14070.0,
        search_end=14102.0,
        coarse_anchors=coarse,
    )

    assert debug is not None
    assert int(debug["verified_through_offset"]) >= 155
    points = {(int(row["offset"]), float(row["time"])) for row in clock}
    assert (133, 14085.32) in points
    assert (135, 14085.6) in points
    assert (137, 14086.7) in points
    assert (139, 14087.1) in points
    assert (151, 14089.72) in points
    assert (155, 14090.2) in points
    # The repeated decoy "まず" far outside the coarse-time neighborhood is ignored.
    assert not any(int(row["offset"]) == 133 and float(row["time"]) < 14080 for row in clock)
