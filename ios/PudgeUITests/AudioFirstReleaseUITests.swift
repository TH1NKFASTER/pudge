import XCTest

final class AudioFirstReleaseUITests: XCTestCase {
    @MainActor
    func testAudioReleaseNavigation() throws {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Audiobooks"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Downloads"].tap()
        XCTAssertTrue(app.navigationBars["Downloads"].exists)
        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.navigationBars["Settings"].exists)
        XCTAssertTrue(app.staticTexts["Audiobooks between Mac ↔ iPhone"].exists)
        app.tabBars.buttons["Audiobooks"].tap()
        XCTAssertTrue(app.navigationBars["Audiobooks"].exists)
    }
}
