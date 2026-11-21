import Foundation

// MARK: Library grid (cover cards as in the Mac library)

/// Column count and card width of the library grids from the width they get (points).
/// A phone in portrait (narrower than `wideFrom`) shows 2–3 columns of small cards; wider
/// spaces (a phone in landscape, an iPad, Split View) take as many cards of at least
/// `minimumWide` points as fit, which is about 150–180 points each.
public enum LibraryGrid {
    public static let spacing: Double = 12
    public static let padding: Double = 16
    public static let wideFrom: Double = 600
    public static let minimumNarrow: Double = 108
    public static let minimumWide: Double = 145

    public static func columns(width: Double) -> Int {
        let inner = max(0, width - 2 * padding)
        if width < wideFrom {
            let fit = Int(((inner + spacing) / (minimumNarrow + spacing)).rounded(.down))
            return min(3, max(2, fit))
        }
        return max(3, Int(((inner + spacing) / (minimumWide + spacing)).rounded(.down)))
    }

    /// Width of one card for `width` (what the cover is drawn at).
    public static func cardWidth(width: Double) -> Double {
        let count = Double(columns(width: width))
        let inner = max(0, width - 2 * padding)
        return max(1, ((inner - spacing * (count - 1)) / count).rounded(.down))
    }
}

/// Accent of a card (its border and status line), the Mac's status colours:
/// good = downloaded / ready (green), accent = in progress (blue), warn = waiting (amber),
/// danger = deleted on the Mac (red), muted = nothing to do.
public enum CardTone: String, Equatable {
    case good, accent, warn, danger, muted
}

/// The meta lines under a card's title, as the Mac card has them: `detail` (the Mac's middle
/// line, e.g. "Next: episode 4" on an Available to download card; nil when the Mac has none),
/// `text` (its last line, the state), the accent and an optional progress bar (0...1).
/// `lines`: how many lines `text` may wrap to before it ends in "…" (the Mac's chips wrap).
public struct CardStatus: Equatable {
    public let text: String
    public let tone: CardTone
    public let progress: Double?
    public let detail: String?
    public let lines: Int
    public init(_ text: String, _ tone: CardTone, progress: Double? = nil, detail: String? = nil, lines: Int = 1) {
        self.text = text; self.tone = tone
        self.progress = progress.map { min(1, max(0, $0)) }
        self.detail = detail; self.lines = max(1, min(2, lines))
    }
}

/// The Mac's home sections that fit the phone's anime list.
public enum AnimeSection: Int, CaseIterable, Identifiable {
    case continueWatching, newReady, completedReady, waitingOnMac, caughtUp
    public var id: Int { rawValue }
    public var title: String {
        switch self {
        case .continueWatching: return "Continue watching"
        case .newReady: return "New episodes ready"
        case .completedReady: return "Completed & ready"
        case .waitingOnMac: return "Waiting on the Mac"
        case .caughtUp: return "Caught up"
        }
    }
    /// As on the Mac: a started next episode → Continue watching; an unstarted one → New episodes ready
    /// for an ongoing title (airing or on a schedule; unknown counts as ongoing), Completed & ready for a
    /// finished one; none → Caught up.
    public static func of(hasNext: Bool, nextPositionMs: Int64, airing: Bool? = nil) -> AnimeSection {
        guard hasNext else { return .caughtUp }
        if nextPositionMs > CardStatusText.startedMs { return .continueWatching }
        return airing == false ? .completedReady : .newReady
    }
}

/// One card of an anime shelf: a group the Mac offers episodes of, a Mac Home card of a title with
/// nothing to play yet, or both (a group with nothing left to watch that the Mac lists).
public struct AnimeShelfEntry: Hashable, Identifiable {
    public let group: AnimeGroup?
    public let mac: MacHomeCard?
    public init(group: AnimeGroup?, mac: MacHomeCard?) { self.group = group; self.mac = mac }
    public var id: String { group?.id ?? "mac:\(mac?.mediaId ?? 0)" }
    public var mediaId: Int { group?.mediaId ?? mac?.mediaId ?? 0 }
    public var title: String { group?.title ?? mac?.title ?? "" }
}

