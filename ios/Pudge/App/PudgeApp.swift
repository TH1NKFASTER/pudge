import SwiftUI
import UIKit

@MainActor
final class PudgeAppDelegate: NSObject, UIApplicationDelegate {
    static var backgroundCompletion: (() -> Void)?
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        Self.backgroundCompletion = completionHandler
    }
}

@main
@MainActor
struct PudgeApp: App {
    @UIApplicationDelegateAdaptor(PudgeAppDelegate.self) private var delegate
    private let startup: Result<PudgeModel, Error>
    init() { startup = Result { try PudgeModel() } }
    var body: some Scene {
        WindowGroup {
            switch startup {
            case .success(let model): PudgeRoot(model: model)
            case .failure(let error):
                ContentUnavailableView("Storage unavailable", systemImage: "externaldrive.badge.exclamationmark",
                    description: Text(error.localizedDescription + "\nRestart the app. Local files are kept."))
            }
        }
    }
}

struct PudgeRoot: View {
    @StateObject var model: PudgeModel
    @Environment(\.scenePhase) private var phase
    @State private var showPlayer = false
    var body: some View {
        // Same order as the Mac's sidebar. The mini player sits inside each tab,
        // above the tab bar, so it never covers the tabs.
        TabView {
            WatchScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .tabItem { Label("Anime", systemImage: "play.rectangle") }
            NovelsScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .tabItem { Label("Light Novels", systemImage: "book") }
            MangaScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .tabItem { Label("Manga", systemImage: "book.pages") }
            LibraryScreen(model: model).miniPlayer(model.player) { showPlayer = true }
                .tabItem { Label("Audiobooks", systemImage: "headphones") }
            StatsScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .tabItem { Label("Statistics", systemImage: "chart.bar") }
        }
        .tint(PudgeTheme.accent).preferredColorScheme(.dark)
        .safeAreaInset(edge: .top, spacing: 0) {
            // Pairing from a scanned link (Camera): the code to compare with the Mac's Allow/Deny prompt.
            if let code = model.pairingCode {
                HStack {
                    Text("Allow on the Mac only if it shows " + String(code.prefix(3)) + " " + String(code.dropFirst(3))).font(.callout.monospacedDigit())
                    Spacer()
                    Button("Cancel") { model.cancelPairing() }
                }.padding(10).background(.thinMaterial)
            }
        }
        .sheet(isPresented: $showPlayer) { PlayerScreen(model: model, player: model.player) }
        .alert("Pudge", isPresented: Binding(get: { model.message != nil }, set: { shown in
            // Never publish while SwiftUI is updating the view (alert dismissal).
            if !shown { DispatchQueue.main.async { model.message = nil } }
        })) {
            Button("OK", role: .cancel) {}
        } message: { Text(model.message ?? "") }
        .task { await model.refresh() }
        .onChange(of: phase) { _, newPhase in
            if newPhase == .active { Task { await model.refresh(); await model.media.sendVideoPositions() } }
            else if newPhase == .background {
                model.player.snapshot("background")
                let task = UIApplication.shared.beginBackgroundTask(withName: "Pudge position")
                Task { await model.sync(); if task != .invalid { UIApplication.shared.endBackgroundTask(task) } }
            }
        }
        .onOpenURL { url in Task { await model.pair(url.absoluteString) } }
    }
}

extension View {
    func miniPlayer(_ player: AudioPlayerService, open: @escaping () -> Void) -> some View {
        safeAreaInset(edge: .bottom, spacing: 0) { MiniPlayer(player: player, open: open) }
    }
}

/// Settings live behind a gear on every tab (five tabs keep the Mac's order without "More").
struct SettingsButton: View {
    @ObservedObject var model: PudgeModel
    @State private var shown = false
    var body: some View {
        Button { shown = true } label: { Image(systemName: "gearshape") }
            .accessibilityLabel("Settings")
            .sheet(isPresented: $shown) { SettingsScreen(model: model) }
    }
}
