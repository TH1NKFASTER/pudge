import XCTest
@testable import PudgeCore

/// The iPhone/iPad screens: separate subtitle wheels, quality names, the libraries' pull-down
/// header and Storage's empty categories.
final class PhoneScreenWishesTests: XCTestCase {
    func testEachWheelHasItsOwnPanelAndOnlyOneIsOpen() {
        XCTAssertEqual(PlayerWheelPanel.tapped(.timing, open: nil), .timing)
        XCTAssertEqual(PlayerWheelPanel.tapped(.size, open: nil), .size)
        // The other button switches panels: never both at once.
        XCTAssertEqual(PlayerWheelPanel.tapped(.size, open: .timing), .size)
        XCTAssertEqual(PlayerWheelPanel.tapped(.timing, open: .size), .timing)
        // The same button again closes its panel.
        XCTAssertNil(PlayerWheelPanel.tapped(.timing, open: .timing))
        XCTAssertNil(PlayerWheelPanel.tapped(.size, open: .size))
        XCTAssertEqual(PlayerWheelPanel.allCases.count, 2)
    }

    func testQualitiesAreNamedByTheirResolutionOnly() {
        XCTAssertEqual(VideoQuality.allCases.map(\.title), ["Original", "720p", "540p"])
        for quality in VideoQuality.allCases {
            XCTAssertFalse(quality.title.localizedCaseInsensitiveContains("smaller"))
            XCTAssertFalse(quality.title.localizedCaseInsensitiveContains("saver"))
        }
    }

    func testTheHeaderFiltersByTitleAndByWhatIsOnThisDevice() {
        var filter = LibraryFilter()
        XCTAssertFalse(filter.isActive)
        XCTAssertTrue(filter.matches(["Sample Show"], onDevice: false))
        XCTAssertTrue(filter.matches([nil], onDevice: false))

        filter.search = "  show "
        XCTAssertTrue(filter.isActive)
        XCTAssertTrue(filter.matches(["Sample SHOW"], onDevice: false))
        XCTAssertTrue(filter.matches(["Volume 2", nil, "The Show Series"], onDevice: false))  // series or franchise
        XCTAssertFalse(filter.matches(["Other title", nil, ""], onDevice: true))

        filter = LibraryFilter(scope: .onDevice)
        XCTAssertTrue(filter.isActive)
        XCTAssertTrue(filter.matches(["Anything"], onDevice: true))
        XCTAssertFalse(filter.matches(["Anything"], onDevice: false))

        filter.search = "any"
        XCTAssertTrue(filter.matches(["Anything"], onDevice: true))
        XCTAssertFalse(filter.matches(["Anything"], onDevice: false))
        XCTAssertFalse(filter.matches(["Else"], onDevice: true))

        // Whitespace only is no search.
        XCTAssertFalse(LibraryFilter(search: "   ").isActive)
        XCTAssertEqual(LibraryScope.allCases.map { $0.title(device: "iPad") }, ["All", "Only on iPad"])
    }

    func testAnEmptyStorageCategoryShowsADash() {
        let format: (Int64) -> String = { "\($0) B" }
        XCTAssertEqual(StorageSizeText.text(0, formatted: format), "—")
        XCTAssertEqual(StorageSizeText.text(-5, formatted: format), "—")
        XCTAssertEqual(StorageSizeText.text(2048, formatted: format), "2048 B")
    }
}
