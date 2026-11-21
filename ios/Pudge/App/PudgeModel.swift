import Foundation
import AVFoundation
import Combine
import Network
import UIKit
import PudgeCore

/// The Mac's catalog. Its lists are read item by item (LenientList): a book this app cannot read
/// (a missing field, another type, a manifest its rules refuse) is left out and named in
/// `skipped`, and every other book still loads. One such book failed the whole catalog before.
struct CatalogResponse: Decodable {
    let protocolMajor: Int
    let serverId: String
    let items: [AudioManifest]
    let errors: [CatalogError]?
    let preparing: Bool?
    var pending: [PendingBook]? = nil
    /// What was left out ("<title>: <why>"). Not empty: the catalog is not complete, so no
    /// book is marked removed on the Mac and no carried place is dropped because of it.
    var skipped: [String] = []
    private enum CodingKeys: String, CodingKey { case protocolMajor, serverId, items, errors, preparing, pending }
    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        protocolMajor = try fields.decode(Int.self, forKey: .protocolMajor)
        serverId = try fields.decode(String.self, forKey: .serverId)
        let books = try fields.decode(LenientList<AudioManifest>.self, forKey: .items)
        var readable: [AudioManifest] = []
        var left = books.skipped
        for book in books.items {
            do { try book.validate(); readable.append(book) }
            catch { left.append(book.title + ": " + error.localizedDescription) }
        }
        items = readable
        let failed = try fields.decodeIfPresent(LenientList<CatalogError>.self, forKey: .errors)
        errors = failed?.items
        preparing = try fields.decodeIfPresent(Bool.self, forKey: .preparing)
        let waiting = try fields.decodeIfPresent(LenientList<PendingBook>.self, forKey: .pending)
        pending = waiting?.items
        skipped = left + (failed?.skipped ?? []) + (waiting?.skipped ?? [])
    }
}
/// A book of a lazy catalog the Mac prepares (hash, convert) only when asked to.
/// A book in the Mac's lazy catalog (prepared for download on request); its cover comes with it
/// (newer Macs) so it shows in its series with its picture.
struct PendingBook: Decodable, Identifiable, Hashable { let assetId: String; let title: String; let durationMs: Int64?; let seriesTitle: String?; let volume: Int?; let cover: CoverResource?; var id: String { assetId } }
struct PrepareAnswer: Decodable { let status: String; let manifest: AudioManifest?; let error: String? }
/// Progress and bookmarks of several books in one request (Mac capability "state-batch").
struct StateRequest: Encodable { let assets: [String]; let since: String? }
struct StateItem: Decodable { let assetId: String; let progress: ProgressState; let bookmarks: [Bookmark] }
struct StateAnswer: Decodable { let revision: String; let unchanged: Bool; let items: [StateItem] }
struct CatalogError: Decodable { let title: String; let error: String }
struct PairRequest: Codable { let invitation: String; let deviceId: String; let attemptId: String; let name: String }
struct PendingPair: Codable { let payload: PairingPayload; let request: PairRequest }
/// "pending" with the code to compare with the Mac's Allow/Deny prompt, then the token once allowed.
struct PairReceipt: Decodable { let deviceId: String; let serverId: String; let token: String?; let status: String?; let confirmationCode: String? }
struct CheckpointReceipt: Decodable { let checkpointId: String; let revision: Int64; let status: String; var reason: String? = nil }
struct BookmarkReceipt: Decodable { let eventId: String; let revision: Int64; var status: String? = nil; var reason: String? = nil }
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
/// A pairing link opened from outside the app (Camera, Safari, Messages), shown before pairing.
struct PairingLinkOffer: Identifiable {
    let id = UUID()
    let text: String
    let serverName: String
    let hosts: String
    /// The paired Mac's name when the link names another Mac or the paired one with another certificate.
    let pairedName: String?
    let sameMac: Bool
    let certificateChanges: Bool
    func explanation(device: String) -> String {
        var lines = ["“\(serverName)” at \(hosts)."]
        if sameMac && certificateChanges {
            lines.append("It claims to be the paired Mac “\(pairedName ?? serverName)” but has another certificate: connect only if you just reinstalled Pudge on that Mac or reset its certificate.")
        } else if sameMac {
            lines.append("This is the paired Mac: its address and access are renewed.")
        } else if let pairedName {
            lines.append("This is not the paired Mac “\(pairedName)”: this \(device) would sync with the new one.")
        } else {
            lines.append("Connect only if the link came from the QR code of your own Mac.")
        }
        return lines.joined(separator: " ")
    }
}
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
    /// Bumped when audiobook covers arrived: cover views read theirs once per value (BookCoverImages).
    @Published private(set) var coverGeneration = 0
    /// Every stored manifest, also of books hidden as removed or replaced on the Mac (their
    /// queued places still go to the Mac, which answers them).
    private var allBooks: [AudioManifest] = []
    /// Book id → "edition" (the Mac has a newer edition) or "removed" (LocalStore.retireMissing).
    @Published var retired: [String: String] = [:]
    /// Books of a lazy catalog that the Mac prepares when asked ("Prepare on the Mac").
    @Published var pendingBooks: [PendingBook] = []
    @Published var preparingBooks = Set<String>()
    /// The last batched state answer (`since` of the next one) and when every book was last asked.
    private var stateRevision: String?
    private var fullStateAt = 0.0
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
            // An answer by any request counts as a check: the next ones need not ask again at once.
            if macAvailable { probeSchedule.record(answered: true, at: ProcessInfo.processInfo.systemUptime) }
        }
    }
    /// The Mac's answer (or its absence) was seen at least once since launch.
    private(set) var macChecked = false
    /// Paired, but the Mac did not answer the last check: downloaded media work with the
    /// data saved at the last update; grades and other Mac actions wait.
    var macOffline: Bool { paired != nil && macChecked && !macAvailable }
    private var probe: Task<Bool, Never>?
    /// When to ask the Mac again: an answer stands 5 s; after none 5 s, 15 s, then every 60 s
    /// (ProbeSchedule), so switching tabs away from the Mac never waits for it each time.
    private var probeSchedule = ProbeSchedule()
    /// Wi-Fi or a wired network is available: without one (only cellular) the Mac is not asked.
    @Published private(set) var onLocalNetwork = true
    private let pathMonitor = NWPathMonitor()
    private var lastPath: NWPath?
    /// Bumped whenever checks start over: a check still running from before no longer counts.
    private var probeGeneration = 0
    /// The saved address is on a local network (a LAN IP or a ".local" name): without Wi-Fi it is
    /// not asked. One entered for a VPN or a DNS name may answer over cellular.
    /// Only when every address of the Mac is a local one: a VPN or DNS name the user entered
    /// (kept among the known addresses after Bonjour moved the saved one) answers without Wi-Fi.
    private var macOnLAN: Bool {
        paired.map { MacAddresses.candidates(saved: $0.baseURL, known: $0.knownAddresses ?? []).allSatisfy(MacAddresses.isLocalNetwork) } ?? true
    }
    private var macNetworkReachable: Bool { onLocalNetwork || !macOnLAN }
    /// Settings → Connection and the screens' notes.
    var macLink: MacLink { MacLink.evaluate(paired: paired != nil, localNetwork: macNetworkReachable, checked: macChecked, available: macAvailable) }
    /// The same without Wi-Fi for the lists' notes (MediaModel's static texts).
    private(set) static var noLocalNetwork = false
    /// "Mac unavailable · <what still works>"; without Wi-Fi the reason instead.
    static func unavailable(_ note: String) -> String { MacLink.unavailable(note, noLocalNetwork: noLocalNetwork, device: PudgeTheme.device) }
    @Published var busy = false
    /// What the paired Mac's Pudge supports (/api/v2/capabilities), kept for offline use.
    @Published var macCapabilities = Set(UserDefaults.standard.stringArray(forKey: "pudge.macCapabilities") ?? [])
    func macSupports(_ capability: String) -> Bool { macCapabilities.contains(capability) }
    @Published var message: String? { didSet { if let message { AppLog.write("ui", message) } } }
    /// A video plays full screen: the alert for `message` waits until it closes (PlayerContinuity).
    @Published var videoOnScreen = false
    @Published var progress: [String: ProgressState] = [:]
    /// This device's own Finished / Mark unfinished until the Mac answered for the book (the
    /// card shows it at once, also offline); dropped once nothing of the book waits to be sent.
    @Published var finishedHere: [String: Bool] = (UserDefaults.standard.dictionary(forKey: "pudge.finishedHere") as? [String: Bool]) ?? [:] {
        didSet { UserDefaults.standard.set(finishedHere, forKey: "pudge.finishedHere") }
    }
    /// The Mac's Finished flag of each book as it last answered, kept across launches
    /// (MacFinishedFlags): after a launch without the Mac `progress` is empty, and a finished
    /// book listened to again showed unfinished until the Mac answered.
    private var macFinished = MacFinishedFlags((UserDefaults.standard.dictionary(forKey: MacFinishedFlags.defaultsKey) as? [String: Bool]) ?? [:])
    /// The catalog's unreadable books as last said (one alert per launch and set, not per refresh).
    private var skippedNoted: [String] = []
    /// Finished audiobooks whose "Mark light novel read" offer was put away (Not now).
    @Published var novelOfferDismissed = Set(UserDefaults.standard.stringArray(forKey: "pudge.novelOfferDismissed") ?? [])
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
    /// "Connect to this Mac?" for a pairing link opened from outside the app.
    @Published var linkOffer: PairingLinkOffer?
    /// Replaced (configure, adopt, changeAddress) or cleared: the old one closes its URLSessions
    /// in deinit once its last user (stream, transfers, a running request) lets it go.
    private var connection: MacConnection?
    /// Current verified Mac connection (reading, video and study use it too).
    var macConnection: MacConnection? { connection }
    /// Reading, video, study and statistics (shares this connection).
    private(set) lazy var media: MediaModel = MediaModel(app: self)
    private var transferManager: TransferManager?
    private var syncing = false
    /// Callers of syncAfterRunning waiting for the running sync to end.
    private var syncWaiters: [CheckedContinuation<Void, Never>] = []
    private var lastSend = 0.0
    private var maintenance: Task<Void, Never>?
    /// What is downloaded here, reported to the Mac for its library (extension below).
    private var inventoryPolicy = InventoryReportPolicy()
    private var inventoryTask: Task<Void, Never>?
    /// One report at a time; a change during one is sent right after it.
    private var inventorySending = false
    private var inventoryAgain = false
    private var inventoryWatch = Set<AnyCancellable>()
    /// Leftover files and watched downloads are cleaned once per launch (launchCleanup).
    private var launchCleanupDone = false

    init() throws {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true).appendingPathComponent("PudgeAudio", isDirectory: true)
        store = try LocalStore.open(at: directory)
        activity = ActivityRecorder(store: store)
        var freshDevice = false
        if let existing = try KeychainStore.load("device-id", as: String.self) { deviceId = existing }
        else { deviceId = UUID().uuidString.lowercased(); try KeychainStore.save("device-id", deviceId); freshDevice = true }
        player = AudioPlayerService(store: store, deviceId: deviceId)
        paired = try KeychainStore.load("paired-mac", as: PairedMac.self)
        if freshDevice, let moved = try? store.adoptDeviceId(deviceId), moved > 0 {
            // Restored from a backup or moved to a new iPhone: the Keychain ID stayed behind.
            AppLog.write("pairing", "new device ID: \(moved) unsent place(s) re-addressed to it")
        }
        reloadLocal()
        store.mergePeriodic = macSupports("checkpoint-runs")
        if let paired { try configure(paired) }
        // Wi-Fi comes and goes (home Wi-Fi ↔ cellular): the Mac is asked again at once then.
        // Delivered on the main queue (start(queue: .main)).
        pathMonitor.pathUpdateHandler = { [weak self] path in
            MainActor.assumeIsolated { self?.networkChanged(path) }
        }
        pathMonitor.start(queue: .main)
        // The lock screen / Apple Watch bookmark: the player's own Bookmark (an untitled one).
        player.onBookmark = { [weak self] in
            guard let self, let book = self.player.book else { return }
            self.addBookmark(book, title: "")
        }
        player.onCheckpoint = { [weak self] reason in
            guard let self else { return }
            if reason == "finish" || reason == "unfinish", let asset = self.player.book?.assetId {
                self.finishedHere[asset] = reason == "finish"
            }
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
    /// Runs after every sync (every 15 s): the lists are published only when they changed, as
    /// each assignment redrew every screen watching this model.
    func reloadLocal() {
        do {
            allBooks = try store.manifests()
            let records = try store.transfers()
            if records != transfers { transfers = records }
            let reasons = try store.retiredReasons()
            if reasons != retired { retired = reasons }
            // Removed or replaced on the Mac: shown only while downloaded here (their Download failed for good).
            let shown = allBooks.filter { retired[$0.id] == nil || (try? store.ready($0)) == true }
            if shown != books { books = shown }
            var marks: [String: [Bookmark]] = bookmarks
            for book in books { marks[book.id] = try store.bookmarks(assetId: book.assetId).filter { $0.editionId == book.editionId } }
            if marks != bookmarks { bookmarks = marks }
        } catch { message = error.localizedDescription }
    }
    /// A download's progress changed: only the transfer list (decoding every manifest and
    /// querying every book's bookmarks twice a second made the library stutter).
    func reloadTransfers() {
        if let records = try? store.transfers(), records != transfers { transfers = records }
    }
    func syncStatus(_ book: AudioManifest) -> String {
        if retired[book.id] == "edition" { return "Older edition: the Mac has a newer one" }
        if retired[book.id] == "removed" { return "Removed on the Mac; kept on this \(PudgeTheme.device)" }
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
        connectionGeneration += 1
        // Paired (again) or another Mac: it gets the whole inventory on the next refresh.
        inventoryPolicy.reset()
        // And is asked for every book's place in full (a revision of the previous Mac means nothing here).
        stateRevision = nil; fullStateAt = 0
        startChecksOver()
        updateNetworkNote()
        if let manager = transferManager, manager.pin == mac.certificateSHA256 { manager.updateConnection(client) }
        else { transferManager?.onChange = nil; transferManager?.cancelAll(); transferManager?.invalidate(); transferManager = try TransferManager(store: store, connection: client) }
        transferManager?.onChange = { [weak self] in self?.reloadLocal() }
        transferManager?.onProgress = { [weak self] in self?.reloadTransfers() }
        discovery.onAddress = { [weak self] address, numbers, named in
            guard let self, let saved = self.paired else { return }
            if saved.baseURL == address || (self.macAvailable && numbers.contains(saved.baseURL)) {
                // The Mac where it is reached now (by its name, or its IP after the name stopped
                // answering): nothing to adopt; its numeric addresses that answer as this Mac
                // are kept for when the name stops answering (macOS renames a Mac after a name
                // conflict, and the old name no longer resolves).
                Task { await self.rememberVerified(numbers) }
                return
            }
            Task {
                // The saved address still answers (e.g. a VPN or DNS name entered in Settings):
                // it stays the saved one; Bonjour used to replace it, and it was lost for good.
                if let current = self.connection, await current.answers() {
                    await self.rememberVerified(numbers)
                    return
                }
                // The advertised name first, then the service's numeric addresses.
                for candidate in [address] + numbers {
                    var mac = saved
                    mac.baseURL = candidate
                    guard let client = try? MacConnection(mac) else { continue }
                    do {
                        struct Identity: Decodable { let serverId: String; let protocolMajor: Int }
                        let info = try await client.send("/api/v2/capabilities", as: Identity.self)
                        guard info.serverId == mac.serverId && info.protocolMajor == 2 else { throw PudgeError.invalid("A different Mac was found") }
                        // Only the service's addresses that answer as this Mac are kept (a spoofed
                        // announcement or a bridge/VPN address must not push the good ones out).
                        let checked = await self.verified(numbers, by: client)
                        // The address it replaces stays known (probeMac asks it when this one stops answering).
                        mac.knownAddresses = MacAddresses.remember(checked + [saved.baseURL], in: self.paired?.knownAddresses ?? [])
                        // Existing background tasks keep their original request.
                        // Retries use the newly verified endpoint after recovery.
                        self.adopt(mac, client: client)
                        await self.refresh()
                        return
                    } catch {
                        client.close()
                        AppLog.write("pairing", "found Pudge at \(candidate): not the paired Mac")
                    }
                }
                // Macs advertise a name of their own per network now: another Pudge Mac on
                // this network (its certificate is not the paired one) says nothing about ours.
                if named { self.macAvailable = false }
            }
        }
        discovery.start(serverId: mac.serverId)
    }
    /// A pairing link opened from outside the app is shown first and paired only on "Connect":
    /// it paired at once, so one tapped link moved sync (address, certificate and token) to any
    /// host that named the paired Mac's id, which the Mac announces on the network.
    func offerLink(_ text: String) {
        do {
            let payload = try PairingPayload.parse(text)
            let hosts = payload.candidates.compactMap { URL(string: $0)?.host }
            let sameMac = paired?.serverId == payload.serverId
            let certificateChanges = sameMac && paired?.certificateSHA256 != payload.certificateSHA256
            linkOffer = PairingLinkOffer(text: text, serverName: payload.serverName, hosts: hosts.joined(separator: ", "),
                                         pairedName: sameMac && !certificateChanges ? nil : paired?.serverName,
                                         sameMac: sameMac, certificateChanges: certificateChanges)
            AppLog.write("pairing", "link opened for \(payload.serverName) (paired Mac: \(sameMac), new certificate: \(certificateChanges)); asking")
        } catch { message = error.localizedDescription }
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
                // Places and "watched" marks of downloaded episodes and volumes too: after the
                // switch they belong to the previous Mac and are never sent.
                await media.sendVideoPositions(); await media.sendReadingPositions()
                AppLog.write("pairing", "sent unsent changes to the previous Mac before switching")
            }
            let mac = try await pairWithMac(payload)
            let summary = try store.rebindMac(to: payload.serverId)
            media.macSwitched()
            AppLog.write("pairing", "switched Mac: phone books \(summary.phoneBooks), moved places \(summary.carriedPositions), dropped positions \(summary.droppedPositions), bookmarks \(summary.droppedBookmarks), statistics \(summary.droppedStatistics)")
            try KeychainStore.save("paired-mac", mac)
            macAvailable = false; progress = [:]
            // The previous Mac's Finished flags say nothing about this Mac's books.
            macFinished = MacFinishedFlags(); saveFinishedFlags()
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
        pairingCancelled = false
        var mac = try await reachableMac(payload)
        let client = try MacConnection(mac)
        // Only this loop uses it (configure opens the kept one): its URLSessions end with it.
        defer { pairingCode = nil; client.close() }
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
    /// The invitation lists the Mac's LAN addresses (Wi-Fi, Ethernet ...), best first. On a first
    /// pairing iOS asks for Local Network access while the first attempt runs, and that attempt
    /// fails: all addresses are tried once more after 3 s before giving up.
    private func reachableMac(_ payload: PairingPayload) async throws -> PairedMac {
        for attempt in 1...2 {
            if attempt > 1 {
                try await Task.sleep(nanoseconds: 3_000_000_000)
                if pairingCancelled { throw PudgeError.invalid("Pairing cancelled") }
            }
            for address in payload.candidates {
                // Every address of the invitation is kept: tried later when this one stops answering.
                let mac = PairedMac(deviceId: deviceId, serverId: payload.serverId, serverName: payload.serverName,
                                    baseURL: address, certificateSHA256: payload.certificateSHA256, token: "",
                                    knownAddresses: payload.candidates)
                let client = try MacConnection(mac)
                // Only a probe: its URLSessions (and their delegates) are released, never kept.
                let answered = await client.answers()
                client.close()
                if answered { return mac }
                AppLog.write("pairing", "no answer at \(address) (attempt \(attempt) of 2)")
            }
        }
        let device = PudgeTheme.device
        throw PudgeError.invalid("The Mac did not answer at " + payload.candidates.joined(separator: ", ")
            + ". Check that this \(device) and the Mac are on the same Wi-Fi network, that Local Network access is on for Pudge"
            + " (Settings → Privacy & Security → Local Network) and that no VPN is active on this \(device).")
    }
    func disconnect() {
        do { try KeychainStore.delete("paired-mac"); discovery.stop(); transferManager?.onChange = nil; transferManager?.cancelAll(); transferManager?.invalidate(); transferManager = nil; connection = nil; paired = nil; macAvailable = false }
        catch { message = error.localizedDescription }
    }
    func changeAddress(_ address: String) async {
        guard var mac = paired else { return }
        do {
            guard let url = URL(string: address), url.scheme == "https", url.host != nil,
                  url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else {
                throw PudgeError.invalid("Enter the Mac's HTTPS address without login, parameters or fragment")
            }
            let previous = mac.baseURL
            mac.baseURL = address
            // Both stay known: Bonjour or probeMac moving the saved address never loses the one entered here.
            mac.knownAddresses = MacAddresses.remember([address, previous], in: mac.knownAddresses ?? [])
            let client = try MacConnection(mac)
            struct Identity: Decodable { let serverId: String }
            let identity = try await client.send("/api/v2/capabilities", as: Identity.self)
            guard identity.serverId == mac.serverId else { throw PudgeError.invalid("A different Mac") }
            try KeychainStore.save("paired-mac", mac); paired = mac; connection = client; connectionGeneration += 1
            transferManager?.updateConnection(client)
            // As adopt: a check still running against the old address no longer counts (it could
            // mark the Mac unavailable or move the saved address back).
            startChecksOver()
            updateNetworkNote()
            await refreshNow()
            message = "Mac address updated"
        } catch { message = error.localizedDescription }
    }
    /// Whether the paired Mac answers now (a few seconds at most, one check shared by callers).
    /// No answer marks the Mac unavailable; it becomes available again only through an
    /// authorised request (catalog or sync), so a revoked device never looks connected.
    /// Without Wi-Fi it is not asked at all, and after no answer it is asked again only after
    /// 5 s, 15 s, then every 60 s (ProbeSchedule; a network change, the foreground, pull to
    /// refresh and Refresh ask at once): callers get the last result without waiting meanwhile.
    /// The saved address first, then the Mac's other known addresses (probeMac).
    func reachMac() async -> Bool {
        guard connection != nil else { return false }
        guard macNetworkReachable else {
            if macAvailable || !macChecked {
                AppLog.write("sync", "no Wi-Fi: the Mac is not asked")
                macAvailable = false
            }
            return false
        }
        let generation = probeGeneration
        if let probe {
            let answered = await probe.value
            if generation == probeGeneration { return answered }
            // Begun before the checks started over: ask again (on the network of now).
            return await reachMac()
        }
        guard probeSchedule.shouldProbe(now: ProcessInfo.processInfo.systemUptime) else { return probeSchedule.lastAnswered }
        let started = ProcessInfo.processInfo.systemUptime
        let task = Task { await self.probeMac() }
        probe = task
        var answered = await task.value
        // A network change, the foreground, Refresh or a new address meanwhile: this answer says
        // nothing now (and must not clear a newer check or mark the Mac unavailable).
        guard generation == probeGeneration else { return await reachMac() }
        probe = nil
        // An authorised request got an answer while this check waited: the Mac is there.
        if !answered, macAvailable, probeSchedule.lastAnswered, let at = probeSchedule.lastAt, at > started { answered = true }
        probeSchedule.record(answered: answered, at: ProcessInfo.processInfo.systemUptime)
        if !answered {
            if macAvailable || !macChecked {
                AppLog.write("sync", "Mac unavailable")
                macAvailable = false
            }
            // The Bonjour search starts over on the first miss (the Mac may have a new name) or
            // when it is not running; a running one is kept while the Mac stays away.
            if probeSchedule.failures == 1 || !discovery.isSearching { discovery.restart() }
        }
        return answered
    }
    /// The saved address first; the Mac's other known addresses (its last IPs, the invitation's)
    /// join after a short head start over the same pinned session, with the same certificate
    /// and server ID check, so a dead name costs no extra wait. The saved address wins when it
    /// answers; otherwise the first other one that answered becomes the saved one.
    private func probeMac() async -> Bool {
        guard let connection, let mac = paired else { return false }
        let others = Array(MacAddresses.candidates(saved: mac.baseURL, known: mac.knownAddresses ?? []).dropFirst())
        let winner = await withTaskGroup(of: (Int, Bool).self, returning: Int?.self) { group in
            group.addTask { (0, await connection.answers()) }
            for (index, address) in others.enumerated() {
                group.addTask {
                    // On the Mac's network the saved address answers well within this.
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    if Task.isCancelled { return (index + 1, false) }
                    return (index + 1, await connection.answers(at: address))
                }
            }
            var answered = Set<Int>()
            for await result in group where result.1 {
                if result.0 == 0 { group.cancelAll(); return 0 }
                answered.insert(result.0)
            }
            return answered.min()
        }
        guard let winner else { return false }
        guard winner > 0 else { return true }
        guard var moved = paired, moved.serverId == mac.serverId else { return false }
        // Bonjour found it at another address meanwhile (checked there): it is reachable.
        guard moved.baseURL == mac.baseURL else { return true }
        let address = others[winner - 1]
        moved.baseURL = address
        moved.knownAddresses = MacAddresses.remember([address, mac.baseURL], in: moved.knownAddresses ?? [])
        guard let client = try? MacConnection(moved) else { return false }
        AppLog.write("pairing", "no answer at the saved address; the Mac answered at another of its addresses")
        adopt(moved, client: client)
        return true
    }
    /// The paired Mac answered at another address: it is the saved one from now on.
    private func adopt(_ mac: PairedMac, client: MacConnection) {
        try? KeychainStore.save("paired-mac", mac)
        paired = mac; connection = client; connectionGeneration += 1
        transferManager?.updateConnection(client)
        transferManager?.onChange = { [weak self] in self?.reloadLocal() }
        transferManager?.onProgress = { [weak self] in self?.reloadTransfers() }
        startChecksOver()
        updateNetworkNote()
        AppLog.write("pairing", "the Mac answers at \(mac.baseURL) now")
    }
    /// Of these addresses, those where this Mac answers (certificate pin and server ID), in order.
    private func verified(_ addresses: [String], by client: MacConnection) async -> [String] {
        guard !addresses.isEmpty else { return [] }
        let answered = await withTaskGroup(of: (Int, Bool).self, returning: Set<Int>.self) { group in
            for (index, address) in addresses.enumerated() {
                group.addTask { (index, await client.answers(at: address)) }
            }
            var found = Set<Int>()
            for await result in group where result.1 { found.insert(result.0) }
            return found
        }
        return addresses.enumerated().filter { answered.contains($0.offset) }.map { $0.element }
    }
    /// The paired Mac's numeric addresses as Bonjour resolved them, kept once they answer as this
    /// Mac (unchecked ones could push the invitation's good addresses out of the list).
    private func rememberVerified(_ numbers: [String]) async {
        guard let connection, let mac = paired else { return }
        let new = numbers.filter { !(mac.knownAddresses ?? []).contains($0) }
        let good = await verified(new, by: connection)
        guard !good.isEmpty, var current = paired, current.serverId == mac.serverId else { return }
        current.knownAddresses = MacAddresses.remember(good, in: current.knownAddresses ?? [])
        try? KeychainStore.save("paired-mac", current)
        paired = current
    }
    /// The checks start over: the next one asks at once, and one still running (begun on
    /// another network, before the foreground, Refresh or a new address) no longer counts.
    private func startChecksOver() {
        probeSchedule.reset()
        probeGeneration += 1
        probe = nil
    }
    /// The lists' "No Wi-Fi" note follows the network and the saved address.
    private func updateNetworkNote() { Self.noLocalNetwork = !macNetworkReachable }
    /// Wi-Fi came, went or changed (another network keeps the same interface): the Mac is asked
    /// again at once (in the background) and discovery starts over; without Wi-Fi it is not asked.
    private func networkChanged(_ path: NWPath) {
        guard path != lastPath else { return }
        let first = lastPath == nil
        lastPath = path
        #if targetEnvironment(simulator)
        onLocalNetwork = true
        #else
        onLocalNetwork = path.availableInterfaces.contains { MacLink.localInterface(type: Self.kind($0.type), name: $0.name) }
        #endif
        updateNetworkNote()
        startChecksOver()
        guard paired != nil else { return }
        if !macNetworkReachable {
            if macAvailable { macAvailable = false }
            AppLog.write("sync", "no Wi-Fi: syncing waits for the Mac's network")
            return
        }
        guard !first else { return }
        // iOS reports one change as two or three updates within a second: one Bonjour restart
        // and one refresh after they settle (each restarted the search and asked the Mac).
        networkSettle?.cancel()
        networkSettle = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.networkSettleSeconds * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            self.networkSettle = nil
            AppLog.write("sync", "network changed: asking the Mac")
            self.discovery.restart()
            await self.refresh()
        }
    }
    private var networkSettle: Task<Void, Never>?
    static let networkSettleSeconds = 1.5
    private static func kind(_ type: NWInterface.InterfaceType) -> String {
        switch type {
        case .wifi: return "wifi"
        case .wiredEthernet: return "wiredEthernet"
        case .cellular: return "cellular"
        case .loopback: return "loopback"
        default: return "other"
        }
    }
    /// Back in the foreground: the Mac is asked at once and discovery starts over (a search that
    /// stalled in the background or off Wi-Fi is not kept).
    func cameToForeground() {
        startChecksOver()
        if paired != nil { discovery.restart() }
    }
    /// Pull to refresh and Refresh: the Mac is asked now, whatever the last check said.
    func retryMacNow() { startChecksOver() }
    func refreshNow() async { retryMacNow(); await refresh() }
    /// The Mac answers again after being unavailable: everything refreshes as usual.
    private func reconnected() {
        AppLog.write("sync", "Mac available again")
        Task {
            if !refreshing { await refresh() }
            await media.reconnected()
        }
    }
    /// One refresh at a time: the launch, the foreground, a network change (iOS reports one
    /// twice), "Mac available again" and Bonjour each started their own, so the catalog, sync and
    /// inventory ran twice ("[catalog] 6 books" twice in a second). A caller while one runs waits
    /// for it; only after a new connection (pairing, another address) does it run once more.
    func refresh() async {
        if let running = refreshTask {
            let began = refreshConnection
            await running.value
            guard began != connectionGeneration else { return }
            if let next = refreshTask { await next.value; return }
        }
        let task = Task { await self.refreshOnce(); self.refreshTask = nil }
        refreshTask = task
        refreshConnection = connectionGeneration
        await task.value
    }
    private var refreshTask: Task<Void, Never>?
    /// connectionGeneration when the running refresh began.
    private var refreshConnection = 0
    /// Bumped whenever the Mac connection is replaced (configure, adopt, a new address).
    private var connectionGeneration = 0

    private func refreshOnce() async {
        guard self.connection != nil else { reloadLocal(); return }
        refreshing = true
        var perf = PerfSteps("refresh", at: ProcessInfo.processInfo.systemUptime)
        defer {
            refreshing = false; lastRefresh = Date()
            if let line = perf.report(at: ProcessInfo.processInfo.systemUptime) { AppLog.write("perf", line) }
        }
        // Away from the Mac the library shows what is on this iPhone without waiting.
        guard await reachMac() else { reloadLocal(); return }
        perf.mark("reach", at: ProcessInfo.processInfo.systemUptime)
        // It may have answered at another of its addresses (now the saved one).
        guard let connection else { reloadLocal(); return }
        do {
            // What this Mac's Pudge supports, before anything here depends on it. Asked after the
            // catalog, the first refresh after pairing (nothing kept yet) took the full catalog,
            // for which a Mac hashes and converts its whole library; after switching Macs the
            // previous Mac's list stood until the catalog had come.
            struct Capabilities: Decodable { let capabilities: [String]? }
            if let supported = try? await connection.send("/api/v2/capabilities", as: Capabilities.self) {
                let found = Set(supported.capabilities ?? [])
                if found != macCapabilities {
                    macCapabilities = found
                    UserDefaults.standard.set(Array(macCapabilities).sorted(), forKey: "pudge.macCapabilities")
                }
                store.mergePeriodic = macSupports("checkpoint-runs")
            }
            // A Mac that prepares books on request lists the others without a manifest (pending):
            // it hashed and converted the whole library, also books never downloaded.
            let lazy = macSupports("lazy-catalog")
            let route = lazy ? "/api/v2/catalog?lazy=1" : "/api/v2/catalog"
            // "Changed since?" with the kept answer's ETag (an older Mac answers in full): an
            // unchanged library is a 304 and the kept catalog is used again. Read and decoded
            // off the main thread (a large library's catalog is MBs).
            let cache = CatalogCache(directory: store.directory), server = paired?.serverId
            let fetched = try await Task.detached(priority: .userInitiated) { () async throws -> (catalog: CatalogResponse, answer: CatalogCache.Answer, etag: String?) in
                let sentTag = cache.etag(route: route, serverId: server)
                var asked = try await connection.dataIfChanged(route, etag: sentTag)
                if let kept = try? cache.answer(route: route, serverId: server, fresh: asked.body, sentETag: sentTag) {
                    do {
                        let decoded = try JSONDecoder().decode(CatalogResponse.self, from: kept.body)
                        return (decoded, kept, asked.etag)
                    } catch {
                        // A damaged kept file (304) is asked again in full below, never an
                        // error; a fresh answer that cannot be read is one.
                        cache.clear()
                        if !kept.unchanged { throw error }
                    }
                }
                // A 304 whose kept body is gone or damaged: asked again in full.
                cache.clear(); asked = try await connection.dataIfChanged(route, etag: nil)
                let answer = try cache.answer(route: route, serverId: server, fresh: asked.body, sentETag: nil)
                do {
                    let decoded = try JSONDecoder().decode(CatalogResponse.self, from: answer.body)
                    return (decoded, answer, asked.etag)
                } catch { cache.clear(); throw error }
            }.value
            let catalog = fetched.catalog, answer = fetched.answer
            perf.mark("catalog", at: ProcessInfo.processInfo.systemUptime)
            AppLog.write("catalog", "\(catalog.items.count) books, preparing=\(catalog.preparing ?? false), errors=\(catalog.errors?.count ?? 0), pending=\(catalog.pending?.count ?? 0)"
                + (catalog.skipped.isEmpty ? "" : ", unreadable=\(catalog.skipped.count)") + (answer.unchanged ? ", unchanged" : ""))
            guard catalog.protocolMajor == 2 && catalog.serverId == paired?.serverId else { cache.clear(); throw PudgeError.invalid("Unsupported server") }
            noteSkipped(catalog.skipped)
            // Unchanged: only books missing here (a reset store) are saved, not every manifest again.
            let saved = try answer.unchanged ? store.manifestIds() : Set<String>()
            // Marked as this Mac's also when the manifest is here already: a book whose mark is
            // gone stayed a book of this device only (never synced, never retired) while the catalog was a 304.
            let marked = try answer.unchanged ? store.remoteIds(serverId: catalog.serverId) : Set<String>()
            do {
                for book in catalog.items where !saved.contains(book.id) {
                    try store.saveManifest(book)
                }
                for book in catalog.items where !marked.contains(book.id) { try store.markRemote(book, serverId: catalog.serverId) }
            } catch { cache.clear(); throw error }
            if !answer.unchanged {
                let etag = fetched.etag, body = answer.body, server = catalog.serverId
                await Task.detached(priority: .utility) { cache.keep(route: route, serverId: server, etag: etag, body: body) }.value
            }
            try moveCarriedPlaces(catalog)
            pendingBooks = (catalog.pending ?? []).filter { item in !catalog.items.contains { $0.assetId == item.assetId } }
            if catalog.preparing != true, (catalog.errors ?? []).isEmpty, catalog.skipped.isEmpty {
                // Complete: books of this Mac it no longer lists are an older edition or gone there.
                // (A book left out as unreadable is still on the Mac: nothing is retired then.)
                try store.retireMissing(serverId: catalog.serverId, listed: catalog.items, pendingAssets: Set(pendingBooks.map(\.assetId)))
            }
            macAvailable = true; reloadLocal()
            forgetFinishedFlags()
            perf.mark("store", at: ProcessInfo.processInfo.systemUptime)
            if let errors = catalog.errors, !errors.isEmpty { message = "Could not prepare: " + errors.map(\.title).joined(separator: ", ") }
            await prefetchCovers(catalog.items, pending: pendingBooks, connection: connection)
            perf.mark("covers", at: ProcessInfo.processInfo.systemUptime)
            for book in books where book.lightNovelId != nil && (try? store.ready(book)) == true {
                media.downloadLinkedNovel(book.lightNovelId!, title: book.seriesTitle ?? book.title)
            }
            // Downloaded light novels and manga: changed on the Mac? (at most every 6 hours)
            Task { await media.checkReadingUpdates() }
            // Episode downloads cut off earlier (Pudge closed, the Mac away) go on where they stopped.
            Task { await media.resumeInterruptedDownloads() }
            await sync()
            // What is downloaded here, for the Mac's library (after connecting or refreshing).
            watchInventory()
            await reportInventory()
            perf.mark("sync and inventory", at: ProcessInfo.processInfo.systemUptime)
            if catalog.preparing == true, !catalogRetryScheduled {
                // The Mac is still preparing books/covers: pick them up shortly.
                catalogRetryScheduled = true
                Task { try? await Task.sleep(nanoseconds: 5_000_000_000); catalogRetryScheduled = false; await refresh() }
            }
        } catch {
            macAvailable = false
            // The Mac went away during the refresh (quit for an update, asleep): the lists say
            // "Mac unavailable" and it is asked again; only another failure is an alert.
            if TransferRetry.isTransient(error) { AppLog.write("sync", "refresh stopped: \(error.localizedDescription)") }
            else { message = error.localizedDescription }
            reloadLocal()
        }
    }
    /// Books of the catalog this app could not read (CatalogResponse.skipped): logged, and said
    /// once per launch and set of books (every refresh would say it again).
    private func noteSkipped(_ skipped: [String]) {
        guard skipped != skippedNoted else { return }
        skippedNoted = skipped
        guard !skipped.isEmpty else { return }
        for line in skipped.prefix(20) { AppLog.write("catalog", "left out: " + line) }
        message = "This \(PudgeTheme.device) could not read \(skipped.count) audiobook(s) of the Mac's library; the others are listed. "
            + "Update Pudge on the Mac and on this \(PudgeTheme.device) (details: Settings → Share diagnostics report)."
    }
    /// The Mac's answer for a book: shown now, and its Finished flag kept for a launch without the Mac.
    private func noteMacProgress(_ asset: String, _ state: ProgressState) {
        progress[asset] = state
        if macFinished.record(asset, finished: state.finished) { saveFinishedFlags() }
    }
    private func saveFinishedFlags() {
        UserDefaults.standard.set(macFinished.flags, forKey: MacFinishedFlags.defaultsKey)
    }
    /// The kept flags of books no longer on this device go.
    private func forgetFinishedFlags() {
        if macFinished.keep(Set(allBooks.map(\.assetId))) { saveFinishedFlags() }
    }
    /// After switching Macs: the last unsent place of each book goes to the same audio on this Mac;
    /// once the catalog is complete, places without a match are dropped (counted).
    private func moveCarriedPlaces(_ catalog: CatalogResponse) throws {
        guard try store.carriedCount() > 0 else { return }
        var moved = 0
        for book in catalog.items {
            if try store.adoptCarried(book, deviceId: deviceId) { moved += 1 }
        }
        var dropped = 0
        // Not while a book was left out as unreadable: it may be the one a place waits for.
        if catalog.preparing != true, catalog.skipped.isEmpty { dropped = try store.dropCarried() }
        if moved + dropped > 0 {
            message = "Last place of \(moved) audiobook(s) moved to this Mac" + (dropped > 0 ? "; \(dropped) had no matching audiobook here and were dropped" : "")
            AppLog.write("pairing", message ?? "")
        }
    }
    /// Covers are small: fetch them for every catalog book, not only downloaded ones. A read-along
    /// text (up to 50 MB, new whenever the Mac parsed more of the novel) only for downloaded books.
    private func prefetchCovers(_ items: [AudioManifest], pending: [PendingBook] = [], connection: MacConnection) async {
        let folder = store.directory.appendingPathComponent("resources", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        var changed = false
        let wanted = items.compactMap { $0.cover } + items.filter { (try? store.ready($0)) == true }.compactMap { $0.readAlong }
        // Books still to be prepared on the Mac show their covers too (in their series).
        let waiting: [CoverResource] = pending.compactMap(\.cover)
        for cover in wanted + waiting {
            if let local = try? store.resource(cover.resourceId), local.state.verified, local.state.sha256 == cover.sha256,
               FileManager.default.fileExists(atPath: local.url.path) { continue }
            do {
                let ext = (cover.fileName as NSString).pathExtension
                let target = folder.appendingPathComponent(cover.resourceId + "." + ext)
                // Straight into the file: a read-along text (up to 50 MB) is never held in memory.
                try await connection.download("/api/v2/resources/\(cover.resourceId)/content?sha256=\(cover.sha256)", to: target)
                try store.markVerified(cover.downloadPart, at: target)
                changed = true
            } catch { continue }  // A missing cover/text never blocks the library.
        }
        if changed { coverGeneration += 1; reloadLocal() }
    }
    /// Going to the background: the place just saved goes now. A sync already running took its
    /// queue before that place was saved, and a new one returned at once (the place waited for the
    /// next sync, often the next foreground): after the running one ends, once more.
    func syncAfterRunning() async {
        if syncing { await withCheckedContinuation { syncWaiters.append($0) } }
        await sync()
    }
    func sync() async {
        guard self.connection != nil, !syncing else { return }
        syncing = true; lastSend = ProcessInfo.processInfo.systemUptime
        defer {
            syncing = false; reloadLocal()
            let waiters = syncWaiters; syncWaiters = []
            for waiter in waiters { waiter.resume() }
        }
        if syncIssue != nil { syncIssue = nil }
        // While the Mac is unavailable a quick check comes first (no long timeouts).
        if !macAvailable, !(await reachMac()) { return }
        // It may have answered at another of its addresses (now the saved one).
        guard let connection else { return }
        do {
            var refused: [String] = []
            for checkpoint in try store.pendingCheckpoints() {
                guard let book = allBooks.first(where: { $0.assetId == checkpoint.assetId && $0.editionId == checkpoint.editionId }),
                      let server = paired?.serverId, try store.isRemote(book, serverId: server) else { continue }
                let receipt: CheckpointReceipt
                do {
                    receipt = try await connection.post("/api/v2/checkpoints", value: checkpoint, as: CheckpointReceipt.self)
                } catch let refusal as MacRefusal {
                    // Refused for good (HTTP 409/422): set aside, the rest of the queue still goes.
                    try store.park(checkpoint.checkpointId, reason: refusal.localizedDescription)
                    AppLog.write("sync", "position of \(book.title) set aside: \(refusal.localizedDescription)")
                    continue
                }
                guard receipt.checkpointId == checkpoint.checkpointId else { throw PudgeError.invalid("Invalid position receipt") }
                try store.acknowledge(checkpoint.checkpointId, revision: receipt.revision)
                if receipt.status == "rejected" {
                    refused.append("“\(book.title)”: " + Self.rejection(receipt.reason))
                    AppLog.write("sync", "position of \(book.title) not taken by the Mac: \(receipt.reason ?? "-")")
                }
            }
            for bookmark in try store.pendingBookmarks() {
                guard let book = allBooks.first(where: { $0.assetId == bookmark.assetId && $0.editionId == bookmark.editionId }),
                      let server = paired?.serverId, try store.isRemote(book, serverId: server) else { continue }
                let receipt: BookmarkReceipt
                do {
                    receipt = try await connection.post("/api/v2/bookmarks", value: bookmark, as: BookmarkReceipt.self)
                } catch let refusal as MacRefusal {
                    try store.park(bookmark.eventId, reason: refusal.localizedDescription)
                    AppLog.write("sync", "bookmark of \(book.title) set aside: \(refusal.localizedDescription)")
                    continue
                }
                guard receipt.eventId == bookmark.eventId else { throw PudgeError.invalid("Invalid bookmark receipt") }
                if receipt.status == "rejected" {
                    // Changed on the Mac first: its version is taken (the local copy kept its own for good).
                    try store.dropLocalBookmark(bookmark.bookmarkId)
                    AppLog.write("sync", "bookmark of \(book.title) changed on the Mac first (\(receipt.reason ?? "-")): taking the Mac's")
                }
                try store.acknowledgeBookmark(bookmark.eventId)
            }
            if !refused.isEmpty { message = "Not taken by the Mac: " + refused.joined(separator: "; ") }
            try await syncState(connection)
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
            // Every 15 s: set (publishing a redraw) only when it was not available.
            if macAvailable { probeSchedule.record(answered: true, at: ProcessInfo.processInfo.systemUptime) } else { macAvailable = true }
        } catch {
            // Only a network failure makes the Mac unavailable (MacFailure); it stays the issue shown.
            if MacFailure.unreachable(error) { macAvailable = false }
            syncIssue = error.localizedDescription
        }
        if macAvailable { await sendActivity(connection) }
    }
    /// Why the Mac did not take a position (its rejected receipt).
    static func rejection(_ reason: String?) -> String {
        switch reason {
        case "unknown_asset": return "the book is no longer on the Mac"
        case "edition_changed": return "the Mac has a newer edition of the book"
        case "device_changed": return "it was saved before this device was set up again"
        default: return "the Mac kept another position"
        }
    }
    /// Books whose position and bookmarks are asked on every sync: downloaded here, playing, or
    /// with changes waiting. All books are asked every 5 minutes (each sync asked twice per book
    /// of the whole library: about 300 requests every 15 s for 150 books).
    private func polledBooks(all: Bool) -> [AudioManifest] {
        guard let server = paired?.serverId else { return [] }
        return books.filter { book in
            guard (try? store.isRemote(book, serverId: server)) == true else { return false }
            let downloaded = (try? store.ready(book)) == true
            // A downloaded copy marked as an older edition keeps its place and bookmarks in sync
            // (store.adopt takes a place only of its own edition); a hidden one is not asked.
            if retired[book.id] != nil && !downloaded { return false }
            return all || downloaded || player.book?.assetId == book.assetId
                || (try? store.hasPending(assetId: book.assetId)) == true || !(progress[book.assetId]?.conflicts.isEmpty ?? true)
        }
    }
    private func syncState(_ connection: MacConnection, only: [AudioManifest]? = nil) async throws {
        let now = ProcessInfo.processInfo.systemUptime
        let all = only == nil && now - fullStateAt >= 300
        let polled = only ?? polledBooks(all: all)
        guard !polled.isEmpty else { return }
        if all { fullStateAt = now }
        if macSupports("state-batch") {
            // At most StateBatch.size books per request: the Mac refuses over 1000 (HTTP 409), and
            // every full ask of a larger library failed.
            let batches = StateBatch.batches(polled.map(\.assetId))
            // `since` only for the frequent small set: an unchanged answer then carries no items.
            let since: String? = (all || only != nil) ? nil : StateBatch.since(stateRevision, batches: batches.count)
            var revision: String?
            var playingHere = false
            for batch in batches {
                let answer = try await connection.post("/api/v2/sync/state", value: StateRequest(assets: batch, since: since), as: StateAnswer.self)
                revision = answer.revision
                // Unchanged (no items): the Mac's flag already here counts as its answer (a
                // Finished the Mac refused left it as it was, and "Finished" stayed here for good).
                if answer.unchanged { for asset in batch where progress[asset] != nil { adoptMacFinished(asset) } }
                for item in answer.items {
                    noteMacProgress(item.assetId, item.progress)
                    adoptMacFinished(item.assetId)
                    for book in polled where book.assetId == item.assetId {
                        // Not taken while it plays here: asked again in full next time.
                        if player.book?.assetId != book.assetId { try store.adopt(item.progress, book: book) } else { playingHere = true }
                    }
                    try store.mergeBookmarks(item.bookmarks)
                }
            }
            // Kept only once every item was applied: saved before, a failure halfway (store.adopt,
            // mergeBookmarks) left it, and the next "unchanged" answer never brought those items again.
            if !all && only == nil { stateRevision = StateBatch.kept(revision, batches: batches.count, playingHere: playingHere) }
            else if playingHere { stateRevision = nil }
            return
        }
        for book in polled {
            let value = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
            noteMacProgress(book.assetId, value)
            adoptMacFinished(book.assetId)
            if player.book?.assetId != book.assetId { try store.adopt(value, book: book) }
            let marks = try await connection.send("/api/v2/assets/\(book.assetId)/bookmarks", as: BookmarkEnvelope.self)
            try store.mergeBookmarks(marks.items)
        }
    }
    /// The book's page opened: its position and bookmarks now (it may not be among the polled ones).
    func refreshState(_ book: AudioManifest) async {
        guard let connection, macAvailable, !syncing else { return }
        do { try await syncState(connection, only: [book]) } catch { AppLog.write("sync", "state of \(book.title): \(error.localizedDescription)") }
    }
    /// A book of a lazy catalog: the Mac prepares it now (first of all), then it can be downloaded.
    func prepareOnMac(_ item: PendingBook) async {
        guard let connection, !preparingBooks.contains(item.assetId) else { return }
        preparingBooks.insert(item.assetId); defer { preparingBooks.remove(item.assetId) }
        struct Request: Encodable { let assetId: String }
        do {
            for _ in 0..<60 {
                let answer = try await connection.post("/api/v2/assets/prepare", value: Request(assetId: item.assetId), as: PrepareAnswer.self)
                if answer.status == "failed" { throw PudgeError.invalid("The Mac could not prepare “\(item.title)”: \(answer.error ?? "error")") }
                if answer.status == "ready", let manifest = answer.manifest, let server = paired?.serverId {
                    try store.saveManifest(manifest); try store.markRemote(manifest, serverId: server)
                    pendingBooks.removeAll { $0.assetId == item.assetId }
                    reloadLocal()
                    return
                }
                try await Task.sleep(nanoseconds: 5_000_000_000)
            }
            throw PudgeError.invalid("The Mac is still preparing “\(item.title)”. Try again later.")
        } catch { message = error.localizedDescription }
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
    /// Library → Mark unfinished: clears Finished on every device; the place stays (from the
    /// very end the book starts over, as on the Mac: the end would count as finished again).
    /// Only for a Mac that knows the "unfinish" checkpoint (an older one would refuse it).
    func markUnfinished(_ book: AudioManifest) {
        guard macSupports("progress-unfinish") else { return }
        if player.book?.assetId == book.assetId {
            // Open in the player at the very end: starts over too (the next save of a place at
            // the end would count as finished again on the Mac).
            if AudiobookFinish.reachedEnd(positionMs: player.positionMs, durationMs: book.durationMs) {
                Task {
                    do { try await player.seek(0, reason: "unfinish") } catch { player.save("unfinish") }
                }
            } else { player.save("unfinish") }
            return
        }
        do {
            var position = try store.localPosition(assetId: book.assetId, editionId: book.editionId)
            if let saved = progress[book.assetId]?.selected, saved.editionId == book.editionId { position = saved.positionMs }
            let revision = try store.revision(assetId: book.assetId)
            let checkpoint = try AudioCheckpoint.make(book: book, deviceId: deviceId, sessionId: UUID().uuidString.lowercased(),
                                                      sequence: 1, revision: revision,
                                                      positionMs: AudiobookFinish.unfinishedPosition(positionMs: position, durationMs: book.durationMs),
                                                      reason: "unfinish")
            try store.enqueue(checkpoint); finishedHere[book.assetId] = false; reloadLocal(); Task { await sync() }
        } catch { message = error.localizedDescription }
    }
    /// The Mac answered for a book: its Finished counts once nothing of this device waits to be sent.
    private func adoptMacFinished(_ asset: String) {
        guard finishedHere[asset] != nil, (try? store.hasPending(assetId: asset)) == false else { return }
        finishedHere.removeValue(forKey: asset)
    }
    /// Finished, as on the Mac (PudgeCore AudiobookFinish): this device's unsent Finished / Mark
    /// unfinished, else the Mac's flag (its answer of this launch, else its Finished kept from an
    /// earlier one), else the place here.
    func isFinished(_ book: AudioManifest) -> Bool {
        let position = player.book?.assetId == book.assetId
            ? player.positionMs
            : ((try? store.localPosition(assetId: book.assetId, editionId: book.editionId)) ?? 0)
        return AudiobookFinish.isFinished(local: finishedHere[book.assetId], mac: progress[book.assetId]?.finished ?? macFinished.finished(book.assetId),
                                          positionMs: position, durationMs: book.durationMs)
    }
    /// The light novel linked to a book on the Mac (Read and listen), from the light-novel list.
    func linkedNovel(_ book: AudioManifest) -> LightNovelItem? {
        if let id = book.lightNovelId, let item = media.novels.first(where: { $0.id == id }) { return item }
        return media.novels.first { $0.audioAssetIds?.contains(book.assetId) == true }
    }
    /// A finished book's linked light novel that is not marked read yet (nil after Not now).
    func novelToFinish(_ book: AudioManifest) -> LightNovelItem? {
        guard !novelOfferDismissed.contains(book.assetId), isFinished(book),
              let novel = linkedNovel(book), !novel.finished else { return nil }
        return novel
    }
    func dismissNovelOffer(_ book: AudioManifest) {
        novelOfferDismissed.insert(book.assetId)
        UserDefaults.standard.set(Array(novelOfferDismissed), forKey: "pudge.novelOfferDismissed")
    }
    func markNovelFinished(_ novel: LightNovelItem) async {
        if await media.markNovelFinished(novel) { objectWillChange.send() }
        else { message = "The light novel was not marked read: the Mac did not answer." }
    }
    /// Read-along text of a book, loaded once per downloaded resource.
    func readAlong(_ book: AudioManifest) -> ReadAlongDocument? {
        guard let resource = book.readAlong, let local = try? store.resource(resource.resourceId), local.state.verified else { return nil }
        if let cached = readAlongCache[resource.resourceId] { return cached }
        guard let document = try? ReadAlongDocument.load(local.url) else { return nil }
        // Only the book open now is kept: a text is up to tens of MB, and every book opened (and
        // every newer text the Mac parsed, a new resource) stayed in memory until the app ended.
        readAlongCache = [resource.resourceId: document]
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
            // Hashing a whole part takes seconds: not on the main thread (the app froze meanwhile).
            let url = local.url, sha = part.sha256, size = part.sizeBytes
            try await Task.detached(priority: .utility) { try FileIntegrity.verify(url, sha256: sha, sizeBytes: size) }.value
            let file = try FileHandle(forReadingFrom: local.url); defer { try? file.close() }
            var offset = plan.offsets[part.resourceId] ?? 0
            guard offset >= 0 && offset <= part.sizeBytes else { throw PudgeError.invalid("Invalid archive offset") }
            try file.seek(toOffset: UInt64(offset))
            while offset < part.sizeBytes {
                guard let chunk = try file.read(upToCount: 1024 * 1024), !chunk.isEmpty else { throw PudgeError.invalid("The file changed") }
                var request = try connection.request("/api/v2/archives/\(plan.transferId)/resources/\(part.resourceId)?offset=\(offset)", method: "PUT", data: chunk)
                request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
                let (data, response) = try await connection.data(for: request)
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
                // evict hashes the part first: off the main thread (the app froze meanwhile).
                let library = self.store, resourceId = part.resourceId, generation = copy.state.generation
                try await Task.detached(priority: .utility) {
                    try library.evict(resourceId: resourceId, expectedGeneration: generation, receipt: receipt, nowMs: observed, verifiedLocallyAt: observed)
                }.value
            }
            reloadLocal()
            // Freed here: the Mac's card no longer shows this device.
            inventoryChanged()
        } catch { message = error.localizedDescription; reloadLocal() }
    }
    func resolve(_ book: AudioManifest, checkpointId: String) async {
        do {
            guard let connection, let state = progress[book.assetId] else { return }
            if player.book?.assetId == book.assetId { player.stop(); await sync() }
            let latest = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
            struct Resolution: Encodable { let assetId: String; let expectedRevision: Int64; let checkpointId: String }
            struct Receipt: Decodable { let revision: Int64 }
            guard latest.revision == state.revision else { noteMacProgress(book.assetId, latest); throw PudgeError.invalid("The position changed; choose again") }
            _ = try await connection.post("/api/v2/progress/resolve", value: Resolution(assetId: book.assetId, expectedRevision: latest.revision, checkpointId: checkpointId), as: Receipt.self)
            let selected = try await connection.send("/api/v2/assets/\(book.assetId)/progress", as: ProgressState.self)
            noteMacProgress(book.assetId, selected); try store.adopt(selected, book: book)
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
        // Files copied so far: removed again when the import fails (they stayed, with their rows).
        var copied: [(id: String, url: URL)] = []
        do {
            var parts: [AudioPart] = []; var offset: Int64 = 0
            var chapters: [AudioChapter] = []; var cover: CoverResource?
            let folder = store.directory.appendingPathComponent("resources")
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            for url in urls {
                // Only the formats an audiobook may hold: the picker also offers AIFF, CAF and
                // audio-only MP4, which played here but the library refused after copying them.
                guard AudioManifest.audioExtensions.contains(url.pathExtension.lowercased()) else {
                    throw PudgeError.invalid("“\(url.lastPathComponent)”: this format cannot be imported on this \(PudgeTheme.device) (MP3, M4A, M4B, AAC, WAV, FLAC, Opus or Ogg). Import the file on the Mac to prepare it.")
                }
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
                        copied.append((coverId, coverURL))
                        try jpeg.write(to: coverURL, options: .atomic)
                        cover = CoverResource(resourceId: coverId, sha256: try FileIntegrity.sha256(coverURL), sizeBytes: Int64(jpeg.count), fileName: "cover.jpg")
                        try store.markVerified(cover!.downloadPart, at: coverURL, original: true)
                    }
                }
                guard duration.seconds.isFinite && duration.seconds > 0 else { throw PudgeError.invalid("Could not determine the duration") }
                let id = UUID().uuidString.lowercased()
                let destination = folder.appendingPathComponent(id + "." + url.pathExtension.lowercased())
                copied.append((id, destination))
                // Copying and hashing a whole audiobook part takes seconds: not on the main thread
                // (the app froze meanwhile); markVerified takes that hash instead of hashing again.
                let library = self.store, partMs = Int64((duration.seconds * 1000).rounded()), partOffset = offset
                let part = try await Task.detached(priority: .userInitiated) { () throws -> AudioPart in
                    try FileManager.default.copyItem(at: url, to: destination)
                    try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: destination.path)
                    var file = destination
                    var policy = URLResourceValues(); policy.isExcludedFromBackup = false; try file.setResourceValues(policy)
                    let sha = try FileIntegrity.sha256(file)
                    let size = Int64(try file.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0)
                    let made = AudioPart(partId: UUID().uuidString.lowercased(), resourceId: id, sha256: sha, sizeBytes: size,
                                         fileName: url.lastPathComponent, offsetMs: partOffset, durationMs: partMs, playbackDurationMs: nil)
                    try library.markVerified(made, at: file, original: true, hashed: sha)
                    return made
                }.value
                parts.append(part); offset += part.durationMs
            }
            let book = AudioManifest(protocolMajor: 2, assetId: UUID().uuidString.lowercased(), editionId: UUID().uuidString.lowercased(),
                                     timelineRevision: UUID().uuidString.lowercased(), title: urls.first?.deletingPathExtension().lastPathComponent ?? "Audiobook",
                                     durationMs: offset, parts: parts, chapters: chapters, cover: cover)
            try store.saveManifest(book); reloadLocal()
        } catch {
            for file in copied { try? FileManager.default.removeItem(at: file.url) }
            try? store.forgetResources(copied.map { $0.id })
            message = error.localizedDescription
        }
    }
}


