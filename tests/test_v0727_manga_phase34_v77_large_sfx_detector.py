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


def _large_sfx_page() -> Image.Image:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    # Three thick, nearby SFX strokes. Thin panel art should disappear in the
    # detector opening and must not become a proposal by itself.
    draw.polygon([(265, 60), (305, 45), (320, 205), (275, 210)], fill="black")
    draw.rectangle((330, 52, 360, 188), fill="black")
    draw.polygon([(372, 30), (410, 25), (405, 153), (370, 160)], fill="black")
    draw.line((230, 220, 480, 220), fill="black", width=2)
    return image


def test_v77_large_sfx_detector_proposes_only_supported_thick_component_cluster() -> None:
    image = _large_sfx_page()
    try:
        proposals = worker._large_stylized_sfx_component_proposals(image, [])
    finally:
        image.close()

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["source"] == "large-stylized-sfx-component-proposal-v1"
    assert proposal["orientation"] == "horizontal"
    assert proposal["detector"] == "page-ink-large-sfx-components-v1"
    assert 0.16 <= float(proposal["width"]) <= 0.24
    assert 0.12 <= float(proposal["height"]) <= 0.18
    assert len(proposal["segments"]) == 3
    assert all(row["source"] == "page-ink-large-sfx-component-v1" for row in proposal["segments"])


def test_v77_large_sfx_detector_rejects_cluster_already_owned_by_existing_region() -> None:
    image = _large_sfx_page()
    existing = [{"x": 0.33, "y": 0.81, "width": 0.24, "height": 0.18, "text": "既存"}]
    try:
        proposals = worker._large_stylized_sfx_component_proposals(image, existing)
    finally:
        image.close()
    assert proposals == []


def test_v77_large_sfx_proposal_requires_strong_multiview_kana_consensus() -> None:
    image = _large_sfx_page()
    try:
        proposal = worker._large_stylized_sfx_component_proposals(image, [])[0]
        model = _SequenceModel(["ドン", "ドン", "ドン", "ドン", "トン"])
        repaired = worker._recognize_large_stylized_sfx_proposal(model, image, proposal)
    finally:
        image.close()

    assert repaired is not None
    assert repaired["text"] == "ドン"
    assert repaired["selected_hypothesis_id"] == "manga-ocr-large-component-sfx-majority-v1"
    assert repaired["large_component_sfx_vote_count"] == 4
    assert len(repaired["segments"]) == 1
    assert repaired["segments"][0]["text"] == "ドン"
    assert repaired["segments"][0]["source"] == "page-ink-large-sfx-component-union-v1"


def test_v77_large_sfx_proposal_rejects_weak_or_non_kana_consensus() -> None:
    image = _large_sfx_page()
    try:
        proposal = worker._large_stylized_sfx_component_proposals(image, [])[0]
        weak = _SequenceModel(["ドン", "トン", "そして", "ドン", "100"])
        assert worker._recognize_large_stylized_sfx_proposal(weak, image, proposal) is None
    finally:
        image.close()


def test_v77_existing_component_sfx_can_fall_back_to_per_glyph_consensus() -> None:
    piece = {
        "text": "い、よーん",
        "raw_text": "ANCHOR ひよーん！",
        "orientation": "horizontal",
        "x": 0.08,
        "y": 0.38,
        "width": 0.74,
        "height": 0.16,
        "confidence": 0.30,
        "detector": "vision-contrast+vision-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {"text": "ひ", "orientation": "horizontal", "x": 0.10, "y": 0.40, "width": 0.13, "height": 0.12, "source": "vision-accurate-range-v2"},
            {"text": "よ", "orientation": "horizontal", "x": 0.24, "y": 0.40, "width": 0.12, "height": 0.12, "source": "vision-accurate-range-v2"},
            {"text": "ー", "orientation": "horizontal", "x": 0.37, "y": 0.40, "width": 0.09, "height": 0.12, "source": "vision-accurate-range-v2"},
            {"text": "ん", "orientation": "horizontal", "x": 0.47, "y": 0.40, "width": 0.16, "height": 0.12, "source": "vision-accurate-range-v2"},
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "ANCHOR ひよーん！", "selected": False},
            {"id": "manga-ocr", "text": "い、よーん", "selected": True},
        ],
    }
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for segment in piece["segments"]:
        left = round(float(segment["x"]) * image.width)
        right = round((float(segment["x"]) + float(segment["width"])) * image.width)
        top = round((1.0 - float(segment["y"]) - float(segment["height"])) * image.height)
        bottom = round((1.0 - float(segment["y"])) * image.height)
        draw.rectangle((left + 6, top + 6, right - 6, bottom - 6), fill="black")

    # Whole-region five-view consensus intentionally fails. Each physical glyph
    # then gets a 2/3 local vote, yielding a supported assembled surface.
    outputs = ["びょーん", "ひょーん", "ぴよーん", "じゃあ", "びょん"]
    for char in ["び", "ょ", "ー", "ん"]:
        outputs.extend([char, char, "x"])
    model = _SequenceModel(outputs)
    try:
        repaired = worker._repair_component_isolated_stylized_sfx(model, image, piece)
    finally:
        image.close()

    assert repaired["text"] == "びょーん"
    assert repaired["recognition_selection"] == "component-isolated-stylized-sfx-glyph-consensus-v2"
    assert repaired["component_sfx_glyph_vote_counts"] == [2, 2, 2, 2]
    assert [row["text"] for row in repaired["segments"]] == ["び", "ょ", "ー", "ん"]


def test_v77_pipeline_generation_markers_are_current() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")
    worker_source = (root / "pudge" / "manga_ocr_worker.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    assert "large-stylized-sfx-component-proposal-v1" in worker_source
    assert "component-isolated-stylized-sfx-glyph-consensus-v2" in worker_source


def test_v77_large_sfx_detector_rejects_vertically_disjoint_art_cluster() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((75, 430, 190, 530), fill="black")
    draw.rectangle((120, 570, 160, 685), fill="black")
    draw.rectangle((175, 560, 210, 660), fill="black")
    try:
        assert worker._large_stylized_sfx_component_proposals(image, []) == []
    finally:
        image.close()
