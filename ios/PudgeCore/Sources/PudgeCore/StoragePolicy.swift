import Foundation

public enum StoragePolicy {
    public static func reserve(totalBytes: Int64) -> Int64 {
        min(5 * 1024 * 1024 * 1024, max(2 * 1024 * 1024 * 1024, totalBytes / 20))
    }
    public static func canDownload(totalBytes: Int64, freeBytes: Int64, queuedBytes: Int64, newBytes: Int64) -> Bool {
        guard newBytes >= 0, queuedBytes >= 0, newBytes <= Int64.max / 2 else { return false }
        let available = freeBytes - reserve(totalBytes: totalBytes)
        return available >= queuedBytes && available - queuedBytes >= 2 * newBytes
    }
    public static func canEvict(_ local: LocalResource, receipt: ArchiveReceipt?, expectedGeneration: Int64, nowMs: Int64, verifiedLocallyAt: Int64? = nil) -> Bool {
        guard let receipt else { return false }
        let observed = verifiedLocallyAt ?? receipt.verifiedAt
        return local.verified && !local.pinned && !local.active && !local.transferring
            && local.generation == expectedGeneration && receipt.retained
            && receipt.resourceId == local.resourceId && receipt.sha256 == local.sha256
            && receipt.sizeBytes == local.sizeBytes && observed >= 0 && observed <= nowMs
            && nowMs - observed <= 60000
    }
}
