import SwiftUI
import UIKit
import PudgeCore

struct SettingsScreen: View {
    @ObservedObject var model: PudgeModel
    @State private var scanner = false
    @State private var invitation = ""
    @State private var address = ""
    @State private var disconnectPrompt = false
    @State private var diagnostics: URL?
    @State private var usage = StorageUsage()
    @AppStorage("pudge.cellularDownloads") private var cellularDownloads = false
    var body: some View {
        NavigationStack {
            Form {
                Section("Mac") {
                    if let mac = model.paired {
                        Label(mac.serverName, systemImage: "desktopcomputer")
                        LabeledContent("Connection", value: model.macAvailable ? "Available" : "Offline")
                        Text(mac.baseURL).font(.caption).textSelection(.enabled)
                        Button { Task { await model.refresh() } } label: {
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
                        Text("On the Mac open Pudge → Settings → Advanced → iPhone / iPad · Audiobooks, then scan the QR code here. Both devices must be on the same local network. Several iPhones and iPads can be paired with one Mac.").font(.subheadline)
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
                Section("On \(PudgeTheme.device)") {
                    LabeledContent("Audiobooks", value: String(model.books.count))
                    let pending = (try? model.store.pendingCheckpoints().count) ?? 0
                    LabeledContent("Positions waiting to be sent", value: String(pending))
                    if let issue = model.syncIssue { Text(issue).foregroundStyle(.orange) }
                    Text("Local audio, position and bookmarks work offline. Downloaded episodes, light novels and manga open without the Mac; tapped words show the meanings and colours saved at the last update, and grades wait for the Mac. Archiving and freeing space need the Mac. Downloads resume after a restart; if you force-quit the app, iOS may pause them until the next launch.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Storage") {
                    LabeledContent("Audio", value: PudgeTheme.size(usage.audio))
                    LabeledContent("Covers", value: PudgeTheme.size(usage.covers))
                    if usage.texts > 0 { LabeledContent("Read-along texts", value: PudgeTheme.size(usage.texts)) }
                    if usage.words > 0 { LabeledContent("Saved word data", value: PudgeTheme.size(usage.words)) }
                    if model.media.videoBytes > 0 { LabeledContent("Episodes", value: PudgeTheme.size(model.media.videoBytes)) }
                    if !model.media.readingDownloads.isEmpty {
                        NavigationLink {
                            ReadingDownloadsScreen(media: model.media)
                        } label: {
                            LabeledContent("Light novels and manga", value: PudgeTheme.size(model.media.readingBytes))
                        }
                    }
                    if usage.temporary > 0 {
                        LabeledContent("Interrupted downloads", value: PudgeTheme.size(usage.temporary))
                        Button("Delete interrupted downloads") { model.deleteTemporaryFiles(); usage = model.storageUsage() }
                    }
                    LabeledContent("Pudge total", value: PudgeTheme.size(usage.total))
                    LabeledContent("Free on \(PudgeTheme.device)", value: PudgeTheme.size(usage.free))
                    ForEach(model.books.filter { model.downloadedBytes($0) > 0 }) { book in
                        NavigationLink { BookScreen(model: model, book: book) } label: {
                            LabeledContent(book.title, value: PudgeTheme.size(model.downloadedBytes(book)))
                        }
                    }
                    Text("Open a book to free its space; Pudge first verifies the copy on the Mac.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Downloads") {
                    Toggle("Download over cellular", isOn: $cellularDownloads)
                    Text(cellularDownloads ? "Large audio may use mobile data." : "Downloads wait for Wi‑Fi.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Diagnostics") {
                    Button("Prepare diagnostics report") {
                        do { diagnostics = try model.diagnosticsFile() } catch { model.message = error.localizedDescription }
                    }
                    if let diagnostics {
                        ShareLink(item: diagnostics) { Label("Share diagnostics report", systemImage: "square.and.arrow.up") }
                    }
                    Text("Contains book titles, sync and download state and the app log. No passwords or tokens.").font(.caption).foregroundStyle(.secondary)
                    let rejected = (try? model.store.rejectedActivityCount()) ?? 0
                    if rejected > 0 {
                        NavigationLink { RejectedStatisticsScreen(model: model) } label: {
                            LabeledContent("Statistics the Mac did not take", value: String(rejected))
                        }
                    }
                }
                Section("Pudge") {
                    LabeledContent("Version", value: "0.7.30 · iPhone P1")
                    Text("Audiobooks, reading, episodes and study with the Mac").foregroundStyle(.secondary)
                }
            }.scrollContentBackground(.hidden).background(PudgeTheme.background).navigationTitle("Settings")
                .onAppear { usage = model.storageUsage() }
                .refreshable { usage = model.storageUsage() }
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
