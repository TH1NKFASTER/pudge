import SwiftUI
import Combine
import UIKit
import PudgeCore

struct SettingsScreen: View {
    @ObservedObject var model: PudgeModel
    @State private var scanner = false
    @State private var invitation = ""
    @State private var address = ""
    @State private var disconnectPrompt = false
    @State private var usage = StorageUsage()
    /// Bumped to measure storage again (off the main thread, debounced: several episode-list
    /// changes in a row measure once).
    @State private var usageRequest = 0
    /// The share sheet is anchored to the button (an iPad presents it as a popover from there).
    @State private var diagnosticsAnchor = ShareAnchor()
    @AppStorage("pudge.cellularDownloads") private var cellularDownloads = false
    @AppStorage(StreamCacheSetting.defaultsKey) private var streamCacheMB = StreamCacheSetting.defaultMB
    @AppStorage(VideoQuality.downloadKey) private var downloadQuality = VideoQuality.original.rawValue
    @AppStorage(VideoQuality.streamKey) private var streamQuality = VideoQuality.original.rawValue
    @State private var streamCacheBytes: Int64 = 0
    @AppStorage(WatchedCleanupChoice.defaultsKey) private var deleteWatched = WatchedCleanupChoice.mac.rawValue

    /// "Same as Mac (24 h)" with the Mac's own setting, once its episode list said it.
    private var macCleanupTitle: String {
        guard let hours = model.media.macDeleteWatchedHours else { return "Same as Mac (not known yet: off)" }
        if hours <= 0 { return "Same as Mac (immediately)" }
        if hours >= 48, hours.truncatingRemainder(dividingBy: 24) == 0 { return "Same as Mac (\(Int(hours / 24)) d)" }
        return "Same as Mac (" + String(format: "%g", hours) + " h)"
    }

