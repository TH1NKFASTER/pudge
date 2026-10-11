"""Context furigana: readings of known words on hover and while the word is read aloud,
two reader settings (behaviour; audit 71Б rewrite). Where the readings come from:
tests/test_v0747_ln_f8_and_context_furigana.py; layout: tests/test_v0748_*."""

from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from t71g21_support import READER, computed_style, paired


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return LightNovelService(config)


def test_context_furigana_settings_default_on_and_persist(tmp_path: Path) -> None:
    service = _service(tmp_path)
    defaults = service.settings_payload()
    assert defaults["furigana_on_hover"] is True
    assert defaults["furigana_on_reading"] is True

    saved = service.save_settings(
        {
            "furigana_on_hover": False,
            "furigana_on_reading": False,
        }
    )
    assert saved["furigana_on_hover"] is False
    assert saved["furigana_on_reading"] is False
    reloaded = LightNovelService(service.config).settings_payload()
    assert reloaded["furigana_on_hover"] is False
    assert reloaded["furigana_on_reading"] is False


# The paired book without its audiobook.
UNPAIRED = r"""
const pairedOpen = __api.light_novel_open;
__api.light_novel_open = async id => { const book = await pairedOpen(id); delete book.paired_audio; return book; };
"""
GEAR = (
    READER
    + r"""
await openReader();
await __click('#lnReaderAppearanceToggle'); await __tick(20);
const gear = () => ({hover: __visible('#lnrFuriganaHover') && $('lnrFuriganaHover').checked,
  reading: __visible('#lnrFuriganaReading') && $('lnrFuriganaReading').checked,
  spacer: !$('lnrFuriganaReadingSpacer').hidden,
  labels: [__text('label:has(#lnrFuriganaHover)'), __text('label:has(#lnrFuriganaReading)')],
  readingClass: $('lnReader').classList.contains('furigana-reading')});
return {gear: gear(), texts: [t('settings.lnFuriganaHover'), t('settings.lnFuriganaReading')]};
"""
)


def test_reader_gear_has_both_switches_and_reading_only_with_an_audiobook(page) -> None:
    for lang in ("en", "ru"):
        linked = page.run(GEAR, lang=lang, **paired()).value
        assert linked["gear"] == {
            "hover": True,
            "reading": True,
            "spacer": True,
            "labels": linked["texts"],
            "readingClass": True,
        }
        # Without an audiobook nothing is read aloud: the second switch (and its grid
        # spacer) is hidden and the reader never reveals readings "while reading".
        alone = page.run(GEAR, lang=lang, **paired(extra=UNPAIRED)).value
        assert alone["gear"]["hover"] is True
        assert (alone["gear"]["reading"], alone["gear"]["spacer"], alone["gear"]["readingClass"]) == (
            False,
            False,
            False,
        )


def test_context_furigana_switches_change_the_reader_and_are_saved(page) -> None:
    # Known words: 明日 (1) and 話し (3). The highlight is painted again after each switch
    # (with the audiobook stopped; see the tests below for switching while listening).
    result = page.run(
        READER
        + computed_style("display")
        + r"""
        await openReader();
        const overlay = i => __css(wordEls()[i].querySelector('.ln-furigana-ruby'), 'display', 'after');
        const look = (hovered, read) => { __hover(wordEls()[hovered]); const hover = overlay(hovered); __hover(document.body);
                                          return {hover, read: overlay(read), quiet: overlay(hovered)}; };
        paintAt(3, 103);                                   // 明日 is being read
        const out = {on: look(3, 1)};
        await __click('#lnReaderAppearanceToggle'); await __tick(20);
        await __input('#lnrFuriganaHover', false); await __tick(300);
        paintAt(7, 107);                                   // 話し
        out.noHover = look(1, 3);
        await __input('#lnrFuriganaReading', false); await __tick(300);
        paintAt(9, 109);                                   // 先日 (unknown)
        out.neither = {read: overlay(4), known: [1, 3].map(i => Boolean(wordEls()[i].querySelector('.ln-furigana-ruby'))),
                       unknown: [0, 2, 4].map(i => wordEls()[i].querySelector('.ln-furigana-ruby')?.dataset.lnReading)};
        return out;
        """,
        **paired(),
    )
    out = result.value
    assert out["on"] == {"hover": "block", "read": "block", "quiet": "none"}
    assert out["noHover"] == {"hover": "none", "read": "block", "quiet": "none"}
    # With both switches off nothing is revealed and known words have no readings at all;
    # furigana of unknown words is a separate setting and stays.
    assert out["neither"]["read"] == "none"
    assert out["neither"]["known"] == [False, False]
    assert out["neither"]["unknown"] == ["きょう", "きのう", "せんじつ"]
    saved = [call[0] for call in result.calls_of("light_novel_save_settings")]
    assert [(row["furigana_on_hover"], row["furigana_on_reading"]) for row in saved] == [
        (False, True),
        (False, False),
    ]


def test_changing_a_reader_setting_while_listening_keeps_following_the_audio(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        await untilAudio(106.2);                           // 話し
        await __click('#lnReaderAppearanceToggle'); await __tick(20);
        await __input('#lnrFont', 'gothic');
        await untilAudio(109);                             // 先日 by now
        return current();
        """,
        **paired(playing=True),
    )
    assert result.value == 4


def test_rerendering_the_chapter_while_listening_keeps_the_spoken_word_highlighted(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        await untilAudio(106.2);
        const before = current();
        await __click('#lnUnknownFuriganaToggle');
        await untilAudio(107.5);                           // still 話し
        return {before, after: current()};
        """,
        **paired(playing=True),
    )
    assert result.value == {"before": 3, "after": 3}
