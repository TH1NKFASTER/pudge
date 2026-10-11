import Foundation

public enum PudgeError: Error, LocalizedError {
    case invalid(String)
    public var errorDescription: String? {
        switch self { case .invalid(let text): return text }
    }
}

public struct AudioPart: Codable, Hashable, Identifiable {
    public var partId: String
    public var resourceId: String
    public var sha256: String
    public var sizeBytes: Int64
    public var fileName: String
    public var offsetMs: Int64
    public var durationMs: Int64
    public var playbackDurationMs: Int64?
    public var id: String { partId }
    public init(partId: String, resourceId: String, sha256: String, sizeBytes: Int64, fileName: String,
                offsetMs: Int64, durationMs: Int64, playbackDurationMs: Int64? = nil) {
        self.partId = partId; self.resourceId = resourceId; self.sha256 = sha256; self.sizeBytes = sizeBytes
        self.fileName = fileName; self.offsetMs = offsetMs; self.durationMs = durationMs; self.playbackDurationMs = playbackDurationMs
    }
}

public struct AudioChapter: Codable, Hashable, Identifiable {
    public var title: String
    public var startMs: Int64
    public var endMs: Int64
    public var id: Int64 { startMs }
    public init(title: String, startMs: Int64, endMs: Int64) { self.title = title; self.startMs = startMs; self.endMs = endMs }
}

public struct CoverResource: Codable, Hashable {
    public var resourceId: String
    public var sha256: String
    public var sizeBytes: Int64
    public var fileName: String
    public init(resourceId: String, sha256: String, sizeBytes: Int64, fileName: String) {
        self.resourceId = resourceId; self.sha256 = sha256; self.sizeBytes = sizeBytes; self.fileName = fileName
    }
    public var downloadPart: AudioPart { AudioPart(partId: resourceId, resourceId: resourceId, sha256: sha256,
        sizeBytes: sizeBytes, fileName: fileName, offsetMs: 0, durationMs: 1) }
}

public struct AudioManifest: Codable, Hashable, Identifiable {
    public var protocolMajor: Int
    public var assetId: String
    public var editionId: String
    public var timelineRevision: String
    public var title: String
    public var durationMs: Int64
    public var parts: [AudioPart]
    public var chapters: [AudioChapter]
    public var cover: CoverResource?
    /// Series grouping from the Mac library (optional; imported books have none).
    public var seriesTitle: String? = nil
    public var volume: Int? = nil
    /// Linked light-novel text on the audiobook clock (optional, from the Mac).
    public var readAlong: CoverResource? = nil
    /// Light novel linked on the Mac (Read and listen); opens the reader with the audio.
    public var lightNovelId: Int? = nil
    public var id: String { assetId + ":" + editionId }
    public var resources: [AudioPart] { parts + (cover.map { [$0.downloadPart] } ?? []) + (readAlong.map { [$0.downloadPart] } ?? []) }
    public var sizeBytes: Int64 { resources.reduce(0) { $0 + $1.sizeBytes } }
    public init(protocolMajor: Int = 2, assetId: String, editionId: String, timelineRevision: String,
                title: String, durationMs: Int64, parts: [AudioPart], chapters: [AudioChapter], cover: CoverResource? = nil) {
        self.protocolMajor = protocolMajor; self.assetId = assetId; self.editionId = editionId
        self.timelineRevision = timelineRevision; self.title = title; self.durationMs = durationMs
        self.parts = parts; self.chapters = chapters; self.cover = cover
    }

