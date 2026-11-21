import XCTest
@testable import PudgeCore

/// Library grids of cover cards (`swift test` in ios/PudgeCore on the Mac).
final class LibraryCardsTests: XCTestCase {
    func testPhonePortraitHasTwoOrThreeColumns() {
        XCTAssertEqual(LibraryGrid.columns(width: 320), 2)  // iPhone SE (1st) / narrow Split View
        XCTAssertEqual(LibraryGrid.columns(width: 375), 2)  // iPhone SE, mini
        XCTAssertEqual(LibraryGrid.columns(width: 393), 3)  // iPhone 15/16
        XCTAssertEqual(LibraryGrid.columns(width: 440), 3)  // Pro Max
        XCTAssertEqual(LibraryGrid.columns(width: 0), 2)
    }

    func testLandscapeAndIPadShowMoreColumnsOfAbout150To180Points() {
        XCTAssertGreaterThan(LibraryGrid.columns(width: 750), LibraryGrid.columns(width: 393))  // phone landscape
        for width in [667.0, 750, 768, 820, 1024, 1180, 1366] {
            let card = LibraryGrid.cardWidth(width: width)
            XCTAssertGreaterThanOrEqual(card, LibraryGrid.minimumWide, "width \(width)")
            XCTAssertLessThanOrEqual(card, 185, "width \(width)")
        }
        XCTAssertEqual(LibraryGrid.columns(width: 1024), 6)
        XCTAssertEqual(LibraryGrid.columns(width: 1366), 8)
        XCTAssertGreaterThanOrEqual(LibraryGrid.columns(width: 600), 3)
    }

    func testCardsFillTheRow() {
        for width in stride(from: 300.0, through: 1400, by: 37) {
            let count = Double(LibraryGrid.columns(width: width))
            let used = count * LibraryGrid.cardWidth(width: width) + (count - 1) * LibraryGrid.spacing + 2 * LibraryGrid.padding
            XCTAssertLessThanOrEqual(used, width + 0.001, "width \(width)")
            XCTAssertGreaterThan(used, width - count - 1, "width \(width)")
        }
    }

    // The phone's cards say what the Mac's cards say for the same item (pudge/web, locales.en.js):
    // Continue watching   continueHomeCard     "Episode 4 • resume at 10:00" / "Movie • resume at …"
    // New / Completed     downloadedEpisodeText "Episode 8", "Episodes 8-10", "All episodes ready", "… • Final"
    // Caught up           caughtUpHomeCard     "Episode 8 in 3d" / "Up-to-date"
    // Waiting on the Mac  episodePresentationStatus, needsActionHomeCard, downloadAvailableHomeCard
    // Light novels        lnBookCard chips, the series head ("5 volumes", "Continue · vol. 3"), franchise tile
    // Manga               mangaLibraryCard chips, the series head
    // Audiobooks          the summary line ("12:34 / 1:02:03 · 20% · 49:29 left"), the series head
    // No "Watched N of M" and no "Next: …" line (the Mac shows neither); phone downloads: "Downloading: 42%".
    private let now = 1_000_000.0

