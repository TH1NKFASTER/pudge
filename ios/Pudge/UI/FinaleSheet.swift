import CoreGraphics
import Foundation
import SwiftUI
import UIKit
import PudgeCore

/// The end of an anime watched on this device, as on the Mac after mpv closes on the final
/// episode: the score wheel (the AniList score), then the next entry the Mac suggests (its direct
/// sequel: Planning, Watching, Download). Shown once the player closed (played to the end, or
/// closed after the watch counted); the Mac's answer to the watched mark says when
/// (`finale`, pudge/iphone_finale.py), and without the Mac the list's episode count does.
/// A score chosen while the Mac is away waits here and goes once it answers (the Mac queues it
/// for AniList itself when AniList is away).
@MainActor
final class AnimeFinaleModel: ObservableObject {
    unowned let media: MediaModel
    @Published var shown: AnimeFinale?
    @Published private(set) var sequel: FinaleSequel?
    @Published private(set) var sequelLoading = false
    @Published private(set) var sequelMessage: String?
    @Published private(set) var acting = false
    /// Finales of watches counted here, by episode id, not shown yet (shown once no player is open).
    private var waiting: [Int: AnimeFinale] = [:]
    /// Shown since launch (closed without an answer: not shown again for the same watch).
    private var offered = Set<String>()
    private var queue: FinaleQueue
    private var sending = false
    /// Next entries asked ahead (the final episode's player opened) and the requests still on their way.
    private var sequels = FinaleSequelCache()
    private var sequelTasks: [Int: Task<FinaleSequel?, Error>] = [:]
    private static let key = "pudge.finaleQueue"

    init(media: MediaModel) {
        self.media = media
        if let data = UserDefaults.standard.data(forKey: Self.key), let value = try? JSONDecoder().decode(FinaleQueue.self, from: data) {
            queue = value
        } else {
            queue = FinaleQueue()
        }
    }

    /// A watch counted on this device: the list's count guesses whether it ended the anime (the
    /// Mac's answer replaces the guess when it comes).
    func counted(_ item: LocalEpisode) {
        guard let mediaId = item.mediaId, mediaId > 0, waiting[item.id] == nil else { return }
        let group = media.animeGroups.first { $0.mediaId == mediaId }
        let total = group?.total ?? media.macHomeCard(mediaId)?.total
        guard let guess = AnimeFinaleRule.local(mediaId: mediaId, title: group?.title ?? item.title, number: item.number,
                                                total: total, movie: group?.movie) else { return }
        waiting[item.id] = guess
        AppLog.write("video", "episode \(item.id) ends \(mediaId) here (\(guess.kind)): the score wheel after the player")
    }

    /// The Mac counted the watch: its `finale` (none: the guess stays; an older Mac sends none).
    /// Only the end of the anime shows the wheel (an older Mac's "caught up" finale drops the guess).
    func mac(_ finale: AnimeFinale?, episode id: Int) {
        guard let finale else { return }
        guard finale.offersWheel else { waiting[id] = nil; return }
        waiting[id] = finale
        offer()
    }

    /// The player opened episode `id`: when it is the title's final one (the list's count, as
    /// `counted`), the Mac's next entry is asked now, so the sheet after it shows it at once.
    func playerOpened(_ id: Int) {
        let local = media.localEpisodes[id]
        let listed = media.episodes.first { $0.id == id }
        guard let mediaId = local?.mediaId ?? listed?.mediaId, mediaId > 0 else { return }
        let number = local?.number ?? listed?.number ?? 0
        let group = media.animeGroups.first { $0.mediaId == mediaId }
        let total = group?.total ?? media.macHomeCard(mediaId)?.total
        guard AnimeFinaleRule.local(mediaId: mediaId, title: group?.title ?? "", number: number,
                                    total: total, movie: group?.movie) != nil else { return }
        guard !sequels.fresh(mediaId, now: Date().timeIntervalSince1970), sequelTasks[mediaId] == nil else { return }
        if sequelRequest(mediaId) != nil {
            AppLog.write("video", "episode \(id) ends \(mediaId): next entry asked ahead")
        }
    }

    /// The request for `mediaId`'s next entry: the one on its way, or a new one (nil: no Mac).
    private func sequelRequest(_ mediaId: Int) -> Task<FinaleSequel?, Error>? {
        if let running = sequelTasks[mediaId] { return running }
        guard let client = media.app.macConnection, !media.app.macOffline else { return nil }
        let task = Task<FinaleSequel?, Error> {
            try await client.media("anime/\(mediaId)/next-entry", as: FinaleSequelAnswer.self).sequel
        }
        sequelTasks[mediaId] = task
        Task { @MainActor [weak self] in
            let result = await task.result
            guard let self else { return }
            self.sequelTasks[mediaId] = nil
            if case .success(let value) = result {
                self.sequels.store(mediaId, sequel: value, at: Date().timeIntervalSince1970)
            }
        }
        return task
    }

