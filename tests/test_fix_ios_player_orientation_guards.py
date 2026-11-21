"""iPhone: Control Center's rotation lock (a portrait lock on the iPhone) turned on while
watching in landscape threw the video player into portrait. The iPhone player is landscape on its
own (as video apps are) with its own lock button; the app's orientations come back when it closes;
the iPad is unchanged.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The rule is PudgeCore
code with XCTest (PlayerOrientationTests); the wiring is guarded by reading the Swift source.
"""

from __future__ import annotations

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"


def test_rule_and_xctests() -> None:
    core = source("ios/PudgeCore/Sources/PudgeCore/PlayerOrientation.swift")
    assert "guard !isPad, playerOnScreen else { return .app }" in core
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/PlayerOrientationTests.swift")
    for name in (
        "testTheIPhonePlayerStaysLandscapeWhateverThePortraitLockSays",
        "testTheIPadKeepsItsOrientations",
        "testTheLockButtonKeepsTheSideShownAndUnlocks",
    ):
        assert f"func {name}()" in tests


def test_the_app_delegate_asks_the_gate_and_the_player_drives_it() -> None:
    app = source("ios/Pudge/App/PudgeApp.swift")
    assert "supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {" in app
    assert "PlayerOrientationGate.mask" in app
    watch = source(WATCH)
    gate = watch.split("enum PlayerOrientationGate {", 1)[1].split("\nstruct VideoPlayerScreen", 1)[0]
    assert "current.setNeedsUpdateOfSupportedInterfaceOrientations()" in gate
    assert "scene.requestGeometryUpdate(.iOS(interfaceOrientations: mask))" in gate
    assert "case .app: return isPad ? .all : .allButUpsideDown" in gate
    player = watch.split("struct VideoPlayerScreen: View {", 1)[1]
    appear = player.split("        .onAppear {\n            // The text size of the earlier", 1)[1].split(
        "\n        }\n", 1
    )[0]
    assert "PlayerOrientationGate.apply(playerOnScreen: true, lock: rotationLock)" in appear
    gone = player.split("        .onDisappear {\n", 1)[1].split("\n        }\n", 1)[0]
    assert "PlayerOrientationGate.apply(playerOnScreen: false, lock: rotationLock)" in gone
    assert (
        ".onChange(of: rotationLock) { _, lock in PlayerOrientationGate.apply(playerOnScreen: true, lock: lock) }"
        in player
    )
    # The lock button is the iPhone's only.
    assert "if UIDevice.current.userInterfaceIdiom != .pad {" in player
    assert (
        "rotationLock = PlayerOrientation.toggled(rotationLock, current: PlayerOrientationGate.currentSide)"
        in player
    )
    assert '.accessibilityIdentifier("player-rotation-lock")' in player
