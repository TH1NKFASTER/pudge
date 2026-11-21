import Foundation

/// An episode counted as watched on this device, kept until the Mac's list shows it.
public struct LocalAnimeWatch: Hashable, Sendable {
    public let mediaId: Int
    /// The episode's number on its title (the Mac's `number`).
    public let number: Int
    /// When the Mac confirmed the watch (seconds since 1970); nil: not sent yet (the Mac away).
    public var deliveredAt: Double?
    public init(mediaId: Int, number: Int, deliveredAt: Double? = nil) {
        self.mediaId = mediaId; self.number = number; self.deliveredAt = deliveredAt
    }

    /// A list asked for at `listRequestedAt` cannot contain this watch yet.
    public func newer(than listRequestedAt: Double) -> Bool {
        guard let deliveredAt else { return true }
        return deliveredAt >= listRequestedAt
    }
}

/// "Watched N of M" right after a watch on this device: the Mac's count, raised to an episode
/// watched here that the Mac's last list could not know about yet (sent after that list was asked
/// for, or still waiting for the Mac). A list asked for after the watch reached the Mac is shown
/// as it is (the Mac decides, e.g. AniList refused the episode or the progress was lowered there).
public enum AnimeProgressOverlay {
    public static func progress(mac: Int, total: Int?, mediaId: Int, watches: [LocalAnimeWatch], listRequestedAt: Double) -> Int {
        guard mediaId > 0 else { return mac }
        let here = watches.filter { $0.mediaId == mediaId && $0.newer(than: listRequestedAt) }.map(\.number).max() ?? 0
        guard here > mac else { return mac }
        if let total, total > 0 { return min(here, total) }
        return here
    }

    public static func apply(_ groups: [AnimeGroup], watches: [LocalAnimeWatch], listRequestedAt: Double) -> [AnimeGroup] {
        guard !watches.isEmpty else { return groups }
        return groups.map { group in
            let shown = progress(mac: group.progress, total: group.total, mediaId: group.mediaId, watches: watches, listRequestedAt: listRequestedAt)
            guard shown != group.progress else { return group }
            return AnimeGroup(mediaId: group.mediaId, title: group.title, progress: shown, total: group.total,
                              schedule: group.schedule, airing: group.airing, episodes: group.episodes)
        }
    }

    public static func apply(_ home: MacHomeShelves?, watches: [LocalAnimeWatch], listRequestedAt: Double) -> MacHomeShelves? {
        guard let home, !watches.isEmpty else { return home }
        func raised(_ cards: [MacHomeCard]) -> [MacHomeCard] {
            cards.map { card in
                let shown = progress(mac: card.progress, total: card.total, mediaId: card.mediaId, watches: watches, listRequestedAt: listRequestedAt)
                guard shown != card.progress else { return card }
                return MacHomeCard(mediaId: card.mediaId, title: card.title, progress: shown, total: card.total, section: card.section,
                                   status: card.status, nextEpisode: card.nextEpisode, nextAt: card.nextAt, downloadFraction: card.downloadFraction)
            }
        }
        return MacHomeShelves(caughtUp: raised(home.caughtUp), waiting: raised(home.waiting))
    }
}

/// The player stays open while the Mac is away (quit for an update, asleep, another Wi-Fi).
public enum PlayerContinuity {
    /// The app's alert waits while a full-screen video is open: SwiftUI presenting an alert from
    /// the root while the player covers it closed the player ("kicked out of the episode" when a
    /// refresh failed during the Mac's restart). It shows once the player has closed.
    public static func showsAlert(message: String?, videoOnScreen: Bool) -> Bool {
        message != nil && !videoOnScreen
    }

    /// A stream from the Mac that stopped this way is resumed on its own once the Mac answers
    /// again (the player keeps the last frame meanwhile). A device the Mac no longer accepts, or a
    /// copy that changed there, waits for Try again.
    public static func reconnects(after failure: VideoStreamFailure?) -> Bool {
        failure?.retryable ?? false
    }

    /// Seconds before reconnect check `attempt` (1-based): soon at first, then every 15 s.
    public static func reconnectDelay(attempt: Int) -> Double { min(15, 3 * Double(max(1, attempt))) }
}