    /// The player closed: the wheel once its dismissal has finished.
    func playerClosed() {
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 800_000_000)
            self?.offer()
        }
    }

    private func offer() {
        guard shown == nil, !media.playerOnScreen else { return }
        waiting = waiting.filter { queue.offers($0.value) && !offered.contains($0.value.id) }
        guard let entry = waiting.min(by: { $0.key < $1.key }) else { return }
        let next = entry.value
        waiting[entry.key] = nil
        offered.insert(next.id)
        sequel = nil; sequelMessage = nil; sequelLoading = false
        shown = next
        AppLog.write("video", "score wheel for \(next.mediaId) (\(next.kind), episode \(next.episode))")
        Task { await loadSequel(next) }
    }

    /// The sheet closed without Use or Skip.
    func dismissed() { shown = nil }

    func score(_ value: Int) { answer(Double(value)) }
    func skip() { answer(nil) }

    private func answer(_ score: Double?) {
        guard let finale = shown else { return }
        queue.answer(finale, score: score, at: Date().timeIntervalSince1970)
        save()
        shown = nil
        AppLog.write("video", "score for \(finale.mediaId): \(score.map { String(Int($0)) } ?? "skipped")")
        Task { await sendAnswers() }
    }

    private func save() {
        if let data = try? JSONEncoder().encode(queue) { UserDefaults.standard.set(data, forKey: Self.key) }
    }

    /// The answers the Mac has not got yet (with the episode places, and right after an answer).
    func sendAnswers() async {
        guard !sending, !queue.pending.isEmpty, let client = media.app.macConnection, !media.app.macOffline else { return }
        sending = true
        defer { sending = false }
        for answer in queue.pending {
            do {
                let receipt = try await client.media("anime/\(answer.mediaId)/score", post: FinaleScoreRequest(answer), as: FinaleScoreReceipt.self)
                queue.sent(answer)
                AppLog.write("video", "score for \(answer.mediaId) → Mac" + (receipt.anilistPending == true ? " (AniList later)" : ""))
            } catch let refusal as MacRefusal where FinaleRefusal.final(refusal.status) {
                // Refused for good (an older Mac without the route, a title gone): not sent again.
                queue.sent(answer)
                AppLog.write("video", "score for \(answer.mediaId) refused: \(refusal.localizedDescription)")
            } catch {
                // The Mac is away: sent with the next places.
                break
            }
            save()
        }
        save()
    }

    private func loadSequel(_ finale: AnimeFinale) async {
        guard media.app.macConnection != nil, !media.app.macOffline else {
            sequelMessage = "The next entry shows when the Mac answers."
            return
        }
        // Asked ahead when the final episode's player opened: shown at once (or that request reused).
        if case .hit(let ready) = sequels.lookup(finale.mediaId, now: Date().timeIntervalSince1970) {
            sequel = ready
            return
        }
        guard let request = sequelRequest(finale.mediaId) else {
            sequelMessage = "The next entry shows when the Mac answers."
            return
        }
        sequelLoading = true
        defer { sequelLoading = false }
        do {
            let answer = try await request.value
            guard shown?.id == finale.id else { return }
            sequel = answer
        } catch {
            guard shown?.id == finale.id else { return }
            sequelMessage = error.localizedDescription
        }
    }

    /// Planning, Watching or Download of the next entry, as the Mac's buttons do.
    func act(_ action: String) async {
        guard let finale = shown, !acting else { return }
        guard let client = media.app.macConnection, !media.app.macOffline else {
            sequelMessage = "This needs the Mac: the \(PudgeTheme.device) cannot reach it now."
            return
        }
        acting = true
        defer { acting = false }
        if action == "download" { sequelMessage = "Searching for a release…" }
        do {
            let answer = try await client.media("anime/\(finale.mediaId)/next-entry", post: FinaleActionRequest(action), as: FinaleActionAnswer.self)
            // Its buttons changed: the next entry asked ahead no longer holds.
            if let next = answer.sequel {
                sequels.store(finale.mediaId, sequel: next, at: Date().timeIntervalSince1970)
            } else {
                sequels.forget(finale.mediaId)
            }
            guard shown?.id == finale.id else { return }
            if let next = answer.sequel { sequel = next }
            sequelMessage = answer.message(action)
            Task { await media.refreshPlanning(); await media.refreshEpisodes() }
        } catch {
            guard shown?.id == finale.id else { return }
            sequelMessage = error.localizedDescription
        }
    }
}

