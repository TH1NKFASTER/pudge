"""Procedural OCR scenes: invented ink and readings, no imported page pixels."""

import pytest
from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def region(text, box, *, size=(760, 1200), **fields):
    left, top, right, bottom = box
    width, height = size
    row = {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "x": left / width,
        "y": 1 - bottom / height,
        "width": (right - left) / width,
        "height": (bottom - top) / height,
    }
    row.update(fields)
    return row


def glyphs(image, box, count):
    """Draw separated hollow blocks, rather than published letter shapes."""
    left, top, right, bottom = box
    draw = ImageDraw.Draw(image)
    step = (bottom - top) / count
    for index in range(count):
        y = round(top + index * step)
        end = round(top + (index + 1) * step) - 4
        draw.rectangle((left, y, right - 1, end), fill="black")
        if right - left > 10 and end - y > 10:
            draw.rectangle((left + 5, y + 5, right - 6, end - 5), fill="white")


def leading_scene(*, offset=0, count=3):
    image = Image.new("RGB", (760, 1200), "white")
    tail = region("青空を見上げる", (300 + offset, 320, 341 + offset, 464))
    glyphs(image, (303 + offset, 320, 337 + offset, 462), 6)
    draw = ImageDraw.Draw(image)
    for index in range(count):
        top = 148 + 30 * index
        draw.rectangle((310 + offset, top, 334 + offset, top + 18), fill="black")
    return image, [tail]


def merged_scene(*, offset=0):
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    # A shared vertical stroke connects seven dense glyph-like bands.
    draw.rectangle((240 + offset, 140, 268 + offset, 329), outline="black", width=4)
    for top in range(142, 327, 27):
        draw.rectangle((241 + offset, top, 267 + offset, top + 9), fill="black")
    return image, []


def ruby_scene(*, omitted_band=None):
    image = Image.new("RGB", (760, 1200), "white")
    anchor = region("ほしぞら", (470, 780, 484, 854))
    glyphs(image, (471, 781, 483, 853), 4)
    glyphs(image, (438, 615, 459, 927), 7)
    if omitted_band is not None:
        spans = ((615, 674), (674, 775), (798, 839), (858, 924))
        top, bottom = spans[omitted_band]
        ImageDraw.Draw(image).rectangle((438, top, 458, bottom - 1), fill="white")
    return image, [anchor]


class Readings:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def __call__(self, crop):
        self.calls.append(crop.size)
        return next(self.answers)


def forbidden(_crop):
    pytest.fail("OCR must not run without physical evidence")
