import Foundation
import XCTest
@testable import PudgeCore

/// "Explain grammar" chats on the iPhone/iPad (`swift test` in ios/PudgeCore on the Mac).
final class GrammarChatTests: XCTestCase {
    private func place(_ paragraph: Int, _ offset: Int) -> TextPosition { TextPosition(paragraph: paragraph, offset: offset) }

    private let chapter = [
        TextParagraph(text: "一つ目の段落。", tokens: []),
        TextParagraph(text: "", image: true, tokens: []),
        TextParagraph(text: "二つ目。猫が好きだ。", tokens: []),
    ]
    private func paragraph(_ index: Int) -> TextParagraph? { chapter.indices.contains(index) ? chapter[index] : nil }

    private func explanation(_ summary: String = "- ～が好き — нравится") -> GrammarExplanation {
        GrammarExplanation(translation: "Я люблю кошек.", points: [GrammarPoint(pattern: "～が好き", meaning: "нравится")], summary: summary)
    }

    func testTheContextIsTheTextBeforeTheSentenceWithoutIllustrations() {
        // "猫が好きだ。" starts at UTF-16 offset 4 of paragraph 2.
        XCTAssertEqual(GrammarContext.preceding(paragraph, before: place(2, 4)), "一つ目の段落。\n二つ目。")
        XCTAssertEqual(GrammarContext.preceding(paragraph, before: place(2, 4), limit: 4), "二つ目。")
        XCTAssertEqual(GrammarContext.preceding(paragraph, before: place(0, 0)), "")
        XCTAssertEqual(GrammarContext.preceding(paragraph, before: place(2, 99)), "一つ目の段落。\n二つ目。猫が好きだ。")
        XCTAssertEqual(GrammarContext.joined([" 吹き出し1 ", "", "吹き出し2"]), "吹き出し1\n吹き出し2")
        XCTAssertEqual(GrammarContext.joined(["あいうえお", "かきくけこ"], limit: 3), "くけこ")
    }