/// The wheel, the chosen score (Use / Spin again), Skip and the next entry.
struct AnimeFinaleSheet: View {
    @ObservedObject var model: AnimeFinaleModel
    let finale: AnimeFinale
    @State private var rotation: Double = 0
    @State private var score: Int?
    @State private var last: Int?
    @State private var spinning = false
    /// Spins of this wheel display (the first one runs longer, ScoreWheel.spinDuration).
    @State private var spins = 0
    /// The content's height: the sheet is sized to it (FinaleSheetLayout), no empty area below.
    @State private var contentHeight: CGFloat = 0
    /// iPad (regular width): a fitted, centered sheet instead of the tall form sheet.
    let regular: Bool

    var body: some View {
        NavigationStack {
            GeometryReader { viewport in
                ScrollView {
                    VStack(spacing: 16) {
                        if let current = finale.userScore, current > 0 {
                            Text("Rating: " + Self.number(current)).font(.footnote).foregroundStyle(.secondary)
                        }
                        ZStack(alignment: .top) {
                            ScoreWheelView().rotationEffect(.degrees(rotation))
                            Image(systemName: "arrowtriangle.down.fill").font(.title2).foregroundStyle(.white)
                                .shadow(radius: 2).offset(y: -14)
                        }
                        .frame(width: 240, height: 240).padding(.top, 14)
                        .contentShape(Circle())
                        // A swipe on the wheel spins it: the score is the same draw as Spin again's;
                        // its speed only lengthens the spin a little (capped).
                        .gesture(DragGesture(minimumDistance: 12).onEnded { value in
                            let distance = Double(hypot(value.translation.width, value.translation.height))
                            let speed = Double(hypot(value.velocity.width, value.velocity.height))
                            if ScoreWheel.isFlick(distance: distance, speed: speed) { spin(flickSpeed: speed) }
                        })
                        .accessibilityHidden(true)
                        Text(score.map { String($0) } ?? "…").font(.system(size: 44, weight: .bold, design: .rounded)).monospacedDigit()
                        HStack(spacing: 12) {
                            Button { if let score { model.score(score) } } label: {
                                Text(score.map { "Use \($0)" } ?? "…").frame(minWidth: 90)
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(spinning || score == nil)
                            Button("Spin again") { spin() }.buttonStyle(.bordered).disabled(spinning)
                        }
                        nextEntry
                    }
                    .padding(20)
                    .frame(maxWidth: 520)
                    .frame(maxWidth: .infinity)
                    .onGeometryChange(for: CGFloat.self) { proxy in proxy.size.height } action: { value in
                        if abs(value - contentHeight) >= 1 { contentHeight = value }
                    }
                    // Shorter than the sheet (iOS 17 iPad keeps its form sheet size): centered.
                    .frame(minHeight: viewport.size.height)
                }
            }
            .background(PudgeTheme.background)
            .japaneseNavigationTitle(finale.title.isEmpty ? "Rate" : "Rate \(finale.title)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Skip") { model.skip() } } }
        }
        .preferredColorScheme(.dark)
        .modifier(FinaleSheetSizing(height: CGFloat(FinaleSheetLayout.height(content: Double(contentHeight))), regular: regular))
        .onAppear { if score == nil, !spinning { spin() } }
    }

    @ViewBuilder private var nextEntry: some View {
        if model.sequelLoading {
            HStack(spacing: 8) { ProgressView(); Text("Checking AniList…").font(.callout).foregroundStyle(.secondary) }
        } else if let sequel = model.sequel {
            VStack(alignment: .leading, spacing: 10) {
                Text("Next anime").font(.headline)
                VStack(alignment: .leading, spacing: 2) {
                    if let link = sequel.siteUrl.flatMap({ URL(string: $0) }), ChatMarkdown.webLink(link) {
                        Link(sequel.title, destination: link).font(.body.weight(.semibold))
                    } else {
                        Text(sequel.title).font(.body.weight(.semibold))
                    }
                    Text(sequel.statusText + ((sequel.startDate ?? "").isEmpty ? "" : " · " + (sequel.startDate ?? "")))
                        .font(.caption).foregroundStyle(.secondary)
                }
                HStack(spacing: 10) {
                    if sequel.canPlanning == true { Button("Planning") { Task { await model.act("planning") } }.buttonStyle(.bordered) }
                    if sequel.canWatching == true { Button("Watching") { Task { await model.act("watching") } }.buttonStyle(.bordered) }
                    if sequel.canDownload == true { Button("Download") { Task { await model.act("download") } }.buttonStyle(.borderedProminent) }
                }
                .disabled(model.acting)
                if let message = model.sequelMessage { Text(message).font(.footnote).foregroundStyle(.secondary) }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(PudgeTheme.panel, in: RoundedRectangle(cornerRadius: 12))
        } else if let message = model.sequelMessage {
            Text(message).font(.footnote).foregroundStyle(.secondary)
        }
    }

    private func spin(flickSpeed: Double? = nil) {
        guard !spinning else { return }
        spinning = true
        score = nil
        let result = ScoreWheel.spin(previous: last) { Double.random(in: 0..<1) }
        let turns = 5 + Int.random(in: 0..<3)
        let duration = ScoreWheel.spinDuration(spinsBefore: spins, flickSpeed: flickSpeed)
        spins += 1
        withAnimation(.easeOut(duration: duration)) {
            rotation = ScoreWheel.rotation(from: rotation, turns: turns, target: result.target)
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
            score = result.score
            last = result.score
            spinning = false
        }
    }

    private static func number(_ value: Double) -> String {
        value.rounded() == value ? String(Int(value)) : String(format: "%.1f", value)
    }
}