/// Small rotating text log for "Export diagnostics" (no tokens, no file contents).
enum AppLog {
    private static let queue = DispatchQueue(label: "pudge.applog")
    /// One formatter for every line (ISO8601DateFormatter is thread-safe; making one per line was costly).
    private static let stamp = ISO8601DateFormatter()
    static var folder: URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("PudgeAudio/logs", isDirectory: true)
    }
    static func write(_ category: String, _ text: String) {
        let line = stamp.string(from: Date()) + " [" + category + "] " + text + "\n"
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
    var summary = StorageSummary()
    var free: Int64 = 0
    subscript(kind: StorageKind) -> Int64 { summary[kind] }
    var total: Int64 { summary.total }
}

extension PudgeModel {
    /// "0.8.0 (1) · 1a2b3c4d5e6f": the app's version, build and the commit it was built from.
    static var appVersion: String {
        let info = Bundle.main.infoDictionary
        let version = (info?["CFBundleShortVersionString"] as? String) ?? "?"
        let build = (info?["CFBundleVersion"] as? String) ?? "?"
        let commit = (info?["PudgeGitCommit"] as? String).flatMap { $0.isEmpty || $0 == "unknown" ? nil : $0 }
        return version + " (" + build + ")" + (commit.map { " · " + $0 } ?? "")
    }

