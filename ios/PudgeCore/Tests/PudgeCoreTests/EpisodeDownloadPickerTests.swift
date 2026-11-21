import Foundation
import XCTest
@testable import PudgeCore

/// The anime title screen's download button and its episode sheet (`swift test` in ios/PudgeCore on the Mac).
final class EpisodeDownloadPickerTests: XCTestCase {
    private func row(_ id: Int, _ number: Int, watched: Bool = false, _ state: EpisodePickRow.State = .available) -> EpisodePickRow {
        EpisodePickRow(id: id, number: number, watched: watched, state: state)
    }

    func testOneEpisodeDownloadsAtOnceAndSeveralOpenTheSheet() {
        XCTAssertEqual(EpisodeDownloadPicker.action([row(7, 1)]), .download(7))
        XCTAssertEqual(EpisodeDownloadPicker.action([row(7, 1, .onDevice)]), .unavailable)
        XCTAssertEqual(EpisodeDownloadPicker.action([row(7, 1, .downloading)]), .unavailable)
        XCTAssertEqual(EpisodeDownloadPicker.action([]), .unavailable)
        XCTAssertEqual(EpisodeDownloadPicker.action([row(1, 1, .onDevice), row(2, 2)]), .choose)
        XCTAssertEqual(EpisodeDownloadPicker.action([row(1, 1, .onDevice), row(2, 2, .downloading)]), .unavailable)
    }

    func testEpisodesHereOrOnTheirWayStayFixed() {
        let rows = [row(1, 1, .onDevice), row(2, 2, .downloading), row(3, 3)]
        XCTAssertTrue(rows[0].fixed && rows[1].fixed && !rows[2].fixed)
        XCTAssertEqual(EpisodeDownloadPicker.toggled([], id: 1, in: rows), [])
        XCTAssertEqual(EpisodeDownloadPicker.toggled([], id: 2, in: rows), [])
        XCTAssertEqual(EpisodeDownloadPicker.toggled([], id: 3, in: rows), [3])
        XCTAssertEqual(EpisodeDownloadPicker.toggled([3], id: 3, in: rows), [])
        XCTAssertEqual(EpisodeDownloadPicker.toggled([3], id: 99, in: rows), [3])
        // Only episodes not here are sent, in number order.
        XCTAssertEqual(EpisodeDownloadPicker.chosen([1, 2, 3], in: rows), [3])
        let more = [row(30, 3), row(10, 1), row(20, 2)]
        XCTAssertEqual(EpisodeDownloadPicker.chosen([30, 10, 20], in: more), [10, 20, 30])
    }

    func testAllNotWatchedSkipsWatchedAndThoseHere() {
        let rows = [row(1, 1, watched: true), row(2, 2, .onDevice), row(3, 3), row(4, 4), row(5, 5, watched: true, .available)]
        XCTAssertEqual(EpisodeDownloadPicker.notWatched(rows), [3, 4])
    }

    func testNextNStartsAtTheNextToWatch() {
        let rows = [row(1, 1, watched: true), row(2, 2, watched: true), row(3, 3, .onDevice), row(4, 4), row(5, 5), row(6, 6), row(7, 7)]
        XCTAssertEqual(EpisodeDownloadPicker.next(2, in: rows, from: 3), [4, 5])
        XCTAssertEqual(EpisodeDownloadPicker.next(10, in: rows, from: 6), [6, 7])
        XCTAssertEqual(EpisodeDownloadPicker.next(0, in: rows, from: 3), [])
        // Without a next one: from the lowest not here.
        XCTAssertEqual(EpisodeDownloadPicker.next(2, in: rows, from: nil), [1, 2])
    }

    func testTheSheetOpensWithTheNextEpisodeTicked() {
        let rows = [row(1, 1, watched: true), row(2, 2), row(3, 3)]
        XCTAssertEqual(EpisodeDownloadPicker.initial(rows, nextToWatch: 2), [2])
        XCTAssertEqual(EpisodeDownloadPicker.initial([row(1, 1), row(2, 2, .onDevice)], nextToWatch: 2), [])
        XCTAssertEqual(EpisodeDownloadPicker.initial(rows, nextToWatch: nil), [])
    }
}
