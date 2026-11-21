"""iPhone app review fixes (Swift; built and run only in Xcode on the Mac).

policy: nothing here can compile or run the iPhone app, so these guards read its Swift source,
like tests/test_iphone_native_swift_guards.py. The logic that could move into PudgeCore is
unit-tested there (ios/PudgeCore/Tests, run on the Mac by run-ios-xcode-checks.command):
DownloadOwner, OpenedPlace, ReadingPackage.renumber and its unpack hook, evict, forgetResources.

What went wrong before, and what happens now:

1. Switching Macs kept the previous Mac's numbers. Downloaded episodes and volumes are numbered by
   the Mac (from 1 on every Mac), so an unsent "watched" mark of the old episode 17 went to the new
   Mac's episode 17, its subtitles replaced the local ones and downloads/check swapped or pruned
   unrelated volumes. Now every download records its Mac (older ones are stamped once with the Mac
   bound at the update); those of another Mac move to local ids below zero, stay playable and
   readable, and are never synced.
2. Progress went backwards on the Mac: a downloaded copy opened at the phone's old place and closing
   it sent that place with a fresh time. Now list refreshes bring the Mac's place to downloads with
   nothing unsent, a place is saved only after the player or reader moved away from where it opened,
   and every position carries `sentAt` so the Mac corrects a phone clock that runs ahead.
3. A pairing link opened from outside the app paired at once (it could move sync to any host naming
   the paired Mac's id). Now it asks first, showing the Mac, its address and a certificate change.
4. Deleting a download dropped an unsent "watched" mark or place. Now the record waits (without the
   file) and is sent when the Mac answers.
5. Updating a downloaded volume wrote back the place read when the download started, and a kill
   between unpacking and writing the record left a volume without it. Now the place is taken right
   before writing, and the record is in the new copy before it replaces the old one.
6. Importing, archiving and freeing space hashed whole audiobooks on the main thread (frozen app).
7. Importing AIFF, CAF or audio-only MP4 copied the files and then failed, leaving them behind.
8. Disconnecting left the background download session valid; pairing the same Mac again opened a
   second one with the same identifier.
9. When iOS ended the background time of an episode download it was cancelled, its data deleted and
   "Download all" dropped it. Now it pauses with the app and continues when Pudge is open again.
"""

from __future__ import annotations

import re
import time

from source_policy import source, sources

MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"
APP = "ios/Pudge/App/PudgeApp.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
READ = "ios/Pudge/UI/ReadScreen.swift"
TRANSFERS = "ios/Pudge/Features/Transfers/TransferManager.swift"
STORE = "ios/PudgeCore/Sources/PudgeCore/LocalStore.swift"
PACKAGE = "ios/PudgeCore/Sources/PudgeCore/ReadingPackage.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/CoreTests.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    """Swift text with whitespace collapsed; from `start` up to the next `end` when given."""
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def _struct(text: str, name: str) -> str:
    body = text[text.index(f"struct {name}:") :]
    return body[: body.index("\n}")]


def test_downloads_keep_their_mac_and_another_macs_are_never_synced() -> None:
    media = source(MEDIA)
    for name in ("LocalEpisode", "ReadingDownload"):
        assert "var serverId: String?" in _struct(media, name), name
    load_video = _swift(MEDIA, "func loadLocalEpisodes(", "func subtitles(")
    load_reading = _swift(MEDIA, "func loadReadingDownloads(", "private func saveReadingRecord(")
    for load in (load_video, load_reading):
        assert (
            "if record.serverId == nil, let bound {" in load or "if item.serverId == nil, let bound {" in load
        )
        assert "DownloadOwner.localIds(owners, bound: bound)" in load
    assert "serverId: client.mac.serverId)" in _swift(
        MEDIA, "func download(_ episode:", "private func preparedVideo("
    )
    assert "record.serverId = serverId" in _swift(
        MEDIA, "private nonisolated static func packageRecord(", "// MARK:"
    )
    send_video = _swift(MEDIA, "func sendVideoPositions(", "private func updateEpisode(")
    assert send_video.count("&& !isOtherMac(item)") == 2
    assert "for record in records where !isOtherMac(record) {" in _swift(
        MEDIA, "func sendReadingPositions(", "// MARK: Study"
    )
    check = _swift(MEDIA, "func checkReadingUpdates(", "/// Opening a volume deleted on the Mac")
    assert "readingDownloads.values.filter { !isOtherMac($0) }" in check
    assert "!isOtherMac(local), let revision = item.subtitleRevision" in media
    # Renumbered downloads stay another Mac's after switching back to their Mac (A -> B -> A):
    # their ids name other items there, so they were posted, checked and pruned as missing.
    assert "{ episode.id < 0 || isOtherMac(episode.serverId) }" in media
    assert "{ record.id < 0 || isOtherMac(record.serverId) }" in media
    # Switching sends the downloads' marks to the previous Mac first, then moves them aside.
    switch = _swift(MODEL, "func switchMac(", "private func pairWithMac(")
    assert switch.index(
        "await media.sendVideoPositions(); await media.sendReadingPositions()"
    ) < switch.index("let summary = try store.rebindMac(to: payload.serverId) media.macSwitched()")
    assert "func testDownloadsOfAPreviousMacGetLocalIdsBelowZero()" in source(CORE_TESTS)


def test_positions_follow_the_mac_and_opening_alone_saves_nothing() -> None:
    assert "adoptMacPlaces(episodes)" in _swift(
        MEDIA, "func refreshEpisodes(", "private func adoptMacPlaces("
    )
    reading = _swift(MEDIA, "func refreshReading(", "func refreshEpisodes(")
    assert reading.index("adoptMacReadingPlaces()") < reading.index("novels += savedNovelItems()")
    adopt = _swift(MEDIA, "private func adoptMacPlaces(", "private func refreshDownloadedSubtitles(")
    assert "!local.positionDirty, !isOtherMac(local)" in adopt
    assert "record.positionDirty != true && !isOtherMac(record)" in adopt
    watch = _swift(WATCH)
    assert watch.count("if video.opened.counts(position: Double(") == 2
    assert "media.saveVideoPosition(" not in watch.replace(
        "if video.opened.counts(position: Double(value)) { media.saveVideoPosition(", ""
    ).replace("if video.opened.counts(position: Double(video.timeMs)) { media.saveVideoPosition(", "")
    assert "opened = OpenedPlace(position: Double(startMs), tolerance: 5000)" in watch
    read = _swift(READ)
    assert "await open(start.chapter, fraction: start.offset, initial: true)" in read
    save = _swift(READ, "private func save(force: Bool)", "private struct ChromeTimer")
    assert save.index("if !moved { return }") < save.index("media.saveNovelPosition(")
    # Manga: only a turn is saved (MangaReadingPlace, PudgeCore; opening, rotating and pairing
    # the pages anew are none), see tests/test_fix_manga_two_page_view.py.
    manga = _swift(READ, "struct MangaReaderScreen", "struct MangaPageView")
    assert manga.index(
        "guard let save = place.turned(to: tag, generation: generation) else { return }"
    ) < manga.index("await media.saveMangaPosition(")
    assert "func testOpenedPlaceCountsOnlyAfterMovingAway()" in source(CORE_TESTS)


def test_positions_carry_the_phone_clock_the_mac_corrects() -> None:
    # The field the Mac reads (behaviour) is the one every position request sends.
    from pudge.iphone_media import IPhoneMedia

    media = source(MEDIA)
    for name in ("NovelPositionRequest", "MangaPositionRequest", "EpisodePositionRequest"):
        line = next(row for row in media.splitlines() if row.startswith(f"struct {name}:"))
        assert "var sentAt: Double? = Date().timeIntervalSince1970" in line, name
    now = time.time()
    phone = now + 3600  # the phone's clock runs an hour ahead
    observed = IPhoneMedia._observed({"observedAt": phone - 60, "sentAt": phone})
    assert abs(observed - (now - 60)) < 5
    assert IPhoneMedia._observed({"observedAt": now - 60}) == now - 60


def test_a_pairing_link_asks_before_pairing() -> None:
    app = _swift(APP)
    assert ".onOpenURL { url in model.offerLink(url.absoluteString) }" in app
    assert "await model.pair(url" not in app
    dialog = app[app.index('.confirmationDialog("Connect to this Mac?"') :]
    assert (
        'Button("Connect to \\(offer.serverName)") { model.linkOffer = nil; Task { await model.pair(offer.text) } }'
        in dialog
    )
    assert "Text(offer.explanation(device: PudgeTheme.device))" in dialog
    offer = _swift(MODEL, "func offerLink(", "func pair(_ text: String)")
    assert "await" not in offer and "pair(" not in offer
    assert "paired?.certificateSHA256 != payload.certificateSHA256" in offer
    assert "another certificate" in _swift(MODEL, "struct PairingLinkOffer", "@MainActor")


def test_deleting_a_download_keeps_what_the_mac_has_not_got() -> None:
    episode = _swift(MEDIA, "func deleteEpisode(", "/// Downloads episodes one after another")
    assert (
        "let files = unsent ? [videoURL(id), cuesURL(id)] : [videoURL(id), cuesURL(id), metaURL(id)]"
        in episode
    )
    assert "Task { await sendVideoPositions() }" in episode
    assert "else if item.unsent && !isOtherMac(item) { pending[item.id] = item }" in _swift(
        MEDIA, "func loadLocalEpisodes(", "func subtitles("
    )
    reading = _swift(MEDIA, "func deleteReading(", "/// The Mac packs the volume")
    assert "record.positionDirty == true, record.deletedOnMacAt == nil, !isOtherMac(record)" in reading
    assert "write(to: pendingReadingURL(kind, id), options: .atomic)" in reading
    send = _swift(MEDIA, "func sendReadingPositions(", "// MARK: Study")
    assert "+ pendingReading.values.filter { $0.positionDirty == true }" in send


def test_a_volume_update_keeps_the_place_read_meanwhile_and_its_record() -> None:
    download = _swift(MEDIA, "func downloadReading(", "// MARK: Manga OCR on request")
    assert "try ReadingPackage.unpack(partial, into: target) { unpacked in" in download
    assert (
        'try JSONEncoder().encode(staged).write(to: unpacked.appendingPathComponent("download.json")'
        in download
    )
    after = download[download.index("}.value") :]
    assert "previous: placeToKeep(key), serverId: server)" in after
    assert "let previous = readingDownloads[key]" not in download
    unpack = _swift(PACKAGE, "public static func unpack(", "extension ReadingPackage")
    assert unpack.index("try beforeReplacing?(temporary)") < unpack.index(
        "try manager.moveItem(at: temporary, to: folder)"
    )
    assert "func testUnpackAddsTheRecordBeforeTheNewCopyReplacesTheOldOne()" in source(CORE_TESTS)


def test_audiobook_hashing_is_off_the_main_thread() -> None:
    importing = _swift(MODEL, "func importAudio(", "/// Small rotating text log")
    detached = importing[importing.index("try await Task.detached(priority: .userInitiated)") :]
    assert detached.index("try FileManager.default.copyItem(at: url, to: destination)") < detached.index(
        "}.value"
    )
    # Hashed once: markVerified takes the hash just computed (IOS-10).
    assert detached.index(
        "try library.markVerified(made, at: file, original: true, hashed: sha)"
    ) < detached.index("}.value")
    archive = _swift(MODEL, "func archive(", "func offload(")
    assert (
        "try await Task.detached(priority: .utility) { try FileIntegrity.verify(url, sha256: sha, sizeBytes: size) }.value"
        in archive
    )
    offload = _swift(MODEL, "func offload(", "func resolve(")
    assert "try await Task.detached(priority: .utility) { try library.evict(" in offload
    evict = _swift(STORE, "public func evict(", "public func forgetResources(")
    assert evict.index("try FileIntegrity.verify(checked.url") < evict.index("try withLock {")
    assert "copy.state.generation == checked.state.generation" in evict


def test_imports_check_the_format_first_and_clean_up_on_failure() -> None:
    importing = _swift(MODEL, "func importAudio(", "/// Small rotating text log")
    assert importing.index(
        "guard AudioManifest.audioExtensions.contains(url.pathExtension.lowercased()) else {"
    ) < (importing.index("copyItem("))
    failure = importing[importing.rindex("} catch {") :]
    assert "for file in copied { try? FileManager.default.removeItem(at: file.url) }" in failure
    assert "try? store.forgetResources(copied.map { $0.id })" in failure
    assert "Self.audioExtensions.contains(" in _swift("ios/PudgeCore/Sources/PudgeCore/Contracts.swift")


def test_disconnecting_ends_the_background_download_session() -> None:
    assert "func invalidate() { transfers.invalidateAndCancel() }" in _swift(TRANSFERS)
    disconnect = _swift(MODEL, "func disconnect()", "func changeAddress(")
    assert "transferManager?.invalidate(); transferManager = nil" in disconnect
    configure = _swift(MODEL, "private func configure(", "func pair(")
    assert "transferManager?.invalidate(); transferManager = try TransferManager(" in configure


def test_an_episode_download_pauses_in_the_background_instead_of_being_cancelled() -> None:
    download = _swift(MEDIA, "func download(_ episode:", "private func preparedVideo(")
    expiry = download[
        download.index('beginBackgroundTask(withName: "Pudge video")') : download.index("defer {")
    ]
    assert "cancelDownload" not in expiry
    assert "UIApplication.shared.endBackgroundTask(background.id)" in expiry


def test_rv_ios_background_time_and_image_cache_guards() -> None:
    """Whole-codebase iOS review: background tasks end on expiry, images are cost-limited,
    probe connections are closed, the video clock ignores invalid times."""
    app = source("ios/Pudge/App/PudgeApp.swift")
    model = source("ios/Pudge/App/PudgeModel.swift")
    media = source("ios/Pudge/Features/Media/MediaModel.swift")
    watch = source("ios/Pudge/UI/WatchScreen.swift")
    for text, name in ((app, '"Pudge position"'), (media, '"Pudge episode place"')):
        start = text.index("beginBackgroundTask(withName: " + name + ") {")
        assert "UIApplication.shared.endBackgroundTask(task.id)" in text[start : start + 400]
    assert "images.totalCostLimit" in media
    assert "images.setObject(image, forKey: path as NSString)\n" not in media
    pair = model[model.index("private func pairWithMac(") : model.index("func cancelPairing()")]
    assert "client.close()" in pair
    assert "ISO8601DateFormatter()" not in model[model.index("static func write(") :][:300]
    assert "guard let self, time.seconds.isFinite else { return }" in watch


def test_rv2_replaced_mac_connections_close_once_unreferenced() -> None:
    """A replaced connection (changeAddress, adopt, configure) leaked two URLSessions: it now
    closes in deinit, i.e. only when no stream, transfer or running request holds it. Its
    sessions are private, so no task can be created on them after that."""
    connection = _swift(
        "ios/Pudge/Networking/MacConnection.swift", "final class MacConnection:", "/// Finds the paired Mac"
    )
    assert "private let session: URLSession" in connection
    assert "private let probeSession: URLSession" in connection
    assert "deinit { close() }" in connection
    assert (
        "func data(for request: URLRequest) async throws -> (Data, URLResponse) { try await session.data(for: request) }"
        in connection
    )
    assert (
        "guard first else { return } session.finishTasksAndInvalidate(); probeSession.finishTasksAndInvalidate()"
        in connection
    )
    assert "invalidateAndCancel" not in connection
    for relative, text in sources("ios/Pudge/**/*.swift").items():
        if relative.endswith("MacConnection.swift"):
            continue
        assert ".session.data(" not in text, relative


def test_rv2_video_download_expiry_ends_its_task_synchronously() -> None:
    download = _swift(MEDIA, "func download(_ episode:", "private func preparedVideo(")
    expiry = download[
        download.index('beginBackgroundTask(withName: "Pudge video")') : download.index("defer {")
    ]
    assert "MainActor.assumeIsolated {" in expiry
    assert "Task {" not in expiry


def test_rv2_pictures_are_downsampled_off_the_main_thread() -> None:
    helper = _swift(MEDIA, "enum DownsampledImage", "/// One (ranged) GET")
    for option in (
        "kCGImageSourceCreateThumbnailFromImageAlways: true",
        "kCGImageSourceCreateThumbnailWithTransform: true",
        "kCGImageSourceShouldCacheImmediately: true",
        "kCGImageSourceThumbnailMaxPixelSize: limit",
    ):
        assert option in helper
    assert "max(screen.bounds.width, screen.bounds.height) * screen.scale * 2" in helper
    image = _swift(MEDIA, "func image(_ path: String) async -> UIImage? {", "// MARK: Light novels")
    assert image.count("await Task.detached(priority: .userInitiated)") == 2
    assert "UIImage(contentsOfFile:" not in image and "UIImage(data:" not in image
    assert "images.setObject(saved, forKey: key, cost: DownsampledImage.cost(saved))" in image
    assert "images.setObject(fetched, forKey: key, cost: DownsampledImage.cost(fetched))" in image
    # The manga reader gets its pages through this cache.
    assert 'await media.image("manga/\\(book)/pages/\\(index)")' in _swift(READ)


def test_rv2_audiobook_covers_load_once_per_cover() -> None:
    library = _swift("ios/Pudge/UI/LibraryScreen.swift", "enum BookCoverImages", "struct BookScreen: View")
    assert "UIImage(contentsOfFile:" not in library
    assert "store.resource(cover.resourceId)" not in library
    assert library.count("image = await BookCoverImages.load(book.cover, store: store)") == 2
    assert (
        library.count(
            '.task(id: "\\(book.cover?.resourceId ?? ""):\\(book.cover?.sha256 ?? ""):\\(generation)")'
        )
        == 2
    )
    assert "await Task.detached(priority: .utility)" in library
    assert "cache.totalCostLimit" in library
    assert "if changed { coverGeneration += 1; reloadLocal() }" in _swift(MODEL)


def test_rv2_storage_usage_is_measured_off_the_main_thread_and_debounced() -> None:
    settings = _swift("ios/Pudge/UI/SettingsScreen.swift")
    assert "model.storageUsage()" not in settings
    assert ".onReceive(model.media.$localEpisodes.dropFirst()) { _ in usageRequest += 1 }" in settings
    assert ".task(id: usageRequest) {" in settings
    assert "try? await Task.sleep(nanoseconds: 300_000_000) if Task.isCancelled { return }" in settings
    load = _swift(
        MODEL, "func loadStorageUsage() async -> StorageUsage {", "/// Settings → Storage → Other files"
    )
    assert "await Task.detached(priority: .utility)" in load


def test_rv2_on_device_episodes_need_no_json_round_trip() -> None:
    assert "try!" not in source(WATCH)
    assert "extension EpisodeItem {" not in source(WATCH)
    contracts = _swift(
        "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift",
        "public struct EpisodeItem:",
        "public enum VideoQuality",
    )
    assert (
        "public init(id: Int, mediaId: Int, number: Int, title: String, episodeTitle: String, watched: Bool,"
        in contracts
    )


def test_rv2_transfer_progress_reads_one_record() -> None:
    progress = _swift(TRANSFERS, "didWriteData bytesWritten", "func urlSessionDidFinishEvents")
    assert "if var record = try? store.transfer(id) {" in progress
    assert "store.transfers()" not in progress
    assert 'query("SELECT body FROM transfers WHERE id=?", [id])' in _swift(STORE)
    assert "func testOneTransferIsReadById()" in source(CORE_TESTS)


def test_rv2_reading_files_are_read_off_the_main_thread() -> None:
    media = _swift(MEDIA)
    assert "func savedChapter(_ book: Int, index: Int) async -> ChapterText? {" in media
    text = _swift(MEDIA, "func mangaText(", "func mangaSpreadPages(")
    assert "await Task.detached(priority: .userInitiated)" in text
    parse = _swift(MEDIA, "private func savedParse(", "private func parsedFiles()")
    assert "await Task.detached(priority: .userInitiated)" in parse
    assert "if savedParses == nil, parsedFiles() == files { savedParses = all }" in parse
