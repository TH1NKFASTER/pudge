import XCTest
@testable import PudgeCore

/// Text selection in the iPhone/iPad reader and subtitles (`swift test` in ios/PudgeCore on the Mac).
final class TextSelectionTests: XCTestCase {
    private func place(_ paragraph: Int, _ offset: Int) -> TextPosition { TextPosition(paragraph: paragraph, offset: offset) }

    private func word(_ start: Int, _ end: Int, entry: Bool = true) -> StudyToken {
        StudyToken(start: start, end: end, wordId: entry ? 100 + start : 0, readingIndex: 0, state: 0)
    }

    /// A chapter: text, an illustration, text, text.
    private let chapter = [
        TextParagraph(text: "一二三四", tokens: []),
        TextParagraph(text: "", image: true, tokens: []),
        TextParagraph(text: "五六七", tokens: []),
        TextParagraph(text: "八九", tokens: []),
    ]
    private func paragraph(_ index: Int) -> TextParagraph? { chapter.indices.contains(index) ? chapter[index] : nil }
    private func length(_ index: Int) -> Int {
        guard let item = paragraph(index), !item.image else { return 0 }
        return item.text.utf16.count
    }

    func testEitherOrderGivesTheSameRangeAndEachParagraphItsPart() {
        let range = TextSelectionRange(place(3, 2), place(1, 3))
        XCTAssertEqual(range.start, place(1, 3))
        XCTAssertEqual(range.end, place(3, 2))
        XCTAssertEqual(range, TextSelectionRange(place(1, 3), place(3, 2)))
        XCTAssertNil(range.part(ofParagraph: 0, length: 10))
        XCTAssertEqual(range.part(ofParagraph: 1, length: 10), 3..<10)
        XCTAssertEqual(range.part(ofParagraph: 2, length: 4), 0..<4)
        XCTAssertEqual(range.part(ofParagraph: 3, length: 6), 0..<2)
        XCTAssertNil(range.part(ofParagraph: 4, length: 6))
        // Nothing of a paragraph the start sits at the very end of.
        XCTAssertNil(range.part(ofParagraph: 1, length: 3))
        XCTAssertEqual(TextSelectionRange(paragraph: 2, range: 1..<3), TextSelectionRange(place(2, 1), place(2, 3)))
        XCTAssertTrue(TextSelectionRange(place(2, 1), place(2, 1)).isEmpty)
    }

    func testCopyKeepsParagraphBreaksAndSkipsIllustrations() {
        let range = TextSelectionRange(place(0, 2), place(3, 1))
        XCTAssertEqual(range.text(paragraph), "三四\n五六七\n八")
        XCTAssertEqual(TextSelectionRange(paragraph: 0, range: 1..<3).text(paragraph), "二三")
        // One subtitle line with a line break in it: copied as it is.
        let line = TextParagraph(text: "おはよう\nございます", tokens: [])
        XCTAssertEqual(TextSelectionRange(paragraph: 0, range: 2..<7).text { $0 == 0 ? line : nil }, "よう\nござ")
    }

    func testEndsOnAParagraphEdgeMoveIntoWhatTheySelect() {
        // Start after the first paragraph's last character, end before the last one's first:
        // past the illustration, only the third paragraph is selected.
        XCTAssertEqual(TextSelectionRange(place(0, 4), place(3, 0)).normalized(length: length),
                       TextSelectionRange(place(2, 0), place(2, 3)))
        // Offsets out of their paragraph are clamped.
        XCTAssertEqual(TextSelectionRange(place(0, -3), place(0, 99)).normalized(length: length),
                       TextSelectionRange(place(0, 0), place(0, 4)))
        // Nothing left: nil.
        XCTAssertNil(TextSelectionRange(place(0, 4), place(2, 0)).normalized(length: length))
        XCTAssertNil(TextSelectionRange(place(2, 1), place(2, 1)).normalized(length: length))
        // Already inside: unchanged.
        let inside = TextSelectionRange(place(0, 1), place(3, 1))
        XCTAssertEqual(inside.normalized(length: length), inside)
    }

