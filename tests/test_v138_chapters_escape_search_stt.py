"""EPUB chapter titles, Escape and text selection, shared search look, the light-novel card
badge, and speech transcripts as a text clock (behaviour; audit 71Б rewrite).

Covered elsewhere: the first click on a blurred illustration only reveals it
(test_v137_pair_blur_images.py::test_reader_blur_setting_and_click_to_reveal_then_zoom_contract);
Escape leaves library multi-select first (test_v151_preview_jiten_escape.py,
test_v227_r1_escape_vn.py); "Pair audiobook" in the card menu (test_v137_pair_blur_images.py)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from css_support import css_rules
from pudge.config import SyncConfig
from pudge.light_novels import LightNovelService, _epub_metadata
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles import stt
from pudge.syncing import _stt_text_clock_candidate, _subtitle_stt_text_score, _try_japanese_stt_fallback
from tests.i18n_support import en, has_text


def _wolf_epub(path: Path, *, visible_contents: bool) -> None:
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    manifest = [
        '<item id="badnav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="contents" href="contents.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="contents"/>']
    for i in range(1, 4):
        manifest.append(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    opf = f"""<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>狼と香辛料</dc:title></metadata>
      <manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine>
    </package>"""
    badnav = """<html xmlns="http://www.w3.org/1999/xhtml"><body><nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><ol>
      <li><a href="c1.xhtml">狼と香辛料</a></li><li><a href="c2.xhtml">狼と香辛料</a></li><li><a href="c3.xhtml">狼と香辛料</a></li>
    </ol></nav></body></html>"""
    links = (
        '<a href="c1.xhtml">序幕</a><a href="c2.xhtml">第一幕</a><a href="c3.xhtml">第二幕</a>'
        if visible_contents
        else "<p>Contents page without usable chapter links.</p>"
    )
    contents = f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Contents</h1>{links}</body></html>'
    chapter_text = "これは物語の本文です。" * 20
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", badnav)
        zf.writestr("OEBPS/contents.xhtml", contents)
        for i in range(1, 4):
            zf.writestr(
                "OEBPS/c%d.xhtml" % i,
                f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{chapter_text}{i}</p></body></html>',
            )


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


def test_escape_clears_a_text_selection_before_it_leaves_full_screen(page) -> None:
    result = page.run(
        r"""
        setPage('current'); await __tick(50);
        const range = document.createRange(); range.selectNodeContents(document.querySelector('#current'));
        getSelection().addRange(range);
        const view = () => ({text: !getSelection().isCollapsed, fullscreenExits: __apiCalls.filter(c => c.name === 'exit_fullscreen').length});
        const out = {before: view()};
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(10); out.first = view();
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(10); out.second = view();
        return out;
        """,
        state={
            "home": {
                "waiting": [
                    {
                        "media_id": 7,
                        "title": "Show",
                        "next_episode": 3,
                        "site_url": "https://anilist.co/anime/7",
                    }
                ]
            }
        },
    )
    assert result.value == {
        "before": {"text": True, "fullscreenExits": 0},
        "first": {"text": False, "fullscreenExits": 0},
        "second": {"text": False, "fullscreenExits": 1},
    }


SEARCHES = ["plannedSearch", "lnFindInput", "lnAudioLinkSearch", "settingsSearch", "globalSearchInput"]


def test_every_search_field_has_the_shared_search_look(page) -> None:
    result = page.run(
        r"""
        setPage('planned'); await __tick(20);
        setPage('settings'); await __tick(50);
        openGlobalSearch(); await __tick(20); closeGlobalSearch();
        await showLnAudiobookLink({id: 6, title: 'Novel', volume: 1}); await __tick(20);
        return Object.fromEntries(IDS.map(id => [id, document.getElementById(id)?.classList.contains('pudge-search-input') ?? null]));
        """.replace("IDS", json.dumps(SEARCHES)),
        api={"light_novel_audiobook_candidates": []},
    )
    assert result.value == {name: True for name in SEARCHES}
    rules = css_rules("index.html")
    look = rules.declarations(".pudge-search-input")
    assert look["border-radius"] == "9px" and look["padding"] == "10px 12px" and look["font-size"] == "14px"
    assert rules.get(".pudge-search-surface", "border-radius") == "14px"
    assert en("ui.pairAudiobook") == "Pair audiobook"
    assert not has_text("Pair audiobook…")


def test_headphone_badge_sits_next_to_the_title_and_is_never_clamped_away(page) -> None:
    # The 🎧 badge was inside the two-line-clamped title: long titles cut it off.
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        const heading = document.querySelector('#lightNovelsContent [data-ln-book="6"] h3');
        return {children: [...heading.children].map(el => el.className), badge: heading.querySelector('.ln-paired-audio-badge').textContent,
                inTitle: Boolean(heading.querySelector('.ln-card-title .ln-paired-audio-badge'))};
        """,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [
                    {
                        "id": 6,
                        "title": "A very long light novel title " * 4,
                        "volume": 1,
                        "paired_audio": {"ln_book_id": 6, "book": {"id": 75}},
                    }
                ],
            }
        },
    )
    assert result.value == {
        "children": ["ln-card-title", "ln-paired-audio-badge"],
        "badge": "🎧",
        "inTitle": False,
    }
    rules = css_rules("index.html")
    heading, title, badge = (
        rules.declarations(sel) for sel in (".ln-card h3", ".ln-card-title", ".ln-paired-audio-badge")
    )
    assert (heading["display"], heading["align-items"], heading["gap"]) == ("flex", "center", "1px")
    assert (
        title["-webkit-line-clamp"] == "2" and title["flex"] == "0 1 auto" and title["width"] == "fit-content"
    )
    assert title["max-width"] == "calc(100% - 16px)"  # room for the badge after a long title
    assert badge["flex"] == "0 0 auto" and badge["margin-left"] == "0"
    assert (badge["display"], badge["align-items"], badge["transform"]) == (
        "inline-flex",
        "center",
        "translateY(.5px)",
    )


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


