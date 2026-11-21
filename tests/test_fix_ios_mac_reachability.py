"""The iPhone/iPad finds its Mac again after a network change and never makes a tab wait for it.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The rules are pure
code in PudgeCore with XCTest cases (ios/PudgeCore/Tests/PudgeCoreTests/MacReachabilityTests.swift,
`swift test --package-path ios/PudgeCore` on the Mac): the order of the Mac's addresses, the
backoff between checks, the connection state and its texts. The wiring (Bonjour, NWPathMonitor,
the screens) is guarded here by reading the Swift source.

1. Maintainer's report: back on the home Wi-Fi after LTE the phone stayed "Offline". Its saved
   address was the Mac's Bonjour name (MacBook-Pro-Maksim-3391.local.), macOS had renamed the Mac
   (…-3395), and the Bonjour search, failed or stalled off Wi-Fi, was never started again. Now
   the search restarts on a network change, in the foreground and when the Mac does not answer;
   the Mac's IP addresses (resolved, and the pairing invitation's) are kept and tried with the
   same pin and server ID check before the Mac counts as offline.
2. Without Wi-Fi (only cellular) the Mac is not asked at all and the app says why.
3. Tabs show their local content at once: after no answer the Mac is asked again only after
   5 s, 15 s, then every 60 s (sooner after a network change, in the foreground, on pull to
   refresh and Refresh).
"""

from __future__ import annotations

import re

from source_policy import source

CORE = "ios/PudgeCore/Sources/PudgeCore/MacReachability.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/MacReachabilityTests.swift"
CONNECTION = "ios/Pudge/Networking/MacConnection.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
APP = "ios/Pudge/App/PudgeApp.swift"
SETTINGS = "ios/Pudge/UI/SettingsScreen.swift"
LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
STATS = "ios/Pudge/UI/StatsScreen.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_the_rules_have_xctest_cases() -> None:
    tests = source(CORE_TESTS)
    for case in (
        "testTheSavedAddressFirstThenTheKnownOnesEachOnce",
        "testAddressesJustSeenComeFirst",
        "testResolvedAddressesBecomeURLs",
        "testAfterNoAnswerTheMacIsAskedAfter5Then15ThenEvery60Seconds",
        "testAnAnswerIsReusedBrieflyAndAResetAsksAtOnce",
        "testTheConnectionStateSaysWhyNothingSyncs",
    ):
        assert f"func {case}()" in tests, case
    core = _swift(CORE)
    assert "public static let backoff: [TimeInterval] = [5, 15, 60]" in core
    assert '"No Wi-Fi — syncing with the Mac waits until this \\(device) is on the Mac\'s network"' in core


def test_bonjour_starts_over_and_reports_the_macs_numeric_addresses() -> None:
    discovery = _swift(CONNECTION, "final class MacDiscovery", "func stop()")
    # A search that did not start or stopped can be started again.
    assert "didNotSearch errorDict: [String: NSNumber]) { searching = false" in discovery
    assert (
        "func netServiceBrowserDidStopSearch(_ browser: NetServiceBrowser) { searching = false }" in discovery
    )
    restart = _swift(CONNECTION, "func restart() {", "func netServiceBrowser(")
    assert "browser = NetServiceBrowser(); searching = false start(serverId: target)" in restart
    assert "services.removeAll(); reported.removeAll()" in restart
    assert "MacAddresses.url(host: $0, port: sender.port)" in discovery
    paired = _swift(CONNECTION, "struct PairedMac: Codable {", "enum KeychainStore")
    assert "var knownAddresses: [String]? = nil" in paired


