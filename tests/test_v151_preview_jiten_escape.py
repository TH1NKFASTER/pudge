"""Cover preview closes on clicks outside the image as it is drawn now, Jiten word cards put
furigana only over kanji, Escape leaves library multi-select before clearing a text
selection (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

# A cover on the page; the preview's high-resolution answer is held until the test answers it.
COVER = """
const MakeImage = window.Image;
window.Image = function Image() {
  const img = MakeImage();
  img.decode = async () => { img.__naturalWidth = 1600; img.__naturalHeight = 2400; };
  return img;
};
window.__pending = new Map();
__api.cover_preview_resolve = (kind, id) => new Promise(resolve => window.__pending.set(Number(id), resolve));
"""


def test_cover_preview_closes_on_clicks_outside_the_zoomed_image(page) -> None:
    result = page.run(
        """
        const shelf = document.createElement('div');
        shelf.innerHTML = '<div class="cover-shell"><img id="cover" src="thumb.jpg" data-pudge-cover-kind="manga" data-pudge-cover-id="7"></div>';
        document.body.appendChild(shelf);
        const open = async () => {
          PudgeCoverPreview.open(document.querySelector('#cover'));
          await __tick(20);
          window.__pending.get(7)({preview_url: 'full.jpg', source_kind: 'local_page', source_revision: 'r'});
          await __tick(20);
          // Zoomed out to half: the image is drawn smaller than its layout box (the card).
          await __fire('.pudge-cover-preview img', 'wheel', {ctrlKey: true, deltaY: 100, clientX: 650, clientY: 450}, 'WheelEvent');
          document.querySelector('.pudge-cover-preview img').__rect = {x: 512, y: 243, width: 276, height: 414};
        };
        const out = {};
        await open();
        out.zoom = document.querySelector('.pudge-cover-preview img').style.transform;
        await __click('.pudge-cover-preview img', {clientX: 650, clientY: 450});
        out.onImage = PudgeCoverPreview.isOpen();
        await __click('.pudge-cover-preview-card', {clientX: 700, clientY: 600});     // card, but over the image
        out.cardOverImage = PudgeCoverPreview.isOpen();
        await __click('.pudge-cover-preview-card', {clientX: 400, clientY: 100});     // card, outside the drawn image
        out.cardOutside = PudgeCoverPreview.isOpen();
        await open();
        await __click('.pudge-cover-preview', {clientX: 30, clientY: 30});            // the backdrop
        out.backdrop = PudgeCoverPreview.isOpen();
        return out;
        """,
        before=COVER,
    )
    out = result.value
    assert out["zoom"].endswith("scale(0.5)")
    # Clicks on the drawn image keep the preview; anything else closes it, also the
    # transparent card around a zoomed-out image (the old check was "target is the backdrop").
    assert (out["onImage"], out["cardOverImage"]) == (True, True)
    assert (out["cardOutside"], out["backdrop"]) == (False, False)


def test_jiten_word_card_puts_furigana_only_over_kanji(page) -> None:
    result = page.run(
        """
        const words = [
          {text: '間もない', card: {spelling: '間もない', reading: 'まもない'}},
          {text: '食べ物', token: {reading: 'たべもの'}, card: {spelling: '食べ物'}},     // the reading comes with the token
          {text: '食べ物', card: {spelling: '食べ物', reading: '食[た]べ物[もの]'}},       // Jiten bracket furigana
          {text: 'すこし', card: {spelling: 'すこし', reading: 'すこし'}},
        ];
        let pos = 0;
        const tokens = words.map((w, i) => { const start = pos; pos += w.text.length;
          return {wordId: i + 1, readingIndex: 0, start, end: pos, card: w.card, ...(w.token || {})}; });
        const box = document.createElement('div');
        box.innerHTML = PudgeReadingTools.study.renderParsedText(
          {paragraphs: [words.map(w => w.text).join('')], tokens: [tokens], vocabulary: []}, {backend: 'jiten'});
        document.body.appendChild(box);
        const terms = [];
        for (const node of box.querySelectorAll('[data-pudge-study-token]')) {
          await __click(`[data-pudge-study-token="${node.dataset.pudgeStudyToken}"]`);
          await __tick(10);
          const term = document.querySelector('#pudgeStudyCard .pudge-study-term > span');
          terms.push([...term.childNodes].map(n => n.nodeType === 3 ? n.textContent : [n.querySelector('span').textContent, n.querySelector('rt').textContent]));
          await __click('[data-pudge-study-close]');
        }
        return terms;
        """,
    )
    assert result.value == [
        [["間", "ま"], "もない"],  # the old whole-word ruby put もない under the same <rt>
        [["食", "た"], "べ", ["物", "もの"]],
        [["食", "た"], "べ", ["物", "もの"]],
        ["すこし"],
    ]


LIBRARIES = {
    "light_novel_state": {
        "version": 1,
        "settings": {},
        "books": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
    },
    "manga_state": {"books": [{"id": 3, "title": "Manga", "page_count": 3}], "ocr_available": False},
    "audiobook_state": {"books": [{"id": 4, "title": "Audio"}]},
}


def test_escape_leaves_library_multiselect_before_clearing_text_selection(page) -> None:
    result = page.run(
        """
        const selected = {
          lightnovels: () => ui.lnSelection.size,
          manga: () => PudgeMangaReaderV2.selectedBookIds().length,
          audiobooks: () => PudgeAudiobookSelection.selectedBookIds().length,
        };
        const escape = type => window.dispatchEvent(new KeyboardEvent(type, {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}));
        const textSelected = () => !window.getSelection().isCollapsed;
        const out = {};
        for (const name of Object.keys(selected)) {
          setPage(name);
          await __tick(300);
          await __key('a', {metaKey: true}, 'body');                   // Cmd+A selects every card...
          const range = document.createRange();                        // ...and text ends up selected too
          range.selectNodeContents(document.querySelector('.page.active'));
          window.getSelection().addRange(range);
          const before = {cards: selected[name](), text: textSelected()};
          escape('keydown'); await __tick(20);
          const down = {cards: selected[name](), text: textSelected()};
          escape('keyup'); await __tick(20);
          out[name] = {before, down, up: {cards: selected[name](), text: textSelected()}};
        }
        return out;
        """,
        api=LIBRARIES,
    )
    cards = {"lightnovels": 2, "manga": 1, "audiobooks": 1}
    for name, out in result.value.items():
        assert out["before"] == {"cards": cards[name], "text": True}, name
        # The key press leaves multi-select first; the text selection goes with the release.
        assert out["down"] == {"cards": 0, "text": True}, name
        assert out["up"] == {"cards": 0, "text": False}, name
