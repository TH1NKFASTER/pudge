import SwiftUI
import AVFoundation
import UIKit
import PudgeCore

/// Starts an episode: the Mac's review gate first, then the player.
@MainActor
final class EpisodeLauncher: ObservableObject {
    @Published var playing: LocalEpisode?
    @Published var gating: LocalEpisode?
    @Published var checking: Int?
    var granted: LocalEpisode?

    /// Reviews before the episode, as on the Mac (skipped only when the Mac is unreachable).
    func start(_ local: LocalEpisode, media: MediaModel) {
        guard checking == nil else { return }
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
}

struct WatchScreen: View {
    @ObservedObject var media: MediaModel
    @AppStorage("pudge.animeSection") private var section = 0
    @StateObject private var launcher = EpisodeLauncher()

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
                result.append(AnimeGroup(mediaId: item.mediaId, title: item.title, episodes: [item]))
            }
        }
        // The Mac's order (ready new, ready old, …); episodes kept only on the iPhone come last.
        return result
    }

    var body: some View {
        NavigationStack {
            List {
                Picker("Section", selection: $section) {
                    Text("Ready").tag(0)
                    Text("Planning").tag(1)
                }.pickerStyle(.segmented)
                if section == 0 { ready } else { planning }
            }
            .scrollContentBackground(.hidden).background(PudgeTheme.background)
            .navigationTitle("Anime")
            .toolbar { ToolbarItem(placement: .primaryAction) { SettingsButton(model: media.app) } }
            .overlay { if media.loadingEpisodes && groups.isEmpty && section == 0 { ProgressView() } }
            .refreshable { await media.refreshEpisodes(); await media.refreshPlanning() }
            .task {
                if media.animeGroups.isEmpty { await media.refreshEpisodes() }
                if media.planning == nil { await media.refreshPlanning() }
            }
            .fullScreenCover(item: $launcher.playing) { item in VideoPlayerScreen(media: media, episode: item) }
        }
        .fullScreenCover(item: $launcher.gating, onDismiss: {
            if let next = launcher.granted { launcher.granted = nil; launcher.playing = next }
        }) { item in
            ReviewGateScreen(media: media, episode: item) { launcher.granted = item; launcher.gating = nil }
        }
    }

    @ViewBuilder private var ready: some View {
        if let error = media.episodesError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
        if groups.isEmpty && !media.loadingEpisodes {
            ContentUnavailableView("No episodes", systemImage: "play.rectangle",
                description: Text("Unwatched episodes with ready Japanese subtitles on the Mac appear here. Download one to watch offline."))
        }
        ForEach(groups) { group in
            AnimeCard(media: media, launcher: launcher, group: group)
        }
        if !media.localEpisodes.isEmpty {
            Section { LabeledContent("Episodes on \(PudgeTheme.device)", value: PudgeTheme.size(media.videoBytes)) }
        }
    }

    @ViewBuilder private var planning: some View {
        if let error = media.planningError { Label(error, systemImage: "wifi.slash").font(.subheadline).foregroundStyle(.secondary) }
        if let planning = media.planning {
            Section("Watching") {
                if planning.current.isEmpty { Text("Nothing in progress").foregroundStyle(.secondary) }
                ForEach(planning.current) { item in PlanningRow(media: media, item: item) }
            }
            if !planning.planned.isEmpty {
                Section("Planning") {
                    ForEach(planning.planned) { item in PlanningRow(media: media, item: item) }
                }
            }
        } else if media.planningError == nil {
            ProgressView()
        }
    }
}

extension EpisodeItem {
    init(id: Int, mediaId: Int, number: Int, title: String, episodeTitle: String, watched: Bool,
         position: Double, duration: Double, prepared: Bool, preparing: Bool) {
        let json: [String: Any] = ["id": id, "mediaId": mediaId, "number": number, "title": title, "episodeTitle": episodeTitle,
                                   "watched": watched, "position": position, "duration": duration, "prepared": prepared, "preparing": preparing]
        // Decoding keeps the Core type's stored properties public-read-only.
        self = try! JSONDecoder().decode(EpisodeItem.self, from: JSONSerialization.data(withJSONObject: json))
    }
}