def test_the_phone_asks_again_after_network_changes_and_tries_the_known_addresses() -> None:
    init = _swift(MODEL, "init() throws {", "func reloadLocal()")
    assert "pathMonitor.pathUpdateHandler = {" in init and "pathMonitor.start(queue: .main)" in init
    assert "MainActor.assumeIsolated { self?.networkChanged(path) }" in init
    changed = _swift(MODEL, "private func networkChanged(", "func cameToForeground()")
    # Whole paths compared: Wi-Fi A -> Wi-Fi B keeps en0 and still counts as a change.
    assert "guard path != lastPath else { return }" in changed
    assert "MacLink.localInterface(type: Self.kind($0.type), name: $0.name)" in changed
    assert "startChecksOver()" in changed and "discovery.restart()" in changed
    # IOSPERF: one refresh once the updates of one change settle (iOS reports a change twice).
    assert "networkSettle?.cancel()" in changed and "await self.refresh()" in changed
    reach = _swift(MODEL, "func reachMac() async -> Bool {", "private func probeMac()")
    # No Wi-Fi (for a LAN address): not asked at all; otherwise only when the schedule says so.
    assert reach.index("guard macNetworkReachable else {") < reach.index(
        "let task = Task { await self.probeMac() }"
    )
    assert (
        "guard probeSchedule.shouldProbe(now: ProcessInfo.processInfo.systemUptime) else { return probeSchedule.lastAnswered }"
        in reach
    )
    # A check begun before the checks started over is not counted and does not clear a newer one.
    assert "guard generation == probeGeneration else { return await reachMac() }" in reach
    assert reach.index("guard generation == probeGeneration") < reach.index("probe = nil")
    assert reach.index("probe = nil") < reach.index("probeSchedule.record(answered: answered")
    assert "if generation == probeGeneration { return answered }" in reach
    # An authorised answer during the check wins over its miss.
    assert (
        "if !answered, macAvailable, probeSchedule.lastAnswered, let at = probeSchedule.lastAt, at > started { answered = true }"
        in reach
    )
    # Bonjour starts over only on the first miss or when its search is not running.
    assert "if probeSchedule.failures == 1 || !discovery.isSearching { discovery.restart() }" in reach
    over = _swift(MODEL, "private func startChecksOver() {", "private func updateNetworkNote()")
    assert "probeSchedule.reset() probeGeneration += 1 probe = nil" in over
    assert "func retryMacNow() { startChecksOver() }" in _swift(MODEL)
    probe = _swift(MODEL, "private func probeMac() async -> Bool {", "private func adopt(")
    assert "MacAddresses.candidates(saved: mac.baseURL, known: mac.knownAddresses ?? [])" in probe
    assert "group.addTask { (0, await connection.answers()) }" in probe
    # The other addresses over the same pinned session: no URLSession per address.
    assert "return (index + 1, await connection.answers(at: address))" in probe
    assert "MacConnection(candidate)" not in probe
    assert "if result.0 == 0 { group.cancelAll(); return 0 }" in probe
    # Bonjour moved the address meanwhile: reachable, not a miss.
    assert "guard moved.baseURL == mac.baseURL else { return true }" in probe
    assert "adopt(moved, client: client)" in probe
    adopt = _swift(MODEL, "private func adopt(", "private func verified(")
    assert "startChecksOver()" in adopt
    answers = _swift(CONNECTION, "func answers(at address: String) async -> Bool {", "func close()")
    assert (
        "probeSession.data(for: URLRequest(url: url))" in answers
        and "identity.serverId == mac.serverId" in answers
    )
    assert "var isSearching: Bool { searching }" in _swift(
        CONNECTION, "final class MacDiscovery", "func stop()"
    )
    # The invitation's addresses are kept at pairing; resolved ones only once they answer as this Mac.
    assert "knownAddresses: payload.candidates)" in _swift(
        MODEL, "private func reachableMac(", "func disconnect()"
    )
    found = _swift(MODEL, "discovery.onAddress = {", "discovery.start(serverId:")
    assert "rememberAddresses" not in found
    # Already reached there (its name, or its IP while available): remember checked addresses, no new refresh.
    assert (
        "if saved.baseURL == address || (self.macAvailable && numbers.contains(saved.baseURL)) {"
        " // The Mac where it is reached now" in found
    )
    assert "Task { await self.rememberVerified(numbers) } return" in found
    assert "let checked = await self.verified(numbers, by: client)" in found
    assert "MacAddresses.remember(checked + [saved.baseURL], in: self.paired?.knownAddresses ?? [])" in found
    assert "for candidate in [address] + numbers {" in found
    remember = _swift(MODEL, "private func rememberVerified(", "private func startChecksOver()")
    assert "let good = await verified(new, by: connection)" in remember
    # A request after the check uses the address that answered.
    refresh = _swift(MODEL, "private func refreshOnce() async {", "do {")
    assert refresh.index("guard await reachMac()") < refresh.index(
        "guard let connection else { reloadLocal(); return }"
    )
    sync = _swift(MODEL, "func sync() async {", "do {")
    assert sync.index("await reachMac()") < sync.index("guard let connection else { return }")
    app = _swift(APP)
    # IOSPERF: only back from the background (the launch task refreshes at launch).
    assert (
        "if newPhase == .active { guard wasInBackground else { return } wasInBackground = false model.cameToForeground();"
        in app
    )