    /// Where Pudge's files are: its data folder, the temporary folder (a download cut off when
    /// the app ended leaves URLSession's file there) and the background session's folder.
    private var storageRoots: [(String, URL)] {
        var roots = [("store", store.directory), ("tmp", FileManager.default.temporaryDirectory)]
        if let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first {
            roots.append(("sessions", caches.appendingPathComponent("com.apple.nsurlsessiond", isDirectory: true)))
        }
        return roots
    }

    nonisolated static func storedFiles(_ roots: [(String, URL)]) -> [StoredFile] {
        let manager = FileManager.default
        let keys: [URLResourceKey] = [.fileSizeKey, .isRegularFileKey, .contentModificationDateKey]
        var files: [StoredFile] = []
        for (name, root) in roots {
            let base = root.resolvingSymlinksInPath().path + "/"
            guard let walker = manager.enumerator(at: root, includingPropertiesForKeys: keys) else { continue }
            for case let url as URL in walker {
                guard let values = try? url.resourceValues(forKeys: Set(keys)), values.isRegularFile == true else { continue }
                let path = url.resolvingSymlinksInPath().path
                guard path.hasPrefix(base) else { continue }
                files.append(StoredFile(path: name + "/" + String(path.dropFirst(base.count)), bytes: Int64(values.fileSize ?? 0),
                                        modified: values.contentModificationDate?.timeIntervalSince1970 ?? 0))
            }
        }
        return files
    }