extension MediaModel {
    /// Watched to the end on this iPhone (the Mac may not know yet).
    func finishedHere(_ item: EpisodeItem) -> Bool {
        guard let local = localEpisodes[item.id], local.durationMs > 0 else { return false }
        if local.watchedSent != nil { return true }
        return local.positionMs >= local.durationMs - 120_000
    }
    /// Nearest episode still to watch.
    func nextEpisode(_ group: AnimeGroup) -> EpisodeItem? {
        group.episodes.sorted { $0.number < $1.number }.first { !$0.watched && !finishedHere($0) }
    }
}

/// Prominent action with an always-visible icon (also inside list rows).
struct PillButton: View {
    let title: String
    let systemImage: String
    var tint = PudgeTheme.accent
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(.subheadline.bold()).foregroundStyle(.white)
                .padding(.horizontal, 14).padding(.vertical, 8)
                .background(Capsule().fill(tint))
        }.buttonStyle(.borderless)
    }
}

/// One anime: the nearest unwatched episode to play or download, "download all", and the episode list.
struct AnimeCard: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let group: AnimeGroup
    var body: some View {
        let next = media.nextEpisode(group)
        let missing = group.episodes.filter { media.localEpisodes[$0.id] == nil && !media.finishedHere($0) }
        Section {
            NavigationLink { AnimeDetailScreen(media: media, launcher: launcher, group: group) } label: {
                HStack(alignment: .top, spacing: 12) {
                    RemoteCover(media: media, path: "cover/anime/\(group.mediaId)", width: 58, symbol: "play.rectangle")
                    VStack(alignment: .leading, spacing: 6) {
                        Text(group.title).font(.headline).lineLimit(2)
                        Text(status(next)).font(.caption).foregroundStyle(.secondary)
                    }
                }.padding(.vertical, 4)
            }
            if let next { EpisodeAction(media: media, launcher: launcher, item: next) }
            if missing.count > 1 {
                Button { media.downloadAll(missing) } label: {
                    Label("Download all \(missing.count) episodes", systemImage: "square.and.arrow.down.on.square")
                }.disabled(media.app.macConnection == nil || media.app.macOffline || missing.allSatisfy { item in media.videoQueue.contains { $0.id == item.id } || media.videoJobs[item.id] != nil })
            }
        }
    }
    private func status(_ next: EpisodeItem?) -> String {
        var parts: [String] = []
        if let total = group.total, group.progress > 0 { parts.append("Watched \(group.progress) of \(total)") }
        let ready = group.episodes.filter { !media.finishedHere($0) }.count
        if ready > 0 { parts.append("\(ready) ready") }
        if let schedule = group.schedule, next == nil, let at = schedule.nextUnlockAt {
            parts.append("Up to date · next on " + Date(timeIntervalSince1970: at).formatted(date: .abbreviated, time: .shortened))
        }
        return parts.isEmpty ? "No episodes to watch" : parts.joined(separator: " · ")
    }
}

/// Play / continue / download for one episode.
struct EpisodeAction: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let item: EpisodeItem
    var body: some View {
        if let local = media.localEpisodes[item.id] {
            HStack(spacing: 10) {
                PillButton(title: (local.positionMs > 5000 ? "Continue " : "Play ") + "ep \(item.number)",
                           systemImage: "play.fill") { launcher.start(local, media: media) }
                    .disabled(launcher.checking != nil)
                if launcher.checking == item.id { ProgressView() }
            }
        } else if let job = media.videoJobs[item.id] {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Ep \(item.number): " + job.phase).font(.caption)
                    Spacer()
                    Button("Cancel") { media.cancelDownload(item.id) }.buttonStyle(.borderless).font(.caption)
                }
                ProgressView(value: job.fraction)
            }
        } else if media.videoQueue.contains(where: { $0.id == item.id }) {
            HStack {
                Label("Ep \(item.number) queued", systemImage: "clock").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Cancel") { media.cancelDownload(item.id) }.buttonStyle(.borderless).font(.caption)
            }
        } else {
            PillButton(title: "Download ep \(item.number)", systemImage: "arrow.down.circle.fill") {
                Task { await media.download(item) }
            }.disabled(media.app.macConnection == nil || media.app.macOffline)
        }
    }
}

