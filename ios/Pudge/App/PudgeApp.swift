import SwiftUI
import UIKit
import PudgeCore

@MainActor
final class PudgeAppDelegate: NSObject, UIApplicationDelegate {
    static var backgroundCompletion: (() -> Void)?
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        Self.backgroundCompletion = completionHandler
    }
    /// The video player on the iPhone allows landscape only (PlayerOrientationGate): Control
    /// Center's rotation lock (a portrait lock there) no longer turns it to portrait.
    func application(_ application: UIApplication, supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        PlayerOrientationGate.mask
    }
}

@main
@MainActor
struct PudgeApp: App {
    @UIApplicationDelegateAdaptor(PudgeAppDelegate.self) private var delegate
    private let startup: Result<PudgeModel, Error>
    init() {
        let began = LaunchClock.appInit
        startup = Result { try PudgeModel() }
        LaunchClock.modelSeconds = ProcessInfo.processInfo.systemUptime - began
    }
    var body: some Scene {
        WindowGroup {
            Group {
                switch startup {
                case .success(let model): PudgeRoot(model: model)
                case .failure(let error):
                    ContentUnavailableView("Storage unavailable", systemImage: "externaldrive.badge.exclamationmark",
                        description: Text(error.localizedDescription + "\nRestart the app. Local files are kept."))
                }
            }
            // Titles, words, subtitles, chats are Japanese: kanji in Japanese forms, not Chinese
            // (PingFang), on every screen, sheet and cover (see JapaneseType).
            .japaneseTypesetting()
        }
    }
}

struct PudgeRoot: View {
    @StateObject var model: PudgeModel
    @Environment(\.scenePhase) private var phase
    @State private var showPlayer = false
    @State private var contentWidth: CGFloat = 0
    @State private var tab: AppTab = .anime
    /// Per tab: how often it was tapped again while shown (its stack goes back to its first screen).
    @State private var rootRequests: [AppTab: Int] = [:]
    /// The app was in the background since: back to active it refreshes (not at launch, where
    /// the launch task does, nor after Control Center or a notification only made it inactive).
    @State private var wasInBackground = false
    /// Another tab keeps its inner screen; the tab shown, tapped again, goes back to its first
    /// screen (TabReselection). SwiftUI calls the setter for a tap on the selected tab too.
    private var tabSelection: Binding<AppTab> {
        Binding(get: { tab }, set: { tapped in
            if TabReselection.action(current: tab, tapped: tapped) == .popToRoot { rootRequests[tapped, default: 0] += 1 }
            tab = tapped
        })
    }
    var body: some View {
        // Same order as the Mac's sidebar. The mini player sits inside each tab,
        // above the tab bar, so it never covers the tabs.
        TabView(selection: tabSelection) {
            WatchScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .environment(\.tabRootRequest, rootRequests[.anime] ?? 0)
                .tabItem { Label("Anime", systemImage: "play.rectangle") }.tag(AppTab.anime)
            NovelsScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .environment(\.tabRootRequest, rootRequests[.novels] ?? 0)
                .tabItem { Label("Light Novels", systemImage: "book") }.tag(AppTab.novels)
            MangaScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .environment(\.tabRootRequest, rootRequests[.manga] ?? 0)
                .tabItem { Label("Manga", systemImage: "book.pages") }.tag(AppTab.manga)
            LibraryScreen(model: model).miniPlayer(model.player) { showPlayer = true }
                .environment(\.tabRootRequest, rootRequests[.audiobooks] ?? 0)
                .tabItem { Label("Audiobooks", systemImage: "headphones") }.tag(AppTab.audiobooks)
            StatsScreen(media: model.media).miniPlayer(model.player) { showPlayer = true }
                .environment(\.tabRootRequest, rootRequests[.statistics] ?? 0)
                .tabItem { Label("Statistics", systemImage: "chart.bar") }.tag(AppTab.statistics)
        }
        .libraryContainer($contentWidth)
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
        // Never over a full-screen video: presenting it from here closed the player.
        .alert("Pudge", isPresented: Binding(get: { PlayerContinuity.showsAlert(message: model.message, videoOnScreen: model.videoOnScreen) }, set: { shown in
            // Never publish while SwiftUI is updating the view (alert dismissal).
            if !shown { DispatchQueue.main.async { model.message = nil } }
        })) {
            Button("OK", role: .cancel) {}
        } message: { Text(model.message ?? "") }
        .task {
            LaunchClock.firstScreen()
            model.launchCleanup()
            // The saved word data (MBs) is read off the main thread while the Mac is asked.
            Task(priority: .utility) { await model.media.preloadOfflineWords() }
            await model.refresh()
        }
        // In front: the anime list follows the Mac's downloads (cancelled when the app leaves the front).
        .task(id: phase) { if phase == .active { await model.media.liveRefreshLoop() } }
        .onChange(of: phase) { _, newPhase in
            // Watched downloads past their delay go on return too (as on the Mac).
            if newPhase == .active {
                guard wasInBackground else { return }
                wasInBackground = false
                model.cameToForeground(); model.media.deleteWatchedEpisodes(); Task { await model.refresh(); await model.media.sendVideoPositions() }
            }
            else if newPhase == .background {
                wasInBackground = true
                model.player.snapshot("background")
                // Ended when iOS ends the background time too (a task left running gets the app killed).
                let task = BackgroundTaskBox()
                task.id = UIApplication.shared.beginBackgroundTask(withName: "Pudge position") {
                    // Called on the main thread.
                    MainActor.assumeIsolated {
                        if task.id != .invalid { UIApplication.shared.endBackgroundTask(task.id); task.id = .invalid }
                    }
                }
                // Episode places too (continue on the Mac): they waited for the next foreground.
                // A sync running now took its queue before this place: one more after it.
                Task {
                    await model.syncAfterRunning(); await model.media.sendVideoPositions()
                    if task.id != .invalid { UIApplication.shared.endBackgroundTask(task.id); task.id = .invalid }
                }
            }
        }
        // A link from outside the app (Camera, Safari, Messages) pairs only after "Connect".
        .onOpenURL { url in model.offerLink(url.absoluteString) }
        .confirmationDialog("Connect to this Mac?", isPresented: Binding(get: { model.linkOffer != nil }, set: { shown in
            if !shown { DispatchQueue.main.async { model.linkOffer = nil } }
        }),
                            titleVisibility: .visible, presenting: model.linkOffer) { offer in
            Button("Connect to \(offer.serverName)") { model.linkOffer = nil; Task { await model.pair(offer.text) } }
            Button("Cancel", role: .cancel) { model.linkOffer = nil }
        } message: { offer in Text(offer.explanation(device: PudgeTheme.device)) }
    }
}

