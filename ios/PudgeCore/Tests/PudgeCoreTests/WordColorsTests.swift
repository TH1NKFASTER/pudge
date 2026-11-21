import Foundation
import XCTest
@testable import PudgeCore

/// Word states and colours on the iPhone/iPad (`swift test` in ios/PudgeCore on the Mac): the
/// Mac's tokens of every state (tests/fixtures/iphone_word_states.json, kept equal to what the
/// Mac sends by tests/test_fix_iphone_word_state_colours.py) decode to the state the Mac means
/// and take exactly the Mac's colour and JitenMPV style; the word card's frequency label.
final class WordColorsTests: XCTestCase {
    private struct Case: Decodable { let name: String; let token: StudyToken; let state: Int; let wordState: String?; let jitenState: String }
    private struct Style: Decodable { let text: String; let opacity: Int }
    private struct Table: Decodable {
        let cases: [Case]
        let readerColors: [String: String]
        let jitenStates: [String]
        let themes: [String: [Style]]
        let defaultLook: [Style]
    }

    private func table() throws -> Table {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("tests/fixtures/iphone_word_states.json"))
        return try JSONDecoder().decode(Table.self, from: data)
    }

    func testEveryStateTheMacSendsIsTheStateItMeans() throws {
        let table = try table()
        XCTAssertGreaterThan(table.cases.count, 15)
        for item in table.cases {
            XCTAssertEqual(item.token.wordState, item.wordState, item.name)
            XCTAssertEqual(table.jitenStates[item.token.jitenState], item.jitenState, item.name)
            XCTAssertEqual(item.token.state, item.state, item.name)
        }
        // A mature word that is blacklisted is blacklisted everywhere; "known" is mature.
        let named = Dictionary(uniqueKeysWithValues: table.cases.map { ($0.name, $0) })
        XCTAssertEqual(named["mature and blacklisted"]?.token.wordState, "blacklisted")
        XCTAssertEqual(named["known"]?.token.wordState, "mature")
        XCTAssertEqual(named["known"].map { table.jitenStates[$0.token.jitenState] }, "mature")
    }

    func testTheReaderShowsTheMacsColourOfEachState() throws {
        let table = try table()
        let look = ReaderLook(colors: table.readerColors)
        for item in table.cases {
            guard let state = item.wordState else { continue }
            XCTAssertEqual(look.color(for: item.token), table.readerColors[state], item.name)
        }
        for state in ReaderLook.wordStates {
            XCTAssertEqual(table.readerColors[state], ReaderLook.defaultColors[state], state)
        }
    }

    func testSubtitleWordsTakeTheirJitenMPVStyleUnchanged() throws {
        let table = try table()
        XCTAssertEqual(table.defaultLook.map(\.text), table.themes["Default"]?.map(\.text))
        // ##"…"##: the colours' "# would end a #"…"# string.
        let states = table.defaultLook.map { ##"{"text":"\##($0.text)","outline":"#000000","outlineSize":3,"opacity":\##($0.opacity)}"## }
        let json = ##"{"source":"default","theme":"Default","fontFamily":"Hiragino Sans","fontSize":48,"borderSize":3,"unparsed":{"text":"#eeeeee","outline":"#000000","outlineSize":3},"states":["##
            + states.joined(separator: ",") + "]}"
        let look = try JSONDecoder().decode(SubtitleLook.self, from: Data(json.utf8))
        for item in table.cases {
            let index = try XCTUnwrap(table.jitenStates.firstIndex(of: item.jitenState), item.name)
            XCTAssertEqual(look.style(item.token).text, table.defaultLook[index].text, item.name)
            XCTAssertEqual(look.style(item.token).opacity, table.defaultLook[index].opacity, item.name)
        }
        // JitenMPV's Default theme dims Blacklisted, Redundant and Suspended (the phone keeps
        // their outline solid, JapaneseText.swift).
        for state in ["blacklisted", "redundant", "suspended"] {
            let index = try XCTUnwrap(table.jitenStates.firstIndex(of: state))
            XCTAssertEqual(table.defaultLook[index].text, "#969696", state)
            XCTAssertEqual(table.defaultLook[index].opacity, 150, state)
        }
    }

    func testTheWordCardShowsJitensFrequencyRankLikeTheMac() throws {
        XCTAssertEqual(WordFrequency.label(1234), "Frequency #1234")
        XCTAssertEqual(WordFrequency.label(1), "Frequency #1")
        XCTAssertNil(WordFrequency.label(nil))
        XCTAssertNil(WordFrequency.label(0))
        // The Mac sends it with the word; an older Mac or an unknown rank sends none.
        let json = #"{"wordId":7,"readingIndex":1,"spelling":"猫","reading":"ねこ","readings":[],"meanings":[],"state":0,"stateLabel":"","frequencyRank":1234,"inDeck":null,"complete":true}"#
        let detail = try JSONDecoder().decode(WordDetail.self, from: Data(json.utf8))
        XCTAssertEqual(WordFrequency.label(detail.frequencyRank), "Frequency #1234")
        let older = try JSONDecoder().decode(WordDetail.self, from: Data(json.replacingOccurrences(of: #""frequencyRank":1234,"#, with: "").utf8))
        XCTAssertNil(WordFrequency.label(older.frequencyRank))
    }
}