def _fake_tools(monkeypatch, streams: list[dict]) -> list[list[str]]:
    """ffprobe reports `streams`; ffmpeg writes the audio; the speech worker writes a word transcript."""
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        command = [str(value) for value in command]
        commands.append(command)
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": streams}), stderr="")
        if "pudge.subtitles.stt_worker" in command:
            words = [
                {
                    "start": i * 2.0,
                    "end": i * 2.0 + 0.5,
                    "text": f"台詞{i}",
                    "words": [{"word": f"台詞{i}", "start": i * 2.0, "end": i * 2.0 + 0.5}],
                }
                for i in range(10)
            ]
            Path(command[-2]).write_text(
                json.dumps({"segments": words}, ensure_ascii=False), encoding="utf-8"
            )
        else:
            Path(command[-1]).write_bytes(b"flac")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stt.subprocess, "run", run)
    monkeypatch.setattr(stt, "_worker_availability", lambda _python: {"available": True})
    return commands


def test_speech_transcript_keeps_words_and_is_cached_by_video_content_and_audio_track(
    tmp_path: Path, monkeypatch
) -> None:
    streams = [
        {"index": 1, "codec_name": "aac", "tags": {"language": "eng"}},
        {"index": 2, "codec_name": "aac", "tags": {"language": "jpn"}},
    ]
    commands = _fake_tools(monkeypatch, streams)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video" * 1000)
    prepare = lambda path: stt.prepare_japanese_stt_reference(  # noqa: E731
        path,
        tmp_path / "cache",
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        model="tiny",
        timeout_seconds=60,
    )

    reference, info = prepare(video)

    assert reference is not None and info["cache"] == "miss" and info["word_timestamps"] is True
    [extract] = [command for command in commands if command[0] == "ffmpeg"]
    assert extract[extract.index("-map") + 1] == "0:2"  # the Japanese track
    [worker] = [command for command in commands if "pudge.subtitles.stt_worker" in command]
    assert "--words" in worker
    # The word-level transcript stays next to the reference (the text clock reads it later).
    transcript_output = Path(info["transcript_path"])
    transcript = json.loads(transcript_output.read_text(encoding="utf-8"))
    assert transcript["segments"][0]["words"][0]["word"] == "台詞0"
    # A renamed copy of the same file is the same speech; changed content is not.
    renamed = tmp_path / "Show - 01 [renamed].mkv"
    renamed.write_bytes(video.read_bytes())
    assert prepare(renamed)[1]["cache"] == "hit"
    changed = tmp_path / "episode-v2.mkv"
    changed.write_bytes(b"other" * 1000)
    assert prepare(changed)[1]["cache"] == "miss"
    # Another Japanese track (a second dub) is transcribed separately.
    streams[1]["index"] = 3
    assert prepare(video)[1]["cache"] == "miss"


def test_speech_fallback_uses_the_transcript_text_as_its_clock(tmp_path: Path, monkeypatch) -> None:
    import pudge.syncing as syncing

    phrases = [
        "今日は市場へ行って林檎を買います",
        "旅の途中で商人と話をしました",
        "北の村では雪が静かに降ります",
        "荷馬車の上で狼は笑っています",
        "明日の朝には港町へ着くでしょう",
    ]
    source_cues = [(i * 3.2, i * 3.2 + 1.8, f"{phrases[i % 5]}{i}番") for i in range(20)]
    source, reference = tmp_path / "source.srt", tmp_path / "reference.ja.srt"
    write_srt(source_cues, source)
    write_srt(
        [(start + 8.4, end + 8.4, text) for start, end, text in source_cues], reference
    )  # what was spoken, 8.4 s later
    monkeypatch.setattr(
        syncing,
        "prepare_japanese_stt_reference",
        lambda *_a, **_k: (reference, {"available": True, "cache": "hit"}),
    )
    monkeypatch.setattr(
        syncing,
        "synchronize_with_alass",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ALASS is not needed")),
    )

    output, result = _try_japanese_stt_fallback(
        tmp_path / "video.mkv",
        source,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )

    assert output is not None and result["engine"] == "japanese-stt+text-clock"
    assert result["stt_text_alignment"]["accepted"] is True
    assert abs(parse_srt(output)[0][0] - 8.4) < 0.5
