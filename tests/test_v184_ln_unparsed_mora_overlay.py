"""Light novels: text the dictionary did not parse is still made of words — the reader
follows them with the audiobook and opens the shared word card on them (behaviour; audit
71Б rewrite).

The reader runs in node (tests/page_harness.py). Word weights of an unparsed word with
small kana (きゃく = 0.5,0.5,1) are checked in test_v0723_ln_audio_weighted_highlight.py.
Blurred illustrations (the rest of v1.84): test_v186_ttsu_images_clock_window.py."""

from __future__ import annotations

from i18n_support import has_text

# Light novel 9 "Novel" linked to audiobook 31 (paused). Its one paragraph has dictionary
# tokens only for 猫 and アリス: the text before, between and after them is unparsed.
#     「ねえ、猫だ」とアリスは言った      audio characters: ねえ 0-2 · 猫 2-3 · だ 3-4 · と 4-5
#                                        · アリス 5-8 · は言った 8-12 (punctuation is silent)
UNPARSED = r"""
window.PAIRED = {linked: true, audiobook_id: 31, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
  playing: false, paused: true, player_running: false, position: 0, speed: 1, duration: 600, ln_chapter_index: 0,
  chapter_char_offset_exact: 0, anchor_window: {path: [{time: 0, offset: 0}, {time: 20, offset: 20}]}};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0,
  chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
    book: {id: 31, title: 'Audio', duration: 600, position: 0, speed: 1, playing: false, paused: true, player_running: false}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index,
  paragraphs: ['「ねえ、猫だ」とアリスは言った'],
  tokens: [[{start: 4, end: 5, wordId: 1, readingIndex: 0}, {start: 8, end: 11, wordId: 2, readingIndex: 0}]],
  vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', cardState: ['new']},
               {wordId: 2, readingIndex: 0, spelling: 'アリス', reading: 'アリス', cardState: ['known']}]});
__api.light_novel_paired_state = async () => ({...PAIRED});
__api.light_novel_play_paired_at_offset = async () => ({...PAIRED, playing: true, paused: false, player_running: true});
__api.light_novel_bookmark = async (id, chapter, offset) => ({id, current_chapter: chapter, current_offset: offset});
"""
NO_GATE = {"settings": {"review_gate_ln_enabled": False}}

OPEN = r"""
await __settle(openLightNovel(9)); await __tick(300);
const surface = node => { const copy = node.cloneNode(true); copy.querySelectorAll('rt,rp').forEach(n => n.remove()); return copy.textContent; };
const words = () => [...document.querySelectorAll('#lnReader .ln-word')];
"""


def test_unparsed_text_is_read_along_as_words(page) -> None:
    # The gaps before the first token, between tokens and after the last one become words
    # in the same audio coordinates as the dictionary words (so the highlight and "Play
    # from here" reach them); punctuation stays plain, silent text.
    result = page.run(
        OPEN
        + r"""
        const rows = words().map(w => [surface(w), w.classList.contains('ln-word-fallback'),
          Number(w.dataset.lnAudioStart), Number(w.dataset.lnAudioEnd), w.dataset.lnAudioWeights]);
        const paragraph = document.querySelector('#lnReader [data-ln-paragraph="0"]');
        const plain = [...paragraph.childNodes].filter(n => n.nodeType === 3).map(n => n.data);
        const lit = [];
        for (const offset of [1, 3.5, 9]) {
          renderLnPairedPosition({alignment_mode: 'stt', playing: true, ln_chapter_index: 0, position: offset}, offset, {speechActive: true, seekJump: true});
          lit.push(words().filter(w => w.classList.contains('ln-paired-word-current')).map(surface));
        }
        return {rows, plain, text: paragraph.textContent, end: Number(paragraph.dataset.lnAudioEnd), lit};
        """,
        before=UNPARSED,
        state=NO_GATE,
    )
    out = result.value
    assert out["rows"] == [
        ["ねえ", True, 0, 2, "1,1"],
        ["猫", False, 2, 3, "2"],
        ["だ", True, 3, 4, "1"],
        ["と", True, 4, 5, "1"],
        ["アリス", False, 5, 8, "1,1,1"],
        ["は言った", True, 8, 12, "1,1,1,1"],
    ]
    assert out["plain"] == ["「", "、", "」"]
    assert out["text"] == "「ねえ、猫だ」とアリスは言った" and out["end"] == 12
    assert out["lit"] == [["ねえ"], ["だ"], ["は言った"]]