/// One section of the phone's anime list and its cards.
public struct AnimeShelf: Identifiable {
    public let section: AnimeSection
    public let entries: [AnimeShelfEntry]
    public init(section: AnimeSection, entries: [AnimeShelfEntry]) { self.section = section; self.entries = entries }
    public var id: Int { section.rawValue }
}

public enum AnimeShelves {
    /// The phone's anime sections in the Mac Home's order. Groups with an episode to watch go where
    /// `section` puts them; Waiting on the Mac and Caught up list the Mac's cards (its order), each once
    /// and never a title already shown with an episode to watch; groups with nothing left that the Mac
    /// does not list (finished titles still on this device) get no card. Without `mac` (an older Mac)
    /// Caught up holds the groups with nothing left, as before.
    public static func arrange(_ groups: [AnimeGroup], mac: MacHomeShelves?,
                               section: (AnimeGroup) -> AnimeSection) -> [AnimeShelf] {
        var lists: [AnimeSection: [AnimeShelfEntry]] = [:]
        var idle: [AnimeGroup] = []
        var playable = Set<Int>()
        for group in groups {
            let place = section(group)
            if place == .caughtUp || place == .waitingOnMac {
                idle.append(group)
            } else {
                lists[place, default: []].append(AnimeShelfEntry(group: group, mac: nil))
                if group.mediaId > 0 { playable.insert(group.mediaId) }
            }
        }
        var used = Set<String>()
        var listed = Set<Int>()
        func macEntries(_ cards: [MacHomeCard]) -> [AnimeShelfEntry] {
            var result: [AnimeShelfEntry] = []
            for card in cards where card.mediaId > 0 && !playable.contains(card.mediaId) && !listed.contains(card.mediaId) {
                listed.insert(card.mediaId)
                let group = idle.first { $0.mediaId == card.mediaId }
                if let group { used.insert(group.id) }
                result.append(AnimeShelfEntry(group: group, mac: card))
            }
            return result
        }
        if let mac {
            lists[.waitingOnMac] = macEntries(mac.waiting)
            lists[.caughtUp] = macEntries(mac.caughtUp)
        }
        // A group with nothing left that the Mac does not list as caught up is a finished title still
        // downloaded here: no card (its episodes stay under "Episodes on this device"). An older Mac
        // lists nothing: such groups stay under Caught up, as before.
        for group in idle where !used.contains(group.id) && mac == nil {
            lists[.caughtUp, default: []].append(AnimeShelfEntry(group: group, mac: nil))
        }
        return AnimeSection.allCases.compactMap { place -> AnimeShelf? in
            guard let entries = lists[place], !entries.isEmpty else { return nil }
            return AnimeShelf(section: place, entries: entries)
        }
    }
}

/// Download state of a card's item on this device.
public enum CardDownload: Equatable {
    case none
    case queued
    case running(phase: String, fraction: Double)
    case downloaded
}

