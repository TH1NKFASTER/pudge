import SwiftUI
import UniformTypeIdentifiers
import PudgeCore

struct PudgeTheme {
    /// "iPhone" or "iPad": texts name the device Pudge runs on (several can be paired with one Mac).
    @MainActor static var device: String { UIDevice.current.model }
    /// Longest comfortable line for reading text on an iPad (points).
    static let readingWidth: CGFloat = 820
    static let background = Color(red: 11/255, green: 19/255, blue: 32/255)
    static let panel = Color(red: 17/255, green: 29/255, blue: 45/255)
    static let accent = Color(red: 111/255, green: 140/255, blue: 1)
    static let good = Color(red: 87/255, green: 211/255, blue: 140/255)
    static func time(_ milliseconds: Int64) -> String {
        let seconds = max(0, milliseconds / 1000)
        if seconds >= 3600 { return String(format: "%lld:%02lld:%02lld", seconds/3600, seconds/60%60, seconds%60) }
        return String(format: "%lld:%02lld", seconds/60, seconds%60)
    }
    static func size(_ bytes: Int64) -> String { ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file) }
}

struct LibraryScreen: View {
    @ObservedObject var model: PudgeModel
    @State private var search = ""
    @State private var localOnly = false
    @State private var importing = false
    @State private var showTransfers = false
    @AppStorage("pudge.librarySort") private var sortByRecent = true
    var filtered: [AudioManifest] {
        model.books.filter { (search.isEmpty || $0.title.localizedCaseInsensitiveContains(search) || ($0.seriesTitle ?? "").localizedCaseInsensitiveContains(search))
            && (!localOnly || (try? model.store.ready($0)) == true) }
    }
    /// Volumes of one series stay together (as on the Mac); groups are ordered by
    /// the most recently listened volume, or by title.
    var groups: [BookGroup] {
        let grouped = Dictionary(grouping: filtered) { $0.seriesTitle?.isEmpty == false ? $0.seriesTitle! : $0.title }
        let items = grouped.map { key, value in
            BookGroup(title: key, books: value.sorted { ($0.volume ?? 0, $0.title) < ($1.volume ?? 0, $1.title) })
        }
        func recent(_ books: [AudioManifest]) -> Double { books.map { model.lastPlayed[$0.assetId] ?? 0 }.max() ?? 0 }
        return items.sorted { left, right in
            if sortByRecent {
                let a = recent(left.books), b = recent(right.books)
                if a != b { return a > b }
            }
            return left.title.localizedStandardCompare(right.title) == .orderedAscending
        }
    }
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label(model.paired == nil ? "Connect a Mac in Settings or import a file" : model.refreshing ? "Refreshing library…" : model.macAvailable ? "Mac available" : "Mac unavailable · local audio works",
                          systemImage: model.macAvailable ? "desktopcomputer" : "wifi.slash")
                        .font(.subheadline).foregroundStyle(.secondary)
                    Toggle("Only on \(PudgeTheme.device)", isOn: $localOnly)
                    Picker("Sort", selection: $sortByRecent) {
                        Text("Recently played").tag(true)
                        Text("Title").tag(false)
                    }.pickerStyle(.segmented)
                }
                if filtered.isEmpty {
                    ContentUnavailableView(search.isEmpty ? "No audiobooks" : "Nothing found", systemImage: "books.vertical",
                        description: Text("Download books from the Mac or add audio from the Files app."))
                }
                ForEach(groups) { group in
                    if group.books.count > 1 {
                        Section(group.title) {
                            ForEach(group.books) { book in BookRow(model: model, book: book, showVolume: true) }
                        }
                    } else if let book = group.books.first {
                        Section { BookRow(model: model, book: book, showVolume: false) }
                    }
                }
            }
            .scrollContentBackground(.hidden).background(PudgeTheme.background)
            .navigationTitle("Audiobooks").searchable(text: $search, prompt: "Book title")
            .refreshable { await model.refresh() }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showTransfers = true } label: {
                        Label("Downloads", systemImage: model.transfers.contains { ["queued", "running", "verifying"].contains($0.status) } ? "arrow.down.circle.fill" : "arrow.down.circle")
                    }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button { importing = true } label: { Label("Import audio", systemImage: "plus") }.disabled(model.busy)
                }
                ToolbarItem(placement: .primaryAction) { SettingsButton(model: model) }
            }
            .sheet(isPresented: $showTransfers) { TransfersScreen(model: model).presentationDetents([.medium, .large]) }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.audio], allowsMultipleSelection: true) { result in
                switch result { case .success(let urls): Task { await model.importAudio(urls) }; case .failure(let error): model.message = error.localizedDescription }
            }
        }
    }
}

struct BookGroup: Identifiable {
    let title: String
    let books: [AudioManifest]
    var id: String { title }
}