    func testAnimeStatus() {
        func status(_ next: Int?, position: Int64 = 0, download: CardDownload = .none, ready: [Int] = [], total: Int? = 12,
                    movie: Bool = false, unlock: (Int, Double)? = nil) -> CardStatus {
            CardStatusText.anime(nextNumber: next, positionMs: position, durationMs: 1_440_000, download: download, ready: ready,
                                 total: total, movie: movie, unlockEpisode: unlock?.0, unlockAt: unlock?.1, now: now)
        }
        // New episodes ready / Completed & ready (downloadedEpisodeText).
        XCTAssertEqual(status(8, ready: [8]), CardStatus("Episode 8", .accent))
        XCTAssertEqual(status(8, ready: [10, 8, 9, 12]).text, "Episodes 8-10,12")
        XCTAssertEqual(status(8, download: .downloaded, ready: [8, 9]), CardStatus("Episodes 8-9", .good))
        XCTAssertEqual(status(1, ready: Array(1...12)).text, "All episodes ready")
        XCTAssertEqual(status(12, ready: [12]).text, "Episode 12 • Final")
        XCTAssertEqual(status(1, ready: [1], total: 1).text, "All episodes ready • Final")
        XCTAssertEqual(status(3, ready: [3, 4], total: nil).text, "Episodes 3-4")
        XCTAssertEqual(CardStatusText.readyEpisodes([], total: 12), "Ready to watch")
        // Continue watching (continueHomeCard).
        let started = status(4, position: 600_000, ready: [4, 5])
        XCTAssertEqual(started.text, "Episode 4 • resume at 10:00")
        XCTAssertEqual(started.tone, .accent)
        XCTAssertEqual(started.progress ?? 0, 600_000.0 / 1_440_000, accuracy: 0.0001)
        XCTAssertEqual(status(1, position: 3_723_000, ready: [1], total: 1, movie: true).text, "Movie • resume at 1:02:03")
        XCTAssertEqual(status(4, position: 600_000, download: .downloaded, ready: [4]).tone, .good)
        // A few seconds in is not started yet.
        XCTAssertEqual(status(4, position: 3000, ready: [4]).text, "Episode 4")
        // The phone's own download, as compact as the Mac's progress line.
        XCTAssertEqual(status(5, download: .running(phase: "Downloading", fraction: 0.426), ready: [5]),
                       CardStatus("Downloading: 43%", .accent, progress: 0.426))
        XCTAssertEqual(status(5, download: .queued, ready: [5]), CardStatus("Waiting for download", .warn))
        // Caught up (caughtUpHomeCard).
        XCTAssertEqual(status(nil), CardStatus("Up-to-date", .muted))
        XCTAssertEqual(status(nil, unlock: (9, now + 3 * 86_400 + 50)), CardStatus("Episode 9 in 3d", .warn))
        // None of the invented lines.
        for text in [status(8, ready: [8, 9]).text, status(4, position: 600_000, ready: [4]).text, status(nil).text] {
            XCTAssertFalse(text.contains("Next") || text.contains("Watched") || text.contains("ready ·"), text)
        }
    }

    func testMacWordingHelpers() {
        XCTAssertEqual(MacCardText.remaining(3 * 86_400 + 7200), "3d")
        XCTAssertEqual(MacCardText.remaining(5 * 3600 + 12 * 60 + 30), "5h 12m")
        XCTAssertEqual(MacCardText.remaining(59), "0m")
        XCTAssertEqual(MacCardText.remaining(-40), "0m")
        XCTAssertEqual(MacCardText.clock(0), "0:00")
        XCTAssertEqual(MacCardText.clock(245.9), "4:05")
        XCTAssertEqual(MacCardText.clock(3723), "1:02:03")
        XCTAssertEqual(MacCardText.compactNumber(950), "950")
        XCTAssertEqual(MacCardText.compactNumber(1234), "1.2k")
        XCTAssertEqual(MacCardText.compactNumber(123_456), "123k")
        XCTAssertEqual(MacCardText.compactNumber(1_500_000), "1.5m")
        XCTAssertEqual(MacCardText.episodeRanges([5, 1, 2, 3, 3, 7, 8]), "1-3,5,7-8")
        XCTAssertEqual(MacCardText.compactDownload(kind: "downloading", fraction: 0.42), "42%")
        XCTAssertEqual(MacCardText.compactDownload(kind: "paused", fraction: 0.42), "Paused · 42%")
        XCTAssertEqual(MacCardText.compactDownload(kind: "stalled", fraction: 0.1), "No connections · 10%")
        XCTAssertEqual(MacCardText.compactDownload(kind: "downloading", fraction: 1), "Waiting for subs")
        XCTAssertEqual(MacCardText.compactDownload(kind: "finished", fraction: 0.5), "Waiting for subs")
    }

    func testAnimeSections() {
        XCTAssertEqual(AnimeSection.of(hasNext: true, nextPositionMs: 0), .newReady)
        XCTAssertEqual(AnimeSection.of(hasNext: true, nextPositionMs: 60_000), .continueWatching)
        XCTAssertEqual(AnimeSection.of(hasNext: false, nextPositionMs: 60_000), .caughtUp)
        XCTAssertEqual(AnimeSection.of(hasNext: true, nextPositionMs: 0, airing: true), .newReady)
        XCTAssertEqual(AnimeSection.of(hasNext: true, nextPositionMs: 0, airing: false), .completedReady)
        XCTAssertEqual(AnimeSection.of(hasNext: true, nextPositionMs: 60_000, airing: false), .continueWatching)
        XCTAssertEqual(AnimeSection.of(hasNext: false, nextPositionMs: 0, airing: true), .caughtUp)
        XCTAssertEqual(AnimeSection.allCases.map(\.title),
                       ["Continue watching", "New episodes ready", "Completed & ready", "Waiting on the Mac", "Caught up"])
    }

