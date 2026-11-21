import Foundation

/// POST /api/v2/sync/state takes at most `macLimit` books in one request: the Mac refuses a
/// longer list (HTTP 409), and every full ask (all books, every 5 minutes) of a larger library
/// failed and stopped that sync. A larger library is asked in batches of `size`.
public enum StateBatch {
    /// The Mac's IPhoneService.STATE_BATCH_LIMIT.
    public static let macLimit = 1000
    /// Books per request (well within the Mac's limit).
    public static let size = 500

    /// The asset ids once each, in order, in batches of at most `size`.
    public static func batches(_ assets: [String], size: Int = StateBatch.size) -> [[String]] {
        var seen = Set<String>()
        let unique = assets.filter { seen.insert($0).inserted }
        let step = max(1, min(size, macLimit))
        return stride(from: 0, to: unique.count, by: step).map { Array(unique[$0..<min(unique.count, $0 + step)]) }
    }

    /// `since` (an unchanged answer carries no items) names the answer to one whole request: with
    /// several batches each one is asked in full.
    public static func since(_ revision: String?, batches: Int) -> String? {
        batches == 1 ? revision : nil
    }

    /// The revision kept for the next `since`: only after every item of the answer was applied,
    /// from a single batch, and not while a polled book plays here (it is asked in full again).
    public static func kept(_ revision: String?, batches: Int, playingHere: Bool) -> String? {
        batches == 1 && !playingHere ? revision : nil
    }
}