def test_personal_hotspot_and_a_vpn_address_are_not_blocked_as_no_wifi() -> None:
    model = _swift(MODEL)
    assert (
        "private var macOnLAN: Bool { paired.map { MacAddresses.candidates(saved: $0.baseURL, known: $0.knownAddresses ?? [])"
        ".allSatisfy(MacAddresses.isLocalNetwork) } ?? true }" in model
    )
    assert "private var macNetworkReachable: Bool { onLocalNetwork || !macOnLAN }" in model
    assert "MacLink.evaluate(paired: paired != nil, localNetwork: macNetworkReachable," in model
    core = _swift(CORE)
    assert 'type == "wifi" || type == "wiredEthernet" || name.hasPrefix("bridge")' in core
    assert "func testLocalNetworkAddressesAndInterfaces()" in source(CORE_TESTS)


def test_one_inventory_report_at_a_time() -> None:
    report = _swift(MODEL, "func reportInventory() async {", "private func sendInventory()")
    assert "if inventorySending { inventoryAgain = true; return }" in report
    assert "repeat { inventoryAgain = false await sendInventory() } while inventoryAgain" in report


def test_tabs_show_local_content_first_and_pull_to_refresh_asks_at_once() -> None:
    reading = _swift(MEDIA, "func refreshReading(", "func refreshEpisodes(live: Bool = false)")
    assert reading.index("novels = savedNovelItems(); manga = savedMangaItems()") < reading.index(
        "await app.reachMac()"
    )
    assert "if forceUpdates { app.retryMacNow() }" in reading
    # Covers not on the device are not waited for while the Mac is away.
    image = _swift(MEDIA, "func image(_ path: String) async -> UIImage? {", "// MARK: Light novels")
    assert 'guard !app.macOffline, let data = try? await connection().bytes("/api/v2/media/" + path)' in image
    assert (
        ".refreshable { media.app.retryMacNow(); await media.refreshEpisodes(); await media.refreshPlanning() }"
        in _swift(WATCH)
    )
    assert ".refreshable { media.app.retryMacNow(); await media.refreshStats(period: period) }" in _swift(
        STATS
    )
    assert ".refreshable { await model.refreshNow() }" in _swift(LIBRARY)
    assert "Button { Task { await model.refreshNow() } } label: {" in _swift(SETTINGS)


def test_without_wifi_the_app_says_why_instead_of_offline() -> None:
    settings = _swift(SETTINGS, 'Section("Mac") {', 'Section("Storage")')
    assert 'LabeledContent("Connection", value: model.macLink.status)' in settings
    assert "if let reason = model.macLink.explanation(device: PudgeTheme.device) {" in settings
    media = _swift(MEDIA)
    assert (
        'static var episodesOffline: String { PudgeModel.unavailable("downloaded episodes play offline") }'
        in media
    )
    assert 'PudgeModel.unavailable("volumes downloaded to the \\(PudgeTheme.device) open offline")' in media
    assert 'statsError = PudgeModel.unavailable("statistics come from the Mac")' in media
    library = _swift(LIBRARY)
    assert 'PudgeModel.unavailable("local audio works")' in library
    assert 'LabeledContent("Mac", value: model.macLink.status)' in library
    assert '"Offline"' not in library and '"Offline"' not in _swift(SETTINGS)


def test_bonjour_keeps_an_address_entered_in_settings() -> None:
    model = re.sub(r"\s+", " ", source("ios/Pudge/App/PudgeModel.swift"))
    found = model[model.index("discovery.onAddress = {") : model.index("discovery.start(serverId:")]
    # The saved address still answers: no adoption of the advertised one.
    keep = "if let current = self.connection, await current.answers() { await self.rememberVerified(numbers) return }"
    assert keep in found and found.index(keep) < found.index("for candidate in [address] + numbers {")
    # Adopting keeps the replaced address among the known ones.
    assert "MacAddresses.remember(checked + [saved.baseURL]," in found
    change = model[model.index("func changeAddress(") : model.index("func reachMac()")]
    assert (
        "mac.knownAddresses = MacAddresses.remember([address, previous], in: mac.knownAddresses ?? [])"
        in change
    )
    assert change.index("mac.knownAddresses = MacAddresses.remember(") < change.index(
        "try KeychainStore.save("
    )
    probe = model[model.index("private func probeMac()") : model.index("private func adopt(")]
    assert "MacAddresses.remember([address, mac.baseURL], in: moved.knownAddresses ?? [])" in probe
