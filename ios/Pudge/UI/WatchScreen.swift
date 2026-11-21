import SwiftUI
import AVFoundation
import CoreMedia
import MediaPlayer
import UIKit
import PudgeCore

/// Starts an episode: the Mac's review gate first, then the player.
@MainActor
final class EpisodeLauncher: ObservableObject {
    @Published var playing: LocalEpisode?
    /// Watched from the Mac (Watch).
    @Published var streaming: StreamPlayback?
    @Published var gating: LocalEpisode?
    @Published var checking: Int?
    var granted: LocalEpisode?
    /// Watched from the Mac once the review gate lets it through.
    private var gatedStream: EpisodeItem?

    /// Something is starting or playing: one player at a time (an episode being prepared to
    /// watch from the Mac counts, its row has Cancel).
    func busy(_ media: MediaModel) -> Bool {
        checking != nil || !media.streamJobs.isEmpty || playing != nil || streaming != nil || gating != nil
    }

    /// Reviews before the episode, as on the Mac (skipped only when the Mac is unreachable).
    func start(_ local: LocalEpisode, media: MediaModel) {
        guard !busy(media) else { return }
        // An episode of a previous Mac: this Mac's episode of that number is another one.
        if media.isOtherMac(local) { playing = local; return }
        guard media.app.macConnection != nil, !media.app.macOffline else { playing = local; return }
        checking = local.id
        Task {
            defer { checking = nil }
            // A quick check first: away from the Mac the episode plays at once.
            guard await media.app.reachMac() else {
                AppLog.write("gate", "skipped: Mac unavailable")
                playing = local; return
            }
            do {
                let status = try await media.gateBegin(local.id)
                if status.blocking && !status.granted { gating = local } else { playing = local }
            } catch {
                AppLog.write("gate", "skipped: \(error.localizedDescription)")
                playing = local
            }
        }
    }

    /// Watch from the Mac without downloading: the review gate first (as for a downloaded
    /// episode), then the Mac prepares it (progress in the list) and the player streams it.
    func watch(_ item: EpisodeItem, media: MediaModel) {
        guard !busy(media) else { return }
        checking = item.id
        Task {
            var blocked = false
            do {
                let status = try await media.gateBegin(item.id)
                blocked = status.blocking && !status.granted
            } catch {
                AppLog.write("gate", "skipped: \(error.localizedDescription)")
            }
            checking = nil
            guard !blocked else {
                gatedStream = item
                gating = LocalEpisode(id: item.id, title: item.title, number: item.number, sha256: "", sizeBytes: 0,
                                      durationMs: 0, positionMs: 0, positionDirty: false, savedAt: Date().timeIntervalSince1970)
                return
            }
            await streamNow(item, media: media)
        }
    }

    /// Download and play now: the download starts (from the episode's start, in order) and the
    /// player opens once the Mac has the copy ready, playing the part already here and the rest
    /// from the Mac while the download goes on (play while downloading).
    func downloadAndWatch(_ item: EpisodeItem, media: MediaModel, quality: VideoQuality? = nil) {
        guard !busy(media) else { return }
        if media.videoJobs[item.id] == nil, !media.videoQueue.contains(where: { $0.id == item.id }) {
            Task { await media.download(item, quality: quality) }
        }
        checking = item.id
        Task {
            // The Mac prepares the copy (a smaller one is made first): its progress shows in the row.
            // The download task may not have started yet: a few seconds' grace before "not running".
            let began = Date()
            while media.downloadingCopies[item.id] == nil && media.localEpisodes[item.id] == nil {
                let running = media.videoJobs[item.id] != nil || media.videoQueue.contains(where: { $0.id == item.id })
                if !running && Date().timeIntervalSince(began) > 3 { break }
                try? await Task.sleep(nanoseconds: 500_000_000)
                guard checking == item.id else { return }
            }
            checking = nil
            if let local = media.localEpisodes[item.id] { start(local, media: media) }
            else if media.downloadingCopies[item.id] != nil { watch(item, media: media) }
            // Cancelled or failed: the download said why.
        }
    }

    private func streamNow(_ item: EpisodeItem, media: MediaModel) async {
        guard let playback = await media.prepareStream(item) else { return }
        // Another player opened meanwhile: this one does not pop up later over it.
        guard playing == nil, streaming == nil, gating == nil else {
            playback.stream.close()
            media.endStream(item.id)
            return
        }
        streaming = playback
    }

    /// The review gate closed: the episode it let through starts (downloaded or from the Mac).
    func gateClosed(media: MediaModel) {
        let stream = gatedStream
        gatedStream = nil
        guard let next = granted else { return }
        granted = nil
        if let stream, stream.id == next.id {
            Task { await streamNow(stream, media: media) }
        } else {
            playing = next
        }
    }
}

struct WatchScreen: View {
    @ObservedObject var media: MediaModel
    @AppStorage("pudge.animeSection") private var section = 0
    @StateObject private var launcher = EpisodeLauncher()
    @State private var path = NavigationPath()
    @State private var width: CGFloat = 390
    /// The pull-down header: search (title) and "Only on iPhone/iPad" (a title with an episode here).
    @State private var filter = LibraryFilter()

    /// The Mac's anime plus episodes kept on the iPhone that the Mac no longer offers.
    var groups: [AnimeGroup] {
        var result = media.animeGroups
        let known = Set(result.flatMap { $0.episodes.map(\.id) })
        for local in media.localEpisodes.values.sorted(by: { $0.number < $1.number }) where !known.contains(local.id) {
            let item = EpisodeItem(id: local.id, mediaId: local.mediaId ?? 0, number: local.number, title: local.title, episodeTitle: "",
                                   watched: false, position: Double(local.positionMs) / 1000, duration: Double(local.durationMs) / 1000,
                                   prepared: true, preparing: false)
            if let index = result.firstIndex(where: { ($0.mediaId > 0 && $0.mediaId == item.mediaId) || $0.title == item.title }) {
                result[index].episodes.append(item)
                result[index].episodes.sort { $0.number < $1.number }
            } else {
                // The Mac no longer offers it: its count from the Mac Home's card (Caught up …).
                let card = media.macHomeCard(item.mediaId)
                result.append(AnimeGroup(mediaId: item.mediaId, title: item.title, progress: card?.progress ?? 0, total: card?.total,
                                         airing: false, episodes: [item]))
            }
        }
        // One item per episode number (two files of one episode on the Mac, or a copy kept here
        // and the Mac's other one): the copy on this device, else the one downloading, else the Mac's.
        for index in result.indices {
            result[index].episodes = media.oneCopyEach(result[index].episodes)
        }
        // The Mac's order (ready new, ready old, …); episodes kept only on the iPhone come last.
        // A watch on this device counts at once, before the Mac's next list has it.
        return media.shown(result)
    }

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Picker("Section", selection: $section) {
                        Text("Ready").tag(0)
                        Text("Planning").tag(1)
                    }.pickerStyle(.segmented)
                    if section == 0 { ready } else { planning }
                }
                .padding(.horizontal, CGFloat(LibraryGrid.padding)).padding(.bottom, 24)
            }
            .libraryWidth($width)
            .background(PudgeTheme.background)
            .navigationTitle("Anime")
            .libraryFilterHeader($filter, prompt: "Title", device: PudgeTheme.device)
            .navigationDestination(for: AnimeGroup.self) { group in AnimeDetailScreen(media: media, launcher: launcher, group: group) }
            .navigationDestination(for: DownloadedEpisodesRoute.self) { _ in DownloadedEpisodesScreen(media: media) }
            .popsToRootOnReselect($path)
            .toolbar { ToolbarItem(placement: .primaryAction) { SettingsButton(model: media.app) } }
            .overlay { if media.loadingEpisodes && sections.isEmpty && section == 0 { ProgressView() } }
            .refreshable { media.app.retryMacNow(); await media.refreshEpisodes(); await media.refreshPlanning() }
            .task {
                if media.animeGroups.isEmpty { await media.refreshEpisodes() }
                if media.planning == nil { await media.refreshPlanning() }
            }
            .fullScreenCover(item: $launcher.playing) { item in VideoPlayerScreen(media: media, episode: item) }
            .fullScreenCover(item: $launcher.streaming) { playback in
                VideoPlayerScreen(media: media, episode: playback.record, stream: playback)
            }
        }
        .fullScreenCover(item: $launcher.gating, onDismiss: { launcher.gateClosed(media: media) }) { item in
            ReviewGateScreen(media: media, episode: item) { launcher.granted = item; launcher.gating = nil }
        }
        // The end of an anime watched here: the score wheel, then the next entry (FinaleSheet).
        .animeFinale(media.finales)
    }

    /// The Mac's home sections: Continue watching, New episodes ready, Completed & ready, Waiting on the Mac,
    /// Caught up (the Mac's order inside each; the last two list every title the Mac Home does).
    var sections: [AnimeShelf] {
        AnimeShelves.arrange(groups, mac: media.shownMacHome) { group in
            let next = media.nextEpisode(group)
            return AnimeSection.of(hasNext: next != nil, nextPositionMs: next.map { media.placeMs($0) } ?? 0,
                                   airing: group.airing ?? (group.schedule != nil ? true : nil))
        }
    }

    /// The anime with an episode kept on this device (the header's "Only on iPhone/iPad").
    private var mediaOnDevice: Set<Int> { Set(media.localEpisodes.values.compactMap(\.mediaId)) }

    /// A card of the Ready list through the header (title; with an episode on this device).
    private func shown(_ entry: AnimeShelfEntry) -> Bool {
        let here = entry.group.map { group in group.episodes.contains { media.localEpisodes[$0.id] != nil } } ?? false
        return filter.matches([entry.title], onDevice: here)
    }

    /// The sections through the header; a section left empty is not shown.
    private var shownSections: [AnimeShelf] {
        guard filter.isActive else { return sections }
        return sections.compactMap { shelf in
            let entries = shelf.entries.filter(shown)
            return entries.isEmpty ? nil : AnimeShelf(section: shelf.section, entries: entries)
        }
    }

    @ViewBuilder private var ready: some View {
        if let error = media.episodesError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
        let shelves = shownSections
        if shelves.isEmpty && !media.loadingEpisodes {
            if filter.isActive {
                ContentUnavailableView.search(text: filter.search)
            } else {
                ContentUnavailableView("No episodes", systemImage: "play.rectangle",
                    description: Text("Unwatched episodes with ready Japanese subtitles on the Mac appear here. Download one to watch offline."))
            }
        }
        let layout = LibraryLayout(width: width)
        ForEach(shelves) { shelf in
            LibrarySectionHeader(title: shelf.section.title, count: shelf.entries.count)
            LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                ForEach(shelf.entries) { entry in
                    if let group = entry.group {
                        AnimeGridCard(media: media, launcher: launcher, group: group, width: layout.card, mac: entry.mac) { path.append(group) }
                    } else if let card = entry.mac {
                        // Nothing to play yet: read-only; its page says why (no episodes listed).
                        MacHomeAnimeCard(media: media, card: card, width: layout.card) {
                            path.append(AnimeGroup(mediaId: card.mediaId, title: card.title, progress: card.progress,
                                                   total: card.total, episodes: []))
                        }
                    }
                }
            }.libraryGridIdentity(layout)
        }
        if !media.localEpisodes.isEmpty {
            // The size next to the label; opens the downloaded episodes (as Settings → Storage → Episodes).
            // A route in the path (not a view link): tapping the Anime tab again goes back from it too.
            Button { path.append(DownloadedEpisodesRoute()) } label: {
                HStack(spacing: 4) {
                    Text("Episodes on \(PudgeTheme.device) · \(PudgeTheme.size(media.videoBytes))")
                    Image(systemName: "chevron.right").font(.caption2.weight(.semibold))
                }
                .font(.footnote).foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .padding(.top, 12)
            .accessibilityIdentifier("episodes-on-device")
        }
    }

    @ViewBuilder private var planning: some View {
        if let error = media.planningError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
        if let planning = media.planning {
            let layout = LibraryLayout(width: width)
            let here = mediaOnDevice
            let current = planning.current.filter { filter.matches([$0.title], onDevice: here.contains($0.mediaId)) }
            let planned = planning.planned.filter { filter.matches([$0.title], onDevice: here.contains($0.mediaId)) }
            LibrarySectionHeader(title: "Watching", count: current.count)
            if current.isEmpty { Text(filter.isActive ? "Nothing found" : "Nothing in progress").foregroundStyle(.secondary) }
            LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                ForEach(current) { item in PlanningCard(media: media, item: item, width: layout.card) }
            }.libraryGridIdentity(layout)
            if !planned.isEmpty {
                LibrarySectionHeader(title: "Planning", count: planned.count)
                LazyVGrid(columns: layout.columns, alignment: .leading, spacing: CGFloat(LibraryGrid.spacing)) {
                    ForEach(planned) { item in PlanningCard(media: media, item: item, width: layout.card) }
                }.libraryGridIdentity(layout)
            }
        } else if media.planningError == nil {
            ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
        }
    }
}

/// Settings → Storage → Episodes, pushed from the Anime tab's "Episodes on …" row.
struct DownloadedEpisodesRoute: Hashable {}

/// A title the Mac Home lists with nothing to play yet (Caught up, Waiting on the Mac): cover, title
/// and the Mac card's lines (next airing / what the Mac is doing). No actions; it opens the title's page.
struct MacHomeAnimeCard: View {
    @ObservedObject var media: MediaModel
    let card: MacHomeCard
    let width: CGFloat
    let open: () -> Void
    var body: some View {
        LibraryCard(title: card.title, status: CardStatusText.macHome(card, now: Date().timeIntervalSince1970),
                    width: width, open: open) {
            CardCover(media: media, path: "cover/anime/\(card.mediaId)", symbol: "play.rectangle")
        }
    }
}

