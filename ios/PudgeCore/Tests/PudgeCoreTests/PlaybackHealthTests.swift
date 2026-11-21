import XCTest
@testable import PudgeCore

final class PlaybackHealthTests: XCTestCase {
    private func sample(wanted: Double, advanced: Double, video: Bool = true, size: Double = 1080,
                        frame: Bool = true, failed: Bool = false) -> PlaybackSample {
        PlaybackSample(wantedSeconds: wanted, advancedSeconds: advanced, hasVideoTrack: video,
                       presentationWidth: size * 16 / 9, presentationHeight: size, firstFrameShown: frame, failed: failed)
    }

    func testAPlayingEpisodeWithAPictureIsHealthyAtOnce() {
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 2, advanced: 1.5)), .healthy)
        // Audio only: no picture expected.
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 2, advanced: 1.5, video: false, size: 0, frame: false)), .healthy)
    }

    func testItWaitsBeforeTheGraceEnds() {
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 0, advanced: 0, frame: false)), .wait)
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: PlaybackWatchdog.grace - 1, advanced: 0, size: 0, frame: false)), .wait)
    }

    func testTheClockAtTheStartAfterTheGraceStopsTheEpisode() {
        // The iPad: sound, the clock at 0:00, no picture.
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: PlaybackWatchdog.grace, advanced: 0, size: 0, frame: false)),
                       .stuck(.clockStopped))
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 12, advanced: 0.4)), .stuck(.clockStopped))
    }

    func testTimeMovingWithoutAPictureStopsTheEpisode() {
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 9, advanced: 8, frame: false)), .stuck(.noPicture))
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 9, advanced: 8, size: 0)), .stuck(.noPicture))
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 3, advanced: 2, frame: false)), .wait)
    }

    func testAFailedItemStopsAtOnce() {
        XCTAssertEqual(PlaybackWatchdog.judge(sample(wanted: 0, advanced: 0, failed: true)), .stuck(.failed))
    }

    func testTheMessageSaysWhy() {
        let text = PlaybackWatchdog.message(.clockStopped, device: "iPad")
        XCTAssertTrue(text.hasPrefix("This episode can't be played on this iPad: "))
        XCTAssertTrue(PlaybackWatchdog.message(.failed, detail: "Cannot Decode", device: "iPad").contains("Cannot Decode"))
        XCTAssertTrue(PlaybackWatchdog.message(.noPicture).contains("no picture"))
    }

    func testAMainThreadStallIsLoggedOncePerStall() {
        XCTAssertFalse(MainThreadStall.shouldLog(lastAnswer: 100, now: 102, logged: false))
        XCTAssertTrue(MainThreadStall.shouldLog(lastAnswer: 100, now: 100 + MainThreadStall.threshold, logged: false))
        XCTAssertFalse(MainThreadStall.shouldLog(lastAnswer: 100, now: 130, logged: true))
    }

    func testADownloadThatPlaysPasses() {
        let probe = MediaProbe(isPlayable: true, hasVideo: true, hasAudio: true, durationSeconds: 1420.1)
        XCTAssertEqual(DownloadCheck.judge(probe, expectedSeconds: 1420), .ok)
        // The Mac's duration is the source's (an mkv may count its subtitles longer): a little shorter is fine.
        XCTAssertEqual(DownloadCheck.judge(probe, expectedSeconds: 1440), .ok)
        XCTAssertEqual(DownloadCheck.judge(probe, expectedSeconds: nil), .ok)
    }

    func testADownloadThatDoesNotPlayIsRefused() {
        func bad(_ probe: MediaProbe, _ expected: Double? = 1420) -> Bool {
            if case .bad = DownloadCheck.judge(probe, expectedSeconds: expected) { return true }
            return false
        }
        XCTAssertTrue(bad(MediaProbe(isPlayable: false, hasVideo: true, hasAudio: true, durationSeconds: 1420)))
        XCTAssertTrue(bad(MediaProbe(isPlayable: true, hasVideo: false, hasAudio: true, durationSeconds: 1420)))
        XCTAssertTrue(bad(MediaProbe(isPlayable: true, hasVideo: true, hasAudio: false, durationSeconds: 1420)))
        XCTAssertTrue(bad(MediaProbe(isPlayable: true, hasVideo: true, hasAudio: true, videoDecodable: false, durationSeconds: 1420)))
        XCTAssertTrue(bad(MediaProbe(isPlayable: true, hasVideo: true, hasAudio: true, durationSeconds: nil)))
        // Cut in the middle: much shorter than the Mac's copy.
        XCTAssertTrue(bad(MediaProbe(isPlayable: true, hasVideo: true, hasAudio: true, durationSeconds: 700)))
    }

    func testOnlyOtherBytesAreDownloadedOnceMore() {
        XCTAssertEqual(DownloadCheck.next(after: .integrity, attempt: 1), .redownload)
        XCTAssertEqual(DownloadCheck.next(after: .integrity, attempt: 2), .giveUp)
        // The Mac's own bytes would only come again the same.
        XCTAssertEqual(DownloadCheck.next(after: .playability, attempt: 1), .giveUp)
        XCTAssertEqual(DownloadCheck.durationTolerance(10), 2)
        XCTAssertEqual(DownloadCheck.durationTolerance(1000), 30, accuracy: 1e-9)
    }

    func testTheH264LevelIsReadFromAvcC() {
        let value = VideoFormatInfo.avcProfileLevel([1, 100, 0, 41, 0xff])
        XCTAssertEqual(value?.profile, 100)
        XCTAssertEqual(value?.level, 41)
        XCTAssertNil(VideoFormatInfo.avcProfileLevel([0, 100, 0, 41]))
        XCTAssertNil(VideoFormatInfo.avcProfileLevel([1, 100]))
        XCTAssertEqual(VideoFormatInfo.describe(profile: 100, level: 41), "High@4.1")
        XCTAssertEqual(VideoFormatInfo.describe(profile: 77, level: 51), "Main@5.1")
        XCTAssertEqual(VideoFormatInfo.describe(profile: 1, level: 30), "profile 1@3.0")
    }

    /// REVIEW (ipad-seqpre #8): back from the background the monitor's own timer did not run
    /// either: no "main thread not answering" line for the time the app was suspended.
    func testASuspendedAppIsNoMainThreadStall() {
        XCTAssertFalse(MainThreadStall.resumed(lastTick: 100, now: 101))
        XCTAssertTrue(MainThreadStall.resumed(lastTick: 100, now: 100 + 600))
    }
}