    func testTheMacsAnswersAreRead() throws {
        // The answers the Mac sends (tests/test_fix_iphone_grammar_chat.py checks the same file).
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("tests/fixtures/iphone_grammar_answers.json"))
        struct Answers: Decodable { let explained: GrammarJob; let answered: GrammarJob; let running: GrammarJob; let failed: GrammarJob }
        let answers = try JSONDecoder().decode(Answers.self, from: data)
        XCTAssertEqual(answers.explained.state, "done")
        XCTAssertEqual(answers.explained.explanation?.translation, "Я люблю кошек.")
        XCTAssertEqual(answers.explained.explanation?.points, [GrammarPoint(pattern: "～が好き", meaning: "нравится", explanation: "が отмечает объект чувства.")])
        XCTAssertEqual(answers.explained.explanation?.summary, "Перевод: Я люблю кошек.\n- ～が好き — нравится")
        XCTAssertEqual(answers.answered.reply, "Потому что **が** отмечает [объект](https://example.com) чувства.")
        XCTAssertTrue(answers.running.running)
        XCTAssertEqual(answers.running.job, "JOB")
        XCTAssertEqual(answers.failed.state, "failed")
        XCTAssertTrue(answers.failed.error?.contains("did not answer") == true)
        // Points without a pattern are dropped; missing or odd fields are empty.
        let odd = try JSONDecoder().decode(GrammarExplanation.self, from: Data(#"{"points":[{"pattern":""},{"pattern":"～て","meaning":3}]}"#.utf8))
        XCTAssertEqual(odd.points, [GrammarPoint(pattern: "～て")])
        XCTAssertEqual(odd.translation, "")
    }

    func testAQuestionCarriesTheChatWithTheExplanationAsTheMacSumsItUp() {
        var chat = GrammarChat(book: GrammarChat.book(server: "mac", kind: "ln", id: 3), source: GrammarSource(sentence: "猫が好きだ。", context: "前"))
        XCTAssertEqual(chat.book, "mac/ln/3")
        chat.append(GrammarChatMessage(role: .user, text: GrammarChat.request("猫が好きだ。")))
        XCTAssertFalse(chat.explained)
        XCTAssertTrue(chat.waitingForAnswer)
        chat.append(GrammarChatMessage(role: .assistant, text: "Я люблю кошек.", explanation: explanation()))
        XCTAssertTrue(chat.explained)
        XCTAssertFalse(chat.waitingForAnswer)
        chat.append(GrammarChatMessage(role: .user, text: "Почему が?"))
        let request = chat.askRequest
        XCTAssertEqual(request.sentence, "猫が好きだ。")
        XCTAssertEqual(request.context, "前")
        XCTAssertEqual(request.points, [GrammarPointRequest(pattern: "～が好き", meaning: "нравится")])
        XCTAssertEqual(request.messages, [
            GrammarTurn(role: "user", content: "Explain the grammar of «猫が好きだ。»"),
            GrammarTurn(role: "assistant", content: "- ～が好き — нравится"),
            GrammarTurn(role: "user", content: "Почему が?"),
        ])
        // An explanation without a summary goes back as its text.
        XCTAssertEqual(GrammarChatMessage(role: .assistant, text: "text", explanation: explanation("")).modelText, "text")
    }

    func testLongChatsKeepTheExplanationAndSendOnlyTheNewestMessages() {
        var chat = GrammarChat(book: "b", source: GrammarSource(sentence: "猫"))
        chat.append(GrammarChatMessage(role: .user, text: "explain"))
        chat.append(GrammarChatMessage(role: .assistant, text: "", explanation: explanation()))
        for index in 0..<70 { chat.append(GrammarChatMessage(role: index % 2 == 0 ? .user : .assistant, text: "m\(index)")) }
        XCTAssertEqual(chat.messages.count, GrammarLimits.messages)
        XCTAssertEqual(chat.messages[0].text, "explain")
        XCTAssertNotNil(chat.messages[1].explanation)
        XCTAssertEqual(chat.messages.last?.text, "m69")
        let turns = chat.turns()
        XCTAssertEqual(turns.count, GrammarLimits.turns)
        XCTAssertEqual(turns.last?.content, "m69")
        XCTAssertEqual(chat.turns(limit: 2, characters: 1).map(\.content), ["m", "m"])
        XCTAssertEqual(GrammarLimits.clip("猫が好き", 2), "猫が")
        XCTAssertEqual(GrammarLimits.clip("猫", 2), "猫")
    }

    func testEachBookKeepsItsLastFewChats() {
        var archive = GrammarChatArchive()
        let start = Date(timeIntervalSince1970: 1_000)
        var chats: [GrammarChat] = []
        for index in 0..<7 {
            let chat = GrammarChat(book: "mac/ln/1", source: GrammarSource(sentence: "s\(index)"), date: start.addingTimeInterval(Double(index)))
            chats.append(chat)
            archive.keep(chat)
        }
        archive.keep(GrammarChat(book: "mac/manga/2", source: GrammarSource(sentence: "bubble"), date: start))
        XCTAssertEqual(archive.chats(book: "mac/ln/1").map(\.source.sentence), ["s6", "s5", "s4", "s3", "s2"])
        XCTAssertEqual(archive.latest(book: "mac/manga/2")?.source.sentence, "bubble")
        XCTAssertNil(archive.latest(book: "other/ln/1"))
        // Used again: it is the book's latest and stays.
        var used = chats[2]
        used.append(GrammarChatMessage(role: .user, text: "?"), now: start.addingTimeInterval(100))
        archive.keep(used)
        XCTAssertEqual(archive.latest(book: "mac/ln/1")?.id, used.id)
        XCTAssertEqual(archive.chats(book: "mac/ln/1").count, GrammarLimits.chatsPerBook)
        archive.remove(used.id)
        XCTAssertEqual(archive.chats(book: "mac/ln/1").count, GrammarLimits.chatsPerBook - 1)
        // At most `total` in all, the least recently used go first.
        archive.keep(GrammarChat(book: "x", source: GrammarSource(sentence: "new"), date: start.addingTimeInterval(200)), total: 2)
        XCTAssertEqual(archive.chats.map(\.source.sentence), ["new", "s6"])
    }

    func testTheStoreKeepsChatsAcrossLaunchesAndStartsEmptyOnADamagedFile() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("grammar-chats.json")
        let when = Date(timeIntervalSince1970: 1_700_000_000)
        var chat = GrammarChat(book: "mac/ln/1", source: GrammarSource(sentence: "猫が好きだ。", context: "前"), date: when)
        chat.append(GrammarChatMessage(role: .user, text: GrammarChat.request("猫が好きだ。"), date: when), now: when)
        chat.append(GrammarChatMessage(role: .assistant, text: "", explanation: explanation(), date: when), now: when)
        GrammarChatStore(file: file).save(chat)
        XCTAssertEqual(GrammarChatStore(file: file).archive.latest(book: "mac/ln/1"), chat)
        let store = GrammarChatStore(file: file)
        store.remove(chat.id)
        XCTAssertTrue(GrammarChatStore(file: file).archive.chats.isEmpty)
        try Data("{not json".utf8).write(to: file)
        XCTAssertTrue(GrammarChatStore(file: file).archive.chats.isEmpty)
    }

    func testAnswersShowSimpleMarkdownButLinksAndImagesOpenNothing() {
        let shown = GrammarText.answer("Потому что **が** отмечает [объект](https://example.com) ![x](https://example.com/a.png) чувства.")
        XCTAssertTrue(String(shown.characters).hasPrefix("Потому что が отмечает объект"))
        XCTAssertTrue(shown.runs.allSatisfy { $0.link == nil && $0.imageURL == nil })
        XCTAssertTrue(shown.runs.contains { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true })
        XCTAssertEqual(String(GrammarText.answer("plain <b>text</b>").characters).contains("text"), true)
    }

    func testTheReaderMenuOffersExplainGrammarForASelection() {
        let paragraphs = [TextParagraph(text: "猫が好き", tokens: [StudyToken(start: 0, end: 1, wordId: 5, readingIndex: 0, state: 0)])]
        let lookup = { (index: Int) -> TextParagraph? in paragraphs.indices.contains(index) ? paragraphs[index] : nil }
        let range = TextSelectionRange(place(0, 0), place(0, 4))
        XCTAssertEqual(range.actions(lookup, listen: true, markHere: false, explain: true),
                       [.copy, .lookUp(TextWord(paragraph: 0, token: 0)), .explain, .listen(place(0, 0))])
        // Elsewhere (subtitles, the word card) the menu stays as it was.
        XCTAssertEqual(range.actions(lookup, listen: false, markHere: false), [.copy, .lookUp(TextWord(paragraph: 0, token: 0))])
        let blank = [TextParagraph(text: "  ", tokens: [])]
        XCTAssertEqual(TextSelectionRange(place(0, 0), place(0, 2)).actions({ blank.indices.contains($0) ? blank[$0] : nil }, listen: false, markHere: false, explain: true), [.copy])
    }
}
