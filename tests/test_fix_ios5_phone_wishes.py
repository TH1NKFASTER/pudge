"""iPhone/iPad wishes (IOS5): no "On iPhone/iPad" note section and no "Audiobooks, reading,
episodes…" line in Settings, a download button on the anime title screen (one episode at once,
several through a sheet of episodes to tick), and the player's touch filter in the selection's
own window after a rotation.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The picker's
decisions are PudgeCore code with XCTest (EpisodeDownloadPickerTests); the SwiftUI and UIKit
wiring is guarded here by reading the Swift source.
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
SETTINGS = "ios/Pudge/UI/SettingsScreen.swift"
STATS = "ios/Pudge/UI/StatsScreen.swift"
PICKER = "ios/PudgeCore/Sources/PudgeCore/EpisodeDownloadPicker.swift"
PICKER_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/EpisodeDownloadPickerTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_settings_has_no_on_device_section_and_no_kinds_line() -> None:
    settings = _flat(SETTINGS)
    assert 'Section("On \\(PudgeTheme.device)")' not in settings
    assert "Local audio, position and bookmarks work offline." not in settings
    assert "Audiobooks, reading, episodes and study with the Mac" not in settings
    # The rest of the bottom stays.
    pudge = _flat(SETTINGS, 'Section("Pudge") {', ".scrollContentBackground(")
    assert 'LabeledContent("Version", value: PudgeModel.appVersion)' in pudge
    assert 'LabeledContent("Positions waiting to be sent", value: String(pending))' in pudge
    assert "if let issue = model.syncIssue { Text(issue).foregroundStyle(.orange) }" in pudge
    assert settings.index('Section("Mac") {') < settings.index('Section("Storage") {')
    # Statistics has no per-device section either.
    assert "On \\(PudgeTheme.device)" not in _flat(STATS)


def test_picker_decisions_have_xctests() -> None:
    picker = _flat(PICKER)
    assert "public enum EpisodeDownloadPicker {" in picker
    assert "public struct EpisodePickRow: Equatable, Sendable {" in picker
    for name in ("action", "notWatched", "next", "initial", "toggled", "chosen"):
        assert f"public static func {name}(" in picker, name
    tests = source(PICKER_TESTS)
    for name in (
        "testOneEpisodeDownloadsAtOnceAndSeveralOpenTheSheet",
        "testEpisodesHereOrOnTheirWayStayFixed",
        "testAllNotWatchedSkipsWatchedAndThoseHere",
        "testNextNStartsAtTheNextToWatch",
        "testTheSheetOpensWithTheNextEpisodeTicked",
    ):
        assert f"func {name}()" in tests, name


def test_anime_title_screen_has_a_download_button_top_left() -> None:
    detail = _flat(WATCH, "struct AnimeDetailScreen: View {", "struct EpisodeDownloadSheet: View {")
    assert "let pickRows = EpisodeDownloadSheet.rows(value.episodes, media: media)" in detail
    assert "let pickAction = EpisodeDownloadPicker.action(pickRows)" in detail
    toolbar = detail[detail.index(".toolbar {") :]
    assert "ToolbarItem(placement: .topBarLeading) {" in toolbar
    # One episode: the same download as its own Download button (Settings' quality).
    assert (
        "case .download(let id): if let item = value.episodes.first(where: { $0.id == id }) "
        "{ Task { await media.download(item) } }" in toolbar
    )
    assert "case .choose: picking = true" in toolbar
    assert (
        ".disabled(pickAction == .unavailable || media.app.macConnection == nil || media.app.macOffline)"
        in toolbar
    )
    assert (
        ".sheet(isPresented: $picking) { EpisodeDownloadSheet(media: media, episodes: value.episodes, "
        "mediaId: value.mediaId) }" in toolbar
    )
    # The "Download all" row stays.
    assert 'Label("Download all \\(missing.count)"' in detail


def test_the_sheet_queues_through_download_all() -> None:
    sheet = _flat(WATCH, "struct EpisodeDownloadSheet: View {", "struct EpisodeRow: View {")
    # Here, downloading or queued: fixed and ticked.
    assert "media.localEpisodes[item.id] != nil ? .onDevice" in sheet
    assert (
        "(media.videoJobs[item.id] != nil || media.videoQueue.contains { $0.id == item.id }) ? .downloading"
        in sheet
    )
    assert "watched: item.watched || media.finishedHere(item)" in sheet
    assert 'Button("All not watched") { selection = EpisodeDownloadPicker.notWatched(rows) }' in sheet
    assert "selection = EpisodeDownloadPicker.next(count, in: rows, from: next)" in sheet
    assert "let next = media.nextToWatchNumber(mediaId)" in sheet
    assert "selection = EpisodeDownloadPicker.toggled(selection, id: row.id, in: rows)" in sheet
    assert ".disabled(row.fixed)" in sheet
    assert 'row.fixed || selection.contains(row.id) ? "checkmark.circle.fill" : "circle"' in sheet
    # The download queue keeps its nearest-first order (downloadAll arranges it).
    assert "let chosen = EpisodeDownloadPicker.chosen(selection, in: rows)" in sheet
    assert "media.downloadAll(items)" in sheet
    assert "await media.download(" not in sheet
    media = _flat("ios/Pudge/Features/Media/MediaModel.swift", "func downloadAll(", "func nextToWatchNumber(")
    assert "EpisodeDownloadOrder.arrange(waiting" in media


def test_the_player_touch_filter_uses_the_layers_window() -> None:
    layer = _flat(WATCH, "struct VideoGestureLayer: UIViewRepresentable {", "final class VideoController")
    receive = layer[
        layer.index("shouldReceive touch: UITouch) -> Bool {") : layer.index(
            "func gestureRecognizerShouldBegin("
        )
    ]
    assert "let point = touch.windowPoint(in: recognizer.view)" in receive
    assert receive.index("let point = touch.windowPoint(in: recognizer.view)") < receive.index(
        "TextSelectionController.activeOwnsTouch(at: point)"
    )
    code = re.sub(r"//[^\n]*", "", source(WATCH))
    for pattern in ("location(in: nil)", "to: nil)", "from: nil)"):
        assert pattern not in code, pattern
    touch = _flat(WATCH, "final class PlayerTouchView: UIView {", "final class VideoController")
    assert "selection.handle(at: inWindow(point)) != nil" in touch
    assert "let area = base.width > 60 ? fromWindow(base) : bounds.inset(by: safeAreaInsets)" in touch
