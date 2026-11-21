"""IOSPERF: the iPad app became slow to open and to use.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. Its rules are
PudgeCore code with XCTest (PerfPolicyTests); the app-side wiring is guarded by reading the
Swift source.
"""

from __future__ import annotations

from source_policy import source

MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"
APP = "ios/Pudge/App/PudgeApp.swift"


def _body(text: str, head: str) -> str:
    return text.split(head, 1)[1].split("\n    }\n", 1)[0]


def test_core_rules_have_xctests() -> None:
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/PerfPolicyTests.swift")
    for name in (
        "testOnlyARunningDownloadOnTheMacMakesTheListFrequent",
        "testNoTickWhileWatchingReadingOrRightAfterATouch",
        "testSlowWorkIsReportedStepByStep",
        "testWordsWithoutAMeaningAreNotAskedOnEveryConnect",
    ):
        assert f"func {name}()" in tests
    cards = source("ios/PudgeCore/Sources/PudgeCore/LibraryCards.swift")
    assert "(home?.waiting.contains(where: downloading) ?? false) ? busySeconds : idleSeconds" in cards
    assert "(home?.waiting.isEmpty ?? true)" not in cards


def test_a_live_tick_is_quiet_and_asks_only_whether_the_list_changed() -> None:
    media = source(MEDIA)
    loop = _body(media, "func liveRefreshLoop() async {")
    assert "AnimeLiveRefresh.shouldRefresh(" in loop and "readerOnScreen: activity.readerOnScreen" in loop
    assert "await refreshEpisodes(live: true)" in loop
    load = _body(media, "private func loadEpisodes(live: Bool) async {")
    # No spinner (a redraw of the Anime tab) for a tick; the list only when the Mac's answer changed.
    assert "if !live { loadingEpisodes = true }" in load
    assert 'sendIfChanged("/api/v2/media/anime", etag: live ? animeListTag : nil' in load
    assert "guard let list = answer.value else {" in load
    assert "if animeGroups != list.items { animeGroups = list.items }" in load
    assert "await refreshDownloadedSubtitles(words: !live)" in load
    # One list refresh at a time.
    refresh = _body(media, "func refreshEpisodes(live: Bool = false) async {")
    assert "if let running = episodesRefresh {" in refresh
    connection = source("ios/Pudge/Networking/MacConnection.swift")
    assert 'asked.setValue(etag, forHTTPHeaderField: "If-None-Match")' in connection
    assert "http.statusCode == 304, etag != nil { return (nil, etag) }" in connection


def test_saved_words_are_read_off_the_main_thread_and_not_asked_again() -> None:
    media = source(MEDIA)
    words = _body(media, "func rememberWords(_ tokens: [StudyToken]) async -> Bool {")
    assert "await preloadOfflineWords()" in words
    assert "offlineWords.markAsked(chunk)" in words
    preload = _body(media, "func preloadOfflineWords() async {")
    assert "Task.detached(priority: .utility) { OfflineWords.load(url) }" in preload
    subtitles = _body(media, "private func refreshDownloadedSubtitles(words: Bool = true) async {")
    assert "MediaModel.cueTokens(url)" in subtitles and "subtitles(id)" not in subtitles
    app = source(APP)
    assert "Task(priority: .utility) { await model.media.preloadOfflineWords() }" in app


def test_one_refresh_at_a_time_and_one_per_network_change() -> None:
    model = source(MODEL)
    refresh = _body(model, "    func refresh() async {")
    assert "if let running = refreshTask {" in refresh
    assert "guard began != connectionGeneration else { return }" in refresh
    network = _body(model, "private func networkChanged(_ path: NWPath) {")
    assert "networkSettle?.cancel()" in network
    assert "Task { if !refreshing { await refresh() } }" not in network
    # Every 15 s sync: nothing is published when nothing changed.
    reload = _body(model, "    func reloadLocal() {")
    assert "if shown != books { books = shown }" in reload
    assert "if marks != bookmarks { bookmarks = marks }" in reload
    assert "if syncIssue != nil { syncIssue = nil }" in model
    app = source(APP)
    # Back from the background only (not at launch or after Control Center).
    assert "guard wasInBackground else { return }" in app


def test_perf_lines_reach_the_diagnostics() -> None:
    app = source(APP)
    assert "LaunchClock.firstScreen()" in app
    assert 'AppLog.write("perf", LaunchPerf.line(' in app
    model = source(MODEL)
    assert 'var perf = PerfSteps("refresh"' in model
    assert 'AppLog.write("perf", line)' in model
    media = source(MEDIA)
    assert 'AppLog.write("perf", line)' in media