def test_unparsed_word_opens_the_shared_card_with_its_reading_actions(page) -> None:
    # The same study card as dictionary words, with only what makes sense without a Jiten
    # entry: the word, Play from here (listen tone), Read up to here and close — no
    # explanation text, meanings, deck or review buttons, and no Jiten/deck lookups.
    assert not has_text("No Jiten entry") and not has_text("Нет записи Jiten", "ru")
    for lang in ("en", "ru"):
        result = page.run(
            OPEN
            + r"""
            const card = () => document.getElementById('pudgeStudyCard');
            const lookups = since => __apiCalls.slice(since).map(c => c.name).filter(name => /^(study_|jiten|light_novel_decks)/.test(name));
            const out = {labels: [t('ui.playFromHere'), t('ui.readUpToHere')]};
            let mark = __apiCalls.length;
            await __click('#lnReader [data-ln-token="0:fallback:3"]'); await __tick(1000);
            out.card = {open: card().classList.contains('open'), text: card().innerText,
              tones: [...card().querySelectorAll('.pudge-study-header-action')].map(b => b.dataset.pudgeStudyActionTone),
              study: card().querySelectorAll('.pudge-study-meanings,#pudgeStudyDeck,.pudge-study-review-actions,.pudge-study-add').length,
              cards: document.querySelectorAll('.pudge-study-card.open,#lnStudyPop.open').length, lookups: lookups(mark)};
            mark = __apiCalls.length;
            await __click('#pudgeStudyCard [data-pudge-study-extra-action="paired-audio-here"]'); await __tick(100);
            out.played = {open: card().classList.contains('open'), calls: __apiCalls.slice(mark).filter(c => c.name === 'light_novel_play_paired_at_offset').map(c => c.args)};
            await __click('#lnReader [data-ln-token="0:fallback:0"]'); await __tick(300);
            out.second = card().innerText.split('\n')[0];
            await __click('#pudgeStudyCard [data-pudge-study-extra-action="bookmark-here"]'); await __tick(100);
            out.bookmarked = {open: card().classList.contains('open'), toasts: __toasts(), saved: t('ui.bookmarkAndProgressUpdated')};
            await __click('#lnReader [data-ln-token="0:fallback:1"]'); await __tick(300);
            const reopened = card().classList.contains('open');
            await __click('#pudgeStudyCard [data-pudge-study-close]'); await __tick(100);
            out.close = [reopened, card().classList.contains('open')];
            return out;
            """,
            lang=lang,
            before=UNPARSED,
            state=NO_GATE,
        )
        out = result.value
        play, read = out["labels"]
        assert out["card"] == {
            "open": True,
            "text": f"は言った\n{play}\n{read}\n×",
            "tones": ["listen", ""],
            "study": 0,
            "cards": 1,
            "lookups": [],
        }
        # Play from here starts the audiobook at the word (audio character 8).
        assert out["played"] == {"open": False, "calls": [[9, 0, 8, 1]]}
        assert out["second"] == "ねえ"
        [[book, chapter, _offset, source]] = result.calls_of("light_novel_bookmark")
        assert (book, chapter, source) == (9, 0, "manual")
        assert (
            out["bookmarked"]["open"] is False and out["bookmarked"]["saved"] in out["bookmarked"]["toasts"]
        )
        assert out["close"] == [True, False]
