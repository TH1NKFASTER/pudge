"""The iPhone app's side of the iPhone sync review (IOS-1, 3, 5, 6, 8, 9, 10, 11, 14, 15, WEBSEC-7).

policy: the iPhone app (ios/) is Swift, built and run only in Xcode on the Mac; nothing here can
compile or run it, so these guards read the Swift source (as tests/test_iphone_native_swift_guards.py
does). The Mac side of each item is tested by running it (tests/test_fix_iphone_*.py).
"""

from __future__ import annotations

import re

from source_policy import source

MODEL = "ios/Pudge/App/PudgeModel.swift"
STORE = "ios/PudgeCore/Sources/PudgeCore/LocalStore.swift"
CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/Contracts.swift"
CONNECTION = "ios/Pudge/Networking/MacConnection.swift"
TRANSFERS = "ios/Pudge/Features/Transfers/TransferManager.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    """Swift text with whitespace collapsed; from `start` up to the next `end` when given."""
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_ios1_a_new_device_id_takes_over_the_queue_and_a_refused_item_is_set_aside() -> None:
    init = _swift(MODEL, "init() throws {", "func reloadLocal()")
    assert "freshDevice = true" in init
    assert "if freshDevice, let moved = try? store.adoptDeviceId(deviceId), moved > 0 {" in init
    adopt = _swift(STORE, "public func adoptDeviceId(", "public func park(")
    assert "SELECT id,body FROM outbox WHERE kind='checkpoint'" in adopt
    assert "checkpoint.deviceId = deviceId" in adopt and "FROM carried_positions" in adopt
    # Carried places (switching Macs) get this device's current ID.
    assert "deviceId: deviceId ?? old.deviceId" in _swift(
        STORE, "public func adoptCarried(", "public func dropCarried("
    )
    assert "try store.adoptCarried(book, deviceId: deviceId)" in _swift(
        MODEL, "private func moveCarriedPlaces(", "private func prefetchCovers("
    )
    # HTTP 409/422 is a refusal the phone can tell apart; the queue goes on past it.
    send = _swift(CONNECTION, "func send<T: Decodable>(", "/// Raw bytes of a small resource")
    assert "if code == 409 || code == 422 { throw MacRefusal(status: code, text: text) }" in send
    sync = _swift(MODEL, "func sync() async {", "/// Why the Mac did not take a position")
    checkpoints = sync[
        sync.index("for checkpoint in try store.pendingCheckpoints()") : sync.index("for bookmark in")
    ]
    assert "} catch let refusal as MacRefusal {" in checkpoints
    assert "try store.park(checkpoint.checkpointId, reason: refusal.localizedDescription)" in checkpoints
    assert "continue" in checkpoints
    assert '"Changes the Mac refused (set aside): "' in _swift(MODEL, "func diagnosticsFile()", "return url")


def test_ios3_books_the_mac_replaced_or_removed_are_marked_and_rejections_are_shown() -> None:
    refresh = _swift(MODEL, "func refresh() async {", "private func moveCarriedPlaces(")
    # Round 5: also not while a book of the catalog was left out as unreadable.
    assert (
        "if catalog.preparing != true, (catalog.errors ?? []).isEmpty, catalog.skipped.isEmpty {" in refresh
    )
    assert "try store.retireMissing(serverId: catalog.serverId, listed: catalog.items" in refresh
    retire = _swift(STORE, "public func retireMissing(", "public func retiredReasons(")
    assert "guard try isRemote(book, serverId: serverId), try !isPhoneBook(book) else { continue }" in retire
    assert 'assets.contains(book.assetId) ? "edition" : "removed"' in retire
    reload = _swift(MODEL, "func reloadLocal() {", "func reloadTransfers()")
    # IOSPERF: published only when changed.
    assert "let shown = allBooks.filter { retired[$0.id] == nil || (try? store.ready($0)) == true }" in reload
    assert "if shown != books { books = shown }" in reload
    status = _swift(MODEL, "func syncStatus(", "private func configure(")
    assert '"Older edition: the Mac has a newer one"' in status
    sync = _swift(MODEL, "func sync() async {", "/// Why the Mac did not take a position")
    assert 'if receipt.status == "rejected" { refused.append(' in sync
    assert 'message = "Not taken by the Mac: "' in sync
    assert "var reason: String? = nil" in _swift(MODEL, "struct CheckpointReceipt", "struct BookmarkReceipt")


def test_ios5_one_state_request_for_the_books_that_need_it() -> None:
    sync = _swift(MODEL, "func sync() async {", "/// Why the Mac did not take a position")
    assert "try await syncState(connection)" in sync
    assert '"/api/v2/assets/\\(book.assetId)/progress"' not in sync
    state = _swift(MODEL, "private func syncState(", "/// The book's page opened")
    assert 'if macSupports("state-batch") {' in state
    assert (
        'connection.post("/api/v2/sync/state", value: StateRequest(assets: batch, since: since), as: StateAnswer.self)'
        in state
    )
    assert "let all = only == nil && now - fullStateAt >= 300" in state
    polled = _swift(MODEL, "private func polledBooks(", "private func syncState(")
    assert "return all || downloaded || player.book?.assetId == book.assetId" in polled
    # A downloaded copy marked as an older edition still syncs its place (wave-3 review note).
    assert "if retired[book.id] != nil && !downloaded { return false }" in polled
    # The book's page asks for its own state (it may not be among the polled books).
    assert ".task { await model.refreshState(book) }" in _swift(LIBRARY)


def test_ios6_a_rejected_bookmark_takes_the_macs_version() -> None:
    sync = _swift(MODEL, "func sync() async {", "/// Why the Mac did not take a position")
    bookmarks = sync[sync.index("for bookmark in try store.pendingBookmarks()") :]
    assert (
        bookmarks.index('if receipt.status == "rejected" {')
        < bookmarks.index("try store.dropLocalBookmark(bookmark.bookmarkId)")
        < bookmarks.index("try store.acknowledgeBookmark(bookmark.eventId)")
    )
    assert 'try execute("DELETE FROM bookmarks WHERE id=?", [bookmarkId])' in _swift(STORE)


def test_ios8_one_failed_watched_mark_no_longer_blocks_the_others() -> None:
    send = _swift(
        MEDIA, "func sendVideoPositions() async {", "/// Changes the record of a downloaded episode"
    )
    watched = send[: send.index("item.positionDirty && !isOtherMac(item)")]
    assert "} catch let refusal as MacRefusal where refusal.status == 422 {" in watched
    assert "if unreachable(error) { break }" in watched
    assert "dropStale(item, after: error); break" not in watched


def test_ios9_unsent_periodic_saves_of_one_session_are_merged() -> None:
    enqueue = _swift(STORE, "private func enqueueLocked(", "public func bindHandoff(")
    assert 'if old == nil, mergesPeriodic, checkpoint.reason == "periodic",' in enqueue
    assert "previous.playbackSessionId == checkpoint.playbackSessionId" in enqueue
    assert (
        "checkpoint.continuousSince = previous.continuousSince ?? CheckpointRun(observedAt: previous.observedAt, positionMs: previous.positionMs)"
        in enqueue
    )
    assert 'store.mergePeriodic = macSupports("checkpoint-runs")' in _swift(MODEL)
    assert "public var continuousSince: CheckpointRun? = nil" in _swift(CONTRACTS)


def test_ios10_hashing_frees_each_chunk_and_a_download_is_hashed_once() -> None:
    hashing = _swift(
        "ios/PudgeCore/Sources/PudgeCore/FileIntegrity.swift", "public static func sha256(", "#else"
    )
    assert "try autoreleasepool {" in hashing
    finished = _swift(
        TRANSFERS, "didFinishDownloadingTo location: URL)", "func urlSession(_ session: URLSession, task:"
    )
    assert (
        "try FileIntegrity.verify(temporary, sha256: record.part.sha256, sizeBytes: record.part.sizeBytes)"
        in finished
    )
    assert "try store.markVerified(record.part, at: final, hashed: record.part.sha256)" in finished
    assert "if let hashed, hashed == part.sha256 {" in _swift(
        STORE, "public func markVerified(", "public func ready("
    )


def test_ios11_download_progress_reloads_only_the_transfers() -> None:
    progress = _swift(TRANSFERS, "didWriteData bytesWritten: Int64,", "func urlSessionDidFinishEvents(")
    assert "try? store.saveTransfer(record); notifyProgress()" in progress
    model = _swift(MODEL)
    assert "transferManager?.onProgress = { [weak self] in self?.reloadTransfers() }" in model
    assert (
        "func reloadTransfers() { if let records = try? store.transfers(), records != transfers { transfers = records } }"
        in model
    )


def test_ios15_only_covers_are_fetched_for_books_not_downloaded() -> None:
    covers = _swift(MODEL, "private func prefetchCovers(", "func sync() async {")
    assert (
        "let wanted = items.compactMap { $0.cover } + items.filter { (try? store.ready($0)) == true }.compactMap { $0.readAlong }"
        in covers
    )
    assert "[$0.cover, $0.readAlong]" not in covers


def test_ios7_books_the_mac_prepares_on_request_are_listed_with_a_prepare_button() -> None:
    refresh = _swift(MODEL, "func refresh() async {", "private func moveCarriedPlaces(")
    assert 'let lazy = macSupports("lazy-catalog")' in refresh
    assert '"/api/v2/catalog?lazy=1"' in refresh
    prepare = _swift(MODEL, "func prepareOnMac(", "/// Statistics: time read and watched here")
    assert (
        'connection.post("/api/v2/assets/prepare", value: Request(assetId: item.assetId), as: PrepareAnswer.self)'
        in prepare
    )
    assert "Task { await model.prepareOnMac(item) }" in _swift(LIBRARY)


def test_websec7_the_phone_finds_its_mac_under_any_advertised_name() -> None:
    discovery = _swift(CONNECTION, "final class MacDiscovery", "func stop()")
    assert "guard service.name == target else { return }" not in discovery
    # The name's address, its numeric addresses (kept for when the name stops answering), ours or not.
    assert "onAddress?(address, numeric, sender.name == target)" in discovery
    # The legacy and the private advertisement of one Mac are one address: reported once.
    assert "if let last = reported[address], now - last < 30 { return }" in discovery
    # Every request says this build finds the Mac under any name (the Mac drops the legacy name).
    assert 'request.setValue("any-name", forHTTPHeaderField: "X-Pudge-Discovery")' in _swift(
        CONNECTION, "func request(", "func send<"
    )
    # A temporary failure on the Mac (503) is not a refusal: the item stays queued.
    assert "if code == 503, let reason = Self.reason(body) { throw PudgeError.invalid(reason) }" in _swift(
        CONNECTION, "func send<T: Decodable>(", "/// Raw bytes of a small resource"
    )
    found = _swift(MODEL, "discovery.onAddress = {", "discovery.start(serverId:")
    assert "if named { self.macAvailable = false }" in found
    assert "guard info.serverId == mac.serverId && info.protocolMajor == 2" in found


def test_ios14_the_v2_protocol_is_documented() -> None:
    doc = source("ios/PROTOCOL.md")
    for route in (
        "/api/v2/pair",
        "/api/v2/capabilities",
        "/api/v2/catalog?lazy=1",
        "/api/v2/assets/prepare",
        "/api/v2/sync/state",
        "/api/v2/checkpoints",
        "/api/v2/bookmarks",
        "/api/v2/handoff",
        "/api/v2/commands",
        "/api/v2/archives",
        "/api/v2/activity",
        "/api/v2/media/",
    ):
        assert route in doc, route
    for rule in ("device_changed", "edition_changed", "bookmark_changed", "continuousSince", "8766"):
        assert rule in doc, rule