    private func shareDiagnostics() {
        do {
            let file = try model.diagnosticsFile()
            if !ShareSheet.present([file], from: diagnosticsAnchor.view) { model.message = "Could not open the share sheet" }
        } catch { model.message = error.localizedDescription }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Mac") {
                    if let mac = model.paired {
                        Label(mac.serverName, systemImage: "desktopcomputer")
                        LabeledContent("Connection", value: model.macLink.status)
                        // Not on Wi-Fi: why nothing syncs (the Mac is not asked until then).
                        if let reason = model.macLink.explanation(device: PudgeTheme.device) {
                            Text(reason).font(.caption).foregroundStyle(.orange)
                        }
                        Text(mac.baseURL).font(.caption).textSelection(.enabled)
                        Button { Task { await model.refreshNow() } } label: {
                            HStack {
                                Text(model.refreshing ? "Refreshing…" : "Refresh library and send changes")
                                Spacer()
                                if model.refreshing { ProgressView() }
                            }.contentShape(Rectangle())
                        }.disabled(model.refreshing)
                        if let at = model.lastRefresh {
                            Text("Last refreshed " + at.formatted(date: .omitted, time: .shortened)).font(.caption).foregroundStyle(.secondary)
                        }
                        TextField("https://mac-address:8766", text: $address).textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                        Button("Check new address") { Task { await model.changeAddress(address) } }.disabled(address.isEmpty)
                        // A new Mac or a reinstalled Pudge: pairing it offers to switch this device's data over.
                        Button { scanner = true } label: { Label("Connect another Mac", systemImage: "qrcode.viewfinder") }
                        Button("Disconnect this Mac", role: .destructive) { disconnectPrompt = true }
                    } else {
                        Text("On the Mac open Pudge → Settings → Advanced → iPhone / iPad and show the pairing code, then scan the QR code here. Both devices must be on the same local network. Several iPhones and iPads can be paired with one Mac.").font(.subheadline)
                        Button { scanner = true } label: { Label("Scan the Mac's QR code", systemImage: "qrcode.viewfinder") }.accessibilityIdentifier("pair-scanner")
                    }
                    TextField("Paste invitation", text: $invitation, axis: .vertical).lineLimit(2...5).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .privacySensitive()
                    Button("Connect Mac") { let text = invitation; invitation = ""; Task { await model.pair(text) } }.disabled(invitation.isEmpty || model.busy)
                    if let code = model.pairingCode {
                        // The Mac asks Allow/Deny with the same code: a code that differs is someone else.
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Allow this \(PudgeTheme.device) on the Mac").font(.headline)
                            Text(String(code.prefix(3)) + " " + String(code.dropFirst(3))).font(.system(.largeTitle, design: .monospaced)).accessibilityIdentifier("pairing-code")
                            Text("Choose Allow on the Mac only if it shows this code.").font(.caption).foregroundStyle(.secondary)
                        }
                        Button("Cancel pairing", role: .cancel) { model.cancelPairing() }
                    } else if model.busy { ProgressView("Connecting…") }
                }
                Section("Storage") {
                    // Measured from the files: every byte is in a list below or under "Other files".
                    LabeledContent("Audio", value: PudgeTheme.storageSize(usage[.audio]))
                    LabeledContent("Covers", value: PudgeTheme.storageSize(usage[.covers]))
                    if usage[.texts] > 0 { LabeledContent("Read-along texts", value: PudgeTheme.size(usage[.texts])) }
                    if usage[.words] > 0 { LabeledContent("Saved word data", value: PudgeTheme.size(usage[.words])) }
                    if usage[.episodes] > 0 || !model.media.localEpisodes.isEmpty {
                        NavigationLink {
                            DownloadedEpisodesScreen(media: model.media)
                        } label: {
                            LabeledContent("Episodes", value: PudgeTheme.storageSize(usage[.episodes]))
                        }
                    }
                    if usage[.reading] > 0 || !model.media.readingDownloads.isEmpty {
                        NavigationLink {
                            ReadingDownloadsScreen(media: model.media)
                        } label: {
                            LabeledContent("Light novels and manga", value: PudgeTheme.storageSize(usage[.reading]))
                        }
                    }
                    if usage[.database] + usage[.logs] > 0 {
                        LabeledContent("Sync data and log", value: PudgeTheme.size(usage[.database] + usage[.logs]))
                    }
                    if usage.summary.otherBytes > 0 {
                        LabeledContent("Other files (\(usage.summary.other.count))", value: PudgeTheme.size(usage.summary.otherBytes))
                        Button("Clear other files") { model.clearOtherFiles(); usageRequest += 1 }
                            .disabled(usage.summary.clearable.isEmpty)
                        Text(usage.summary.clearableBytes < usage.summary.otherBytes
                             ? "Interrupted downloads, temporary files and files whose download was removed. Some belong to downloads in progress and stay until they finish."
                             : "Interrupted downloads, temporary files and files whose download was removed.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    LabeledContent("Pudge total", value: PudgeTheme.storageSize(usage.total))
                    LabeledContent("Free on \(PudgeTheme.device)", value: PudgeTheme.size(usage.free))
                    ForEach(model.books.filter { model.downloadedBytes($0) > 0 }) { book in
                        NavigationLink { BookScreen(model: model, book: book) } label: {
                            LabeledContent(book.title, value: PudgeTheme.size(model.downloadedBytes(book)))
                        }
                    }
                    Text("Open a book to free its space; Pudge first verifies the copy on the Mac.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Downloads") {
                    // Kept: on the Mac's Wi-Fi it changes nothing, but a Mac reached by a VPN or DNS
                    // address answers over cellular too (PudgeModel.macOnLAN), and then this lets
                    // audiobook, episode and volume downloads (URLSession allowsCellularAccess) and
                    // the volume update checks use mobile data.
                    Toggle("Download over cellular", isOn: $cellularDownloads)
                    Text(cellularDownloads
                         ? "Downloads from a Mac reached over a VPN or the internet may use mobile data."
                         : "Downloads wait for Wi‑Fi. This matters only for a Mac reached over a VPN or the internet.")
                        .font(.caption).foregroundStyle(.secondary)
                    Picker("Delete watched episodes", selection: $deleteWatched) {
                        Text(macCleanupTitle).tag(WatchedCleanupChoice.mac.rawValue)
                        Text("Immediately").tag(WatchedCleanupChoice.immediately.rawValue)
                        Text("After 1 day").tag(WatchedCleanupChoice.day.rawValue)
                        Text("After 1 week").tag(WatchedCleanupChoice.week.rawValue)
                        Text("Off").tag(WatchedCleanupChoice.off.rawValue)
                    }
                    .onChange(of: deleteWatched) { _, _ in model.media.deleteWatchedEpisodes() }
                    Text("An episode watched on this \(PudgeTheme.device) is deleted from it after this time. One you are watching again, or downloaded again after watching, stays.")
                        .font(.caption).foregroundStyle(.secondary)
                    Picker("Download quality", selection: $downloadQuality) {
                        ForEach(VideoQuality.allCases, id: \.self) { Text($0.title).tag($0.rawValue) }
                    }
                    Picker("Stream quality", selection: $streamQuality) {
                        ForEach(VideoQuality.allCases, id: \.self) { Text($0.title).tag($0.rawValue) }
                    }
                    Text("Smaller copies are made by the Mac (about 270 MB for 24 min at 720p, 140 MB at 540p, instead of 1–2 GB), so more episodes fit. Holding Download on an episode offers every size. A smaller stream helps on a slow connection.")
                        .font(.caption).foregroundStyle(.secondary)
                    Picker("Watch cache", selection: $streamCacheMB) {
                        ForEach(StreamCacheSetting.choices, id: \.self) { mb in
                            Text(PudgeTheme.size(Int64(mb) * 1024 * 1024)).tag(mb)
                        }
                    }
                    .onChange(of: streamCacheMB) { _, _ in
                        MacVideoStream.updateCacheLimit()
                        streamCacheBytes = MacVideoStream.blockCache.totalBytes
                    }
                    Text("Watching from the Mac keeps the next minutes ahead of where you are on this \(PudgeTheme.device), so a short Wi‑Fi drop does not stop the video; parts already fetched are used again. The oldest parts go first. In use: \(PudgeTheme.size(streamCacheBytes)).")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("Clear watch cache") {
                        MacVideoStream.blockCache.clear()
                        streamCacheBytes = 0
                    }
                    .disabled(streamCacheBytes == 0)
                }
                Section("Diagnostics") {
                    // One tap: the report is written and the share sheet opens (anchored to the
                    // button on iPad). The former ShareLink in this sheet did nothing on iPad.
                    Button { shareDiagnostics() } label: {
                        Label("Share diagnostics report", systemImage: "square.and.arrow.up").contentShape(Rectangle())
                    }
                    .background(ShareAnchorView(anchor: diagnosticsAnchor))
                    Text("Contains book titles, sync and download state and the app log. No passwords or tokens.").font(.caption).foregroundStyle(.secondary)
                    let rejected = (try? model.store.rejectedActivityCount()) ?? 0
                    if rejected > 0 {
                        NavigationLink { RejectedStatisticsScreen(model: model) } label: {
                            LabeledContent("Statistics the Mac did not take", value: String(rejected))
                        }
                    }
                }
                Section("Pudge") {
                    LabeledContent("Version", value: PudgeModel.appVersion)
                    let pending = (try? model.store.pendingCheckpoints().count) ?? 0
                    LabeledContent("Positions waiting to be sent", value: String(pending))
                    if let issue = model.syncIssue { Text(issue).foregroundStyle(.orange) }
                }
            }.scrollContentBackground(.hidden).background(PudgeTheme.background).navigationTitle("Settings")
                .onAppear { streamCacheBytes = MacVideoStream.blockCache.totalBytes }
                // On appear and after each request (the last of several within 0.3 s).
                .task(id: usageRequest) {
                    if usageRequest > 0 {
                        try? await Task.sleep(nanoseconds: 300_000_000)
                        if Task.isCancelled { return }
                    }
                    let measured = await model.loadStorageUsage()
                    if !Task.isCancelled { usage = measured }
                }
                // An episode deleted (by hand, in the list, or after its watch) changes the numbers.
                .onReceive(model.media.$localEpisodes.dropFirst()) { _ in usageRequest += 1 }
                .refreshable { usage = await model.loadStorageUsage() }
                .sheet(isPresented: $scanner) {
                    NavigationStack {
                        PairingScanner(onScan: { text in
                            scanner = false; Task { await model.pair(text) }
                        }, onError: { error in
                            scanner = false; model.message = error
                        }).ignoresSafeArea(edges: .bottom).navigationTitle("Point at the Mac's QR code")
                            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { scanner = false } } }
                    }
                }
                .confirmationDialog("Disconnect the Mac?", isPresented: $disconnectPrompt, titleVisibility: .visible) {
                    Button("Disconnect", role: .destructive) { model.disconnect() }
                } message: { Text("Local books stay. To revoke this \(PudgeTheme.device)'s access, also remove it in the Mac's settings.") }
                .confirmationDialog("Switch to this Mac?", isPresented: Binding(get: { model.switchOffer != nil }, set: { if !$0 { model.switchOffer = nil } }),
                                    titleVisibility: .visible, presenting: model.switchOffer) { offer in
                    Button("Switch to \(offer.serverName)") { Task { await model.switchMac(offer) } }
                    Button("Cancel", role: .cancel) { model.switchOffer = nil }
                } message: { offer in
                    Text("This \(PudgeTheme.device)'s books and unsent changes belong to another Mac. If that Mac answers, its changes are sent to it first. " + offer.summary.text(device: PudgeTheme.device))
                }
        }
    }
}

