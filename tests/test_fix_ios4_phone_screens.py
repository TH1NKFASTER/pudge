"""iPhone/iPad screens (IOS4): the player's two subtitle wheels apart, qualities named by their
resolution, one pull-down header on every library, a tidier Audiobooks screen and Settings.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (PhoneScreenWishesTests: PlayerWheelPanel, VideoQuality titles,
LibraryFilter, StorageSizeText); the SwiftUI wiring is guarded here by reading the Swift source.
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
READ = "ios/Pudge/UI/ReadScreen.swift"
LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"
GRID = "ios/Pudge/UI/LibraryGrid.swift"
SETTINGS = "ios/Pudge/UI/SettingsScreen.swift"
CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift"
GESTURES = "ios/PudgeCore/Sources/PudgeCore/PlayerGestures.swift"
CARDS = "ios/PudgeCore/Sources/PudgeCore/LibraryCards.swift"
STORAGE = "ios/PudgeCore/Sources/PudgeCore/StorageScan.swift"
TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PhoneScreenWishesTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_core_decisions_have_xctests() -> None:
    tests = source(TESTS)
    for name in (
        "testEachWheelHasItsOwnPanelAndOnlyOneIsOpen",
        "testQualitiesAreNamedByTheirResolutionOnly",
        "testTheHeaderFiltersByTitleAndByWhatIsOnThisDevice",
        "testAnEmptyStorageCategoryShowsADash",
    ):
        assert f"func {name}()" in tests
    assert "public enum PlayerWheelPanel" in source(GESTURES)
    assert "public struct LibraryFilter" in source(CARDS)
    assert "public enum StorageSizeText" in source(STORAGE)


def test_timing_and_text_size_have_their_own_buttons_and_panels() -> None:
    watch = _flat(WATCH)
    # One panel at a time (no Bool shared by both wheels).
    assert "@State private var wheelPanel: PlayerWheelPanel?" in watch
    assert "@State private var timing = false" not in watch
    top = _flat(WATCH, "private var topBar: some View {", "/// A line without words")
    assert "Button { toggleWheelPanel(.timing) }" in top
    assert "Button { toggleWheelPanel(.size) }" in top
    assert 'Section("Subtitle timing and size")' not in watch
    assert "wheelPanel = PlayerWheelPanel.tapped(panel, open: wheelPanel)" in watch
    panel = _flat(
        WATCH, "private func wheelPanelView(_ panel: PlayerWheelPanel)", "// MARK: Skip opening / ending"
    )
    assert "case .timing: SubtitleTimingSheet(delays: delays, episodeId: episode.id)" in panel
    assert "case .size: SubtitleSizeSheet(textPercent: $textPercent)" in panel
    timing = _flat(WATCH, "struct SubtitleTimingSheet: View {", "struct SubtitleSizeSheet: View {")
    size = _flat(WATCH, "struct SubtitleSizeSheet: View {", "struct CompactWheel")
    # Each panel holds one wheel only.
    assert timing.count("CompactWheel(") == 1 and "SubtitleTextSize" not in timing
    assert size.count("CompactWheel(") == 1 and "SubtitleDelayWheel" not in size
    assert "wheelPanel != nil || lookup != nil" in watch  # an open panel keeps the controls


def test_qualities_show_only_the_resolution() -> None:
    quality = _flat(CONTRACTS, "public enum VideoQuality", "/// Bits per second")
    assert 'case .smaller: return "720p"' in quality
    assert 'case .saver: return "540p"' in quality
    assert "Smaller" not in quality and "saver (" not in quality.lower()
    assert "Data saver" not in source(WATCH) + source(SETTINGS)
    # Sizes stay next to the name in the Download menu.
    assert (
        'return quality.title + " · " + (size.estimated ? "~" : "") + PudgeTheme.size(size.bytes)'
        in _flat(WATCH)
    )


def test_every_library_has_the_same_pull_down_header() -> None:
    grid = _flat(GRID, "private struct LibraryFilterHeader: ViewModifier {", "extension PudgeTheme {")
    assert "placement: .navigationBarDrawer(displayMode: .automatic)" in grid
    assert ".searchScopes($filter.scope, activation: .onSearchPresentation)" in grid
    assert "Text(scope.title(device: device)).tag(scope)" in grid
    # Cancel shows everything again: nothing stays filtered with the header gone.
    assert ".onChange(of: presented) { _, open in if !open { filter = LibraryFilter() } }" in grid
    watch = _flat(WATCH, "struct WatchScreen: View {", "struct DownloadedEpisodesRoute")
    read = _flat(READ)
    library = _flat(LIBRARY, "struct LibraryScreen: View {", "struct BookSeriesRoute")
    assert '.libraryFilterHeader($filter, prompt: "Title", device: PudgeTheme.device)' in watch
    assert read.count('.libraryFilterHeader($filter, prompt: "Title", device: PudgeTheme.device)') == 2
    assert '.libraryFilterHeader($filter, prompt: "Book title", device: PudgeTheme.device)' in library
    for screen in (watch, read, library):
        assert ".searchable(" not in screen
        assert "@State private var filter = LibraryFilter()" in screen
    # What "on this device" means on each screen.
    assert 'onDevice: media.isDownloaded("ln", $0.id)' in read
    assert 'onDevice: media.isDownloaded("manga", $0.id)' in read
    assert "group.episodes.contains { media.localEpisodes[$0.id] != nil }" in watch
    assert "let shelves = shownSections" in watch
    assert "onDevice: (try? model.store.ready($0)) == true" in library
    assert "onDevice: false" in library  # the Mac's lazy catalog is never on the device


def test_audiobooks_screen_has_no_status_toggle_row_or_downloads_button() -> None:
    library = _flat(LIBRARY, "struct LibraryScreen: View {", "struct BookSeriesRoute")
    assert '"Mac available"' not in library
    assert "Toggle(" not in library and "localOnly" not in library
    assert "showTransfers" not in library and 'Label("Downloads"' not in library
    assert "TransfersScreen(" not in library
    # Kept: both sorts and "+" (import audio from the Files app).
    assert 'Text("Recently played").tag(true)' in library and 'Text("Title").tag(false)' in library
    assert 'Label("Import audio", systemImage: "plus")' in library
    assert ".fileImporter(isPresented: $importing" in library
    # A problem is still said (no Mac paired, Mac out of reach).
    assert "} else if model.macOffline {" in library


def test_settings_on_device_section_and_storage_dashes() -> None:
    settings = _flat(SETTINGS)
    # IOS5: the "On iPhone/iPad" section (a note only) is gone; Storage follows Mac.
    assert 'Section("On \\(PudgeTheme.device)")' not in settings
    assert 'LabeledContent("Audiobooks"' not in settings
    bottom = _flat(SETTINGS, 'Section("Pudge") {', ".scrollContentBackground(")
    assert bottom.index('LabeledContent("Version"') < bottom.index(
        'LabeledContent("Positions waiting to be sent"'
    )
    assert 'Section("Pudge") {' in settings and settings.index('Section("Diagnostics")') < settings.index(
        'Section("Pudge") {'
    )
    # Download over cellular is kept: it gates URLSession for a Mac reached by VPN / DNS.
    assert 'Toggle("Download over cellular", isOn: $cellularDownloads)' in settings
    assert "allowsCellularAccess" in source("ios/Pudge/Features/Transfers/TransferManager.swift")
    # Storage: "—" for an empty category.
    for row in ("Audio", "Covers", "Episodes", "Light novels and manga", "Pudge total"):
        assert f'LabeledContent("{row}", value: PudgeTheme.storageSize(' in settings, row
    assert (
        "static func storageSize(_ bytes: Int64) -> String { StorageSizeText.text(bytes, formatted: size) }"
        in (_flat(LIBRARY))
    )
