import Foundation
import AVFoundation
import Combine
import UIKit
import PudgeCore

struct CatalogResponse: Decodable { let protocolMajor: Int; let serverId: String; let items: [AudioManifest]; let errors: [CatalogError]?; let preparing: Bool? }
struct CatalogError: Decodable { let title: String; let error: String }
struct PairRequest: Codable { let invitation: String; let deviceId: String; let attemptId: String; let name: String }
struct PendingPair: Codable { let payload: PairingPayload; let request: PairRequest }
/// "pending" with the code to compare with the Mac's Allow/Deny prompt, then the token once allowed.
struct PairReceipt: Decodable { let deviceId: String; let serverId: String; let token: String?; let status: String?; let confirmationCode: String? }
struct CheckpointReceipt: Decodable { let checkpointId: String; let revision: Int64; let status: String }
struct BookmarkReceipt: Decodable { let eventId: String; let revision: Int64 }
struct BookmarkEnvelope: Decodable { let items: [Bookmark] }
struct ArchivePlan: Decodable { let transferId: String; let offsets: [String: Int64] }
struct CommitReceipt: Decodable { let receiptId: String; let retained: Bool }
struct ChunkReceipt: Decodable { let offset: Int64 }
struct HandoffRequest: Codable { let operationId: String; let assetId: String; let expectedRevision: Int64; let sourceSessionId: String?; let targetReady: Bool }
struct HandoffResult: Decodable { let operationId: String; let status: String; let positionMs: Int64?; let revision: Int64? }
struct Commands: Decodable { let items: [HandoffRequest] }
struct HandoffAck: Codable { let operationId: String; let checkpointId: String }
/// `sentAt` (this phone's clock) lets the Mac correct a clock that runs ahead.
struct ActivityBatch: Encodable { let items: [ActivityInterval]; let sentAt: Double }
struct ActivityReceipt: Decodable { let id: String; let status: String; let reason: String? }
/// Pairing a Mac other than the one this phone's data belongs to (a new Mac, Pudge reinstalled).
struct MacSwitchOffer: Identifiable { let id = UUID(); let text: String; let serverName: String; let summary: MacSwitchSummary }
struct ActivityResults: Decodable { let results: [ActivityReceipt] }

@MainActor
final class PudgeModel: ObservableObject {
    let store: LocalStore
    let player: AudioPlayerService
    /// Reading and watching time for the Mac's Statistics.
    let activity: ActivityRecorder
    let deviceId: String
    let discovery = MacDiscovery()
    @Published var books: [AudioManifest] = []
    @Published var transfers: [TransferRecord] = []
    @Published var continueFallback: AudioManifest?
    private var catalogRetryScheduled = false
    private var readAlongCache: [String: ReadAlongDocument] = [:]
    @Published var refreshing = false
    @Published var lastRefresh: Date?
    @Published var paired: PairedMac?
    @Published var macAvailable = false {
        didSet {
            if macAvailable && !oldValue && macChecked { reconnected() }
            macChecked = true
        }
    }
    /// The Mac's answer (or its absence) was seen at least once since launch.
    private(set) var macChecked = false
    /// Paired, but the Mac did not answer the last check: downloaded media work with the
    /// data saved at the last update; grades and other Mac actions wait.
    var macOffline: Bool { paired != nil && macChecked && !macAvailable }
    private var probe: Task<Bool, Never>?
    private var probedAt = -100.0
    @Published var busy = false
    /// What the paired Mac's Pudge supports (/api/v2/capabilities), kept for offline use.
    @Published var macCapabilities = Set(UserDefaults.standard.stringArray(forKey: "pudge.macCapabilities") ?? [])
    func macSupports(_ capability: String) -> Bool { macCapabilities.contains(capability) }
    @Published var message: String? { didSet { if let message { AppLog.write("ui", message) } } }
    @Published var progress: [String: ProgressState] = [:]
    @Published var bookmarks: [String: [Bookmark]] = [:]
    @Published var archiving = Set<String>()
    @Published var lastArchive: [String: Int64] = [:]
    @Published var syncIssue: String? { didSet { if let syncIssue { AppLog.write("sync", syncIssue) } } }
    @Published var lastPlayed: [String: Double] = (UserDefaults.standard.dictionary(forKey: "pudge.lastPlayed") as? [String: Double]) ?? [:]
    /// While pairing waits for Allow on the Mac: the code both devices show.
    @Published var pairingCode: String?
    private var pairingCancelled = false
    /// "Switch to this Mac?" before re-binding the local data to another Mac.
    @Published var switchOffer: MacSwitchOffer?
    private var connection: MacConnection?
    /// Current verified Mac connection (reading, video and study use it too).
    var macConnection: MacConnection? { connection }
    /// Reading, video, study and statistics (shares this connection).
    private(set) lazy var media: MediaModel = MediaModel(app: self)
    private var transferManager: TransferManager?
    private var syncing = false
    private var lastSend = 0.0
    private var maintenance: Task<Void, Never>?

