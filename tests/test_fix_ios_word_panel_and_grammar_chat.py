"""iPhone/iPad: the word card opens small at the bottom, and "Explain grammar" (LN reader, manga).

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The chat's rules
(what a question sends, what is kept per book, how answers are shown) are pure code in PudgeCore
with XCTest tests (ios/PudgeCore/Tests/PudgeCoreTests/GrammarChatTests.swift, the Mac's answers
in tests/fixtures/iphone_grammar_answers.json); the Mac's side is behaviour-tested in
tests/test_fix_iphone_grammar_chat.py. What only SwiftUI can do is guarded here by reading the
Swift source:

1. The word card, Look up and the grammar chat are BottomPanels (StudyViews.swift), not sheets: a
   sheet covers the whole screen in landscape and is a large card in the middle of an iPad. The
   panel opens compact, drags up to large and down to close, and leaves what is behind usable.
2. A panel lives inside the screen it is over, so its content never uses `dismiss`, a
   NavigationStack, a toolbar or a navigation title (they would close or retitle the reader).
3. "Explain Grammar" is in the reader's selection menu, the word card (its sentence) and a manga
   text box's look-up; answers are shown as text (GrammarText: links open nothing), never HTML.
"""

from __future__ import annotations

import re

from source_policy import source

STUDY = "ios/Pudge/UI/StudyViews.swift"
CHAT = "ios/Pudge/UI/GrammarChat.swift"
READ = "ios/Pudge/UI/ReadScreen.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
PLAYER = "ios/Pudge/UI/PlayerScreen.swift"
TEXT = "ios/Pudge/UI/JapaneseText.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_word_cards_and_look_ups_are_bottom_panels_everywhere() -> None:
    read, watch, player = _flat(READ), _flat(WATCH), _flat(PLAYER)
    for text in (read, watch, player):
        assert not re.search(r"\.sheet\(item: \$(lookup|textLookup)\b", text)
    assert ".bottomPanel(item: $lookup, onDismiss: {" in read  # light novels
    assert '.bottomPanel(item: $lookup, title: "Look up") { item in TextLookupSheet(' in read  # manga
    assert (
        ".bottomPanel(item: $lookup, onDismiss: resume) { item in WordSheet(media: media, lookup: item) }"
        in watch
    )
    assert '.bottomPanel(item: $textLookup, title: "Look up", onDismiss: resume)' in watch
    assert ".bottomPanel(item: $lookup, onDismiss: {" in player  # the read-along
    panel = _flat(STUDY, "struct BottomPanel<Content: View>: View {", "private enum PanelShown")
    # Compact by default, large when dragged up, closed when dragged down; a corner card when wide.
    assert "private var resting: CGFloat { expanded ? tallest : small }" in panel
    assert "if end < small * 0.6 { onClose() } else { expanded = end > (small + tallest) / 2 }" in panel
    assert "private var wide: Bool { space.width >= 600 }" in panel
    assert "alignment: wide ? .bottomTrailing : .bottom" in panel
    assert ".accessibilityAction(.escape) { onClose() }" in panel
    # In landscape on an iPhone the compact panel leaves the video and its subtitles in view.
    assert "space.height < 500 ? space.height * 0.55 : .infinity" in panel
    # Typing in the chat moves the panel, not the reader or the manga pager behind it.
    presenter = _flat(STUDY, "private struct BottomPanelPresenter", "extension View {")
    assert (
        "content .ignoresSafeArea(.keyboard, edges: keyboard ? .bottom : Edge.Set()) .overlay {" in presenter
    )


def test_panel_contents_never_act_on_the_screen_behind_them() -> None:
    contents = {
        "word card and look-up": _flat(STUDY, "struct WordDetailView", "/// Cover image served by the Mac"),
        "grammar chat": _flat(CHAT),
    }
    for name, text in contents.items():
        for forbidden in (
            "NavigationStack",
            "@Environment(\\.dismiss)",
            ".navigationTitle(",
            ".toolbar",
            ".preferredColorScheme(",
            ".presentationDetents(",
        ):
            assert forbidden not in text, (name, forbidden)


