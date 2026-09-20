from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


class _SequenceModel:
    def __init__(self, outputs: Iterable[str]) -> None:
        self.outputs = iter(outputs)
        self.calls = 0

    def __call__(self, _image: Image.Image) -> str:
        self.calls += 1
        return next(self.outputs)


def _thin_core_page() -> Image.Image:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    # Four dense stroke cores laid out like the p34 ガチャ… SFX after the
    # conservative 5x5 opening.  None is individually large enough for v77.
    draw.rectangle((457, 109, 466, 139), fill="black")
    draw.rectangle((472, 132, 480, 147), fill="black")
    draw.rectangle((507, 127, 517, 152), fill="black")
    draw.rectangle((538, 147, 546, 154), fill="black")
    # Thin original strokes around those cores.  They should be present in the
    # OCR crop but are intentionally not required to survive morphology.
    draw.line((430, 92, 457, 125), fill="black", width=2)
    draw.line((443, 88, 450, 174), fill="black", width=2)
    draw.line((485, 100, 481, 169), fill="black", width=2)
    draw.line((520, 110, 531, 165), fill="black", width=2)
    draw.line((547, 145, 564, 170), fill="black", width=2)
    return image


def test_v91_thin_large_sfx_core_detector_recovers_p34_like_cluster_only() -> None:
    image = _thin_core_page()
    try:
        assert worker._large_stylized_sfx_component_proposals(image, []) == []
        proposals = worker._thin_large_sfx_core_proposals(image, [])
    finally:
        image.close()

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["source"] == "thin-large-sfx-core-proposal-v1"
    assert proposal["detector"] == "page-ink-thin-large-sfx-cores-v1"
    assert proposal["orientation"] == "horizontal"
    assert len(proposal["segments"]) == 4
    assert all(
        segment["source"] == "page-ink-thin-large-sfx-core-v1"
        for segment in proposal["segments"]
    )


def test_v91_thin_large_sfx_core_detector_rejects_shallow_art_band() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    # p23-like shallow horizontal art: enough little cores to look tempting,
    # but physical union height is only ~2.5% of the page.
    draw.rectangle((230, 1081, 244, 1097), fill="black")
    draw.rectangle((261, 1079, 272, 1095), fill="black")
    draw.rectangle((293, 1078, 304, 1095), fill="black")
    draw.rectangle((324, 1069, 337, 1098), fill="black")
    try:
        assert worker._thin_large_sfx_core_proposals(image, []) == []
    finally:
        image.close()


def test_v91_thin_large_sfx_core_requires_strong_five_view_consensus() -> None:
    image = _thin_core_page()
    try:
        proposal = worker._thin_large_sfx_core_proposals(image, [])[0]
        model = _SequenceModel(["ガチャ．．．", "ガチャ．．．", "ガチャ．．．", "ガチャ．．．", "ガチャ．"])
        recovered = worker._recognize_thin_large_sfx_core_proposal(model, image, proposal)
    finally:
        image.close()

    assert recovered is not None
    assert recovered["text"] == "ガチャ..."
    assert recovered["recognition_selection"] == "thin-large-sfx-core-majority-v1"
    assert recovered["thin_large_sfx_vote_count"] == 4
    assert recovered["selected_hypothesis_id"] == "manga-ocr-thin-large-sfx-core-majority-v1"


def test_v91_thin_large_sfx_core_rejects_weak_consensus() -> None:
    image = _thin_core_page()
    try:
        proposal = worker._thin_large_sfx_core_proposals(image, [])[0]
        model = _SequenceModel(["ガチャ．．．", "カチャ", "ガチャ", "ガチャ．", "チャ"])
        assert worker._recognize_thin_large_sfx_core_proposal(model, image, proposal) is None
    finally:
        image.close()


def test_v91_pipeline_generation_markers_are_current() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