/// Wording helpers of the Mac's cards (pudge/web: index.html, home_status.js, media.js; English
/// strings from locales.en.js, as the phone's interface is English).
public enum MacCardText {
    /// formatRemaining: "3d", "5h 12m", "12m".
    public static func remaining(_ seconds: Double) -> String {
        let value = max(0, seconds.isFinite ? seconds : 0)
        let days = Int(value / 86_400), hours = Int(value.truncatingRemainder(dividingBy: 86_400) / 3600)
        let minutes = Int(value.truncatingRemainder(dividingBy: 3600) / 60)
        if days > 0 { return "\(days)d" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(minutes)m"
    }

    /// formatClock / formatAudioTime: "1:02:03", "4:05".
    public static func clock(_ seconds: Double) -> String {
        let total = Int(max(0, seconds.isFinite ? seconds : 0))
        let hours = total / 3600, minutes = total % 3600 / 60, rest = total % 60
        let two = { (value: Int) in value < 10 ? "0\(value)" : "\(value)" }
        return hours > 0 ? "\(hours):\(two(minutes)):\(two(rest))" : "\(minutes):\(two(rest))"
    }

    /// compactJitenNumber: "950", "1.2k", "123k", "1.5m".
    public static func compactNumber(_ value: Int) -> String {
        let number = Double(max(0, value))
        func fixed(_ scaled: Double, _ digits: Int) -> String { String(format: "%.\(digits)f", scaled) }
        if number >= 1e6 { return fixed(number / 1e6, number >= 1e7 ? 0 : 1) + "m" }
        if number >= 1e3 { return fixed(number / 1e3, number >= 1e4 ? 0 : 1) + "k" }
        return String(Int(number))
    }

    /// formatEpisodeRanges: "1-3,5".
    public static func episodeRanges(_ values: [Int]) -> String {
        let episodes = Array(Set(values)).sorted()
        guard var start = episodes.first else { return "" }
        var end = start
        var ranges: [String] = []
        for value in episodes.dropFirst() {
            if value == end + 1 { end = value; continue }
            ranges.append(start == end ? "\(start)" : "\(start)-\(end)")
            start = value; end = value
        }
        ranges.append(start == end ? "\(start)" : "\(start)-\(end)")
        return ranges.joined(separator: ",")
    }

    /// Math.round(fraction * 100), within 0...100.
    public static func percent(_ fraction: Double) -> Int {
        Int((min(1, max(0, fraction.isFinite ? fraction : 0)) * 100).rounded())
    }

    /// English plural of the Mac's {one, other} entries.
    static func count(_ value: Int, _ one: String, _ other: String) -> String { "\(value) " + (value == 1 ? one : other) }

    /// torrentUiLabel of a download kind (torrent_status_ui.js).
    static func torrentLabel(_ kind: String) -> String {
        switch kind {
        case "finished": return "Finished"
        case "error": return "Error"
        case "paused": return "Paused"
        case "waiting": return "Queued"
        case "stalled": return "No connections"
        case "checking": return "Checking"
        case "downloading": return "Downloading"
        default: return "Status unknown"
        }
    }

    /// compactDownloadStatus without the live ETA: "42%", "Paused · 42%", "Waiting for subs".
    public static func compactDownload(kind: String, fraction: Double?) -> String {
        let percent = percent(fraction ?? 0)
        if kind == "finished" || percent >= 100 { return "Waiting for subs" }
        if kind == "downloading" { return "\(percent)%" }
        return "\(torrentLabel(kind)) · \(percent)%"
    }

    /// label.downloading: "Downloading: 42%" (also the phone's own downloads, as compact).
    public static func downloading(_ fraction: Double) -> String { "Downloading: \(percent(fraction))%" }

    /// extraFileKinds: "2 OP, ED".
    static func extraKinds(_ kinds: [String]) -> String {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for raw in kinds {
            let kind = raw.isEmpty ? "OVA" : raw
            if counts[kind] == nil { order.append(kind) }
            counts[kind, default: 0] += 1
        }
        return order.map { counts[$0]! > 1 ? "\(counts[$0]!) \($0)" : $0 }.joined(separator: ", ")
    }
}

/// The text of each card, line for line as the Mac shows the same item (see the table in
/// `LibraryCardsTests`). Phone-only states (its own downloads) use the Mac's compact wording.
public enum CardStatusText {
    /// A place past this (ms) counts as started ("Continue" rather than "Play").
    public static let startedMs: Int64 = 5000

