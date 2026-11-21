import Foundation

// The anime title screen's download button (toolbar, top left): a title with one episode
// downloads it at once; with more, a sheet lists the episodes to tick (those on this device or
// on their way shown ticked and fixed), with "All not watched" and "Next N" to pick quickly.

/// One episode of the title in the picker.
public struct EpisodePickRow: Equatable, Sendable {
    public enum State: Equatable, Sendable {
        /// Not on this device: can be ticked.
        case available
        /// Downloaded here already.
        case onDevice
        /// Downloading or waiting in the download queue.
        case downloading
    }

    public let id: Int
    public let number: Int
    public let watched: Bool
    public let state: State

    public init(id: Int, number: Int, watched: Bool, state: State) {
        self.id = id
        self.number = number
        self.watched = watched
        self.state = state
    }

    /// Shown ticked and not changeable (here or on its way).
    public var fixed: Bool { state != .available }
}

public enum EpisodeDownloadPicker {
    public enum Action: Equatable, Sendable {
        /// Nothing to download (no episode, or every one here or on its way): the button is disabled.
        case unavailable
        /// The title's only episode: downloaded at once.
        case download(Int)
        /// Several episodes: the sheet opens.
        case choose
    }

    /// What the toolbar button does for the title's episodes.
    public static func action(_ rows: [EpisodePickRow]) -> Action {
        let available = rows.filter { !$0.fixed }
        if rows.count == 1 { return available.first.map { Action.download($0.id) } ?? .unavailable }
        return available.isEmpty ? .unavailable : .choose
    }

    /// "All not watched": every episode not here and not watched.
    public static func notWatched(_ rows: [EpisodePickRow]) -> Set<Int> {
        Set(rows.filter { !$0.fixed && !$0.watched }.map(\.id))
    }

    /// "Next N": the first `count` episodes not here, from the next one to watch on (by number;
    /// without a known next one from the lowest). Fewer are left after it: only those.
    public static func next(_ count: Int, in rows: [EpisodePickRow], from nextToWatch: Int?) -> Set<Int> {
        guard count > 0 else { return [] }
        let start = nextToWatch ?? Int.min
        let picked = rows.filter { !$0.fixed && $0.number >= start }
            .sorted { ($0.number, $0.id) < ($1.number, $1.id) }
            .prefix(count)
        return Set(picked.map(\.id))
    }

    /// The selection when the sheet opens: the next episode to watch, when it can be downloaded.
    public static func initial(_ rows: [EpisodePickRow], nextToWatch: Int?) -> Set<Int> {
        guard let nextToWatch else { return [] }
        return Set(rows.filter { !$0.fixed && $0.number == nextToWatch }.prefix(1).map(\.id))
    }

    /// A tap on a row: an episode here or on its way does not change.
    public static func toggled(_ selection: Set<Int>, id: Int, in rows: [EpisodePickRow]) -> Set<Int> {
        guard let row = rows.first(where: { $0.id == id }), !row.fixed else { return selection }
        var result = selection
        if result.contains(id) { result.remove(id) } else { result.insert(id) }
        return result
    }

    /// The episodes "Download" sends to the download queue: ticked and not here, by number.
    public static func chosen(_ selection: Set<Int>, in rows: [EpisodePickRow]) -> [Int] {
        rows.filter { !$0.fixed && selection.contains($0.id) }
            .sorted { ($0.number, $0.id) < ($1.number, $1.id) }
            .map(\.id)
    }
}