    nonisolated static func remove(_ files: [StoredFile], roots: [(String, URL)]) -> (count: Int, bytes: Int64) {
        var count = 0, bytes: Int64 = 0
        for file in files {
            guard let slash = file.path.firstIndex(of: "/"), let root = roots.first(where: { $0.0 == file.path[..<slash] })?.1 else { continue }
            let relative = String(file.path[file.path.index(after: slash)...])
            guard !relative.split(separator: "/").contains("..") else { continue }
            if (try? FileManager.default.removeItem(at: root.appendingPathComponent(relative))) != nil { count += 1; bytes += file.bytes }
        }
        return (count, bytes)
    }

    /// What every stored file is checked against (nil: the store could not be read, nothing is
    /// called a leftover then).
    func storageReferences() -> StorageReferences? {
        guard let audio = try? store.resourcePaths(), let records = try? store.transfers() else { return nil }
        let active = records.contains { ["queued", "running", "verifying"].contains($0.status) }
        return StorageReferences(episodes: Set(media.localEpisodes.keys), episodeMarks: media.episodeMarkIds,
                                 readingFolders: Set(media.readingDownloads.values.map { "\($0.kind)-\($0.id)" }),
                                 audioFiles: audio, downloading: active || media.downloadsInProgress,
                                 audioResumable: active || records.contains { $0.resumeData != nil })
    }