    init() throws {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true).appendingPathComponent("PudgeAudio", isDirectory: true)
        store = try LocalStore.open(at: directory)
        activity = ActivityRecorder(store: store)
        if let existing = try KeychainStore.load("device-id", as: String.self) { deviceId = existing }
        else { deviceId = UUID().uuidString.lowercased(); try KeychainStore.save("device-id", deviceId) }
        player = AudioPlayerService(store: store, deviceId: deviceId)
        paired = try KeychainStore.load("paired-mac", as: PairedMac.self)
        reloadLocal()
        if let paired { try configure(paired) }
        player.onCheckpoint = { [weak self] reason in
            guard let self else { return }
            if reason != "periodic" || ProcessInfo.processInfo.systemUptime - self.lastSend >= 15 {
                Task { await self.sync() }
            }
        }
        maintenance = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                guard let self else { return }
                await self.sync()
            }
        }
    }
    func reloadLocal() {
        do {
            books = try store.manifests(); transfers = try store.transfers()
            for book in books { bookmarks[book.id] = try store.bookmarks(assetId: book.assetId).filter { $0.editionId == book.editionId } }
        } catch { message = error.localizedDescription }
    }
    func syncStatus(_ book: AudioManifest) -> String {
        guard let server = paired?.serverId, (try? store.isRemote(book, serverId: server)) == true else { return "Only on \(PudgeTheme.device)" }
        let status = SynchronizationStatus.evaluate(pending: (try? store.hasPending(assetId: book.assetId)) ?? true,
            checkpointId: try? store.latestCheckpointId(book), progress: progress[book.assetId])
        switch status {
        case .pending: return "Changes waiting to be sent"
        case .conflict: return "Choose where to continue"
        case .synced: return "Position synced"
        case .awaiting: return "Waiting for position confirmation"
        }
    }
    private func configure(_ mac: PairedMac) throws {
        try store.bindMac(serverId: mac.serverId)
        let client = try MacConnection(mac)
        connection = client
        if let manager = transferManager, manager.pin == mac.certificateSHA256 { manager.updateConnection(client) }
        else { transferManager?.onChange = nil; transferManager?.cancelAll(); transferManager = try TransferManager(store: store, connection: client) }
        transferManager?.onChange = { [weak self] in self?.reloadLocal() }
        discovery.onAddress = { [weak self] address in
            guard let self, var mac = self.paired, mac.baseURL != address else { return }
            mac.baseURL = address
            Task {
                do {
                    let client = try MacConnection(mac)
                    struct Identity: Decodable { let serverId: String; let protocolMajor: Int }
                    let info = try await client.send("/api/v2/capabilities", as: Identity.self)
                    guard info.serverId == mac.serverId && info.protocolMajor == 2 else { throw PudgeError.invalid("A different Mac was found") }
                    try KeychainStore.save("paired-mac", mac); self.paired = mac; self.connection = client
                    // Existing background tasks keep their original request.
                    // Retries use the newly verified endpoint after recovery.
                    self.transferManager?.updateConnection(client)
                    self.transferManager?.onChange = { [weak self] in self?.reloadLocal() }
                    await self.refresh()
                } catch { self.macAvailable = false }
            }
        }
        discovery.start(serverId: mac.serverId)
    }
    func pair(_ text: String) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        do {
            let payload = try PairingPayload.parse(text)
            if let bound = try store.boundMac(), bound != payload.serverId {
                // Another Mac (a new one, or Pudge reinstalled): ask before moving the data over.
                switchOffer = MacSwitchOffer(text: text, serverName: payload.serverName, summary: try store.macSwitchSummary())
                return
            }
            let mac = try await pairWithMac(payload)
            try KeychainStore.save("paired-mac", mac)
            paired = mac; try configure(mac); message = nil; await refresh()
        } catch { message = error.localizedDescription }
    }
    /// "Switch to this Mac": unsent changes go to the previous Mac first when it answers. Then
    /// the data is bound to the new Mac (MacSwitchSummary: books stay, the last place moves to
    /// the same audio, the rest is dropped and counted).
    func switchMac(_ offer: MacSwitchOffer) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        switchOffer = nil
        do {
            let payload = try PairingPayload.parse(offer.text)
            if connection != nil, await reachMac() {
                await sync()
                AppLog.write("pairing", "sent unsent changes to the previous Mac before switching")
            }
            let mac = try await pairWithMac(payload)
            let summary = try store.rebindMac(to: payload.serverId)
            AppLog.write("pairing", "switched Mac: phone books \(summary.phoneBooks), moved places \(summary.carriedPositions), dropped positions \(summary.droppedPositions), bookmarks \(summary.droppedBookmarks), statistics \(summary.droppedStatistics)")
            try KeychainStore.save("paired-mac", mac)
            macAvailable = false; progress = [:]
            paired = mac; try configure(mac)
            message = "Switched to \(mac.serverName). " + summary.text(device: PudgeTheme.device)
            await refresh()
        } catch { message = error.localizedDescription }
    }
    /// Pairs with the Mac of an invitation: the first of its addresses that answers as that Mac,
    /// then waits (showing the confirmation code) until the Mac's user allows this device.
    private func pairWithMac(_ payload: PairingPayload) async throws -> PairedMac {
        let previous = try KeychainStore.load("pending-pair", as: PendingPair.self)
        let request = previous?.payload.invitation == payload.invitation ? previous!.request : PairRequest(invitation: payload.invitation, deviceId: deviceId, attemptId: UUID().uuidString.lowercased(), name: UIDevice.current.name)
        try KeychainStore.save("pending-pair", PendingPair(payload: payload, request: request))
        var mac = try await reachableMac(payload)
        let client = try MacConnection(mac)
        pairingCancelled = false
        defer { pairingCode = nil }
        while true {
            let result = try await client.post("/api/v2/pair", value: request, as: PairReceipt.self)
            guard result.serverId == payload.serverId && result.deviceId == deviceId else { throw PudgeError.invalid("The reply belongs to another device") }
            if let token = result.token, !token.isEmpty { mac.token = token; break }
            guard result.status == "pending", let code = result.confirmationCode else { throw PudgeError.invalid("The Mac did not accept the pairing") }
            pairingCode = code
            try await Task.sleep(nanoseconds: 2_000_000_000)
            if pairingCancelled { throw PudgeError.invalid("Pairing cancelled") }
        }
        try KeychainStore.delete("pending-pair")
        return mac
    }
    func cancelPairing() { pairingCancelled = true }
    /// The invitation lists the Mac's LAN addresses (Wi-Fi, Ethernet ...), best first.
    private func reachableMac(_ payload: PairingPayload) async throws -> PairedMac {
        for address in payload.candidates {
            let mac = PairedMac(deviceId: deviceId, serverId: payload.serverId, serverName: payload.serverName,
                                baseURL: address, certificateSHA256: payload.certificateSHA256, token: "")
            let client = try MacConnection(mac)
            if await client.answers() { return mac }
            AppLog.write("pairing", "no answer at \(address)")
        }
        throw PudgeError.invalid("The Mac did not answer at " + payload.candidates.joined(separator: ", ") + ". Check that this \(PudgeTheme.device) and the Mac are on the same network.")
    }
    func disconnect() {
        do { try KeychainStore.delete("paired-mac"); discovery.stop(); transferManager?.onChange = nil; transferManager?.cancelAll(); transferManager = nil; connection = nil; paired = nil; macAvailable = false }
        catch { message = error.localizedDescription }
    }
    func changeAddress(_ address: String) async {
        guard var mac = paired else { return }
        do {
            guard let url = URL(string: address), url.scheme == "https", url.host != nil,
                  url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else {
                throw PudgeError.invalid("Enter the Mac's HTTPS address without login, parameters or fragment")
            }
            mac.baseURL = address
            let client = try MacConnection(mac)
            struct Identity: Decodable { let serverId: String }
            let identity = try await client.send("/api/v2/capabilities", as: Identity.self)
            guard identity.serverId == mac.serverId else { throw PudgeError.invalid("A different Mac") }
            try KeychainStore.save("paired-mac", mac); paired = mac; connection = client
            transferManager?.updateConnection(client)
            await refresh()
            message = "Mac address updated"
        } catch { message = error.localizedDescription }
    }
    /// Whether the paired Mac answers now (a few seconds at most, shared by callers for 5 s).
    /// No answer marks the Mac unavailable; it becomes available again only through an
    /// authorised request (catalog or sync), so a revoked device never looks connected.
    func reachMac() async -> Bool {
        guard let connection else { return false }
        if let probe, ProcessInfo.processInfo.systemUptime - probedAt < 5 { return await probe.value }
        probedAt = ProcessInfo.processInfo.systemUptime
        let task = Task { await connection.answers() }
        probe = task
        let answered = await task.value
        if !answered && (macAvailable || !macChecked) {
            AppLog.write("sync", "Mac unavailable")
            macAvailable = false
        }
        return answered
    }
    /// The Mac answers again after being unavailable: everything refreshes as usual.
    private func reconnected() {
        AppLog.write("sync", "Mac available again")
        Task {
            if !refreshing { await refresh() }
            await media.reconnected()
        }
    }
    func refresh() async {
        guard let connection else { reloadLocal(); return }
        refreshing = true
        defer { refreshing = false; lastRefresh = Date() }
        // Away from the Mac the library shows what is on this iPhone without waiting.
        guard await reachMac() else { reloadLocal(); return }
        do {
            let catalog = try await connection.send("/api/v2/catalog", as: CatalogResponse.self)
            AppLog.write("catalog", "\(catalog.items.count) books, preparing=\(catalog.preparing ?? false), errors=\(catalog.errors?.count ?? 0)")
            guard catalog.protocolMajor == 2 && catalog.serverId == paired?.serverId else { throw PudgeError.invalid("Unsupported server") }
            struct Capabilities: Decodable { let capabilities: [String]? }
            if let answer = try? await connection.send("/api/v2/capabilities", as: Capabilities.self) {
                macCapabilities = Set(answer.capabilities ?? [])
                UserDefaults.standard.set(Array(macCapabilities).sorted(), forKey: "pudge.macCapabilities")
            }
            for book in catalog.items { try store.saveManifest(book); try store.markRemote(book, serverId: catalog.serverId) }
            try moveCarriedPlaces(catalog)
            macAvailable = true; reloadLocal()
            if let errors = catalog.errors, !errors.isEmpty { message = "Could not prepare: " + errors.map(\.title).joined(separator: ", ") }
            await prefetchCovers(catalog.items, connection: connection)
            for book in books where book.lightNovelId != nil && (try? store.ready(book)) == true {
                media.downloadLinkedNovel(book.lightNovelId!, title: book.seriesTitle ?? book.title)
            }
            // Downloaded light novels and manga: changed on the Mac? (at most every 6 hours)
            Task { await media.checkReadingUpdates() }
            await sync()
            if catalog.preparing == true, !catalogRetryScheduled {
                // The Mac is still preparing books/covers: pick them up shortly.
                catalogRetryScheduled = true
                Task { try? await Task.sleep(nanoseconds: 5_000_000_000); catalogRetryScheduled = false; await refresh() }
            }
        } catch { macAvailable = false; message = error.localizedDescription; reloadLocal() }
    }
    /// After switching Macs: the last unsent place of each book goes to the same audio on this Mac;
    /// once the catalog is complete, places without a match are dropped (counted).
    private func moveCarriedPlaces(_ catalog: CatalogResponse) throws {
        guard try store.carriedCount() > 0 else { return }
        var moved = 0
        for book in catalog.items {
            if try store.adoptCarried(book) { moved += 1 }
        }
        var dropped = 0
        if catalog.preparing != true { dropped = try store.dropCarried() }
        if moved + dropped > 0 {
            message = "Last place of \(moved) audiobook(s) moved to this Mac" + (dropped > 0 ? "; \(dropped) had no matching audiobook here and were dropped" : "")
            AppLog.write("pairing", message ?? "")
        }
    }
    /// Covers are small: fetch them for every catalog book, not only downloaded ones.
    private func prefetchCovers(_ items: [AudioManifest], connection: MacConnection) async {
        let folder = store.directory.appendingPathComponent("resources", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        var changed = false
        for cover in items.flatMap({ [$0.cover, $0.readAlong].compactMap { $0 } }) {
            if let local = try? store.resource(cover.resourceId), local.state.verified, local.state.sha256 == cover.sha256,
               FileManager.default.fileExists(atPath: local.url.path) { continue }
            do {
                let data = try await connection.bytes("/api/v2/resources/\(cover.resourceId)/content?sha256=\(cover.sha256)")
                let ext = (cover.fileName as NSString).pathExtension
                let target = folder.appendingPathComponent(cover.resourceId + "." + ext)
                try data.write(to: target, options: .atomic)
                try store.markVerified(cover.downloadPart, at: target)
                changed = true
            } catch { continue }  // A missing cover/text never blocks the library.
        }
        if changed { reloadLocal() }
    }
    func sync() async {
        guard let connection, !syncing else { return }
        syncing = true; lastSend = ProcessInfo.processInfo.systemUptime; defer { syncing = false; reloadLocal() }
        syncIssue = nil
        // While the Mac is unavailable a quick check comes first (no long timeouts).
        if !macAvailable, !(await reachMac()) { return }
        do {
            for checkpoint in try store.pendingCheckpoints() {
                guard let book = books.first(where: { $0.assetId == checkpoint.assetId && $0.editionId == checkpoint.editionId }),
                      let server = paired?.serverId, try store.isRemote(book, serverId: server) else { continue }
                let receipt = try await connection.post("/api/v2/checkpoints", value: checkpoint, as: CheckpointReceipt.self)
                guard receipt.checkpointId == checkpoint.checkpointId else { throw PudgeError.invalid("Invalid position receipt") }
                try store.acknowledge(checkpoint.checkpointId, revision: receipt.revision)
            }
            for bookmark in try store.pendingBookmarks() {
                guard let book = books.first(where: { $0.assetId == bookmark.assetId && $0.editionId == bookmark.editionId }),
                      let server = paired?.serverId, try store.isRemote(book, serverId: server) else { continue }
                let receipt = try await connection.post("/api/v2/bookmarks", value: bookmark, as: BookmarkReceipt.self)
                guard receipt.eventId == bookmark.eventId else { throw PudgeError.invalid("Invalid bookmark receipt") }
                try store.acknowledgeBookmark(bookmark.eventId)
            }
            for book in books {
                guard let server = paired?.serverId, try store.isRemote(book, serverId: server) else { continue }
                let value = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
                progress[book.assetId] = value
                if player.book?.assetId != book.assetId { try store.adopt(value, book: book) }
                let marks = try await connection.send("/api/v2/assets/\(book.assetId)/bookmarks", as: BookmarkEnvelope.self)
                try store.mergeBookmarks(marks.items)
            }
            let commands = try await connection.send("/api/v2/commands", as: Commands.self)
            for command in commands.items {
                if (try store.handoffCheckpoint(operationId: command.operationId)) != nil || (player.book?.assetId == command.assetId && player.sessionId == command.sourceSessionId) {
                    let checkpoint = try player.pauseForHandoff(operationId: command.operationId, assetId: command.assetId, sourceSessionId: command.sourceSessionId)
                    let receipt = try await connection.post("/api/v2/checkpoints", value: checkpoint, as: CheckpointReceipt.self)
                    guard receipt.checkpointId == checkpoint.checkpointId && receipt.status == "accepted" else { throw PudgeError.invalid("The Mac did not confirm the handoff position") }
                    try store.acknowledge(checkpoint.checkpointId, revision: receipt.revision)
                    _ = try await connection.post("/api/v2/handoff/ack", value: HandoffAck(operationId: command.operationId, checkpointId: checkpoint.checkpointId), as: HandoffResult.self)
                }
            }
            macAvailable = true
        } catch { macAvailable = false; syncIssue = error.localizedDescription }
        if macAvailable { await sendActivity(connection) }
    }
    /// Statistics: time read and watched here stays on the phone until the Mac answers for it.
    private func sendActivity(_ connection: MacConnection) async {
        do {
            try store.pruneActivity(olderThan: Date().addingTimeInterval(-30 * 86_400))
            for _ in 0..<10 {
                let batch = try store.pendingActivity(limit: 200)
                guard !batch.isEmpty else { return }
                let answer = try await connection.post("/api/v2/activity", value: ActivityBatch(items: batch, sentAt: Date().timeIntervalSince1970), as: ActivityResults.self)
                let sent = Set(batch.map(\.id))
                let answered = answer.results.filter { sent.contains($0.id) }
                // Not taken by the Mac: kept with its reason (Settings → Diagnostics), not deleted.
                var rejected: [String: String] = [:]
                for result in answered where result.status == "rejected" { rejected[result.id] = result.reason ?? "Not accepted by the Mac" }
                try store.rejectActivity(rejected)
                try store.acknowledgeActivity(answered.filter { $0.status != "rejected" }.map(\.id))
                if !rejected.isEmpty { AppLog.write("stats", "\(rejected.count) intervals not taken: " + Set(rejected.values).sorted().joined(separator: "; ")) }
                if answered.count < batch.count { return }
            }
        } catch { AppLog.write("stats", "activity not sent: \(error.localizedDescription)") }
    }
    func download(_ book: AudioManifest) {
        do {
            guard let manager = transferManager else { throw PudgeError.invalid("Connect a Mac") }
            try manager.download(book)
            // Its light novel comes along, so reading along works without the Mac.
            if let novel = book.lightNovelId { media.downloadLinkedNovel(novel, title: book.seriesTitle ?? book.title) }
        }
        catch { message = error.localizedDescription }
        reloadLocal()
    }
    func retry(_ record: TransferRecord) { do { try transferManager?.retry(record.id) } catch { message = error.localizedDescription }; reloadLocal() }
    func pauseDownload(_ record: TransferRecord) { transferManager?.pause(record.id) }
    func play(_ book: AudioManifest, position: Int64? = nil) async {
        do { try await player.play(book, position: position); markPlayed(book) } catch { message = error.localizedDescription }
    }
    /// Library → Mark unfinished: clears Finished on every device; the place stays.
    /// Only for a Mac that knows the "unfinish" checkpoint (an older one would refuse it).
    func markUnfinished(_ book: AudioManifest) {
        guard macSupports("progress-unfinish") else { return }
        if player.book?.assetId == book.assetId { player.save("unfinish"); return }
        do {
            var position = try store.localPosition(assetId: book.assetId, editionId: book.editionId)
            if let saved = progress[book.assetId]?.selected, saved.editionId == book.editionId { position = saved.positionMs }
            let revision = try store.revision(assetId: book.assetId)
            let checkpoint = try AudioCheckpoint.make(book: book, deviceId: deviceId, sessionId: UUID().uuidString.lowercased(),
                                                      sequence: 1, revision: revision,
                                                      positionMs: min(max(0, position), book.durationMs), reason: "unfinish")
            try store.enqueue(checkpoint); reloadLocal(); Task { await sync() }
        } catch { message = error.localizedDescription }
    }
    /// Read-along text of a book, loaded once per downloaded resource.
    func readAlong(_ book: AudioManifest) -> ReadAlongDocument? {
        guard let resource = book.readAlong, let local = try? store.resource(resource.resourceId), local.state.verified else { return nil }
        if let cached = readAlongCache[resource.resourceId] { return cached }
        guard let document = try? ReadAlongDocument.load(local.url) else { return nil }
        readAlongCache[resource.resourceId] = document
        return document
    }
    func markPlayed(_ book: AudioManifest) {
        lastPlayed[book.assetId] = Date().timeIntervalSince1970
        UserDefaults.standard.set(lastPlayed, forKey: "pudge.lastPlayed")
    }
    /// Rejected statistics back into the queue (e.g. after updating Pudge on the Mac).
    func retryRejectedStatistics() {
        do { try store.retryRejectedActivity(); Task { await sync() } } catch { message = error.localizedDescription }
    }
    func pauseAllDownloads() { for record in transfers where record.status == "running" || record.status == "queued" { transferManager?.pause(record.id) } }
    func resumeAllDownloads() { for record in transfers where record.status == "paused" || record.status == "failed" { retry(record) } }
    func addBookmark(_ book: AudioManifest, title: String) {
        do { try store.enqueueBookmark(Bookmark(book: book, positionMs: player.positionMs, title: title)); reloadLocal(); Task { await sync() } }
        catch { message = error.localizedDescription }
    }
    func deleteBookmark(_ bookmark: Bookmark) {
        do { var value = bookmark; value.deleted = true; value.eventId = UUID().uuidString.lowercased(); try store.enqueueBookmark(value); reloadLocal(); Task { await sync() } }
        catch { message = error.localizedDescription }
    }
    func pin(_ book: AudioManifest, value: Bool) {
        do { for part in book.resources { try store.setProtection(resourceId: part.resourceId, pinned: value) }; objectWillChange.send() }
        catch { message = error.localizedDescription }
    }
    func archive(_ book: AudioManifest) async throws {
        guard let connection, try store.ready(book) else { throw PudgeError.invalid("Local files and a connected Mac are required") }
        archiving.insert(book.id); defer { archiving.remove(book.id) }
        let plan = try await connection.post("/api/v2/archives", value: book, as: ArchivePlan.self)
        for part in book.resources {
            guard let local = try store.resource(part.resourceId) else { throw PudgeError.invalid("File unavailable") }
            try FileIntegrity.verify(local.url, sha256: part.sha256, sizeBytes: part.sizeBytes)
            let file = try FileHandle(forReadingFrom: local.url); defer { try? file.close() }
            var offset = plan.offsets[part.resourceId] ?? 0
            guard offset >= 0 && offset <= part.sizeBytes else { throw PudgeError.invalid("Invalid archive offset") }
            try file.seek(toOffset: UInt64(offset))
            while offset < part.sizeBytes {
                guard let chunk = try file.read(upToCount: 1024 * 1024), !chunk.isEmpty else { throw PudgeError.invalid("The file changed") }
                var request = try connection.request("/api/v2/archives/\(plan.transferId)/resources/\(part.resourceId)?offset=\(offset)", method: "PUT", data: chunk)
                request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
                let (data, response) = try await connection.session.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw PudgeError.invalid("Transfer interrupted; a retry continues the archive") }
                let result = try JSONDecoder().decode(ChunkReceipt.self, from: data)
                guard result.offset == offset + Int64(chunk.count) else { throw PudgeError.invalid("The Mac confirmed a different offset") }
                offset = result.offset
            }
        }
        let receipt = try await connection.post("/api/v2/archives/commit", value: ["transferId": plan.transferId], as: CommitReceipt.self)
        guard receipt.retained, let server = paired?.serverId else { throw PudgeError.invalid("The Mac did not confirm the archive") }
        try store.markRemote(book, serverId: server)
        lastArchive[book.id] = Int64(Date().timeIntervalSince1970 * 1000)
    }
    func offload(_ book: AudioManifest) async {
        do {
            guard player.book?.assetId != book.assetId else { throw PudgeError.invalid("Close this book's player before freeing space") }
            if let server = paired?.serverId, try !store.isRemote(book, serverId: server) { try await archive(book) }
            await sync()
            guard try !store.hasPending(assetId: book.assetId), let connection else { throw PudgeError.invalid("Send changes to the Mac first") }
            for part in book.resources {
                guard let copy = try store.resource(part.resourceId), copy.state.verified else { continue }
                guard !copy.state.pinned else { throw PudgeError.invalid("The book is kept on this \(PudgeTheme.device)") }
                if (try? await connection.post("/api/v2/archives/verify", value: ["resourceId": part.resourceId, "editionId": book.editionId], as: ArchiveReceipt.self)) == nil { try await archive(book) }
                let receipt = try await connection.post("/api/v2/archives/verify", value: ["resourceId": part.resourceId, "editionId": book.editionId], as: ArchiveReceipt.self)
                let observed = Int64(Date().timeIntervalSince1970 * 1000)
                try store.evict(resourceId: part.resourceId, expectedGeneration: copy.state.generation, receipt: receipt, nowMs: observed, verifiedLocallyAt: observed)
            }
            reloadLocal()
        } catch { message = error.localizedDescription; reloadLocal() }
    }
    func resolve(_ book: AudioManifest, checkpointId: String) async {
        do {
            guard let connection, let state = progress[book.assetId] else { return }
            if player.book?.assetId == book.assetId { player.stop(); await sync() }
            let latest = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
            struct Resolution: Encodable { let assetId: String; let expectedRevision: Int64; let checkpointId: String }
            struct Receipt: Decodable { let revision: Int64 }
            guard latest.revision == state.revision else { progress[book.assetId] = latest; throw PudgeError.invalid("The position changed; choose again") }
            _ = try await connection.post("/api/v2/progress/resolve", value: Resolution(assetId: book.assetId, expectedRevision: latest.revision, checkpointId: checkpointId), as: Receipt.self)
            let selected = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
            progress[book.assetId] = selected; try store.adopt(selected, book: book)
        } catch { message = error.localizedDescription }
    }
    /// The single "Continue" action: sync first, then either take over from the Mac or
    /// another iPhone/iPad playing this book (it pauses and hands over its exact place) or
    /// play locally from the newest known position.
    func continueListening(_ book: AudioManifest) async {
        if !macOffline { await sync() }
        if connection != nil, macAvailable, let playing = progress[book.assetId]?.playingOn, playing != deviceId {
            if await handoff(book, quiet: true) { return }
            continueFallback = book
            return
        }
        await play(book)
    }
    @discardableResult
    func handoff(_ book: AudioManifest, quiet: Bool = false) async -> Bool {
        guard !busy else { return false }; busy = true; defer { busy = false }
        do {
            guard let connection, try store.ready(book) else { throw PudgeError.invalid("Download the book first") }
            let state = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
            let previous = try KeychainStore.load("handoff-intent", as: HandoffRequest.self)
            let request = previous?.assetId == book.assetId ? previous! : HandoffRequest(operationId: UUID().uuidString.lowercased(), assetId: book.assetId, expectedRevision: state.revision, sourceSessionId: state.selected?.playbackSessionId, targetReady: true)
            try KeychainStore.save("handoff-intent", request)
            let source = state.playingOnName ?? "The other device"
            var result = try await connection.post("/api/v2/handoff", value: request, as: HandoffResult.self)
            // Another iPhone/iPad pauses on its next sync (up to ~15 s): wait for its answer.
            var waited = 0
            while result.status == "pending" && waited < 15 {
                try await Task.sleep(nanoseconds: 2_000_000_000); waited += 1
                result = try await connection.post("/api/v2/handoff", value: request, as: HandoffResult.self)
            }
            guard result.status == "source_paused", let position = result.positionMs else { try KeychainStore.delete("handoff-intent"); throw PudgeError.invalid("\(source) did not confirm the pause. You can continue from the last saved position instead.") }
            let finalState = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
            guard finalState.revision == result.revision else { try KeychainStore.delete("handoff-intent"); throw PudgeError.invalid("The source already changed its position; try the handoff again") }
            try store.adopt(finalState, book: book)
            try await player.play(book, position: position); try KeychainStore.delete("handoff-intent")
            markPlayed(book); AppLog.write("handoff", "took over \(book.title) from \(source) at \(position) ms")
            return true
        } catch {
            AppLog.write("handoff", "failed: " + error.localizedDescription)
            if !quiet { message = error.localizedDescription }; return false
        }
    }
    func importAudio(_ urls: [URL]) async {
        guard !busy else { return }; busy = true; defer { busy = false }
        do {
            var parts: [AudioPart] = []; var offset: Int64 = 0
            var chapters: [AudioChapter] = []; var cover: CoverResource?
            let folder = store.directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            for url in urls {
                let access = url.startAccessingSecurityScopedResource(); defer { if access { url.stopAccessingSecurityScopedResource() } }
                let source = AVURLAsset(url: url)
                guard try await source.load(.isPlayable) else { throw PudgeError.invalid("This \(PudgeTheme.device) does not support this format. Import the file on the Mac to prepare it.") }
                let duration = try await source.load(.duration)
                for group in source.chapterMetadataGroups(bestMatchingPreferredLanguages: Locale.preferredLanguages) {
                    let title = group.items.first(where: { $0.commonKey == .commonKeyTitle })?.stringValue ?? "Chapter"
                    let start = group.timeRange.start.seconds; let end = CMTimeRangeGetEnd(group.timeRange).seconds
                    if start.isFinite && end.isFinite && start >= 0 && end >= start {
                        chapters.append(AudioChapter(title: title, startMs: offset + Int64((start * 1000).rounded()), endMs: offset + Int64((end * 1000).rounded())))
                    }
                }
                if cover == nil {
                    let metadata = try await source.load(.commonMetadata)
                    if let artwork = metadata.first(where: { $0.commonKey == .commonKeyArtwork }),
                       let data = try await artwork.load(.dataValue), data.count <= 10 * 1024 * 1024,
                       let image = UIImage(data: data), let jpeg = image.jpegData(compressionQuality: 0.9) {
                        let coverId = UUID().uuidString.lowercased(); let coverURL = folder.appendingPathComponent(coverId + ".jpg")
                        try jpeg.write(to: coverURL, options: .atomic)
                        cover = CoverResource(resourceId: coverId, sha256: try FileIntegrity.sha256(coverURL), sizeBytes: Int64(jpeg.count), fileName: "cover.jpg")
                        try store.markVerified(cover!.downloadPart, at: coverURL, original: true)
                    }
                }
                guard duration.seconds.isFinite && duration.seconds > 0 else { throw PudgeError.invalid("Could not determine the duration") }
                let id = UUID().uuidString.lowercased()
                var destination = folder.appendingPathComponent(id + "." + url.pathExtension.lowercased())
                try FileManager.default.copyItem(at: url, to: destination)
                try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: destination.path)
                var policy = URLResourceValues(); policy.isExcludedFromBackup = false; try destination.setResourceValues(policy)
                let sha = try FileIntegrity.sha256(destination)
                let size = Int64(try destination.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0)
                let part = AudioPart(partId: UUID().uuidString.lowercased(), resourceId: id, sha256: sha, sizeBytes: size,
                                     fileName: url.lastPathComponent, offsetMs: offset, durationMs: Int64((duration.seconds * 1000).rounded()), playbackDurationMs: nil)
                try store.markVerified(part, at: destination, original: true); parts.append(part); offset += part.durationMs
            }
            let book = AudioManifest(protocolMajor: 2, assetId: UUID().uuidString.lowercased(), editionId: UUID().uuidString.lowercased(),
                                     timelineRevision: UUID().uuidString.lowercased(), title: urls.first?.deletingPathExtension().lastPathComponent ?? "Audiobook",
                                     durationMs: offset, parts: parts, chapters: chapters, cover: cover)
            try store.saveManifest(book); reloadLocal()
        } catch { message = error.localizedDescription }
    }
}


