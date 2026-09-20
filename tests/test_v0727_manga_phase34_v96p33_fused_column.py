"""Synthetic fused-neighbour glyph-column regression; no manga fixture dependency."""
from __future__ import annotations

from copy import deepcopy

import pytest
from PIL import Image

from pudge import manga, manga_ocr_worker as worker


WIDTH, HEIGHT = 760, 1200
BOX = (214, 90, 238, 212)
BANDS = [(92 + 16 * i, 104 + 16 * i) for i in range(7)]
READING = "新しい海へ行く"
RETRY = "fused-neighbor-column-crop-consensus-v1"


def row(text, box):
    left, top, right, bottom = box
    return {"text": text, "source": worker._LAYOUT_LINE_SOURCE,
            "orientation": "vertical", "x": left / WIDTH,
            "y": 1 - bottom / HEIGHT, "width": (right-left) / WIDTH,
            "height": (bottom-top) / HEIGHT}


@pytest.fixture
def case(monkeypatch):
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    peers = [row("朝の天気です", (175, 90, 201, 210)),
             row("今日は晴れ", (250, 91, 274, 210))]

    def candidates(_image, rows):
        if any(item.get("recognizer_retry") == RETRY for item in rows):
            return []
        return [(BOX, BANDS, (170, 80, 280, 220))]

    monkeypatch.setattr(worker, "_fused_neighbor_column_candidates", candidates)
    monkeypatch.setattr(worker, "_layout_line_character_segments",
                        lambda _row, text, **_kw: [{"text": ch, "height": .012} for ch in text])
    yield image, peers
    image.close()


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, crop):
        self.calls.append(crop.size)
        return next(self.responses)


def test_recovery_keeps_independent_peers_and_has_character_hitboxes(case):
    image, peers = case
    snapshot = deepcopy(peers)
    model = Model([READING] * 3)
    result = worker._recover_fused_neighbor_column(model, image, peers)
    assert peers == snapshot and result[:2] == peers
    assert len(result) == 3 and len(model.calls) == 3
    recovered = result[-1]
    assert recovered["text"] == READING
    assert recovered["recognizer_retry"] == RETRY
    assert [segment["text"] for segment in recovered["segments"]] == list(READING)
    assert sum(row.get("recognizer_retry") == RETRY for row in
               manga._finalize_recognized_regions(worker._finalize_worker_output_regions(result))) == 1
    assert worker._recover_fused_neighbor_column(Model([]), image, result) == result


@pytest.mark.parametrize("answers", [
    [READING, READING[:-1], READING + "ね"],
    [READING, "空の向こうまで", "山を見に行く"],
    ["", "", ""],
])
def test_disagreement_does_not_generate_column(case, answers):
    image, peers = case
    assert worker._recover_fused_neighbor_column(Model(answers), image, peers) == peers


def test_occupied_candidate_cannot_overwrite_existing_word(case):
    image, peers = case
    existing = row("既知文字列", BOX)
    assert worker._recover_fused_neighbor_column(Model([]), image, peers + [existing]) == peers + [existing]