    func testDraggingAHandleCrossesParagraphsAndSwapsPastTheOtherEnd() {
        let range = TextSelectionRange(place(1, 2), place(1, 5))
        // The end handle dragged down into a later paragraph; the start stays.
        let down = range.moving(.end, to: place(3, 1))
        XCTAssertEqual(down.range, TextSelectionRange(place(1, 2), place(3, 1)))
        XCTAssertEqual(down.handle, .end)
        // The start handle dragged up into an earlier paragraph.
        let up = range.moving(.start, to: place(0, 7))
        XCTAssertEqual(up.range, TextSelectionRange(place(0, 7), place(1, 5)))
        XCTAssertEqual(up.handle, .start)
        // The end dragged above the start: the handles swap, the old start becomes the end.
        let swapped = range.moving(.end, to: place(0, 1))
        XCTAssertEqual(swapped.range, TextSelectionRange(place(0, 1), place(1, 2)))
        XCTAssertEqual(swapped.handle, .start)
        // Onto the other end itself: nothing changes, never an empty selection.
        let same = range.moving(.start, to: place(1, 5))
        XCTAssertEqual(same.range, range)
        XCTAssertEqual(same.handle, .start)
        // Dragged to the edge of the next paragraph and normalised: the end of the paragraph it was in.
        let edge = TextSelectionRange(place(0, 1), place(0, 2)).moving(.end, to: place(2, 0))
        XCTAssertEqual(edge.range.normalized(length: length), TextSelectionRange(place(0, 1), place(0, 4)))
    }

    func testALongPressGrowsByWholeWordsOverParagraphs() {
        let anchor = TextSelectionRange(paragraph: 2, range: 3..<5)
        XCTAssertEqual(anchor.union(TextSelectionRange(paragraph: 4, range: 0..<2)), TextSelectionRange(place(2, 3), place(4, 2)))
        XCTAssertEqual(anchor.union(TextSelectionRange(paragraph: 1, range: 6..<8)), TextSelectionRange(place(1, 6), place(2, 5)))
        XCTAssertEqual(anchor.union(TextSelectionRange(paragraph: 2, range: 4..<5)), anchor)
    }

    func testTwoTapsSelectTheWordOrARunOfOneKind() {
        // 今日はラーメンを食べた、ABC12です。 with words 今日 and 食べた.
        let text = TextParagraph(text: "今日はラーメンを食べた、ABC12です。", tokens: [word(0, 2), word(8, 11)])
        XCTAssertEqual(TextUnits.word(text, at: 1), 0..<2)
        XCTAssertEqual(TextUnits.word(text, at: 9), 8..<11)
        XCTAssertEqual(TextUnits.word(text, at: 2), 2..<3)      // は between a word and katakana
        XCTAssertEqual(TextUnits.word(text, at: 4), 3..<7)      // ラーメン, ー goes with the katakana
        XCTAssertEqual(TextUnits.word(text, at: 7), 7..<8)      // を
        XCTAssertEqual(TextUnits.word(text, at: 11), 11..<12)   // 、
        XCTAssertEqual(TextUnits.word(text, at: 15), 12..<17)   // ABC12
        XCTAssertEqual(TextUnits.word(text, at: 18), 17..<19)   // です
        XCTAssertNil(TextUnits.word(text, at: 20))
        XCTAssertNil(TextUnits.word(text, at: -1))
        // A word without a dictionary entry is still a word.
        let plain = TextParagraph(text: "そして", tokens: [word(0, 3, entry: false)])
        XCTAssertEqual(TextUnits.word(plain, at: 1), 0..<3)
        // A kanji outside the Basic Multilingual Plane is one character (two UTF-16 units).
        let wide = TextParagraph(text: "𠮟る", tokens: [])
        XCTAssertEqual(TextUnits.word(wide, at: 1), 0..<2)
        XCTAssertEqual(TextUnits.word(wide, at: 2), 2..<3)
        // A long press: the word, or one character outside words.
        XCTAssertEqual(TextUnits.unit(text, at: 9), 8..<11)
        XCTAssertEqual(TextUnits.unit(text, at: 4), 4..<5)
        XCTAssertEqual(TextUnits.unit(wide, at: 0), 0..<2)
        XCTAssertEqual(TextUnits.character("か\u{3099}", at: 1), 0..<2)  // a combining sound mark stays with its kana
    }

