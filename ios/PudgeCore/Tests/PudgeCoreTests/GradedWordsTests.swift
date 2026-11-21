import Foundation
import XCTest
@testable import PudgeCore

/// A word graded in the word card takes its new state everywhere on screen at once, and a due
/// word is Due in every look (subtitle colour, furigana, reader, label) until it is graded.
final class GradedWordsTests: XCTestCase {
    private let dueFlags = 3 | ((4 + 1) << 3) | ((5 + 1) << 7)  // JitenMPV Due, reader "due"
    private let matureFlags = 3 | 4 | ((2 + 1) << 3) | ((3 + 1) << 7)  // JitenMPV Mature, reader "mature"

    private func cue(_ text: String, _ tokens: [StudyToken]) -> SubtitleCue {
        SubtitleCue(start: 0, end: 1000, text: text, tokens: tokens, ruby: nil)
    }

    func testAGradeRecoloursEveryOccurrenceInEveryCue() throws {
        let due = { (start: Int) in StudyToken(start: start, end: start + 1, wordId: 7, readingIndex: 0, state: 2, flags: self.dueFlags) }
        let other = StudyToken(start: 1, end: 2, wordId: 8, readingIndex: 0, state: 2, flags: dueFlags)
        let track = SubtitleTrack(tokenized: true, cues: [cue("猫犬", [due(0), other]), cue("の猫", [due(1)])])
        var graded = GradedWords()
        graded.record([StudyStateRow(wordId: 7, readingIndex: 0, state: 3, flags: matureFlags)])
        XCTAssertEqual(graded.revision, 1)
        let value = track.updating(graded.rows)
        XCTAssertEqual(value.cues[0].tokens[0].jitenState, 2)
        XCTAssertEqual(value.cues[1].tokens[0].jitenState, 2)
        XCTAssertEqual(value.cues[1].tokens[0].wordState, "mature")
        // Another word (or another reading of it) keeps its state.
        XCTAssertEqual(value.cues[0].tokens[1].jitenState, 4)
        let reading = StudyToken(start: 0, end: 1, wordId: 7, readingIndex: 1, state: 2, flags: dueFlags)
        XCTAssertEqual([reading].updating(graded.rows)[0].state, 2)
        // The reader and a looked-up text take it the same way.
        let parsed = ParsedText(paragraphs: [TextParagraph(text: "猫", tokens: [due(0)])]).updating(graded.rows)
        XCTAssertEqual(parsed.paragraphs[0].tokens[0].state, 3)
        // A second grade is a new revision even with the same state (screens apply it again).
        graded.record([StudyStateRow(wordId: 7, readingIndex: 0, state: 3, flags: matureFlags)])
        XCTAssertEqual(graded.revision, 2)
        graded.record([])
        XCTAssertEqual(graded.revision, 2)
    }

    func testADueWordIsDueWhateverElseItIs() throws {
        // An older Mac encoded a mastered-and-due word as JitenMPV Mastered (reader: due).
        let mastered = StudyToken(start: 0, end: 1, wordId: 7, readingIndex: 0, state: 2, flags: 3 | 4 | ((5 + 1) << 3) | ((5 + 1) << 7))
        XCTAssertEqual(mastered.jitenState, 4)
        XCTAssertEqual(ReaderLook().color(for: mastered), ReaderLook.defaultColors["due"])
        // Without the reader's state (oldest Macs) the state code says due.
        let blacklisted = StudyToken(start: 0, end: 1, wordId: 7, readingIndex: 0, state: 2, flags: 3 | ((3 + 1) << 3))
        XCTAssertEqual(blacklisted.jitenState, 4)
        // Not due: the Mac's JitenMPV state stays.
        let plain = StudyToken(start: 0, end: 1, wordId: 7, readingIndex: 0, state: 3, flags: 3 | 4 | ((5 + 1) << 3) | ((4 + 1) << 7))
        XCTAssertEqual(plain.jitenState, 5)
        XCTAssertFalse(plain.isDue)
    }

    func testTheWordCardLabelsADueWordDue() {
        let fallback: (Int) -> String = { $0 == 3 ? "Known" : "New" }
        XCTAssertEqual(WordStateLabel.text(state: 2, macLabel: "Mature", fallback: fallback), "Due")
        XCTAssertEqual(WordStateLabel.text(state: 3, macLabel: "Mastered", fallback: fallback), "Mastered")
        XCTAssertEqual(WordStateLabel.text(state: 3, macLabel: "", fallback: fallback), "Known")
    }

    func testTheMacsGradeAnswerCarriesTheNewState() throws {
        let new = try JSONDecoder().decode(StudyActionResult.self, from: Data(#"{"ok":true,"outcome":"saved","message":"","row":[7,0,3,35]}"#.utf8))
        XCTAssertEqual(new.row?.key, "7:0")
        XCTAssertEqual(new.row?.state, 3)
        let old = try JSONDecoder().decode(StudyActionResult.self, from: Data(#"{"ok":true,"outcome":"saved","message":""}"#.utf8))
        XCTAssertNil(old.row)
    }
}
