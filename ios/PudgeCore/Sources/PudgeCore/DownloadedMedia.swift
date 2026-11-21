import Foundation

/// Downloaded episodes and reading volumes are numbered by the Mac they came from, and every
/// Mac numbers from 1: after switching Macs, the previous Mac's episode 17 is not this Mac's 17.
public enum DownloadOwner {
    /// New local ids for the downloads of a Mac other than `bound` (the Mac the data belongs
    /// to now): below zero, where no Mac numbers anything, and below every id in use. Such a
    /// download stays playable here but never meets the bound Mac's item of the same number
    /// (a "watched" mark sent there, its subtitles or an update taken from there). Downloads
    /// already renumbered (below zero) and those without an owner stay as they are.
    public static func localIds(_ downloads: [(id: Int, serverId: String?)], bound: String?) -> [Int: Int] {
        guard let bound else { return [:] }
        var next = min(0, downloads.map { $0.id }.min() ?? 0) - 1
        var result: [Int: Int] = [:]
        for item in downloads.sorted(by: { $0.id < $1.id }) where item.id > 0 {
            guard let owner = item.serverId, owner != bound else { continue }
            result[item.id] = next
            next -= 1
        }
        return result
    }
}

/// Where a player or reader opened. Saving that same place again is not news: with a fresh
/// time it took a later place watched or read on the Mac meanwhile back (an old place opened
/// offline won as the newest). Once it moved away (more than `tolerance`), every place counts,
/// also back where it opened.
public struct OpenedPlace: Equatable {
    public let chapter: Int
    public let position: Double
    public let tolerance: Double
    public private(set) var moved = false
    public init(chapter: Int = 0, position: Double, tolerance: Double = 0) {
        self.chapter = chapter; self.position = position; self.tolerance = tolerance
    }
    /// True when `position` in `chapter` is a new place to save.
    public mutating func counts(chapter: Int = 0, position: Double) -> Bool {
        if !moved && chapter == self.chapter && abs(position - self.position) <= tolerance { return false }
        moved = true
        return true
    }
}
