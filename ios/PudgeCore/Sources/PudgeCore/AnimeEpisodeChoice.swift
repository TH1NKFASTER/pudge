import Foundation

/// An episode of a title as the anime card's Play and the episode list see it.
public struct EpisodeChoiceItem: Equatable, Sendable {
    public let id: Int
    public let number: Int
    /// Watched on the Mac, or to the end on this device (it may still be watched again).
    public let watched: Bool
    /// Downloaded to this device.
    public let onDevice: Bool
    public init(id: Int, number: Int, watched: Bool, onDevice: Bool) {
        self.id = id; self.number = number; self.watched = watched; self.onDevice = onDevice
    }
}

/// What the round Play on an anime card does.
public enum CardPlayAction: Equatable, Sendable {
    /// The downloaded copy plays here (continues at its saved place, as the episode's own Play).
    case playHere(Int)
    /// Streams from the Mac (continues at the Mac's place, as the episode row's Watch).
    case watchFromMac(Int)

    public var id: Int {
        switch self {
        case .playHere(let id), .watchFromMac(let id): return id
        }
    }
}

/// The anime card's Play: the nearest episode to watch, never the episode list (the card itself opens that).
public enum AnimeCardPlay {
    /// The next episode to watch: the lowest number not watched; of two copies of one number, the one here.
    public static func next(_ items: [EpisodeChoiceItem]) -> EpisodeChoiceItem? {
        items.filter { !$0.watched }
            .sorted { ($0.number, $0.onDevice ? 0 : 1, $0.id) < ($1.number, $1.onDevice ? 0 : 1, $1.id) }
            .first
    }

    /// The downloaded copy when it is here, else watching from the Mac (only while it is reachable);
    /// nil when there is nothing to watch or nothing can play it now (no Play on the card).
    public static func action(_ items: [EpisodeChoiceItem], macAvailable: Bool) -> CardPlayAction? {
        guard let next = next(items) else { return nil }
        if next.onDevice { return .playHere(next.id) }
        return macAvailable ? .watchFromMac(next.id) : nil
    }
}

/// How an episode row of a title's list is drawn.
public enum EpisodeRowStyle: Equatable, Sendable {
    /// The next episode to watch: the main emphasis.
    case next
    /// Watched: grey and smaller (its actions stay, to watch it again or finish it).
    case watched
    case plain

    public static func of(watched: Bool, next: Bool) -> EpisodeRowStyle {
        if next { return .next }
        return watched ? .watched : .plain
    }

    /// The rows of a list: the next to watch (AnimeCardPlay.next) emphasised, watched ones grey.
    public static func rows(_ items: [EpisodeChoiceItem]) -> [Int: EpisodeRowStyle] {
        let nextId = AnimeCardPlay.next(items)?.id
        var result: [Int: EpisodeRowStyle] = [:]
        for item in items { result[item.id] = of(watched: item.watched, next: item.id == nextId) }
        return result
    }
}
