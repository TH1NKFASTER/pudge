import XCTest
@testable import PudgeCore

final class CoreTests: XCTestCase {
    let asset = "00000000-0000-0000-0000-000000000001"
    let edition = "00000000-0000-0000-0000-000000000002"
    func manifest() throws -> AudioManifest {
        let json = """
        {"protocolMajor":2,"assetId":"\(asset)","editionId":"\(edition)","timelineRevision":"one","title":"Book","durationMs":30000,"parts":[{"partId":"00000000-0000-0000-0000-000000000003","resourceId":"00000000-0000-0000-0000-000000000004","sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","sizeBytes":1,"fileName":"one.mp3","offsetMs":0,"durationMs":10000},{"partId":"00000000-0000-0000-0000-000000000005","resourceId":"00000000-0000-0000-0000-000000000006","sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","sizeBytes":1,"fileName":"two.mp3","offsetMs":10000,"durationMs":20000}],"chapters":[]}
        """
        return try AudioManifest.decode(Data(json.utf8))
    }
    func testTimelineAtBoundaryAndEnd() throws {
        let book = try manifest()
        let timeline = AudioTimeline(book)
        XCTAssertEqual(try timeline.locate(10000).index, 1)
        XCTAssertEqual(try timeline.locate(10000).localMs, 0)
        XCTAssertEqual(try timeline.locate(30000).localMs, 20000)
        XCTAssertThrowsError(try timeline.locate(-1))
        XCTAssertThrowsError(try timeline.locate(30001))
    }
    func testPendingCheckpointsAndFinalPauseSurviveRestart() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let book = try manifest()
        let first = try AudioCheckpoint.make(book: book, deviceId: asset, sessionId: edition,
                                            sequence: 1, revision: 0, positionMs: 1000, reason: "periodic")
        let last = try AudioCheckpoint.make(book: book, deviceId: asset, sessionId: edition,
                                           sequence: 2, revision: 0, positionMs: 2000, reason: "pause")
        do {
            let store = try LocalStore.open(at: folder)
            try store.saveManifest(book)
            try store.enqueue(first)
            try store.enqueue(first)
            try store.enqueue(last)
        }
        let restored = try LocalStore.open(at: folder)
        XCTAssertEqual(try restored.pendingCheckpoints().map(\.reason), ["periodic", "pause"])
        XCTAssertEqual(try restored.localPosition(assetId: asset), 2000)
        try restored.acknowledge(first.checkpointId, revision: 1)
        XCTAssertEqual(try restored.pendingCheckpoints().map(\.reason), ["pause"])
        XCTAssertEqual(try restored.revision(assetId: asset), 1)
    }
    func testEvictionRequiresFreshReceiptAndUnchangedGeneration() throws {
        let copy = LocalResource(resourceId: asset, sha256: String(repeating: "a", count: 64),
                                 sizeBytes: 1, generation: 4, verified: true, pinned: false,
                                 active: false, original: true, transferring: false)
        XCTAssertFalse(StoragePolicy.canEvict(copy, receipt: nil, expectedGeneration: 4, nowMs: 100000))
        let receipt = ArchiveReceipt(receiptId: edition, resourceId: asset, editionId: edition,
                                     sha256: copy.sha256, sizeBytes: 1, verifiedAt: 99999, retained: true)
        XCTAssertTrue(StoragePolicy.canEvict(copy, receipt: receipt, expectedGeneration: 4, nowMs: 100000))
        XCTAssertFalse(StoragePolicy.canEvict(copy, receipt: receipt, expectedGeneration: 3, nowMs: 100000))
        XCTAssertFalse(StoragePolicy.canEvict(copy, receipt: receipt, expectedGeneration: 4, nowMs: 160001))
    }
    func testDiskReserveAndPeakSpace() {
        XCTAssertEqual(StoragePolicy.reserve(totalBytes: 100 * 1024 * 1024 * 1024), 5 * 1024 * 1024 * 1024)
        XCTAssertEqual(StoragePolicy.reserve(totalBytes: 16 * 1024 * 1024 * 1024), 2 * 1024 * 1024 * 1024)
        XCTAssertFalse(StoragePolicy.canDownload(totalBytes: 16 * 1024 * 1024 * 1024, freeBytes: 3 * 1024 * 1024 * 1024, queuedBytes: 0, newBytes: 1024 * 1024 * 1024))
    }
    func testOfflineBookmarkEditsUseConsecutiveRevisions() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        var mark = Bookmark(book: try manifest(), positionMs: 1000, title: "one")
        try store.enqueueBookmark(mark)
        mark.eventId = UUID().uuidString.lowercased(); mark.deleted = true
        try store.enqueueBookmark(mark)
        let queued = try store.pendingBookmarks()
        XCTAssertEqual(queued.map(\.baseRevision), [0, 1])
        try store.acknowledgeBookmark(queued[0].eventId)
        XCTAssertTrue(try store.hasPending(assetId: asset))
        XCTAssertEqual(try store.pendingBookmarks().count, 1)
        XCTAssertTrue(try store.bookmarks(assetId: asset).isEmpty)
    }
    func testEditionsRetainTheirOwnLocalPositions() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        let old = try manifest(); var newer = old
        newer.editionId = UUID().uuidString.lowercased(); newer.timelineRevision = "two"
        try store.saveManifest(old); try store.saveManifest(newer)
        for (book, position) in [(old, Int64(5000)), (newer, Int64(8000))] {
            try store.enqueue(AudioCheckpoint.make(book: book, deviceId: asset, sessionId: UUID().uuidString,
                                                    sequence: 1, revision: 0, positionMs: position, reason: "pause"))
        }
        XCTAssertEqual(try store.manifests().count, 2)
        XCTAssertEqual(try store.localPosition(assetId: asset, editionId: old.editionId), 5000)
        XCTAssertEqual(try store.localPosition(assetId: asset, editionId: newer.editionId), 8000)
    }
    func testEvictionDoesNotDeleteChangedBytes() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        let url = folder.appendingPathComponent("test.mp3")
        try Data("original".utf8).write(to: url)
        var part = try manifest().parts[0]; part.sha256 = try FileIntegrity.sha256(url); part.sizeBytes = 8
        try store.markVerified(part, at: url, original: true)
        let copy = try XCTUnwrap(store.resource(part.resourceId))
        let receipt = ArchiveReceipt(receiptId: edition, resourceId: part.resourceId, editionId: edition,
                                     sha256: part.sha256, sizeBytes: 8, verifiedAt: 1, retained: true)
        try Data("replaced".utf8).write(to: url)
        XCTAssertThrowsError(try store.evict(resourceId: part.resourceId, expectedGeneration: copy.state.generation, receipt: receipt, nowMs: 2))
        XCTAssertEqual(try Data(contentsOf: url), Data("replaced".utf8))
    }

    func testHandoffCheckpointSurvivesAcknowledgementAndRestart() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let book = try manifest()
        let operation = UUID().uuidString
        let checkpoint = try AudioCheckpoint.make(book: book, deviceId: asset, sessionId: edition,
            sequence: 1, revision: 0, positionMs: 4000, reason: "handoff")
        do {
            let store = try LocalStore.open(at: folder)
            try store.bindHandoff(operationId: operation, checkpoint: checkpoint)
            try store.acknowledge(checkpoint.checkpointId, revision: 1)
        }
        let restored = try LocalStore.open(at: folder)
        XCTAssertEqual(try restored.handoffCheckpoint(operationId: operation), checkpoint)
        XCTAssertTrue(try restored.pendingCheckpoints().isEmpty)
        var changed = checkpoint; changed.positionMs = 5000
        XCTAssertThrowsError(try restored.bindHandoff(operationId: operation, checkpoint: changed))
    }

    func testRemoteEditionAndMacIdentityPersistIndependentlyOfCredentials() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let book = try manifest()
        let store = try LocalStore.open(at: folder)
        try store.saveManifest(book)
        try store.bindMac(serverId: asset)
        XCTAssertFalse(try store.isRemote(book, serverId: asset))
        try store.markRemote(book, serverId: asset)
        let restored = try LocalStore.open(at: folder)
        XCTAssertTrue(try restored.isRemote(book, serverId: asset))
        XCTAssertThrowsError(try restored.bindMac(serverId: edition))
        var newer = book; newer.editionId = UUID().uuidString
        XCTAssertFalse(try restored.isRemote(newer, serverId: asset))
    }

    func testVerifyingSharedFilePreservesActiveAndOriginalProtection() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        let url = folder.appendingPathComponent("one.mp3")
        try Data("a".utf8).write(to: url)
        var part = try manifest().parts[0]; part.sha256 = try FileIntegrity.sha256(url)
        try store.markVerified(part, at: url, original: true)
        try store.setProtection(resourceId: part.resourceId, pinned: true, active: true)
        try store.markVerified(part, at: url)
        let value = try XCTUnwrap(store.resource(part.resourceId))
        XCTAssertTrue(value.state.active)
        XCTAssertTrue(value.state.original)
        XCTAssertTrue(value.state.pinned)
    }

    func testMissingOptionalCoverDoesNotBlockOfflineAudio() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        var book = try manifest()
        for index in book.parts.indices {
            let url = folder.appendingPathComponent("\(index).mp3")
            try Data("a".utf8).write(to: url)
            book.parts[index].sha256 = try FileIntegrity.sha256(url)
            try store.markVerified(book.parts[index], at: url)
        }
        book.cover = CoverResource(resourceId: UUID().uuidString, sha256: String(repeating: "a", count: 64), sizeBytes: 1, fileName: "cover.jpg")
        XCTAssertTrue(try store.ready(book))
    }

    func testStaleBookmarkSnapshotCannotUndoTombstone() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        var mark = Bookmark(book: try manifest(), positionMs: 1000, title: "One")
        mark.baseRevision = 1
        try store.mergeBookmarks([mark])
        var deleted = mark; deleted.baseRevision = 2; deleted.deleted = true
        try store.mergeBookmarks([deleted])
        try store.mergeBookmarks([mark])
        XCTAssertTrue(try store.bookmarks(assetId: asset).isEmpty)
    }

    func testSyncLabelRequiresTheCurrentCheckpointReceipt() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        let book = try manifest()
        let event = try AudioCheckpoint.make(book: book, deviceId: asset, sessionId: edition,
            sequence: 1, revision: 0, positionMs: 1000, reason: "pause")
        try store.enqueue(event)
        let remote = ProgressState(revision: 1, selected: event, conflicts: [], finished: false)
        XCTAssertEqual(SynchronizationStatus.evaluate(pending: try store.hasPending(assetId: asset), checkpointId: try store.latestCheckpointId(book), progress: remote), .pending)
        try store.acknowledge(event.checkpointId, revision: 1)
        XCTAssertEqual(SynchronizationStatus.evaluate(pending: false, checkpointId: try store.latestCheckpointId(book), progress: remote), .synced)
        let newer = try AudioCheckpoint.make(book: book, deviceId: asset, sessionId: edition,
            sequence: 2, revision: 1, positionMs: 2000, reason: "seek")
        try store.enqueue(newer); try store.acknowledge(newer.checkpointId, revision: 2)
        XCTAssertEqual(SynchronizationStatus.evaluate(pending: false, checkpointId: try store.latestCheckpointId(book), progress: remote), .awaiting)
    }

    func testBackgroundTransferStateAndResumeIdentitySurviveRestart() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        var record = TransferRecord(assetId: asset, part: try manifest().parts[0])
        record.status = "paused"; record.resumeData = Data("opaque-resume-data".utf8)
        record.resumeIdentity = "verified-endpoint-and-credential"; record.receivedBytes = 1
        try store.saveTransfer(record)
        let restored = try XCTUnwrap(LocalStore.open(at: folder).transfers().first)
        XCTAssertEqual(restored.resumeData, record.resumeData)
        XCTAssertTrue(restored.canResume(identity: "verified-endpoint-and-credential"))
        XCTAssertFalse(restored.canResume(identity: "changed-address-or-credential"))
    }

    func testArchiveFreshnessUsesTheClientObservationWhenClocksDiffer() {
        let copy = LocalResource(resourceId: asset, sha256: String(repeating: "a", count: 64), sizeBytes: 1,
            generation: 1, verified: true, pinned: false, active: false, original: true, transferring: false)
        let receipt = ArchiveReceipt(receiptId: edition, resourceId: asset, editionId: edition, sha256: copy.sha256,
            sizeBytes: 1, verifiedAt: 1000000000, retained: true)
        XCTAssertTrue(StoragePolicy.canEvict(copy, receipt: receipt, expectedGeneration: 1, nowMs: 10000, verifiedLocallyAt: 9999))
        XCTAssertFalse(StoragePolicy.canEvict(copy, receipt: receipt, expectedGeneration: 1, nowMs: 100000, verifiedLocallyAt: 9999))
    }

    func testDownloadedFilesSurviveAppContainerMove() throws {
        // Xcode reinstalls move the container: stored paths must stay valid.
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let first = root.appendingPathComponent("A/PudgeAudio", isDirectory: true)
        let store = try LocalStore.open(at: first)
        var book = try manifest()
        let folder = first.appendingPathComponent("resources", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        for index in book.parts.indices {
            let url = folder.appendingPathComponent("\(index).mp3")
            try Data("a".utf8).write(to: url)
            book.parts[index].sha256 = try FileIntegrity.sha256(url)
            try store.markVerified(book.parts[index], at: url)
        }
        let moved = root.appendingPathComponent("B/PudgeAudio", isDirectory: true)
        try FileManager.default.createDirectory(at: moved.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.moveItem(at: first, to: moved)
        let reopened = try LocalStore.open(at: moved)
        XCTAssertTrue(try reopened.ready(book))
        XCTAssertTrue(try reopened.resource(book.parts[0].resourceId)!.url.path.hasPrefix(moved.path))
        XCTAssertEqual(reopened.resolvedURL("/old/container/PudgeAudio/resources/x.mp3").path, moved.appendingPathComponent("resources/x.mp3").path)
    }
    func testManifestSeriesFieldsAreOptional() throws {
        XCTAssertNil(try manifest().seriesTitle)
        var data = try JSONEncoder().encode(try manifest())
        var object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        object["seriesTitle"] = "Spice and Wolf"; object["volume"] = 2
        data = try JSONSerialization.data(withJSONObject: object)
        let decoded = try AudioManifest.decode(data)
        XCTAssertEqual(decoded.seriesTitle, "Spice and Wolf")
        XCTAssertEqual(decoded.volume, 2)
    }
    func testReadAlongFollowsTheAudioClock() throws {
        let json = """
        {"schema":1,"title":"Novel","chapters":[{"index":1,"title":"One","startMs":0,"endMs":4000,
         "sentences":[{"p":0,"text":"「おはよう。」","start":0,"end":4},{"p":0,"text":"と言った。","start":4,"end":8},{"p":1,"text":"彼は笑った！","start":8,"end":13}],
         "anchors":[[0,0],[2000,8],[4000,13]]}]}
        """
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        try Data(json.utf8).write(to: url); defer { try? FileManager.default.removeItem(at: url) }
        let document = try ReadAlongDocument.load(url)
        XCTAssertEqual(document.location(atMs: 500)?.sentence, 0)
        XCTAssertEqual(document.location(atMs: 1500)?.sentence, 1)
        XCTAssertEqual(document.location(atMs: 3000)?.sentence, 2)
        let chapter = document.chapters[0]
        XCTAssertEqual(chapter.timeMs(atOffset: 8), 2000)
        XCTAssertEqual(chapter.paragraphRanges, [0..<2, 2..<3])
    }
    func testStudyTokensSplitTextInUTF16Units() throws {
        let tokens = try JSONDecoder().decode([StudyToken].self, from: Data("[[0,2,5,1,2],[3,4,9,0]]".utf8))
        XCTAssertEqual(tokens[0].state, 2)
        XCTAssertEqual(tokens[1].state, 0)
        let segments = StudyText.segments("𠮟る猫だ", tokens: tokens)
        XCTAssertEqual(segments.map(\.text), ["𠮟", "る", "猫", "だ"])
        XCTAssertEqual(segments.map(\.token), [0, nil, 1, nil])
        XCTAssertEqual(StudyText.sentence("前だ。猫が来た！後", around: StudyToken(start: 3, end: 4, wordId: 1, readingIndex: 0, state: 0)), "猫が来た！")
    }
    func testSubtitleCueLookupAndReadAlongWords() throws {
        let track = try JSONDecoder().decode(SubtitleTrack.self, from: Data("""
        {"schema":1,"tokenized":true,"cues":[{"start":1000,"end":2000,"text":"a","tokens":[]},{"start":1500,"end":4000,"text":"b","tokens":[]},{"start":5000,"end":6000,"text":"c","tokens":[]}]}
        """.utf8))
        XCTAssertNil(track.cue(atMs: 500))
        XCTAssertEqual(track.cue(atMs: 1200), 0)
        XCTAssertEqual(track.cue(atMs: 2500), 1)
        XCTAssertNil(track.cue(atMs: 4500))
        XCTAssertEqual(track.cue(atMs: 5999), 2)
        let sentence = try JSONDecoder().decode(ReadAlongSentence.self, from: Data(#"{"p":0,"text":"猫が来た","start":10,"end":14,"w":[[0,1,10,5,0],[2,4,12,6,0]]}"#.utf8))
        XCTAssertNil(sentence.word(atOffset: 9))
        XCTAssertEqual(sentence.word(atOffset: 11.5), 0)
        XCTAssertEqual(sentence.word(atOffset: 13), 1)
        XCTAssertEqual(sentence.words[1].token.wordId, 6)
        let chapter = try JSONDecoder().decode(ChapterText.self, from: Data(#"{"parsing":false,"index":0,"title":"","paragraphs":[{"text":"ab","image":false,"tokens":[]},{"text":"cdef","image":false,"tokens":[]},{"text":"gh","image":false,"tokens":[]}]}"#.utf8))
        XCTAssertEqual(chapter.paragraph(atFraction: 0), 0)
        XCTAssertEqual(chapter.paragraph(atFraction: 0.5), 1)
        XCTAssertEqual(chapter.paragraph(atFraction: 1), 2)
        XCTAssertEqual(chapter.fraction(atParagraph: 2), 0.75)
    }
    func testReaderPositionMapsToTheAudioClockAndFurigana() throws {
        let chapter = try JSONDecoder().decode(ReadAlongChapter.self, from: Data(#"{"index":0,"title":"","startMs":0,"endMs":9000,"sentences":[{"p":0,"text":"猫だ。","start":0,"end":2,"w":[[0,1,0,1,0]]},{"p":0,"text":"走る！","start":2,"end":4,"w":[[0,2,2,2,0]]},{"p":1,"text":"終わり","start":4,"end":7}],"anchors":[[0,0],[7000,7]]}"#.utf8))
        XCTAssertEqual(chapter.offset(paragraph: 0, utf16: 0), 0)
        XCTAssertEqual(chapter.offset(paragraph: 0, utf16: 4), 2)
        XCTAssertEqual(chapter.offset(paragraph: 1, utf16: 2), 4)
        XCTAssertNil(chapter.offset(paragraph: 5, utf16: 0))
        let paragraph = try JSONDecoder().decode(TextParagraph.self, from: Data(#"{"text":"食べる","image":false,"tokens":[[0,3,1,0,0,1]],"ruby":[[0,1,"た"]]}"#.utf8))
        XCTAssertTrue(paragraph.tokens[0].wantsFurigana)
        XCTAssertFalse(paragraph.tokens[0].wantsMark)
        XCTAssertEqual(paragraph.ruby?.first?.reading, "た")
        XCTAssertEqual(paragraph.token(at: 2), 0)
        XCTAssertNil(paragraph.token(at: 3))
        XCTAssertEqual(ReaderLook().color(state: 2), "#f97316")
        let mature = StudyToken(start: 0, end: 1, wordId: 1, readingIndex: 0, state: 1, flags: 3 | (4 << 7))
        XCTAssertEqual(mature.wordState, "mature")
        XCTAssertEqual(ReaderLook().color(for: mature), "#4ade80")
        XCTAssertEqual(ReaderLook(colors: ["mature": ""]).color(for: mature), "#4ade80")
        XCTAssertEqual(ReaderLook().color(for: StudyToken(start: 0, end: 1, wordId: 1, readingIndex: 0, state: 0, flags: 3 | (2 << 7))), "#f43f5e")
        XCTAssertNil(paragraph.imageId)
        let image = try JSONDecoder().decode(TextParagraph.self, from: Data(#"{"text":"","image":true,"tokens":[],"ruby":[],"imageId":"ln-abc.png"}"#.utf8))
        XCTAssertEqual(image.imageId, "ln-abc.png")
        XCTAssertNil(ReaderLook().blurImages)
    }
    func testReadingClockCountsLikeTheMacAndStatesUpdateTokens() throws {
        XCTAssertEqual(ReadingClock.spokenLength("「猫、だ。」 ab"), 4)
        let tokens = [StudyToken(start: 1, end: 2, wordId: 1, readingIndex: 0, state: 0),
                      StudyToken(start: 3, end: 4, wordId: 2, readingIndex: 0, state: 0)]
        XCTAssertEqual(ReadingClock.tokenOffsets("「猫、だ。」", tokens: tokens, base: 10), [10, 11])
        let rows = try JSONDecoder().decode(StudyStates.self, from: Data(#"{"rows":[[2,0,3,4]]}"#.utf8)).rows
        let updated = tokens.updating(Dictionary(uniqueKeysWithValues: rows.map { ($0.key, $0) }))
        XCTAssertEqual(updated[0].state, 0)
        XCTAssertEqual(updated[1].state, 3)
        XCTAssertTrue(updated[1].isMature)
        let chapter = try JSONDecoder().decode(ChapterText.self, from: Data(#"{"parsing":false,"index":2,"title":"","paragraphs":[{"text":"a","image":false,"tokens":[]},{"text":"","image":true,"tokens":[]},{"text":"b","image":false,"tokens":[]}]}"#.utf8))
        XCTAssertEqual(chapter.readerParagraph(textIndex: 1), 2)
        XCTAssertNil(chapter.readerParagraph(textIndex: 2))
    }
    func testOfflineWordsKeepCompleteEntriesAndLastStatesAcrossRestart() throws {
        func word(_ id: Int, meanings: [String], complete: Bool) throws -> WordDetail {
            let list = String(decoding: try JSONEncoder().encode(meanings), as: UTF8.self)
            let json = #"{"wordId":\#(id),"readingIndex":0,"spelling":"猫","reading":"ねこ","readings":[],"meanings":\#(list),"state":0,"stateLabel":"","complete":\#(complete)}"#
            return try JSONDecoder().decode(WordDetail.self, from: Data(json.utf8))
        }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let file = folder.appendingPathComponent("offline-words.json")
        var words = OfflineWords()
        words.remember([try word(1, meanings: ["cat"], complete: true)], at: 10)
        // A later incomplete answer never replaces the complete entry.
        words.remember([try word(1, meanings: [], complete: false), try word(2, meanings: [], complete: false)], at: 20)
        XCTAssertEqual(words.detail("1:0")?.meanings, ["cat"])
        let rows = try JSONDecoder().decode(StudyStates.self, from: Data(#"{"rows":[[1,0,3,4],[3,0,1,3]]}"#.utf8)).rows
        words.remember(states: rows, at: 30)
        try words.save(file)
        let restored = OfflineWords.load(file)
        XCTAssertEqual(restored.detail("1:0")?.meanings, ["cat"])
        XCTAssertEqual(restored.state("1:0")?.state, 3)
        XCTAssertEqual(Set(restored.states(newerThan: 25).keys), ["1:0", "3:0"])
        XCTAssertTrue(restored.states(newerThan: 30).isEmpty)
        // Words still to learn are asked first; complete ones are not asked again.
        let tokens = [StudyToken(start: 0, end: 1, wordId: 1, readingIndex: 0, state: 0),
                      StudyToken(start: 1, end: 2, wordId: 4, readingIndex: 0, state: 3),
                      StudyToken(start: 2, end: 3, wordId: 2, readingIndex: 0, state: 1),
                      StudyToken(start: 3, end: 4, wordId: 2, readingIndex: 0, state: 1),
                      StudyToken(start: 4, end: 5, wordId: 0, readingIndex: 0, state: 0)]
        XCTAssertEqual(restored.missing(tokens), [[2, 0], [4, 0]])
        XCTAssertTrue(OfflineWords.load(folder.appendingPathComponent("missing.json")).entries.isEmpty)
    }
    func testReadingPackageUnpacksIntoAFolderAndRefusesDamage() throws {
        func makePackage(_ files: [(String, Data)], offsetShift: Int64 = 0) -> Data {
            var entries: [[String: Any]] = [], body = Data(), offset: Int64 = 0
            for (name, data) in files {
                entries.append(["name": name, "offset": offset + offsetShift, "size": data.count])
                body += data; offset += Int64(data.count)
            }
            let header = try! JSONSerialization.data(withJSONObject: ["format": 1, "files": entries])
            var length = UInt64(header.count).bigEndian
            return Data("PUDGEPK1".utf8) + Data(bytes: &length, count: 8) + header + body
        }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("one.pudgepkg"), target = folder.appendingPathComponent("ln-3")
        let description = #"{"revision":"r1","title":"本","book":{"id":3,"title":"本","audioAssetId":null,"chapter":1,"offset":0.5,"finished":false,"chapters":[{"index":0,"title":"一","characters":2}]},"item":null}"#
        try makePackage([("package.json", Data(description.utf8)), ("chapter-0.json", Data(#"{"parsing":false,"index":0,"title":"一","paragraphs":[{"text":"猫","image":false,"tokens":[[0,1,5,0,1,3]]}]}"#.utf8)), ("empty", Data())]).write(to: file)
        XCTAssertEqual(try ReadingPackage.unpack(file, into: target), ["package.json", "chapter-0.json", "empty"])
        let info = try JSONDecoder().decode(NovelPackage.self, from: Data(contentsOf: target.appendingPathComponent("package.json")))
        XCTAssertEqual(info.book.chapter, 1)
        XCTAssertNil(info.item)
        let chapter = try JSONDecoder().decode(ChapterText.self, from: Data(contentsOf: target.appendingPathComponent("chapter-0.json")))
        XCTAssertEqual(chapter.paragraphs[0].tokens[0].wordId, 5)
        // A damaged or unsafe package never replaces the copy on the iPhone.
        try makePackage([("../escape", Data("x".utf8))]).write(to: file)
        XCTAssertThrowsError(try ReadingPackage.unpack(file, into: target))
        try makePackage([("package.json", Data("{}".utf8))], offsetShift: 1).write(to: file)
        XCTAssertThrowsError(try ReadingPackage.unpack(file, into: target))
        try Data("PUDGEPK2".utf8).write(to: file)
        XCTAssertThrowsError(try ReadingPackage.unpack(file, into: target))
        XCTAssertTrue(FileManager.default.fileExists(atPath: target.appendingPathComponent("chapter-0.json").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: target.path + ".unpacking"))
        XCTAssertFalse(ReadingPackage.isSafe(".hidden"))
        XCTAssertTrue(ReadingPackage.isSafe("image-ln-0123.png"))
    }
    func testDownloadedVolumesUpdateWholeOrOnlyTheirOCR() throws {
        let rows = try JSONDecoder().decode(ReadingRevisions.self, from: Data(#"{"items":[{"kind":"ln","id":3,"revision":"b"},{"kind":"manga","id":4,"revision":"m2","pagesRevision":"p1","ocrRevision":"o1","ocrComplete":true},{"kind":"manga","id":5,"revision":"m3","pagesRevision":"p2","ocrRevision":"","ocrComplete":false},{"kind":"ln","id":6,"missing":true}]}"#.utf8)).items
        XCTAssertEqual(ReadingRevisions.update(rows[0], revision: "a", pagesRevision: nil, ocrRevision: nil), .volume)
        XCTAssertEqual(ReadingRevisions.update(rows[0], revision: "b", pagesRevision: nil, ocrRevision: nil), .none)
        // Same pages, OCR finished on the Mac: only the OCR comes.
        XCTAssertEqual(ReadingRevisions.update(rows[1], revision: "m1", pagesRevision: "p1", ocrRevision: ""), .ocr)
        XCTAssertEqual(ReadingRevisions.update(rows[1], revision: "m2", pagesRevision: "p1", ocrRevision: "o1"), .none)
        XCTAssertEqual(ReadingRevisions.update(rows[1], revision: "m1", pagesRevision: "p0", ocrRevision: "o1"), .volume)
        // Incomplete OCR on the Mac never replaces what the phone has.
        XCTAssertEqual(ReadingRevisions.update(rows[2], revision: "m3", pagesRevision: "p2", ocrRevision: nil), .none)
        // A volume downloaded by an older version (no pages revision) is compared as a whole.
        XCTAssertEqual(ReadingRevisions.update(rows[2], revision: "old", pagesRevision: nil, ocrRevision: nil), .volume)
        XCTAssertEqual(ReadingRevisions.update(rows[3], revision: "x", pagesRevision: nil, ocrRevision: nil), .none)
    }
    func testVolumesDeletedOnTheMacLeaveAWeekLaterUnlessOpenedSince() {
        let day = 86_400.0, deleted = 1_000_000.0
        XCTAssertFalse(ReadingRevisions.removable(deletedAt: nil, openedAt: nil, now: deleted + 30 * day))
        XCTAssertFalse(ReadingRevisions.removable(deletedAt: deleted, openedAt: nil, now: deleted + 6 * day))
        XCTAssertTrue(ReadingRevisions.removable(deletedAt: deleted, openedAt: nil, now: deleted + 7 * day))
        // Opened before the deletion only: still removed; opened since: kept until removed by hand.
        XCTAssertTrue(ReadingRevisions.removable(deletedAt: deleted, openedAt: deleted - day, now: deleted + 8 * day))
        XCTAssertFalse(ReadingRevisions.removable(deletedAt: deleted, openedAt: deleted + day, now: deleted + 30 * day))
    }
    func testPositionsOfSeveralDevicesAreNamed() throws {
        let state = try JSONDecoder().decode(ProgressState.self, from: Data(#"{"revision":3,"selected":null,"conflicts":[],"finished":false,"playingOn":"pad","playingOnName":"Maksim iPad","deviceNames":{"pad":"Maksim iPad"}}"#.utf8))
        XCTAssertEqual(state.playingOnName, "Maksim iPad")
        XCTAssertEqual(state.deviceLabel("pad", this: "phone", thisLabel: "This iPhone"), "Maksim iPad")
        XCTAssertEqual(state.deviceLabel("phone", this: "phone", thisLabel: "This iPhone"), "This iPhone")
        XCTAssertEqual(state.deviceLabel("mac", this: "phone", thisLabel: "This iPhone"), "Mac")
        XCTAssertEqual(state.deviceLabel("other", this: "phone", thisLabel: "This iPhone"), "Another device")
        // An older Mac sends no names.
        let old = try JSONDecoder().decode(ProgressState.self, from: Data(#"{"revision":1,"selected":null,"conflicts":[],"finished":false}"#.utf8))
        XCTAssertNil(old.deviceNames)
        XCTAssertEqual(old.deviceLabel("pad", this: "phone", thisLabel: "This iPhone"), "Another device")
    }
    struct Volume: Identifiable { let id: Int; let series: String; let franchise: String?; let volume: Int; let updated: Double }
    func testShelvesGroupFranchiseSeriesAndVolumes() {
        let items = [Volume(id: 1, series: "a", franchise: "f", volume: 2, updated: 5),
                     Volume(id: 2, series: "a", franchise: "f", volume: 1, updated: 1),
                     Volume(id: 3, series: "b", franchise: "f", volume: 1, updated: 2),
                     Volume(id: 4, series: "c", franchise: nil, volume: 1, updated: 9)]
        let shelves = Shelves.group(items, franchise: { $0.franchise.map { ($0, "Franchise", 0) } }, series: { ($0.series, $0.series) },
                                    volume: { $0.volume }, updated: { $0.updated })
        XCTAssertEqual(shelves.map(\.key), ["series:c", "franchise:f"])
        XCTAssertEqual(shelves[1].series.map(\.key), ["a", "b"])
        XCTAssertEqual(shelves[1].series[0].items.map(\.id), [2, 1])
        let token = StudyToken(start: 0, end: 1, wordId: 1, readingIndex: 0, state: 1, flags: 3 | (5 << 3))
        XCTAssertEqual(token.jitenState, 4)
        XCTAssertEqual(StudyToken(start: 0, end: 1, wordId: 1, readingIndex: 0, state: 2).jitenState, 4)
        let look = try! JSONDecoder().decode(SubtitleLook.self, from: Data(#"{"source":"jiten-mpv","theme":"Default","fontFamily":"Hiragino Sans","fontSize":48,"borderSize":3,"unparsed":{"text":"#eeeeee","outline":"#000000","outlineSize":3},"states":[{"text":"#a566ef","outline":"#000000","outlineSize":3}]}"#.utf8))
        XCTAssertEqual(look.style(StudyToken(start: 0, end: 1, wordId: 1, readingIndex: 0, state: 0, flags: 1 << 3)).text, "#a566ef")
        XCTAssertEqual(look.style(token).text, "#eeeeee")
        XCTAssertEqual(look.style(nil).text, "#eeeeee")
    }
    func testActivityIntervalsWaitOfflineAndLeaveOnlyWhenAnswered() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let now = Date()
        let first = ActivityInterval(kind: "manga", item: 3, start: now.addingTimeInterval(-10), end: now.addingTimeInterval(-5),
                                     active: 5, from: ["page": 1], to: ["page": 2])
        let second = ActivityInterval(kind: "light_novel", item: 4, start: now.addingTimeInterval(-5), end: now,
                                      active: 5, from: ["chapter": 0, "fraction": 0.1], to: ["chapter": 0, "fraction": 0.12])
        do {
            let store = try LocalStore.open(at: folder)
            try store.enqueueActivity(first)
            try store.enqueueActivity(first)
            try store.enqueueActivity(second)
        }
        let restored = try LocalStore.open(at: folder)
        XCTAssertEqual(try restored.pendingActivity().map(\.id), [first.id, second.id])
        XCTAssertEqual(try restored.pendingActivityCount(), 2)
        try restored.acknowledgeActivity([first.id])
        XCTAssertEqual(try restored.pendingActivity().map(\.kind), ["light_novel"])
        try restored.pruneActivity(olderThan: now.addingTimeInterval(1))
        XCTAssertTrue(try restored.pendingActivity().isEmpty)
        XCTAssertEqual(try restored.rejectedActivity().map(\.reason), ["Not sent to the Mac within 30 days"])
        var invalid = first
        invalid.end = invalid.start + 60
        XCTAssertThrowsError(try restored.enqueueActivity(invalid))
        invalid = first
        invalid.kind = "audiobook"
        XCTAssertThrowsError(try restored.enqueueActivity(invalid))
    }
    func testStatisticsTheMacDidNotTakeStayWithTheirReasonAndCanBeSentAgain() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let now = Date()
        let first = ActivityInterval(kind: "manga", item: 3, start: now.addingTimeInterval(-10), end: now.addingTimeInterval(-5),
                                     active: 5, from: ["page": 1], to: ["page": 2])
        let second = ActivityInterval(kind: "manga", item: 3, start: now.addingTimeInterval(-5), end: now,
                                      active: 5, from: ["page": 2], to: ["page": 3])
        do {
            let store = try LocalStore.open(at: folder)
            try store.enqueueActivity(first)
            try store.enqueueActivity(second)
            try store.rejectActivity([first.id: "No longer on the Mac"])
            XCTAssertEqual(try store.pendingActivity().map(\.id), [second.id])
        }
        let restored = try LocalStore.open(at: folder)
        let rejected = try restored.rejectedActivity()
        XCTAssertEqual(rejected.map(\.id), [first.id])
        XCTAssertEqual(rejected.first?.reason, "No longer on the Mac")
        try restored.retryRejectedActivity()
        XCTAssertEqual(Set(try restored.pendingActivity().map(\.id)), [first.id, second.id])
        XCTAssertEqual(try restored.rejectedActivityCount(), 0)
    }
    func testSwitchingToAnotherMacKeepsBooksMovesTheLastPlaceAndCountsWhatIsDropped() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let store = try LocalStore.open(at: folder)
        let oldMac = UUID().uuidString.lowercased(), newMac = UUID().uuidString.lowercased()
        let macBook = try manifest()
        var phoneBook = try manifest()
        phoneBook.assetId = UUID().uuidString.lowercased(); phoneBook.editionId = UUID().uuidString.lowercased()
        let url = folder.appendingPathComponent("imported.mp3")
        try Data("a".utf8).write(to: url)
        phoneBook.parts[0].resourceId = UUID().uuidString.lowercased(); phoneBook.parts[0].sha256 = try FileIntegrity.sha256(url)
        phoneBook.parts[1].resourceId = UUID().uuidString.lowercased()
        try store.saveManifest(macBook); try store.saveManifest(phoneBook)
        try store.markVerified(phoneBook.parts[0], at: url, original: true)  // imported on the phone
        try store.bindMac(serverId: oldMac)
        try store.markRemote(macBook, serverId: oldMac)
        for (book, position) in [(macBook, Int64(1000)), (macBook, Int64(12000)), (phoneBook, Int64(3000))] {
            try store.enqueue(try AudioCheckpoint.make(book: book, deviceId: asset, sessionId: edition, sequence: position,
                                                       revision: 0, positionMs: position, reason: "pause"))
        }
        try store.enqueueBookmark(Bookmark(book: macBook, positionMs: 500, title: "mark"))
        let now = Date()
        try store.enqueueActivity(ActivityInterval(kind: "manga", item: 3, start: now.addingTimeInterval(-10), end: now.addingTimeInterval(-5),
                                                   active: 5, from: ["page": 1], to: ["page": 2]))
        XCTAssertThrowsError(try store.bindMac(serverId: newMac))

        let expected = try store.macSwitchSummary()
        XCTAssertEqual([expected.phoneBooks, expected.keptChanges, expected.carriedPositions, expected.droppedPositions,
                        expected.droppedBookmarks, expected.droppedStatistics], [1, 1, 1, 0, 1, 1])
        XCTAssertEqual(try store.rebindMac(to: newMac), expected)
        XCTAssertEqual(try store.boundMac(), newMac)
        XCTAssertNoThrow(try store.bindMac(serverId: newMac))
        XCTAssertEqual(try store.manifests().count, 2)  // no book is deleted
        XCTAssertEqual(try store.pendingCheckpoints().map(\.assetId), [phoneBook.assetId])
        XCTAssertTrue(try store.pendingBookmarks().isEmpty)
        XCTAssertEqual(try store.pendingActivityCount(), 0)

        // The new Mac lists the same audio under its own identifiers: the last unsent place moves there.
        var same = macBook
        same.assetId = UUID().uuidString.lowercased(); same.editionId = UUID().uuidString.lowercased()
        XCTAssertFalse(try store.adoptCarried(phoneBook))
        XCTAssertTrue(try store.adoptCarried(same))
        let moved = try XCTUnwrap(try store.pendingCheckpoints().first { $0.assetId == same.assetId })
        XCTAssertEqual(moved.positionMs, 12000)
        XCTAssertEqual(moved.editionId, same.editionId)
        XCTAssertEqual(try store.dropCarried(), 0)
    }
    func testStatsSummaryDecodesFunStatsAndOlderMacs() throws {
        let old = try JSONDecoder().decode(StatsSummary.self, from: Data(#"{"period":"7d","totalSeconds":60,"activeDays":1,"sessions":1,"streak":1,"breakdown":{"anime":60},"days":[],"works":[]}"#.utf8))
        XCTAssertNil(old.highlights)
        let json = #"{"period":"7d","totalSeconds":60,"activeDays":1,"sessions":1,"streak":1,"breakdown":{},"days":[],"works":[],"highlights":{"longestStreakDays":3,"busiestDay":{"day":"2026-10-01","seconds":600},"longestSession":{"title":"Odd Taxi · 1","kind":"anime","seconds":600},"topWork":null,"animeEpisodes":2,"lnCharactersPerMinute":212.5,"mangaPagesPerHour":null,"words":{"total":120,"unique":80,"new":12,"pending":2,"missing":0,"byKind":{"anime":{"total":120,"unique":80,"new":12}}}}}"#
        let stats = try JSONDecoder().decode(StatsSummary.self, from: Data(json.utf8))
        XCTAssertEqual(stats.highlights?.longestStreakDays, 3)
        XCTAssertEqual(stats.highlights?.busiestDay?.day, "2026-10-01")
        XCTAssertNil(stats.highlights?.topWork)
        XCTAssertEqual(stats.highlights?.words?.byKind["anime"]?.new, 12)
        XCTAssertEqual(stats.highlights?.words?.pending, 2)
    }
    func testCutEpisodeDownloadsAreRetriedButRefusalsAndCancelsAreNot() {
        let lost = URLError(.networkConnectionLost), stalled = URLError(.timedOut), restarting = URLError(.cannotConnectToHost)
        XCTAssertTrue(TransferRetry.shouldRetry(lost, attempt: 1))
        XCTAssertTrue(TransferRetry.shouldRetry(stalled, attempt: 2))
        XCTAssertTrue(TransferRetry.shouldRetry(restarting, attempt: 3))
        XCTAssertFalse(TransferRetry.shouldRetry(lost, attempt: TransferRetry.attempts))
        XCTAssertFalse(TransferRetry.shouldRetry(URLError(.cancelled), attempt: 1))
        XCTAssertFalse(TransferRetry.shouldRetry(URLError(.serverCertificateUntrusted), attempt: 1))
        XCTAssertFalse(TransferRetry.shouldRetry(PudgeError.invalid("File size or SHA-256 does not match"), attempt: 1))
        XCTAssertLessThanOrEqual(TransferRetry.delay(after: 10), 15)
        XCTAssertGreaterThan(TransferRetry.delay(after: 1), 0)
    }
}