/// Small rotating text log for "Export diagnostics" (no tokens, no file contents).
enum AppLog {
    private static let queue = DispatchQueue(label: "pudge.applog")
    static var folder: URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("PudgeAudio/logs", isDirectory: true)
    }
    static func write(_ category: String, _ text: String) {
        let line = ISO8601DateFormatter().string(from: Date()) + " [" + category + "] " + text + "\n"
        queue.async {
            let file = folder.appendingPathComponent("app.log")
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            if let size = try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 1_000_000 {
                let old = folder.appendingPathComponent("app.1.log")
                try? FileManager.default.removeItem(at: old)
                try? FileManager.default.moveItem(at: file, to: old)
            }
            if let handle = try? FileHandle(forWritingTo: file) {
                _ = try? handle.seekToEnd(); try? handle.write(contentsOf: Data(line.utf8)); try? handle.close()
            } else {
                try? Data(line.utf8).write(to: file)
            }
        }
    }
    static func contents() -> String {
        queue.sync {
            ["app.1.log", "app.log"].compactMap { try? String(contentsOf: folder.appendingPathComponent($0), encoding: .utf8) }.joined()
        }
    }
}

struct StorageUsage {
    var audio: Int64 = 0
    var covers: Int64 = 0
    var texts: Int64 = 0
    var words: Int64 = 0
    var temporary: Int64 = 0
    var database: Int64 = 0
    var logs: Int64 = 0
    var free: Int64 = 0
    var total: Int64 { audio + covers + texts + words + temporary + database + logs }
}

