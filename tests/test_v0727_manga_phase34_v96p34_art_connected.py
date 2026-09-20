"""v96p34: image-verified short OCR hallucinations, no page/text hardcoding."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from pudge import manga_ocr_worker as worker


ROOT = Path(os.environ.get("PUDGE_V96P34_FIXTURES", ""))


def _fixture_dir() -> Path:
    if not ROOT.is_dir() or not (ROOT / "pages/page_029.png").is_file():
        pytest.skip("v96p34 PNG/JSON fixtures absent")
    return ROOT


def _regions(number: int) -> list[dict]:
    return json.loads((_fixture_dir()/"fresh"/f"page_{number:02d}.json").read_text())['data']['regions']


@pytest.mark.parametrize(('page','surface'), ((29, '・しかし'), (31, 'こん'), (34, 'あァ！')))
def test_real_art_candidate_suppressed(page, surface):
    with Image.open(_fixture_dir()/"pages"/f"page_{page:03d}.png") as image:
        before = _regions(page)
        after = worker._suppress_art_connected_short_vertical_noise(image, before)
    removed = [r for r in before if all(r is not surviving for surviving in after)]
    assert [r['text'] for r in removed] == [surface]


@pytest.mark.parametrize('page', range(40))
def test_old_forty_page_corpus_only_three_weak_art_guesses(page):
    with Image.open(_fixture_dir()/"pages"/f"page_{page:03d}.png") as image:
        before = _regions(page)
        after = worker._suppress_art_connected_short_vertical_noise(image, before)
    removed = [r['text'] for r in before if all(r is not surviving for surviving in after)]
    expected = {29:['・しかし'], 31:['こん'], 34:['あァ！']}
    assert removed == expected.get(page, [])


def test_p34_actual_dialogue_and_title_retained():
    with Image.open(_fixture_dir()/"pages/page_034.png") as image:
        before = _regions(34)
        after = worker._suppress_art_connected_short_vertical_noise(image, before)
    assert sum(row['text'] == 'あァ！？' for row in after) == 1
    assert any('ONE PIECE' in row['text'] for row in after)


@pytest.mark.parametrize('page,surface', ((29,'・しかし'),(31,'こん')))
def test_durable_raw_observation_and_high_confidence_protected(page,surface):
    with Image.open(_fixture_dir()/"pages"/f"page_{page:03d}.png") as image:
        original = next(row for row in _regions(page) if row.get('text') == surface)
        for change in ({'raw_text':surface},{'confidence':0.92},{'detector':'other'}):
            row = dict(original,**change)
            assert not worker._art_connected_short_vertical_noise(image,row,[row])


def test_donor_not_removed_without_horizontal_verified_latin_peer():
    with Image.open(_fixture_dir()/"pages/page_034.png") as image:
        rows = _regions(34)
        suspect = next(row for row in rows if row.get('text') == 'あァ！')
        assert worker._art_connected_short_vertical_noise(image,suspect,rows)
        assert not worker._art_connected_short_vertical_noise(
            image,suspect,[r for r in rows if r.get('orientation')!='horizontal'])
