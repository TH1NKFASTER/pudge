"""XDEV (iPhone/iPad, Swift runs only in Xcode on the Mac): the player sends its place to the Mac
while it plays (PositionSyncSchedule: every 15 s, pause, seek, background), with the parts played
here for the watched credit summed across devices; a place the Mac counted marks the watch here."""

from __future__ import annotations

from source_policy import source

CORE = "ios/PudgeCore/Sources/PudgeCore/PositionSync.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PositionSyncTests.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
MODEL = "ios/Pudge/Features/Media/MediaModel.swift"
APP = "ios/Pudge/App/PudgeApp.swift"


def test_core_schedule_and_ranges_have_xctests():
    core = source(CORE)
    assert "public struct PositionSyncSchedule" in core and "period: TimeInterval = 15" in core
    assert "case paused, closed, background" in core
    assert "public enum WatchedRanges" in core
    tests = source(CORE_TESTS)
    for name in (
        "testWhilePlayingThePlaceGoesEveryPeriod",
        "testASeekSendsAtOnce",
        "testPauseSendsAtOnce",
        "testCloseAndBackgroundAlwaysSend",
        "testWatchedRangesAreTheSecondsPlayedAsParts",
    ):
        assert name in tests


def test_player_sends_while_playing_on_pause_and_in_the_background():
    watch = source(WATCH)
    assert "@State private var positionSync = PositionSyncSchedule()" in watch
    assert "positionSync.observe(positionMs: value, at: now, playing: video.playing)" in watch
    assert "positionSync.event(.paused" in watch
    assert "@Environment(\\.scenePhase) private var phase" in watch
    assert "newPhase == .background, positionSync.event(.background" in watch
    assert "syncPlace(video.timeMs, background: true)" in watch
    assert "media.syncPlayingPosition(episode.id" in watch
    # The old place-only save every 15 s (never sent while playing) is gone.
    assert "lastSaved" not in watch


def test_model_sends_parts_and_takes_the_macs_count():
    model = source(MODEL)
    assert (
        "func syncPlayingPosition(_ id: Int, positionMs: Int64, seconds: IndexSet, background: Bool = false)"
        in model
    )
    assert 'beginBackgroundTask(withName: "Pudge episode place")' in model
    assert "var ranges: [[Double]]? = nil" in model
    assert "WatchedRanges.ranges($0)" in model
    assert "as: EpisodePositionReceipt.self" in model
    assert "receipt.counted == true" in model
    assert "await model.media.sendVideoPositions()" in source(APP)