    public static func decode(_ data: Data) throws -> AudioManifest {
        let value = try JSONDecoder().decode(Self.self, from: data)
        try value.validate()
        return value
    }
    public func validate() throws {
        guard protocolMajor == 2, UUID(uuidString: assetId) != nil, UUID(uuidString: editionId) != nil,
              !title.isEmpty, title.count <= 1024, !timelineRevision.isEmpty, timelineRevision.count <= 128,
              durationMs > 0, durationMs <= 9_007_199_254_740_991, !parts.isEmpty, parts.count <= 10000 else {
            throw PudgeError.invalid("Unsupported audiobook version")
        }
        var offset: Int64 = 0
        var total: Int64 = 0
        var ids = Set<String>()
        for part in parts {
            guard UUID(uuidString: part.partId) != nil, UUID(uuidString: part.resourceId) != nil,
                  ids.insert(part.partId).inserted, part.offsetMs == offset, part.durationMs > 0,
                  part.durationMs <= durationMs, part.sizeBytes > 0, part.sizeBytes <= 9_007_199_254_740_991,
                  part.sha256.count == 64, part.sha256.allSatisfy({ "0123456789abcdef".contains($0) }),
                  !part.fileName.isEmpty, part.fileName.count <= 240, !part.fileName.contains("/"),
                  !part.fileName.contains("\\"), !part.fileName.contains("\0"),
                  ["mp3", "m4a", "m4b", "aac", "wav", "flac", "opus", "ogg"].contains((part.fileName as NSString).pathExtension.lowercased()) else {
                throw PudgeError.invalid("Invalid audio file map")
            }
            let sum = total.addingReportingOverflow(part.sizeBytes)
            guard !sum.overflow, sum.partialValue <= 9_007_199_254_740_991,
                  (part.playbackDurationMs ?? part.durationMs) > 0 else { throw PudgeError.invalid("Invalid size or duration") }
            total = sum.partialValue
            let next = offset.addingReportingOverflow(part.durationMs)
            guard !next.overflow else { throw PudgeError.invalid("Invalid duration") }
            offset = next.partialValue
        }
        if let cover {
            guard UUID(uuidString: cover.resourceId) != nil, cover.sha256.count == 64,
                  cover.sha256.allSatisfy({ "0123456789abcdef".contains($0) }), cover.sizeBytes > 0,
                  cover.sizeBytes <= 10 * 1024 * 1024, cover.fileName.count <= 240,
                  !cover.fileName.contains("/"), !cover.fileName.contains("\\"), !cover.fileName.contains("\0"),
                  ["jpg", "jpeg", "png"].contains((cover.fileName as NSString).pathExtension.lowercased()),
                  !parts.contains(where: { $0.resourceId == cover.resourceId }), total <= 9_007_199_254_740_991 - cover.sizeBytes else {
                throw PudgeError.invalid("Invalid cover")
            }
        }
        if let readAlong {
            guard UUID(uuidString: readAlong.resourceId) != nil, readAlong.sha256.count == 64,
                  readAlong.sha256.allSatisfy({ "0123456789abcdef".contains($0) }), readAlong.sizeBytes > 0,
                  readAlong.sizeBytes <= 50 * 1024 * 1024, readAlong.fileName == "readalong.json",
                  !parts.contains(where: { $0.resourceId == readAlong.resourceId }), readAlong.resourceId != cover?.resourceId else {
                throw PudgeError.invalid("Invalid read-along")
            }
        }
        guard offset == durationMs, chapters.count <= 10000,
              chapters.allSatisfy({ $0.startMs >= 0 && $0.endMs >= $0.startMs && $0.endMs <= durationMs }) else {
            throw PudgeError.invalid("Invalid chapter map")
        }
    }
}

public struct AudioTimeline {
    public let book: AudioManifest
    public init(_ book: AudioManifest) { self.book = book }
    public func locate(_ positionMs: Int64) throws -> (index: Int, localMs: Int64) {
        guard positionMs >= 0, positionMs <= book.durationMs else { throw PudgeError.invalid("Position outside the audiobook") }
        let index = book.parts.lastIndex(where: { $0.offsetMs <= positionMs }) ?? 0
        return (index, positionMs - book.parts[index].offsetMs)
    }
}

public struct AudioCheckpoint: Codable, Hashable, Identifiable {
    public var checkpointId: String
    public var assetId: String
    public var editionId: String
    public var timelineRevision: String
    public var playbackSessionId: String
    public var deviceId: String
    public var sequence: Int64
    public var baseProgressRevision: Int64
    public var positionMs: Int64
    public var partId: String
    public var partPositionMs: Int64
    public var reason: String
    public var observedAt: Int64
    public var id: String { checkpointId }

    public static func make(book: AudioManifest, deviceId: String, sessionId: String,
                            sequence: Int64, revision: Int64, positionMs: Int64, reason: String) throws -> Self {
        let location = try AudioTimeline(book).locate(positionMs)
        return Self(checkpointId: UUID().uuidString.lowercased(), assetId: book.assetId,
                    editionId: book.editionId, timelineRevision: book.timelineRevision,
                    playbackSessionId: sessionId, deviceId: deviceId, sequence: sequence,
                    baseProgressRevision: revision, positionMs: positionMs,
                    partId: book.parts[location.index].partId, partPositionMs: location.localMs,
                    reason: reason, observedAt: Int64(Date().timeIntervalSince1970 * 1000))
    }
}

