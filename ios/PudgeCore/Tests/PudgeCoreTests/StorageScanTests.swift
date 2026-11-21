import Foundation
import XCTest
@testable import PudgeCore

/// Settings → Storage counts only what its lists reach, plus "Other files"; leftovers nobody
/// references go at launch (`swift test --package-path ios/PudgeCore` on the Mac).
final class StorageScanTests: XCTestCase {
    private let now = 10_000_000.0
    private func file(_ path: String, _ bytes: Int64 = 100, age: Double = 86_400) -> StoredFile {
        StoredFile(path: path, bytes: bytes, modified: now - age)
    }

    func testListedDownloadsAreTheirKindAndNeverLeftovers() {
        let refs = StorageReferences(episodes: [12], episodeMarks: [7], readingFolders: ["ln-3"], audioFiles: ["resources/a.m4a"])
        let files = [file("store/video/12.mp4", 1_000), file("store/video/12.cues.json", 10), file("store/video/12.json", 1),
                     file("store/video/7.json", 1), file("store/reading/ln-3/download.json", 5), file("store/reading/ln-3/text/1.json", 50),
                     file("store/reading/manga-4.pending.json", 2), file("store/resources/a.m4a", 400),
                     file("store/image-cache/cover_anime_1", 30), file("store/offline-words.json", 20), file("store/state.sqlite", 8),
                     file("store/state.sqlite-wal", 8), file("store/logs/app.log", 9), file("store/grammar-chats.json", 3)]
        let summary = StorageScan.summarize(files, refs, now: now)
        XCTAssertEqual(summary[.episodes], 1_011)
        XCTAssertEqual(summary[.reading], 55)
        XCTAssertEqual(summary[.audio], 400)
        XCTAssertEqual(summary[.covers], 30)
        XCTAssertEqual(summary[.words], 20)
        XCTAssertEqual(summary[.database], 1 + 2 + 16)
        XCTAssertEqual(summary[.logs], 9)
        XCTAssertEqual(summary[.texts], 3)
        XCTAssertEqual(summary.otherBytes, 0)
        XCTAssertEqual(summary.total, files.reduce(0) { $0 + $1.bytes })
        XCTAssertTrue(StorageScan.launchCleanup(files, refs, now: now).isEmpty)
    }

    /// The phantom "1.5 GB of episodes": a video whose record went (or never got written: the
    /// app ended between moving the video in and writing its record) is no download in any list.
    func testAVideoWithoutItsRecordIsAnOtherFileRemovedAtLaunch() {
        let refs = StorageReferences()
        let files = [file("store/video/12.mp4", 1_500_000_000), file("store/video/12.cues.json", 10)]
        let summary = StorageScan.summarize(files, refs, now: now)
        XCTAssertEqual(summary[.episodes], 0)
        XCTAssertEqual(summary.otherBytes, 1_500_000_010)
        XCTAssertEqual(summary.clearableBytes, 1_500_000_010)
        XCTAssertEqual(StorageScan.launchCleanup(files, refs, now: now).map(\.path), files.map(\.path))
    }

    func testARecordThatCouldNotBeReadWaitsForClear() {
        let files = [file("store/video/12.mp4", 1_000), file("store/video/12.json", 1),
                     file("store/reading/ln-9/download.json", 1), file("store/reading/ln-9/text/1.json", 9)]
        let summary = StorageScan.summarize(files, StorageReferences(), now: now)
        XCTAssertEqual(summary.otherBytes, 1_011)
        XCTAssertEqual(summary.clearable.count, 4)
        XCTAssertTrue(StorageScan.launchCleanup(files, StorageReferences(), now: now).isEmpty)
    }

    func testAudiobookCoversAndTextsAreNotAudio() {
        let refs = StorageReferences(audioFiles: ["resources/a.m4b", "resources/c.jpg", "resources/t.json"])
        let summary = StorageScan.summarize([file("store/resources/a.m4b", 900), file("store/resources/c.jpg", 20),
                                             file("store/resources/t.json", 7)], refs, now: now)
        XCTAssertEqual(summary[.audio], 900)
        XCTAssertEqual(summary[.covers], 20)
        XCTAssertEqual(summary[.texts], 7)
    }

    func testInterruptedDownloadsStayWhileSomethingDownloads() {
        let files = [file("store/video/12.part", 700, age: 4 * 86_400), file("store/reading/ln-3.part", 50), file("store/reading/manga-4.ocr.part", 5),
                     file("store/reading/manga-4.ocr/1.json", 5), file("store/reading/ln-5.unpacking/a", 5),
                     file("store/resources/x.partial", 40), file("tmp/CFNetworkDownload_ab12.tmp", 900)]
        let busy = StorageReferences(downloading: true)
        let during = StorageScan.summarize(files, busy, now: now)
        XCTAssertEqual(during.otherBytes, 1_705)
        XCTAssertTrue(during.clearable.isEmpty)
        XCTAssertTrue(StorageScan.launchCleanup(files, busy, now: now).isEmpty)
        let idle = StorageReferences()
        XCTAssertEqual(StorageScan.summarize(files, idle, now: now).clearableBytes, 1_705)
        XCTAssertEqual(StorageScan.launchCleanup(files, idle, now: now).count, files.count)
    }

    func testBackgroundSessionFilesStayWhileAnAudiobookCanResume() {
        let files = [file("sessions/Downloads/app.pudge/CFNetworkDownload_1.tmp", 300), file("store/resources/old.mp3", 200)]
        let resumable = StorageReferences(audioResumable: true)
        XCTAssertTrue(StorageScan.launchCleanup(files, resumable, now: now).isEmpty)
        XCTAssertTrue(StorageScan.summarize(files, resumable, now: now).clearable.isEmpty)
        // An audiobook file no resource record references any more: an other file.
        let idle = StorageScan.summarize(files, StorageReferences(), now: now)
        XCTAssertEqual(idle.otherBytes, 500)
        XCTAssertEqual(StorageScan.launchCleanup(files, StorageReferences(), now: now).count, 2)
    }

    func testNewFilesAreNotRemovedAtLaunch() {
        let fresh = [file("tmp/CFNetworkDownload_2.tmp", 10, age: 60), file("store/video/3.mp4", 10, age: 60)]
        XCTAssertTrue(StorageScan.launchCleanup(fresh, StorageReferences(), now: now).isEmpty)
        XCTAssertEqual(StorageScan.summarize(fresh, StorageReferences(), now: now).clearable.count, 2)
    }

    func testEpisodeIdsOfAPreviousMacAreBelowZero() {
        let refs = StorageReferences(episodes: [-2])
        let summary = StorageScan.summarize([file("store/video/-2.mp4", 10), file("store/video/-2.json", 1)], refs, now: now)
        XCTAssertEqual(summary[.episodes], 11)
        XCTAssertEqual(StorageScan.episodeId("12.cues.json"), 12)
        XCTAssertNil(StorageScan.episodeId("notes.txt"))
    }
}
