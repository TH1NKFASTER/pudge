"""FINALE2 on the phone: no "caught up" wheel, no explanation line, a sheet sized to its content,
a longer first spin and a swipe that spins the wheel.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The rules are
PudgeCore code with XCTest (FinaleSpinTests); the sheet's use of them is guarded here by reading
the Swift source.
"""

from __future__ import annotations

from source_policy import source

SHEET = "ios/Pudge/UI/FinaleSheet.swift"


def test_only_the_end_of_the_anime_shows_the_wheel_and_without_explanation() -> None:
    sheet = source(SHEET)
    assert "guard finale.offersWheel else { waiting[id] = nil; return }" in sheet
    assert "The wheel will choose a score" not in sheet
    assert "You are caught up with the aired episodes" not in sheet
    core = source("ios/PudgeCore/Sources/PudgeCore/AnimeFinale.swift")
    assert "public var offersWheel: Bool { isFinal && episode > 0 }" in core


def test_the_sheet_fits_its_content_on_iphone_and_ipad() -> None:
    sheet = source(SHEET)
    assert (
        "FinaleSheetSizing(height: CGFloat(FinaleSheetLayout.height(content: Double(contentHeight))), regular: regular)"
        in sheet
    )
    assert "content.presentationDetents([.height(height)]).presentationDragIndicator(.hidden)" in sheet
    assert "content.frame(idealWidth: 540, idealHeight: height).presentationSizing(.fitted)" in sheet
    assert ".frame(minHeight: viewport.size.height)" in sheet
    assert "regular: sizeClass == .regular" in sheet


def test_the_first_spin_is_longer_and_a_swipe_spins_with_the_same_draw() -> None:
    sheet = source(SHEET)
    spin = sheet.split("private func spin(flickSpeed: Double? = nil) {", 1)[1].split("\n    }\n", 1)[0]
    assert "ScoreWheel.spinDuration(spinsBefore: spins, flickSpeed: flickSpeed)" in spin
    assert "withAnimation(.easeOut(duration: duration))" in spin
    assert "UInt64(duration * 1_000_000_000)" in spin
    assert "ScoreWheel.spin(previous: last)" in spin
    assert "if ScoreWheel.isFlick(distance: distance, speed: speed) { spin(flickSpeed: speed) }" in sheet
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/FinaleSpinTests.swift")
    assert "XCTAssertEqual(ScoreWheel.spinDuration(spinsBefore: 0), 2.0, accuracy: 0.0001)" in tests