    /// What Pudge keeps on this iPhone/iPad, measured from the files themselves; every byte is in
    /// a list (episodes, volumes, books) or under "Other files".
    func storageUsage() -> StorageUsage {
        // Unreadable store: everything stays where it is, shown as other files nobody may clear.
        let refs = storageReferences() ?? StorageReferences(downloading: true, audioResumable: true)
        return Self.storageUsage(directory: store.directory, roots: storageRoots, refs: refs)
    }
    /// The same with the folders walked off the main thread (Settings → Storage).
    func loadStorageUsage() async -> StorageUsage {
        let refs = storageReferences() ?? StorageReferences(downloading: true, audioResumable: true)
        let directory = store.directory, roots = storageRoots
        return await Task.detached(priority: .utility) {
            PudgeModel.storageUsage(directory: directory, roots: roots, refs: refs)
        }.value
    }
    nonisolated private static func storageUsage(directory: URL, roots: [(String, URL)], refs: StorageReferences) -> StorageUsage {
        var usage = StorageUsage()
        if let values = try? directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
           let free = values.volumeAvailableCapacityForImportantUsage { usage.free = free }
        usage.summary = StorageScan.summarize(storedFiles(roots), refs)
        return usage
    }

    /// Settings → Storage → Other files → Clear: leftovers no list reaches and no transfer needs.
    func clearOtherFiles() {
        guard let refs = storageReferences() else { message = "Could not read this \(PudgeTheme.device)'s downloads"; return }
        let roots = storageRoots
        let summary = StorageScan.summarize(Self.storedFiles(roots), refs)
        let removed = Self.remove(summary.clearable, roots: roots)
        URLCache.shared.removeAllCachedResponses()
        AppLog.write("storage", "other files cleared: \(removed.count) (\(PudgeTheme.size(removed.bytes)))")
        objectWillChange.send()
    }