    /// downloadedEpisodeText of a Mac ready card: "Episode 8", "Episodes 8-10,12",
    /// "All episodes ready", "Episode 12 • Final", "Ready to watch".
    public static func readyEpisodes(_ episodes: [Int], total: Int?) -> String {
        let numbers = Array(Set(episodes.filter { $0 > 0 })).sorted()
        let first = numbers.first
        let final = total != nil && first != nil && first! >= total!
        let all = (total ?? 0) > 0 && Set(1...(total!)).isSubset(of: Set(numbers))
        let suffix = final ? " • Final" : ""
        if all { return "All episodes ready" + suffix }
        if final, let first { return "Episode \(first) • Final" }
        if numbers.count == 1 { return "Episode \(numbers[0])" + suffix }
        return numbers.isEmpty ? "Ready to watch" : "Episodes \(MacCardText.episodeRanges(numbers))" + suffix
    }

    /// caughtUpHomeCard: "Episode 8 in 3d" (next airing / unlock), else "Up-to-date".
    public static func caughtUp(nextEpisode: Int?, nextAt: Double?, now: Double) -> CardStatus {
        if let episode = nextEpisode, episode > 0, let at = nextAt {
            return CardStatus("Episode \(episode) in \(MacCardText.remaining(at - now))", .warn)
        }
        return CardStatus("Up-to-date", .muted)
    }

    /// An anime card with episodes on the phone, as the Mac's home card of its section:
    /// Continue watching "Episode 4 • resume at 9:50" (or "Movie • resume at …"), New episodes /
    /// Completed & ready `readyEpisodes(…)`, Caught up `caughtUp(…)`. While the phone downloads
    /// the episode: "Downloading: 42%"; queued: "Waiting for download".
    /// - `ready`: episode numbers still to watch; `unlockEpisode` / `unlockAt`: a personal schedule's next.
    public static func anime(nextNumber: Int?, positionMs: Int64, durationMs: Int64, download: CardDownload,
                             ready: [Int], total: Int?, movie: Bool = false,
                             unlockEpisode: Int? = nil, unlockAt: Double? = nil, now: Double) -> CardStatus {
        guard let number = nextNumber else { return caughtUp(nextEpisode: unlockEpisode, nextAt: unlockAt, now: now) }
        switch download {
        case .queued:
            return CardStatus("Waiting for download", .warn)
        case .running(_, let fraction):
            return CardStatus(MacCardText.downloading(fraction), .accent, progress: fraction)
        case .downloaded, .none:
            let tone: CardTone = download == .downloaded ? .good : .accent
            if positionMs > startedMs {
                let fraction = durationMs > 0 ? Double(positionMs) / Double(durationMs) : nil
                let at = MacCardText.clock(Double(positionMs) / 1000)
                return CardStatus(movie ? "Movie • resume at \(at)" : "Episode \(number) • resume at \(at)", tone, progress: fraction)
            }
            return CardStatus(readyEpisodes(ready.isEmpty ? [number] : ready, total: total), tone)
        }
    }

    /// A Mac Home card with nothing to play on the phone (Waiting on the Mac, Caught up): the
    /// Mac card's lines. Needs action shows what its button says; Available to download its
    /// "Next: episode N" line and its download state or button.
    public static func macHome(_ card: MacHomeCard, now: Double) -> CardStatus {
        switch card.section {
        case "caught_up":
            return caughtUp(nextEpisode: card.nextEpisode, nextAt: card.nextAt, now: now)
        case "needs_action":
            return CardStatus(needsAction(card), .danger)
        case "download_available":
            let next = card.progress > 0 ? card.nextEpisode.map { "Next: episode \($0)" } : nil
            if let kind = card.downloadKind, !kind.isEmpty {
                let text = kind == "downloading" ? MacCardText.downloading(card.downloadFraction ?? 0)
                    : MacCardText.compactDownload(kind: kind, fraction: card.downloadFraction)
                return CardStatus(text, kind == "downloading" ? .accent : .warn, progress: card.downloadFraction, detail: next)
            }
            if card.queuedRelease == true { return CardStatus("Waiting for torrents to be turned on", .warn, detail: next) }
            return CardStatus(next != nil ? "Download next episodes" : "Download anime", .muted, detail: next)
        default:
            return waiting(card)
        }
    }

