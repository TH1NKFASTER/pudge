import Foundation
import XCTest
@testable import PudgeCore

/// IOS6: after the final episode on the phone, the Mac's score wheel and next entry
/// (`swift test` in ios/PudgeCore on the Mac). Invented titles only.
final class AnimeFinaleTests: XCTestCase {
    func testTheMacsAnswerIsReadAndABrokenFinaleNeverFailsTheWatch() throws {
        let json = #"{"ok":true,"outcome":"counted","message":"","finale":{"mediaId":901,"title":"Invented","kind":"final","episode":12,"userScore":null,"prompted":false}}"#
        let answer = try JSONDecoder().decode(EpisodeWatchedAnswer.self, from: Data(json.utf8))
        XCTAssertEqual(answer.finale, AnimeFinale(mediaId: 901, title: "Invented", kind: "final", episode: 12))
        XCTAssertEqual(answer.finale?.id, "901:12")
        let older = try JSONDecoder().decode(EpisodeWatchedAnswer.self, from: Data(#"{"ok":true,"outcome":"counted","message":""}"#.utf8))
        XCTAssertNil(older.finale)
        let broken = try JSONDecoder().decode(EpisodeWatchedAnswer.self, from: Data(#"{"ok":false,"outcome":"not_enough","message":"m","finale":{"title":1}}"#.utf8))
        XCTAssertEqual(broken.ok, false)
        XCTAssertNil(broken.finale)
    }

    func testWithoutTheMacOnlyTheFinalEpisodeByTheListsCountAsks() {
        XCTAssertEqual(AnimeFinaleRule.local(mediaId: 9, title: "T", number: 12, total: 12, movie: false)?.episode, 12)
        XCTAssertNil(AnimeFinaleRule.local(mediaId: 9, title: "T", number: 11, total: 12, movie: false))
        XCTAssertNil(AnimeFinaleRule.local(mediaId: 9, title: "T", number: 5, total: nil, movie: nil))
        XCTAssertEqual(AnimeFinaleRule.local(mediaId: 9, title: "T", number: 1, total: nil, movie: true)?.episode, 1)
        XCTAssertNil(AnimeFinaleRule.local(mediaId: 0, title: "T", number: 12, total: 12, movie: nil))
    }

    func testTheWheelMatchesTheMacsWeightsAndNeverRepeatsAScore() {
        let segments = ScoreWheel.segments
        XCTAssertEqual(segments.map(\.score), Array(1...10))
        XCTAssertEqual(segments.first?.start, 0)
        XCTAssertEqual(segments.last?.end ?? 0, 360, accuracy: 1e-9)
        XCTAssertEqual(ScoreWheel.score(0), 1)
        XCTAssertEqual(ScoreWheel.score(0.5), 6)
        XCTAssertEqual(ScoreWheel.score(0.9999), 10)
        var draws = [0.5, 0.5, 0.45, 0.3].makeIterator()
        let spin = ScoreWheel.spin(previous: 6) { draws.next() ?? 0 }
        XCTAssertEqual(spin.score, 5)
        let segment = segments[4]
        XCTAssertTrue(spin.target >= segment.start && spin.target < segment.end)
        // The rotation stops with the target under the pointer.
        let rotation = ScoreWheel.rotation(from: 123, turns: 5, target: spin.target)
        XCTAssertGreaterThan(rotation, 123)
        XCTAssertEqual(ScoreWheel.scoreAt(rotation: rotation), 5)
    }

    func testAnswersWaitForTheMacAndAreNotOfferedAgain() {
        var queue = FinaleQueue()
        let finale = AnimeFinale(mediaId: 901, title: "T", episode: 12)
        XCTAssertTrue(queue.offers(finale))
        XCTAssertFalse(queue.offers(AnimeFinale(mediaId: 901, title: "T", episode: 12, prompted: true)))
        queue.answer(finale, score: 7, at: 1)
        XCTAssertFalse(queue.offers(finale))
        // Another answer for the title before the Mac answered: the newer one is sent.
        queue.answer(finale, score: 12, at: 2)
        XCTAssertEqual(queue.pending, [FinaleAnswer(mediaId: 901, episode: 12, score: 10, at: 2)])
        queue.sent(FinaleAnswer(mediaId: 901, episode: 12, score: 7, at: 1))
        XCTAssertEqual(queue.pending.count, 1)
        queue.sent(queue.pending[0])
        XCTAssertTrue(queue.pending.isEmpty)
        // Skip is an answer too (sent as skip).
        queue.answer(AnimeFinale(mediaId: 903, title: "A", episode: 1), score: nil, at: 3)
        let request = FinaleScoreRequest(queue.pending[0])
        XCTAssertEqual(request.skip, true)
        XCTAssertNil(request.score)
        // Kept across launches.
        let copy = try? JSONDecoder().decode(FinaleQueue.self, from: JSONEncoder().encode(queue))
        XCTAssertEqual(copy, queue)
    }

    func testTheNextEntryAndItsActionsInTheMacsWords() throws {
        let json = #"{"sequel":{"mediaId":902,"title":"Invented 2","siteUrl":"https://anilist.co/anime/902","listStatus":"PLANNING","mediaStatus":"FINISHED","released":true,"canPlanning":false,"canWatching":true,"canDownload":true,"episodes":null}}"#
        let sequel = try XCTUnwrap(try JSONDecoder().decode(FinaleSequelAnswer.self, from: Data(json.utf8)).sequel)
        XCTAssertEqual(sequel.statusText, "In Planning")
        XCTAssertEqual(sequel.canWatching, true)
        let refused = try JSONDecoder().decode(FinaleActionAnswer.self, from: Data(#"{"ok":false,"reason":"no_release"}"#.utf8))
        XCTAssertTrue(refused.message("download").contains("No suitable torrent"))
        let planned = try JSONDecoder().decode(FinaleActionAnswer.self, from: Data(#"{"ok":true,"reason":"","anilistPending":true}"#.utf8))
        XCTAssertEqual(planned.message("planning"), "Added to Planning (AniList is updated later).")
    }

    func testNextEntriesFetchedAheadServeTheSheetForAWhile() {
        var cache = FinaleSequelCache(ttl: 600)
        XCTAssertEqual(cache.lookup(901, now: 100), .miss)
        let next = FinaleSequel(mediaId: 902, title: "Invented Second", listStatus: "", released: true, canPlanning: true)
        cache.store(901, sequel: next, at: 100)
        XCTAssertEqual(cache.lookup(901, now: 400), .hit(next))
        XCTAssertTrue(cache.fresh(901, now: 699))
        XCTAssertEqual(cache.lookup(901, now: 700), .miss)
        // "No next entry" is an answer too.
        cache.store(903, sequel: nil, at: 100)
        XCTAssertEqual(cache.lookup(903, now: 200), .hit(nil))
        cache.forget(903)
        XCTAssertEqual(cache.lookup(903, now: 200), .miss)
        cache.store(0, sequel: next, at: 100)
        XCTAssertEqual(cache.lookup(0, now: 100), .miss)
        for id in 1000..<(1000 + FinaleSequelCache.keep + 5) { cache.store(id, sequel: nil, at: 200 + Double(id - 1000)) }
        XCTAssertEqual(cache.lookup(1000, now: 300), .miss)
        XCTAssertEqual(cache.lookup(1000 + FinaleSequelCache.keep + 4, now: 300), .hit(nil))
    }

    /// REVIEW r2 ios-audio #4: a score is dropped only when the Mac refuses it for good.
    func testAScoreIsDroppedOnlyWhenRefusedForGood() {
        XCTAssertTrue(FinaleRefusal.final(422))
        XCTAssertTrue(FinaleRefusal.final(404))
        XCTAssertFalse(FinaleRefusal.final(401))
        XCTAssertFalse(FinaleRefusal.final(409))
        XCTAssertFalse(FinaleRefusal.final(429))
    }
}