    /// At launch: watched downloads past their delay, then files no record references (a video
    /// whose record went, interrupted downloads, URLSession's leftovers), never what downloads now.
    func launchCleanup() {
        guard !launchCleanupDone else { return }
        launchCleanupDone = true
        media.deleteWatchedEpisodes()
        guard let refs = storageReferences() else { return }
        let roots = storageRoots
        Task.detached(priority: .utility) {
            let files = PudgeModel.storedFiles(roots)
            let leftovers = StorageScan.launchCleanup(files, refs)
            guard !leftovers.isEmpty else { return }
            let removed = PudgeModel.remove(leftovers, roots: roots)
            let names = leftovers.prefix(5).map(\.path).joined(separator: ", ")
            AppLog.write("storage", "launch: removed \(removed.count) leftover file(s), \(PudgeTheme.size(removed.bytes)): " + names)
        }
    }

    /// Downloaded bytes of one book (verified local parts only).
    func downloadedBytes(_ book: AudioManifest) -> Int64 {
        book.parts.reduce(Int64(0)) { sum, part in
            guard let local = try? store.resource(part.resourceId), local.state.verified,
                  FileManager.default.fileExists(atPath: local.url.path) else { return sum }
            return sum + part.sizeBytes
        }
    }
    /// Plain-text report for debugging: state summary + recent log. No credentials.
    func diagnosticsFile() throws -> URL {
        var lines: [String] = []
        lines.append("Pudge \(PudgeTheme.device) diagnostics " + ISO8601DateFormatter().string(from: Date()))
        lines.append("App " + Self.appVersion)
        lines.append("iOS " + UIDevice.current.systemVersion + " · " + UIDevice.current.model)
        if let mac = paired {
            lines.append("Mac: " + mac.serverName + " · " + mac.baseURL + " · available=" + String(macAvailable) + " · " + macLink.status)
            lines.append("Mac capabilities: " + (macCapabilities.isEmpty ? "-" : macCapabilities.sorted().joined(separator: ", ")))
        } else { lines.append("Mac: not paired") }
        if let issue = syncIssue { lines.append("Sync issue: " + issue) }
        lines.append("Pending checkpoints: " + String((try? store.pendingCheckpoints().count) ?? -1))
        lines.append("Changes the Mac refused (set aside): " + String((try? store.parkedCount()) ?? -1))
        let unreadable = (try? store.unreadableManifests()) ?? []
        if !unreadable.isEmpty { lines.append("Saved audiobooks that cannot be read (not listed): " + unreadable.joined(separator: ", ")) }
        if !skippedNoted.isEmpty { lines.append("Audiobooks of the Mac's catalog left out: " + skippedNoted.joined(separator: "; ")) }
        lines.append("Pending statistics: " + String((try? store.pendingActivityCount()) ?? -1))
        let rejected = (try? store.rejectedActivity(limit: 2000)) ?? []
        lines.append("Statistics not taken by the Mac: " + String(rejected.count))
        for (reason, count) in Dictionary(grouping: rejected, by: \.reason).mapValues(\.count).sorted(by: { $0.key < $1.key }) {
            lines.append("- " + reason + ": " + String(count))
        }
        let usage = storageUsage()
        lines.append("Storage: " + StorageKind.allCases.filter { usage[$0] > 0 }.map { $0.rawValue + " " + PudgeTheme.size(usage[$0]) }
            .joined(separator: ", ") + ", total " + PudgeTheme.size(usage.total) + ", free " + PudgeTheme.size(usage.free))
        for file in usage.summary.other.sorted(by: { $0.bytes > $1.bytes }).prefix(20) {
            lines.append("- other: " + file.path + " · " + PudgeTheme.size(file.bytes))
        }
        let cleanup = media.watchedCleanupChoice
        lines.append("Delete watched episodes: " + cleanup.rawValue + " · Mac " + (media.macDeleteWatchedHours.map { String(format: "%g h", $0) } ?? "not sent"))
        lines.append("")
        lines.append("Episodes on this \(PudgeTheme.device) (\(media.localEpisodes.count)):")
        for item in media.localEpisodes.values.sorted(by: { $0.id < $1.id }) {
            let deletion = media.watchedDeletion(item.id).map { " · " + WatchedCleanup.label(deleteAt: $0, now: Date().timeIntervalSince1970) } ?? ""
            lines.append("- \(item.id) · " + item.title + " #\(item.number) · " + PudgeTheme.size(item.sizeBytes)
                + " · at " + PudgeTheme.time(item.positionMs) + "/" + PudgeTheme.time(item.durationMs)
                + " · watched=" + (item.watchedSent.map { $0 ? "sent" : "unsent" } ?? "no") + (item.kept == true ? " · kept" : "") + deletion)
        }
        let reading = media.readingDownloads.values.sorted { $0.key < $1.key }
        lines.append("Light novels and manga on this \(PudgeTheme.device) (\(reading.count)):")
        for record in reading { lines.append("- " + record.key + " · " + record.title + " · " + PudgeTheme.size(record.sizeBytes)) }
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
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("pudge-" + PudgeTheme.device.lowercased() + "-diagnostics-" + String(Int(Date().timeIntervalSince1970)) + ".txt")
        try lines.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
        return url
    }
}

