from __future__ import annotations

import zipfile
from pathlib import Path

from pudge.light_novels import LightNovelService, _epub_metadata
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import _stt_text_clock_candidate, _subtitle_stt_text_score


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
COVER_PREVIEW = ROOT / "pudge" / "web" / "cover_preview.js"
STT = ROOT / "pudge" / "subtitles" / "stt.py"
SYNCING = ROOT / "pudge" / "syncing.py"


def _wolf_epub(path: Path, *, visible_contents: bool) -> None:
    container = '''<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>'''
    manifest = [
        '<item id="badnav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="contents" href="contents.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="contents"/>']
    for i in range(1, 4):
        manifest.append(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    opf = f'''<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>狼と香辛料</dc:title></metadata>
      <manifest>{''.join(manifest)}</manifest><spine>{''.join(spine)}</spine>
    </package>'''
    badnav = '''<html xmlns="http://www.w3.org/1999/xhtml"><body><nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>
      <li><a href="c1.xhtml">狼と香辛料</a></li><li><a href="c2.xhtml">狼と香辛料</a></li><li><a href="c3.xhtml">狼と香辛料</a></li>
    </ol></nav></body></html>'''
    links = (
        '<a href="c1.xhtml">序幕</a><a href="c2.xhtml">第一幕</a><a href="c3.xhtml">第二幕</a>'
        if visible_contents
        else '<p>Contents page without usable chapter links.</p>'
    )
    contents = f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Contents</h1>{links}</body></html>'
    chapter_text = "これは物語の本文です。" * 20
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", badnav)
        zf.writestr("OEBPS/contents.xhtml", contents)
        for i in range(1, 4):
            zf.writestr("OEBPS/c%d.xhtml" % i, f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{chapter_text}{i}</p></body></html>')


def test_epub_visible_contents_recovers_real_chapter_titles(tmp_path: Path) -> None:
    epub = tmp_path / "wolf.epub"
    _wolf_epub(epub, visible_contents=True)
    title, chapters, _cover = _epub_metadata(epub)
    assert title == "狼と香辛料"
    story_titles = [name for name, text in chapters if "物語の本文" in text]
    assert story_titles == ["序幕", "第一幕", "第二幕"]
    assert LightNovelService.CONTENT_SCHEMA >= 8


def test_epub_repeated_book_title_falls_back_to_numbered_chapters(tmp_path: Path) -> None:
    epub = tmp_path / "wolf.epub"
    _wolf_epub(epub, visible_contents=False)
    _title, chapters, _cover = _epub_metadata(epub)
    story_titles = [name for name, text in chapters if "物語の本文" in text]
    assert story_titles == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert all(name != "狼と香辛料" for name in story_titles)


def test_blurred_image_first_click_is_consumed() -> None:
    html = INDEX.read_text(encoding="utf-8")
    preview = COVER_PREVIEW.read_text(encoding="utf-8")
    assert "pudgeConsumeRevealClick='1'" in preview
    assert "figure?.dataset?.pudgeConsumeRevealClick==='1'" in html
    assert "delete figure.dataset.pudgeConsumeRevealClick;return;" in html


def test_escape_clears_selection_before_fullscreen_exit() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "function clearSelectionOnEscape()" in html
    assert "nativeSelection.removeAllRanges()" in html
    assert "clearLnSelection();return true" in html
    assert "PudgeMangaReaderV2.clearSelection" in html
    assert "PudgeAudiobookSelection.clearSelection" in html
    assert html.index("if(clearSelectionOnEscape())return;") < html.index("pywebview.api.exit_fullscreen()")


def test_searches_share_common_style_and_pair_label_has_no_ellipsis() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert ".pudge-search-input{" in html
    assert ".pudge-search-surface{" in html
    assert 'id="plannedSearch" type="search" class="pudge-search-input"' in html
    assert 'id="lnFindInput" class="pudge-search-input"' in html
    assert 'id="lnAudioLinkSearch" class="pudge-search-input"' in html
    assert 'id="settingsSearch" class="pudge-search-input"' in html
    assert 'id="globalSearchInput" class="pudge-search-input"' in html
    assert "'Pair audiobook'" in html
    assert "Pair audiobook…" not in html


def test_paired_badge_is_outside_clamped_title() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert '<span class="ln-card-title">${title}</span>${pairedBadge}' in html
    assert '>🎧</span>' in html
    assert '.ln-card h3{line-height:1.25;display:flex' in html
    assert '.ln-paired-audio-badge{flex:0 0 auto}' in html


def test_stt_text_clock_recovers_shifted_japanese_timeline(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    reference = tmp_path / "reference.ja.srt"
    source_cues = []
    reference_cues = []
    phrases = [
        "今日は市場へ行って林檎を買います",
        "旅の途中で商人と話をしました",
        "北の村では雪が静かに降ります",
        "荷馬車の上で狼は笑っています",
        "明日の朝には港町へ着くでしょう",
    ]
    for i in range(20):
        start = i * 3.2
        text = f"{phrases[i % len(phrases)]}{i}番"
        source_cues.append((start, start + 1.8, text))
        reference_cues.append((start + 8.4, start + 10.2, text))
    write_srt(source_cues, source)
    write_srt(reference_cues, reference)

    aligned, diagnostics = _stt_text_clock_candidate(source, reference, tmp_path / "cache", model="test")
    assert aligned is not None, diagnostics
    assert diagnostics["accepted"] is True
    assert int(diagnostics["matched_anchor_count"] or 0) >= 8
    assert float(diagnostics["text_alignment"]["score"]) >= 0.9
    out = parse_srt(aligned)
    assert out[0][0] - source_cues[0][0] > 7.0
    semantic = _subtitle_stt_text_score(aligned, reference)
    assert semantic["available"] is True
    assert float(semantic["score"]) >= 0.9


def test_stt_cache_keeps_word_transcript_and_sync_uses_text_clock() -> None:
    stt = STT.read_text(encoding="utf-8")
    syncing = SYNCING.read_text(encoding="utf-8")
    assert '"--words"' in stt
    assert "stt-v4-content-audio-text-clock" in stt
    assert "video_content_fingerprint" in stt
    assert "audio_stream" in stt
    assert "result_path.unlink(missing_ok=True)" not in stt
    assert "align_light_novel_to_transcript(" in syncing
    assert 'engine="japanese-stt+text-clock"' in syncing
    assert "stt_text_alignment" in syncing