extension MediaModel {
    /// Watched to the end on this iPhone (the Mac may not know yet).
    func finishedHere(_ item: EpisodeItem) -> Bool {
        guard let local = localEpisodes[item.id], local.durationMs > 0 else { return false }
        if local.watchedSent != nil { return true }
        return local.positionMs >= local.durationMs - 120_000
    }
    /// Where the episode is: the place on this device, else where watching from the Mac starts (ms).
    func placeMs(_ item: EpisodeItem) -> Int64 {
        if let local = localEpisodes[item.id] { return local.positionMs }
        return streamPositionMs(item)
    }
    /// Its download to this device, for the card.
    func cardDownload(_ item: EpisodeItem) -> CardDownload {
        if localEpisodes[item.id] != nil { return .downloaded }
        if let job = videoJobs[item.id] { return .running(phase: job.phase, fraction: job.fraction) }
        if let job = streamJobs[item.id] { return .running(phase: job.phase, fraction: job.fraction) }
        if videoQueue.contains(where: { $0.id == item.id }) { return .queued }
        return .none
    }
    /// The Mac Home's card of a title with nothing to play yet.
    func macHomeCard(_ mediaId: Int) -> MacHomeCard? {
        guard mediaId > 0, let home = macHome else { return nil }
        return (home.waiting + home.caughtUp).first { $0.mediaId == mediaId }
    }
    /// One item per episode number: the copy on this device, else the one downloading, else the first.
    func oneCopyEach(_ items: [EpisodeItem]) -> [EpisodeItem] {
        EpisodeCopies.unique(items) { item in
            if localEpisodes[item.id] != nil { return 2 }
            return videoJobs[item.id] != nil || videoQueue.contains(where: { $0.id == item.id }) ? 1 : 0
        }
    }
    /// The episode as the card's Play and the list's emphasis see it (PudgeCore AnimeCardPlay).
    func choiceItem(_ item: EpisodeItem) -> EpisodeChoiceItem {
        EpisodeChoiceItem(id: item.id, number: item.number, watched: item.watched || finishedHere(item),
                          onDevice: localEpisodes[item.id] != nil)
    }
    /// Nearest episode still to watch (of two copies of one number, the one here).
    func nextEpisode(_ group: AnimeGroup) -> EpisodeItem? {
        guard let next = AnimeCardPlay.next(group.episodes.map(choiceItem)) else { return nil }
        return group.episodes.first { $0.id == next.id }
    }
}

/// Prominent action with an always-visible icon (also inside list rows).
struct PillButton: View {
    let title: String
    let systemImage: String
    var tint = PudgeTheme.accent
    /// Smaller (a watched episode's Play in its list).
    var compact = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(compact ? Font.caption.bold() : Font.subheadline.bold()).foregroundStyle(.white)
                .padding(.horizontal, compact ? 10 : 14).padding(.vertical, compact ? 5 : 8)
                .background(Capsule().fill(tint))
        }.buttonStyle(.borderless)
    }
}

/// One anime as a cover card (the Mac's home cards): the nearest episode to watch in the status
/// line, Play on the cover, and in ⋯ every action the list row had (watch from the Mac,
/// download, cancel, download all, delete). The card opens the episode list; Play never does:
/// it opens the nearest episode to watch (PudgeCore AnimeCardPlay) exactly as that episode's own
/// Play / Watch in the list (the same launcher calls, so the same place to continue from).
struct AnimeGridCard: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let group: AnimeGroup
    let width: CGFloat
    /// The Mac Home's card of the title when nothing is left to watch here (its next airing, its state).
    var mac: MacHomeCard? = nil
    let open: () -> Void
    @State private var deletePrompt: EpisodeItem?
    var body: some View {
        let next = media.nextEpisode(group)
        let missing = group.episodes.filter { media.localEpisodes[$0.id] == nil && !media.finishedHere($0) }
        let kept = group.episodes.filter { media.localEpisodes[$0.id] != nil }.count
        LibraryCard(title: group.title, status: status(next),
                    badge: kept > 0 ? .onDevice(kept > 1 ? kept : nil) : nil,
                    width: width, accessory: playButton(), identifier: "anime-card", open: open) {
            CardCover(media: media, path: "cover/anime/\(group.mediaId)", symbol: "play.rectangle")
        } actions: {
            if let next { actions(next) }
            if missing.count > 1 {
                Button { media.downloadAll(missing) } label: {
                    Label("Download all \(missing.count) episodes", systemImage: "square.and.arrow.down.on.square")
                }.disabled(media.app.macConnection == nil || media.app.macOffline || missing.allSatisfy { item in media.videoQueue.contains { $0.id == item.id } || media.videoJobs[item.id] != nil })
            }
            Button(action: open) { Label("Episodes", systemImage: "list.bullet") }
        }
        .confirmationDialog("Delete this episode from the \(PudgeTheme.device)?",
                            isPresented: Binding(get: { deletePrompt != nil }, set: { if !$0 { deletePrompt = nil } }),
                            titleVisibility: .visible, presenting: deletePrompt) { item in
            Button("Delete", role: .destructive) { media.deleteEpisode(item.id) }
        }
    }

    /// The Mac Home card's line of the title's section (PudgeCore decides the wording).
    private func status(_ next: EpisodeItem?) -> CardStatus {
        let now = Date().timeIntervalSince1970
        if next == nil, let mac { return CardStatusText.macHome(mac, now: now) }
        let local = next.flatMap { media.localEpisodes[$0.id] }
        return CardStatusText.anime(nextNumber: next?.number, positionMs: next.map { media.placeMs($0) } ?? 0,
                                    durationMs: local?.durationMs ?? next.map { Int64($0.duration * 1000) } ?? 0,
                                    download: next.map { media.cardDownload($0) } ?? .none,
                                    ready: group.episodes.filter { !$0.watched && !media.finishedHere($0) }.map(\.number),
                                    total: group.total, movie: group.movie == true,
                                    unlockEpisode: group.schedule?.nextEpisode, unlockAt: group.schedule?.nextUnlockAt, now: now)
    }

    /// Play on the cover: the next episode to watch, its downloaded copy here, else from the Mac (as
    /// its row's Watch, also while it downloads). Never disabled: a disabled button let the tap
    /// through to the card, which opened the episode list; while another player starts it only dims
    /// (the launcher ignores the tap).
    private func playButton() -> AnyView? {
        let macAvailable = media.app.macConnection != nil && !media.app.macOffline
        guard let action = AnimeCardPlay.action(group.episodes.map(media.choiceItem), macAvailable: macAvailable),
              let item = group.episodes.first(where: { $0.id == action.id }) else { return nil }
        let busy = launcher.checking == item.id || media.streamJobs[item.id] != nil
        let dimmed = launcher.busy(media)
        switch action {
        case .playHere:
            guard let local = media.localEpisodes[item.id] else { return nil }
            let title = (local.positionMs > 5000 ? "Continue " : "Play ") + "ep \(item.number)"
            return AnyView(CardPlayButton(label: title, busy: busy, dimmed: dimmed) { launcher.start(local, media: media) })
        case .watchFromMac:
            // Downloading with the copy ready on the Mac: plays now from the part already here.
            if media.downloadingCopies[item.id] != nil {
                let title = (media.streamPositionMs(item) > 5000 ? "Continue " : "Play ") + "ep \(item.number)"
                return AnyView(CardPlayButton(label: title, hint: "Plays now from the part downloaded and the Mac", busy: busy, dimmed: dimmed) {
                    launcher.watch(item, media: media)
                })
            }
            let title = (media.streamPositionMs(item) > 5000 ? "Continue " : "Watch ") + "ep \(item.number)"
            return AnyView(CardPlayButton(label: title, hint: "Plays from the Mac without downloading", busy: busy, dimmed: dimmed) {
                launcher.watch(item, media: media)
            })
        }
    }

    /// What EpisodeAction offers for the next episode, as menu items.
    @ViewBuilder private func actions(_ item: EpisodeItem) -> some View {
        if let local = media.localEpisodes[item.id] {
            Button { launcher.start(local, media: media) } label: {
                Label((local.positionMs > 5000 ? "Continue " : "Play ") + "ep \(item.number)", systemImage: "play.fill")
            }.disabled(launcher.busy(media))
            Button(role: .destructive) { deletePrompt = item } label: {
                Label("Delete ep \(item.number) from \(PudgeTheme.device)", systemImage: "trash")
            }
        } else if media.videoJobs[item.id] != nil || media.videoQueue.contains(where: { $0.id == item.id }) {
            if media.downloadingCopies[item.id] != nil {
                Button { launcher.watch(item, media: media) } label: {
                    Label((media.streamPositionMs(item) > 5000 ? "Continue " : "Play ") + "ep \(item.number) while it downloads", systemImage: "play.fill")
                }.disabled(media.app.macConnection == nil || media.app.macOffline || launcher.busy(media))
            }
            Button(role: .destructive) { media.cancelDownload(item.id) } label: {
                Label("Cancel download of ep \(item.number)", systemImage: "xmark.circle")
            }
        } else if media.streamJobs[item.id] != nil {
            Button(role: .destructive) { media.cancelStream(item.id) } label: {
                Label("Cancel watching ep \(item.number)", systemImage: "xmark.circle")
            }
        } else {
            let unavailable = media.app.macConnection == nil || media.app.macOffline
            Button { launcher.watch(item, media: media) } label: {
                Label((media.streamPositionMs(item) > 5000 ? "Continue " : "Watch ") + "ep \(item.number) from the Mac", systemImage: "play.fill")
            }.disabled(unavailable || launcher.busy(media))
            Button { Task { await media.download(item) } } label: {
                Label("Download ep \(item.number)", systemImage: "arrow.down.circle")
            }.disabled(unavailable)
            if media.app.macSupports("video-quality") {
                Menu {
                    DownloadQualityChoices(media: media, item: item)
                } label: {
                    Label("Download ep \(item.number) as…", systemImage: "arrow.down.circle.dotted")
                }.disabled(unavailable)
            }
        }
    }
}

/// A Watching / Planning title (AniList) as a card; the Mac decides what gets ready.
struct PlanningCard: View {
    @ObservedObject var media: MediaModel
    let item: PlanningItem
    let width: CGFloat
    var body: some View {
        LibraryCard(title: item.title, subtitle: airing,
                    status: CardStatusText.planning(progress: item.progress, episodes: item.episodes, ready: item.readyEpisodes),
                    width: width,
                    cover: { CardCover(media: media, path: "cover/anime/\(item.mediaId)", symbol: "play.rectangle") })
    }
    /// The Mac card's airing line: "Episode 8 in 3d".
    private var airing: String? {
        guard let next = item.nextEpisode, let at = item.nextAiringAt else { return nil }
        return CardStatusText.caughtUp(nextEpisode: next, nextAt: Double(at), now: Date().timeIntervalSince1970).text
    }
}

/// Play / continue a downloaded episode; watch from the Mac or download one that is not here.
struct EpisodeAction: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let item: EpisodeItem
    /// The list's emphasis (EpisodeRowStyle): the next to watch in the accent, a watched one grey and smaller.
    var style: EpisodeRowStyle = .plain
    private var pillTint: Color {
        switch style {
        case .next: return PudgeTheme.accent
        case .watched: return Color.gray.opacity(0.55)
        case .plain: return PudgeTheme.accent.opacity(0.6)
        }
    }
    var body: some View {
        if let local = media.localEpisodes[item.id] {
            HStack(spacing: 10) {
                PillButton(title: (local.positionMs > 5000 ? "Continue " : "Play ") + "ep \(item.number)",
                           systemImage: "play.fill", tint: pillTint, compact: style == .watched) { launcher.start(local, media: media) }
                    .disabled(launcher.busy(media))
                if launcher.checking == item.id { ProgressView() }
            }
        } else if let job = media.videoJobs[item.id] {
            VStack(alignment: .leading, spacing: 4) {
                // Cancel on the left, with the episode's other actions.
                HStack {
                    Button("Cancel") { media.cancelDownload(item.id) }.buttonStyle(.borderless).font(.caption)
                    Text("Ep \(item.number): " + job.phase).font(.caption)
                    Spacer()
                    // The copy is coming: Play starts now from the part here, the rest from the Mac.
                    if media.downloadingCopies[item.id] != nil {
                        Button { launcher.watch(item, media: media) } label: {
                            Label("Play", systemImage: "play.fill").font(.caption.bold())
                        }
                        .buttonStyle(.borderless)
                        .disabled(media.app.macConnection == nil || media.app.macOffline || launcher.busy(media))
                        .accessibilityLabel("Play ep \(item.number) while it downloads")
                    }
                    if launcher.checking == item.id { ProgressView() }
                }
                ProgressView(value: job.fraction)
            }
        } else if media.videoQueue.contains(where: { $0.id == item.id }) {
            HStack {
                Button("Cancel") { media.cancelDownload(item.id) }.buttonStyle(.borderless).font(.caption)
                Label("Ep \(item.number) queued", systemImage: "clock").font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
        } else if let job = media.streamJobs[item.id] {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Button("Cancel") { media.cancelStream(item.id) }.buttonStyle(.borderless).font(.caption)
                    Text("Ep \(item.number): " + job.phase).font(.caption)
                    Spacer()
                }
                ProgressView(value: job.fraction)
            }
        } else {
            let unavailable = media.app.macConnection == nil || media.app.macOffline
            HStack(spacing: 10) {
                // Streams from the Mac over the local network; nothing stays on this device.
                PillButton(title: (media.streamPositionMs(item) > 5000 ? "Continue " : "Watch ") + "ep \(item.number)",
                           systemImage: "play.fill", tint: pillTint, compact: style == .watched) { launcher.watch(item, media: media) }
                    .disabled(unavailable || launcher.busy(media))
                    .accessibilityHint("Plays from the Mac without downloading")
                // A tap downloads the copy chosen in Settings; held, every copy with its size.
                Menu {
                    if media.app.macSupports("video-quality") { DownloadQualityChoices(media: media, item: item) }
                } label: {
                    Label("Download", systemImage: "arrow.down.circle").font(style == .watched ? Font.caption.bold() : Font.subheadline.bold())
                } primaryAction: {
                    Task { await media.download(item) }
                }
                .buttonStyle(.borderless)
                .disabled(unavailable)
                .accessibilityLabel("Download ep \(item.number) to watch without the Mac")
                if launcher.checking == item.id { ProgressView() }
            }
        }
    }
}

/// One button per copy of the episode (original, 720p, 540p) with its size: the Mac's
/// (exact once prepared) or estimated from the duration ("~").
struct DownloadQualityChoices: View {
    @ObservedObject var media: MediaModel
    let item: EpisodeItem
    var body: some View {
        let chosen = VideoQuality.stored(VideoQuality.downloadKey)
        ForEach(VideoQuality.allCases, id: \.self) { quality in
            Button { Task { await media.download(item, quality: quality) } } label: {
                Label(Self.title(quality, item: item), systemImage: quality == chosen ? "checkmark" : "arrow.down.circle")
            }
        }
    }

    static func title(_ quality: VideoQuality, item: EpisodeItem) -> String {
        guard let size = quality.size(in: item.qualities, durationSeconds: item.duration) else { return quality.title }
        return quality.title + " · " + (size.estimated ? "~" : "") + PudgeTheme.size(size.bytes)
    }
}

