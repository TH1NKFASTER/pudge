"""Synthetic-only regressions; no copyrighted manga images or OCR exports."""
from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _synthetic():
    image = Image.new('RGB', (320, 300), 'white')
    draw = ImageDraw.Draw(image)
    components = []
    for x in (40, 70, 100):
        for y in (40, 61, 82, 103, 124, 145):
            draw.rectangle((x + 2, y + 2, x + 9, y + 10), fill='black')
            components.append({'x': float(x), 'y': float(y), 'width': 14.0,
                               'height': 14.0, 'cx': float(x + 7)})
    rows = [
        {'text': '正しい言葉', 'orientation': 'vertical', 'source': worker._LAYOUT_LINE_SOURCE,
         'x': x / 320, 'y': 1 - 171 / 300, 'width': 14 / 320, 'height': 131 / 300}
        for x in (190, 220, 250)
    ]
    return image, rows, components


def test_three_physical_columns_independent_of_word_or_page(monkeypatch):
    image, rows, components = _synthetic()
    monkeypatch.setattr(worker, '_raw_layout_component_candidates', lambda _image: components)
    proposals = worker._multi_column_gap_proposals_v96p41(image, rows)
    assert len(proposals) == 3
    assert [round(p['x'] * 320) for p in proposals] == [39, 69, 99]
    assert all(p['provenance']['component_count'] == 6 for p in proposals)


def test_never_hallucinate_without_three_tracks(monkeypatch):
    image, rows, components = _synthetic()
    monkeypatch.setattr(worker, '_raw_layout_component_candidates', lambda _image: components[:12])
    assert worker._multi_column_gap_proposals_v96p41(image, rows) == []


def test_recovery_uses_independent_consensus_and_keeps_existing(monkeypatch):
    image, rows, components = _synthetic()
    monkeypatch.setattr(worker, '_raw_layout_component_candidates', lambda _image: components)
    monkeypatch.setattr(worker, '_recognize_vertical_companion_consensus',
                        lambda _model, _image, _proposal: '新しい文字列')
    output = worker._recover_three_track_raw_gaps_v96p41(object(), image, rows)
    assert output[:3] == rows
    assert len(output) == 6
    assert all(item['text'] == '新しい文字列' for item in output[3:])
    assert all(len(item['segments']) == 6 for item in output[3:])


def test_recovery_requires_repeatable_recognition(monkeypatch):
    image, rows, components = _synthetic()
    monkeypatch.setattr(worker, '_raw_layout_component_candidates', lambda _image: components)
    monkeypatch.setattr(worker, '_recognize_vertical_companion_consensus',
                        lambda _model, _image, _proposal: '')
    assert worker._recover_three_track_raw_gaps_v96p41(object(), image, rows) == rows


def test_detached_final_kana_ink_extends_verified_gap_bbox():
    # Synthetic three-stroke shape: one thin long stroke and two detached
    # horizontal strokes.  The generic glyph-component filter drops these;
    # a tight crop would omit the last character.
    image = Image.new('RGB', (100, 180), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((33, 100, 35, 111), fill='black')
    draw.rectangle((39, 101, 43, 102), fill='black')
    draw.rectangle((38, 109, 44, 110), fill='black')
    box = (30., 20., 47., 98.)
    assert worker._extend_three_track_trailing_ink_v96p41(image, box) == (
        30., 20., 47., 113.,
    )


def test_terminal_stroke_extension_requires_both_detached_cross_strokes():
    image = Image.new('RGB', (100, 180), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((33, 100, 35, 111), fill='black')
    draw.rectangle((39, 101, 43, 102), fill='black')
    box = (30., 20., 47., 98.)
    assert worker._extend_three_track_trailing_ink_v96p41(image, box) == box


def test_terminal_stroke_extension_rejects_distant_markings():
    image = Image.new('RGB', (100, 180), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((33, 125, 35, 136), fill='black')
    draw.rectangle((39, 126, 43, 127), fill='black')
    draw.rectangle((38, 134, 44, 135), fill='black')
    box = (30., 20., 47., 98.)
    assert worker._extend_three_track_trailing_ink_v96p41(image, box) == box


def test_short_transverse_fragment_cannot_hide_verified_vertical_lane(monkeypatch):
    """A short horizontal recognition may cover the start of an omitted lane."""
    image = Image.new('RGB', (320, 300), 'white')
    proposal = {
        'text': '', 'orientation': 'vertical', 'source': worker._LAYOUT_LINE_SOURCE,
        'x': 70 / 320, 'y': 1 - 200 / 300, 'width': 20 / 320,
        'height': 120 / 300, 'provenance': {'component_count': 6},
    }
    fragment = {
        'text': 'かき', 'orientation': 'horizontal',
        'x': 67 / 320, 'y': 1 - 114 / 300,
        'width': 27 / 320, 'height': 29 / 300,
    }
    monkeypatch.setattr(worker, '_multi_column_gap_proposals_v96p41',
                        lambda _image, _rows: [proposal])
    monkeypatch.setattr(worker, '_recognize_vertical_companion_consensus',
                        lambda _model, _image, _proposal: '新しい文字列')
    assert worker._region_coverage(proposal, fragment) > .5
    assert worker._region_coverage(fragment, proposal) < .5
    result = worker._recover_three_track_raw_gaps_v96p41(object(), image, [fragment])
    assert len(result) == 2
    assert result[0] == fragment
    assert result[1]['text'] == '新しい文字列'
    assert len(result[1]['segments']) == len(result[1]['text'])


def test_broad_crossing_or_same_orientation_peers_still_block_recovery(monkeypatch):
    image = Image.new('RGB', (320, 300), 'white')
    proposal = {
        'text': '', 'orientation': 'vertical', 'source': worker._LAYOUT_LINE_SOURCE,
        'x': 70 / 320, 'y': 1 - 200 / 300, 'width': 20 / 320,
        'height': 120 / 300, 'provenance': {'component_count': 6},
    }
    monkeypatch.setattr(worker, '_multi_column_gap_proposals_v96p41',
                        lambda _image, _rows: [proposal])
    monkeypatch.setattr(worker, '_recognize_vertical_companion_consensus',
                        lambda _model, _image, _proposal: '新しい文字列')
    for orientation, top, bottom in [('horizontal', 80, 170), ('vertical', 80, 105)]:
        existing = {
            'text': '既存の文章', 'orientation': orientation,
            'x': 67 / 320, 'y': 1 - bottom / 300,
            'width': 26 / 320, 'height': (bottom - top) / 300,
        }
        assert worker._recover_three_track_raw_gaps_v96p41(
            object(), image, [existing]) == [existing]
