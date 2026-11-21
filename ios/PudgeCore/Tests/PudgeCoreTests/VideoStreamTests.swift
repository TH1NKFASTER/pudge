import Foundation
import XCTest
@testable import PudgeCore

/// Watching an episode from the Mac: what the resource loader asks for and accepts
/// (`swift test` in ios/PudgeCore on the Mac).
final class VideoStreamTests: XCTestCase {
    func testTheAddressNeedsTheLoaderAndTheRouteNamesTheStream() {
        let url = VideoStreamAddress.assetURL(resourceId: "6F1C2B9E-0D7A-4C1E-9E55-2C7B1A0F9D31")
        XCTAssertEqual(url.scheme, "pudge-stream")
        XCTAssertEqual(url.pathExtension, "mp4")
        XCTAssertEqual(VideoStreamAddress.assetURL(resourceId: "../x?y").absoluteString, "pudge-stream://mac/xy.mp4")
        XCTAssertEqual(VideoStreamAddress.route(resourceId: "r1", sha256: "ab", stream: "0123"),
                       "/api/v2/resources/r1/content?sha256=ab&stream=0123")
        // A Mac without streams: still marked as played, not downloaded.
        XCTAssertEqual(VideoStreamAddress.route(resourceId: "r1", sha256: "ab", stream: nil),
                       "/api/v2/resources/r1/content?sha256=ab&stream=play")
    }

    func testRequestedBytesStayInsideTheFile() {
        let size: Int64 = 1000
        // AVPlayer's first request: two bytes with the content information.
        XCTAssertEqual(VideoStreamRanges.requested(offset: 0, length: 2, toEnd: false, size: size), 0...1)
        XCTAssertEqual(VideoStreamRanges.requested(offset: 0, length: 1000, toEnd: false, size: size), 0...999)
        XCTAssertEqual(VideoStreamRanges.requested(offset: 990, length: 100, toEnd: false, size: size), 990...999)
        // To the end, with a length up to Int.max (no overflow).
        XCTAssertEqual(VideoStreamRanges.requested(offset: 400, length: Int.max, toEnd: true, size: size), 400...999)
        XCTAssertEqual(VideoStreamRanges.requested(offset: 400, length: Int.max, toEnd: false, size: size), 400...999)
        XCTAssertEqual(VideoStreamRanges.requested(offset: 999, length: 1, toEnd: false, size: size), 999...999)
        XCTAssertNil(VideoStreamRanges.requested(offset: 1000, length: 1, toEnd: false, size: size))
        XCTAssertNil(VideoStreamRanges.requested(offset: 0, length: 0, toEnd: false, size: size))
        XCTAssertNil(VideoStreamRanges.requested(offset: -1, length: 5, toEnd: false, size: size))
        XCTAssertNil(VideoStreamRanges.requested(offset: 0, length: 5, toEnd: true, size: 0))
    }

    func testChunksStartSmallThenCoverTheRangeExactly() {
        XCTAssertEqual(VideoStreamRanges.plan(0...1, first: 4, chunk: 10), [0...1])
        XCTAssertEqual(VideoStreamRanges.plan(0...3, first: 4, chunk: 10), [0...3])
        XCTAssertEqual(VideoStreamRanges.plan(0...4, first: 4, chunk: 10), [0...3, 4...4])
        XCTAssertEqual(VideoStreamRanges.plan(5...30, first: 4, chunk: 10), [5...8, 9...18, 19...28, 29...30])
        XCTAssertEqual(VideoStreamRanges.plan(7...7, first: 4, chunk: 10), [7...7])
        // A first chunk larger than the others is cut to their size; nonsense sizes still move on.
        XCTAssertEqual(VideoStreamRanges.plan(0...25, first: 50, chunk: 10), [0...9, 10...19, 20...25])
        XCTAssertEqual(VideoStreamRanges.plan(0...2, first: 0, chunk: 0), [0...0, 1...1, 2...2])
        // A whole episode: contiguous, in order, nothing twice, the defaults' sizes.
        let size: Int64 = 734_003_207
        let chunks = VideoStreamRanges.plan(0...(size - 1))
        XCTAssertEqual(chunks.first, 0...(VideoStreamRanges.firstChunk - 1))
        XCTAssertEqual(chunks.last?.upperBound, size - 1)
        for (left, right) in zip(chunks, chunks.dropFirst()) {
            XCTAssertEqual(left.upperBound + 1, right.lowerBound)
            XCTAssertLessThanOrEqual(Int64(right.count), VideoStreamRanges.chunk)
        }
        XCTAssertEqual(chunks.reduce(Int64(0)) { $0 + Int64($1.count) }, size)
        // One loading request gets its first chunks only (AVPlayer asks again for the rest).
        let served = VideoStreamRanges.served(1000...(size - 1))
        XCTAssertEqual(served.count, VideoStreamRanges.chunksPerRequest)
        XCTAssertEqual(served, Array(VideoStreamRanges.plan(1000...(size - 1)).prefix(VideoStreamRanges.chunksPerRequest)))
        XCTAssertEqual(served.last?.upperBound, 1000 + VideoStreamRanges.firstChunk + Int64(VideoStreamRanges.chunksPerRequest - 1) * VideoStreamRanges.chunk - 1)
        XCTAssertLessThanOrEqual(served.reduce(Int64(0)) { $0 + Int64($1.count) }, 32 * 1024 * 1024)
        XCTAssertEqual(VideoStreamRanges.served(0...1), [0...1])
        XCTAssertEqual(VideoStreamRanges.plan(0...30, first: 4, chunk: 10, limit: 2), [0...3, 4...13])
        XCTAssertEqual(VideoStreamRanges.plan(0...30, first: 4, chunk: 10, limit: 0), [0...3])
        XCTAssertEqual(VideoStreamRanges.header(5...8), "bytes=5-8")
        XCTAssertEqual(VideoStreamRanges.header(0...0), "bytes=0-0")
    }