def test_explain_grammar_is_offered_in_the_reader_the_word_card_and_manga() -> None:
    menu = _flat(
        TEXT,
        "func editMenuInteraction(_ interaction: UIEditMenuInteraction, menuFor",
        "func editMenuInteraction(",
    )
    assert "explain: onExplain != nil" in menu
    assert 'case .explain: actions.append(UIAction(title: "Explain Grammar"' in menu
    assert "self?.clear(); self?.onExplain?(selected)" in menu
    assert "onExplain = nil" in _flat(TEXT, "func forgetActions()", "func textChanged(")
    read = _flat(READ)
    assert "textSelection.onExplain = { range in interacted(); explain(selection: range) }" in read
    assert "GrammarContext.preceding(paragraph, before: range.start)" in read
    assert "WordSheet(media: media, lookup: item, onExplain: explainAction(lookupSource))" in read
    assert "TextLookupSheet(media: media, lookup: item, onExplain: { text in explainBubble(text) })" in read
    assert (
        read.count(
            '.bottomPanel(isPresented: $grammar.shown, title: "Explain grammar", compact: 380, keyboard: true) { GrammarChatPanel(chats: grammar, media: media) }'
        )
        == 2
    )
    # A word or bubble tapped while the chat is open shows on top of it (applied after it);
    # the chat's button closes the word card first.
    novel = _flat(READ, "struct NovelReaderScreen", "struct MangaReaderScreen")
    manga = _flat(READ, "struct MangaReaderScreen", "struct MangaPageView")
    for screen in (novel, manga):
        assert screen.index(".bottomPanel(isPresented: $grammar.shown") < screen.index(
            ".bottomPanel(item: $lookup"
        )
    # Hidden, the chat comes back with a button; each volume has its own chats on the phone.
    assert read.count("GrammarChatButton(chats: grammar) { lookup = nil }") == 2
    # A bubble's chat opens at once; the page text is gathered while it shows "Explaining…".
    assert "grammar.explain(GrammarSource(sentence: text), media: media) {" in manga
    assert (
        'grammar.open(book: GrammarChat.book(server: media.app.paired?.serverId, kind: "ln", id: bookId))'
        in read
    )
    assert (
        'grammar.open(book: GrammarChat.book(server: media.app.paired?.serverId, kind: "manga", id: book.id))'
        in read
    )
    study = _flat(STUDY, "struct WordDetailView", "struct WordSheet")
    assert 'Section("Sentence") { Text(lookup.sentence).textSelection(.enabled) if let onExplain {' in study


def test_the_chat_asks_the_macs_routes_and_shows_answers_as_text() -> None:
    chat = _flat(CHAT)
    assert 'client.media("grammar/explain", post: request, as: GrammarJob.self)' in chat
    assert 'client.media("grammar/ask", post: request, as: GrammarJob.self)' in chat
    assert 'client.media("grammar/jobs/\\(job)?wait=1", as: GrammarJob.self)' in chat
    # IOS6: answers are drawn from their Markdown (ChatMarkdownView, the parser in PudgeCore's
    # ChatMarkdown with its own tests), still never HTML.
    assert "ChatMarkdownView(text: message.text)" in chat
    assert "ChatMarkdown.inline(value)" in chat
    for forbidden in ("WKWebView", "import WebKit", "DocumentType.html", "AttributedString(markdown:"):
        assert forbidden not in chat


def test_furigana_takes_its_words_colour_and_dimmed_words_keep_a_solid_outline() -> None:
    # Maintainer: furigana in the colour of its word (subtitles, colour mode); words without a
    # state colour keep the furigana colour they had. Colours and opacities are exactly the Mac's
    # (no lifting); dimmed JitenMPV words keep a solid outline, as in mpv (the theme's opacity is
    # taken as the text's alone, ASS \\1a).
    text = _flat(TEXT)
    assert "let ownColor = owner.flatMap { paragraph.tokens[safe: $0] }.flatMap { wordColor($0) }" in text
    assert (
        "let rubyColor = outline ? UIColor.clear.cgColor : ownColor ?? style.text.withAlphaComponent(0.85).cgColor"
        in text
    )
    colour = _flat(TEXT, "private func wordColor(_ token: StudyToken) -> CGColor? {", "private func clamp(")
    assert "if let look = style.subtitleLook { return style.subtitleFill(look.style(token)) }" in colour
    assert "return style.marks == .color ? style.color(for: token).cgColor : nil" in colour
    assert (
        "result.addAttribute(strokeColorKey, value: Self.color(word.outline, opacity: nil), range: range)"
        in text
    )
    assert "result.addAttribute(colorKey, value: style.subtitleFill(word), range: range)" in text
    fill = _flat(TEXT, "func subtitleFill(_ word: JitenWordStyle) -> CGColor {", "\n}")
    assert (
        "(UIColor(hex: word.text) ?? .white).withAlphaComponent(CGFloat(min(255, max(0, word.opacity ?? 255))) / 255)"
        in fill
    )
    assert (
        "func stateColor(_ token: StudyToken) -> UIColor { look.color(for: token).flatMap { UIColor(hex: $0) } ?? text }"
        in text
    )
    assert "WordContrast" not in text


def test_the_word_card_shows_the_frequency_rank_in_its_compact_part() -> None:
    card = _flat(STUDY, "struct WordDetailView", "struct WordSheet")
    body = card[card.index("var body: some View") :]
    rank = body.index("if let frequency = WordFrequency.label(detail?.frequencyRank) {")
    # Next to the state, above the grades: in the compact panel.
    assert body.index("WordStateLabel.text(state: state") < rank < body.index("ForEach(options.grades)")