struct AnimeDetailScreen: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let group: AnimeGroup
    /// Live version of the group (downloads and refreshes change it).
    private var current: AnimeGroup {
        let value: AnimeGroup = media.animeGroups.first { $0.id == group.id }.map { (live: AnimeGroup) -> AnimeGroup in
            var merged = live
            for item in group.episodes where !merged.episodes.contains(where: { $0.id == item.id }) { merged.episodes.append(item) }
            merged.episodes.sort { $0.number < $1.number }
            merged.episodes = media.oneCopyEach(merged.episodes)
            return merged
        } ?? group
        // "Watched N of M" counts an episode just watched here (the Mac's list may be older).
        return media.shown([value]).first ?? value
    }
    @State private var picking = false
    var body: some View {
        let value = current
        let missing = value.episodes.filter { media.localEpisodes[$0.id] == nil && !media.finishedHere($0) }
        let pickRows = EpisodeDownloadSheet.rows(value.episodes, media: media)
        let pickAction = EpisodeDownloadPicker.action(pickRows)
        List {
            Section {
                HStack(spacing: 14) {
                    RemoteCover(media: media, path: "cover/anime/\(value.mediaId)", width: 90, symbol: "play.rectangle")
                    VStack(alignment: .leading, spacing: 6) {
                        Text(value.title).font(.title3.bold())
                        if let total = value.total { Text("Watched \(value.progress) of \(total)").font(.caption).foregroundStyle(.secondary) }
                        if let schedule = value.schedule, let at = schedule.nextUnlockAt {
                            Label("Weekly schedule · next " + Date(timeIntervalSince1970: at).formatted(date: .abbreviated, time: .shortened),
                                  systemImage: "calendar").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                if !missing.isEmpty {
                    Button { media.downloadAll(missing) } label: { Label("Download all \(missing.count)", systemImage: "square.and.arrow.down.on.square") }
                        .disabled(media.app.macConnection == nil || media.app.macOffline)
                }
            }
            Section("Episodes") {
                if value.episodes.isEmpty {
                    // A Caught up / Waiting on the Mac title: why nothing is listed.
                    Text(media.macHomeCard(value.mediaId).map { CardStatusText.macHome($0, now: Date().timeIntervalSince1970).text }
                         ?? "No episodes ready yet").foregroundStyle(.secondary)
                }
                // The next to watch stands out; watched ones are grey and smaller (still playable).
                let styles = EpisodeRowStyle.rows(value.episodes.map(media.choiceItem))
                ForEach(value.episodes) { item in
                    EpisodeRow(media: media, launcher: launcher, item: item, style: styles[item.id] ?? .plain)
                        .listRowBackground(styles[item.id] == .next ? PudgeTheme.accent.opacity(0.16) : nil)
                }
            }
        }
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        .japaneseNavigationTitle(value.title)
        .refreshable { media.app.retryMacNow(); await media.refreshEpisodes() }
        .toolbar {
            // Top left: one episode downloads at once (Settings' quality, like its own Download);
            // several open the sheet to tick them (EpisodeDownloadPicker).
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    switch pickAction {
                    case .download(let id):
                        if let item = value.episodes.first(where: { $0.id == id }) { Task { await media.download(item) } }
                    case .choose: picking = true
                    case .unavailable: break
                    }
                } label: {
                    Label("Download episodes", systemImage: "arrow.down.circle")
                }
                .disabled(pickAction == .unavailable || media.app.macConnection == nil || media.app.macOffline)
                .accessibilityIdentifier("anime-download-episodes")
            }
        }
        .sheet(isPresented: $picking) {
            EpisodeDownloadSheet(media: media, episodes: value.episodes, mediaId: value.mediaId)
        }
    }
}

/// The title's episodes to tick for download (AnimeDetailScreen's toolbar button): those on this
/// device or on their way are ticked and fixed; "Download" queues the rest through downloadAll
/// (one at a time, nearest to the next to watch first, Settings' quality).
struct EpisodeDownloadSheet: View {
    @ObservedObject var media: MediaModel
    let episodes: [EpisodeItem]
    let mediaId: Int
    @Environment(\.dismiss) private var dismiss
    @State private var selection: Set<Int> = []
    @State private var started = false

    static func rows(_ episodes: [EpisodeItem], media: MediaModel) -> [EpisodePickRow] {
        episodes.map { item in
            let state: EpisodePickRow.State = media.localEpisodes[item.id] != nil ? .onDevice
                : (media.videoJobs[item.id] != nil || media.videoQueue.contains { $0.id == item.id }) ? .downloading
                : .available
            return EpisodePickRow(id: item.id, number: item.number, watched: item.watched || media.finishedHere(item), state: state)
        }
    }

    var body: some View {
        let rows = Self.rows(episodes, media: media)
        let next = media.nextToWatchNumber(mediaId)
        // The episode's own title under its number (as in the episode list).
        let names = Dictionary(episodes.filter { !$0.episodeTitle.isEmpty && $0.episodeTitle != $0.title }.map { ($0.id, $0.episodeTitle) },
                               uniquingKeysWith: { first, _ in first })
        NavigationStack {
            List {
                Section {
                    Button("All not watched") { selection = EpisodeDownloadPicker.notWatched(rows) }
                    Menu("Next…") {
                        ForEach([1, 2, 3, 5, 10], id: \.self) { count in
                            Button("Next \(count)") { selection = EpisodeDownloadPicker.next(count, in: rows, from: next) }
                        }
                    }
                    Button("None") { selection = [] }.disabled(selection.isEmpty)
                }
                Section("Episodes") {
                    ForEach(rows, id: \.id) { row in
                        Button { selection = EpisodeDownloadPicker.toggled(selection, id: row.id, in: rows) } label: {
                            HStack {
                                Image(systemName: row.fixed || selection.contains(row.id) ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(row.fixed ? Color.secondary : Color.accentColor)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("Episode \(row.number)").foregroundStyle(row.fixed ? Color.secondary : Color.primary)
                                    if let name = names[row.id] {
                                        Text(name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                }
                                Spacer()
                                if row.state == .onDevice {
                                    Text("On \(PudgeTheme.device)").font(.caption).foregroundStyle(.secondary)
                                } else if row.state == .downloading {
                                    Text("Downloading").font(.caption).foregroundStyle(.secondary)
                                } else if row.watched {
                                    Text("Watched").font(.caption).foregroundStyle(.secondary)
                                }
                            }.contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(row.fixed)
                    }
                }
            }
            .scrollContentBackground(.hidden).background(PudgeTheme.background)
            .navigationTitle("Download episodes")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    let chosen = EpisodeDownloadPicker.chosen(selection, in: rows)
                    Button(chosen.isEmpty ? "Download" : "Download \(chosen.count)") {
                        let items = chosen.compactMap { id in episodes.first { $0.id == id } }
                        media.downloadAll(items)
                        dismiss()
                    }
                    .disabled(chosen.isEmpty || media.app.macConnection == nil || media.app.macOffline)
                }
            }
            .onAppear {
                guard !started else { return }
                started = true
                selection = EpisodeDownloadPicker.initial(rows, nextToWatch: next)
            }
        }
        .presentationDetents([.medium, .large])
    }
}

struct EpisodeRow: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let item: EpisodeItem
    /// PudgeCore EpisodeRowStyle: the next to watch has the main emphasis, a watched one is grey and smaller.
    var style: EpisodeRowStyle = .plain
    @State private var deletePrompt = false
    private var titleFont: Font {
        switch style {
        case .next: return .title3.bold()
        case .watched: return .subheadline
        case .plain: return .headline
        }
    }
    var body: some View {
        let local = media.localEpisodes[item.id]
        VStack(alignment: .leading, spacing: style == .watched ? 5 : 8) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    if style == .next {
                        Text("Next to watch").font(.caption2.bold()).foregroundStyle(PudgeTheme.accent)
                    }
                    Text("Episode \(item.number)").font(titleFont)
                        .foregroundStyle(style == .watched ? Color.secondary : Color.primary)
                    if !item.episodeTitle.isEmpty && item.episodeTitle != item.title {
                        Text(item.episodeTitle).font(style == .watched ? Font.caption2 : Font.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    if item.subtitleNotAligned == true {
                        Text("Subtitles not aligned").font(.caption).foregroundStyle(.orange)
                    }
                }
                Spacer()
                if item.watched || media.finishedHere(item) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(style == .watched ? Color.secondary : PudgeTheme.good)
                        .accessibilityLabel("Watched")
                }
                if let local {
                    Text(PudgeTheme.size(local.sizeBytes)).font(.caption).foregroundStyle(.secondary)
                    Button(role: .destructive) { deletePrompt = true } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless).accessibilityLabel("Delete from \(PudgeTheme.device)")
                        // A popover on the trash can itself: a confirmation dialog inside a list row
                        // lost its anchor on iPad (landscape) and showed in the middle of the screen.
                        .deleteConfirmation("Delete this episode from the \(PudgeTheme.device)?", isPresented: $deletePrompt) {
                            media.deleteEpisode(item.id)
                        }
                }
            }
            PendingDeletionLine(media: media, id: item.id)
            EpisodeAction(media: media, launcher: launcher, item: item, style: style)
        }
        .padding(.vertical, style == .watched ? 1 : 4)
    }
}

// MARK: Player

final class PlayerLayerView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
    /// isReadyForDisplay: the first video frame is there (the player's watchdog and log).
    var readyObservation: NSKeyValueObservation?
}

struct VideoSurface: UIViewRepresentable {
    let player: AVPlayer
    /// Called once on the main thread when the layer has its first video frame.
    var onFirstFrame: (@MainActor () -> Void)? = nil
    func makeUIView(context: Context) -> PlayerLayerView {
        let view = PlayerLayerView()
        view.playerLayer.player = player
        view.playerLayer.videoGravity = .resizeAspect
        view.backgroundColor = .black
        let report = onFirstFrame
        view.readyObservation = view.playerLayer.observe(\.isReadyForDisplay, options: [.initial, .new]) { layer, _ in
            guard layer.isReadyForDisplay else { return }
            DispatchQueue.main.async { MainActor.assumeIsolated { report?() } }
        }
        return view
    }
    static func dismantleUIView(_ view: PlayerLayerView, coordinator: ()) {
        view.readyObservation = nil
        view.playerLayer.player = nil
    }
    func updateUIView(_ view: PlayerLayerView, context: Context) { view.playerLayer.player = player }
}

/// A subtitle line with words, drawn by the player's touch layer (PlayerTouchView).
struct SubtitleLine {
    let paragraph: TextParagraph
    let style: ReaderStyle
    /// The box behind the words (JitenMPV draws outlined words without one).
    let boxed: Bool
    /// Points from the line's bottom to the Skip button / the controls (more while they are hidden).
    let bottomGap: CGFloat
    /// Room above and below the text for the selection handles' knobs (JapaneseTextView.handleRoom).
    let handleRoom: CGFloat
    let onTap: (Int) -> Void
    let onSelect: () -> Void
    let onDeselect: () -> Void
}

