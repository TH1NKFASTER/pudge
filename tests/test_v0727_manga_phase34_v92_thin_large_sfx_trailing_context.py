from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


class _SequenceModel:
    def __init__(self, outputs: Iterable[str]) -> None:
        self.outputs = iter(outputs)

    def __call__(self, _image: Image.Image) -> str:
        return next(self.outputs)


def _thin_core_page() -> Image.Image:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((457, 109, 466, 139), fill="black")
    draw.rectangle((472, 132, 480, 147), fill="black")
    draw.rectangle((507, 127, 517, 152), fill="black")
    draw.rectangle((538, 147, 546, 154), fill="black")
    draw.line((430, 92, 457, 125), fill="black", width=2)
    draw.line((443, 88, 450, 174), fill="black", width=2)
    draw.line((485, 100, 481, 169), fill="black", width=2)
    draw.line((520, 110, 531, 165), fill="black", width=2)
    draw.line((547, 145, 564, 170), fill="black", width=2)
    return image


def test_v92_thin_large_sfx_ocr_crop_keeps_trailing_punctuation_context() -> None:
    image = _thin_core_page()
    try:
        proposal = worker._thin_large_sfx_core_proposals(image, [])[0]
    finally:
        image.close()

    core_bbox = proposal["thin_large_sfx_core_bbox_px"]
    ocr_bbox = proposal["thin_large_sfx_ocr_bbox_px"]
    assert proposal["thin_large_sfx_trailing_context_px"] == 8
    assert ocr_bbox[2] - core_bbox[2] >= 40


def test_v92_real_p34_surface_does_not_invent_small_tsu() -> None:
    image = _thin_core_page()
    try:
        proposal = worker._thin_large_sfx_core_proposals(image, [])[0]
        model = _SequenceModel(["ガチャ．．．"] * 4 + ["ガチャ．"])
        recovered = worker._recognize_thin_large_sfx_core_proposal(model, image, proposal)
    finally:
        image.close()

    assert recovered is not None
    assert recovered["text"] == "ガチャ..."
    assert "ッ" not in recovered["text"]
    assert recovered["thin_large_sfx_vote_count"] == 4


def test_v92_pipeline_generation_markers_are_current() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
