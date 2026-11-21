import Foundation
import XCTest
@testable import PudgeCore

/// IOS6: LLM answers in the chat are drawn from their Markdown (`swift test` in ios/PudgeCore on the Mac).
final class ChatMarkdownTests: XCTestCase {
    func testFencedCodeIsOneBlockWithItsLanguageAndLinesKept() {
        let text = "Here:\n\n```swift\nlet a = 1\n    let b = [a]\n```\nAfter."
        XCTAssertEqual(ChatMarkdown.blocks(text), [
            .paragraph("Here:"),
            .code(language: "swift", text: "let a = 1\n    let b = [a]", closed: true),
            .paragraph("After."),
        ])
        // Markdown inside a fence stays text; tildes fence too.
        XCTAssertEqual(ChatMarkdown.blocks("~~~\n# not a heading\n- not a list\n~~~"),
                       [.code(language: "", text: "# not a heading\n- not a list", closed: true)])
    }

    func testAPartialAnswerWithAnOpenFenceIsCodeSoFar() {
        XCTAssertEqual(ChatMarkdown.blocks("Look:\n```py\nprint(1)\nfor x in"), [
            .paragraph("Look:"),
            .code(language: "py", text: "print(1)\nfor x in", closed: false),
        ])
        XCTAssertEqual(ChatMarkdown.blocks("```"), [.code(language: "", text: "", closed: false)])
        // A shorter run does not close a longer fence.
        XCTAssertEqual(ChatMarkdown.blocks("````\n```\n````"), [.code(language: "", text: "```", closed: true)])
    }

    func testHeadingsListsQuotesAndRules() {
        let text = """
        ## Particles ##
        - **が** marks the subject
        - *を* marks the object
          and more on it
        1. first
        2) second

        > a quote
        ---
        Plain line
        next line
        """
        XCTAssertEqual(ChatMarkdown.blocks(text), [
            .heading(level: 2, text: "Particles"),
            .list(ordered: false, items: [ChatListItem(marker: "•", text: "**が** marks the subject"),
                                          ChatListItem(marker: "•", text: "*を* marks the object\nand more on it")]),
            .list(ordered: true, items: [ChatListItem(marker: "1.", text: "first"), ChatListItem(marker: "2.", text: "second")]),
            .quote("a quote"),
            .rule,
            .paragraph("Plain line\nnext line"),
        ])
    }

    func testNestedItemsLooseListsAndThingsThatAreNotBlocks() {
        let blocks = ChatMarkdown.blocks("- one\n  - inner\n\n- two\n\nAfter")
        XCTAssertEqual(blocks, [
            .list(ordered: false, items: [ChatListItem(marker: "•", text: "one"), ChatListItem(marker: "•", text: "inner", level: 1),
                                          ChatListItem(marker: "•", text: "two")]),
            .paragraph("After"),
        ])
        // No space after the marker, #hashtags, bold at the start, `inline` fences: paragraphs.
        XCTAssertEqual(ChatMarkdown.blocks("-x\n#tag\n**bold** start\n```a`b```"),
                       [.paragraph("-x\n#tag\n**bold** start\n```a`b```")])
        XCTAssertEqual(ChatMarkdown.blocks("a\r\n\r\nb"), [.paragraph("a"), .paragraph("b")])
        XCTAssertEqual(ChatMarkdown.blocks(""), [])
    }

    func testInlineMarkdownAndOnlyWebLinksKeepTheirTarget() {
        let shown = ChatMarkdown.inline("**が** and `code` [web](https://example.com/a) [bad](javascript:alert(1)) [app](pudge://x) ![i](https://example.com/i.png)")
        XCTAssertTrue(String(shown.characters).hasPrefix("が and code web bad app"))
        XCTAssertTrue(shown.runs.contains { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true })
        XCTAssertTrue(shown.runs.contains { $0.inlinePresentationIntent?.contains(.code) == true })
        let links = shown.runs.compactMap { $0.link }
        XCTAssertEqual(links, [URL(string: "https://example.com/a")!])
        XCTAssertTrue(shown.runs.allSatisfy { $0.imageURL == nil })
        // HTML is never interpreted; line breaks stay.
        XCTAssertTrue(String(ChatMarkdown.inline("a <b>x</b>\nb").characters).contains("\n"))
    }
}