    private func group(_ id: Int, _ title: String) -> AnimeGroup { AnimeGroup(mediaId: id, title: title, episodes: []) }

    func testCaughtUpListsEveryMacTitleInTheMacOrder() {
        // 1: ready (New episodes ready); 2: listed with nothing left (a schedule's caught-up card);
        // 3, 4, 5: only on the Mac Home; 9: a group with nothing left the Mac does not list.
        let groups = [group(1, "Ready"), group(2, "Scheduled"), group(9, "Phone only")]
        let place: [Int: AnimeSection] = [1: .newReady, 2: .caughtUp, 9: .caughtUp]
        let mac = MacHomeShelves(
            caughtUp: [MacHomeCard(mediaId: 4, title: "Delta", section: "caught_up", nextEpisode: 8, nextAt: 100),
                       MacHomeCard(mediaId: 2, title: "Scheduled", section: "caught_up"),
                       MacHomeCard(mediaId: 1, title: "Ready", section: "caught_up"),
                       MacHomeCard(mediaId: 3, title: "Gamma", section: "caught_up")],
            waiting: [MacHomeCard(mediaId: 5, title: "Epsilon", section: "waiting", status: "waiting_subtitles", nextEpisode: 3),
                      MacHomeCard(mediaId: 4, title: "Delta", section: "download_available")])
        let shelves = AnimeShelves.arrange(groups, mac: mac) { place[$0.mediaId] ?? .caughtUp }
        XCTAssertEqual(shelves.map(\.section), [.newReady, .waitingOnMac, .caughtUp])
        XCTAssertEqual(shelves[0].entries.map(\.mediaId), [1])
        XCTAssertEqual(shelves[1].entries.map(\.mediaId), [5, 4])
        // Mac order, a title once (Delta is already waiting, Ready has an episode); the group the Mac
        // does not list (a finished title still downloaded here) gets no card.
        XCTAssertEqual(shelves[2].entries.map(\.mediaId), [2, 3])
        XCTAssertNotNil(shelves[2].entries[0].group)  // the group keeps its card (badges, actions)
        XCTAssertEqual(shelves[2].entries[0].mac?.section, "caught_up")
        XCTAssertNil(shelves[2].entries[1].group)  // Mac-only: a read-only card
        XCTAssertEqual(Set(shelves.flatMap(\.entries).map(\.id)).count, shelves.flatMap(\.entries).count)
    }

    func testOlderMacKeepsTheGroupsWithNothingLeftAsCaughtUp() {
        let groups = [group(1, "Ready"), group(2, "Done")]
        let shelves = AnimeShelves.arrange(groups, mac: nil) { $0.mediaId == 1 ? .completedReady : .caughtUp }
        XCTAssertEqual(shelves.map(\.section), [.completedReady, .caughtUp])
        XCTAssertEqual(shelves[1].entries.map(\.mediaId), [2])
        XCTAssertTrue(AnimeShelves.arrange([], mac: MacHomeShelves(caughtUp: [], waiting: [])) { _ in .caughtUp }.isEmpty)
    }

