import Foundation

/// The last catalog the Mac sent (GET /api/v2/catalog), kept on disk with its ETag. The next
/// refresh (foreground, network change, "Mac available again") asks "changed since?"
/// (If-None-Match): an unchanged library is a body-less 304 and the kept answer is used again,
/// instead of every manifest being sent, decoded and saved once more. An older Mac answers in
/// full without an ETag, and nothing is kept.
public struct CatalogCache: Sendable {
    /// What the kept answer was for: the same route (lazy or not) of the same Mac.
    public struct Entry: Codable, Equatable, Sendable {
        public let route: String
        public let serverId: String
        public let etag: String
        public init(route: String, serverId: String, etag: String) {
            self.route = route; self.serverId = serverId; self.etag = etag
        }
    }
    /// The answer to decode, and whether it is the kept one (304: nothing changed on the Mac).
    public struct Answer: Equatable, Sendable {
        public let body: Data
        public let unchanged: Bool
    }

    public let directory: URL
    public init(directory: URL) { self.directory = directory }
    private var bodyFile: URL { directory.appendingPathComponent("catalog-cache.json") }
    private var entryFile: URL { directory.appendingPathComponent("catalog-cache.meta.json") }

    /// The kept answer's entry, when its body is still there.
    public func entry() -> Entry? {
        guard let data = try? Data(contentsOf: entryFile),
              let entry = try? JSONDecoder().decode(Entry.self, from: data),
              FileManager.default.fileExists(atPath: bodyFile.path) else { return nil }
        return entry
    }

    /// The ETag to send: only for a kept answer of the same route of the same Mac.
    public func etag(route: String, serverId: String?) -> String? {
        guard let serverId, let entry = entry(), entry.route == route, entry.serverId == serverId else { return nil }
        return entry.etag
    }

    /// The body the Mac sent (`fresh`), or nil for a 304 to `sentETag`: the kept body then.
    /// A 304 without a kept body (removed meanwhile) is an error: the caller asks again in full.
    public func answer(route: String, serverId: String?, fresh: Data?, sentETag: String?) throws -> Answer {
        if let fresh { return Answer(body: fresh, unchanged: false) }
        guard let sentETag, etag(route: route, serverId: serverId) == sentETag,
              let body = try? Data(contentsOf: bodyFile) else {
            throw PudgeError.invalid("The kept catalog is gone; asking the Mac again")
        }
        return Answer(body: body, unchanged: true)
    }

    /// Keep an answer once it was applied (a failure half-way must not be skipped next time).
    /// Without an ETag (an older Mac) nothing is kept.
    public func keep(route: String, serverId: String, etag: String?, body: Data) {
        guard let etag, !etag.isEmpty else { clear(); return }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            // The entry goes last: a body without one is never used.
            try? FileManager.default.removeItem(at: entryFile)
            try body.write(to: bodyFile, options: .atomic)
            try JSONEncoder().encode(Entry(route: route, serverId: serverId, etag: etag)).write(to: entryFile, options: .atomic)
        } catch { clear() }
    }

    /// Forget the kept answer (another Mac, a failed apply, an older Mac).
    public func clear() {
        try? FileManager.default.removeItem(at: entryFile)
        try? FileManager.default.removeItem(at: bodyFile)
    }
}
