"""Mokuro text blocks in the manga reader and statistics pagination (behaviour; audit 71Б:
the Mokuro scenarios of tests/js/mokuro_jiten_g10.cjs, which ran functions cut out of
manga_reader_v2.js, now click the page in the page harness)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.t71g06_support import reader
from tests.t71g07_support import CLICKS

ROOT = Path(__file__).resolve().parents[1]


def mokuro(
    text: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    status: str = "approximate",
    source: str = "mokuro",
    lines: list | None = None,
) -> dict:
    """An OCR region imported from Mokuro: a block rectangle (image fractions, y from the
    bottom) and, when the export has them, its line rectangles; no per-character geometry."""
    provenance: dict = {"source": source}
    if lines is not None:
        provenance["line_boxes"] = lines
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "geometry_status": status,
        "provenance": provenance,
    }


def line(text: str, x: float | None, y: float, width: float, height: float) -> dict:
    return {"text": text, "x": x, "y": y, "width": width, "height": height}


# Page images are 1000 x 1500 px at (0, 0): an OCR box (x, y, w, h) covers client x
# [1000x, 1000(x+w)] and client y [1500(1-y-h), 1500(1-y)].
PAGES = {
    0: [
        mokuro("日本語", 0.2, 0.3, 0.2, 0.3),  # x 200-400, y 600-1050: block only
        mokuro("本日", 0.6, 0.7, 0.1, 0.2, status="unknown"),  # x 600-700, y 150-450
        mokuro("語学", 0.2, 0.75, 0.1, 0.1, source="vision"),  # x 200-300, y 225-375
        mokuro(
            "日本語",
            0.6,
            0.1,
            0.2,
            0.3,
            lines=[
                line("日本", 0.61, 0.25, 0.15, 0.1),  # y 975-1125
                line("語", 0.61, 0.11, 0.15, 0.1),
            ],
        ),  # y 1185-1335
    ],
    1: [
        mokuro(
            "日本語", 0.2, 0.3, 0.2, 0.3, lines=[line("WRONG", 0.8, 0.3, 0.1, 0.1)]
        ),  # the line lies outside the block
        mokuro("語学", 0.6, 0.1, 0.2, 0.3, lines=[line("WRONG", None, 0.35, 0.1, 0.1)]),  # a line without x
        mokuro(
            "本日", 0.6, 0.6, 0.2, 0.2, lines=[line("日本", 0.58, 0.65, 0.05, 0.1)]
        ),  # x 600-800; the line starts at 580
        {**mokuro("毎日", 0.05, 0.05, 0.1, 0.1), "width": "unknown"},  # broken geometry
    ],
}
# Every block text is two words: a single word filling a whole region would be its own
# (native) hit area.
WORDS = {
    "日本語": [["日本", 11, 0], ["語", 12, 0]],
    "日本": [["日本", 11, 0]],
    "語": [["語", 12, 0]],
    "本日": [["本", 14, 0], ["日", 15, 0]],
    "語学": [["語", 12, 0], ["学", 13, 0]],
    "毎日": [["毎", 16, 0], ["日", 15, 0]],
}


def test_mokuro_blocks_and_lines_open_the_text_actually_under_the_pointer(page) -> None:
    # Mokuro gives block and line rectangles, not word boxes: a click inside one looks up its
    # whole text (the card shows its first word); nothing is clickable outside the observed
    # rectangles, and no word geometry is made up.
    result = reader(
        page,
        CLICKS
        + r"""
        const at = async (x, y) => { const term = await clickImage(x / W, 1 - y / H); await closeCard(); return term; };
        const out = {page1: {block: await at(300, 800), outside: await at(900, 800), unknownGeometry: await at(650, 300),
                             vision: await at(250, 300), firstLine: await at(680, 1050), secondLine: await at(680, 1260),
                             betweenLines: await at(680, 1155)}};
        await __click('#mangaReaderV2 [data-manga-v2-action="next"]'); await __tick(300); __sizeImages(); await __tick(50);
        out.page2 = {block: await at(300, 800), whereTheForeignLineIs: await at(850, 1200), missingX: await at(700, 1100),
                     clippedLine: await at(615, 500), lineOutsideTheBlock: await at(590, 500), broken: await at(100, 1350)};
        out.lookups = __apiCalls.filter(c => c.name === 'study_parse_text').map(c => c.args[0]);
        return out;
        """,
        regions=PAGES,
        parse=WORDS,
    )
    out = result.value
    assert out["page1"] == {
        "block": "日本",
        "outside": None,
        "unknownGeometry": None,
        "vision": None,
        "firstLine": "日本",
        "secondLine": "語",
        "betweenLines": None,
    }
    assert out["page2"] == {
        "block": "日本",
        "whereTheForeignLineIs": None,
        "missingX": "語",
        "clippedLine": "日本",
        "lineOutsideTheBlock": None,
        "broken": None,
    }


def test_statistics_exhausted_pagination_stops_querying() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/statistics_async_g7.cjs"), str(ROOT / "pudge/web/statistics.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pagination exhaustion: PASS" in result.stdout