/// The video's taps, pinch and pan as UIKit recognizers on a clear view over the picture (under
/// the controls, which take their own touches first). SwiftUI's gestures on a clear view proved
/// unreliable here (a GestureMask of `.none` on the pan disabled the tap and the pinch with it);
/// UIKit's recognizers are not affected by SwiftUI's gesture composition.
/// - One-finger taps go to PudgeCore VideoTaps as they happen (a series of taps on a side adds
///   10 s each, which `require(toFail:)` between a single- and a double-tap recognizer cannot
///   express); a lone tap toggles the controls `doubleTapInterval` later, on the main run loop.
/// - Pinch (two fingers) zooms around where it began; a one-finger drag pans, only while zoomed.
/// - The subtitle line with words lives in this view too (PlayerTouchView): a touch on it, on
///   its selection or on a selection handle is the line's, never the video's.
struct VideoGestureLayer: UIViewRepresentable {
    var zoomed: Bool
    /// The line with words on screen (nil: none, or a plain line SwiftUI draws).
    var subtitle: SubtitleLine? = nil
    /// The Skip button and the controls in window coordinates: the line ends above `minY`,
    /// between `minX` and `maxX` (the safe area's width).
    var subtitleBase: CGRect = .zero
    var onTap: (VideoTapAction) -> Void
    var onZoom: (VideoZoomGesture) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> PlayerTouchView {
        let view = PlayerTouchView()
        let coordinator = context.coordinator
        let tap = UITapGestureRecognizer(target: coordinator, action: #selector(Coordinator.tapped(_:)))
        tap.numberOfTapsRequired = 1
        tap.numberOfTouchesRequired = 1
        let pinch = UIPinchGestureRecognizer(target: coordinator, action: #selector(Coordinator.pinched(_:)))
        let pan = UIPanGestureRecognizer(target: coordinator, action: #selector(Coordinator.panned(_:)))
        pan.minimumNumberOfTouches = 1
        pan.maximumNumberOfTouches = 1
        for recognizer in [tap, pinch, pan] as [UIGestureRecognizer] {
            recognizer.delegate = coordinator
            view.addGestureRecognizer(recognizer)
        }
        coordinator.pan = pan
        coordinator.pinch = pinch
        return view
    }

    func updateUIView(_ view: PlayerTouchView, context: Context) {
        context.coordinator.layer = self
        view.show(subtitle, above: subtitleBase)
    }

    static func dismantleUIView(_ view: PlayerTouchView, coordinator: Coordinator) { coordinator.stopWaiting() }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var layer: VideoGestureLayer
        private var taps = VideoTaps()
        private var focus = CGPoint.zero
        weak var pan: UIPanGestureRecognizer?
        weak var pinch: UIPinchGestureRecognizer?

        init(_ layer: VideoGestureLayer) { self.layer = layer }

        @objc func tapped(_ recognizer: UITapGestureRecognizer) {
            guard recognizer.state == .ended, let view = recognizer.view else { return }
            let point = recognizer.location(in: view)
            let now = ProcessInfo.processInfo.systemUptime
            stopWaiting()
            if let action = taps.tap(x: Double(point.x), width: Double(view.bounds.width), at: now) {
                layer.onTap(action)
                // A tap on the other side than the first one: the first toggled the controls,
                // this one waits as a first tap (VideoTaps: both taps of a double tap in one zone).
                if taps.waiting {
                    perform(#selector(settle), with: nil, afterDelay: VideoTaps.doubleTapInterval, inModes: [.common])
                }
                return
            }
            // A lone tap toggles the controls once no second tap came.
            perform(#selector(settle), with: nil, afterDelay: VideoTaps.doubleTapInterval, inModes: [.common])
        }

        @objc func settle() {
            if let action = taps.settle(at: ProcessInfo.processInfo.systemUptime) { layer.onTap(action) }
        }

        func stopWaiting() {
            NSObject.cancelPreviousPerformRequests(withTarget: self, selector: #selector(settle), object: nil)
        }

        @objc func pinched(_ recognizer: UIPinchGestureRecognizer) {
            guard let view = recognizer.view else { return }
            switch recognizer.state {
            case .began, .changed:
                if recognizer.state == .began {
                    let point = recognizer.location(in: view)
                    focus = CGPoint(x: point.x - view.bounds.midX, y: point.y - view.bounds.midY)
                }
                layer.onZoom(.pinch(magnification: Double(recognizer.scale), focusX: Double(focus.x), focusY: Double(focus.y)))
            case .ended, .cancelled, .failed:
                layer.onZoom(.pinchEnded)
            default:
                break
            }
        }

        @objc func panned(_ recognizer: UIPanGestureRecognizer) {
            guard let view = recognizer.view else { return }
            switch recognizer.state {
            case .began, .changed:
                let moved = recognizer.translation(in: view)
                layer.onZoom(.pan(dx: Double(moved.x), dy: Double(moved.y)))
            case .ended, .cancelled, .failed:
                layer.onZoom(.panEnded)
            default:
                break
            }
        }

        /// A touch on the subtitle line (a word, its selection, a handle's target past its edges)
        /// or on any selection on screen (or while a handle is dragged) is the text's: no tap,
        /// pinch or pan of the video starts from it.
        func gestureRecognizer(_ recognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
            return MainActor.assumeIsolated {
                if let view = recognizer.view as? PlayerTouchView, view.lineOwns(touch) { return false }
                // Taken in the layer and converted to its own window (TextSelectionController's
                // space): `location(in: nil)` was the touch's window, in portrait after a rotation.
                let point = touch.windowPoint(in: recognizer.view)
                guard TextSelectionController.activeOwnsTouch(at: point) else { return true }
                SelectionTrace.log("video \(SelectionTrace.name(recognizer)) leaves a touch to the selection")
                return false
            }
        }

        /// The drag only pans a zoomed picture (at 1× it does not start, nothing else waits on it).
        func gestureRecognizerShouldBegin(_ recognizer: UIGestureRecognizer) -> Bool {
            recognizer === pan ? layer.zoomed : true
        }

        /// Pinch and pan go together (VideoZoom ignores the pan while the pinch lasts).
        func gestureRecognizer(_ recognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
            (recognizer === pinch && other === pan) || (recognizer === pan && other === pinch)
        }
    }
}

/// The player's touch layer (VideoGestureLayer) with the subtitle line with words in it, both
/// UIKit. In SwiftUI the line's selection handles did not move in the player (they did in the
/// reader): SwiftUI hit-tests a hosted view only inside the frame it gives it (a knob hangs past
/// the line), and SwiftUI's views and gestures over the line came between the finger and the
/// handles' drag. This view covers the whole player and hit-tests itself: a selection handle
/// first (its 44-point target, also where it hangs past the line), then the line, then the
/// video's own taps, pinch and pan; the line is one view for the player's lifetime (a SwiftUI
/// redraw or the next line never makes it again mid-drag). Only the controls, the panels and
/// the buttons over it take their touches first (SwiftUI, above this layer).
final class PlayerTouchView: UIView {
    let line = JapaneseTextView()
    private let box = UIView()
    private var base = CGRect.zero
    private var bottomGap: CGFloat = 8
    private var traced: TimeInterval = -1

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .clear
        isOpaque = false
        isMultipleTouchEnabled = true
        box.backgroundColor = UIColor.black.withAlphaComponent(0.45)
        box.layer.cornerRadius = 10
        box.isUserInteractionEnabled = false
        box.isHidden = true
        line.isHidden = true
        line.tripleTap = .paragraph
        addSubview(box)
        addSubview(line)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    /// The line on screen now (nil: none), ending above `base` (window coordinates).
    func show(_ subtitle: SubtitleLine?, above base: CGRect) {
        self.base = base
        if let subtitle {
            line.paragraph = subtitle.paragraph
            line.style = subtitle.style
            line.handleRoom = subtitle.handleRoom
            line.onTap = subtitle.onTap
            line.onSelect = subtitle.onSelect
            line.onDeselect = subtitle.onDeselect
            bottomGap = subtitle.bottomGap
            box.isHidden = !subtitle.boxed
        } else {
            // No line (a gap between lines): nothing of it stays selected.
            if !line.isHidden, line.selection.hasSelection { line.selection.clear() }
            box.isHidden = true
        }
        line.isHidden = subtitle == nil
        setNeedsLayout()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        guard !line.isHidden, window != nil else { return }
        // Before SwiftUI measured the controls: the safe area's bottom.
        let area = base.width > 60 ? fromWindow(base) : bounds.inset(by: safeAreaInsets)
        let bottom = (base.width > 60 ? area.minY : area.maxY) - bottomGap
        let outer = area.insetBy(dx: 24, dy: 0)
        let width = max(1, outer.width - 28)
        let height = line.height(for: width)
        let frame = CGRect(x: outer.minX + 14, y: bottom - height, width: width, height: height)
        if line.frame != frame { line.frame = frame }
        box.frame = CGRect(x: outer.minX, y: frame.minY, width: outer.width, height: height)
    }

    /// Handles first, then the line, then this view (the video's recognizers).
    override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? {
        guard isUserInteractionEnabled, !isHidden, alpha > 0.01, self.point(inside: point, with: event) else { return nil }
        guard !line.isHidden else { return self }
        let selection = line.selection
        if selection.hasSelection, selection.handle(at: inWindow(point)) != nil {
            trace(event, "touch on a selection handle: the subtitle line takes it")
            return line
        }
        if line.point(inside: convert(point, to: line), with: event) {
            if selection.hasSelection { trace(event, "touch on the selected line"); return line }
            // Without a selection only the text itself (its glyph boxes per line, plus a small
            // slop) is the line's: past the words a tap shows the controls and two seek.
            if line.textContains(convert(point, to: line)) { return line }
        }
        return self
    }

    /// The touch landed on the line (a word, the selection, a handle): the video's gestures leave it.
    func lineOwns(_ touch: UITouch) -> Bool {
        guard !line.isHidden, let view = touch.view else { return false }
        return view === line || view.isDescendant(of: line)
    }

    /// One diagnostics line per touch (hit-testing runs more than once for it).
    private func trace(_ event: UIEvent?, _ text: String) {
        let stamp = event?.timestamp ?? 0
        guard stamp != traced else { return }
        traced = stamp
        SelectionTrace.log(text)
    }
}

/// The video player's audio session, set on its own serial queue: AVAudioSession's calls go to
/// the media server and can block (a busy or stuck media server froze the player on the main
/// thread: no time, no Close). In order, so a close and a quick reopen never cross.
enum PlayerAudioSession {
    private static let queue = DispatchQueue(label: "pudge.player-audio-session", qos: .userInitiated)

    static func activate() {
        queue.async {
            let started = ProcessInfo.processInfo.systemUptime
            do {
                try AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
                try AVAudioSession.sharedInstance().setActive(true)
            } catch {
                AppLog.write("player", "audio session not activated: \(error.localizedDescription)")
            }
            let took = ProcessInfo.processInfo.systemUptime - started
            if took > 1 { AppLog.write("player", String(format: "audio session activation took %.1f s", took)) }
        }
    }

    /// Lets other apps' audio go on (and the lock screen's player go away).
    static func deactivate() {
        queue.async {
            do { try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation) }
            catch { AppLog.write("player", "audio session not released: \(error.localizedDescription)") }
        }
    }
}

/// While the player is open: a background timer asks the main thread every second; a main thread
/// that does not answer for MainThreadStall.threshold seconds is logged from the background (the
/// player froze: no time, no Close, while the sound went on).
final class MainThreadMonitor: @unchecked Sendable {
    private let lock = NSLock()
    private var lastAnswer = ProcessInfo.processInfo.systemUptime
    private var lastTick = ProcessInfo.processInfo.systemUptime
    private var stallLogged = false
    private var timer: DispatchSourceTimer?
    private let label: String

    init(label: String) { self.label = label }

    func start() {
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in self?.tick() }
        lock.lock(); self.timer = timer; lock.unlock()
        timer.resume()
    }

    func stop() {
        lock.lock(); let current = timer; timer = nil; lock.unlock()
        current?.cancel()
    }

    private func tick() {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        if MainThreadStall.resumed(lastTick: lastTick, now: now) {
            // Back from the background (the timer did not run either): not a stall.
            lastAnswer = now; stallLogged = false
        }
        lastTick = now
        let last = lastAnswer, logged = stallLogged
        let log = MainThreadStall.shouldLog(lastAnswer: last, now: now, logged: logged)
        if log { stallLogged = true }
        lock.unlock()
        if log { AppLog.write("player", String(format: "main thread not answering for %.1f s (%@)", now - last, label)) }
        DispatchQueue.main.async { [weak self] in self?.answered() }
    }

    private func answered() {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        let last = lastAnswer, logged = stallLogged
        lastAnswer = now; stallLogged = false
        lock.unlock()
        if logged { AppLog.write("player", String(format: "main thread answered again after %.1f s (%@)", now - last, label)) }
    }
}

@MainActor
final class VideoController: ObservableObject {
    let player: AVPlayer
    @Published var timeMs: Int64 = 0
    @Published var durationMs: Int64 = 0
    @Published var playing = false
    /// What is already here, as parts of the position bar (0...1): its lighter track (BufferedBar).
    @Published private(set) var buffered: [ClosedRange<Double>] = []
    private var bufferTicks = 0
    /// Seconds played at normal speed since the last collection (jumps excluded).
    private(set) var played = IndexSet()
    private var lastTick: (media: Double, wall: Double)?
    private var observer: Any?
    /// Where it opened: a place within 5 s of it is not saved until the video moved away.
    var opened: OpenedPlace
    /// Watched from the Mac: the loader that feeds the player (nil: the downloaded file).
    private(set) var stream: MacVideoStream?
    /// The Mac stopped answering (or refused): paused with this message, Try again or Download.
    @Published var streamProblem: String?
    /// Why the stream stopped (nil: a message without a known reason): the Mac away reconnects on its own.
    @Published var streamFailure: VideoStreamFailure?
    /// A piece of the episode did not come at once: the Mac is asked again.
    @Published var waitingForMac = false
    /// Played to the end (the item's did-play-to-end, or stopped by itself at the end): the
    /// player closes as Close does (PlaybackEnd).
    @Published private(set) var ended = false
    private var wasPlaying = false
    private var endObserver: NSObjectProtocol?
    private var itemStatus: NSKeyValueObservation?
    /// The episode can't be played here (PlaybackWatchdog): the player shows it, Re-download / Close.
    @Published var unplayable: String?
    /// For the log: "episode 12 (downloaded)".
    private var label = "episode"
    private var openedAt = ProcessInfo.processInfo.systemUptime
    private var startSeconds: Double
    private var firstFrameAt: Double?
    private var hasVideoTrack = false
    private var closed = false
    private var watchdog: Task<Void, Never>?
    private var monitor: MainThreadMonitor?
    private var diagnostics: [NSObjectProtocol] = []
    private var itemLog: [NSKeyValueObservation] = []
    private var playerLog: NSKeyValueObservation?

    convenience init(url: URL, startMs: Int64) { self.init(item: AVPlayerItem(url: url), stream: nil, startMs: startMs) }
    convenience init(stream: MacVideoStream, startMs: Int64) { self.init(item: nil, stream: stream, startMs: startMs) }

    private init(item: AVPlayerItem?, stream: MacVideoStream?, startMs: Int64) {
        player = AVPlayer()
        opened = OpenedPlace(position: Double(startMs), tolerance: 5000)
        startSeconds = Double(startMs) / 1000
        // Off the main thread: the media server's answer can take long (PlayerAudioSession).
        PlayerAudioSession.activate()
        if let stream { attach(stream) }
        let current = stream?.playerItem() ?? item
        player.replaceCurrentItem(with: current)
        watchStatus(current)
        watchEnd(current)
        logItem(current)
        if startMs > 0 { player.seek(to: CMTime(value: startMs, timescale: 1000), toleranceBefore: .zero, toleranceAfter: .zero) }
        observer = player.addPeriodicTimeObserver(forInterval: CMTime(value: 1, timescale: 10), queue: .main) { [weak self] time in
            MainActor.assumeIsolated {
                // An invalid time (NaN) would trap in Int64(_:).
                guard let self, time.seconds.isFinite else { return }
                self.timeMs = Int64(time.seconds * 1000)
                self.playing = self.player.timeControlStatus != .paused
                self.track(time.seconds)
                if let item = self.player.currentItem, item.duration.isNumeric { self.durationMs = Int64(item.duration.seconds * 1000) }
                self.updateBuffered()
                // Stopped by itself at the end (a stream whose end notice did not come).
                if self.streamProblem == nil, !self.waitingForMac,
                   PlaybackEnd.reached(positionMs: self.timeMs, durationMs: self.durationMs, playing: self.playing, wasPlaying: self.wasPlaying) {
                    self.finish()
                }
                self.wasPlaying = self.playing
            }
        }
    }
    /// Counts a second as watched only when playback moved like real time (no seek, no skip).
    private func track(_ seconds: Double) {
        let wall = ProcessInfo.processInfo.systemUptime
        defer { lastTick = (seconds, wall) }
        guard playing, let last = lastTick, seconds.isFinite else { return }
        let moved = seconds - last.media, elapsed = wall - last.wall
        guard moved > 0, elapsed > 0, moved <= elapsed * Double(max(1, player.rate)) + 0.35 else { return }
        played.insert(integersIn: Int(last.media)..<max(Int(last.media) + 1, Int(seconds)))
    }
    func collectPlayed() -> IndexSet { defer { played = IndexSet() }; return played }

    /// The lighter track, about once a second: a downloaded file is here whole; from the Mac,
    /// AVPlayer's buffer and the bytes on this device (watch cache, the download's part).
    private func updateBuffered() {
        bufferTicks += 1
        guard bufferTicks % 10 == 1 else { return }
        guard let stream else {
            let whole: [ClosedRange<Double>] = durationMs > 0 ? [0...1] : []
            if buffered != whole { buffered = whole }
            return
        }
        var parts: [ClosedRange<Double>] = []
        if let item = player.currentItem, durationMs > 0 {
            let ranges = item.loadedTimeRanges.map { value -> (start: Double, duration: Double) in
                let range = value.timeRangeValue
                return (start: range.start.seconds, duration: range.duration.seconds)
            }
            parts += BufferedBar.fromTimes(ranges, durationSeconds: Double(durationMs) / 1000)
        }
        parts += BufferedBar.fromBytes(stream.availableBytes(), fileSize: stream.video.sizeBytes)
        let merged = BufferedBar.merge(parts)
        if merged != buffered { buffered = merged }
    }

    func toggle() { if player.timeControlStatus == .paused { player.play() } else { player.pause() }; playing = player.timeControlStatus != .paused }
    func pause() { player.pause(); playing = false }
    func seek(_ ms: Int64) {
        let target = max(0, durationMs > 0 ? min(durationMs, ms) : ms)
        timeMs = target
        player.seek(to: CMTime(value: target, timescale: 1000), toleranceBefore: .zero, toleranceAfter: .zero)
    }
    /// The item played to its end (AVPlayerItem.didPlayToEndTimeNotification).
    private func watchEnd(_ item: AVPlayerItem?) {
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        endObserver = nil
        guard let item else { return }
        endObserver = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification, object: item,
                                                             queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                // A Mac stream cut short also posts this, far from the end: the player closed
                // mid-episode and the episode counted as watched. Only the real end closes it.
                let seconds = self.player.currentTime().seconds
                let position = seconds.isFinite ? max(self.timeMs, Int64(seconds * 1000)) : self.timeMs
                guard self.streamProblem == nil, !self.waitingForMac,
                      PlaybackEnd.noticeIsEnd(positionMs: position, durationMs: self.durationMs) else {
                    AppLog.write("player", "end notice ignored at \(position) of \(self.durationMs) ms (\(self.label))")
                    return
                }
                self.finish()
            }
        }
    }

    private func finish() {
        guard !ended, observer != nil else { return }
        if durationMs > 0 { timeMs = max(timeMs, durationMs) }
        playing = false
        ended = true
    }

    /// Nothing here waits for the Mac, the disk or the media server: the place is saved by the
    /// caller (a small local file), the stream's close goes to the Mac in its own task and the
    /// audio session is released on its own queue.
    /// `releaseAudio`: no audiobook is loaded (its lock-screen player stays otherwise).
    func close(releaseAudio: Bool = true) {
        guard !closed else { return }
        closed = true
        let started = ProcessInfo.processInfo.systemUptime
        AppLog.write("player", "close requested (\(label)) at \(timeMs) ms")
        watchdog?.cancel(); watchdog = nil
        player.pause()
        if let observer { player.removeTimeObserver(observer) }
        observer = nil
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        endObserver = nil
        diagnostics.forEach { NotificationCenter.default.removeObserver($0) }
        diagnostics = []
        itemLog = []; playerLog = nil
        itemStatus = nil
        stream?.close()
        player.replaceCurrentItem(with: nil)
        if releaseAudio {
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
            PlayerAudioSession.deactivate()
        }
        monitor?.stop(); monitor = nil
        AppLog.write("player", String(format: "close finished in %.0f ms (%@)", (ProcessInfo.processInfo.systemUptime - started) * 1000, label))
    }

    // MARK: Log and watchdog ([player] lines in the diagnostics)

    /// The player opened: what it plays, then the watchdog (a downloaded file that does not
    /// play stops with a message) and the main-thread check.
    func begin(episodeId: Int, local: Bool, fileBytes: Int64?, expectedMs: Int64, subtitles: Int?) {
        label = "episode \(episodeId) (\(local ? "downloaded" : "from the Mac"))"
        openedAt = ProcessInfo.processInfo.systemUptime
        let bytes = fileBytes.map { "\($0) bytes" } ?? "size unknown"
        let cues = subtitles.map { "\($0) subtitle lines" } ?? "no subtitles"
        AppLog.write("player", "open \(label): \(bytes), \(expectedMs / 1000) s expected, from \(Int(startSeconds)) s, \(cues)")
        let watch = MainThreadMonitor(label: label)
        watch.start()
        monitor = watch
        watchdog?.cancel()
        watchdog = Task { [weak self] in await self?.runWatchdog(stops: local) }
    }

    /// The layer has its first video frame (VideoSurface).
    func firstFrame() {
        guard firstFrameAt == nil, !closed else { return }
        let at = ProcessInfo.processInfo.systemUptime - openedAt
        firstFrameAt = at
        AppLog.write("player", String(format: "first video frame after %.1f s (%@)", at, label))
    }

    private func runWatchdog(stops: Bool) async {
        var wanted = 0.0
        // Only real playback counts as moving (steps of up to 2.5 s a second): the opening seek
        // to the saved place or a jump by hand are not playback.
        var advanced = 0.0
        var last: Double?
        while !Task.isCancelled, !closed {
            do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
            guard !closed else { return }
            // Counted only while it should play and is shown (no picture in the background).
            guard UIApplication.shared.applicationState == .active else { continue }
            if player.timeControlStatus != .paused { wanted += 1 }
            let item = player.currentItem
            let now = player.currentTime().seconds
            if now.isFinite {
                if let last, now - last > 0, now - last <= 2.5 { advanced += now - last }
                last = now
            }
            let size = item?.presentationSize ?? .zero
            let sample = PlaybackSample(wantedSeconds: wanted, advancedSeconds: advanced,
                                        hasVideoTrack: hasVideoTrack, presentationWidth: Double(size.width),
                                        presentationHeight: Double(size.height), firstFrameShown: firstFrameAt != nil,
                                        failed: item?.status == .failed)
            switch PlaybackWatchdog.judge(sample) {
            case .wait:
                continue
            case .healthy:
                AppLog.write("player", String(format: "playing (%@): time moves, picture %.0fx%.0f", label, size.width, size.height))
                return
            case .stuck(let problem):
                let detail = item?.error?.localizedDescription
                AppLog.write("player", "stuck (\(label)): \(problem.rawValue) after \(Int(wanted)) s playing; time \(String(format: "%.1f", now)) s, "
                             + "picture \(Int(size.width))x\(Int(size.height)), first frame \(firstFrameAt != nil), status \(item?.status.rawValue ?? -1), "
                             + "waiting \(player.reasonForWaitingToPlay?.rawValue ?? "-"), error \(detail ?? "-")")
                // From the Mac its own messages handle it (Waiting for the Mac, Try again).
                guard stops else { return }
                player.pause(); playing = false
                unplayable = PlaybackWatchdog.message(problem, detail: detail, device: PudgeTheme.device)
                return
            }
        }
    }

    /// Item status, tracks, picture size, errors and stalls into the log.
    private func logItem(_ item: AVPlayerItem?) {
        diagnostics.forEach { NotificationCenter.default.removeObserver($0) }
        diagnostics = []
        itemLog = []
        if playerLog == nil {
            playerLog = player.observe(\.timeControlStatus, options: [.new]) { [weak self] player, _ in
                let status = player.timeControlStatus.rawValue
                let reason = player.reasonForWaitingToPlay?.rawValue
                Task { @MainActor [weak self] in
                    guard let self, !self.closed else { return }
                    AppLog.write("player", "time control \(status)\(reason.map { " (\($0))" } ?? "") at \(self.timeMs) ms (\(self.label))")
                }
            }
        }
        guard let item else { return }
        itemLog.append(item.observe(\.status, options: [.new]) { [weak self] item, _ in
            let status = item.status
            let error = item.error?.localizedDescription
            Task { @MainActor [weak self] in await self?.statusChanged(item, status: status, error: error) }
        })
        itemLog.append(item.observe(\.presentationSize, options: [.new]) { [weak self] item, _ in
            let size = item.presentationSize
            Task { @MainActor [weak self] in
                guard let self, !self.closed else { return }
                AppLog.write("player", "picture size \(Int(size.width))x\(Int(size.height)) (\(self.label))")
            }
        })
        let center = NotificationCenter.default
        diagnostics.append(center.addObserver(forName: AVPlayerItem.failedToPlayToEndTimeNotification, object: item, queue: .main) { [weak self] note in
            let error = (note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error)?.localizedDescription ?? "unknown error"
            MainActor.assumeIsolated { AppLog.write("player", "failed to play to the end: \(error) (\(self?.label ?? "-"))") }
        })
        diagnostics.append(center.addObserver(forName: AVPlayerItem.newErrorLogEntryNotification, object: item, queue: .main) { [weak self] note in
            let entry = (note.object as? AVPlayerItem)?.errorLog()?.events.last
            let text = entry.map { "\($0.errorStatusCode) \($0.errorDomain) \($0.errorComment ?? "")" } ?? "-"
            MainActor.assumeIsolated { AppLog.write("player", "error log: \(text) (\(self?.label ?? "-"))") }
        })
        diagnostics.append(center.addObserver(forName: AVPlayerItem.playbackStalledNotification, object: item, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                AppLog.write("player", "stalled at \(self.timeMs) ms (\(self.label))")
            }
        })
    }