// MARK: Downloads on this device, for the Mac's library

extension PudgeModel {
    /// What is downloaded here for the paired Mac (DeviceInventory): its episodes and volumes
    /// (not those of a previous Mac) and the audiobooks of its catalog downloaded completely.
    func currentInventory() -> DeviceInventory? {
        guard let server = paired?.serverId else { return nil }
        let media = self.media
        let episodes = media.localEpisodes.values.filter { !media.isOtherMac($0) }.map { $0.id }
        let reading = media.readingDownloads.values.filter { !media.isOtherMac($0) && $0.deletedOnMacAt == nil }
        let audiobooks = books.filter { book in
            (try? store.ready(book)) == true && (try? store.isRemote(book, serverId: server)) == true
        }.map { $0.assetId }
        return DeviceInventory(isPad: UIDevice.current.userInterfaceIdiom == .pad, episodes: episodes,
                               lightNovels: reading.filter { $0.kind == "ln" }.map { $0.id },
                               manga: reading.filter { $0.kind == "manga" }.map { $0.id },
                               audiobooks: audiobooks)
    }

    /// Downloads finished or were removed here: the Mac hears about it once they settle.
    func inventoryChanged() {
        inventoryTask?.cancel()
        inventoryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(InventoryReportPolicy.debounce * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.reportInventory()
        }
    }

