import Foundation

/// The last answer of each heavy screen (Statistics per period, Planning) kept on the device, so
/// the screen shows it at once on open (offline too) while the Mac's answer comes quietly.
///
/// Bounded: one small file per screen, at most `maxTotalBytes` in all (the oldest go first),
/// none over `maxEntryBytes`; files of another cache `version` or another Mac are not used and
/// are deleted. Only the screen being shown is decoded into memory.
public struct ScreenCache {
    public static let version = 1
    public let directory: URL
    public let maxTotalBytes: Int
    public let maxEntryBytes: Int

    public init(directory: URL, maxTotalBytes: Int = 2 * 1024 * 1024, maxEntryBytes: Int = 512 * 1024) {
        self.directory = directory
        self.maxTotalBytes = maxTotalBytes
        self.maxEntryBytes = maxEntryBytes
    }

    struct Envelope<Value: Codable>: Codable {
        let version: Int
        let server: String?
        let savedAt: Double
        let value: Value
    }
    private struct Header: Codable {
        let version: Int
        let server: String?
        let savedAt: Double
    }

    /// A key as a file name ("stats-30d"): letters, digits, "-" and "_" only.
    public static func fileName(_ key: String) -> String? {
        let allowed = Set("abcdefghijklmnopqrstuvwxyz0123456789-_")
        guard !key.isEmpty, key.count <= 64, key.allSatisfy({ allowed.contains($0) }) else { return nil }
        return "\(key).v\(version).json"
    }

    /// The saved answer of `key` from Mac `server` and when it was saved (nil: none, another
    /// Mac's, another version or unreadable).
    public func load<Value: Codable>(_ key: String, server: String?, as type: Value.Type) -> (value: Value, savedAt: Date)? {
        guard let name = Self.fileName(key) else { return nil }
        let url = directory.appendingPathComponent(name)
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard let envelope = try? JSONDecoder().decode(Envelope<Value>.self, from: data),
              envelope.version == Self.version, envelope.server == server else {
            try? FileManager.default.removeItem(at: url)
            return nil
        }
        return (envelope.value, Date(timeIntervalSince1970: envelope.savedAt))
    }

    /// When `key` was saved (without decoding its value; nil: nothing usable saved).
    public func savedAt(_ key: String, server: String?) -> Date? {
        guard let name = Self.fileName(key),
              let data = try? Data(contentsOf: directory.appendingPathComponent(name)),
              let header = try? JSONDecoder().decode(Header.self, from: data),
              header.version == Self.version, header.server == server else { return nil }
        return Date(timeIntervalSince1970: header.savedAt)
    }

    /// Keeps `value` as the answer of `key`; false when it was too big (nothing is kept then).
    @discardableResult
    public func save<Value: Codable>(_ value: Value, key: String, server: String?, now: Date = Date()) -> Bool {
        guard let name = Self.fileName(key) else { return false }
        let url = directory.appendingPathComponent(name)
        guard let data = try? JSONEncoder().encode(Envelope(version: Self.version, server: server, savedAt: now.timeIntervalSince1970, value: value)),
              data.count <= maxEntryBytes else {
            try? FileManager.default.removeItem(at: url)
            return false
        }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
        } catch { return false }
        prune(keeping: name)
        return true
    }

    /// Files to delete so that the rest fits `maxTotal` (oldest first, never `keeping`), and
    /// every file of another version.
    public static func evictions(_ files: [(name: String, bytes: Int, modified: Date)], maxTotal: Int, keeping: String? = nil) -> [String] {
        let suffix = ".v\(version).json"
        var drop = files.filter { !$0.name.hasSuffix(suffix) }.map(\.name)
        let current = files.filter { $0.name.hasSuffix(suffix) }
        var total = current.reduce(0) { $0 + $1.bytes }
        for file in current.sorted(by: { ($0.modified, $0.name) < ($1.modified, $1.name) }) where total > maxTotal && file.name != keeping {
            drop.append(file.name)
            total -= file.bytes
        }
        return drop
    }

    public func prune(keeping: String? = nil) {
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey]
        guard let urls = try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: keys) else { return }
        let files: [(name: String, bytes: Int, modified: Date)] = urls.compactMap { url in
            guard let values = try? url.resourceValues(forKeys: Set(keys)), values.isRegularFile == true else { return nil }
            return (url.lastPathComponent, values.fileSize ?? 0, values.contentModificationDate ?? .distantPast)
        }
        for name in Self.evictions(files, maxTotal: maxTotalBytes, keeping: keeping) {
            try? FileManager.default.removeItem(at: directory.appendingPathComponent(name))
        }
    }

    /// Everything (another Mac was chosen).
    public func clear() { try? FileManager.default.removeItem(at: directory) }

    /// Whether a saved answer is old enough to be asked for again by a prefetch.
    public static func needsPrefetch(savedAt: Date?, now: Date = Date(), maxAge: TimeInterval = 600) -> Bool {
        guard let savedAt else { return true }
        return now.timeIntervalSince(savedAt) >= maxAge || savedAt > now.addingTimeInterval(60)
    }

    /// Seconds to wait before asking again while the Mac answers with its kept copy marked
    /// stale (it is rebuilding): nil after `attempt` tries, the shown copy then stays.
    public static func staleRetryDelay(attempt: Int) -> TimeInterval? {
        let delays: [TimeInterval] = [1.5, 3, 6]
        return attempt >= 0 && attempt < delays.count ? delays[attempt] : nil
    }
}
