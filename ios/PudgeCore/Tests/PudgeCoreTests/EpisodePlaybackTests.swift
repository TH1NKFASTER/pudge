import Foundation
import XCTest
@testable import PudgeCore

/// One copy per episode, per-episode subtitle timing, the OP/ED Skip button (`swift test` in ios/PudgeCore on the Mac).
final class EpisodePlaybackTests: XCTestCase {
    private func item(_ id: Int, number: Int, mediaId: Int = 99426, extra: String = "") throws -> EpisodeItem {
        let json = """
        {"id": \(id), "mediaId": \(mediaId), "number": \(number), "title": "Sora", "episodeTitle": "", "watched": false,
         "position": 0, "duration": 1400, "prepared": false, "preparing": false\(extra)}
        """
        return try JSONDecoder().decode(EpisodeItem.self, from: Data(json.utf8))
    }

    func testOneItemPerEpisodeNumberKeepsTheDownloadedCopy() throws {
        let single = try item(194491, number: 2), batch = try item(194660, number: 2)
        let one = try item(194659, number: 1), three = try item(194661, number: 3)
        // The Mac's order and nothing downloaded: the first copy stays.
        XCTAssertEqual(EpisodeCopies.unique([one, single, batch, three]).map(\.id), [194659, 194491, 194661])
        // The other copy is on this device: that one is shown (in its place).
        let rank: (EpisodeItem) -> Int = { $0.id == 194660 ? 2 : 0 }
        XCTAssertEqual(EpisodeCopies.unique([one, single, batch, three], rank: rank).map(\.id), [194659, 194660, 194661])
        XCTAssertEqual(EpisodeCopies.unique([one, three]).map(\.id), [194659, 194661])
    }

    func testASecondDownloadOfTheSameEpisodeIsFound() throws {
        let batch = try item(194660, number: 2)
        XCTAssertEqual(EpisodeCopies.otherCopy(of: batch, among: [(id: 194491, mediaId: 99426, number: 2)]), 194491)
        XCTAssertNil(EpisodeCopies.otherCopy(of: batch, among: [(id: 194660, mediaId: 99426, number: 2)]))  // itself
        XCTAssertNil(EpisodeCopies.otherCopy(of: batch, among: [(id: 1, mediaId: 99426, number: 3), (id: 2, mediaId: 5, number: 2)]))
        let untitled = try item(7, number: 2, mediaId: 0)
        XCTAssertNil(EpisodeCopies.otherCopy(of: untitled, among: [(id: 8, mediaId: 0, number: 2)]))
    }

    func testNewKeysDecodeAndOlderMacsLeaveThemOut() throws {
        let old = try item(1, number: 1)
        XCTAssertNil(old.subtitleDelayMs); XCTAssertNil(old.skipSegments)
        let new = try item(1, number: 1, extra: #", "subtitleDelayMs": -700, "skipSegments": [{"kind": "intro", "start": 60, "end": 149.5}]"#)
        XCTAssertEqual(new.subtitleDelayMs, -700)
        XCTAssertEqual(new.skipSegments, [SkipSegment(kind: "intro", start: 60, end: 149.5)])
    }

    func testTimingIsKeptPerEpisodeAndTheMacValueOnlyWhenNothingWaits() {
        var delays = SubtitleDelays()
        XCTAssertEqual(delays.ms(1), 0)
        XCTAssertFalse(delays.set(0, for: 1))
        XCTAssertTrue(delays.set(1300, for: 1))
        XCTAssertFalse(delays.set(1300, for: 1))
        XCTAssertEqual(delays.ms(1), 1300); XCTAssertEqual(delays.ms(2), 0)
        // Not sent yet: the Mac's older value does not take it back (the old snap back to 0).
        XCTAssertFalse(delays.adopt(mac: 0, for: 1))
        XCTAssertEqual(delays.ms(1), 1300)
        XCTAssertEqual(delays.unsent.map(\.id), [1]); XCTAssertEqual(delays.unsent.map(\.ms), [1300])
        delays.sent(1300, for: 1)
        XCTAssertTrue(delays.unsent.isEmpty)
        // Sent: a delay set in mpv later comes in; an older Mac (nil) changes nothing.
        XCTAssertTrue(delays.adopt(mac: -500, for: 1))
        XCTAssertEqual(delays.ms(1), -500)
        XCTAssertFalse(delays.adopt(mac: nil, for: 1))
        XCTAssertEqual(delays.ms(1), -500)
        // Reset to 0 here is sent too, then forgotten.
        XCTAssertTrue(delays.set(0, for: 1))
        XCTAssertEqual(delays.unsent.map(\.ms), [0])
        delays.sent(0, for: 1)
        XCTAssertTrue(delays.entries.isEmpty)
        // A change made while an older value was being sent stays unsent.
        delays.set(400, for: 3); delays.set(600, for: 3); delays.sent(400, for: 3)
        XCTAssertEqual(delays.unsent.map(\.ms), [600])
        // Kept on the device (offline).
        let saved = try! JSONDecoder().decode(SubtitleDelays.self, from: JSONEncoder().encode(delays))
        XCTAssertEqual(saved, delays)
    }

    func testSkipButtonLikeMpv() {
        let segments = [SkipSegment(kind: "intro", start: 60, end: 149.5), SkipSegment(kind: "outro", start: 1300, end: 1390)]
        XCTAssertNil(SkipSegments.button(segments, atMs: 59_999, durationMs: 1_400_000))
        XCTAssertEqual(SkipSegments.button(segments, atMs: 60_000, durationMs: 1_400_000)?.isOpening, true)
        XCTAssertNotNil(SkipSegments.button(segments, atMs: 64_900, durationMs: 1_400_000))
        XCTAssertNil(SkipSegments.button(segments, atMs: 65_000, durationMs: 1_400_000))  // first 5 s only
        XCTAssertNotNil(SkipSegments.button(segments, atMs: 100_000, durationMs: 1_400_000, controlsShown: true))
        XCTAssertNil(SkipSegments.button(segments, atMs: 149_500, durationMs: 1_400_000, controlsShown: true))
        XCTAssertEqual(SkipSegments.button(segments, atMs: 1_301_000, durationMs: 1_400_000)?.kind, "outro")
        XCTAssertEqual(SkipSegments.targetMs(segments[0]), 149_500)
        // Past the end of the file, unknown kinds and empty ranges are ignored (as in mpv).
        XCTAssertNil(SkipSegments.button(segments, atMs: 1_301_000, durationMs: 1_350_000))
        XCTAssertNil(SkipSegments.button([SkipSegment(kind: "recap", start: 0, end: 30)], atMs: 1000, durationMs: 0))
        XCTAssertNil(SkipSegments.button([SkipSegment(kind: "intro", start: 30, end: 30)], atMs: 30_000, durationMs: 0))
        XCTAssertNotNil(SkipSegments.button(segments, atMs: 61_000, durationMs: 0))  // length unknown yet
    }
}