    /// Sends the inventory when the Mac has not got this one yet (or long ago). A failure is only
    /// logged: the next refresh or download sends it again.
    func reportInventory() async {
        // A report in flight could be marked sent after a newer one: one at a time, then again.
        if inventorySending { inventoryAgain = true; return }
        inventorySending = true
        defer { inventorySending = false }
        repeat {
            inventoryAgain = false
            await sendInventory()
        } while inventoryAgain
    }

    private func sendInventory() async {
        guard let connection, macAvailable, macSupports("inventory"), let server = paired?.serverId,
              let inventory = currentInventory() else { return }
        let now = Date().timeIntervalSince1970
        guard inventoryPolicy.needsSending(inventory, serverId: server, now: now) else { return }
        struct Receipt: Decodable { let ok: Bool? }
        do {
            _ = try await connection.post(DeviceInventory.route, value: inventory, as: Receipt.self)
            inventoryPolicy.markSent(inventory, serverId: server, at: now)
            AppLog.write("sync", "downloads reported to the Mac: \(inventory.count) item(s)")
        } catch {
            AppLog.write("sync", "downloads not reported to the Mac: \(error.localizedDescription)")
        }
    }

    /// Episodes, volumes and audiobooks downloaded or removed report the inventory again.
    func watchInventory() {
        guard inventoryWatch.isEmpty else { return }
        media.$localEpisodes.map { Set($0.keys) }.removeDuplicates().dropFirst()
            .sink { [weak self] _ in self?.inventoryChanged() }.store(in: &inventoryWatch)
        media.$readingDownloads.map { Set($0.keys) }.removeDuplicates().dropFirst()
            .sink { [weak self] _ in self?.inventoryChanged() }.store(in: &inventoryWatch)
        $transfers.map { records in records.map { $0.id + ":" + $0.status } }.removeDuplicates().dropFirst()
            .sink { [weak self] _ in self?.inventoryChanged() }.store(in: &inventoryWatch)
    }
}
