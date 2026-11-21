"""iPhone/iPad readers (Swift; built and run only in Xcode on the Mac, iOS/iPadOS 17+).

policy: nothing here can compile or run the iPhone app, so these guards read its Swift source,
like tests/test_iphone_native_swift_guards.py. What the Mac sends and accepts is behaviour-tested
in tests/test_fix_ln_read_marker.py.

1. Light novels: the "read up to here" marker (pudge/ln_read_marker.py). The reader draws a thin
   red caret right before the marker's character (under the text, so nothing moves) and a ribbon
   in the margin by its line; a tap on the ribbon shows "Read up to here · set by you / automatic
   · <time>" for a moment (no hover on the phone). "Read up to here" is in the selection menu;
   the place it saves (the paragraph at the top) and the linked audiobook move the marker on the
   Mac's rules; a button goes back to it when it is off screen. Older Macs: no `marker` decodes.
   Markers set without the Mac wait in a queue kept on the device (every manual one, the newest
   automatic one per volume), each with a stable id, and go with the queued places.
2. Manga: "Hide text box outlines" is remembered; with the outlines hidden a tap is a tap on the
   page, a long press (0.4 s, the finger still, never while zooming or panning) opens the text
   box under the finger, with light haptic feedback. Shown, a tap or a long press opens it.
"""

from __future__ import annotations

import re

from source_policy import source