    private func statusChanged(_ item: AVPlayerItem, status: AVPlayerItem.Status, error: String?) async {
        guard !closed else { return }
        switch status {
        case .readyToPlay:
            let tracks = (try? await item.asset.load(.tracks)) ?? []
            hasVideoTrack = tracks.contains { $0.mediaType == .video }
            var names: [String] = []
            for track in tracks {
                if track.mediaType == .video { names.append("video " + (await VideoFileProbe.describe(track))) }
                else { names.append(track.mediaType.rawValue) }
            }
            let duration = item.duration.isNumeric ? String(format: "%.1f s", item.duration.seconds) : "no duration"
            AppLog.write("player", String(format: "ready after %.1f s (%@): %@, tracks: %@", ProcessInfo.processInfo.systemUptime - openedAt,
                                          label, duration, names.joined(separator: ", ")))
        case .failed:
            AppLog.write("player", "failed (\(label)): \(error ?? "unknown error")")
        default:
            break
        }
    }

    // MARK: Watching from the Mac

    /// Try again after the Mac stopped answering: a new stream, on from the same place.
    func resume(with fresh: MacVideoStream) {
        // Closed while Try again was on its way (the player went away): nothing to resume.
        guard observer != nil else { fresh.close(); return }
        let at = timeMs
        stream?.close()
        attach(fresh)
        streamProblem = nil; streamFailure = nil; waitingForMac = false
        let item = fresh.playerItem()
        player.replaceCurrentItem(with: item)
        watchStatus(item)
        watchEnd(item)
        logItem(item)
        seek(at)
        player.play(); playing = true
    }

    private func attach(_ fresh: MacVideoStream) {
        stream = fresh
        fresh.onEvent = { [weak self, weak fresh] event in
            Task { @MainActor [weak self, weak fresh] in
                guard let self, let fresh, self.stream === fresh else { return }
                self.streamEvent(event)
            }
        }
    }

    private func streamEvent(_ event: MacVideoStream.Event) {
        switch event {
        case .waiting: waitingForMac = true
        case .flowing: waitingForMac = false
        case .failed(let failure): stopStream(failure)
        }
    }

    /// A streamed item that fails on its own (e.g. after a failed piece) stops as the stream does.
    private func watchStatus(_ item: AVPlayerItem?) {
        itemStatus = nil
        guard stream != nil, let item else { return }
        itemStatus = item.observe(\.status) { [weak self] _, _ in
            Task { @MainActor [weak self] in self?.itemFailed() }
        }
    }

    private func itemFailed() {
        guard stream != nil, let item = player.currentItem, item.status == .failed else { return }
        AppLog.write("video", "streamed episode stopped: \(item.error?.localizedDescription ?? "unknown error")")
        stopStream(.unreachable)
    }

    private func stopStream(_ failure: VideoStreamFailure) {
        waitingForMac = false
        // A stop without a reason never replaces the reason the Mac gave.
        if streamProblem != nil && failure == .unreachable { return }
        player.pause(); playing = false
        streamProblem = Self.message(failure)
        streamFailure = failure
    }

    static func message(_ failure: VideoStreamFailure) -> String {
        switch failure {
        case .revoked:
            return "The Mac no longer accepts this \(PudgeTheme.device). Connect the Mac again in Settings."
        case .changed:
            return "The Mac's copy of this episode changed. Try again to go on watching."
        case .unreachable, .interrupted, .refused:
            return "The Mac stopped answering. Check that it is awake with Pudge open and on the same Wi-Fi as this \(PudgeTheme.device), then try again, or download the episode to watch it without the Mac."
        }
    }
}

/// The orientations the app allows now (PlayerOrientation), asked by the app delegate. On the
/// iPhone the video player is landscape on its own, as in video apps; the iPad is unchanged.
@MainActor
enum PlayerOrientationGate {
    private(set) static var allowed: PlayerOrientation.Allowed = .app
    private static var isPad: Bool { UIDevice.current.userInterfaceIdiom == .pad }

    static var mask: UIInterfaceOrientationMask {
        switch allowed {
        // The app's own orientations (Info.plist): the iPhone without upside down.
        case .app: return isPad ? .all : .allButUpsideDown
        case .landscape: return .landscape
        case .only(let side): return mask(side)
        }
    }

    private static func mask(_ side: ScreenSide) -> UIInterfaceOrientationMask {
        switch side {
        case .portrait: return .portrait
        case .portraitUpsideDown: return .portraitUpsideDown
        case .landscapeLeft: return .landscapeLeft
        case .landscapeRight: return .landscapeRight
        }
    }

    private static var scene: UIWindowScene? {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        return scenes.first { $0.activationState == .foregroundActive } ?? scenes.first
    }

    /// The side the screen shows now (nil: unknown).
    static var currentSide: ScreenSide? {
        switch scene?.interfaceOrientation {
        case .portrait: return .portrait
        case .portraitUpsideDown: return .portraitUpsideDown
        case .landscapeLeft: return .landscapeLeft
        case .landscapeRight: return .landscapeRight
        default: return nil
        }
    }

    /// The player opened, closed or its lock changed: the allowed orientations follow, and the
    /// screen turns to them now (landscape for the player; back to the device's way after it).
    static func apply(playerOnScreen: Bool, lock: PlayerRotationLock) {
        allowed = PlayerOrientation.allowed(isPad: isPad, playerOnScreen: playerOnScreen, lock: lock)
        guard let scene else { return }
        for window in scene.windows {
            var controller = window.rootViewController
            while let current = controller {
                current.setNeedsUpdateOfSupportedInterfaceOrientations()
                controller = current.presentedViewController
            }
        }
        guard allowed != .app else { return }
        scene.requestGeometryUpdate(.iOS(interfaceOrientations: mask)) { error in
            AppLog.write("video", "orientation not changed: \(error.localizedDescription)")
        }
    }
}

