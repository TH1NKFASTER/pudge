"""Public synthetic OCR tests. No real manga images or captured OCR data."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'pudge' / 'manga_ocr_worker.py'
spec = importlib.util.spec_from_file_location('pudge_v96p38_worker_under_test', WORKER)
assert spec is not None and spec.loader is not None
worker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = worker
spec.loader.exec_module(worker)


def artificial_example(text='！！！？１２箱ある', source='manga-layout-line-v1', **overrides):
    im = Image.new('RGB', (760, 1200), 'white')
    draw = ImageDraw.Draw(im)
    for y in (95, 111, 128, 144, 163, 180):
        draw.rectangle((46, y, 65, y+11), fill='black')
    row = dict(source=source, detector='manga-ink-components-v1', orientation='vertical',
               x=45/760, y=(1200-190)/1200, width=30/760, height=110/1200,
               confidence=.78, text=text, raw_text='', segments=[], provenance={})
    row.update(overrides)
    return im, row


def test_two_agreeing_crops_recover_leading_print_and_its_hitboxes():
    im,row=artificial_example()
    reads=iter(('箱に12箱ある', '箱に12箱ある', 'wrong'))
    found=worker._retry_spurious_punctuation_before_vertical_number(lambda im:next(reads),im,row)
    assert found['text']=='箱に12箱ある'
    assert ''.join(x['text'] for x in found['segments'])==found['text']
    assert found['recognizer_retry']=='vertical-punctuation-prefix-reread-v1'
    assert row['text']=='！！！？１２箱ある'


def test_consensus_rejects_nonmatching_suffix():
    im,row=artificial_example()
    for answers in [('箱に11箱ある',)*3, ('箱に12箱ある','wrong','bad'), ('?!12箱ある',)*3]:
        reads=iter(answers)
        assert worker._retry_spurious_punctuation_before_vertical_number(lambda im:next(reads),im,row) is row


def test_non_numeric_punctuation_and_regular_dialogue_are_untouched():
    im,row=artificial_example(text='．．．大丈夫！！')
    def must_not_ocr(_):
        raise AssertionError('unrelated text should never invoke OCR')
    assert worker._retry_spurious_punctuation_before_vertical_number(must_not_ocr,im,row) is row
    for text in ('３箱ある','それから！', '！！！１２箱ある'):
        _,different=artificial_example(text=text)
        if text=='！！！１２箱ある':
            different['recognizer_retry']='already-fixed'
        assert worker._retry_spurious_punctuation_before_vertical_number(must_not_ocr,im,different) is different


def test_bad_geometry_detector_and_confidence_are_ignored():
    im,row=artificial_example()
    def must_not_ocr(_):
        raise AssertionError('not an eligible region')
    for change in ({'orientation':'horizontal'}, {'confidence':.99}, {'width':.12},
                   {'source':'vision-line'}, {'raw_text':'known print'},
                   {'detector':'vision-original'}):
        item={**row,**change}
        assert worker._retry_spurious_punctuation_before_vertical_number(must_not_ocr,im,item) is item


def test_ocr_failure_is_failsafe():
    im,row=artificial_example()
    def failed(_): raise RuntimeError('model failed')
    assert worker._retry_spurious_punctuation_before_vertical_number(failed,im,row) is row
