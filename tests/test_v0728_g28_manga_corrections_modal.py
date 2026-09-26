from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _z_index(css: str, selector: str) -> int:
    match = re.search(
        re.escape(selector) + r"\s*\{[^}]*z-index\s*:\s*(\d+)",
        css,
    )
    assert match, f"missing z-index rule for {selector}"
    return int(match.group(1))


def test_manga_reader_modals_render_above_reader() -> None:
    index = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    reader_css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    reader_js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    reader_z = _z_index(reader_css, ".manga-v2-reader")
    default_modal_z = _z_index(index, ".modal-backdrop")
    reader_modal_z = _z_index(
        index,
        "body.manga-v2-reading .modal-backdrop.open",
    )

    assert default_modal_z < reader_z < reader_modal_z
    assert "document.body.classList.add('manga-v2-reading')" in reader_js
    assert "document.body.classList.remove('manga-v2-reading')" in reader_js


def test_ocr_corrections_open_the_shared_modal_while_reader_is_active() -> None:
    reader_js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    correction_list = reader_js.split(
        "async function showMangaCorrectionList()", 1
    )[1].split(
        "function positionMangaContextMenu", 1
    )[0]

    assert "$('modalBackdrop')" in correction_list
    assert "backdrop.classList.add('open')" in correction_list
    assert "API().manga_ocr_corrections" in correction_list
