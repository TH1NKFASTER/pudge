"""Light-novel reader on a rotated iPhone: touches and paragraph frames in one coordinate space.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac, so this guard
reads its Swift source, like tests/test_fix_reader_landscape_selection.py.

The device's "[reader-selection]" lines in landscape had touch points up to y 561 in an
852 x 393 window while the paragraph frames were in that window: `location(in: nil)` gives the
point in the window the *touch* is in, which was not the reader's (portrait coordinates).
Every hit-test missed and the nearest paragraph, far above the finger, was selected. Now every
touch and recognizer point is taken in a view and converted to that view's own window
(`windowPoint`), and every frame goes to its view's window explicitly (`inWindow` /
`fromWindow`); nothing in the selection code converts with `nil` any more. The diagnostics line
also has the window point, UIKit's `nil` point and the recognizer's view with its bounds.
"""

from __future__ import annotations

import re

from source_policy import source

TEXT = "ios/Pudge/UI/JapaneseText.swift"


def _flat(start: str = "", end: str = "") -> str:
    text = source(TEXT)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_points_are_taken_in_a_view_and_converted_to_its_own_window() -> None:
    helpers = _flat("extension UIView {", "struct JapaneseText: UIViewRepresentable {")
    assert (
        "func inWindow(_ point: CGPoint) -> CGPoint { window.map { convert(point, to: $0) } ?? point }"
        in helpers
    )
    assert (
        "func inWindow(_ rect: CGRect) -> CGRect { window.map { convert(rect, to: $0) } ?? rect }" in helpers
    )
    assert (
        "func fromWindow(_ point: CGPoint) -> CGPoint { window.map { convert(point, from: $0) } ?? point }"
        in helpers
    )
    assert "return view.inWindow(location(in: view))" in helpers
    assert helpers.count("return view.inWindow(location(in: view))") == 2


def test_no_selection_geometry_converts_with_nil() -> None:
    body = _flat("final class JapaneseTextView", "/// Selection geometry is in window coordinates")
    log = _flat("private func logStart(", "private func endDrag()")
    rest = body.replace(log, "")
    for pattern in ("location(in: nil)", "to: nil)", "from: nil)"):
        assert pattern not in rest, pattern
    # The diagnostics compare UIKit's nil point with the one used.
    assert "recognizer.location(in: nil)" in log


def test_the_reader_gestures_use_window_points() -> None:
    for start, end in (
        ("@objc private func textPressed(", "func beginPress("),
        ("@objc private func handleMoved(", "private func follow()"),
    ):
        assert "let point = recognizer.windowPoint" in _flat(start, end)
    controller = _flat("final class TextSelectionController", "final class SelectionHandleView")
    assert "let point = touch.windowPoint(in: gestureRecognizer.view ?? host)" in controller
    hit = _flat("private func hit(at point: CGPoint, boundary: Bool", "private func logStart(")
    assert "let frame = view.inWindow(view.bounds)" in hit and "let local = view.fromWindow(point)" in hit
    view = _flat("final class JapaneseTextView", "final class TextSelectionController")
    assert "let point = touch.windowPoint(in: self)" in view
    assert "window: recognizer.windowPoint" in view


def test_the_diagnostics_line_names_the_reference_view() -> None:
    log = _flat("private func logStart(", "private func endDrag()")
    for part in (
        '" (window)"',
        '"nil-point "',
        '"reference "',
        "String(describing: type(of: source))",
        "source.inWindow(source.bounds)",
    ):
        assert part in log, part
    pressed = _flat("@objc private func textPressed(", "func beginPress(")
    assert "recognizer: recognizer)" in pressed