/// Statistics the Mac answered but did not take (with its reason), instead of deleting them.
struct RejectedStatisticsScreen: View {
    @ObservedObject var model: PudgeModel
    @State private var items: [RejectedActivity] = []
    var body: some View {
        List {
            if items.isEmpty { ContentUnavailableView("Nothing here", systemImage: "checkmark.circle") }
            ForEach(items) { entry in
                VStack(alignment: .leading, spacing: 4) {
                    Text(Self.kind(entry.interval.kind) + " #" + String(entry.interval.item) + " · " + PudgeTheme.time(Int64(entry.interval.active * 1000))).font(.subheadline)
                    Text(Date(timeIntervalSince1970: entry.interval.start).formatted(date: .abbreviated, time: .shortened)).font(.caption).foregroundStyle(.secondary)
                    Text(entry.reason).font(.caption).foregroundStyle(.orange)
                }
            }
            Section {
                Text("“Send again” offers them to the Mac once more, for example after updating Pudge on the Mac.").font(.caption).foregroundStyle(.secondary)
            }
        }.scrollContentBackground(.hidden).background(PudgeTheme.background).navigationTitle("Statistics not taken")
            .toolbar {
                ToolbarItemGroup(placement: .primaryAction) {
                    Button("Send again") { model.retryRejectedStatistics(); load() }.disabled(items.isEmpty)
                    Button("Delete", role: .destructive) { try? model.store.clearRejectedActivity(); load() }.disabled(items.isEmpty)
                }
            }
            .onAppear { load() }
    }
    private func load() { items = (try? model.store.rejectedActivity()) ?? [] }
    private static func kind(_ value: String) -> String {
        switch value {
        case "light_novel": "Light novel"
        case "manga": "Manga"
        case "anime": "Episode"
        default: value
        }
    }
}

