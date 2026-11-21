import XCTest
@testable import PudgeCore

/// iPhone: Control Center's rotation lock (a portrait lock) turned on while watching in landscape
/// threw the player into portrait. The player is landscape on its own and has its own lock.
final class PlayerOrientationTests: XCTestCase {
    func testTheIPhonePlayerStaysLandscapeWhateverThePortraitLockSays() {
        XCTAssertEqual(PlayerOrientation.allowed(isPad: false, playerOnScreen: true, lock: .followDevice), .landscape)
        XCTAssertEqual(PlayerOrientation.allowed(isPad: false, playerOnScreen: true, lock: .locked(.landscapeLeft)), .only(.landscapeLeft))
        XCTAssertEqual(PlayerOrientation.allowed(isPad: false, playerOnScreen: true, lock: .locked(.portrait)), .landscape)
        // Closed: the app's usual orientations again.
        XCTAssertEqual(PlayerOrientation.allowed(isPad: false, playerOnScreen: false, lock: .locked(.landscapeLeft)), .app)
    }

    func testTheIPadKeepsItsOrientations() {
        XCTAssertEqual(PlayerOrientation.allowed(isPad: true, playerOnScreen: true, lock: .followDevice), .app)
        XCTAssertEqual(PlayerOrientation.allowed(isPad: true, playerOnScreen: true, lock: .locked(.landscapeRight)), .app)
    }

    func testTheLockButtonKeepsTheSideShownAndUnlocks() {
        XCTAssertEqual(PlayerOrientation.toggled(.followDevice, current: .landscapeLeft), .locked(.landscapeLeft))
        XCTAssertEqual(PlayerOrientation.toggled(.followDevice, current: .landscapeRight), .locked(.landscapeRight))
        XCTAssertEqual(PlayerOrientation.toggled(.followDevice, current: .portrait), .locked(.landscapeRight))
        XCTAssertEqual(PlayerOrientation.toggled(.followDevice, current: nil), .locked(.landscapeRight))
        XCTAssertEqual(PlayerOrientation.toggled(.locked(.landscapeLeft), current: .landscapeLeft), .followDevice)
        XCTAssertTrue(PlayerOrientation.isLocked(.locked(.landscapeLeft)))
        XCTAssertFalse(PlayerOrientation.isLocked(.followDevice))
    }
}