    func testMacHomeCardStatus() {
        func card(_ section: String, status: String? = nil, progress: Int = 0, next: Int? = nil, at: Double? = nil,
                  fraction: Double? = nil, kind: String? = nil, queued: Bool? = nil, action: String? = nil,
                  extras: [String]? = nil) -> CardStatus {
            CardStatusText.macHome(MacHomeCard(mediaId: 1, title: "A", progress: progress, section: section, status: status,
                                               nextEpisode: next, nextAt: at, downloadFraction: fraction, downloadKind: kind,
                                               queuedRelease: queued, actionCode: action, extraKinds: extras), now: now)
        }
        // Caught up.
        XCTAssertEqual(card("caught_up", next: 8, at: now + 5 * 3600 + 120), CardStatus("Episode 8 in 5h 2m", .warn))
        XCTAssertEqual(card("caught_up", next: 8), CardStatus("Up-to-date", .muted))
        XCTAssertEqual(card("caught_up"), CardStatus("Up-to-date", .muted))
        // Waiting for preparation (episodePresentationStatus).
        XCTAssertEqual(card("waiting", status: "downloading", next: 4, fraction: 0.42, kind: "downloading"),
                       CardStatus("42%", .accent, progress: 0.42))
        XCTAssertEqual(card("waiting", status: "downloading", next: 4, fraction: 0.42).text, "42%")
        XCTAssertEqual(card("waiting", status: "waiting_download", next: 4).text, "Waiting for download")
        XCTAssertEqual(card("waiting", status: "paused_download", fraction: 0.3, kind: "paused").text, "Paused · 30%")
        XCTAssertEqual(card("waiting", status: "paused_download").text, "Download paused")
        XCTAssertEqual(card("waiting", status: "waiting_subtitles", next: 4).text, "Waiting for subs")
        XCTAssertEqual(card("waiting", status: "waiting_preparation").text, "Waiting for subs")
        XCTAssertEqual(card("waiting", status: "waiting_text_subtitles").text, "Waiting for text subs")
        XCTAssertEqual(card("waiting", status: "waiting_verification").text, "Waiting for Japanese speech verification")
        XCTAssertEqual(card("waiting", status: "couldnt_sync").text, "Not aligned — raw subtitles shown, timing may be off")
        XCTAssertEqual(card("waiting", status: "queued_torrents_off").text, "Waiting for torrents to be turned on")
        XCTAssertEqual(card("waiting", status: "download_error"), CardStatus("Download error", .danger))
        XCTAssertEqual(card("waiting", status: "missing", next: 6).text, "Episode 6 is not ready")
        XCTAssertEqual(card("waiting", status: "").text, "Not ready yet")
        // Action required: what the Mac card's button or note says.
        XCTAssertEqual(card("needs_action", status: "needs_action", action: "configure_jimaku"), CardStatus("Configure Jimaku", .danger))
        XCTAssertEqual(card("needs_action", status: "needs_action", action: "grant_folder_access").text, "Grant folder access")
        XCTAssertEqual(card("needs_action", status: "needs_action", action: "enable_subtitle_ocr").text, "Enable OCR")
        XCTAssertEqual(card("needs_action", status: "needs_action").text, "Open settings")
        XCTAssertEqual(card("needs_action", action: "review_extra_file", extras: ["NCOP"]).text, "Extra NCOP came with the download")
        XCTAssertEqual(card("needs_action", action: "review_extra_file", extras: ["OP", "OP", "ED"]).text,
                       "Extras came with the download: 2 OP, ED")
        XCTAssertEqual(card("needs_action", action: "subtitle_retry_exhausted").text, "Subtitle preparation keeps failing the same way")
        // Available to download: its "Next: episode N" line (a started title) and the button / download.
        XCTAssertEqual(card("download_available", status: "missing", next: 5), CardStatus("Download anime", .muted))
        XCTAssertEqual(card("download_available", progress: 4, next: 5),
                       CardStatus("Download next episodes", .muted, detail: "Next: episode 5"))
        XCTAssertEqual(card("download_available", progress: 4, next: 5, fraction: 0.4, kind: "downloading"),
                       CardStatus("Downloading: 40%", .accent, progress: 0.4, detail: "Next: episode 5"))
        XCTAssertEqual(card("download_available", fraction: 0.4, kind: "paused").text, "Paused · 40%")
        XCTAssertEqual(card("download_available", queued: true).text, "Waiting for torrents to be turned on")
        // The invented phrases are gone.
        for text in [card("waiting", status: "downloading", next: 4, fraction: 0.42).text, card("waiting", status: "waiting_subtitles", next: 4).text,
                     card("download_available", next: 5).text, card("caught_up", next: 8, at: now + 60).text] {
            XCTAssertFalse(text.contains("on the Mac") || text.hasPrefix("Next:") || text.hasPrefix("Ep "), text)
        }
    }

