from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _dark_column_small_kana_polarity_consensus,
    _recognize_dark_column_surfaces,
)


def test_trace_surface_accepts_two_view_small_tsu_consensus() -> None:
    repaired, changes = _dark_column_small_kana_polarity_consensus(
        "放つた一言は",
        "放った一言は",
        "放った一言は",
    )
    assert repaired == "放った一言は"
    assert changes == [{"index": 1, "from": "つ", "to": "っ"}]


def test_one_original_polarity_view_is_not_enough() -> None:
    assert _dark_column_small_kana_polarity_consensus(
        "放つた一言は",
        "放った一言は",
        "放つた一言は",
    ) == ("放つた一言は", [])


def test_unrelated_character_change_is_rejected() -> None:
    assert _dark_column_small_kana_polarity_consensus(
        "放つた一言は",
        "放った二言は",
        "放った二言は",
    ) == ("放つた一言は", [])


def test_size_change_needs_matching_context_on_both_sides() -> None:
    assert _dark_column_small_kana_polarity_consensus(
        "つた一言は",
        "った一言は",
        "った一言は",
    ) == ("つた一言は", [])


def test_dark_column_pipeline_uses_confirmed_original_polarity_small_tsu() -> None:
    image = Image.new("RGB", (200, 260), "black")
    segments = [
        {
            "text": "仮",
            "x": 0.72,
            "y": 0.20,
            "width": 0.08,
            "height": 0.60,
            "source": "dark-columns-v1",
        },
        {
            "text": "仮",
            "x": 0.48,
            "y": 0.20,
            "width": 0.08,
            "height": 0.60,
            "source": "dark-columns-v1",
        },
    ]

    class TraceModel:
        def __init__(self) -> None:
            self.values = iter([
                "放つた一言は",       # inverted square baseline
                "放った一言は",       # padded original polarity
                "放った一言は",       # tight original polarity
                "全世界の人々を",     # next inverted square lane
            ])

        def __call__(self, _crop: Image.Image) -> str:
            return next(self.values)

    corrected, surfaces = _recognize_dark_column_surfaces(
        TraceModel(),
        image,
        segments,
        "放った一言は全世界の人々を",
    )
    image.close()

    assert surfaces == ["放った一言は", "全世界の人々を"]
    assert corrected == "放った一言は全世界の人々を"