/// How long the launch took, for the "[perf]" line of the app log (diagnostics).
@MainActor
enum LaunchClock {
    /// The app's init began (uptime seconds).
    static let appInit = ProcessInfo.processInfo.systemUptime
    /// Opening the store and the downloads (PudgeModel) before the first screen.
    static var modelSeconds = 0.0
    private static var logged = false
    /// The first screen is up: logged once per launch.
    static func firstScreen() {
        guard !logged else { return }
        logged = true
        let now = ProcessInfo.processInfo.systemUptime
        AppLog.write("perf", LaunchPerf.line(sinceProcessStart: sinceProcessStart(), sinceAppInit: now - appInit, model: modelSeconds))
    }
    /// Seconds since the process started (the system's start time of this process), nil if unknown.
    private static func sinceProcessStart() -> Double? {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var name: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()]
        guard sysctl(&name, 4, &info, &size, nil, 0) == 0 else { return nil }
        let start = info.kp_proc.p_un.__p_starttime
        let started = Double(start.tv_sec) + Double(start.tv_usec) / 1_000_000
        let seconds = Date().timeIntervalSince1970 - started
        return seconds >= 0 && seconds < 600 ? seconds : nil
    }
}

/// The app's tabs (the Mac's sidebar order).
enum AppTab: Hashable {
    case anime, novels, manga, audiobooks, statistics
}

private struct TabRootRequestKey: EnvironmentKey {
    static let defaultValue = 0
}

extension EnvironmentValues {
    /// Grows each time the tab holding this view is tapped again while shown.
    var tabRootRequest: Int {
        get { self[TabRootRequestKey.self] }
        set { self[TabRootRequestKey.self] = newValue }
    }
}

/// The tab tapped again: its NavigationStack goes back to its first screen.
private struct TabRootPop: ViewModifier {
    @Environment(\.tabRootRequest) private var request
    @Binding var path: NavigationPath
    func body(content: Content) -> some View {
        content.onChange(of: request) { _, _ in
            if !path.isEmpty { path = NavigationPath() }
        }
    }
}

extension View {
    /// Re-selecting this view's tab pops `path` to the root (TabReselection).
    func popsToRootOnReselect(_ path: Binding<NavigationPath>) -> some View { modifier(TabRootPop(path: path)) }

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