struct TransfersScreen: View {
    @ObservedObject var model: PudgeModel
    var body: some View {
        NavigationStack {
            List {
                if model.transfers.isEmpty { ContentUnavailableView("No downloads", systemImage: "arrow.down.circle", description: Text("Open an audiobook and tap “Download to \(PudgeTheme.device)”.")) }
                ForEach(model.transfers.sorted { $0.id < $1.id }) { record in
                    VStack(alignment: .leading, spacing: 10) {
                        Text(model.books.first(where: { $0.assetId == record.assetId })?.title ?? record.part.fileName).font(.headline)
                        Text(record.part.fileName + " · " + PudgeTheme.size(record.part.sizeBytes)).font(.caption).foregroundStyle(.secondary)
                        Label(status(record.status), systemImage: record.status == "complete" ? "checkmark.circle.fill" : "arrow.down.circle")
                            .foregroundStyle(record.status == "complete" ? PudgeTheme.good : .secondary)
                        if record.status == "running" {
                            ProgressView(value: Double(record.receivedBytes ?? 0), total: Double(record.part.sizeBytes))
                            Text(PudgeTheme.size(record.receivedBytes ?? 0) + " / " + PudgeTheme.size(record.part.sizeBytes)).font(.caption.monospacedDigit())
                        } else if record.status == "verifying" { ProgressView() }
                        if let message = record.message { Text(message).font(.caption).foregroundStyle(.secondary) }
                        if record.status == "running" { Button("Pause") { model.pauseDownload(record) }.buttonStyle(.borderless) }
                        else if record.status != "complete" { Button("Retry download") { model.retry(record) }.buttonStyle(.borderless).disabled(model.paired == nil) }
                    }.padding(.vertical, 5)
                }
            }.scrollContentBackground(.hidden).background(PudgeTheme.background).navigationTitle("Downloads")
                .refreshable { model.reloadLocal(); await model.sync() }
                .toolbar {
                    if model.transfers.contains(where: { $0.status == "running" || $0.status == "queued" }) {
                        Button("Pause all") { model.pauseAllDownloads() }
                    } else if model.transfers.contains(where: { $0.status == "paused" || $0.status == "failed" }) {
                        Button("Resume all") { model.resumeAllDownloads() }.disabled(model.paired == nil)
                    }
                }
        }
    }
    private func status(_ value: String) -> String {
        switch value {
        case "queued": "Queued"
        case "running": "Downloading"
        case "verifying": "Verifying"
        case "complete": "File verified"
        case "paused": "Paused"
        default: "Retry needed"
        }
    }
}