struct AnimeDetailScreen: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let group: AnimeGroup
    /// Live version of the group (downloads and refreshes change it).
    private var current: AnimeGroup {
        media.animeGroups.first { $0.id == group.id }.map { live in
            var merged = live
            for item in group.episodes where !merged.episodes.contains(where: { $0.id == item.id }) { merged.episodes.append(item) }
            merged.episodes.sort { $0.number < $1.number }
            return merged
        } ?? group
    }
    var body: some View {
        let value = current
        let missing = value.episodes.filter { media.localEpisodes[$0.id] == nil && !media.finishedHere($0) }
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
                if value.episodes.isEmpty { Text("No episodes ready yet").foregroundStyle(.secondary) }
                ForEach(value.episodes) { item in EpisodeRow(media: media, launcher: launcher, item: item) }
            }
        }
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        .navigationTitle(value.title).navigationBarTitleDisplayMode(.inline)
        .refreshable { await media.refreshEpisodes() }
    }
}

struct EpisodeRow: View {
    @ObservedObject var media: MediaModel
    @ObservedObject var launcher: EpisodeLauncher
    let item: EpisodeItem
    @State private var deletePrompt = false
    var body: some View {
        let local = media.localEpisodes[item.id]
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Episode \(item.number)").font(.headline)
                    if !item.episodeTitle.isEmpty && item.episodeTitle != item.title {
                        Text(item.episodeTitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer()
                if item.watched || media.finishedHere(item) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(PudgeTheme.good).accessibilityLabel("Watched")
                }
                if let local {
                    Text(PudgeTheme.size(local.sizeBytes)).font(.caption).foregroundStyle(.secondary)
                    Button(role: .destructive) { deletePrompt = true } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless).accessibilityLabel("Delete from \(PudgeTheme.device)")
                }
            }
            EpisodeAction(media: media, launcher: launcher, item: item)
        }
        .padding(.vertical, 4)
        .confirmationDialog("Delete this episode from the \(PudgeTheme.device)?", isPresented: $deletePrompt, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { media.deleteEpisode(item.id) }
        }
    }
}

// MARK: Player

final class PlayerLayerView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
}

struct VideoSurface: UIViewRepresentable {
    let player: AVPlayer
    func makeUIView(context: Context) -> PlayerLayerView {
        let view = PlayerLayerView()
        view.playerLayer.player = player
        view.playerLayer.videoGravity = .resizeAspect
        view.backgroundColor = .black
        return view
    }
    func updateUIView(_ view: PlayerLayerView, context: Context) { view.playerLayer.player = player }
}

@MainActor
final class VideoController: ObservableObject {
    let player: AVPlayer
    @Published var timeMs: Int64 = 0
    @Published var durationMs: Int64 = 0
    @Published var playing = false
    /// Seconds played at normal speed since the last collection (jumps excluded).
    private(set) var played = IndexSet()
    private var lastTick: (media: Double, wall: Double)?
    private var observer: Any?
    init(url: URL, startMs: Int64) {
        player = AVPlayer(url: url)
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback)
        try? AVAudioSession.sharedInstance().setActive(true)
        if startMs > 0 { player.seek(to: CMTime(value: startMs, timescale: 1000), toleranceBefore: .zero, toleranceAfter: .zero) }
        observer = player.addPeriodicTimeObserver(forInterval: CMTime(value: 1, timescale: 10), queue: .main) { [weak self] time in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.timeMs = Int64(time.seconds * 1000)
                self.playing = self.player.timeControlStatus != .paused
                self.track(time.seconds)
                if let item = self.player.currentItem, item.duration.isNumeric { self.durationMs = Int64(item.duration.seconds * 1000) }
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

    func toggle() { if player.timeControlStatus == .paused { player.play() } else { player.pause() }; playing = player.timeControlStatus != .paused }
    func pause() { player.pause(); playing = false }
    func seek(_ ms: Int64) {
        let target = max(0, durationMs > 0 ? min(durationMs, ms) : ms)
        timeMs = target
        player.seek(to: CMTime(value: target, timescale: 1000), toleranceBefore: .zero, toleranceAfter: .zero)
    }
    func close() {
        player.pause()
        if let observer { player.removeTimeObserver(observer) }
        observer = nil
    }
}

struct VideoPlayerScreen: View {
    @ObservedObject var media: MediaModel
    let episode: LocalEpisode
    @StateObject private var video: VideoController
    @Environment(\.dismiss) private var dismiss
    @State private var track: SubtitleTrack?
    @State private var chrome = true
    @State private var lookup: WordLookup?
    @State private var textLookup: TextLookup?
    @State private var resumeAfterLookup = false
    @State private var lastSaved = Date()
    @AppStorage("pudge.subtitleDelayMs") private var delayMs = 0
    @AppStorage("pudge.subtitleSize") private var subtitleSize = 24.0
    /// Subtitle size relative to JitenMPV's (1 = same share of the picture height as in mpv).
    @AppStorage("pudge.subtitleScale") private var subtitleScale = 1.0
    @State private var screenBox: CGSize = .zero
    @AppStorage("pudge.subtitleFurigana") private var furigana = "off"

