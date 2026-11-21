#if canImport(CoreGraphics)
import CoreGraphics
#endif
import XCTest
@testable import PudgeCore

/// The iPhone/iPad two-page manga view (`swift test` in ios/PudgeCore on the Mac).
final class MangaSpreadsTests: XCTestCase {
    func testOnePageEachUnlessTwoPages() {
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 4, wide: [], twoPages: false), [[0], [1], [2], [3]])
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 0, wide: [], twoPages: true), [])
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 1, wide: [], twoPages: true), [[0]])
    }

    func testCoverAloneThenPairsAndALastPageAlone() {
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 6, wide: [], twoPages: true), [[0], [1, 2], [3, 4], [5]])
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 5, wide: [], twoPages: true), [[0], [1, 2], [3, 4]])
    }

    func testAWidePageIsAloneAndThePairingStartsAgainAfterIt() {
        // Page 4 is a double-page spread: page 3 has no partner, pages after it pair from 5.
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 9, wide: [4], twoPages: true),
                       [[0], [1, 2], [3], [4], [5, 6], [7, 8]])
        // A spread where a pair would start: the next pair starts right after it.
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 8, wide: [3], twoPages: true),
                       [[0], [1, 2], [3], [4, 5], [6, 7]])
        // A wide cover stays alone; two spreads in a row stay apart.
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 5, wide: [0, 1, 2], twoPages: true), [[0], [1], [2], [3, 4]])
    }

    func testThePlaceIsTheFirstPageOfTheShownSpread() {
        let layout = MangaSpreads.spreads(pageCount: 9, wide: [4], twoPages: true)
        XCTAssertEqual(MangaSpreads.first(of: 2, in: layout), 1)
        XCTAssertEqual(MangaSpreads.first(of: 1, in: layout), 1)
        XCTAssertEqual(MangaSpreads.first(of: 4, in: layout), 4)
        XCTAssertEqual(MangaSpreads.first(of: 6, in: layout), 5)
        XCTAssertEqual(MangaSpreads.first(of: 40, in: layout), 7)
        XCTAssertEqual(MangaSpreads.first(of: -3, in: layout), 0)
        // Back to one page each: the same page.
        XCTAssertEqual(MangaSpreads.first(of: 5, in: MangaSpreads.spreads(pageCount: 9, wide: [4], twoPages: false)), 5)
        XCTAssertEqual(MangaSpreads.first(of: 5, in: []), 5)
    }

    func testRightToLeftPutsTheLowerPageOnTheRight() {
        XCTAssertEqual(MangaSpreads.onScreen([4, 5], rightToLeft: true), [5, 4])
        XCTAssertEqual(MangaSpreads.onScreen([4, 5], rightToLeft: false), [4, 5])
        XCTAssertEqual(MangaSpreads.onScreen([0], rightToLeft: true), [0])
    }

    func testTitleShowsBothPagesOfAPair() {
        XCTAssertEqual(MangaSpreads.title([3, 4], pageCount: 211), "4–5 / 211")
        XCTAssertEqual(MangaSpreads.title([4, 3], pageCount: 211), "4–5 / 211")
        XCTAssertEqual(MangaSpreads.title([0], pageCount: 211), "1 / 211")
    }

    func testTwoPagesByTheSpaceAndThePageShapeNotTheDevice() {
        let page = MangaSpreads.defaultAspect
        XCTAssertFalse(MangaSpreads.showsTwoPages(width: 393, height: 852, pageAspect: page))  // iPhone portrait
        XCTAssertTrue(MangaSpreads.showsTwoPages(width: 852, height: 393, pageAspect: page))   // iPhone landscape
        XCTAssertFalse(MangaSpreads.showsTwoPages(width: 834, height: 1194, pageAspect: page)) // iPad portrait
        XCTAssertTrue(MangaSpreads.showsTwoPages(width: 1194, height: 834, pageAspect: page))  // iPad landscape
        XCTAssertTrue(MangaSpreads.showsTwoPages(width: 1600, height: 1200, pageAspect: page)) // 4:3 unfolded, wide
        XCTAssertFalse(MangaSpreads.showsTwoPages(width: 1000, height: 1000, pageAspect: page)) // square: one page
        XCTAssertFalse(MangaSpreads.showsTwoPages(width: 852, height: 393, pageAspect: 0.7))   // landscape pages
        XCTAssertFalse(MangaSpreads.showsTwoPages(width: 0, height: 393, pageAspect: page))
        XCTAssertFalse(MangaSpreads.showsTwoPages(width: 852, height: 393, pageAspect: .nan))
    }

    func testAPairMeetsAtTheMiddleWhereAFoldIs() {
        let box = CGSize(width: 1000, height: 500)
        let frames = MangaSpreads.frames([CGSize(width: 700, height: 1000), CGSize(width: 700, height: 1000)], in: box)
        XCTAssertEqual(frames.count, 2)
        XCTAssertEqual(frames[0].maxX, 500, accuracy: 0.001)
        XCTAssertEqual(frames[1].minX, 500, accuracy: 0.001)
        XCTAssertEqual(frames[0].height, 500, accuracy: 0.001)
        XCTAssertEqual(frames[1].width, 350, accuracy: 0.001)
        // Narrow halves (pages wider than half): each fills its half, still meeting in the middle.
        let narrow = MangaSpreads.frames([CGSize(width: 700, height: 1000), CGSize(width: 700, height: 1000)],
                                         in: CGSize(width: 600, height: 500))
        XCTAssertEqual(narrow[0].minX, 0, accuracy: 0.001)
        XCTAssertEqual(narrow[0].maxX, 300, accuracy: 0.001)
        XCTAssertEqual(narrow[1].maxX, 600, accuracy: 0.001)
        // One page: centred, as before.
        let one = MangaSpreads.frames([CGSize(width: 700, height: 1000)], in: box)
        XCTAssertEqual(one[0].midX, 500, accuracy: 0.001)
        XCTAssertEqual(MangaSpreads.frames([.zero], in: box), [CGRect(origin: .zero, size: box)])
    }

    func testWidePagesAndTheUsualShapeFromPageSizes() {
        let sizes: [Int: CGSize] = [0: CGSize(width: 700, height: 1000), 1: CGSize(width: 1400, height: 1000),
                                    2: CGSize(width: 700, height: 1000), 3: CGSize(width: 720, height: 1000)]
        let pages = MangaSpreads.pages(sizes: sizes)
        XCTAssertEqual(pages.wide, [1])
        XCTAssertEqual(pages.aspect ?? 0, 1000.0 / 700.0, accuracy: 0.0001)
        XCTAssertNil(MangaSpreads.pages(sizes: [:]).aspect)
        XCTAssertTrue(MangaSpreads.isWide(width: 1150, height: 1000))
        XCTAssertFalse(MangaSpreads.isWide(width: 1100, height: 1000))
    }

    func testTheMacsSpreadsAnswerDecodes() throws {
        let value = try JSONDecoder().decode(MangaSpreadPages.self, from: Data(#"{"wide":[4,37],"aspect":1.43}"#.utf8))
        XCTAssertEqual(value, MangaSpreadPages(wide: [4, 37], aspect: 1.43))
        XCTAssertNil(value.shift)  // an older Mac
        let unknown = try JSONDecoder().decode(MangaSpreadPages.self, from: Data(#"{"wide":[],"aspect":null}"#.utf8))
        XCTAssertNil(unknown.aspect)
        let shifted = try JSONDecoder().decode(MangaSpreadPages.self,
                                               from: Data(#"{"wide":[3],"aspect":1.4,"shift":true,"spreads":[[0],[1],[2],[3]]}"#.utf8))
        XCTAssertEqual(shifted, MangaSpreadPages(wide: [3], aspect: 1.4, shift: true))
    }

    /// The Mac's table and the phone's are the same: the shared cases (pudge/manga_spreads.py).
    func testTheSameSpreadsAsTheMac() throws {
        struct Case: Decodable { let name: String; let pageCount: Int; let wide: [Int]; let shift: Bool; let spreads: [[Int]] }
        struct Cases: Decodable { let cases: [Case] }
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let file = root.appendingPathComponent("tests/fixtures/manga_spreads.json")
        let cases = try JSONDecoder().decode(Cases.self, from: Data(contentsOf: file)).cases
        XCTAssertGreaterThan(cases.count, 5)
        for item in cases {
            XCTAssertEqual(MangaSpreads.spreads(pageCount: item.pageCount, wide: Set(item.wide), twoPages: true, shift: item.shift),
                           item.spreads, item.name)
        }
    }

    /// The Mac's page transforms are drawn the same here: the shared cases (pudge/manga_align.py,
    /// also the Mac reader's), and taps go back to the same page point.
    func testTheSamePageTransformsAsTheMac() throws {
        struct Case: Decodable {
            let name: String; let transform: MangaPageTransform; let aspect: Double
            let points: [[Double]]; let drawn: [[Double]]; let backing: [Double]?
        }
        struct Cases: Decodable { let cases: [Case] }
        // tests/fixtures beside ios/ (this file is ios/PudgeCore/Tests/PudgeCoreTests/…).
        let file = URL(fileURLWithPath: "../../../../tests/fixtures/manga_alignment.json",
                       relativeTo: URL(fileURLWithPath: #filePath)).standardizedFileURL
        let cases = try JSONDecoder().decode(Cases.self, from: Data(contentsOf: file)).cases
        XCTAssertGreaterThan(cases.count, 2)
        for item in cases {
            for (point, drawn) in zip(item.points, item.drawn) {
                let out = item.transform.drawn(CGPoint(x: point[0], y: point[1]), aspect: item.aspect)
                XCTAssertEqual(Double(out.x), drawn[0], accuracy: 1e-5, item.name)
                XCTAssertEqual(Double(out.y), drawn[1], accuracy: 1e-5, item.name)
                let back = item.transform.page(at: out, aspect: item.aspect)
                XCTAssertEqual(Double(back.x), point[0], accuracy: 1e-9, item.name)
                XCTAssertEqual(Double(back.y), point[1], accuracy: 1e-9, item.name)
            }
        }
    }

    func testTheMacsAlignmentAnswerDecodesAndGivesEachPageItsTransform() throws {
        let json = #"""
        {"wide":[],"aspect":1.47,"shift":false,"align":true,"spreads":[[0],[1,2]],
         "alignment":{"version":1,"pending":0,"single":{"0":{"angle":-0.3,"scale":1.0,"dx":0.0,"dy":0.0}},
          "pairs":{"1-2":{"rtl":[{"angle":0.2,"scale":0.99,"dx":-0.01,"dy":0.004,"gutter":"left",
                                  "backing":[0.0,0.01,0.003,0.99],"fill":"#f0f0f0"},null]}}}}
        """#
        let value = try JSONDecoder().decode(MangaSpreadPages.self, from: Data(json.utf8))
        XCTAssertEqual(value.align, true)
        let alignment = try XCTUnwrap(value.alignment)
        let pair = try XCTUnwrap(alignment.transforms(for: [1, 2], rightToLeft: true))
        XCTAssertEqual(pair.count, 2)
        XCTAssertEqual(pair[0]?.gutter, "left")
        XCTAssertNil(pair[1])  // left as it is
        XCTAssertNil(alignment.transforms(for: [1, 2], rightToLeft: false))  // nothing for left to right
        XCTAssertNil(alignment.transforms(for: [3, 4], rightToLeft: true))
        let single = try XCTUnwrap(alignment.transforms(for: [0], rightToLeft: true))
        XCTAssertEqual(single.count, 1)
        XCTAssertEqual(single[0]?.angle, -0.3)
        let page = try XCTUnwrap(pair[0])
        XCTAssertEqual(page.fillComponents[0], 240.0 / 255, accuracy: 1e-9)
        let backing = try XCTUnwrap(page.backingRect(in: CGSize(width: 1000, height: 500)))
        XCTAssertEqual(backing.minX, 0, accuracy: 1e-9)
        XCTAssertEqual(backing.width, 3, accuracy: 1e-9)
        XCTAssertEqual(backing.minY, 5, accuracy: 1e-9)
        // A tap on the drawn page is the page point under it; past the gutter it is the other page's.
        let frame = CGRect(x: 400, y: 20, width: 300, height: 440)
        let shown = page.screenPoint(CGPoint(x: 500, y: 100), in: frame)
        let back = page.pagePoint(shown, in: frame)
        XCTAssertEqual(back.x, 500, accuracy: 1e-6)
        XCTAssertEqual(back.y, 100, accuracy: 1e-6)
        XCTAssertFalse(page.keeps(CGPoint(x: 399, y: 100), in: frame))
        XCTAssertTrue(page.keeps(CGPoint(x: 401, y: 100), in: frame))
        // An older Mac: no alignment at all.
        let older = try JSONDecoder().decode(MangaSpreadPages.self, from: Data(#"{"wide":[],"aspect":1.4,"shift":true}"#.utf8))
        XCTAssertNil(older.align)
        XCTAssertNil(older.alignment)
    }

    /// One odd value in the Mac's answer (or a downloaded volume's package) never stops the
    /// volume from opening: that page is drawn as it is.
    func testAnOddAlignmentNeverStopsAVolumeFromOpening() throws {
        let odd = #"""
        {"wide":[],"aspect":1.4,"align":true,
         "alignment":{"pending":"many","single":{"0":{"angle":"x"},"1":7},
          "pairs":{"1-2":{"rtl":[{"angle":0.2,"scale":"big"},"oops"]}}}}
        """#
        let value = try JSONDecoder().decode(MangaSpreadPages.self, from: Data(odd.utf8))
        let alignment = try XCTUnwrap(value.alignment)
        XCTAssertNil(alignment.pending)
        XCTAssertEqual(alignment.single?["0"], MangaPageTransform())  // nothing readable: no change
        XCTAssertNil(alignment.single?["1"])
        let pair = try XCTUnwrap(alignment.transforms(for: [1, 2], rightToLeft: true))
        XCTAssertEqual(pair[0]?.angle, 0.2)
        XCTAssertEqual(pair[0]?.scale, 1)
        XCTAssertNil(pair[1])
        let broken = #"{"wide":[],"aspect":1.4,"alignment":"oops","pairs":{"3-4":"nope"}}"#
        let opened = try JSONDecoder().decode(MangaSpreadPages.self, from: Data(broken.utf8))
        XCTAssertNil(opened.alignment?.pairs)
        XCTAssertNil(opened.alignment?.transforms(for: [3, 4], rightToLeft: true))
    }

    func testAnAlignedPairIsDrawnAtOneHeight() {
        // The right page is wider: fitted into its half it would be lower than the left one.
        let sizes = [CGSize(width: 700, height: 1000), CGSize(width: 760, height: 1000)]
        let box = CGSize(width: 900, height: 600)
        let plain = MangaSpreads.frames(sizes, in: box)
        XCTAssertGreaterThan(plain[0].height, plain[1].height)
        let aligned = MangaSpreads.frames(sizes, in: box, sameHeight: true)
        XCTAssertEqual(aligned[0].height, aligned[1].height, accuracy: 1e-9)
        XCTAssertEqual(aligned[0].height, plain[1].height, accuracy: 1e-9)
        XCTAssertEqual(aligned[0].maxX, 450, accuracy: 1e-9)  // they meet at the gutter
        XCTAssertEqual(aligned[1].minX, 450, accuracy: 1e-9)
        XCTAssertEqual(aligned[0].midY, 300, accuracy: 1e-9)
        // One page: nothing changes.
        XCTAssertEqual(MangaSpreads.frames([sizes[0]], in: box, sameHeight: true), MangaSpreads.frames([sizes[0]], in: box))
    }

    func testShiftPutsThePageAfterTheCoverAlone() {
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 7, wide: [], twoPages: true, shift: true), [[0], [1], [2, 3], [4, 5], [6]])
        // One page each: no shift.
        XCTAssertEqual(MangaSpreads.spreads(pageCount: 3, wide: [], twoPages: false, shift: true), [[0], [1], [2]])
    }

    func testTurningTheDeviceNeverMovesOrSavesThePlace() {
        let singles = MangaSpreads.spreads(pageCount: 9, wide: [], twoPages: false)
        let pairs = MangaSpreads.spreads(pageCount: 9, wide: [], twoPages: true)  // [0] [1,2] [3,4] [5,6] [7,8]
        var place = MangaReadingPlace(page: 4, layout: singles)
        XCTAssertEqual(place.shown, 4)
        let portrait = place.generation
        place.relayout(pairs)
        XCTAssertEqual(place.shown, 3)
        XCTAssertEqual(place.shownPages, [3, 4])
        XCTAssertEqual(place.page, 4)  // the place itself does not move back to 3
        // The portrait pager, going away, reports a page: not a turn.
        XCTAssertNil(place.turned(to: 5, generation: portrait))
        // The new pager settles on the spread on screen: not a turn either.
        XCTAssertNil(place.turned(to: 3, generation: place.generation))
        place.relayout(pairs)  // the same layout again changes nothing
        place.relayout(singles)
        XCTAssertEqual(place.shown, 4)  // back in one page each: the same page
        XCTAssertEqual(place.page, 4)
    }

    func testATurnSavesTheFirstPageAndReadsTheSecond() {
        let pairs = MangaSpreads.spreads(pageCount: 9, wide: [], twoPages: true)
        var place = MangaReadingPlace(page: 4, layout: pairs)
        let pager = place.generation
        XCTAssertEqual(place.turned(to: 5, generation: pager), MangaSave(page: 5, readThrough: 6))
        XCTAssertEqual(place.page, 5)
        XCTAssertNil(place.turned(to: 5, generation: pager))  // the same spread again
        XCTAssertNil(place.turned(to: 2, generation: pager))  // no spread starts there
        XCTAssertEqual(place.turned(to: 0, generation: pager), MangaSave(page: 0, readThrough: nil))
        // A spread found while reading pairs the pages anew around the place: nothing saved.
        place = MangaReadingPlace(page: 5, layout: pairs)
        place.relayout(MangaSpreads.spreads(pageCount: 9, wide: [6], twoPages: true))  // [0] [1,2] [3,4] [5] [6] [7,8]
        XCTAssertEqual(place.shownPages, [5])
        XCTAssertEqual(place.page, 5)
        XCTAssertEqual(place.generation, 1)
    }
}