struct BookRow: View {
    @ObservedObject var model: PudgeModel
    let book: AudioManifest
    let showVolume: Bool
    var body: some View {
        let ready = (try? model.store.ready(book)) == true
        let active = model.transfers.filter { $0.assetId == book.assetId && ["queued", "running", "verifying"].contains($0.status) }
        NavigationLink { BookScreen(model: model, book: book) } label: {
            HStack(spacing: 14) {
                BookCover(store: model.store, book: book, width: 54)
                VStack(alignment: .leading, spacing: 5) {
                    Text(showVolume && book.volume != nil ? "Volume \(book.volume!)" : book.title).font(.headline)
                    if showVolume && book.volume != nil { Text(book.title).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                    Text(PudgeTheme.time(book.durationMs) + " · " + PudgeTheme.size(book.sizeBytes)).font(.caption).foregroundStyle(.secondary)
                    if ready {
                        Label("Available offline", systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(PudgeTheme.good)
                    } else if !active.isEmpty {
                        let received = model.transfers.filter { $0.assetId == book.assetId }
                            .reduce(Int64(0)) { $0 + ($1.status == "complete" ? $1.part.sizeBytes : ($1.receivedBytes ?? 0)) }
                        let percent = Int(Double(received) / Double(max(1, book.sizeBytes)) * 100)
                        Label("Downloading \(min(100, percent))%", systemImage: "arrow.down.circle").font(.caption).foregroundStyle(PudgeTheme.accent)
                    } else {
                        Label("Not fully downloaded", systemImage: "icloud.and.arrow.down").font(.caption).foregroundStyle(.secondary)
                    }
                    let position = (try? model.store.localPosition(assetId: book.assetId, editionId: book.editionId)) ?? 0
                    ProgressView(value: Double(position), total: Double(book.durationMs))
                    if model.books.filter({ $0.assetId == book.assetId }).count > 1 { Text("Edition " + book.editionId.prefix(8)).font(.caption2).foregroundStyle(.secondary) }
                }
            }.padding(.vertical, 5)
        }
    }
}

struct BookCover: View {
    let store: LocalStore
    let book: AudioManifest
    var width: CGFloat = 100
    var body: some View {
        Group {
            if let cover = book.cover, let local = try? store.resource(cover.resourceId),
               let image = UIImage(contentsOfFile: local.url.path) {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                ZStack { PudgeTheme.panel; Image(systemName: "headphones").font(.system(size: width * 0.38)).foregroundStyle(PudgeTheme.accent) }
            }
        }.frame(width: width, height: width * 1.35).clipShape(RoundedRectangle(cornerRadius: 10)).accessibilityHidden(true)
    }
}

struct BookScreen: View {
    @ObservedObject var model: PudgeModel
    let book: AudioManifest
    @State private var playerVisible = false
    @State private var offloadConfirmation = false
    @State private var startOverConfirmation = false
    var ready: Bool { (try? model.store.ready(book)) == true }
    var bookTransfers: [TransferRecord] { model.transfers.filter { $0.assetId == book.assetId } }
    var activeTransfers: [TransferRecord] { bookTransfers.filter { ["queued", "running", "verifying"].contains($0.status) } }
    var failedTransfer: TransferRecord? { bookTransfers.first { $0.status == "failed" || $0.status == "paused" } }
    var receivedBytes: Int64 {
        bookTransfers.reduce(Int64(0)) { $0 + ($1.status == "complete" ? $1.part.sizeBytes : ($1.receivedBytes ?? 0)) }
    }
    var pinned: Bool { book.parts.contains { (try? model.store.resource($0.resourceId))?.state.pinned == true } }
    var body: some View {
        List {
            Section {
                HStack { Spacer(); BookCover(store: model.store, book: book, width: 128); Spacer() }
                Text(book.title).font(.title2.bold())
                LabeledContent("Duration", value: PudgeTheme.time(book.durationMs))
                LabeledContent("Size", value: PudgeTheme.size(book.sizeBytes))
                LabeledContent("On \(PudgeTheme.device)", value: ready ? "Available offline" : "Not fully downloaded")
                LabeledContent("Mac", value: model.macAvailable ? "Available" : "Offline")
                LabeledContent("Position", value: model.syncStatus(book))
                if let verified = model.lastArchive[book.id] { Text("Archive verified " + Date(timeIntervalSince1970: Double(verified)/1000).formatted()).font(.caption).foregroundStyle(.secondary) }
                if let state = model.progress[book.assetId], state.finished {
                    Label("Finished", systemImage: "checkmark.seal")
                    // Finished is cleared only here (or on the Mac); Start over keeps it.
                    if model.macSupports("progress-unfinish") {
                        Button("Mark unfinished") { model.markUnfinished(book) }.accessibilityIdentifier("mark-unfinished")
                    }
                }
            }
            Section {
                if ready {
                    Button { Task { await model.continueListening(book); playerVisible = model.player.book != nil } } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Label("Continue", systemImage: "play.fill")
                            if let state = model.progress[book.assetId], let playing = state.playingOn, playing != model.deviceId {
                                Text("Playing on \(state.playingOnName ?? (playing == "mac" ? "the Mac" : "another device")) now — it will pause")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    }
                    .accessibilityIdentifier("play-book").disabled(model.busy)
                    .confirmationDialog("The other device did not confirm the pause",
                                        isPresented: Binding(get: { model.continueFallback?.id == book.id }, set: { if !$0 { model.continueFallback = nil } }),
                                        titleVisibility: .visible) {
                        Button("Continue from saved position") {
                            let saved = model.progress[book.assetId]?.selected
                            model.continueFallback = nil
                            Task { await model.play(book, position: saved?.editionId == book.editionId ? saved?.positionMs : nil); playerVisible = model.player.book != nil }
                        }
                    } message: { Text("It may still be playing.") }
                    Toggle("Keep on \(PudgeTheme.device)", isOn: Binding(get: { pinned }, set: { model.pin(book, value: $0) }))
                    Button { Task { do { try await model.archive(book) } catch { model.message = error.localizedDescription } } } label: { Label("Save archive to Mac", systemImage: "externaldrive.badge.plus") }.disabled(model.paired == nil || model.archiving.contains(book.id))
                    if model.archiving.contains(book.id) { ProgressView("Sending… Keep the app open") }
                    Button("Free up space", role: .destructive) { offloadConfirmation = true }.disabled(pinned)
                    Button("Start over") { startOverConfirmation = true }.accessibilityIdentifier("start-over")
                        .confirmationDialog("Start over from the beginning?", isPresented: $startOverConfirmation, titleVisibility: .visible) {
                            Button("Start over") { Task { await model.play(book, position: 0); model.player.save("reset"); playerVisible = model.player.book != nil } }
                        } message: { Text("Listening starts again at 0:00 on every device. Finished stays as it is.") }
                } else if let active = activeTransfers.first {
                    // Visible feedback right after the tap; details are in «Загрузки».
                    VStack(alignment: .leading, spacing: 6) {
                        Label(active.status == "verifying" ? "Verifying…" : active.status == "queued" ? "Queued…" : "Downloading…",
                              systemImage: "arrow.down.circle")
                        ProgressView(value: Double(receivedBytes), total: Double(max(1, book.sizeBytes)))
                        Text(PudgeTheme.size(receivedBytes) + " / " + PudgeTheme.size(book.sizeBytes)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                } else {
                    if let failed = failedTransfer, let text = failed.message { Text(text).font(.caption).foregroundStyle(.secondary) }
                    Button { model.download(book) } label: {
                        Label(failedTransfer == nil ? "Download to \(PudgeTheme.device)" : "Retry download", systemImage: "arrow.down.circle")
                            .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    }.disabled(model.paired == nil)
                }
            }
            if let state = model.progress[book.assetId], !state.conflicts.isEmpty {
                Section("Choose where to continue") {
                    Text("Devices listened independently. The one you choose applies to every device; all histories are kept.").font(.caption).foregroundStyle(.secondary)
                    ForEach(([state.selected].compactMap { $0 } + state.conflicts).filter { $0.editionId == book.editionId }) { checkpoint in
                        Button {
                            Task { await model.resolve(book, checkpointId: checkpoint.checkpointId) }
                        } label: {
                            VStack(alignment: .leading) {
                                Text(state.deviceLabel(checkpoint.deviceId, this: model.deviceId, thisLabel: "This \(PudgeTheme.device)"))
                                Text(PudgeTheme.time(checkpoint.positionMs) + " · " + Date(timeIntervalSince1970: Double(checkpoint.observedAt)/1000).formatted()).font(.caption)
                            }
                        }
                    }
                }
            }
            Section("Chapters") {
                if book.chapters.isEmpty { Text("No chapter map").foregroundStyle(.secondary) }
                ForEach(book.chapters) { chapter in
                    Button { Task { await model.play(book, position: chapter.startMs); playerVisible = model.player.book != nil } } label: {
                        HStack { Text(chapter.title); Spacer(); Text(PudgeTheme.time(chapter.startMs)).font(.caption.monospacedDigit()) }
                    }.disabled(!ready)
                }
            }
            Section("Bookmarks") {
                ForEach(model.bookmarks[book.id] ?? []) { mark in
                    Button { Task { await model.play(book, position: mark.positionMs); playerVisible = model.player.book != nil } } label: {
                        HStack { Text(mark.title.isEmpty ? "Bookmark" : mark.title); Spacer(); Text(PudgeTheme.time(mark.positionMs)).font(.caption.monospacedDigit()) }
                    }.disabled(!ready).swipeActions { Button("Delete", role: .destructive) { model.deleteBookmark(mark) } }
                }
            }
        }
        .navigationTitle("Audiobook").navigationBarTitleDisplayMode(.inline)
        .scrollContentBackground(.hidden).background(PudgeTheme.background)
        .sheet(isPresented: $playerVisible) { PlayerScreen(model: model, player: model.player) }
        .confirmationDialog("Delete local audio files?", isPresented: $offloadConfirmation, titleVisibility: .visible) {
            Button("Verify archive and free up space", role: .destructive) { Task { await model.offload(book) } }
        } message: { Text("Pudge verifies an exact copy on the Mac first. Position, chapters and bookmarks stay. Without the Mac's confirmation the files stay on this \(PudgeTheme.device).") }
    }
}