/// The Mac's wheel: ten segments sized by ScoreWheel.weights, 1 at the top going clockwise.
struct ScoreWheelView: View {
    static let colors: [Color] = [0xa93c4a, 0xc64e42, 0xd66d37, 0xdb9235, 0xd2b63c, 0xaebe48, 0x78b85b, 0x4cac78, 0x439eb0, 0x6f8cff]
        .map { Color(red: Double(($0 >> 16) & 0xff) / 255, green: Double(($0 >> 8) & 0xff) / 255, blue: Double($0 & 0xff) / 255) }

    var body: some View {
        GeometryReader { proxy in
            let radius = min(proxy.size.width, proxy.size.height) / 2
            let center = CGPoint(x: proxy.size.width / 2, y: proxy.size.height / 2)
            ZStack {
                ForEach(ScoreWheel.segments, id: \.score) { segment in
                    Path { path in
                        path.move(to: center)
                        path.addArc(center: center, radius: radius, startAngle: .degrees(segment.start - 90),
                                    endAngle: .degrees(segment.end - 90), clockwise: false)
                        path.closeSubpath()
                    }
                    .fill(Self.colors[segment.score - 1])
                    Text("\(segment.score)").font(.caption.bold()).foregroundStyle(.white)
                        .position(Self.point(center, radius * 0.8, degrees: segment.center))
                }
                Circle().stroke(Color.white.opacity(0.25), lineWidth: 2)
            }
        }
        .aspectRatio(1, contentMode: .fit)
    }

    /// The point `distance` from `center` at `degrees` clockwise from the top.
    private static func point(_ center: CGPoint, _ distance: CGFloat, degrees: Double) -> CGPoint {
        let angle = (degrees - 90) * Double.pi / 180
        return CGPoint(x: center.x + distance * CGFloat(cos(angle)), y: center.y + distance * CGFloat(sin(angle)))
    }
}

/// The sheet at its content's height: a fitted detent on iPhone; on iPad (iOS 18) a fitted form
/// sheet (iOS 17 iPad keeps the form sheet size, the content is centered in it).
private struct FinaleSheetSizing: ViewModifier {
    let height: CGFloat
    let regular: Bool
    @ViewBuilder
    func body(content: Content) -> some View {
        if regular {
            if #available(iOS 18.0, *) {
                content.frame(idealWidth: 540, idealHeight: height).presentationSizing(.fitted)
            } else {
                content.presentationDetents([.height(height)])
            }
        } else {
            content.presentationDetents([.height(height)]).presentationDragIndicator(.hidden)
        }
    }
}

private struct AnimeFinalePresenter: ViewModifier {
    @ObservedObject var model: AnimeFinaleModel
    @Environment(\.horizontalSizeClass) private var sizeClass
    func body(content: Content) -> some View {
        content.sheet(item: Binding(get: { model.shown }, set: { value in
            // Never publish while SwiftUI updates the view (sheet dismissal).
            if value == nil { DispatchQueue.main.async { model.dismissed() } }
        })) { finale in
            AnimeFinaleSheet(model: model, finale: finale, regular: sizeClass == .regular)
        }
    }
}

extension View {
    /// The score wheel after the end of an anime watched here (AnimeFinaleModel).
    func animeFinale(_ model: AnimeFinaleModel) -> some View { modifier(AnimeFinalePresenter(model: model)) }
}