struct VideoPlayerScreen: View {
    @ObservedObject var media: MediaModel
    let episode: LocalEpisode
    /// Watched from the Mac (nil: the downloaded file).
    let stream: StreamPlayback?
    @StateObject private var video: VideoController
    @State private var retrying = false
    /// The player's own rotation lock (iPhone): one landscape side, or follow the device.
    @State private var rotationLock: PlayerRotationLock = .followDevice
    @Environment(\.dismiss) private var dismiss
    @State private var track: SubtitleTrack?
    /// Set once the player went (onDisappear): a subtitle read still running starts nothing.
    @State private var playerClosed = false
    @State private var chrome = true
    @State private var lookup: WordLookup?
    @State private var textLookup: TextLookup?
    @State private var resumeAfterLookup = false
    /// When the place goes to the Mac while this plays (continue there in mpv).
    @State private var positionSync = PositionSyncSchedule()
    @Environment(\.scenePhase) private var phase
    /// Subtitle timing of this episode (kept per episode, shared with the Mac's mpv delay).
    @ObservedObject private var delays: SubtitleDelayStore
    private var delayMs: Int { delays.ms(episode.id) }
    /// Subtitle text size, % of the default (JitenMPV's share of the picture height, or 24 points
    /// without the Mac's look): its own wheel and panel (not with the timing), kept on this device for all episodes.
    @AppStorage(SubtitleTextSize.defaultsKey) private var textPercent = SubtitleTextSize.standard
    @State private var screenBox: CGSize = .zero
    /// The Skip button and the controls (window coordinates): the subtitle line ends above them.
    @State private var subtitleBase: CGRect = .zero
    @AppStorage("pudge.subtitleFurigana") private var furigana = "off"
    /// The "+20 s" that taps on the video show (the taps themselves: VideoGestureLayer).
    @State private var ripple: SeekRipple?
    @State private var rippleSerial = 0
    /// Pinch to zoom the picture (not the subtitles), pan while zoomed.
    @State private var zoom = VideoZoom()
    /// The open subtitle wheel panel (timing or text size, each with its own button; one at a time).
    @State private var wheelPanel: PlayerWheelPanel?
    /// The subtitle line whose text is selected: it stays on screen while the selection lasts.
    @State private var heldCue: Int?
    /// Grows with every interaction (tap, drag, wheel, button): the controls hide
    /// PlayerChromeTimer.delay after the last one.
    @State private var interactions = 0
    /// The position slider is being dragged.
    @State private var scrubbing = false

    init(media: MediaModel, episode: LocalEpisode, stream: StreamPlayback? = nil) {
        self.media = media; self.episode = episode; self.stream = stream
        _delays = ObservedObject(wrappedValue: media.subtitleDelays)
        let startMs = episode.durationMs > 0 && episode.positionMs > episode.durationMs - 30_000 ? 0 : episode.positionMs
        if let stream {
            _video = StateObject(wrappedValue: VideoController(stream: stream.stream, startMs: startMs))
        } else {
            let url = media.videoURL(episode.id)
            _video = StateObject(wrappedValue: VideoController(url: url, startMs: startMs))
        }
    }