    func testThreeTapsSelectTheSentenceInTheReaderAndTheWholeLineInSubtitles() {
        let text = "「おはよう。」と言った。\nまた明日！？\u{3000}じゃあね"
        let paragraph = TextParagraph(text: text, tokens: [])
        XCTAssertEqual(TextUnits.sentence(text, at: 2), 0..<7)    // 「おはよう。」 with its closing bracket
        XCTAssertEqual(TextUnits.sentence(text, at: 5), 0..<7)    // on the stop itself
        XCTAssertEqual(TextUnits.sentence(text, at: 8), 7..<12)   // と言った。 without the line break
        XCTAssertEqual(TextUnits.sentence(text, at: 15), 13..<19) // また明日！？
        XCTAssertEqual(TextUnits.sentence(text, at: 22), 20..<24) // じゃあね without the space before it
        XCTAssertNil(TextUnits.sentence("", at: 0))
        // The same stops as the sentence sent with Add/Review.
        let plain = "おはよう。と言った。"
        XCTAssertEqual(TextUnits.sentence(plain, at: 6), 5..<10)
        XCTAssertEqual(StudyText.sentence(plain, around: StudyToken(start: 6, end: 8, wordId: 1, readingIndex: 0, state: 0)), "と言った。")
        XCTAssertEqual(TextUnits.taps(3, in: paragraph, at: 8, triple: .sentence), 7..<12)
        XCTAssertEqual(TextUnits.taps(3, in: paragraph, at: 8, triple: .paragraph), 0..<24)
        XCTAssertEqual(TextUnits.taps(2, in: paragraph, at: 8, triple: .paragraph), 8..<9)  // 言
        XCTAssertEqual(TextUnits.whole(" 字幕\n "), 1..<3)
        XCTAssertNil(TextUnits.whole(" \n"))
    }

    func testTheMenuLooksUpTheFirstWordWithAnEntryAndStartsAtTheSelectionStart() {
        let paragraphs = [
            TextParagraph(text: "あいう", tokens: [word(0, 1, entry: false)]),
            TextParagraph(text: "", image: true, tokens: []),
            TextParagraph(text: "かきく", tokens: [word(0, 1), word(1, 2)]),
        ]
        let lookup = { (index: Int) -> TextParagraph? in paragraphs.indices.contains(index) ? paragraphs[index] : nil }
        let range = TextSelectionRange(place(0, 1), place(2, 2))
        XCTAssertEqual(range.actions(lookup, listen: true, markHere: true),
                       [.copy, .lookUp(TextWord(paragraph: 2, token: 0)), .listen(place(0, 1)), .markHere(place(0, 1))])
        // A word that ends where the selection starts does not count.
        XCTAssertEqual(TextSelectionRange(place(2, 1), place(2, 3)).firstWord(lookup), TextWord(paragraph: 2, token: 1))
        XCTAssertEqual(TextSelectionRange(place(0, 0), place(0, 3)).actions(lookup, listen: false, markHere: false), [.copy])
        XCTAssertEqual(range.text(lookup), "いう\nかき")
    }

    func testTapsInARowCountWithoutWaiting() {
        var taps = TapSequence()
        XCTAssertFalse(taps.touchBegan(time: 0, x: 10, y: 10))
        XCTAssertEqual(taps.tapEnded(time: 0.05, x: 10, y: 10), 1)
        XCTAssertTrue(taps.touchBegan(time: 0.2, x: 14, y: 12))     // drops the waiting lookup
        XCTAssertEqual(taps.tapEnded(time: 0.25, x: 14, y: 12), 2)
        XCTAssertTrue(taps.touchBegan(time: 0.45, x: 12, y: 15))
        XCTAssertEqual(taps.tapEnded(time: 0.5, x: 12, y: 15), 3)
        XCTAssertTrue(taps.touchBegan(time: 0.6, x: 12, y: 15))
        XCTAssertEqual(taps.tapEnded(time: 0.65, x: 12, y: 15), 1)   // a fourth tap starts again
        XCTAssertFalse(taps.touchBegan(time: 2, x: 12, y: 15))      // too late
        XCTAssertEqual(taps.tapEnded(time: 2.05, x: 12, y: 15), 1)
        XCTAssertFalse(taps.touchBegan(time: 2.1, x: 200, y: 15))   // too far
        XCTAssertEqual(taps.tapEnded(time: 2.15, x: 200, y: 15), 1)
        taps.reset()                                                 // the lookup ran
        XCTAssertFalse(taps.touchBegan(time: 2.2, x: 200, y: 15))
        XCTAssertLessThanOrEqual(TapSequence.singleTapDelay, 0.25)
    }