    init(media: MediaModel, episode: LocalEpisode) {
        self.media = media; self.episode = episode
        _video = StateObject(wrappedValue: VideoController(url: media.videoURL(episode.id),
                                                           startMs: episode.durationMs > 0 && episode.positionMs > episode.durationMs - 30_000 ? 0 : episode.positionMs))
    }

    private var cueIndex: Int? { track?.cue(atMs: video.timeMs - Int64(delayMs)) }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
                .background(GeometryReader { proxy in Color.clear.onAppear { screenBox = proxy.size }
                    .onChange(of: proxy.size) { _, size in screenBox = size } })
            VideoSurface(player: video.player).ignoresSafeArea()
                .contentShape(Rectangle())
                .onTapGesture { withAnimation(.easeInOut(duration: 0.2)) { chrome.toggle() } }
            VStack {
                if chrome { topBar }
                Spacer()
                if let index = cueIndex, let cue = track?.cues[index] { subtitle(cue, index: index) }
                if chrome { controls }
            }
        }
        .statusBarHidden(!chrome)
        .preferredColorScheme(.dark)
        .sheet(item: $lookup, onDismiss: resume) { item in WordSheet(media: media, lookup: item) }
        .sheet(item: $textLookup, onDismiss: resume) { item in TextLookupSheet(media: media, lookup: item) }
        .onAppear {
            media.app.player.pause()
            track = media.subtitles(episode.id)
            video.player.play()
            Task { await media.loadReaderLook() }
            Task { await media.loadSubtitleLook() }
            // Fresh Jiten states for the subtitle words (the downloaded ones may be old); they are
            // kept with the episode, so without the Mac words keep the colours of the last update.
            if let cues = track?.cues, media.app.macConnection != nil, !media.app.macOffline {
                Task {
                    let states = await media.liveStates(cues.flatMap(\.tokens))
                    guard !states.isEmpty, var value = track else { return }
                    for index in value.cues.indices { value.cues[index].tokens = value.cues[index].tokens.updating(states) }
                    track = value
                    media.saveSubtitles(episode.id, track: value)
                }
            }
        }
        .onChange(of: video.timeMs) { _, value in
            if Date().timeIntervalSince(lastSaved) > 15 {
                lastSaved = Date()
                media.saveVideoPosition(episode.id, positionMs: value)
                media.recordPlayed(episode.id, seconds: video.collectPlayed(), positionMs: value)
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
            media.saveVideoPosition(episode.id, positionMs: video.timeMs)
            media.recordPlayed(episode.id, seconds: video.collectPlayed(), positionMs: video.timeMs)
            observeWatching(); media.app.activity.stop(kind: "anime", item: episode.id)
            video.close()
            Task { await media.sendVideoPositions() }
        }
    }

    private func resume() { if resumeAfterLookup { resumeAfterLookup = false; video.player.play() } }

    private func observeWatching() {
        media.app.activity.observe(kind: "anime", item: episode.id, active: video.playing,
                                   at: ["seconds": Double(video.timeMs) / 1000])
    }

    private func lookUp(_ cue: SubtitleCue, token index: Int) {
        guard let token = cue.tokens[safe: index] else { return }
        let surface = StudyText.segments(cue.text, tokens: cue.tokens).first { $0.token == index }?.text ?? ""
        resumeAfterLookup = video.playing
        video.pause()
        let start = cue.start + Int64(delayMs)
        let controller = video
        lookup = WordLookup(token: token, surface: surface, sentence: cue.text.replacingOccurrences(of: "\n", with: " "),
                            playFromHere: { [weak controller] in
                                controller?.seek(start); controller?.player.play(); lookup = nil
                            })
    }

    /// Height of the displayed picture (mpv scales subtitles by it), not of the screen.
    private func pictureHeight(_ box: CGSize) -> CGFloat {
        let natural = video.player.currentItem?.presentationSize ?? .zero
        let aspect = natural.width > 0 && natural.height > 0 ? natural.height / natural.width : 9.0 / 16.0
        return min(box.height, box.width * aspect)
    }

