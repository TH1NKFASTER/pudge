from __future__ import annotations

import zipfile
from pathlib import Path

from pudge.light_novels import LightNovelService, _epub_metadata, _ln_epub_image_path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
COMPARE = ROOT / "scripts" / "compare_subtitle_alignment.py"


def _write_epub(path: Path, *, title: str, spine_rows: list[tuple[str, str]], files: dict[str, str | bytes], nav_rows: list[tuple[str, str]] | None = None) -> None:
    container = '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'''
    manifest = []
    spine = []
    for item_id, href in spine_rows:
        props = ' properties="nav"' if item_id == "nav" else ""
        manifest.append(f'<item id="{item_id}" href="{href}" media-type="application/xhtml+xml"{props}/>')
        linear = ' linear="no"' if item_id.startswith("nonlinear") else ""
        spine.append(f'<itemref idref="{item_id}"{linear}/>')
    image_items = []
    for name in files:
        if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            item_id = "img_" + str(len(image_items))
            mt = "image/png" if name.lower().endswith(".png") else "image/jpeg"
            image_items.append(f'<item id="{item_id}" href="{name}" media-type="{mt}"/>')
    if nav_rows is not None and not any(i == "nav" for i, _ in spine_rows):
        manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    opf = f'''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata><manifest>{''.join(manifest)}{''.join(image_items)}</manifest><spine>{''.join(spine)}</spine></package>'''
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        if nav_rows is not None:
            links = "".join(f'<li><a href="{href}">{label}</a></li>' for href, label in nav_rows)
            zf.writestr("OEBPS/nav.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body><nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>{links}</ol></nav></body></html>')
        for name, data in files.items():
            zf.writestr("OEBPS/" + name, data)


def test_svg_image_only_front_matter_is_preserved_before_first_text_chapter(tmp_path: Path) -> None:
    epub = tmp_path / "asobi-like.epub"
    front = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:xlink="http://www.w3.org/1999/xlink"><body><svg xmlns="http://www.w3.org/2000/svg"><image xlink:href="Images/front.jpg"/></svg></body></html>'''
    story = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>これは本文です。物語がここから始まります。</p></body></html>'
    _write_epub(
        epub,
        title="あそびのかんけい 1",
        spine_rows=[("nonlinear_titlepage", "titlepage.xhtml"), ("story", "story.xhtml")],
        files={"titlepage.xhtml": front, "story.xhtml": story, "Images/front.jpg": b"fake-jpeg"},
        nav_rows=[("story.xhtml", "第一章")],
    )
    _title, chapters, _cover = _epub_metadata(epub)
    assert len(chapters) == 1
    chapter_title, chapter_text = chapters[0]
    assert chapter_title == "第一章"
    first_line = chapter_text.splitlines()[0]
    assert first_line.startswith("[[PUDGE_EPUB_IMAGE:")
    assert _ln_epub_image_path(first_line) == "OEBPS/Images/front.jpg"
    assert "物語がここから" in chapter_text
    assert LightNovelService.CONTENT_SCHEMA >= 10


def test_dominant_repeated_volume_title_falls_back_to_chapter_numbers(tmp_path: Path) -> None:
    epub = tmp_path / "wolf2.epub"
    rows = [(f"c{i}", f"c{i}.xhtml") for i in range(1, 6)]
    files = {f"c{i}.xhtml": f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>本文{i}です。狼と商人の長い物語です。</p></body></html>' for i in range(1, 6)}
    _write_epub(
        epub,
        title="狼と香辛料 2",
        spine_rows=rows,
        files=files,
        nav_rows=[(f"c{i}.xhtml", "狼と香辛料 第二巻") for i in range(1, 6)],
    )
    _title, chapters, _cover = _epub_metadata(epub)
    assert [name for name, _text in chapters] == [f"Chapter {i}" for i in range(1, 6)]


def test_visible_contents_repairs_dominant_repeated_title_even_when_not_equal_package_title(tmp_path: Path) -> None:
    epub = tmp_path / "wolf2-contents.epub"
    labels = ["序章", "第一章", "第二章", "第三章", "終章"]
    rows = [("contents", "contents.xhtml"), *[(f"c{i}", f"c{i}.xhtml") for i in range(1, 6)]]
    files: dict[str, str | bytes] = {
        "contents.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body><div>Contents<br/>' + '<br/>'.join(labels) + '</div></body></html>'
    }
    files.update({f"c{i}.xhtml": f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>本文{i}です。狼と商人の長い物語です。</p></body></html>' for i in range(1, 6)})
    _write_epub(
        epub,
        title="狼と香辛料 2",
        spine_rows=rows,
        files=files,
        nav_rows=[(f"c{i}.xhtml", "SPICE AND WOLF II") for i in range(1, 6)],
    )
    _title, chapters, _cover = _epub_metadata(epub)
    assert [name for name, text in chapters if "長い物語" in text] == labels
    assert not any("Contents" in text for _name, text in chapters)


def test_reader_images_follow_ttsu_fit_principles_and_badge_moves_back_up() -> None:
    html = INDEX.read_text(encoding="utf-8")
    image_container = html.split(".ln-inline-image{", 1)[1].split("}", 1)[0]
    image_rule = html.split(".ln-inline-image img{", 1)[1].split("}", 1)[0]
    assert "display:flex" in image_container
    assert "justify-content:center" in image_container
    assert "break-inside:avoid" in image_container
    assert "max-width:100%" in image_rule
    assert "max-height:calc(100vh - 96px)" in image_rule
    assert "width:auto" in image_rule and "height:auto" in image_rule
    assert "transform:translateY(.5px)" in html
    assert "transform:translateY(1.5px)" not in html.split(".ln-paired-audio-badge{", 1)[1].split("}", 1)[0]


def test_ab_comparator_self_reexecs_into_pudge_python_when_system_python_has_no_pillow() -> None:
    script = COMPARE.read_text(encoding="utf-8")
    assert 'importlib.util.find_spec("PIL")' in script
    assert 'PROJECT_ROOT / ".venv-patch-v0725" / "bin" / "python"' in script
    assert 'Path.home() / ".local" / "share" / "pudge" / "venv" / "bin" / "python"' in script
    assert 'os.execve(str(candidate)' in script
    assert 'PUDGE_SUBTITLE_AB_REEXEC' in script