    func testReadingStatus() {
        // A light novel alone: lnBookCard's chips, wrapping to two lines like the chips do.
        let alone = CardStatusText.novel(volume: 2, characters: 123_456, year: 2019, progress: 0.42, finished: false,
                                         compact: false, download: .none)
        XCTAssertEqual(alone, CardStatus("Volume 2 · 123k chars · 42% · 2019", .accent, progress: 0.42, lines: 2))
        XCTAssertEqual(CardStatusText.novel(volume: nil, characters: nil, year: nil, progress: nil, finished: false,
                                            compact: false, download: .none).text, "Volume 1 · 0%")
        XCTAssertEqual(CardStatusText.novel(volume: 1, characters: 9000, year: nil, progress: 0.999, finished: false,
                                            compact: false, download: .downloaded), CardStatus("Volume 1 · 9.0k chars · 99%", .good, progress: 0.99, lines: 2))
        // A volume inside a series: the compact chips.
        XCTAssertEqual(CardStatusText.novel(volume: 3, characters: 80_000, year: 2019, progress: 1, finished: true,
                                            compact: true, download: .none), CardStatus("100% · 80k chars · Finished", .muted, progress: 1, lines: 2))
        XCTAssertEqual(CardStatusText.novel(volume: 3, characters: nil, year: nil, progress: 0.1, finished: false,
                                            compact: true, download: .running(phase: "", fraction: 0.5)).text, "Downloading: 50%")

        XCTAssertEqual(CardStatusText.manga(volume: 4, pages: 180, readPages: 10, compact: false, download: .none),
                       CardStatus("Volume 4 · 180 pages · 6%", .accent, progress: 0.06, lines: 2))
        XCTAssertEqual(CardStatusText.manga(volume: 4, pages: 180, readPages: 180, compact: true, download: .downloaded),
                       CardStatus("100% · 180 pages", .muted, progress: 1, lines: 2))
        XCTAssertEqual(CardStatusText.manga(volume: nil, pages: 1, readPages: 0, compact: true, download: .downloaded).text, "0% · 1 page")
        XCTAssertEqual(CardStatusText.manga(volume: 1, pages: 0, readPages: 0, compact: false, download: .none).text, "Volume 1 · 0%")
        for status in [alone, CardStatusText.manga(volume: 4, pages: 180, readPages: 10, compact: false, download: .none)] {
            XCTAssertFalse(status.text.contains("Chapter") || status.text.contains("Page ") || status.text.contains("Not started"), status.text)
        }
    }

    func testShelfStatus() {
        // A series card's head on the Mac: "5 volumes" and its Continue button.
        XCTAssertEqual(CardStatusText.series(volumes: 7, continueVolume: 3),
                       CardStatus("Continue · vol. 3", .accent, detail: "7 volumes"))
        XCTAssertEqual(CardStatusText.series(volumes: 7, continueVolume: nil), CardStatus("7 volumes", .muted))
        XCTAssertEqual(CardStatusText.series(volumes: 1, continueVolume: nil).text, "1 volume")
        // A light-novel franchise tile.
        XCTAssertEqual(CardStatusText.franchise(series: 2, currentVolume: 3, currentProgress: 0.42, currentFinished: false, allFinished: false),
                       CardStatus("Franchise · 2 series", .accent, progress: 0.42, detail: "Volume 3 · 42%"))
        XCTAssertEqual(CardStatusText.franchise(series: 3, currentVolume: 9, currentProgress: 1, currentFinished: true, allFinished: true).detail,
                       "Finished")
        // An audiobook series head.
        XCTAssertEqual(CardStatusText.audiobookSeries(volumes: 5, unfinished: 2), CardStatus("2 unfinished", .accent, detail: "5 volumes"))
        XCTAssertEqual(CardStatusText.audiobookSeries(volumes: 5, unfinished: 0).text, "All finished")
    }

