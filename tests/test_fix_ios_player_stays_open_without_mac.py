"""The iPad player stays open while the Mac restarts; the count follows a watch at once.

Swift cannot be built here: these guard the wiring of the pure logic in
ios/PudgeCore (PlayerContinuity.swift, tested by PlayerContinuityTests on the Mac).
"""

from __future__ import annotations

from source_policy import source

APP = "ios/Pudge/App/PudgeApp.swift"
APP_MODEL = "ios/Pudge/App/PudgeModel.swift"
MODEL = "ios/Pudge/Features/Media/MediaModel.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
CORE = "ios/PudgeCore/Sources/PudgeCore/PlayerContinuity.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PlayerContinuityTests.swift"


def _body(text: str, start: str) -> str:
    return text.split(start, 1)[1].split("\n    }\n", 1)[0]


def test_app_alert_never_presents_over_the_full_screen_player():
    app = source(APP)
    assert "import PudgeCore" in app
    assert "PlayerContinuity.showsAlert(message: model.message, videoOnScreen: model.videoOnScreen)" in app
    assert "get: { model.message != nil }" not in app
    assert "@Published var videoOnScreen = false" in source(APP_MODEL)
    model = source(MODEL)
    assert "app.videoOnScreen = true" in model.split("func playerOpened(_ id: Int)", 1)[1].split("\n", 1)[0]
    closed = _body(model, "    func playerClosed(_ id: Int) {")
    assert "self.app.videoOnScreen = !self.playingEpisodes.isEmpty" in closed
    assert "Task.sleep" in closed


def test_a_refresh_cut_by_the_mac_going_away_is_not_an_alert():
    refresh = _body(source(APP_MODEL), "    private func refreshOnce() async {")
    assert "TransferRetry.isTransient(error)" in refresh
    assert "macAvailable = false" in refresh


def test_stream_from_the_mac_reconnects_by_itself():
    watch = source(WATCH)
    assert "@Published var streamFailure: VideoStreamFailure?" in watch
    assert ".task(id: video.streamProblem)" in watch
    assert "PlayerContinuity.reconnects(after: video.streamFailure)" in watch
    assert "await retryStream(quiet: true)" in watch
    # The player is never closed by a Mac failure: only the user's Close / Download.
    # (Download, Close and the X of the player, Cancel of the gate; the timing wheel is a panel
    # in the player now, closed by a tap outside, no Done) and, since IOS3, the episode played
    # to its end (never set while the Mac's problem shows: VideoController.finish callers).
    # IOS5: the anime title screen's episode sheet (Cancel, Download) closes only itself.
    sheet_at = watch.index("struct EpisodeDownloadSheet: View {")
    sheet = watch[sheet_at : watch.index("struct EpisodeRow: View {", sheet_at)]
    assert sheet.count("dismiss()") == 2
    # IPADPLAY: the X and Close of the player share closePlayer() (one dismiss(); also Close of
    # "can't be played here", which only a downloaded episode's watchdog shows, never the Mac).
    assert watch.replace(sheet, "").count("dismiss()") == 4
    assert watch.count("closePlayer()") == 5  # its definition, the X, Close (Mac), Re-download, Close
    assert "if self.streamProblem == nil, !self.waitingForMac," in watch
    assert "streamFailure = failure" in watch


def test_watch_on_this_device_counts_at_once_and_the_list_is_asked_again():
    model = source(MODEL)
    assert "@Published private(set) var watchedHere: [Int: LocalAnimeWatch]" in model
    assert "if counted { noteWatched(item) }" in model
    send = _body(model, "    func sendVideoPositions() async {")
    assert "if counted { Task { await refreshEpisodes() } }" in send
    assert "watchedHere[item.id]?.deliveredAt = Date().timeIntervalSince1970" in send
    refresh = _body(model, "    private func loadEpisodes(live: Bool) async {")
    assert refresh.index("let requested = Date().timeIntervalSince1970") < refresh.index(
        '"/api/v2/media/anime"'
    )
    assert "animeListRequestedAt = requested" in refresh
    watch = source(WATCH)
    assert "return media.shown(result)" in watch
    assert "return media.shown([value]).first ?? value" in watch  # REVIEW: never index an empty answer
    assert "else { Task { await media.refreshEpisodes() } }" in watch


def test_core_logic_has_its_xctests():
    core = source(CORE)
    for name in ("enum AnimeProgressOverlay", "struct LocalAnimeWatch", "enum PlayerContinuity"):
        assert name in core
    tests = source(CORE_TESTS)
    assert "testEpisodeWatchedHereRaisesTheCountOfAnOlderList" in tests
    assert "testAlertWaitsWhileTheVideoIsOnScreen" in tests