    func testATouchThatContinuesTheRowIsATapNotAHandle() {
        var taps = TapSequence()
        XCTAssertFalse(taps.wouldContinue(time: 0, x: 10, y: 10))   // nothing tapped yet
        _ = taps.tapEnded(time: 0.05, x: 10, y: 10)
        XCTAssertTrue(taps.wouldContinue(time: 0.2, x: 14, y: 12))  // the third tap of a triple tap
        XCTAssertFalse(taps.wouldContinue(time: 0.5, x: 14, y: 12)) // later: a handle can be taken
        XCTAssertFalse(taps.wouldContinue(time: 0.2, x: 200, y: 10))
        XCTAssertTrue(taps.touchBegan(time: 0.2, x: 14, y: 12))     // asking changed nothing
        // A finger elsewhere does not break the row...
        var row = TapSequence()
        _ = row.tapEnded(time: 0.05, x: 10, y: 10)
        XCTAssertFalse(row.touchBegan(time: 0.1, x: 300, y: 300))
        XCTAssertTrue(row.touchBegan(time: 0.2, x: 12, y: 12))
        XCTAssertEqual(row.tapEnded(time: 0.25, x: 12, y: 12), 2)
        // ...and a touch near the last tap that turned into a scroll never makes a far tap a double tap.
        var scrolled = TapSequence()
        _ = scrolled.tapEnded(time: 0.05, x: 10, y: 10)
        XCTAssertTrue(scrolled.touchBegan(time: 0.1, x: 12, y: 12))
        XCTAssertFalse(scrolled.touchBegan(time: 0.2, x: 300, y: 300))
        XCTAssertEqual(scrolled.tapEnded(time: 0.25, x: 300, y: 300), 1)
    }

    func testOnlyParagraphsWhosePartOrHandlesMovedRedraw() {
        let before = TextSelectionRange(place(1, 2), place(3, 4))
        // The end moved inside paragraph 3: only 3 changes; 1 and 2 look the same.
        let after = TextSelectionRange(place(1, 2), place(3, 6))
        XCTAssertTrue(TextSelectionRange.changes(before, after, paragraph: 3, length: 10))
        XCTAssertFalse(TextSelectionRange.changes(before, after, paragraph: 2, length: 10))
        XCTAssertFalse(TextSelectionRange.changes(before, after, paragraph: 1, length: 10))
        XCTAssertFalse(TextSelectionRange.changes(before, after, paragraph: 0, length: 10))
        // The start dragged up into paragraph 0: 0 and 1 change.
        let up = TextSelectionRange(place(0, 9), place(3, 4))
        XCTAssertTrue(TextSelectionRange.changes(before, up, paragraph: 1, length: 10))
        XCTAssertTrue(TextSelectionRange.changes(before, up, paragraph: 0, length: 10))
        // A paragraph selected from its start in both, but its start handle went away: redraws.
        let whole = TextSelectionRange(place(1, 0), place(3, 4))
        let earlier = TextSelectionRange(place(0, 5), place(3, 4))
        XCTAssertTrue(TextSelectionRange.changes(whole, earlier, paragraph: 1, length: 10))
        // Nothing selected before and nothing of this paragraph after: no redraw.
        XCTAssertFalse(TextSelectionRange.changes(nil, after, paragraph: 5, length: 10))
        XCTAssertTrue(TextSelectionRange.changes(nil, after, paragraph: 2, length: 10))
        XCTAssertTrue(TextSelectionRange.changes(after, nil, paragraph: 3, length: 10))
    }

    func testTheReaderScrollsWhileAHandleIsNearItsEdges() {
        XCTAssertEqual(TextSelectionAutoscroll.step(y: 300, top: 0, bottom: 600), 0)
        XCTAssertLessThan(TextSelectionAutoscroll.step(y: 10, top: 0, bottom: 600), 0)
        XCTAssertGreaterThan(TextSelectionAutoscroll.step(y: 590, top: 0, bottom: 600), 0)
        XCTAssertGreaterThan(abs(TextSelectionAutoscroll.step(y: 5, top: 0, bottom: 600)),
                             abs(TextSelectionAutoscroll.step(y: 40, top: 0, bottom: 600)))
        XCTAssertEqual(TextSelectionAutoscroll.step(y: -50, top: 0, bottom: 600), -16)
        XCTAssertEqual(TextSelectionAutoscroll.step(y: 900, top: 0, bottom: 600), 16)
        XCTAssertEqual(TextSelectionAutoscroll.step(y: 5, top: 0, bottom: 80), 0)  // too short to tell the edges apart
    }