    func testContentRangeParsing() {
        XCTAssertEqual(ContentRange.parse("bytes 0-1/1234"), ContentRange(start: 0, end: 1, total: 1234))
        XCTAssertEqual(ContentRange.parse(" bytes 1233-1233/1234 "), ContentRange(start: 1233, end: 1233, total: 1234))
        XCTAssertEqual(ContentRange.parse("Bytes 5-9/*"), ContentRange(start: 5, end: 9, total: nil))
        for bad in [nil, "", "bytes */1234", "bytes 5-4/10", "bytes 0-10/10", "items 0-1/10", "bytes 0-1", "bytes a-b/10", "bytes 0-1/0",
                    "bytes +5-9/10", "bytes 5-+9/10", "bytes 5-9/+10", "bytes -1-9/10", "bytes 5 -9/10", "bytes 5-9/1_0"] {
            XCTAssertNil(ContentRange.parse(bad), bad ?? "nil")
        }
    }

    func testAnswersOfTheMacAreChecked() throws {
        let size: Int64 = 100
        XCTAssertNoThrow(try VideoStreamResponse.check(status: 206, contentRange: "bytes 10-19/100", received: 10, requested: 10...19, size: size))
        // The whole file may come as 200 (a Mac that answers so); a part may not.
        XCTAssertNoThrow(try VideoStreamResponse.check(status: 200, contentRange: nil, received: 100, requested: 0...99, size: size))
        XCTAssertThrowsError(try VideoStreamResponse.check(status: 200, contentRange: nil, received: 100, requested: 10...19, size: size)) {
            XCTAssertEqual($0 as? VideoStreamFailure, .refused(200))
        }
        // Other bytes or another file than asked for.
        XCTAssertThrowsError(try VideoStreamResponse.check(status: 206, contentRange: "bytes 0-9/100", received: 10, requested: 10...19, size: size)) {
            XCTAssertEqual($0 as? VideoStreamFailure, .changed)
        }
        XCTAssertThrowsError(try VideoStreamResponse.check(status: 206, contentRange: "bytes 10-19/120", received: 10, requested: 10...19, size: size)) {
            XCTAssertEqual($0 as? VideoStreamFailure, .changed)
        }
        XCTAssertThrowsError(try VideoStreamResponse.check(status: 206, contentRange: nil, received: 10, requested: 10...19, size: size))
        // Cut short: asked again.
        XCTAssertThrowsError(try VideoStreamResponse.check(status: 206, contentRange: "bytes 10-19/100", received: 4, requested: 10...19, size: size)) {
            XCTAssertEqual($0 as? VideoStreamFailure, .interrupted)
        }
        let statuses: [(Int, VideoStreamFailure)] = [(401, .revoked), (409, .changed), (416, .changed), (503, .interrupted), (400, .refused(400))]
        for (status, failure) in statuses {
            XCTAssertThrowsError(try VideoStreamResponse.check(status: status, contentRange: nil, received: 0, requested: 0...1, size: size)) {
                XCTAssertEqual($0 as? VideoStreamFailure, failure, "\(status)")
            }
        }
    }