/// Holds the UIKit view a share sheet is anchored to (ShareAnchorView).
final class ShareAnchor {
    weak var view: UIView?
}

/// An invisible view behind a button: the share sheet's popover source on iPad.
struct ShareAnchorView: UIViewRepresentable {
    let anchor: ShareAnchor
    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.isUserInteractionEnabled = false
        view.backgroundColor = .clear
        anchor.view = view
        return view
    }
    func updateUIView(_ view: UIView, context: Context) { anchor.view = view }
}

/// UIActivityViewController from the frontmost controller (Settings is itself a sheet), with the
/// popover source an iPad needs: without one it shows nothing (or stops the app).
@MainActor
enum ShareSheet {
    static func present(_ items: [Any], from view: UIView?) -> Bool {
        guard let view, let window = view.window, var top = window.rootViewController else { return false }
        while let next = top.presentedViewController, !next.isBeingDismissed { top = next }
        let controller = UIActivityViewController(activityItems: items, applicationActivities: nil)
        if let popover = controller.popoverPresentationController {
            popover.sourceView = view
            popover.sourceRect = view.bounds
            popover.permittedArrowDirections = [.up, .down]
        }
        top.present(controller, animated: true)
        return true
    }
}

/// "Deleting in 5 h · Keep" under a watched download (Settings → Downloads → Delete watched episodes).
struct PendingDeletionLine: View {
    @ObservedObject var media: MediaModel
    let id: Int
    var body: some View {
        if let at = media.watchedDeletion(id) {
            HStack(spacing: 12) {
                Label(WatchedCleanup.label(deleteAt: at, now: Date().timeIntervalSince1970), systemImage: "trash")
                    .font(.caption).foregroundStyle(.orange)
                Spacer()
                Button("Keep") { media.keepEpisode(id) }.font(.caption.bold()).buttonStyle(.borderless)
                    .accessibilityLabel("Keep this episode on the \(PudgeTheme.device)")
            }
        }
    }
}

/// Settings → Storage → Episodes: every downloaded episode the number counts, to delete or keep.
struct DownloadedEpisodesScreen: View {
    @ObservedObject var media: MediaModel
    var body: some View {
        let items = media.localEpisodes.values.sorted { ($0.title, $0.number, $0.id) < ($1.title, $1.number, $1.id) }
        List {
            if items.isEmpty { Text("No episodes on this \(PudgeTheme.device)").foregroundStyle(.secondary) }
            ForEach(items, id: \.id) { item in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(item.title).lineLimit(1)
                        Spacer()
                        Text(PudgeTheme.size(item.sizeBytes)).font(.caption).foregroundStyle(.secondary)
                    }
                    Text("Episode \(item.number)" + (item.watchedSent != nil ? " · watched" : "")).font(.caption).foregroundStyle(.secondary)
                    PendingDeletionLine(media: media, id: item.id)
                }
                .swipeActions {
                    Button("Delete", role: .destructive) { media.deleteEpisode(item.id) }
                }
            }
        }
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        .navigationTitle("Episodes")
    }
}
