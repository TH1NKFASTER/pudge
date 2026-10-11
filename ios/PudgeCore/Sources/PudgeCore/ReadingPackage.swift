import Foundation

/// A light novel or manga volume downloaded from the Mac as one verified file:
/// `"PUDGEPK1"`, the header length (8 bytes, big-endian), the header JSON
/// (`{"format":1,"files":[{"name","offset","size"}]}`, offsets after the header), the files.
public enum ReadingPackage {
    public struct Entry: Codable { public let name: String; public let offset: Int64; public let size: Int64 }
    struct Header: Codable { let format: Int; let files: [Entry] }
    static let magic = Data("PUDGEPK1".utf8)

    /// Plain file names only (no folders, no hidden files).
    public static func isSafe(_ name: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        return !name.isEmpty && name.count <= 120 && !name.hasPrefix(".")
            && name.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    /// Unpacks a downloaded package into `folder`, replacing an earlier copy only when every
    /// file was written. Returns the file names.
    @discardableResult
    public static func unpack(_ file: URL, into folder: URL) throws -> [String] {
        let damaged = PudgeError.invalid("The download is damaged; download it again")
        let manager = FileManager.default
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        guard let head = try handle.read(upToCount: 16), head.count == 16, head.prefix(8) == magic else { throw damaged }
        let length = head.suffix(8).reduce(UInt64(0)) { $0 << 8 | UInt64($1) }
        guard length > 0, length < 64 * 1024 * 1024, let data = try handle.read(upToCount: Int(length)), data.count == Int(length),
              let header = try? JSONDecoder().decode(Header.self, from: data) else { throw damaged }
        guard header.format == 1 else { throw PudgeError.invalid("Update Pudge on the iPhone to open this download") }
        let size = ((try? manager.attributesOfItem(atPath: file.path))?[.size] as? NSNumber)?.uint64Value ?? 0
        let base = 16 + length
        let temporary = folder.deletingLastPathComponent().appendingPathComponent(folder.lastPathComponent + ".unpacking", isDirectory: true)
        try? manager.removeItem(at: temporary)
        try manager.createDirectory(at: temporary, withIntermediateDirectories: true)
        do {
            var names = Set<String>()
            for entry in header.files {
                guard isSafe(entry.name), names.insert(entry.name).inserted, entry.offset >= 0, entry.size >= 0, base <= size,
                      UInt64(entry.offset) <= size - base, UInt64(entry.size) <= size - base - UInt64(entry.offset) else { throw damaged }
                try handle.seek(toOffset: base + UInt64(entry.offset))
                let target = temporary.appendingPathComponent(entry.name)
                guard manager.createFile(atPath: target.path, contents: nil) else { throw damaged }
                let output = try FileHandle(forWritingTo: target)
                defer { try? output.close() }
                var remaining = entry.size
                while remaining > 0 {
                    let count = Int(min(remaining, 1 << 20))
                    guard let chunk = try handle.read(upToCount: count), chunk.count == count else { throw damaged }
                    try output.write(contentsOf: chunk)
                    remaining -= Int64(count)
                }
            }
            try? manager.removeItem(at: folder)
            try manager.moveItem(at: temporary, to: folder)
            return header.files.map(\.name)
        } catch {
            try? manager.removeItem(at: temporary)
            throw error
        }
    }
}

/// The Mac's answer while it packs a volume (`media/<ln|manga>/<id>/package`).
public struct ReadingPackageState: Codable {
    public let state: String
    public let progress: Double
    public let message: String
    public let revision: String
    public let package: ResourceRef?
    /// Manga: the archive's and the OCR's own revisions ("" until the OCR is complete).
    public let pagesRevision: String?
    public let ocrRevision: String?
    public let ocrComplete: Bool?
}

/// The Mac's Mokuro OCR of a whole manga volume (`media/manga/<id>/ocr`).
public struct MangaOCRStatus: Codable {
    public let complete: Bool
    public let running: Bool
    public let state: String
    public let totalPages: Int
    public let completedPages: Int
    public let failedPages: Int
    public let progress: Double
    public let message: String
    public let ocrRevision: String
}

/// Current revisions of downloaded volumes (`media/downloads/check`); `missing`: gone from the Mac.
public struct ReadingRevisions: Codable {
    public struct Item: Codable {
        public let kind: String
        public let id: Int
        public let revision: String?
        public let pagesRevision: String?
        public let ocrRevision: String?
        public let ocrComplete: Bool?
        public let missing: Bool?
        /// When the volume was deleted on the Mac (seconds).
        public let deletedAt: Double?
    }
    public let items: [Item]

    /// A volume deleted on the Mac leaves the device a week later, unless it was opened since.
    public static let keepDeleted: Double = 7 * 86_400
    public static func removable(deletedAt: Double?, openedAt: Double?, now: Double) -> Bool {
        guard let deletedAt, now - deletedAt >= keepDeleted else { return false }
        return (openedAt ?? 0) < deletedAt
    }

    /// What a downloaded volume needs: nothing, its OCR only, or the whole volume again.
    public enum Update: Equatable { case none, ocr, volume }
    public static func update(_ item: Item, revision: String, pagesRevision: String?, ocrRevision: String?) -> Update {
        guard item.missing != true, let current = item.revision else { return .none }
        if item.kind == "manga", let pages = pagesRevision, let latest = item.pagesRevision {
            if pages != latest { return .volume }
            if item.ocrComplete == true, let ocr = item.ocrRevision, !ocr.isEmpty, ocr != (ocrRevision ?? "") { return .ocr }
            return .none
        }
        return current == revision ? .none : .volume
    }
}

/// `package.json` of a downloaded light novel.
public struct NovelPackage: Codable {
    public let revision: String
    public let title: String
    public let book: LightNovelDetail
    public let item: LightNovelItem?
}

/// `package.json` of a downloaded manga volume; `pages` are file names in page order.
public struct MangaPackage: Codable {
    public let revision: String
    public let title: String
    public let item: MangaItem
    public let pages: [String]
    /// The volume came with the Mac's complete OCR (text boxes and their words).
    public let ocr: Bool?
    public let pagesRevision: String?
    public let ocrRevision: String?
}

/// `parsed.json` of a manga volume: each OCR text box split into words on the Mac.
public struct ParsedRegions: Codable {
    public struct Item: Codable { public let text: String; public let paragraphs: [TextParagraph] }
    public let items: [Item]
}