    func testFailuresAreRetriedOnlyWhenTheMacMayAnswerAgain() {
        XCTAssertEqual(VideoStreamFailure.from(error: URLError(.timedOut), cancelled: false), .unreachable)
        XCTAssertEqual(VideoStreamFailure.from(error: URLError(.networkConnectionLost), cancelled: false), .unreachable)
        XCTAssertEqual(VideoStreamFailure.from(error: URLError(.cannotConnectToHost), cancelled: false), .unreachable)
        // A seek or the closed player cancel the task: nothing to report.
        XCTAssertNil(VideoStreamFailure.from(error: URLError(.cancelled), cancelled: true))
        XCTAssertNil(VideoStreamFailure.from(error: CancellationError(), cancelled: true))
        XCTAssertNil(VideoStreamFailure.from(error: URLError(.timedOut), cancelled: true))
        // Cancelled without the task being cancelled: the pinned certificate check refused the
        // Mac at that address; the request fails instead of leaving the player waiting.
        XCTAssertEqual(VideoStreamFailure.from(error: URLError(.cancelled), cancelled: false), .revoked)
        XCTAssertFalse(VideoStreamFailure.revoked.retryable)
        XCTAssertEqual(VideoStreamFailure.from(error: VideoStreamFailure.changed, cancelled: false), .changed)
        XCTAssertTrue(VideoStreamRetry.shouldRetry(.unreachable, attempt: 1))
        XCTAssertTrue(VideoStreamRetry.shouldRetry(.interrupted, attempt: VideoStreamRetry.attempts - 1))
        XCTAssertFalse(VideoStreamRetry.shouldRetry(.unreachable, attempt: VideoStreamRetry.attempts))
        XCTAssertFalse(VideoStreamRetry.shouldRetry(.revoked, attempt: 1))
        XCTAssertFalse(VideoStreamRetry.shouldRetry(.changed, attempt: 1))
        XCTAssertGreaterThan(VideoStreamRetry.delay(after: 1), 0)
        XCTAssertLessThan(VideoStreamRetry.delay(after: 1), 1)  // a closed kept-alive connection: at once
        XCTAssertLessThanOrEqual(VideoStreamRetry.delay(after: 10), 4)
        XCTAssertFalse(VideoStreamRetry.showsWaiting(after: 1))
        XCTAssertTrue(VideoStreamRetry.showsWaiting(after: 2))
        // Pudge closed on the Mac (refused at once): the player says so within seconds.
        let refusedAtOnce = (1..<VideoStreamRetry.attempts).reduce(0.0) { $0 + VideoStreamRetry.delay(after: $1) }
        XCTAssertLessThan(refusedAtOnce, 10)
        // The Mac asleep (every try times out): within a minute.
        XCTAssertLessThan(refusedAtOnce + Double(VideoStreamRetry.attempts) * VideoStreamRetry.timeout, 60)
    }

    func testTheMacsAnswerToOpeningAStream() throws {
        let ready = Data("""
        {"id": 12, "title": "Dorohedoro", "number": 3, "state": "ready", "progress": 1.0, "message": "",
         "video": {"resourceId": "r1", "sha256": "ab", "sizeBytes": 1234, "fileName": "episode.mp4"},
         "durationMs": 1440000, "stream": {"id": "0123456789abcdef0123456789abcdef", "leaseSeconds": 90}}
        """.utf8)
        let opened = try JSONDecoder().decode(VideoStreamOpened.self, from: ready)
        XCTAssertEqual(opened.video?.sizeBytes, 1234)
        XCTAssertEqual(opened.stream?.id, "0123456789abcdef0123456789abcdef")
        XCTAssertEqual(opened.stream?.renewEvery, 30)
        let preparing = Data(#"{"id": 12, "title": "", "number": 3, "state": "preparing", "progress": 0.4, "message": ""}"#.utf8)
        let waiting = try JSONDecoder().decode(VideoStreamOpened.self, from: preparing)
        XCTAssertNil(waiting.stream)
        XCTAssertNil(waiting.video)
        XCTAssertEqual(VideoStreamLease(id: "x", leaseSeconds: nil).renewEvery, 30)
        XCTAssertEqual(VideoStreamLease(id: "x", leaseSeconds: 6).renewEvery, 10)
    }
}