READ = "ios/Pudge/UI/ReadScreen.swift"
TEXT = "ios/Pudge/UI/JapaneseText.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    """Swift text with whitespace collapsed; from `start` up to the next `end` when given."""
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def _fields(struct: str) -> set[str]:
    text = source(CONTRACTS)
    body = text[text.index(f"public struct {struct}") :]
    body = body[: body.index("\n}")]
    return set(re.findall(r"public let (\w+):", body))


# ------------------------------------------------------------------ 1. read marker
def test_the_phone_decodes_the_marker_the_mac_sends_and_older_macs_still_decode() -> None:
    from pudge import ln_read_marker

    sent = ln_read_marker.phone_view(
        {"chapter_index": 0, "char_offset": 3, "source": "auto", "updated_at": 1.0, "device_id": "mac"},
        "本文です。",
    )
    assert set(sent) == _fields("ReadMarker")
    detail = _swift(CONTRACTS, "public struct LightNovelDetail", "public struct ReadMarker")
    # A `var` with a default: decoded when present, nil from older Macs.
    assert "public var marker: ReadMarker? = nil" in detail
    assert "public struct ReadMarker: Codable, Hashable {" in source(CONTRACTS)


def test_the_paragraph_draws_the_caret_and_reports_its_line() -> None:
    view = _swift(TEXT, "final class JapaneseTextView", "struct JapaneseText: UIViewRepresentable")
    assert "var marker: Int? { didSet { if marker != oldValue { setNeedsDisplay() } } }" in view
    draw = _swift(
        TEXT, "override func draw(_ rect: CGRect)", "/// UTF-16 index of the character under a point"
    )
    # Under the text (before CTFrameDraw), so the layout never moves; the line goes to the reader.
    caret = draw.index("let caret = marker.flatMap { caretRect($0, in: items) }")
    assert caret < draw.index("CTFrameDraw(frame, context)")
    assert "context.setFillColor(UIColor.systemRed.cgColor) context.fill(caret)" in draw
    assert "reportMarkerLine(caret)" in draw
    assert "if let report = onMarkerLine { DispatchQueue.main.async { report(line) } }" in view
    assert 'UIAction(title: "Read up to Here", image: UIImage(systemName: "bookmark"))' in view
    # The menu belongs to the selection (TextSelectionController): Read up to here at its start.
    assert "self?.clear(); self?.onMarkHere?(place)" in view
    assert "case .markHere(let place):" in view
    representable = _swift(TEXT, "struct JapaneseText: UIViewRepresentable", "extension ReaderStyle")
    assert (
        "view.onMarkerLine = onMarkerLine; view.onMarkHere = onMarkHere; view.marker = marker"
        in representable
    )
    assert "var marker: Int? = nil" in representable


def test_the_reader_shows_syncs_and_returns_to_the_marker() -> None:
    reader = _swift(READ, "struct NovelReaderScreen", "private struct MarkerLine")
    assert "marker = value.marker" in reader
    # One selection over the chapter's paragraphs; Read up to here at the selection's start.
    assert (
        "marker: markerOffset(index), onMarkerLine: { line in placeMarker(index, line) }, "
        "selection: textSelection, paragraphIndex: index, onCurrentLine: { line in placeSpokenLine(index, line) })"
    ) in reader
    assert (
        "textSelection.onMarkHere = { place in markHere(paragraph: place.paragraph, utf16: place.offset) }"
        in reader
    )
    # The ribbon in the margin by the line; a tap shows the label briefly.
    assert "ReadMarkerRibbon(flash: markerFlash)" in reader and ".offset(x: -24, y: line.rect.minY)" in reader
    assert ".onTapGesture { interacted(); showMarkerNote() }" in reader
    assert 'return "Read up to here · \\(kind) · \\(time)"' in reader
    assert 'let kind = value.kind == "manual" ? "set by you" : "automatic"' in reader
    assert "DispatchQueue.main.asyncAfter(deadline: .now() + 2.5)" in reader
    # Back to the marker when its paragraph is not on screen (or it is in another chapter).
    assert "if marker != nil, chapter != nil, !markerShown {" in reader
    assert "Button { jumpToMarker(proxy) }" in reader
    assert "Task { await open(marker.chapter, fraction: 0, paragraph: marker.paragraph) }" in reader
    # Its paragraph number is scrolled to only in its own chapter; another chapter (or chapters
    # not loaded yet) opens first.
    jump = _swift(READ, "private func jumpToMarker(", "private struct MarkerLine")
    assert (
        jump.index("if marker.chapter == chapterIndex { withAnimation { proxy.scrollTo(marker.paragraph")
        < jump.index("} else if detail?.chapters.contains(where: { $0.index == marker.chapter }) != false {")
        < jump.index("Task { await open(marker.chapter")
    )
    assert jump.count("proxy.scrollTo(") == 1
    assert "let paragraph = target ?? value.paragraph(atFraction: fraction)" in reader
    # The saved place (the paragraph at the top), Read up to here and the audio.
    save = _swift(READ, "private func save(force: Bool)", "// MARK: Read marker")
    assert save.index("if !moved { return }") < save.index("media.saveNovelPosition(")
    assert "paragraph: top)) }" in save and "updateMarker(" in save
    assert 'kind: "manual")' in _swift(READ, "private func markHere(", "private func followMarker(")
    follow = _swift(READ, "private func followMarker(", "private func jumpToMarker(")
    # "Auto-bookmark after 20 s" off on the Mac: the audio sends no automatic markers.
    assert follow.index("guard media.readerLook?.autoBookmarks != false else { return }") < follow.index(
        "saveReadMarker("
    )
    assert "public let autoBookmarks: Bool?" in source(CONTRACTS)
    assert "blurImages: Bool? = nil, autoBookmarks: Bool? = nil" in _swift(CONTRACTS)
    assert (
        "guard let spoken, follower.playing, Date().timeIntervalSince(markerAudioAt) > 15 else { return }"
        in follow
    )
    assert 'kind: "auto")' in follow
    assert ".onChange(of: follower.spoken) { _, value in followMarker(value) }" in _swift(READ)


def test_the_marker_requests_are_optional_for_older_macs() -> None:
    media = source(MEDIA)
    position = next(row for row in media.splitlines() if row.startswith("struct NovelPositionRequest:"))
    assert "var paragraph: Int? = nil" in position  # nil is left out: older Macs see the old body
    assert "var sentAt: Double? = Date().timeIntervalSince1970" in position
    marker = next(row for row in media.splitlines() if row.startswith("struct ReadMarkerRequest:"))
    assert "let paragraphOffset: Int; let kind: String" in marker and "var observedAt: Double?" in marker
    saved = _swift(MEDIA, "func saveNovelPosition(", "// MARK: Manga")
    assert 'let route = "/api/v2/media/ln/\\(book)/marker"' in saved
    assert "as: ReadMarkerResponse.self) else { return nil }" in saved
    assert "struct ReadMarkerResponse: Decodable { let ok: Bool?; let marker: ReadMarker? }" in media


def test_markers_set_without_the_mac_wait_in_a_queue_with_stable_ids() -> None:
    media = source(MEDIA)
    marker = next(row for row in media.splitlines() if row.startswith("struct ReadMarkerRequest:"))
    assert "var markerId: String? = nil" in marker
    pending = next(row for row in media.splitlines() if row.startswith("struct PendingReadMarker:"))
    assert (
        "let markerId: String; let book: Int; var request: ReadMarkerRequest; let serverId: String?"
        in pending
    )
    # Kept on the device (survives a restart).
    flat = _swift(MEDIA)
    assert (
        "private lazy var pendingMarkers: [PendingReadMarker] = Self.loadPendingMarkers(pendingMarkersURL)"
        in flat
    )
    assert 'appendingPathComponent("read-markers.pending.json")' in flat
    save = _swift(MEDIA, "func saveReadMarker(", "private static func loadPendingMarkers(")
    # One id per manual marker, made once: every resend carries it. Live automatic ones need none
    # (the Mac keeps ids 30 days); a queued one gets its id when it is queued.
    assert 'markerId: kind == "manual" ? UUID().uuidString : nil)' in save
    assert (
        "guard !app.macOffline, let client = app.macConnection else { queueMarker(book, request); return nil }"
        in save
    )
    # Earlier markers go first; this one waits behind them when they could not go.
    assert save.index("await sendReadMarkers(client)") < save.index(
        "switch await postMarker(client, book, request)"
    )
    assert "guard pendingMarkers.isEmpty else { queueMarker(book, request); return nil }" in save
    assert "case .later: queueMarker(book, request); return nil" in save
    assert "case .noRoute: markersUnsupported(); return nil" in save
    queue = _swift(MEDIA, "private func queueMarker(", "/// Queued markers in the order")
    assert "let id = request.markerId ?? UUID().uuidString request.markerId = id" in queue
    # An older Mac without the route: nothing is queued for it.
    assert 'unsupported == (boundServer ?? "") { return }' in queue
    # Automatic markers coalesce: only the newest per volume; manual ones all stay, in order.
    assert (
        'if request.kind == "auto" { pendingMarkers.removeAll { $0.book == book && $0.request.kind == "auto" } }'
        in queue
    )
    assert (
        "pendingMarkers.append(PendingReadMarker(markerId: id, book: book, request: request, serverId: boundServer))"
        in queue
    )
    assert "savePendingMarkers()" in queue
    send = _swift(MEDIA, "func sendReadMarkers(_ client: MacConnection) async {", "// MARK: Manga")
    assert "for item in pendingMarkers {" in send
    assert "if !isOtherMac(item.serverId) && !stale {" in send and "> 30 * 86_400" in send
    # The time it was set stays (observedAt); sentAt is the send's, for the Mac's clock correction.
    assert "var request = item.request request.sentAt = Date().timeIntervalSince1970" in send
    # Not reachable or busy (503): the rest waits. An older Mac (404): drained. Anything else
    # (the volume deleted on the Mac: 422): this one is dropped and the next one goes.
    assert (
        "switch await postMarker(client, item.book, request) { case .later: return "
        "case .noRoute: markersUnsupported(); return case .sent, .refused: break }"
    ) in send
    assert "pendingMarkers.removeAll { $0.markerId == item.markerId } savePendingMarkers()" in send
    # Sent whenever the queued places are (the Mac answers again, before switching Macs).
    positions = _swift(MEDIA, "func sendReadingPositions() async {", "// Places of removed volumes")
    assert (
        "guard let client = app.macConnection, !app.macOffline else { return } await sendReadMarkers(client)"
        in positions
    )


def test_a_marker_send_tells_later_refused_and_no_route_apart() -> None:
    post = _swift(MEDIA, "private func postMarker(", "/// The Mac this device is bound to answered")
    assert (
        'client.data(for: client.request(route, method: "POST", data: JSONEncoder().encode(request)))' in post
    )
    assert "if (200..<300).contains(status) {" in post
    assert "if status == 503 { return .later }" in post
    assert "return status == 404 ? .noRoute : .refused" in post
    assert "return unreachable(error) ? .later : .refused" in post
    # A marker that reached the Mac again lifts "older Mac".
    assert "UserDefaults.standard.removeObject(forKey: Self.noMarkerRouteKey)" in post
    drain = _swift(MEDIA, "private func markersUnsupported() {", "private static func loadPendingMarkers(")
    assert "pendingMarkers.removeAll() savePendingMarkers()" in drain


# --------------------------------------------------------------- 2. manga text boxes
def test_manga_text_box_outlines_are_remembered_and_hidden_boxes_open_on_a_long_press() -> None:
    screen = _swift(READ, "struct MangaReaderScreen", "struct MangaPageView")
    assert '@AppStorage("pudge.mangaShowTextBoxes") private var showText = true' in screen
    assert "@State private var showText" not in screen
    assert '.accessibilityLabel(showText ? "Hide text box outlines" : "Show text box outlines")' in screen
    page = _swift(READ, "struct MangaPageView", "// MARK: Pinch zoom")
    # The boxes stay for VoiceOver; only their outlines and fills go.
    # (Two pages side by side: each page has its own text boxes; see test_fix_manga_two_page_view.py.)
    assert "if let regions = texts[index]?.regions {" in page and "if showText, let regions" not in page
    assert "Rectangle().fill(showText ? PudgeTheme.accent.opacity(0.12) : Color.clear)" in page
    assert "Rectangle().stroke(showText ? PudgeTheme.accent.opacity(0.7) : Color.clear, lineWidth: 1)" in page
    assert ".accessibilityAction { onRegion(region.text) }" in page
    assert "onLongPress: { point, size in pressed(at: point, in: size) }" in page
    tapped = _swift(
        READ,
        "private func tapped(at point: CGPoint, in size: CGSize)",
        "private func pressed(",
    )
    assert "if showText, let region = region(at: point, in: size) {" in tapped
    assert tapped.index("onRegion(region.text)") < tapped.index("onBackground()")
    pressed = _swift(READ, "private func pressed(at point", "private func region(")
    assert "showText" not in pressed  # outlines shown or hidden
    assert pressed.index("UIImpactFeedbackGenerator(style: .light).impactOccurred()") < pressed.index(
        "onRegion(region.text)"
    )
    hit = _swift(READ, "private func region(at point", "private func rect(_ region: MangaRegion")
    # (An aligned page: the point on the page as drawn, tests/test_fix_manga_page_alignment_ios.py.)
    assert "rect($0, in: frame).insetBy(dx: -4, dy: -4).contains(onPage)" in hit
    assert "hits.min(by: { $0.width * $0.height < $1.width * $1.height })" in hit


def test_the_long_press_never_fights_zoom_and_pan() -> None:
    zoom = _swift(READ, "struct ZoomableContainer<Content: View>", "final class ZoomScrollView")
    assert "long.minimumPressDuration = 0.4" in zoom and "long.allowableMovement = 10" in zoom
    assert "long.delegate = context.coordinator" in zoom and "scroll.addGestureRecognizer(long)" in zoom
    assert "final class Coordinator: NSObject, UIScrollViewDelegate, UIGestureRecognizerDelegate {" in zoom
    should = _swift(READ, "func gestureRecognizerShouldBegin(", "final class ZoomScrollView")
    assert "guard onLongPress != nil, let scroll else { return false }" in should
    assert "return !(scroll.isZooming || scroll.isDragging || scroll.isDecelerating)" in should
    assert "guard recognizer.state == .began, let view = hosting.view else { return }" in zoom
    # The illustration viewer passes no long press: its recognizer never begins.
    assert "ZoomableContainer(onTap: { _, _ in dismiss() })" in _swift(READ)