    func testAudiobookStatus() {
        XCTAssertEqual(CardStatusText.audiobook(positionMs: 754_000, durationMs: 3_723_000, chapter: "Chapter 3", files: 2,
                                                finished: false, ready: true, downloadFraction: nil),
                       CardStatus("12:34 / 1:02:03 · 20% · 49:29 left · Chapter 3 · 2 files", .good, progress: 754.0 / 3723, lines: 2))
        XCTAssertEqual(CardStatusText.audiobook(positionMs: 0, durationMs: 3_600_000, finished: false, ready: false, downloadFraction: nil),
                       CardStatus("0:00 / 1:00:00 · 0% · 1:00:00 left", .muted, lines: 2))
        // Finished: said in words with a full bar, wherever the place is (re-listening keeps it).
        XCTAssertEqual(CardStatusText.audiobook(positionMs: 3_600_000, durationMs: 3_600_000, finished: true, ready: true, downloadFraction: nil),
                       CardStatus("Finished · 1:00:00", .muted, progress: 1, lines: 2))
        XCTAssertEqual(CardStatusText.audiobook(positionMs: 60_000, durationMs: 3_600_000, files: 3, finished: true, ready: false, downloadFraction: nil).text,
                       "Finished · 1:00:00 · 3 files")
        XCTAssertEqual(CardStatusText.audiobook(positionMs: 0, durationMs: 3_600_000, finished: false, ready: false, downloadFraction: 0.333),
                       CardStatus("Downloading: 33%", .accent, progress: 0.333, lines: 2))
        XCTAssertEqual(CardStatus("x", .good, progress: 1.7).progress, 1)
    }

    func testAudiobookFinishedRule() {
        // The Mac's rule (pudge/audiobook_finish.py): the last 2% or the last 5 s.
        XCTAssertTrue(AudiobookFinish.reachedEnd(positionMs: 3_528_000, durationMs: 3_600_000))
        XCTAssertFalse(AudiobookFinish.reachedEnd(positionMs: 3_500_000, durationMs: 3_600_000))
        XCTAssertTrue(AudiobookFinish.reachedEnd(positionMs: 96_000, durationMs: 100_000))
        XCTAssertFalse(AudiobookFinish.reachedEnd(positionMs: 0, durationMs: 0))
        // The Mac's flag wins once it answered; this device's own unsent Finished wins over both.
        XCTAssertFalse(AudiobookFinish.isFinished(mac: false, positionMs: 3_600_000, durationMs: 3_600_000))
        XCTAssertTrue(AudiobookFinish.isFinished(mac: true, positionMs: 0, durationMs: 3_600_000))
        XCTAssertTrue(AudiobookFinish.isFinished(mac: nil, positionMs: 3_599_000, durationMs: 3_600_000))
        XCTAssertFalse(AudiobookFinish.isFinished(mac: nil, positionMs: 1_000, durationMs: 3_600_000))
        XCTAssertTrue(AudiobookFinish.isFinished(local: true, mac: false, positionMs: 0, durationMs: 3_600_000))
        XCTAssertFalse(AudiobookFinish.isFinished(local: false, mac: true, positionMs: 3_600_000, durationMs: 3_600_000))
        // Mark unfinished at the end starts over; elsewhere the place stays.
        XCTAssertEqual(AudiobookFinish.unfinishedPosition(positionMs: 3_600_000, durationMs: 3_600_000), 0)
        XCTAssertEqual(AudiobookFinish.unfinishedPosition(positionMs: 600_000, durationMs: 3_600_000), 600_000)
    }

    func testPlanningStatus() {
        XCTAssertEqual(CardStatusText.planning(progress: 2, episodes: 12, ready: 3), CardStatus("3 ready · 2 / 12 episodes", .good, progress: 2.0 / 12))
        XCTAssertEqual(CardStatusText.planning(progress: 2, episodes: nil, ready: 0), CardStatus("2 episodes watched", .accent))
        XCTAssertEqual(CardStatusText.planning(progress: 0, episodes: 0, ready: 0), CardStatus("0 / 0 episodes", .muted))
    }

    func testLiveRefreshIsFrequentOnlyWhileTheMacGetsSomethingReady() {
        XCTAssertEqual(AnimeLiveRefresh.interval(home: nil), AnimeLiveRefresh.idleSeconds)
        XCTAssertEqual(AnimeLiveRefresh.interval(home: MacHomeShelves(caughtUp: [], waiting: [])), AnimeLiveRefresh.idleSeconds)
        let busy = MacHomeShelves(caughtUp: [], waiting: [MacHomeCard(mediaId: 5, title: "E", section: "waiting", status: "downloading", nextEpisode: 3)])
        XCTAssertEqual(AnimeLiveRefresh.interval(home: busy), AnimeLiveRefresh.busySeconds)
    }
}
