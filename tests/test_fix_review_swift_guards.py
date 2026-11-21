"""External review, iPhone/iPad player (Swift).

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (PlayerPolishTests, PlaybackHealthTests); the AVFoundation wiring is
guarded here by reading the Swift source:

1. AVPlayerItem's "played to its end" notice closes the player only at the episode's end and
   never for a stream with a problem or waiting for the Mac (a stream cut short posted it
   mid-episode: the player closed and the episode counted as watched).
2. The main-thread monitor does not log the time the app spent suspended as a stall.
3. MediaModel: an episode download cut by the Mac keeps its partial file at launch (resumed
   later); a place the Mac refuses no longer stops every later place from being sent; non-finite
   positions/durations never trap in Int64(); a paused audio transfer keeps its resume data.
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"


def _flat(relative: str, start: str, end: str) -> str:
    text = source(relative)
    at = text.index(start)
    return re.sub(r"\s+", " ", text[at : text.index(end, at + len(start))])


def test_the_end_notice_is_checked_like_the_periodic_end() -> None:
    observer = _flat(WATCH, "private func watchEnd(", "private func finish()")
    assert "PlaybackEnd.noticeIsEnd(positionMs: position, durationMs: self.durationMs)" in observer
    assert "self.streamProblem == nil, !self.waitingForMac" in observer
    assert "seconds.isFinite" in observer  # currentTime() can be NaN: never Int64(NaN)
    assert observer.index("noticeIsEnd") < observer.index("self.finish()")
    core = source("ios/PudgeCore/Sources/PudgeCore/PlayerPolish.swift")
    assert "public static func noticeIsEnd(positionMs: Int64, durationMs: Int64) -> Bool" in core
    assert "func testTheEndNoticeEndsTheEpisodeOnlyAtItsEnd()" in source(
        "ios/PudgeCore/Tests/PudgeCoreTests/PlayerPolishTests.swift"
    )


def test_a_suspended_app_is_no_main_thread_stall() -> None:
    monitor = _flat(WATCH, "final class MainThreadMonitor", "final class VideoController")
    assert "MainThreadStall.resumed(lastTick: lastTick, now: now)" in monitor
    assert monitor.index("MainThreadStall.resumed") < monitor.index("MainThreadStall.shouldLog")
    assert "func testASuspendedAppIsNoMainThreadStall()" in source(
        "ios/PudgeCore/Tests/PudgeCoreTests/PlaybackHealthTests.swift"
    )


MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"


def test_episode_downloads_resume_and_place_sync_go_on() -> None:
    model = re.sub(r"\s+", " ", source(MEDIA))
    assert "let resumable: Set<Int> = bound.map { Self.interrupted($0) } ?? []" in model
    assert "!resumable.contains(Int(file.deletingPathExtension().lastPathComponent) ?? -1)" in model
    assert "dropStale(item, after: error); return }" not in model
    assert "Int64(episode.position * 1000)" not in model and "Int64(episode.duration * 1000)" not in model
    transfers = source("ios/Pudge/Features/Transfers/TransferManager.swift")
    assert (
        "if let data = (error as NSError).userInfo[NSURLSessionDownloadTaskResumeData] as? Data { record.resumeData = data }"
        in transfers
    )