    func testAFingerBetweenParagraphsGoesToTheNearestOne() {
        let spans: [(paragraph: Int, top: Double, bottom: Double)] = [(paragraph: 6, top: 260, bottom: 400),
                                                                      (paragraph: 3, top: 0, bottom: 100),
                                                                      (paragraph: 4, top: 112, bottom: 200)]
        XCTAssertEqual(TextSelectionGeometry.nearest(spans, y: 50), 3)
        XCTAssertEqual(TextSelectionGeometry.nearest(spans, y: 105), 3)
        XCTAssertEqual(TextSelectionGeometry.nearest(spans, y: 110), 4)
        XCTAssertEqual(TextSelectionGeometry.nearest(spans, y: 230), 4)   // as near as 6: the earlier one
        XCTAssertEqual(TextSelectionGeometry.nearest(spans, y: 1000), 6)
        XCTAssertEqual(TextSelectionGeometry.nearest(spans, y: -20), 3)
        XCTAssertNil(TextSelectionGeometry.nearest([], y: 0))
    }

    /// An iPad in landscape: the reader's column (780 pt) centred in a 1180 pt window.
    func testNearestParagraphInTwoDirections() {
        typealias Span = TextSelectionGeometry.Span
        let column = [Span(paragraph: 0, minX: 200, minY: 0, maxX: 980, maxY: 100),
                      Span(paragraph: 1, minX: 200, minY: 112, maxX: 980, maxY: 300)]
        XCTAssertEqual(TextSelectionGeometry.nearest(column, x: 500, y: 50), 0)
        XCTAssertEqual(TextSelectionGeometry.nearest(column, x: 500, y: 200), 1)
        XCTAssertEqual(TextSelectionGeometry.nearest(column, x: 20, y: 200), 1)      // the left margin
        XCTAssertEqual(TextSelectionGeometry.nearest(column, x: 1150, y: 60), 0)     // the right margin
        XCTAssertEqual(TextSelectionGeometry.nearest(column, x: 500, y: 106), 0)     // between: as near, the earlier
        XCTAssertEqual(TextSelectionGeometry.nearest(column, x: 500, y: 2000), 1)
        // Two columns side by side: the one under the finger, not the one with the same height.
        let columns = [Span(paragraph: 4, minX: 0, minY: 0, maxX: 500, maxY: 400),
                       Span(paragraph: 9, minX: 600, minY: 0, maxX: 1100, maxY: 400)]
        XCTAssertEqual(TextSelectionGeometry.nearest(columns, x: 700, y: 100), 9)
        XCTAssertEqual(TextSelectionGeometry.nearest(columns, x: 100, y: 100), 4)
        XCTAssertEqual(TextSelectionGeometry.nearest(columns, x: 540, y: 100), 4)
        XCTAssertEqual(TextSelectionGeometry.nearest(columns, x: 560, y: 100), 9)
        // An empty or inverted frame (a view not laid out) is never chosen.
        XCTAssertEqual(TextSelectionGeometry.nearest([Span(paragraph: 0, minX: 10, minY: 10, maxX: 0, maxY: 0),
                                                      Span(paragraph: 2, minX: 0, minY: 500, maxX: 10, maxY: 600)], x: 5, y: 5), 2)
        XCTAssertNil(TextSelectionGeometry.nearest([Span](), x: 0, y: 0))
    }

    func testHandleTargetsAreAtLeast44Points() {
        let small = TextSelectionGeometry.handleTarget(minX: 100, minY: 200, width: 11, height: 30)
        XCTAssertEqual(small.minX, 83.5)
        XCTAssertEqual(small.minY, 193)
        XCTAssertEqual(small.width, 44)
        XCTAssertEqual(small.height, 44)
        let tall = TextSelectionGeometry.handleTarget(minX: 0, minY: 0, width: 11, height: 60)
        XCTAssertEqual(tall.minY, 0)
        XCTAssertEqual(tall.height, 60)
        XCTAssertEqual(tall.width, 44)
    }
}