extension PudgeModel {
    /// What Pudge keeps on this iPhone, measured from the files themselves.
    func storageUsage() -> StorageUsage {
        var usage = StorageUsage()
        let manager = FileManager.default
        if let values = try? store.directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
           let free = values.volumeAvailableCapacityForImportantUsage { usage.free = free }
        guard let files = manager.enumerator(at: store.directory, includingPropertiesForKeys: [.fileSizeKey, .isRegularFileKey]) else { return usage }
        for case let url as URL in files {
            guard let values = try? url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey]), values.isRegularFile == true else { continue }
            let size = Int64(values.fileSize ?? 0)
            let ext = url.pathExtension.lowercased()
            if url.path.contains("/video/") || url.path.contains("/reading/") { continue }  // shown as Episodes and Reading
            if url.path.contains("/image-cache/") { usage.covers += size; continue }
            if url.lastPathComponent == MediaModel.offlineWordsFile { usage.words += size }
            else if url.path.contains("/logs/") { usage.logs += size }
            else if url.lastPathComponent.hasPrefix("state.sqlite") { usage.database += size }
            else if ext == "partial" || ext == "tmp" { usage.temporary += size }
            else if ["jpg", "jpeg", "png"].contains(ext) { usage.covers += size }
            else if ext == "json" { usage.texts += size }
            else { usage.audio += size }
        }
        return usage
    }
    /// Downloaded bytes of one book (verified local parts only).
    func downloadedBytes(_ book: AudioManifest) -> Int64 {
        book.parts.reduce(Int64(0)) { sum, part in
            guard let local = try? store.resource(part.resourceId), local.state.verified,
                  FileManager.default.fileExists(atPath: local.url.path) else { return sum }
            return sum + part.sizeBytes
        }
    }
    /// Leftover partial files of interrupted transfers (never verified data).
    func deleteTemporaryFiles() {
        if transfers.contains(where: { ["queued", "running", "verifying"].contains($0.status) }) {
            message = "Wait until active downloads finish"; return
        }
        let folder = store.directory.appendingPathComponent("resources", isDirectory: true)
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: folder.path) else { return }
        for name in names where name.hasSuffix(".partial") { try? FileManager.default.removeItem(at: folder.appendingPathComponent(name)) }
        AppLog.write("storage", "temporary files removed")
        objectWillChange.send()
    }
    /// Plain-text report for debugging: state summary + recent log. No credentials.
    func diagnosticsFile() throws -> URL {
        var lines: [String] = []
        let info = Bundle.main.infoDictionary
        lines.append("Pudge \(PudgeTheme.device) diagnostics " + ISO8601DateFormatter().string(from: Date()))
        lines.append("App " + ((info?["CFBundleShortVersionString"] as? String) ?? "?") + " (" + ((info?["CFBundleVersion"] as? String) ?? "?") + ")")
        lines.append("iOS " + UIDevice.current.systemVersion + " · " + UIDevice.current.model)
        if let mac = paired { lines.append("Mac: " + mac.serverName + " · " + mac.baseURL + " · available=" + String(macAvailable)) } else { lines.append("Mac: not paired") }
        if let issue = syncIssue { lines.append("Sync issue: " + issue) }
        lines.append("Pending checkpoints: " + String((try? store.pendingCheckpoints().count) ?? -1))
        lines.append("Pending statistics: " + String((try? store.pendingActivityCount()) ?? -1))
        let rejected = (try? store.rejectedActivity(limit: 2000)) ?? []
        lines.append("Statistics not taken by the Mac: " + String(rejected.count))
        for (reason, count) in Dictionary(grouping: rejected, by: \.reason).mapValues(\.count).sorted(by: { $0.key < $1.key }) {
            lines.append("- " + reason + ": " + String(count))
        }
        let usage = storageUsage()
        lines.append("Storage: audio " + PudgeTheme.size(usage.audio) + ", covers " + PudgeTheme.size(usage.covers) + ", temporary " + PudgeTheme.size(usage.temporary) + ", free " + PudgeTheme.size(usage.free))
        lines.append("")
        lines.append("Books (\(books.count)):")
        for book in books {
            let ready = (try? store.ready(book)) == true
            let position = (try? store.localPosition(assetId: book.assetId, editionId: book.editionId)) ?? 0
            let remote = progress[book.assetId]
            lines.append("- " + book.title + " · edition " + String(book.editionId.prefix(8)) + " · ready=" + String(ready)
                + " · local " + PudgeTheme.time(position) + " · mac rev " + String(remote?.revision ?? -1)
                + " · playingOn=" + (remote?.playingOn ?? "-") + " · conflicts=" + String(remote?.conflicts.count ?? 0)
                + " · cover=" + String(book.cover != nil))
        }
        lines.append("")
        lines.append("Transfers (\(transfers.count)):")
        for record in transfers {
            lines.append("- " + record.part.fileName + " · " + record.status + " · " + PudgeTheme.size(record.receivedBytes ?? 0) + "/" + PudgeTheme.size(record.part.sizeBytes) + (record.message.map { " · " + $0 } ?? ""))
        }
        lines.append("")
        lines.append("Log:")
        lines.append(AppLog.contents())
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("pudge-iphone-diagnostics-" + String(Int(Date().timeIntervalSince1970)) + ".txt")
        try lines.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
        return url
    }
}