    private var cueIndex: Int? { track?.cue(atMs: video.timeMs - Int64(delayMs)) }
    /// The line on screen: the selected one while text in it is selected (SubtitleCueHold).
    private var shownCue: Int? { SubtitleCueHold.shown(current: cueIndex, held: heldCue, count: track?.cues.count ?? 0) }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
                .background(GeometryReader { proxy in Color.clear.onAppear { screenBox = proxy.size }
                    .onChange(of: proxy.size) { _, size in screenBox = size } })
            VideoSurface(player: video.player, onFirstFrame: { video.firstFrame() }).ignoresSafeArea()
                .scaleEffect(zoom.scale)
                .offset(x: zoom.offsetX, y: zoom.offsetY)
            // The video's gestures, over the picture and under the subtitles and controls (a tap on
            // a subtitle word still opens the word): UIKit recognizers, see VideoGestureLayer.
            // The subtitle line with words is drawn in this layer too (its selection handles drag
            // there, see PlayerTouchView); a plain line stays in SwiftUI below.
            VideoGestureLayer(zoomed: zoom.isZoomed, subtitle: subtitleLine, subtitleBase: subtitleBase,
                              onTap: { action in perform(action) },
                              onZoom: { gesture in zoomGesture(gesture) })
                .ignoresSafeArea()
                .accessibilityHidden(true)
            if let ripple { rippleView(ripple) }
            VStack {
                if chrome { topBar }
                Spacer()
                if let index = shownCue, let cue = track?.cues[index], cue.tokens.isEmpty { subtitle(cue, index: index) }
                // Where the line with words ends: above the Skip button and the controls.
                VStack(spacing: 8) {
                    Color.clear.frame(height: 0)
                    if let segment = skipSegment { skipButton(segment) }
                    if chrome { controls }
                }
                .background(GeometryReader { proxy in
                    Color.clear
                        .onAppear { subtitleBase = proxy.frame(in: .global) }
                        .onChange(of: proxy.frame(in: .global)) { _, frame in subtitleBase = frame }
                })
            }
            if let problem = video.unplayable { unplayableView(problem) }
            else if let problem = video.streamProblem { streamProblem(problem) } else if video.waitingForMac { waitingForMac }
            if let panel = wheelPanel { wheelPanelView(panel) }
        }
        .statusBarHidden(!chrome)
        .preferredColorScheme(.dark)
        // 5 s after the last interaction the controls hide (also paused); not while a panel, a
        // sheet, a selection or a drag of the slider is open (any of them restarts the wait).
        .task(id: ChromeIdle(interactions: interactions, shown: chrome, holding: chromeHeld)) {
            guard chrome, !chromeHeld else { return }
            do { try await Task.sleep(nanoseconds: UInt64(PlayerChromeTimer.delay * 1_000_000_000)) } catch { return }
            guard PlayerChromeTimer.hides(shown: chrome, idleSeconds: PlayerChromeTimer.delay, holding: chromeHeld) else { return }
            withAnimation(.easeInOut(duration: 0.2)) { chrome = false }
        }
        .onChange(of: textPercent) { _, _ in touched() }
        .onChange(of: delayMs) { _, _ in touched() }
        // Played to the end: closes as Close does (onDisappear saves the place, the watched
        // credit and the final place for the Mac) back to where the player was opened from.
        .onChange(of: video.ended) { _, ended in
            guard ended else { return }
            AppLog.write("video", "episode \(episode.id) played to the end: the player closes")
            JapaneseTextView.clearActiveSelection()
            lookup = nil; textLookup = nil
            dismiss()
        }
        .onChange(of: rotationLock) { _, lock in PlayerOrientationGate.apply(playerOnScreen: true, lock: lock) }
        // Small at the bottom (a card in the corner in landscape): the video stays in view.
        .bottomPanel(item: $lookup, onDismiss: resume) { item in WordSheet(media: media, lookup: item) }
        .bottomPanel(item: $textLookup, title: "Look up", onDismiss: resume) { item in TextLookupSheet(media: media, lookup: item) }
        .onAppear {
            // The text size of the earlier Larger/Smaller menu becomes the wheel's first value (once).
            let defaults = UserDefaults.standard
            if defaults.object(forKey: SubtitleTextSize.defaultsKey) == nil, let scale = defaults.object(forKey: "pudge.subtitleScale") as? Double {
                textPercent = SubtitleTextSize.migrated(scale: scale)
            }
            media.playerOpened(episode.id)
            // On screen (again): the read below may start the player.
            playerClosed = false
            PlayerOrientationGate.apply(playerOnScreen: true, lock: rotationLock)
            media.app.player.pause()
            Task { await media.loadReaderLook() }
            Task { await media.loadSubtitleLook() }
            Task {
                // The subtitles come from the device (or the stream's answer) before playing, whatever
                // the clock does; the line on screen follows the time once it moves. The cue file
                // (MBs) is decoded off the main thread.
                if let streamed = stream?.track { track = streamed } else { track = await media.subtitles(episode.id) }
                // Closed while the file was read: nothing starts playing behind it.
                guard !playerClosed else { return }
                let fileBytes = stream == nil
                    ? ((try? media.videoURL(episode.id).resourceValues(forKeys: [.fileSizeKey]))?.fileSize).map { Int64($0) }
                    : stream?.stream.video.sizeBytes
                video.begin(episodeId: episode.id, local: stream == nil, fileBytes: fileBytes, expectedMs: episode.durationMs,
                            subtitles: track?.cues.count)
                video.player.play()
                // Fresh Jiten states for the subtitle words (the downloaded ones may be old); they are
                // kept with the episode, so without the Mac words keep the colours of the last update.
                if let cues = track?.cues, media.app.macConnection != nil, !media.app.macOffline {
                    let states = await media.liveStates(cues.flatMap(\.tokens))
                    guard !states.isEmpty, var value = track else { return }
                    for index in value.cues.indices { value.cues[index].tokens = value.cues[index].tokens.updating(states) }
                    track = value
                    media.saveSubtitles(episode.id, track: value)
                }
            }
        }
        // A word graded in the word card: every occurrence in the subtitles takes its new state
        // at once (and the downloaded subtitles keep it for offline colours).
        .onChange(of: media.gradedWords) { _, graded in
            guard let value = track?.updating(graded.rows) else { return }
            track = value
            media.saveSubtitles(episode.id, track: value)
        }
        // Playing again by hand: nothing to resume after a lookup any more.
        .onChange(of: video.playing) { _, playing in if playing { resumeAfterLookup = false } }
        // Playing again (by hand, after a Look Up): the selection goes, the clock picks the line.
        .onChange(of: video.playing) { _, playing in if playing, heldCue != nil { JapaneseTextView.clearActiveSelection(); heldCue = nil } }
        .onChange(of: video.timeMs) { _, value in
            // Every 15 s while playing and right after a seek: the place (and the parts played)
            // go to the Mac, so the episode continues there where it is now.
            let now = Date().timeIntervalSince1970
            if positionSync.observe(positionMs: value, at: now, playing: video.playing) {
                positionSync.sent(at: now)
                syncPlace(value)
            }
        }
        // Paused: the place goes at once.
        .onChange(of: video.playing) { _, playing in
            if !playing, positionSync.event(.paused, at: Date().timeIntervalSince1970) { syncPlace(video.timeMs) }
        }
        // The app goes to the background (Home, another app, the screen locked): the place goes
        // now; a closed app never ran onDisappear.
        .onChange(of: phase) { _, newPhase in
            if newPhase == .background, positionSync.event(.background, at: Date().timeIntervalSince1970) {
                syncPlace(video.timeMs, background: true)
            }
        }
        .task(id: video.streamProblem) {
            // The Mac went away (quit for an update, restarting, asleep): the player stays open on
            // the last frame and the stream goes on by itself once the Mac answers again.
            guard stream != nil, video.streamProblem != nil, PlayerContinuity.reconnects(after: video.streamFailure) else { return }
            var attempt = 0
            while video.streamProblem != nil {
                attempt += 1
                do { try await Task.sleep(nanoseconds: UInt64(PlayerContinuity.reconnectDelay(attempt: attempt) * 1_000_000_000)) } catch { return }
                guard !retrying else { continue }
                media.app.retryMacNow()
                if await media.app.reachMac() { await retryStream(quiet: true) }
            }
        }
        .task {
            // Statistics: watching time while the video plays (sampled every 5 s).
            // A cancelled sleep ends the loop: no observation after the screen closed.
            while true {
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
                observeWatching()
            }
        }
        .onDisappear {
            playerClosed = true
            // The sound stops first (pause, no item, the audio session released off the main
            // thread); the place below is a small file on this device, the Mac gets it in a task.
            video.close(releaseAudio: media.app.player.book == nil)
            // Opened and closed at the same place: nothing new. Saved with a fresh time, the old
            // place of a copy opened offline took a later place watched on the Mac back.
            if video.opened.counts(position: Double(video.timeMs)) { media.saveVideoPosition(episode.id, positionMs: video.timeMs) }
            media.recordPlayed(episode.id, seconds: video.collectPlayed(), positionMs: video.timeMs)
            observeWatching(); media.app.activity.stop(kind: "anime", item: episode.id)
            // From the Mac: its copy may go now; the place and a watched mark go to the Mac, and the
            // list (count, next episode, place) is asked for again (refreshEpisodes sends them first).
            if stream != nil { media.endStream(episode.id) } else { Task { await media.refreshEpisodes() } }
            media.playerClosed(episode.id)
            // The app's own orientations again (the iPhone list follows the device or its lock).
            PlayerOrientationGate.apply(playerOnScreen: false, lock: rotationLock)
        }
    }

    /// Something keeps the controls on screen: the timing/size panel, a word or text lookup, a
    /// selection in the subtitle line, a drag of the slider, the Mac's problem message.
    private var chromeHeld: Bool {
        wheelPanel != nil || lookup != nil || textLookup != nil || heldCue != nil || scrubbing || video.streamProblem != nil || video.unplayable != nil
    }

    /// An interaction: the controls' hide timer starts again.
    private func touched() { interactions &+= 1 }

    /// Saves the place here (an unchanged opening place is not news) and sends it to the Mac.
    private func syncPlace(_ value: Int64, background: Bool = false) {
        if video.opened.counts(position: Double(value)) { media.saveVideoPosition(episode.id, positionMs: value) }
        media.syncPlayingPosition(episode.id, positionMs: value, seconds: video.collectPlayed(), background: background)
    }

    // MARK: Watching from the Mac

    private var waitingForMac: some View {
        Label("Waiting for the Mac…", systemImage: "wifi")
            .font(.subheadline).foregroundStyle(.white)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(Capsule().fill(Color.black.opacity(0.7)))
            .allowsHitTesting(false)
    }

    private func streamProblem(_ text: String) -> some View {
        VStack(spacing: 14) {
            Image(systemName: "wifi.exclamationmark").font(.largeTitle)
            Text(text).font(.subheadline).multilineTextAlignment(.center)
            if PlayerContinuity.reconnects(after: video.streamFailure) {
                Label("Goes on by itself when the Mac is back", systemImage: "arrow.clockwise")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if retrying { ProgressView() }
            HStack(spacing: 12) {
                Button("Try again") { Task { await retryStream() } }
                    .buttonStyle(.borderedProminent)
                if let item = stream?.item, media.localEpisodes[item.id] == nil {
                    Button("Download") { dismiss(); Task { await media.download(item) } }
                        .buttonStyle(.bordered)
                }
            }
            .disabled(retrying)
            Button("Close") { closePlayer() }.buttonStyle(.borderless)
        }
        .foregroundStyle(.white)
        .padding(22).frame(maxWidth: 420)
        .background(RoundedRectangle(cornerRadius: 16).fill(Color.black.opacity(0.85)))
        .padding(24)
    }

    /// Close: the player goes first, then the sound stops; nothing waits (onDisappear finishes it).
    private func closePlayer() {
        AppLog.write("player", "Close tapped (episode \(episode.id))")
        JapaneseTextView.clearActiveSelection()
        lookup = nil; textLookup = nil
        dismiss()
        video.pause()
    }

    /// A downloaded episode that does not play here (PlaybackWatchdog): stopped, with the reason.
    private func unplayableView(_ text: String) -> some View {
        VStack(spacing: 14) {
            Image(systemName: "exclamationmark.triangle").font(.largeTitle)
            Text(text).font(.subheadline).multilineTextAlignment(.center)
            HStack(spacing: 12) {
                if stream == nil, media.episodes.contains(where: { $0.id == episode.id }) {
                    Button("Re-download") {
                        let id = episode.id
                        closePlayer()
                        media.redownload(id)
                    }
                    .buttonStyle(.borderedProminent)
                }
                Button("Close") { closePlayer() }.buttonStyle(.bordered)
            }
        }
        .foregroundStyle(.white)
        .padding(22).frame(maxWidth: 420)
        .background(RoundedRectangle(cornerRadius: 16).fill(Color.black.opacity(0.85)))
        .padding(24)
        .accessibilityIdentifier("player-unplayable")
    }

    /// `quiet`: the automatic reconnect; a failure keeps the message shown (it tries again later).
    private func retryStream(quiet: Bool = false) async {
        guard let stream, !retrying else { return }
        retrying = true
        defer { retrying = false }
        do {
            guard let fresh = try await media.reopenStream(stream.item) else { return }
            video.resume(with: fresh)
        } catch {
            AppLog.write("video", "stream not resumed: \(error.localizedDescription)")
            guard !quiet else { return }
            video.streamProblem = error.localizedDescription
            video.streamFailure = VideoStreamFailure.from(error: error, cancelled: false)
        }
    }

    /// The timing wheel or the text size wheel (two different settings, never together) as a small
    /// panel under the buttons (top right): the video stays in view and plays on; a tap anywhere
    /// else closes it (iPhone and iPad, both orientations).
    private func wheelPanelView(_ panel: PlayerWheelPanel) -> some View {
        ZStack(alignment: .topTrailing) {
            Color.clear.contentShape(Rectangle()).ignoresSafeArea()
                .onTapGesture { withAnimation(.easeOut(duration: 0.15)) { wheelPanel = nil } }
                .accessibilityHidden(true)
            Group {
                switch panel {
                case .timing: SubtitleTimingSheet(delays: delays, episodeId: episode.id)
                case .size: SubtitleSizeSheet(textPercent: $textPercent)
                }
            }
            .padding(.top, 56).padding(.trailing, 12)
            .transition(.opacity.combined(with: .scale(scale: 0.95, anchor: .topTrailing)))
        }
    }

    /// A wheel's button in the top bar: opens its panel, closes it when open, switches from the other one.
    private func toggleWheelPanel(_ panel: PlayerWheelPanel) {
        touched()
        withAnimation(.easeOut(duration: 0.15)) { wheelPanel = PlayerWheelPanel.tapped(panel, open: wheelPanel) }
    }

    // MARK: Skip opening / ending

    /// The Mac's detected OP/ED of this file: from the list (watching from the Mac), else kept
    /// with the download (offline too).
    private var segments: [SkipSegment] {
        stream?.item.skipSegments ?? media.localEpisodes[episode.id]?.skipSegments
            ?? media.episodes.first { $0.id == episode.id }?.skipSegments ?? []
    }

    private var skipSegment: SkipSegment? {
        SkipSegments.button(segments, atMs: video.timeMs, durationMs: video.durationMs, controlsShown: chrome)
    }

    private func skipButton(_ segment: SkipSegment) -> some View {
        HStack {
            Spacer()
            Button { touched(); video.seek(SkipSegments.targetMs(segment)) } label: {
                Label(segment.isOpening ? "Skip opening" : "Skip ending", systemImage: "forward.end.fill")
                    .font(.subheadline.bold()).foregroundStyle(.white)
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(Capsule().fill(Color.black.opacity(0.7)))
                    .overlay(Capsule().stroke(Color.white.opacity(0.5), lineWidth: 1))
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("skip-segment")
        }
        .padding(.horizontal, 24).padding(.bottom, 8)
        .transition(.opacity)
    }

    private func resume() { if resumeAfterLookup { resumeAfterLookup = false; video.player.play() } }

    private func observeWatching() {
        media.app.activity.observe(kind: "anime", item: episode.id, active: video.playing,
                                   at: ["seconds": Double(video.timeMs) / 1000])
    }

    private func lookUp(_ cue: SubtitleCue, token index: Int) {
        guard let token = cue.tokens[safe: index] else { return }
        let surface = StudyText.segments(cue.text, tokens: cue.tokens).first { $0.token == index }?.text ?? ""
        // Paused by a selection in the line before: the video still goes on after the lookup.
        resumeAfterLookup = resumeAfterLookup || video.playing
        video.pause()
        let start = cue.start + Int64(delayMs)
        let controller = video
        lookup = WordLookup(token: token, surface: surface, sentence: cue.text.replacingOccurrences(of: "\n", with: " "),
                            playFromHere: { [weak controller] in
                                controller?.seek(start); controller?.player.play(); lookup = nil
                            })
    }

    // MARK: Gestures on the video

    private func perform(_ action: VideoTapAction) {
        touched()
        switch action {
        case .toggleControls:
            withAnimation(.easeInOut(duration: 0.2)) { chrome.toggle() }
        case .togglePlay:
            video.toggle()
        case .seek(let deltaMs, let totalMs):
            video.seek(video.timeMs + deltaMs)
            rippleSerial += 1
            let serial = rippleSerial
            withAnimation(.easeOut(duration: 0.15)) {
                ripple = SeekRipple(forward: deltaMs > 0, text: VideoTaps.label(totalMs: totalMs), serial: serial)
            }
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: UInt64((VideoTaps.seriesWindow + 0.15) * 1_000_000_000))
                if ripple?.serial == serial { withAnimation(.easeOut(duration: 0.25)) { ripple = nil } }
            }
        }
    }

    private func rippleView(_ value: SeekRipple) -> some View {
        HStack {
            if value.forward { Spacer() }
            VStack(spacing: 6) {
                Image(systemName: value.forward ? "forward.fill" : "backward.fill").font(.title2)
                Text(value.text).font(.headline.monospacedDigit())
            }
            .foregroundStyle(.white)
            .frame(width: 128, height: 128)
            .background(Circle().fill(Color.white.opacity(0.18)))
            .padding(.horizontal, 32)
            if !value.forward { Spacer() }
        }
        .allowsHitTesting(false)
        .transition(.opacity)
        .accessibilityHidden(true)
    }

    private var viewSize: ZoomSize { ZoomSize(width: Double(screenBox.width), height: Double(screenBox.height)) }

    /// The picture inside the view (fitted, as AVPlayerLayer draws it).
    private var pictureSize: ZoomSize {
        let height = pictureHeight(screenBox)
        let natural = video.player.currentItem?.presentationSize ?? .zero
        let width = natural.width > 0 && natural.height > 0 ? height * natural.width / natural.height : height * 16 / 9
        return ZoomSize(width: Double(min(screenBox.width, width)), height: Double(height))
    }

    /// A pinch or a drag on the video (VideoGestureLayer); released, it settles with an animation.
    private func zoomGesture(_ gesture: VideoZoomGesture) {
        if gesture == .pinchEnded {
            withAnimation(.easeOut(duration: 0.2)) { zoom.apply(gesture, picture: pictureSize, view: viewSize) }
        } else {
            zoom.apply(gesture, picture: pictureSize, view: viewSize)
        }
    }

    /// Height of the displayed picture (mpv scales subtitles by it), not of the screen.
    private func pictureHeight(_ box: CGSize) -> CGFloat {
        let natural = video.player.currentItem?.presentationSize ?? .zero
        let aspect = natural.width > 0 && natural.height > 0 ? natural.height / natural.width : 9.0 / 16.0
        return min(box.height, box.width * aspect)
    }

    /// The default size (mpv scales subtitle fonts to a 720-line picture; 24 points without the
    /// Mac's look) at the text size wheel's percentage; furigana is half of it.
    private var subtitleFontSize: CGFloat {
        var base = 24.0
        if let look = media.subtitleLook, screenBox.height > 0 { base = Double(pictureHeight(screenBox)) * Double(look.fontSize) / 720 }
        return CGFloat(SubtitleTextSize.fontSize(base: base, percent: textPercent))
    }

    /// Subtitle words in Jiten's colours (as in the Jiten mpv extension), with furigana as on the Mac.
    private var subtitleStyle: ReaderStyle {
        var style = ReaderStyle.make(media.readerLook, fontSize: Double(subtitleFontSize), marks: "color", furigana: furigana)
        style.subtitleLook = media.subtitleLook
        style.palette = .jiten
        style.text = .white
        style.font = "subtitle"
        style.centered = true
        style.shadow = true
        style.lineHeight = 1.25
        return style
    }

    private var topBar: some View {
        HStack(spacing: 16) {
            Button { closePlayer() } label: { Image(systemName: "xmark").font(.title3.bold()).frame(width: 44, height: 44) }
                .accessibilityLabel("Close")
            VStack(alignment: .leading) {
                Text(episode.title).font(.headline).lineLimit(1)
                Text("Episode \(episode.number)" + (stream == nil ? "" : " · from the Mac")).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if UIDevice.current.userInterfaceIdiom != .pad {
                // Keeps the landscape side shown now; again: follows the device (either landscape side).
                Button { touched(); rotationLock = PlayerOrientation.toggled(rotationLock, current: PlayerOrientationGate.currentSide) } label: {
                    Image(systemName: PlayerOrientation.isLocked(rotationLock) ? "lock.rotation" : "lock.open.rotation")
                        .font(.title3).frame(width: 44, height: 44)
                        .foregroundStyle(PlayerOrientation.isLocked(rotationLock) ? Color.accentColor : Color.white)
                }
                .accessibilityLabel(PlayerOrientation.isLocked(rotationLock) ? "Rotation locked: tap to follow the device" : "Lock rotation")
                .accessibilityIdentifier("player-rotation-lock")
            }
            // Timing and text size are two different settings: each its own button and panel.
            Button { toggleWheelPanel(.timing) } label: {
                Image(systemName: "timer").font(.title3).frame(width: 44, height: 44)
                    .foregroundStyle(wheelPanel == .timing ? Color.accentColor : Color.white)
            }
            .accessibilityLabel("Subtitle timing: \(SubtitleDelayWheel.label(tenths: SubtitleDelayWheel.tenths(fromMs: delayMs)))")
            .accessibilityIdentifier("subtitle-timing-button")
            Button { toggleWheelPanel(.size) } label: {
                Image(systemName: "textformat.size").font(.title3).frame(width: 44, height: 44)
                    .foregroundStyle(wheelPanel == .size ? Color.accentColor : Color.white)
            }
            .accessibilityLabel("Subtitle text size: \(SubtitleTextSize.label(textPercent))")
            .accessibilityIdentifier("subtitle-size-button")
            Menu {
                Section("Furigana") {
                    Button("Off") { touched(); furigana = "off" }
                    Button("As on the Mac") { touched(); furigana = "mac" }
                    Button("All words") { touched(); furigana = "all" }
                }
                if media.subtitleLook?.source == "jiten-mpv" { Text("Colours from your JitenMPV theme") }
                if track == nil { Text("No subtitles were downloaded") }
            } label: { Image(systemName: "captions.bubble").font(.title3).frame(width: 44, height: 44) }
                // Opening the menu counts as an interaction (SwiftUI does not tell when it closes).
                .simultaneousGesture(TapGesture().onEnded { touched() })
        }
        .padding(.horizontal, 12).padding(.top, 8)
        .background(LinearGradient(colors: [.black.opacity(0.7), .clear], startPoint: .top, endPoint: .bottom))
    }

    /// A line without words (not split on the Mac): plain text, a tap looks it up. A line with
    /// words is drawn by the touch layer (subtitleLine, PlayerTouchView).
    private func subtitle(_ cue: SubtitleCue, index: Int) -> some View {
        Text(cue.text).font(JapaneseType.font(size: subtitleFontSize, bold: true)).multilineTextAlignment(.center)
            .shadow(color: .black, radius: 3)
            .onTapGesture { video.pause(); resumeAfterLookup = true; textLookup = TextLookup(text: cue.text) }
            .padding(.horizontal, 14).padding(.vertical, Self.subtitlePadding)
            // JitenMPV draws outlined words without a box; the box stays only without its look.
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.black.opacity(media.subtitleLook == nil ? 0.45 : 0)))
            .padding(.horizontal, 24).padding(.bottom, chrome ? 8 : 28)
    }

    /// The line with words on screen, for the touch layer. Selecting in it (handles, two taps a
    /// word, three the whole line) pauses the video; the line stays while its text is selected
    /// (the clock may reach the next line or a gap mid-drag). Its vertical padding is the text
    /// view's handle room: the box looks as before.
    private var subtitleLine: SubtitleLine? {
        guard let index = shownCue, let cue = track?.cues[safe: index], !cue.tokens.isEmpty else { return nil }
        return SubtitleLine(paragraph: TextParagraph(text: cue.text, tokens: cue.tokens, ruby: cue.ruby), style: subtitleStyle,
                            boxed: media.subtitleLook == nil, bottomGap: chrome ? 8 : 28, handleRoom: Self.subtitlePadding,
                            onTap: { token in lookUp(cue, token: token) },
                            onSelect: { heldCue = index; if video.playing { resumeAfterLookup = true }; video.pause() },
                            onDeselect: { if heldCue == index { heldCue = nil } })
    }

    /// The subtitle box's padding above and below the line.
    private static let subtitlePadding: CGFloat = 8

    private var controls: some View {
        VStack(spacing: 8) {
            // The played part, and lighter behind it what is already here (no waiting there).
            SeekBar(timeMs: video.timeMs, durationMs: video.durationMs, buffered: video.buffered,
                    seek: { video.seek($0) },
                    onEditingChanged: { editing in scrubbing = editing; touched() })
                .accessibilityLabel("Position")
            HStack {
                Text(PudgeTheme.time(video.timeMs)).font(.caption.monospacedDigit())
                Spacer()
                Button { touched(); jumpCue(-1) } label: { Image(systemName: "backward.end").frame(width: 44, height: 44) }.accessibilityLabel("Previous line")
                Button { touched(); video.seek(video.timeMs - 10_000) } label: { Image(systemName: "gobackward.10").font(.title2).frame(width: 44, height: 44) }
                    .accessibilityLabel("Back 10 seconds")
                Button { touched(); video.toggle() } label: { Image(systemName: video.playing ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 46)) }
                    .accessibilityLabel(video.playing ? "Pause" : "Play")
                Button { touched(); video.seek(video.timeMs + 10_000) } label: { Image(systemName: "goforward.10").font(.title2).frame(width: 44, height: 44) }
                    .accessibilityLabel("Forward 10 seconds")
                Button { touched(); jumpCue(1) } label: { Image(systemName: "forward.end").frame(width: 44, height: 44) }.accessibilityLabel("Next line")
                Spacer()
                Text("−" + PudgeTheme.time(max(0, video.durationMs - video.timeMs))).font(.caption.monospacedDigit())
            }
        }
        .padding(.horizontal, 16).padding(.bottom, 10)
        .background(LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .top, endPoint: .bottom))
    }

    /// Previous/next subtitle line (the current line restarts when it began over 1.5 s ago).
    private func jumpCue(_ direction: Int) {
        guard let cues = track?.cues, !cues.isEmpty else { video.seek(video.timeMs + Int64(direction) * 5000); return }
        let now = video.timeMs - Int64(delayMs)
        let target: SubtitleCue?
        if direction < 0 {
            if let index = cueIndex, now - cues[index].start > 1500 { target = cues[index] }
            else { target = cues.last { $0.start < now - 300 && $0.start < (cueIndex.map { cues[$0].start } ?? now) } }
        } else {
            target = cues.first { $0.start > now + 100 }
        }
        if let target { video.seek(target.start + Int64(delayMs)) }
    }
}