    private var subtitleFontSize: CGFloat {
        guard let look = media.subtitleLook, screenBox.height > 0 else { return CGFloat(subtitleSize) }
        // mpv scales subtitle fonts to a 720-line picture.
        return max(12, pictureHeight(screenBox) * CGFloat(look.fontSize) / 720 * CGFloat(subtitleScale))
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
            Button { dismiss() } label: { Image(systemName: "xmark").font(.title3.bold()).frame(width: 44, height: 44) }
                .accessibilityLabel("Close")
            VStack(alignment: .leading) {
                Text(episode.title).font(.headline).lineLimit(1)
                Text("Episode \(episode.number)").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Menu {
                Section("Subtitle timing") {
                    Button("Later +0.5 s") { delayMs += 500 }
                    Button("Earlier −0.5 s") { delayMs -= 500 }
                    Button("Reset (\(String(format: "%+.1f", Double(delayMs) / 1000)) s)") { delayMs = 0 }
                }
                Section("Furigana") {
                    Button("Off") { furigana = "off" }
                    Button("As on the Mac") { furigana = "mac" }
                    Button("All words") { furigana = "all" }
                }
                Section("Subtitle size") {
                    Button("Larger") { subtitleSize = min(40, subtitleSize + 2); subtitleScale = min(2, subtitleScale + 0.1) }
                    Button("Smaller") { subtitleSize = max(14, subtitleSize - 2); subtitleScale = max(0.5, subtitleScale - 0.1) }
                    if media.subtitleLook?.source == "jiten-mpv" { Text("Colours from your JitenMPV theme") }
                }
                if track == nil { Text("No subtitles were downloaded") }
            } label: { Image(systemName: "captions.bubble").font(.title3).frame(width: 44, height: 44) }
        }
        .padding(.horizontal, 12).padding(.top, 8)
        .background(LinearGradient(colors: [.black.opacity(0.7), .clear], startPoint: .top, endPoint: .bottom))
    }

    private func subtitle(_ cue: SubtitleCue, index: Int) -> some View {
        Group {
            if cue.tokens.isEmpty {
                Text(cue.text).font(.system(size: subtitleFontSize, weight: .semibold)).multilineTextAlignment(.center)
                    .shadow(color: .black, radius: 3)
                    .onTapGesture { video.pause(); resumeAfterLookup = true; textLookup = TextLookup(text: cue.text) }
            } else {
                JapaneseText(paragraph: TextParagraph(text: cue.text, tokens: cue.tokens, ruby: cue.ruby), style: subtitleStyle,
                             onTap: { token in lookUp(cue, token: token) })
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        // JitenMPV draws outlined words without a box; the box stays only without its look.
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.black.opacity(media.subtitleLook == nil ? 0.45 : 0)))
        .padding(.horizontal, 24).padding(.bottom, chrome ? 8 : 28)
    }

    private var controls: some View {
        VStack(spacing: 8) {
            Slider(value: Binding(get: { Double(video.timeMs) }, set: { video.seek(Int64($0)) }),
                   in: 0...Double(max(1, video.durationMs)))
                .accessibilityLabel("Position")
            HStack {
                Text(PudgeTheme.time(video.timeMs)).font(.caption.monospacedDigit())
                Spacer()
                Button { jumpCue(-1) } label: { Image(systemName: "backward.end").frame(width: 44, height: 44) }.accessibilityLabel("Previous line")
                Button { video.seek(video.timeMs - 10_000) } label: { Image(systemName: "gobackward.10").font(.title2).frame(width: 44, height: 44) }
                    .accessibilityLabel("Back 10 seconds")
                Button { video.toggle() } label: { Image(systemName: video.playing ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 46)) }
                    .accessibilityLabel(video.playing ? "Pause" : "Play")
                Button { video.seek(video.timeMs + 10_000) } label: { Image(systemName: "goforward.10").font(.title2).frame(width: 44, height: 44) }
                    .accessibilityLabel("Forward 10 seconds")
                Button { jumpCue(1) } label: { Image(systemName: "forward.end").frame(width: 44, height: 44) }.accessibilityLabel("Next line")
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
                            Text(card.word).font(.system(size: 46, weight: .bold)).multilineTextAlignment(.center).padding(.top, 24)
                            if revealed {
                                if !card.reading.isEmpty { Text(card.reading).font(.title2).foregroundStyle(.secondary) }
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