    /// needsActionHomeCard: the button (or note) of the Mac's Action required card.
    static func needsAction(_ card: MacHomeCard) -> String {
        switch card.actionCode ?? "" {
        case "review_extra_file":
            let kinds = card.extraKinds ?? []
            return kinds.count > 1 ? "Extras came with the download: \(MacCardText.extraKinds(kinds))"
                : "Extra \(kinds.first.map { $0.isEmpty ? "OVA" : $0 } ?? "OVA") came with the download"
        case "subtitle_retry_exhausted": return "Subtitle preparation keeps failing the same way"
        case "configure_jimaku": return "Configure Jimaku"
        case "grant_folder_access": return "Grant folder access"
        case "enable_subtitle_ocr": return "Enable OCR"
        default: return "Open settings"
        }
    }

    /// episodePresentationStatus of a Waiting for preparation card.
    static func waiting(_ card: MacHomeCard) -> CardStatus {
        let kind = card.downloadKind.flatMap { $0.isEmpty ? nil : $0 }
        let fraction = card.downloadFraction
        switch card.status ?? "" {
        case "waiting_verification": return CardStatus("Waiting for Japanese speech verification", .warn)
        case "needs_action": return CardStatus("Action required", .danger)
        case "couldnt_sync": return CardStatus("Not aligned — raw subtitles shown, timing may be off", .warn)
        case "waiting_text_subtitles": return CardStatus("Waiting for text subs", .warn)
        case "waiting_subtitles", "waiting_preparation": return CardStatus("Waiting for subs", .warn)
        case "paused_download":
            guard kind != nil || fraction != nil else { return CardStatus("Download paused", .warn) }
            return CardStatus(MacCardText.compactDownload(kind: "paused", fraction: fraction), .warn, progress: fraction)
        case "waiting_download", "downloading":
            guard kind != nil || fraction != nil else { return CardStatus("Waiting for download", .warn) }
            let shown = kind ?? "downloading"
            return CardStatus(MacCardText.compactDownload(kind: shown, fraction: fraction), shown == "downloading" ? .accent : .warn,
                              progress: fraction)
        case "queued_torrents_off": return CardStatus("Waiting for torrents to be turned on", .warn)
        case "download_error": return CardStatus("Download error", .danger)
        case "ready", "watched": return CardStatus("All episodes ready", .good)
        default:
            if let kind { return CardStatus(MacCardText.compactDownload(kind: kind, fraction: fraction), .warn, progress: fraction) }
            return CardStatus(card.nextEpisode.map { "Episode \($0) is not ready" } ?? "Not ready yet", .warn)
        }
    }

    /// Percent of a light novel as the Mac shows it: 100 when finished, else at most 99.
    public static func novelPercent(progress: Double?, finished: Bool) -> Int {
        finished ? 100 : min(99, MacCardText.percent(progress ?? 0))
    }

    /// lnBookCard's chips: a book alone "Volume 1 · 123k chars · 42% · 2019 · Finished"; a volume
    /// inside a series (`compact`) "42% · 123k chars · Finished". Wraps to two lines like the chips.
    public static func novel(volume: Int?, characters: Int?, year: Int?, progress: Double?, finished: Bool,
                             compact: Bool, download: CardDownload) -> CardStatus {
        let percent = novelPercent(progress: progress, finished: finished)
        if case .running(_, let fraction) = download {
            return CardStatus(MacCardText.downloading(fraction), .accent, progress: fraction, lines: 2)
        }
        let chars = (characters ?? 0) > 0 ? "\(MacCardText.compactNumber(characters!)) chars" : nil
        let done = finished ? "Finished" : nil
        let chips: [String?] = compact
            ? ["\(percent)%", chars, done]
            : ["Volume \(volume ?? 1)", chars, "\(percent)%", (year ?? 0) > 0 ? "\(year!)" : nil, done]
        let tone: CardTone = finished ? .muted : download == .downloaded ? .good : percent > 0 ? .accent : .muted
        return CardStatus(chips.compactMap { $0 }.joined(separator: " · "), tone, progress: Double(percent) / 100, lines: 2)
    }

