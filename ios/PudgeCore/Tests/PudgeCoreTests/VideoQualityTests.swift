import Foundation
import XCTest
@testable import PudgeCore

/// Smaller copies of an episode (Settings → Downloads → Download / Stream quality).
final class VideoQualityTests: XCTestCase {
    func testEstimatesFollowTheMacsBitRates() {
        let minutes24 = 24.0 * 60
        XCTAssertNil(VideoQuality.original.estimatedBytes(seconds: minutes24))
        let smaller = VideoQuality.smaller.estimatedBytes(seconds: minutes24)!
        let saver = VideoQuality.saver.estimatedBytes(seconds: minutes24)!
        XCTAssertTrue((250_000_000...300_000_000).contains(smaller), "\(smaller)")
        XCTAssertTrue((130_000_000...160_000_000).contains(saver), "\(saver)")
        XCTAssertNil(VideoQuality.smaller.estimatedBytes(seconds: 0))
        XCTAssertNil(VideoQuality.smaller.estimatedBytes(seconds: .nan))
    }

    func testTheMacsSizeWinsOverTheEstimate() {
        let options = [VideoQualityOption(id: "original", sizeBytes: 1_480_000_000, prepared: true, estimated: nil),
                       VideoQualityOption(id: "smaller", sizeBytes: 260_000_000, prepared: false, estimated: true),
                       VideoQualityOption(id: "saver", sizeBytes: nil, prepared: false, estimated: nil)]
        XCTAssertEqual(VideoQuality.original.size(in: options, durationSeconds: 1440)?.bytes, 1_480_000_000)
        XCTAssertEqual(VideoQuality.original.size(in: options, durationSeconds: 1440)?.estimated, false)
        XCTAssertEqual(VideoQuality.smaller.size(in: options, durationSeconds: 1440)?.bytes, 260_000_000)
        XCTAssertEqual(VideoQuality.smaller.size(in: options, durationSeconds: 1440)?.estimated, true)
        // Not sized by the Mac: from the duration; the original then has no size.
        XCTAssertEqual(VideoQuality.saver.size(in: options, durationSeconds: 1440)?.bytes, VideoQuality.saver.estimatedBytes(seconds: 1440))
        XCTAssertNil(VideoQuality.original.size(in: nil, durationSeconds: 1440))
    }

    func testTheRequestOfTheOriginalIsTheEmptyBodyOfBefore() throws {
        XCTAssertEqual(String(data: try JSONEncoder().encode(VideoQualityRequest(.original)), encoding: .utf8), "{}")
        XCTAssertEqual(String(data: try JSONEncoder().encode(VideoQualityRequest(.saver)), encoding: .utf8), #"{"quality":"saver"}"#)
    }

    func testTheSettingDefaultsToTheOriginal() {
        let defaults = UserDefaults(suiteName: "video-quality-\(UUID().uuidString)")!
        XCTAssertEqual(VideoQuality.stored(VideoQuality.downloadKey, defaults), .original)
        defaults.set("smaller", forKey: VideoQuality.downloadKey)
        XCTAssertEqual(VideoQuality.stored(VideoQuality.downloadKey, defaults), .smaller)
        defaults.set("8k", forKey: VideoQuality.downloadKey)
        XCTAssertEqual(VideoQuality.stored(VideoQuality.downloadKey, defaults), .original)
    }

    func testEpisodesOfOlderAndNewerMacsDecode() throws {
        let old = #"{"id":1,"mediaId":2,"number":3,"title":"T","episodeTitle":"","watched":false,"position":0,"duration":1440,"prepared":false,"preparing":false}"#
        XCTAssertNil(try JSONDecoder().decode(EpisodeItem.self, from: Data(old.utf8)).qualities)
        let new = #"{"id":1,"mediaId":2,"number":3,"title":"T","episodeTitle":"","watched":false,"position":0,"duration":1440,"prepared":false,"preparing":false,"qualities":[{"id":"smaller","prepared":true,"sizeBytes":700}]}"#
        let item = try JSONDecoder().decode(EpisodeItem.self, from: Data(new.utf8))
        XCTAssertEqual(item.qualities?.first?.sizeBytes, 700)
        let state = #"{"id":1,"title":"T","number":3,"state":"ready","progress":1,"message":"","quality":"smaller","qualities":[]}"#
        XCTAssertEqual(try JSONDecoder().decode(EpisodeState.self, from: Data(state.utf8)).quality, "smaller")
    }
}
