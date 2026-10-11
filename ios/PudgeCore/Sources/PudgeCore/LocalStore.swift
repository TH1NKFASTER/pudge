import Foundation
import CSQLite

public final class LocalStore: @unchecked Sendable {
    public let directory: URL
    private var db: OpaquePointer?
    private let lock = NSRecursiveLock()
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    public static func open(at directory: URL) throws -> LocalStore { try LocalStore(directory) }
    private init(_ directory: URL) throws {
        self.directory = directory
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let path = directory.appendingPathComponent("state.sqlite").path
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            throw PudgeError.invalid("Could not open the local database")
        }
        sqlite3_busy_timeout(db, 5000)
        let version = Int(try query("PRAGMA user_version").first?[0] ?? "0") ?? 0
        guard version <= 3 else { throw PudgeError.invalid("The local database was created by a newer Pudge") }
        try execute("PRAGMA journal_mode=WAL")
        try execute("PRAGMA synchronous=FULL")
        try execute("CREATE TABLE IF NOT EXISTS manifests(asset TEXT PRIMARY KEY, body TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS positions(asset TEXT PRIMARY KEY, position INTEGER NOT NULL, edition TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)")
        try execute("CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,asset TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY,body TEXT NOT NULL,path TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,body TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS bookmarks(id TEXT PRIMARY KEY,asset TEXT NOT NULL,body TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS edition_positions(asset TEXT NOT NULL,edition TEXT NOT NULL,position INTEGER NOT NULL,PRIMARY KEY(asset,edition))")
        try execute("INSERT OR IGNORE INTO edition_positions(asset,edition,position) SELECT asset,edition,position FROM positions")
        try execute("CREATE TABLE IF NOT EXISTS handoff_pauses(operation TEXT PRIMARY KEY,body TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        try execute("CREATE TABLE IF NOT EXISTS remote_editions(server TEXT NOT NULL,asset TEXT NOT NULL,edition TEXT NOT NULL,PRIMARY KEY(server,asset,edition))")
        try execute("CREATE TABLE IF NOT EXISTS latest_checkpoints(asset TEXT NOT NULL,edition TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(asset,edition))")
        try execute("CREATE TABLE IF NOT EXISTS activity(id TEXT PRIMARY KEY,created REAL NOT NULL,body TEXT NOT NULL)")
        // Statistics the Mac answered but did not take, with its reason (shown in Diagnostics).
        try execute("CREATE TABLE IF NOT EXISTS activity_rejected(id TEXT PRIMARY KEY,created REAL NOT NULL,body TEXT NOT NULL,reason TEXT NOT NULL)")
        // After switching Macs: the last unsent place of a Mac's book, by its audio (MacSwitchSummary).
        try execute("CREATE TABLE IF NOT EXISTS carried_positions(media TEXT PRIMARY KEY,body TEXT NOT NULL)")
        try execute("PRAGMA user_version=3")
        encoder.outputFormatting = [.sortedKeys]
    }
    deinit { sqlite3_close(db) }
    private func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock(); defer { lock.unlock() }; return try body()
    }
    private func error() -> PudgeError { .invalid(String(cString: sqlite3_errmsg(db))) }
    private func query(_ sql: String, _ args: [String] = []) throws -> [[String]] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else { throw error() }
        defer { sqlite3_finalize(statement) }
        for (index, arg) in args.enumerated() {
            guard sqlite3_bind_text(statement, Int32(index + 1), arg, -1, transient) == SQLITE_OK else { throw error() }
        }
        var rows: [[String]] = []
        while true {
            let result = sqlite3_step(statement)
            if result == SQLITE_DONE { return rows }
            guard result == SQLITE_ROW else { throw error() }
            rows.append((0..<sqlite3_column_count(statement)).map { index in
                sqlite3_column_text(statement, index).map { String(cString: $0) } ?? ""
            })
        }
    }
    private func execute(_ sql: String, _ args: [String] = []) throws { _ = try query(sql, args) }
    private func json<T: Encodable>(_ value: T) throws -> String { String(decoding: try encoder.encode(value), as: UTF8.self) }
    private func decoded<T: Decodable>(_ value: String, as type: T.Type) throws -> T { try decoder.decode(type, from: Data(value.utf8)) }
    private func transaction<T>(_ body: () throws -> T) throws -> T {
        try execute("BEGIN IMMEDIATE")
        do { let result = try body(); try execute("COMMIT"); return result }
        catch { try? execute("ROLLBACK"); throw error }
    }
    public func saveManifest(_ book: AudioManifest) throws {
        try book.validate()
        try withLock { try execute("INSERT INTO manifests(asset,body) VALUES(?,?) ON CONFLICT(asset) DO UPDATE SET body=excluded.body", [book.id, try json(book)]) }
    }
    public func manifests() throws -> [AudioManifest] {
        try withLock { try query("SELECT body FROM manifests ORDER BY asset").map { try AudioManifest.decode(Data($0[0].utf8)) } }
    }
    public func enqueue(_ checkpoint: AudioCheckpoint) throws {
        try withLock { try transaction { try enqueueLocked(checkpoint) } }
    }
    private func enqueueLocked(_ checkpoint: AudioCheckpoint) throws {
                let body = try json(checkpoint)
                let old = try query("SELECT body FROM outbox WHERE id=?", [checkpoint.checkpointId]).first
                if let old, old[0] != body { throw PudgeError.invalid("Event retry with different data") }
                try execute("INSERT OR IGNORE INTO outbox(id,asset,kind,body) VALUES(?,?,?,?)", [checkpoint.checkpointId, checkpoint.assetId, "checkpoint", body])
                if old == nil {
                    try execute("INSERT INTO edition_positions(asset,edition,position) VALUES(?,?,?) ON CONFLICT(asset,edition) DO UPDATE SET position=excluded.position", [checkpoint.assetId, checkpoint.editionId, String(checkpoint.positionMs)])
                    try execute("INSERT INTO positions(asset,position,edition) VALUES(?,?,?) ON CONFLICT(asset) DO UPDATE SET position=excluded.position,edition=excluded.edition", [checkpoint.assetId, String(checkpoint.positionMs), checkpoint.editionId])
                    try execute("INSERT INTO latest_checkpoints(asset,edition,id) VALUES(?,?,?) ON CONFLICT(asset,edition) DO UPDATE SET id=excluded.id", [checkpoint.assetId, checkpoint.editionId, checkpoint.checkpointId])
                }
    }
    public func bindHandoff(operationId: String, checkpoint: AudioCheckpoint) throws {
        guard UUID(uuidString: operationId) != nil, checkpoint.reason == "handoff" else { throw PudgeError.invalid("Invalid listening handoff") }
        try withLock { try transaction {
            let body = try json(checkpoint)
            if let previous = try query("SELECT body FROM handoff_pauses WHERE operation=?", [operationId]).first {
                guard previous[0] == body else { throw PudgeError.invalid("The handoff was already saved with another position") }
                return
            }
            try enqueueLocked(checkpoint)
            try execute("INSERT INTO handoff_pauses(operation,body) VALUES(?,?)", [operationId, body])
        } }
    }
    public func handoffCheckpoint(operationId: String) throws -> AudioCheckpoint? {
        try withLock {
            guard let row = try query("SELECT body FROM handoff_pauses WHERE operation=?", [operationId]).first else { return nil }
            return try decoded(row[0], as: AudioCheckpoint.self)
        }
    }
    public func checkMac(serverId: String) throws {
        guard UUID(uuidString: serverId) != nil else { throw PudgeError.invalid("Invalid Mac identifier") }
        try withLock {
            if let row = try query("SELECT value FROM metadata WHERE key='server'").first, row[0] != serverId {
                throw PudgeError.invalid("Local data belongs to another Mac. Connect the original Mac; books and the queue are kept.")
            }
        }
    }
    public func bindMac(serverId: String) throws {
        try withLock { try checkMac(serverId: serverId); try execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('server',?)", [serverId]) }
    }
    /// The Mac this phone's books and queue belong to (nil before the first pairing).
    public func boundMac() throws -> String? {
        try withLock { try query("SELECT value FROM metadata WHERE key='server'").first?[0] }
    }
    /// Imported on this iPhone/iPad: the book's identity is the phone's own, on any Mac.
    func isPhoneBook(_ book: AudioManifest) throws -> Bool {
        for part in book.parts {
            if let copy = try resource(part.resourceId), copy.state.original { return true }
        }
        return false
    }
    private func switchPlan() throws -> (summary: MacSwitchSummary, carried: [String: AudioCheckpoint], drop: [String]) {
        var summary = MacSwitchSummary()
        var books: [String: AudioManifest] = [:]
        var phone = Set<String>()
        for book in try manifests() {
            books[book.assetId] = book
            if try isPhoneBook(book) { phone.insert(book.assetId) }
        }
        summary.phoneBooks = phone.count
        var carried: [String: AudioCheckpoint] = [:]
        var lost = Set<String>()
        var drop: [String] = []
        for row in try query("SELECT id,asset,kind,body FROM outbox ORDER BY seq") {
            if phone.contains(row[1]) { summary.keptChanges += 1; continue }
            drop.append(row[0])
            if row[2] == "bookmark" { summary.droppedBookmarks += 1; continue }
            let checkpoint = try decoded(row[3], as: AudioCheckpoint.self)
            // The newest place of each book moves (rows are in queue order).
            if let book = books[row[1]], checkpoint.editionId == book.editionId { carried[book.mediaKey] = checkpoint }
            else { lost.insert(row[1]) }
        }
        summary.carriedPositions = carried.count
        summary.droppedPositions = lost.count
        summary.droppedStatistics = try pendingActivityCount()
        return (summary, carried, drop)
    }
    /// What "Switch to this Mac" would keep, move and drop (nothing changes yet).
    public func macSwitchSummary() throws -> MacSwitchSummary {
        try withLock { try switchPlan().summary }
    }
    /// Binds the local data to another Mac. Books stay; unsent changes of books imported here stay
    /// queued (same identity on the new Mac); the last unsent place of other books waits for the
    /// same audio on the new Mac (adoptCarried); unsent bookmarks of those books and statistics
    /// (numbered by the previous Mac's library) are dropped. Returns the counts.
    @discardableResult
    public func rebindMac(to serverId: String) throws -> MacSwitchSummary {
        guard UUID(uuidString: serverId) != nil else { throw PudgeError.invalid("Invalid Mac identifier") }
        return try withLock {
            try transaction {
                let plan = try switchPlan()
                for id in plan.drop { try execute("DELETE FROM outbox WHERE id=?", [id]) }
                for (media, checkpoint) in plan.carried {
                    try execute("INSERT INTO carried_positions(media,body) VALUES(?,?) ON CONFLICT(media) DO UPDATE SET body=excluded.body", [media, try json(checkpoint)])
                }
                try execute("DELETE FROM activity")
                try execute("DELETE FROM handoff_pauses")
                try execute("INSERT INTO metadata(key,value) VALUES('server',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [serverId])
                return plan.summary
            }
        }
    }
    public func carriedCount() throws -> Int {
        try withLock { Int(try query("SELECT COUNT(*) FROM carried_positions").first?[0] ?? "0") ?? 0 }
    }
    /// A book of the new Mac with the same audio as a book of the previous one: its last unsent
    /// place is queued for the new Mac. True when a place moved.
    @discardableResult
    public func adoptCarried(_ book: AudioManifest) throws -> Bool {
        try withLock {
            try transaction {
                guard let row = try query("SELECT body FROM carried_positions WHERE media=?", [book.mediaKey]).first else { return false }
                try execute("DELETE FROM carried_positions WHERE media=?", [book.mediaKey])
                guard try !hasPending(assetId: book.assetId) else { return false }
                let old = try decoded(row[0], as: AudioCheckpoint.self)
                let moved = try AudioCheckpoint.make(book: book, deviceId: old.deviceId, sessionId: UUID().uuidString.lowercased(),
                                                     sequence: 1, revision: try revision(assetId: book.assetId),
                                                     positionMs: min(max(0, old.positionMs), book.durationMs), reason: "pause")
                try enqueueLocked(moved)
                return true
            }
        }
    }
    /// Places with no matching audio on the new Mac (after its full catalog): dropped, counted.
    public func dropCarried() throws -> Int {
        try withLock {
            let count = try carriedCount()
            try execute("DELETE FROM carried_positions")
            return count
        }
    }
    public func markRemote(_ book: AudioManifest, serverId: String) throws {
        try withLock { try bindMac(serverId: serverId); try execute("INSERT OR IGNORE INTO remote_editions(server,asset,edition) VALUES(?,?,?)", [serverId, book.assetId, book.editionId]) }
    }
    public func isRemote(_ book: AudioManifest, serverId: String) throws -> Bool {
        try withLock { !(try query("SELECT 1 FROM remote_editions WHERE server=? AND asset=? AND edition=?", [serverId, book.assetId, book.editionId])).isEmpty }
    }
    public func latestCheckpointId(_ book: AudioManifest) throws -> String? {
        try withLock { try query("SELECT id FROM latest_checkpoints WHERE asset=? AND edition=?", [book.assetId, book.editionId]).first?[0] }
    }
    public func pendingCheckpoints() throws -> [AudioCheckpoint] {
        try withLock { try query("SELECT body FROM outbox WHERE kind='checkpoint' ORDER BY seq").map { try decoded($0[0], as: AudioCheckpoint.self) } }
    }
    public func acknowledge(_ checkpointId: String, revision: Int64) throws {
        try withLock {
            try transaction {
                let row = try query("SELECT asset FROM outbox WHERE id=?", [checkpointId]).first
                if let row {
                    try execute("UPDATE positions SET revision=MAX(revision,?) WHERE asset=?", [String(revision), row[0]])
                    try execute("DELETE FROM outbox WHERE id=?", [checkpointId])
                }
            }
        }
    }
    public func hasPending(assetId: String) throws -> Bool {
        try withLock { !(try query("SELECT 1 FROM outbox WHERE asset=? LIMIT 1", [assetId])).isEmpty }
    }
    public func localPosition(assetId: String, editionId: String? = nil) throws -> Int64 {
        try withLock {
            if let editionId {
                return Int64(try query("SELECT position FROM edition_positions WHERE asset=? AND edition=?", [assetId, editionId]).first?[0] ?? "0") ?? 0
            }
            guard let row = try query("SELECT position,edition FROM positions WHERE asset=?", [assetId]).first else { return 0 }
            return Int64(row[0]) ?? 0
        }
    }
    public func revision(assetId: String) throws -> Int64 {
        try withLock { Int64(try query("SELECT revision FROM positions WHERE asset=?", [assetId]).first?[0] ?? "0") ?? 0 }
    }
    public func adopt(_ state: ProgressState, book: AudioManifest) throws {
        try withLock {
            guard try !hasPending(assetId: book.assetId), let selected = state.selected,
                  selected.editionId == book.editionId, selected.timelineRevision == book.timelineRevision else { return }
            try execute("INSERT INTO edition_positions(asset,edition,position) VALUES(?,?,?) ON CONFLICT(asset,edition) DO UPDATE SET position=excluded.position", [book.assetId, book.editionId, String(selected.positionMs)])
            try execute("INSERT INTO positions(asset,position,edition,revision) VALUES(?,?,?,?) ON CONFLICT(asset) DO UPDATE SET position=excluded.position,edition=excluded.edition,revision=excluded.revision", [book.assetId, String(selected.positionMs), book.editionId, String(state.revision)])
            try execute("INSERT INTO latest_checkpoints(asset,edition,id) VALUES(?,?,?) ON CONFLICT(asset,edition) DO UPDATE SET id=excluded.id", [book.assetId, book.editionId, selected.checkpointId])
        }
    }
    public func resource(_ id: String) throws -> (state: LocalResource, url: URL)? {
        try withLock {
            guard let row = try query("SELECT body,path FROM resources WHERE id=?", [id]).first else { return nil }
            return (try decoded(row[0], as: LocalResource.self), resolvedURL(row[1]))
        }
    }
    /// iOS moves the app container on every reinstall/update from Xcode, so
    /// resource paths are stored relative to the store directory. Older
    /// absolute paths are re-based onto the current container.
    func resolvedURL(_ stored: String) -> URL {
        if !stored.hasPrefix("/") { return directory.appendingPathComponent(stored) }
        let marker = "/" + directory.lastPathComponent + "/"
        if let range = stored.range(of: marker, options: .backwards) {
            return directory.appendingPathComponent(String(stored[range.upperBound...]))
        }
        return URL(fileURLWithPath: stored)
    }
    func storedPath(_ url: URL) -> String {
        let base = directory.standardizedFileURL.path + "/"
        let path = url.standardizedFileURL.path
        return path.hasPrefix(base) ? String(path.dropFirst(base.count)) : path
    }
    public func markVerified(_ part: AudioPart, at url: URL, original: Bool = false) throws {
        try FileIntegrity.verify(url, sha256: part.sha256, sizeBytes: part.sizeBytes)
        try withLock {
            let old = try resource(part.resourceId)
            let value = LocalResource(resourceId: part.resourceId, sha256: part.sha256, sizeBytes: part.sizeBytes,
                                      generation: (old?.state.generation ?? 0) + 1, verified: true,
                                      pinned: old?.state.pinned ?? false, active: old?.state.active ?? false,
                                      original: original || (old?.state.original ?? false), transferring: false)
            try execute("INSERT INTO resources(id,body,path) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,path=excluded.path", [part.resourceId, try json(value), storedPath(url)])
        }
    }
    public func ready(_ book: AudioManifest) throws -> Bool {
        try withLock {
            for part in book.parts {
                guard let copy = try resource(part.resourceId), copy.state.verified, copy.state.sha256 == part.sha256,
                      FileManager.default.fileExists(atPath: copy.url.path),
                      (try? copy.url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map({ Int64($0) }) == part.sizeBytes else { return false }
            }
            return true
        }
    }
    public func setProtection(resourceId: String, pinned: Bool? = nil, active: Bool? = nil) throws {
        try withLock {
            guard var copy = try resource(resourceId) else { return }
            if let pinned { copy.state.pinned = pinned }
            if let active { copy.state.active = active }
            try execute("UPDATE resources SET body=? WHERE id=?", [try json(copy.state), resourceId])
        }
    }
    public func evict(resourceId: String, expectedGeneration: Int64, receipt: ArchiveReceipt, nowMs: Int64, verifiedLocallyAt: Int64? = nil) throws {
        try withLock {
            guard var copy = try resource(resourceId),
                  StoragePolicy.canEvict(copy.state, receipt: receipt, expectedGeneration: expectedGeneration, nowMs: nowMs, verifiedLocallyAt: verifiedLocallyAt) else {
                throw PudgeError.invalid("The file changed or is protected from deletion")
            }
            try FileIntegrity.verify(copy.url, sha256: copy.state.sha256, sizeBytes: copy.state.sizeBytes)
            try transaction {
                try FileManager.default.removeItem(at: copy.url)
                copy.state.verified = false; copy.state.generation += 1
                try execute("UPDATE resources SET body=? WHERE id=?", [try json(copy.state), resourceId])
            }
        }
    }
    public func saveTransfer(_ transfer: TransferRecord) throws {
        try withLock { try execute("INSERT INTO transfers(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", [transfer.id, try json(transfer)]) }
    }
    public func transfers() throws -> [TransferRecord] {
        try withLock { try query("SELECT body FROM transfers").map { try decoded($0[0], as: TransferRecord.self) } }
    }
    public func enqueueBookmark(_ incoming: Bookmark) throws {
        try withLock { try transaction {
            var bookmark = incoming
            if let previous = try query("SELECT body FROM outbox WHERE id=?", [bookmark.eventId]).first {
                let saved = try decoded(previous[0], as: Bookmark.self)
                bookmark.baseRevision = saved.baseRevision
                guard try json(bookmark) == previous[0] else { throw PudgeError.invalid("Bookmark retry with different data") }
                return
            }
            if let row = try query("SELECT body FROM bookmarks WHERE id=?", [bookmark.bookmarkId]).first {
                let local = try decoded(row[0], as: Bookmark.self)
                bookmark.baseRevision = max(bookmark.baseRevision, local.baseRevision)
            }
            let body = try json(bookmark)
            var next = bookmark; next.baseRevision += 1
            try execute("INSERT INTO bookmarks(id,asset,body) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", [bookmark.bookmarkId, bookmark.assetId, try json(next)])
            try execute("INSERT INTO outbox(id,asset,kind,body) VALUES(?,?,?,?)", [bookmark.eventId, bookmark.assetId, "bookmark", body])
        } }
    }
    /// Reading and watching time waiting for the Mac (kept offline, oldest first).
    public func enqueueActivity(_ interval: ActivityInterval) throws {
        try interval.validate()
        try withLock { try execute("INSERT OR IGNORE INTO activity(id,created,body) VALUES(?,?,?)", [interval.id, String(interval.end), try json(interval)]) }
    }
    public func pendingActivity(limit: Int = 200) throws -> [ActivityInterval] {
        try withLock { try query("SELECT body FROM activity ORDER BY created,id LIMIT \(max(1, limit))").map { try decoded($0[0], as: ActivityInterval.self) } }
    }
    public func pendingActivityCount() throws -> Int {
        try withLock { Int(try query("SELECT COUNT(*) FROM activity").first?[0] ?? "0") ?? 0 }
    }
    public func acknowledgeActivity(_ ids: [String]) throws {
        guard !ids.isEmpty else { return }
        try withLock { try transaction { for id in ids { try execute("DELETE FROM activity WHERE id=?", [id]) } } }
    }
    /// Answered but not taken by the Mac ({id: reason}): kept with the reason, not sent again.
    public func rejectActivity(_ reasons: [String: String]) throws {
        guard !reasons.isEmpty else { return }
        try withLock {
            try transaction {
                for (id, reason) in reasons { try moveToRejected("id=?", [id], reason: reason) }
                try trimRejected()
            }
        }
    }
    private func moveToRejected(_ condition: String, _ args: [String], reason: String) throws {
        try execute("INSERT OR REPLACE INTO activity_rejected(id,created,body,reason) SELECT id,created,body,? FROM activity WHERE " + condition, [reason] + args)
        try execute("DELETE FROM activity WHERE " + condition, args)
    }
    private func trimRejected() throws {
        try execute("DELETE FROM activity_rejected WHERE id NOT IN (SELECT id FROM activity_rejected ORDER BY created DESC LIMIT 2000)")
    }
    /// Time the Mac never received (no connection for weeks) leaves the queue for the rejected list.
    public func pruneActivity(olderThan cutoff: Date) throws {
        try withLock {
            try transaction {
                try moveToRejected("created<?", [String(cutoff.timeIntervalSince1970)], reason: "Not sent to the Mac within 30 days")
                try trimRejected()
            }
        }
    }
    public func rejectedActivity(limit: Int = 500) throws -> [RejectedActivity] {
        try withLock {
            try query("SELECT body,reason FROM activity_rejected ORDER BY created DESC,id LIMIT \(max(1, limit))").map {
                RejectedActivity(interval: try decoded($0[0], as: ActivityInterval.self), reason: $0[1])
            }
        }
    }
    public func rejectedActivityCount() throws -> Int {
        try withLock { Int(try query("SELECT COUNT(*) FROM activity_rejected").first?[0] ?? "0") ?? 0 }
    }
    /// "Send again" (e.g. after updating the Mac): back into the queue.
    public func retryRejectedActivity() throws {
        try withLock {
            try transaction {
                try execute("INSERT OR IGNORE INTO activity(id,created,body) SELECT id,?,body FROM activity_rejected", [String(Date().timeIntervalSince1970)])
                try execute("DELETE FROM activity_rejected")
            }
        }
    }
    public func clearRejectedActivity() throws {
        try withLock { try execute("DELETE FROM activity_rejected") }
    }
    public func pendingBookmarks() throws -> [Bookmark] {
        try withLock { try query("SELECT body FROM outbox WHERE kind='bookmark' ORDER BY seq").map { try decoded($0[0], as: Bookmark.self) } }
    }
    public func acknowledgeBookmark(_ eventId: String) throws {
        try withLock { try execute("DELETE FROM outbox WHERE id=? AND kind='bookmark'", [eventId]) }
    }
    public func mergeBookmarks(_ bookmarks: [Bookmark]) throws {
        try withLock { for bookmark in bookmarks {
            if try query("SELECT 1 FROM outbox WHERE kind='bookmark' AND asset=?", [bookmark.assetId]).isEmpty {
                if let row = try query("SELECT body FROM bookmarks WHERE id=?", [bookmark.bookmarkId]).first {
                    let existing = try decoded(row[0], as: Bookmark.self)
                    guard existing.baseRevision < bookmark.baseRevision else { continue }
                }
                try execute("INSERT INTO bookmarks(id,asset,body) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", [bookmark.bookmarkId, bookmark.assetId, try json(bookmark)])
            }
        } }
    }
    public func bookmarks(assetId: String) throws -> [Bookmark] {
        try withLock { try query("SELECT body FROM bookmarks WHERE asset=?", [assetId]).map { try decoded($0[0], as: Bookmark.self) }.filter { !$0.deleted }.sorted { $0.positionMs < $1.positionMs } }
    }
}

public struct RejectedActivity: Identifiable {
    public let interval: ActivityInterval
    public let reason: String
    public var id: String { interval.id }
}

extension AudioManifest {
    /// The same audio, whichever Mac lists it: content and timeline of its parts.
    public var mediaKey: String { parts.map { $0.sha256 + ":" + String($0.durationMs) }.joined(separator: ",") }
}

/// What switching this iPhone/iPad to another Mac keeps, moves and drops.
public struct MacSwitchSummary: Equatable {
    /// Audiobooks imported on this device: they stay, with their unsent changes.
    public var phoneBooks = 0
    public var keptChanges = 0
    /// Audiobooks of the previous Mac whose last unsent place moves to the same audio on the new Mac.
    public var carriedPositions = 0
    public var droppedPositions = 0
    public var droppedBookmarks = 0
    public var droppedStatistics = 0
    public init() {}
    public func text(device: String) -> String {
        var lines = ["Downloaded and imported books stay on this \(device)."]
        if keptChanges > 0 { lines.append("Audiobooks imported on this \(device) keep their \(keptChanges) unsent change(s).") }
        if carriedPositions > 0 { lines.append("The last unsent place of \(carriedPositions) audiobook(s) moves to the same audiobooks on the new Mac.") }
        var dropped: [String] = []
        if droppedPositions > 0 { dropped.append("\(droppedPositions) position(s)") }
        if droppedBookmarks > 0 { dropped.append("\(droppedBookmarks) bookmark(s)") }
        if droppedStatistics > 0 { dropped.append("\(droppedStatistics) statistics entr" + (droppedStatistics == 1 ? "y" : "ies")) }
        if !dropped.isEmpty { lines.append("Not sent to the previous Mac and dropped: " + dropped.joined(separator: ", ") + ".") }
        return lines.joined(separator: " ")
    }
}

public struct TransferRecord: Codable, Identifiable {
    public var id: String
    public var assetId: String
    public var part: AudioPart
    public var status: String
    public var resumeData: Data?
    public var message: String?
    public var receivedBytes: Int64?
    public var resumeIdentity: String?
    public func canResume(identity: String) -> Bool { resumeData != nil && resumeIdentity == identity }
    public init(assetId: String, part: AudioPart) {
        id = part.resourceId; self.assetId = assetId; self.part = part; status = "queued"
    }
}