    /// mangaLibraryCard's chips: alone "Volume 2 · 180 pages · 42%"; inside a series "42% · 180 pages".
    public static func manga(volume: Int?, pages: Int, readPages: Int, compact: Bool, download: CardDownload) -> CardStatus {
        if case .running(_, let fraction) = download {
            return CardStatus(MacCardText.downloading(fraction), .accent, progress: fraction, lines: 2)
        }
        let total = max(0, pages)
        let read = total > 0 ? max(0, min(total, readPages)) : 0
        let percent = total > 0 ? MacCardText.percent(Double(read) / Double(total)) : 0
        let count = total > 0 ? MacCardText.count(total, "page", "pages") : nil
        let chips: [String?] = compact ? ["\(percent)%", count] : ["Volume \(volume ?? 1)", count, "\(percent)%"]
        let tone: CardTone = total > 0 && read >= total ? .muted : download == .downloaded ? .good : percent > 0 ? .accent : .muted
        return CardStatus(chips.compactMap { $0 }.joined(separator: " · "), tone, progress: Double(percent) / 100, lines: 2)
    }

    /// One series of several volumes (the Mac's series card head): "5 volumes" and its
    /// "Continue · vol. 3" button while a volume is unfinished.
    public static func series(volumes: Int, continueVolume: Int?) -> CardStatus {
        let count = MacCardText.count(volumes, "volume", "volumes")
        guard let volume = continueVolume else { return CardStatus(count, .muted) }
        return CardStatus("Continue · vol. \(volume)", .accent, detail: count)
    }

    /// A light-novel franchise tile (several series): "Volume 3 · 42%" (or "Finished") and its
    /// "Franchise · 2 series" badge.
    public static func franchise(series: Int, currentVolume: Int?, currentProgress: Double?, currentFinished: Bool,
                                 allFinished: Bool) -> CardStatus {
        let percent = novelPercent(progress: currentProgress, finished: currentFinished)
        let position = allFinished ? "Finished" : "Volume \(currentVolume ?? 1) · \(percent)%"
        return CardStatus("Franchise · \(series) series", allFinished ? .muted : .accent,
                          progress: allFinished ? 1 : Double(percent) / 100, detail: position)
    }

    /// The audiobook card's summary: "12:34 / 1:02:03 · 20% · 49:29 left · Chapter 3 · 2 files";
    /// while the phone downloads it "Downloading: 42%"; a finished book "Finished · 1:02:03"
    /// with a full bar (as a finished light novel: muted, 100%).
    public static func audiobook(positionMs: Int64, durationMs: Int64, chapter: String? = nil, files: Int = 1,
                                 finished: Bool, ready: Bool, downloadFraction: Double?) -> CardStatus {
        if let fraction = downloadFraction, !ready {
            return CardStatus(MacCardText.downloading(fraction), .accent, progress: fraction, lines: 2)
        }
        if finished {
            var parts = ["Finished"]
            if durationMs > 0 { parts.append(MacCardText.clock(Double(durationMs) / 1000)) }
            if files > 1 { parts.append(MacCardText.count(files, "file", "files")) }
            return CardStatus(parts.joined(separator: " · "), .muted, progress: 1, lines: 2)
        }
        let position = Double(max(0, positionMs)) / 1000, duration = Double(max(0, durationMs)) / 1000
        let percent = duration > 0 ? Int(min(100, position / duration * 100).rounded()) : 0
        let remaining = max(0, duration - position)
        var parts = ["\(MacCardText.clock(position)) / \(MacCardText.clock(duration)) · \(percent)%"]
        if remaining > 0 { parts.append("\(MacCardText.clock(remaining)) left") }
        if let chapter, !chapter.isEmpty { parts.append(chapter) }
        if files > 1 { parts.append(MacCardText.count(files, "file", "files")) }
        let tone: CardTone = finished ? .muted : ready ? .good : position > 0 ? .accent : .muted
        return CardStatus(parts.joined(separator: " · "), tone, progress: duration > 0 && position > 0 ? position / duration : nil, lines: 2)
    }

