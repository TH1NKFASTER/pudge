"""v96p29: bounded physical punctuation proof, no page/text literals in production."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pudge import manga, manga_ocr_worker as worker

FIXTURES = Path(os.environ.get('PUDGE_V96P29_FIXTURES', Path(__file__).resolve().parent / 'fixtures/v96p29'))


def _region(text='．．．こここだ！！'):
    return {
        'text': text, 'orientation': 'vertical', 'source': worker._LAYOUT_LINE_SOURCE,
        'detector': 'manga-ink-components-v1', 'x': 668/760,
        'y': 1 - 834/1200, 'width': 19/760, 'height': 119/1200,
    }


def _synthetic(*, fake_glyph=False, omit_stroke=False, displace_dot=False):
    image = Image.new('RGB', (760, 1200), 'white')
    draw = ImageDraw.Draw(image)
    for i in range(9):
        x = 673 + (4 if displace_dot and i == 4 else 0)
        y = 717 + 10*i
        draw.ellipse((x, y, x+6, y+7), fill='black')
    draw.rectangle((669, 808, 675, 823), fill='black')
    if not omit_stroke:
        draw.rectangle((679, 808, 685, 823), fill='black')
    if fake_glyph:
        draw.rectangle((668, 794, 685, 804), fill='black')
    return image


def test_physical_dot_stack_removes_unsupported_kana_without_moving_region():
    with _synthetic() as image:
        original = _region()
        result = worker._repair_ink_verified_punctuation_column(image, original)
        assert result['text'] == '………！！'
        assert result['recognition_correction'] == 'vertical-punctuation-ink-proof-v1'
        assert result['provenance']['punctuation_ink_proof']['dot_components'] == 9
        assert result['provenance']['punctuation_ink_proof']['terminal_strokes'] == 2
        assert len(result['segments']) == len(result['text'])
        assert all(result[k] == original[k] for k in ('x','y','width','height'))
        assert original['text'] == '．．．こここだ！！'
        assert manga._finalize_recognized_regions([result])[0]['text'] == result['text']
        assert worker._repair_ink_verified_punctuation_column(image, result) == result


@pytest.mark.parametrize('image_kwargs', [
    {'fake_glyph': True}, {'omit_stroke': True}, {'displace_dot': True},
])
def test_missing_physical_proof_does_not_relabel(image_kwargs):
    with _synthetic(**image_kwargs) as image:
        row = _region()
        assert worker._repair_ink_verified_punctuation_column(image, row) is row


@pytest.mark.parametrize('change', [
    {'text':'．．．うるせェ！！'}, {'text':'おれは友達を'},
    {'detector':'manga-raw-components-v1'}, {'source':'other'},
    {'orientation':'horizontal'}, {'width':.15},
])
def test_surface_or_geometry_mismatch_stays_unchanged(change):
    # Changed Japanese content alone must never be enough to edit a valid lane.
    with Image.new('RGB', (760, 1200), 'white') as image:
        row = {**_region(), **change}
        assert worker._repair_ink_verified_punctuation_column(image, row) is row


def test_all_40_successful_v96p28_mac_pages_only_change_physically_proven_punctuation():
    pages = FIXTURES / 'pages'
    fresh = FIXTURES / 'fresh'
    if not (pages / 'page_033.png').is_file() or not (fresh / 'page_33.json').is_file():
        pytest.skip('full successful v96p28 Mac corpus not supplied')
    changes = []
    for page in range(40):
        with Image.open(pages / f'page_{page:03d}.png') as image:
            old = json.loads((fresh / f'page_{page:02d}.json').read_text(encoding='utf8'))['data']['regions']
            updated = worker._repair_ink_verified_punctuation_columns(image, old)
            assert len(old) == len(updated)
            for previous, after in zip(old, updated):
                if previous['text'] != after['text']:
                    changes.append((page, previous['text'], after['text']))
                    assert all(previous[key] == after[key] for key in ('x','y','width','height'))
                    assert after['recognition_correction'] == 'vertical-punctuation-ink-proof-v1'
                else:
                    assert after is previous
    assert changes == [(33, '．．．こここだ！！', '………！！')]