/// What the controls' hide timer waits on (VideoPlayerScreen): a change starts the wait again.
/// The player's position bar: the track, the parts already here (lighter, `buffered`), the
/// played part and the thumb. Dragged or tapped anywhere on it, it seeks as the slider did.
struct SeekBar: View {
    let timeMs: Int64
    let durationMs: Int64
    let buffered: [ClosedRange<Double>]
    let seek: (Int64) -> Void
    let onEditingChanged: (Bool) -> Void
    @State private var dragging = false

    private var played: Double {
        durationMs > 0 ? min(1, max(0, Double(timeMs) / Double(durationMs))) : 0
    }

    var body: some View {
        GeometryReader { proxy in
            let width = max(1, proxy.size.width)
            let thumb: CGFloat = dragging ? 22 : 16
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.22)).frame(height: 4)
                ForEach(Array(buffered.enumerated()), id: \.offset) { _, part in
                    Capsule().fill(Color.white.opacity(0.5))
                        .frame(width: max(2, CGFloat(part.upperBound - part.lowerBound) * width), height: 4)
                        .offset(x: CGFloat(part.lowerBound) * width)
                }
                Capsule().fill(Color.accentColor).frame(width: CGFloat(played) * width, height: 4)
                Circle().fill(Color.white).frame(width: thumb, height: thumb)
                    .shadow(radius: 1)
                    .offset(x: min(width - thumb, max(0, CGFloat(played) * width - thumb / 2)))
            }
            .frame(width: width, height: proxy.size.height)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { value in
                    if !dragging { dragging = true; onEditingChanged(true) }
                    seek(Int64(BufferedBar.fraction(x: Double(value.location.x), width: Double(width)) * Double(max(1, durationMs))))
                }
                .onEnded { value in
                    seek(Int64(BufferedBar.fraction(x: Double(value.location.x), width: Double(width)) * Double(max(1, durationMs))))
                    dragging = false; onEditingChanged(false)
                })
            .animation(.easeOut(duration: 0.15), value: dragging)
        }
        .frame(height: 32)
        .accessibilityElement()
        .accessibilityValue(PudgeTheme.time(timeMs))
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: seek(min(durationMs, timeMs + 10_000))
            case .decrement: seek(max(0, timeMs - 10_000))
            @unknown default: break
            }
        }
    }
}

private struct ChromeIdle: Equatable {
    let interactions: Int
    let shown: Bool
    let holding: Bool
}

/// The "+20 s" shown on the side a seek series runs.
struct SeekRipple: Equatable {
    let forward: Bool
    let text: String
    let serial: Int
}

/// Subtitle timing as a wheel, like the Clock app's timer: −10…+10 s in 0.1 s steps with a 0 mark,
/// applied while it turns. A small panel at the top right of the player (the video plays on), with
/// its own button; the text size has its own panel (SubtitleSizeSheet), never shown together.
///
/// The timing wheel has its own state, set once from the episode's timing: bound to the player's
/// value, every redraw of the player (its clock ticks many times a second) set the turning wheel
/// back to the stored value, so a change snapped back to 0. The wheels are UIKit pickers
/// (CompactWheel) that are set from outside only when the value there changed (WheelSync):
/// SwiftUI's wheel picker was set to its value again on redraws while it still turned after a
/// fast flick (the picker reports the row only when it stops), so a flick went back to 0.
struct SubtitleTimingSheet: View {
    @ObservedObject var delays: SubtitleDelayStore
    let episodeId: Int
    @State private var tenths: Int

    init(delays: SubtitleDelayStore, episodeId: Int) {
        self.delays = delays; self.episodeId = episodeId
        _tenths = State(initialValue: SubtitleDelayWheel.tenths(fromMs: delays.ms(episodeId)))
    }

    var body: some View {
        VStack(spacing: 4) {
            HStack {
                Text("Timing").font(.caption.bold())
                Spacer()
                Button("Reset") { tenths = 0 }.font(.caption).disabled(tenths == 0)
            }
            // A few rows of the wheel (like the Clock app's timer), turned in place.
            CompactWheel(values: SubtitleDelayWheel.values, value: $tenths, marked: 0, name: "Subtitle timing",
                         label: { SubtitleDelayWheel.label(tenths: $0) })
                .frame(width: 130, height: 120)
                .clipped()
                .onChange(of: tenths) { _, value in delays.set(SubtitleDelayWheel.ms(fromTenths: value), for: episodeId) }
            Text("+ later, − earlier").font(.caption2).foregroundStyle(.secondary)
        }
        .frame(width: 130)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14).fill(.ultraThinMaterial))
        .environment(\.colorScheme, .dark)
        .accessibilityIdentifier("subtitle-timing-panel")
    }
}

/// Subtitle text size as its own wheel (70…200 %, all episodes, this device): a small panel at the
/// top right of the player with its own button, separate from the timing.
struct SubtitleSizeSheet: View {
    @Binding var textPercent: Int

    var body: some View {
        VStack(spacing: 4) {
            HStack {
                Text("Size").font(.caption.bold())
                Spacer()
                Button("Reset") { textPercent = SubtitleTextSize.standard }.font(.caption)
                    .disabled(textPercent == SubtitleTextSize.standard)
            }
            CompactWheel(values: SubtitleTextSize.values, value: $textPercent, marked: SubtitleTextSize.standard,
                         name: "Subtitle text size", label: { SubtitleTextSize.label($0) })
                .frame(width: 100, height: 120)
                .clipped()
            Text("All episodes").font(.caption2).foregroundStyle(.secondary)
        }
        .frame(width: 100)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 14).fill(.ultraThinMaterial))
        .environment(\.colorScheme, .dark)
        .accessibilityIdentifier("subtitle-size-panel")
    }
}

/// A few rows of a UIKit wheel (UIPickerView) for the player's small panel. The picker reports a
/// row once it stops turning; it is set from outside only when the value there changed (Reset):
/// never to the value it shows or reported, so a redraw mid-flick does not stop it (WheelSync).
struct CompactWheel: UIViewRepresentable {
    let values: [Int]
    @Binding var value: Int
    /// The row drawn bold (the timing's 0, the default size).
    var marked: Int?
    let name: String
    let label: (Int) -> String

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> UIPickerView {
        let picker = UIPickerView()
        picker.dataSource = context.coordinator
        picker.delegate = context.coordinator
        picker.accessibilityLabel = name
        picker.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        picker.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        context.coordinator.sync(picker, animated: false)
        return picker
    }

    func updateUIView(_ picker: UIPickerView, context: Context) {
        let coordinator = context.coordinator
        let changed = coordinator.wheel.values != values
        coordinator.wheel = self
        if changed { picker.reloadAllComponents(); coordinator.forget() }
        coordinator.sync(picker, animated: true)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: UIPickerView, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? 120, height: proposal.height ?? 120)
    }

    final class Coordinator: NSObject, UIPickerViewDataSource, UIPickerViewDelegate {
        var wheel: CompactWheel
        private var state = WheelSync()

        init(_ wheel: CompactWheel) { self.wheel = wheel }

        /// Turns the wheel to the value outside when that changed (not on every redraw).
        func sync(_ picker: UIPickerView, animated: Bool) {
            guard let row = state.external(row: WheelSync.row(of: wheel.value, in: wheel.values)) else { return }
            picker.selectRow(row, inComponent: 0, animated: animated)
        }

        /// Other rows: the next value outside sets the wheel again.
        func forget() { state = WheelSync() }

        func numberOfComponents(in pickerView: UIPickerView) -> Int { 1 }

        func pickerView(_ pickerView: UIPickerView, numberOfRowsInComponent component: Int) -> Int { wheel.values.count }

        func pickerView(_ pickerView: UIPickerView, rowHeightForComponent component: Int) -> CGFloat { 30 }

        func pickerView(_ pickerView: UIPickerView, viewForRow row: Int, forComponent component: Int, reusing view: UIView?) -> UIView {
            let text = (view as? UILabel) ?? UILabel()
            let value = WheelSync.value(atRow: row, in: wheel.values) ?? 0
            text.text = wheel.label(value)
            text.textAlignment = .center
            text.textColor = .white
            text.font = UIFont.monospacedDigitSystemFont(ofSize: 17, weight: value == wheel.marked ? .bold : .regular)
            text.adjustsFontSizeToFitWidth = true
            text.minimumScaleFactor = 0.7
            return text
        }

        func pickerView(_ pickerView: UIPickerView, didSelectRow row: Int, inComponent component: Int) {
            state.picked(row: row)
            guard let picked = WheelSync.value(atRow: row, in: wheel.values), picked != wheel.value else { return }
            wheel.value = picked
        }
    }
}

/// Jiten reviews the Mac requires before an episode (the strict review gate).
struct ReviewGateScreen: View {
    @ObservedObject var media: MediaModel
    let episode: LocalEpisode
    let onGranted: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var status: GateStatus?
    @State private var queue: [GateCard] = []
    @State private var revealed = false
    @State private var working = false
    @State private var message: String?
    @State private var failure: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 18) {
                if let status {
                    Text("Reviewed \(status.completed) of \(status.required)").font(.subheadline).foregroundStyle(.secondary)
                    ProgressView(value: Double(status.completed), total: Double(max(1, status.required)))
                }
                if let card = queue.first {
                    ScrollView {
                        VStack(spacing: 14) {
                            Text(card.word).font(JapaneseType.font(size: 46, bold: true)).multilineTextAlignment(.center).padding(.top, 24)
                            if revealed {
                                if !card.reading.isEmpty { Text(card.reading).font(JapaneseType.font(.title2)).foregroundStyle(.secondary) }
                                VStack(alignment: .leading, spacing: 6) {
                                    ForEach(Array(card.meanings.enumerated()), id: \.offset) { index, meaning in Text("\(index + 1). \(meaning)") }
                                }.frame(maxWidth: .infinity, alignment: .leading)
                                if !card.sentence.isEmpty { Text(card.sentence).font(.title3).foregroundStyle(.secondary).multilineTextAlignment(.center) }
                            }
                        }.frame(maxWidth: .infinity)
                    }
                    if revealed {
                        HStack(spacing: 8) {
                            ForEach(status?.grades ?? []) { grade in
                                Button(grade.label) { Task { await review(card, grade: grade.id) } }
                                    .buttonStyle(.borderedProminent).tint(StudyStyle.tone(grade.tone)).frame(maxWidth: .infinity)
                            }
                        }.disabled(working)
                    } else {
                        Button { revealed = true } label: { Text("Show answer").frame(maxWidth: .infinity).padding(.vertical, 6) }
                            .buttonStyle(.borderedProminent)
                    }
                } else if let failure {
                    Spacer()
                    Text(failure).multilineTextAlignment(.center).foregroundStyle(.orange)
                    Button("Try again") { Task { await begin() } }.buttonStyle(.bordered)
                    Spacer()
                } else {
                    Spacer(); ProgressView("Getting reviews from the Mac…"); Spacer()
                }
                if let message { Text(message).font(.caption).foregroundStyle(.orange).multilineTextAlignment(.center) }
            }
            .padding(20)
            .background(PudgeTheme.background)
            .navigationTitle("Reviews before the episode").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            .task { await begin() }
        }
        .tint(PudgeTheme.accent).preferredColorScheme(.dark)
    }

    private func begin() async {
        working = true; failure = nil
        defer { working = false }
        do { apply(try await media.gateBegin(episode.id)) }
        catch { failure = error.localizedDescription }
    }

    private func apply(_ value: GateStatus) {
        status = value
        if !value.blocking || value.granted { onGranted(); return }
        let known = Set(queue.map(\.id))
        queue += value.cards.filter { !known.contains($0.id) }
        if queue.isEmpty && failure == nil {
            failure = value.reason.isEmpty ? (value.message.isEmpty ? "The Mac has no review cards for this episode right now." : value.message) : value.reason
        }
    }

    private func review(_ card: GateCard, grade: String) async {
        working = true; revealed = false; message = nil
        queue.removeAll { $0.id == card.id }
        switch await media.gateReview(episode.id, card: card, grade: grade) {
        case .success(let value):
            if value.outcome == "unknown" {
                message = value.message.isEmpty ? "The Mac could not confirm that review; it is not repeated automatically." : value.message
            }
            working = false
            apply(value)
            if queue.isEmpty && value.blocking && !value.granted { await begin() }
        case .failure(let error):
            message = error.localizedDescription + " The review is not repeated automatically."
            working = false
            if queue.isEmpty { await begin() }
        }
    }
}


/// "Delete …?" as a small popover pointing at the button that asked (iPhone and iPad, any
/// orientation), with Delete and Cancel.
private struct DeleteConfirmation: ViewModifier {
    let title: String
    @Binding var isPresented: Bool
    let delete: () -> Void
    func body(content: Content) -> some View {
        content.popover(isPresented: $isPresented, attachmentAnchor: .rect(.bounds), arrowEdge: .top) {
            VStack(spacing: 12) {
                Text(title).font(.headline).multilineTextAlignment(.center)
                Button(role: .destructive) {
                    isPresented = false
                    delete()
                } label: {
                    Text("Delete").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).tint(.red)
                Button("Cancel") { isPresented = false }
            }
            .padding(16)
            .frame(minWidth: 240, maxWidth: 320)
            .presentationCompactAdaptation(.popover)
        }
    }
}

extension View {
    func deleteConfirmation(_ title: String, isPresented: Binding<Bool>, delete: @escaping () -> Void) -> some View {
        modifier(DeleteConfirmation(title: title, isPresented: isPresented, delete: delete))
    }
}