    /// The Mac's audiobook series head: "5 volumes" and "2 unfinished" / "All finished".
    public static func audiobookSeries(volumes: Int, unfinished: Int) -> CardStatus {
        CardStatus(unfinished > 0 ? "\(unfinished) unfinished" : "All finished", unfinished > 0 ? .accent : .muted,
                   detail: MacCardText.count(volumes, "volume", "volumes"))
    }

    /// A Planning / Watching title (AniList): episodes ready on the Mac first, then progress.
    public static func planning(progress: Int, episodes: Int?, ready: Int) -> CardStatus {
        let watched = episodes.map { "\(progress) / \($0) episodes" } ?? "\(progress) episodes watched"
        let fraction = episodes.flatMap { $0 > 0 ? Double(progress) / Double($0) : nil }
        if ready > 0 { return CardStatus("\(ready) ready · " + watched, .good, progress: fraction) }
        return CardStatus(watched, progress > 0 ? .accent : .muted, progress: fraction)
    }
}

/// When an audiobook counts as finished, the Mac's rule (pudge/audiobook_finish.py): Finished
/// (the button, the end of playback) or a place in the last 2% / last 5 s sets it, only Mark
/// unfinished clears it. The Mac's flag is what every screen shows once the Mac answered for
/// the book; before that (offline playback) the place on this device decides.
public enum AudiobookFinish {
    public static let fraction = 0.98
    public static let tailMs: Int64 = 5_000

    /// A place at the end of the book.
    public static func reachedEnd(positionMs: Int64, durationMs: Int64) -> Bool {
        guard durationMs > 0 else { return false }
        return Double(positionMs) >= Double(durationMs) * fraction || durationMs - positionMs <= tailMs
    }

    /// `local`: this device's own Finished / Mark unfinished the Mac has not answered for yet
    /// (it wins); `mac`: the Mac's flag (nil while the Mac has not answered for the book).
    public static func isFinished(local: Bool? = nil, mac: Bool?, positionMs: Int64, durationMs: Int64) -> Bool {
        if let local { return local }
        if let mac { return mac }
        return reachedEnd(positionMs: positionMs, durationMs: durationMs)
    }

    /// Mark unfinished at the very end starts the book over (as on the Mac): a place left at
    /// the end would count as finished again on the next save.
    public static func unfinishedPosition(positionMs: Int64, durationMs: Int64) -> Int64 {
        reachedEnd(positionMs: positionMs, durationMs: durationMs) ? 0 : min(max(0, positionMs), max(0, durationMs))
    }
}

/// The Mac's Finished flag of each audiobook as it last answered (asset id → flag), kept on the
/// device across launches. A finished book listened to again has its place away from the end:
/// launched without the Mac, only the place was left to decide and the book showed unfinished
/// until the Mac answered. `AudiobookFinish.isFinished` takes the kept flag as the Mac's until
/// it answers anew.
public struct MacFinishedFlags: Equatable, Sendable {
    public static let defaultsKey = "pudge.macFinished"
    public private(set) var flags: [String: Bool]

    public init(_ flags: [String: Bool] = [:]) { self.flags = flags }

    public subscript(asset: String) -> Bool? { flags[asset] }

