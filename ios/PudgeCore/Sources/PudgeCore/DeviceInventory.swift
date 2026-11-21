import Foundation

/// What this iPhone/iPad has downloaded, reported to the Mac (`POST /api/v2/inventory`, Macs
/// with the "inventory" capability) so the Mac's library shows which devices have a title.
/// The ids are the phone API's: episode ids, light-novel and manga volume ids, audiobook asset
/// ids. The whole inventory goes every time; the Mac keeps the last report of each device and
/// drops it when the device is revoked.
public struct DeviceInventory: Codable, Equatable, Sendable {
    public static let route = "/api/v2/inventory"
    /// At most this many ids of a kind (the Mac refuses a longer list).
    public static let limit = 5000
    /// "phone" or "pad" (the Mac shows an iPhone or an iPad glyph).
    public let idiom: String
    public let episodes: [Int]
    public let lightNovels: [Int]
    public let manga: [Int]
    public let audiobooks: [String]

    public init(isPad: Bool, episodes: [Int], lightNovels: [Int], manga: [Int], audiobooks: [String]) {
        idiom = isPad ? "pad" : "phone"
        self.episodes = Self.macIds(episodes)
        self.lightNovels = Self.macIds(lightNovels)
        self.manga = Self.macIds(manga)
        // The Mac takes UUIDs only (one other value refused the whole report).
        let assets = Set(audiobooks.compactMap { UUID(uuidString: $0)?.uuidString.lowercased() })
        self.audiobooks = Array(assets.sorted().prefix(Self.limit))
    }

    /// The Mac's numbers only: downloads of a previous Mac live below zero here (DownloadOwner).
    static func macIds(_ values: [Int]) -> [Int] {
        Array(Set(values.filter { $0 > 0 }).sorted().prefix(limit))
    }

    public var count: Int { episodes.count + lightNovels.count + manga.count + audiobooks.count }
}

/// When the inventory goes to the Mac: a changed one (after the downloads settle for
/// `debounce` seconds), the same one again only after `refresh` (the Mac marks a report older
/// than a week "as of <date>"), and always to another Mac or after pairing again.
public struct InventoryReportPolicy: Equatable, Sendable {
    public static let debounce: TimeInterval = 3
    public static let refresh: TimeInterval = 6 * 3600
    public private(set) var sent: DeviceInventory?
    public private(set) var sentAt: Double = 0
    public private(set) var serverId: String?

    public init() {}

    public func needsSending(_ inventory: DeviceInventory, serverId: String, now: Double) -> Bool {
        guard let sent, self.serverId == serverId else { return true }
        // A clock set back counts as old: sent again rather than never.
        return sent != inventory || now - sentAt >= Self.refresh || now < sentAt
    }

    /// The Mac confirmed this inventory.
    public mutating func markSent(_ inventory: DeviceInventory, serverId: String, at now: Double) {
        sent = inventory; sentAt = now; self.serverId = serverId
    }

    /// Paired again (the Mac dropped the old report with the old access) or disconnected.
    public mutating func reset() { sent = nil; sentAt = 0; serverId = nil }
}