/// Reading or watching time measured on the iPhone for the Mac's Statistics.
/// `from`/`to` are the positions at both ends: light novels ["chapter", "fraction"],
/// manga ["page"], video ["seconds"]. Times are seconds since 1970 on the phone's clock.
public struct ActivityInterval: Codable, Equatable, Identifiable {
    public var id: String
    public var kind: String
    public var item: Int
    public var start: Double
    public var end: Double
    public var active: Double
    public var from: [String: Double]
    public var to: [String: Double]
    public var timezone: String
    public var offset: Int
    public static let kinds: Set<String> = ["light_novel", "manga", "anime"]
    public init(kind: String, item: Int, start: Date, end: Date, active: Double,
                from: [String: Double], to: [String: Double], timeZone: TimeZone = .current) {
        id = UUID().uuidString.lowercased(); self.kind = kind; self.item = item
        self.start = start.timeIntervalSince1970; self.end = end.timeIntervalSince1970; self.active = active
        self.from = from; self.to = to
        timezone = timeZone.identifier; offset = timeZone.secondsFromGMT(for: end)
    }
    /// Same limits as the Mac: at most 30 s per interval, active time within the clock time.
    public func validate() throws {
        guard Self.kinds.contains(kind), item >= 0, start.isFinite, end.isFinite, active.isFinite,
              end >= start, end - start <= 30, active >= 0, active <= end - start + 2.5 else {
            throw PudgeError.invalid("Invalid activity interval")
        }
    }
}

public struct ProgressState: Codable {
    public var revision: Int64
    public var selected: AudioCheckpoint?
    public var conflicts: [AudioCheckpoint]
    public var finished: Bool
    /// Device that is playing this book right now ("mac" or a phone ID), per the Mac.
    public var playingOn: String?
    /// Its name ("Mac", "Maksim iPad"); several iPhones and iPads can be paired with one Mac.
    public var playingOnName: String? = nil
    /// Names of the iPhones/iPads behind `selected` and `conflicts` (device ID → name).
    public var deviceNames: [String: String]? = nil
    public init(revision: Int64, selected: AudioCheckpoint?, conflicts: [AudioCheckpoint], finished: Bool, playingOn: String? = nil) {
        self.revision = revision; self.selected = selected; self.conflicts = conflicts; self.finished = finished; self.playingOn = playingOn
    }
    /// How a position's device is shown: the Mac, this device, or the other device's name.
    public func deviceLabel(_ deviceId: String, this: String, thisLabel: String) -> String {
        if deviceId == "mac" { return "Mac" }
        if deviceId == this { return thisLabel }
        return deviceNames?[deviceId] ?? "Another device"
    }
}

public enum SynchronizationStatus: Equatable {
    case pending, conflict, synced, awaiting
    public static func evaluate(pending: Bool, checkpointId: String?, progress: ProgressState?) -> Self {
        if let progress, !progress.conflicts.isEmpty { return .conflict }
        if pending { return .pending }
        if let checkpointId, progress?.selected?.checkpointId == checkpointId { return .synced }
        return .awaiting
    }
}

public struct ArchiveReceipt: Codable {
    public var receiptId: String
    public var resourceId: String
    public var editionId: String
    public var sha256: String
    public var sizeBytes: Int64
    public var verifiedAt: Int64
    public var retained: Bool
    public init(receiptId: String, resourceId: String, editionId: String, sha256: String,
                sizeBytes: Int64, verifiedAt: Int64, retained: Bool) {
        self.receiptId = receiptId; self.resourceId = resourceId; self.editionId = editionId
        self.sha256 = sha256; self.sizeBytes = sizeBytes; self.verifiedAt = verifiedAt; self.retained = retained
    }
}

public struct LocalResource: Codable {
    public var resourceId: String
    public var sha256: String
    public var sizeBytes: Int64
    public var generation: Int64
    public var verified: Bool
    public var pinned: Bool
    public var active: Bool
    public var original: Bool
    public var transferring: Bool
    public init(resourceId: String, sha256: String, sizeBytes: Int64, generation: Int64,
                verified: Bool, pinned: Bool, active: Bool, original: Bool, transferring: Bool) {
        self.resourceId = resourceId; self.sha256 = sha256; self.sizeBytes = sizeBytes; self.generation = generation
        self.verified = verified; self.pinned = pinned; self.active = active; self.original = original; self.transferring = transferring
    }
}

public struct Bookmark: Codable, Identifiable {
    public var bookmarkId: String
    public var eventId: String
    public var assetId: String
    public var editionId: String
    public var positionMs: Int64
    public var title: String
    public var deleted: Bool
    public var baseRevision: Int64
    public var id: String { bookmarkId }
    public init(book: AudioManifest, positionMs: Int64, title: String) {
        bookmarkId = UUID().uuidString.lowercased(); eventId = UUID().uuidString.lowercased()
        assetId = book.assetId; editionId = book.editionId; self.positionMs = positionMs
        self.title = title; deleted = false; baseRevision = 0
    }
}