    /// What the kept flag says while the Mac has not answered since the launch (the `mac` of
    /// `AudiobookFinish.isFinished`): Finished when the Mac said so (only Mark unfinished clears
    /// it, and this device's own one wins until the Mac answers). Otherwise nil, so the place
    /// decides as before: a book played to its end without the Mac is finished.
    public func finished(_ asset: String) -> Bool? { flags[asset] == true ? true : nil }

    /// The Mac answered for `asset`. True when the kept flags changed (the caller saves them).
    @discardableResult
    public mutating func record(_ asset: String, finished: Bool) -> Bool {
        guard flags[asset] != finished else { return false }
        flags[asset] = finished
        return true
    }

    /// Only the books still on this device keep their flag. True when a flag went.
    @discardableResult
    public mutating func keep(_ assets: Set<String>) -> Bool {
        let kept = flags.filter { assets.contains($0.key) }
        guard kept.count != flags.count else { return false }
        flags = kept
        return true
    }
}

/// What the library screens' pull-down header shows (Anime, Light novels, Manga, Audiobooks, the
/// same everywhere): everything, or only what is stored on this iPhone/iPad.
public enum LibraryScope: String, CaseIterable, Hashable, Sendable {
    case all
    case onDevice

    /// "All" / "Only on iPhone" (`device`: "iPhone" or "iPad").
    public func title(device: String) -> String { self == .all ? "All" : "Only on \(device)" }
}

/// The pull-down header of a library screen: the search field (titles) and the
/// "Only on iPhone/iPad" choice.
public struct LibraryFilter: Equatable, Sendable {
    public var search: String
    public var scope: LibraryScope

    public init(search: String = "", scope: LibraryScope = .all) { self.search = search; self.scope = scope }

    /// Something is filtered out (the empty list then says "Nothing found").
    public var isActive: Bool { scope == .onDevice || !query.isEmpty }

    private var query: String { search.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// An item with these titles (title, series, franchise; nil or empty ones are skipped):
    /// shown when one of them contains the search (any case) and, with "Only on …", when it is
    /// stored on this device.
    public func matches(_ titles: [String?], onDevice: Bool) -> Bool {
        if scope == .onDevice && !onDevice { return false }
        let query = query
        guard !query.isEmpty else { return true }
        return titles.contains { title in
            guard let title, !title.isEmpty else { return false }
            return title.localizedCaseInsensitiveContains(query)
        }
    }
}

/// How often the phone asks the Mac for the anime list while the app is in front: often only
/// while a download runs on the Mac, rarely otherwise. Any card under Waiting (Action required,
/// Available to download, waiting for subtitles) made it every 15 s, and such a card is often
/// there for days: the whole list was fetched and applied every 15 s.
public enum AnimeLiveRefresh {
    public static let busySeconds: Double = 15
    public static let idleSeconds: Double = 90
    /// After a touch in a reader (a scroll, a page turn) the list waits at least this long.
    public static let quietAfterTouchSeconds: Double = 30
    public static func interval(home: MacHomeShelves?) -> Double {
        (home?.waiting.contains(where: downloading) ?? false) ? busySeconds : idleSeconds
    }
    /// The card's download runs on the Mac now ("Downloading 42%"): its episode is ready soon.
    /// Paused, stalled, queued or failed downloads and cards waiting for an action are not.
    public static func downloading(_ card: MacHomeCard) -> Bool {
        let kind = card.downloadKind ?? ""
        if kind == "downloading" { return true }
        return kind.isEmpty && card.status == "downloading"
    }
    /// A tick asks the Mac only when it answers, nothing plays or is read on this device, no
    /// list refresh runs and no reader was touched lately.
    public static func shouldRefresh(macAvailable: Bool, playerOnScreen: Bool, readerOnScreen: Bool, refreshing: Bool,
                                     secondsSinceTouch: Double) -> Bool {
        macAvailable && !playerOnScreen && !readerOnScreen && !refreshing && secondsSinceTouch >= quietAfterTouchSeconds
    }
}
