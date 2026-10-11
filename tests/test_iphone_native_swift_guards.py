"""Guards in the iPhone app's Swift code (audit 71Б: kept as policy checks)."""

from __future__ import annotations

import re

# policy: the iPhone app (ios/) is Swift, built and run only in Xcode on the Mac; nothing here
# can compile or run it, so these guards read the Swift source. Each names the phone-only
# behaviour it protects; the Mac side of pairing and transfers is tested by running it
# (tests/test_iphone_*.py).
from source_policy import source

AUDIO = "ios/Pudge/Features/Audio/AudioPlayerService.swift"
TRANSFERS = "ios/Pudge/Features/Transfers/TransferManager.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    """Swift text with whitespace collapsed; from `start` up to the next `end` when given."""
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_now_playing_info_uses_the_ios_playback_rate_not_the_macos_only_state() -> None:
    # MPNowPlayingInfoCenter.playbackState exists only on macOS (the iOS build broke on it);
    # the lock screen learns "playing" or "paused" from the playback rate.
    audio = _swift(AUDIO)
    assert "MPNowPlayingInfoCenter.default().playbackState" not in audio
    assert "MPNowPlayingInfoPropertyPlaybackRate: playing ? Double(rate) : 0.0" in audio


def test_failed_player_start_releases_the_files_it_protected() -> None:
    # Parts being played are protected from storage clean-up. A start that fails half-way
    # (audio session, first seek) releases exactly those parts and forgets the session;
    # otherwise the files stay pinned and the book looks active.
    play = _swift(AUDIO, "func play(", "func seek(")
    failure = play[play.index("} catch {") :]
    assert "protected.append(part.resourceId)" in play
    assert (
        "for resourceId in protected { try? store.setProtection(resourceId: resourceId, active: false) }"
        in failure
    )
    assert 'self.book = nil; sessionId = ""' in failure
    assert "throw error" in failure


def test_manual_download_pause_keeps_resume_data() -> None:
    # Pause cancels the download with resume data and saves it; Resume continues from there
    # instead of downloading the part again.
    pause = _swift(TRANSFERS, "func pause(", "func urlSession(")
    assert "task.cancel { resumeData in" in pause
    assert (
        'record.status = "paused" if let resumeData { record.resumeData = resumeData } try? self.store.saveTransfer(record)'
        in pause
    )
    resume = _swift(TRANSFERS)
    assert (
        "if record.canResume(identity: client.taskIdentity), let resumeData = record.resumeData { task = transfers.downloadTask(withResumeData: resumeData)"
        in resume
    )


def test_damaged_existing_download_is_replaced_by_the_new_file() -> None:
    # The target file already exists but fails verification (an earlier, damaged copy): it is
    # replaced. A plain moveItem failed on the existing file and the damaged copy stayed.
    finished = _swift(TRANSFERS, "didFinishDownloadingTo", 'record.status = "complete"')
    assert (
        "if FileManager.default.fileExists(atPath: final.path) { do { try FileIntegrity.verify(final, "
        "sha256: record.part.sha256, sizeBytes: record.part.sizeBytes) } catch { "
        "try FileManager.default.removeItem(at: final) try FileManager.default.moveItem(at: temporary, to: final) }"
    ) in finished


def test_manually_entered_mac_address_must_be_plain_https() -> None:
    # The typed address receives the pairing credentials: HTTPS only, and no login, query or
    # fragment parts that could carry or leak secrets.
    change = _swift(MODEL, "func changeAddress(", "mac.baseURL = address")
    for check in (
        'url.scheme == "https"',
        "url.host != nil",
        "url.user == nil",
        "url.password == nil",
        "url.query == nil",
        "url.fragment == nil",
    ):
        assert check in change, check


MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
STUDY = "ios/Pudge/UI/StudyViews.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"


def test_without_the_mac_the_word_card_shows_saved_data_marked_outdated_and_no_grades() -> None:
    # Away from the Mac a tapped word shows the meanings saved at the last update (no wait for
    # the network), with a "may be outdated" symbol; grades and decks are not offered.
    card = _swift(STUDY, "struct WordDetailView", "struct WordSheet")
    load = card[card.index("private func load(") :]
    assert "if media.app.macOffline { showSaved(); return }" in load
    assert (
        "if let saved = media.savedWord(lookup.token) { detail = saved; outdated = true; failure = nil }"
        in load
    )
    assert 'Image(systemName: "clock.badge.exclamationmark")' in card
    study = card[
        card.index("if !online || outdated {") : card.index("} else if let options = media.options {")
    ]
    assert "Grades and decks need the Mac" in study
    assert "guard !working, online, !outdated else { return }" in card
    # Back online: fresh entry and grades again.
    assert "if next && (outdated || failure != nil) {" in card and "await load(refresh: true)" in card


def test_word_data_is_saved_from_every_update_for_offline_use() -> None:
    media = _swift(MEDIA)
    assert (
        'client.media("study/words", post: Request(pairs: chunk), as: ItemsResponse<WordDetail>.self)'
        in media
    )
    assert "if value.complete { offlineWords.remember([value]); saveOfflineWords() }" in media
    assert (
        "if !result.isEmpty { offlineWords.remember(states: Array(result.values)); saveOfflineWords() }"
        in media
    )
    # Downloaded subtitles keep the last live colours, and later saved states apply to them.
    assert "let newer = offlineWords.states(newerThan: written)" in media
    assert "media.saveSubtitles(episode.id, track: value)" in _swift(WATCH)


def test_downloaded_episodes_open_without_waiting_for_an_absent_mac() -> None:
    start = _swift(WATCH, "func start(", "struct WatchScreen")
    assert (
        "guard media.app.macConnection != nil, !media.app.macOffline else { playing = local; return }"
        in start
    )
    assert "guard await media.app.reachMac() else {" in start
    episodes = _swift(MEDIA, "func refreshEpisodes(", "await sendVideoPositions()")
    assert "guard await app.reachMac() else { episodesError = Self.episodesOffline; return }" in episodes
    refresh = _swift(MODEL, "func refresh() async {", "let catalog =")
    assert "guard await reachMac() else { reloadLocal(); return }" in refresh
    model = _swift(MODEL)
    assert "if macAvailable && !oldValue && macChecked { reconnected() }" in model
    assert "probe.timeoutIntervalForRequest = 6" in _swift("ios/Pudge/Networking/MacConnection.swift")


READ = "ios/Pudge/UI/ReadScreen.swift"


def test_light_novels_and_manga_download_and_open_without_the_mac() -> None:
    # The Mac packs a volume (tests/test_fix_iphone_reading_downloads.py); the phone downloads
    # it as one verified file, unpacks it (PudgeCore ReadingPackage, unit-tested in Xcode)
    # and reads chapters, illustrations, pages and text boxes from it.
    media = _swift(MEDIA)
    download = _swift(MEDIA, "func downloadReading(", "func sendReadingPositions(")
    assert (
        "var state = try await client.media(route, post: EmptyRequest(), as: ReadingPackageState.self)"
        in download
    )
    assert (
        "try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size) try ReadingPackage.unpack(partial, into: target)"
        in download
    )
    assert "if var saved = savedChapter(book, index: index) {" in media
    assert (
        "if let local = localReadingImage(path), let image = UIImage(contentsOfFile: local.path) {" in media
    )
    assert 'let file = readingURL("manga", book).appendingPathComponent("text-\\(page).json")' in media
    assert "if let saved = savedParse(text) { return saved }" in media
    # Without the Mac the lists show the downloaded volumes; positions read here wait for the Mac.
    reading = _swift(MEDIA, "func refreshReading(", "func refreshEpisodes(")
    assert "novels = savedNovelItems(); manga = savedMangaItems()" in reading
    assert "await sendReadingPositions()" in reading
    assert "record.chapter = chapter; record.offset = offset; record.positionDirty = true" in media
    # Every volume has a download button in its reader and a long-press menu in the lists.
    read = _swift(READ)
    assert 'ReadingDownloadButton(media: media, kind: "ln", id: bookId, title: title)' in read
    assert 'ReadingDownloadButton(media: media, kind: "manga", id: book.id, title: book.title)' in read
    assert (
        '.contextMenu { ReadingDownloadMenu(media: media, kind: "ln", id: book.id, title: book.title) }'
        in read
    )
    assert "let start = media.startPosition(bookId, value)" in read
    assert "ReadingDownloadsScreen(media: model.media)" in _swift("ios/Pudge/UI/SettingsScreen.swift")


def test_an_audiobooks_light_novel_comes_along_for_reading_along_offline() -> None:
    model = _swift(MODEL)
    assert (
        "if let novel = book.lightNovelId { media.downloadLinkedNovel(novel, title: book.seriesTitle ?? book.title) }"
        in model
    )
    assert "for book in books where book.lightNovelId != nil && (try? store.ready(book)) == true {" in model
    linked = _swift(MEDIA, "func downloadLinkedNovel(", "func cancelReading(")
    assert '!readingRemoved.contains("ln:\\(id)")' in linked  # a removed volume is not downloaded again
    player = _swift("ios/Pudge/UI/PlayerScreen.swift")
    assert (
        'if let id = book.lightNovelId, !model.macOffline || model.media.isDownloaded("ln", id) { reader = id } else { readingAlong = true }'
        in player
    )


def test_get_ocr_asks_the_mac_and_takes_only_complete_ocr() -> None:
    # The Mac side (only complete OCR is packed, start/progress, the OCR-only package) is
    # tested in tests/test_fix_iphone_manga_ocr_and_updates.py.
    ocr = _swift(MEDIA, "func getOCR(", "// MARK: Updates of downloaded volumes")
    assert "guard app.macConnection != nil, !app.macOffline, await app.reachMac() else {" in ocr
    assert "Getting OCR needs the Mac" in ocr
    assert (
        'var status = try await client.media("manga/\\(id)/ocr", post: EmptyRequest(), as: MangaOCRStatus.self)'
        in ocr
    )
    assert "while !status.complete {" in ocr
    assert 'if isDownloaded("manga", id) { try await downloadOCR(id, client: client) }' in ocr
    assert 'for name in names where name.hasPrefix("text-") || name == "parsed.json" {' in ocr
    assert "record.ocr = true; record.ocrRevision = state.revision" in ocr
    read = _swift(READ)
    assert "ToolbarItem(placement: .primaryAction) { GetOCRButton(media: media, book: book) }" in read
    assert "if media.needsOCR(book) { GetOCRMenuItem(media: media, book: book) }" in read


def test_downloaded_volumes_are_checked_and_updated_keeping_the_place_read() -> None:
    check = _swift(MEDIA, "func checkReadingUpdates(", "/// Places read on this iPhone")
    assert "guard force || Date().timeIntervalSince1970 - last >= 6 * 3600 else { return }" in check
    assert '!UserDefaults.standard.bool(forKey: "pudge.cellularDownloads") { return }' in check
    assert 'client.media("downloads/check", post:' in check
    assert (
        "switch ReadingRevisions.update(row, revision: record.revision, pagesRevision: record.pagesRevision, ocrRevision: record.ocrRevision) {"
        in check
    )
    assert 'readingNote = (updated.count == 1 ? "Updated on \\(PudgeTheme.device): "' in check
    download = _swift(MEDIA, "func downloadReading(", "// MARK: Manga OCR on request")
    assert (
        "record.chapter = previous?.chapter; record.offset = previous?.offset; record.page = previous?.page"
        in download
    )
    assert "Task { await media.checkReadingUpdates() }" in _swift(MODEL)
    assert "Task { await checkReadingUpdates(force: forceUpdates) }" in _swift(MEDIA)
    assert ".refreshable { await media.refreshReading(forceUpdates: true) }" in _swift(READ)


def test_word_card_grade_buttons_sit_under_the_word_above_the_meanings() -> None:
    # User report: in the card opened by tapping a word (LN, subtitles, manga, read-along) the
    # review buttons must be higher, before the definitions. The Study section (grades first,
    # then the deck controls; the offline note in their place) comes right after the word and
    # reading, before Meanings and Sentence.
    card = _swift(STUDY, "struct WordDetailView", "struct WordSheet")
    body = card[card.index("var body: some View") : card.index("private func load(")]
    word = body.index("Text(detail?.spelling.isEmpty == false")
    offline = body.index("if !online || outdated {")
    grades = body.index("ForEach(options.grades)")
    deck = body.index('Picker("Deck"')
    meanings = body.index('Section("Meanings")')
    sentence = body.index('Section("Sentence")')
    assert word < offline < grades < deck < meanings < sentence
    assert body.index('header: { Text("Study") }') < meanings


STATS = "ios/Pudge/UI/StatsScreen.swift"


def test_statistics_bars_are_split_by_content_type_in_the_macs_colours() -> None:
    # User request: the daily bars split into blocks by content type, the same on the phone.
    # Each day's bar is stacked from per-type parts (one BarMark per day and type), coloured by
    # a fixed type -> colour scale; the colours are the ones the Mac's Statistics page uses.
    from tests.css_support import css_rules

    screen = _swift(STATS)
    chart = screen[screen.index("Chart(parts)") : screen.index(".frame(height: 170)")]
    assert 'BarMark(x: .value("Day", part.day), y: .value("Minutes", part.seconds / 60))' in chart
    assert '.foregroundStyle(by: .value("Type", Self.kindName(part.kind)))' in chart
    assert (
        ".chartForegroundStyleScale(domain: kinds.map { Self.kindName($0) }, range: kinds.map { Self.kindColor($0) })"
        in chart
    )
    assert ".chartXSelection(value: $selectedDay)" in chart and ".chartLegend(" in chart
    colours = dict(
        (kind, f"#{r}{g}{b}".lower())
        for kind, r, g, b in re.findall(
            r'case "([a-z_+]+)": return Color\(red: 0x(\w\w)/255, green: 0x(\w\w)/255, blue: 0x(\w\w)/255\)',
            screen,
        )
    )
    rules = css_rules("statistics.css")
    kinds = [
        "anime",
        "light_novel",
        "manga",
        "audiobook",
        "visual_novel",
        "light_novel+audiobook",
        "simultaneous",
        "review",
    ]
    assert {kind: rules.get(f'[data-stats-kind="{kind}"]', "--stats-kind") for kind in kinds} == {
        kind: colours[kind] for kind in kinds
    }
    order = re.search(r"static let kindOrder = \[([^\]]*)\]", screen).group(1)
    assert [k.strip().strip('"') for k in order.split(",")] == [*kinds, "unknown"]


CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift"


def _swift_fields(text: str, struct: str) -> set[str]:
    body = text[text.index(f"public struct {struct}") :]
    body = body[: body.index("\n}")]
    return set(re.findall(r"public let (\w+):", body))


def test_phone_fun_stats_decode_what_the_mac_sends_and_are_shown(tmp_path) -> None:
    # User: "Yes" to the fun stats on the phone. The phone decodes exactly the fields the Mac
    # sends (optional, so an older Mac without them still decodes) and shows them as cards.
    import json
    from types import SimpleNamespace

    from pudge.database import Database
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    words = {"total": 1, "unique": 1, "new": 1, "by_kind": {"anime": {"total": 1, "unique": 1, "new": 1}}}
    highlights = {
        "longest_streak_days": 1,
        "busiest_day": {"day": "2026-10-01", "seconds": 1},
        "longest_session": {"title": "A", "kind": "anime", "seconds": 1},
        "top_work": {"title": "A", "kind": "anime", "seconds": 1},
        "words": {**words, "pending": 0, "missing": 0},
    }
    service.api = SimpleNamespace(
        consumption_statistics_query=lambda scope, limit, offset: {
            "summary": {},
            "days": [],
            "highlights": highlights,
        }
    )
    sent = json.loads(json.dumps(service.media.stats("7d")))
    contracts = re.sub(r"[ \t]+", " ", source(CONTRACTS))
    assert "public let highlights: StatsHighlights?" in contracts
    assert set(sent["highlights"]) == _swift_fields(contracts, "StatsHighlights")
    assert set(sent["highlights"]["words"]) == _swift_fields(contracts, "StatsWords")
    assert set(sent["highlights"]["words"]["byKind"]["anime"]) == _swift_fields(contracts, "StatsWordCounts")
    entry = _swift_fields(contracts, "StatsEntry")
    for name in ("busiestDay", "longestSession", "topWork"):
        assert set(sent["highlights"][name]) <= entry, name
    for optional in ("busiestDay: StatsEntry?", "words: StatsWords?", "lnCharactersPerMinute: Double?"):
        assert f"public let {optional}" in contracts

    screen = _swift(STATS)
    shown = screen[screen.index("if let highlights = stats.highlights") : screen.index('Section("By type")')]
    assert "Self.highlightRows(highlights)" in shown and 'Section("Stats")' in shown
    rows = screen[screen.index("static func highlightRows") : screen.index("private func highlightCard")]
    for title in (
        "Longest streak",
        "Busiest day",
        "Longest session",
        "Most time",
        "Words met",
        "Different words",
        "New words met",
    ):
        assert f'title: "{title}"' in rows, title
    assert "still counting" in rows and "words.byKind[kind]" in rows


LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"


def test_volumes_deleted_on_the_mac_are_marked_and_leave_a_week_later_unless_opened() -> None:
    # The Mac reports when a volume was deleted (tests/test_fix_iphone_multi_device.py); the rule
    # itself is PudgeCore ReadingRevisions.removable (unit-tested in Xcode).
    check = _swift(MEDIA, "func checkReadingUpdates(", "/// Opening a volume deleted on the Mac")
    assert "record.deletedOnMacAt = row.deletedAt ?? Date().timeIntervalSince1970" in check
    assert "pruneDeletedReading()" in check
    media = _swift(MEDIA)
    assert (
        "ReadingRevisions.removable(deletedAt: $0.deletedOnMacAt, openedAt: $0.openedAt, now: now)" in media
    )
    assert "loadReadingDownloads() pruneDeletedReading()" in media  # also without the Mac, at launch
    assert "novels += savedNovelItems().filter { saved in !novels.contains { $0.id == saved.id } }" in media
    read = _swift(READ)
    assert 'Label("Deleted on the Mac", systemImage: "trash.slash")' in read
    assert 'media.markOpened("ln", bookId)' in read and 'media.markOpened("manga", book.id)' in read


def test_several_iphones_and_ipads_are_named_and_hand_over_to_each_other() -> None:
    model = _swift(MODEL)
    assert (
        "if connection != nil, macAvailable, let playing = progress[book.assetId]?.playingOn, playing != deviceId {"
        in model
    )
    assert 'while result.status == "pending" && waited < 15 {' in model
    library = _swift(LIBRARY)
    assert (
        'Text(state.deviceLabel(checkpoint.deviceId, this: model.deviceId, thisLabel: "This \\(PudgeTheme.device)"))'
        in library
    )
    assert "@MainActor static var device: String { UIDevice.current.model }" in library
    # Places read offline carry when they were read (the Mac keeps a newer place from another device).
    media = _swift(MEDIA)
    assert "observedAt: record.positionAt" in media and "observedAt: item.savedAt" in media
    # No text says "iPhone" where an iPad would read it: the device's own word is used.
    import re

    for path in (
        "ios/Pudge/UI/LibraryScreen.swift",
        READ,
        STUDY,
        WATCH,
        "ios/Pudge/UI/SettingsScreen.swift",
        MEDIA,
        MODEL,
    ):
        texts = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', source(path))
        singular = [
            t
            for t in texts
            if re.search(r"\biPhone\b", t) and "iPad" not in t and t not in {"iPhone"} and "P1" not in t
        ]
        assert singular == [], (path, singular)


def test_reading_text_keeps_a_readable_width_on_ipad() -> None:
    assert ".frame(maxWidth: PudgeTheme.readingWidth).frame(maxWidth: .infinity)" in _swift(READ)
    assert ".frame(maxWidth: PudgeTheme.readingWidth).frame(maxWidth: .infinity)" in _swift(
        "ios/Pudge/UI/PlayerScreen.swift"
    )


def test_phone_by_type_lists_only_types_with_time() -> None:
    # User: "Remove items that are zero (where nothing was done)" — on the phone too.
    screen = _swift(STATS)
    section = screen[screen.index("let active = stats.breakdown") : screen.index('Section("Most time")')]
    assert "stats.breakdown.filter { $0.value >= 1 }" in section
    assert "if !active.isEmpty {" in section and "ForEach(active, id: \\.key)" in section


SETTINGS = "ios/Pudge/UI/SettingsScreen.swift"
STORE = "ios/PudgeCore/Sources/PudgeCore/LocalStore.swift"


def test_pairing_another_mac_offers_switching_instead_of_refusing() -> None:
    # Audit: after a reinstall or a new Mac pairing failed with "Local data belongs to another
    # Mac"; only deleting the app helped. Now the phone asks "Switch to this Mac?", sends what it
    # can to the previous Mac first, pairs, and only then re-binds its data (LocalStore.rebindMac,
    # covered by CoreTests).
    pair = _swift(MODEL, "func pair(_ text: String)", "func switchMac(")
    assert "if let bound = try store.boundMac(), bound != payload.serverId {" in pair
    assert (
        "switchOffer = MacSwitchOffer(text: text, serverName: payload.serverName, summary: try store.macSwitchSummary()) return"
        in pair
    )
    switch = _swift(MODEL, "func switchMac(", "private func pairWithMac(")
    flush = switch.index("if connection != nil, await reachMac() { await sync()")
    assert (
        flush
        < switch.index("let mac = try await pairWithMac(payload)")
        < switch.index("let summary = try store.rebindMac(to: payload.serverId)")
    )
    assert "summary.text(device: PudgeTheme.device)" in switch
    dialog = _swift(SETTINGS, '.confirmationDialog("Switch to this Mac?"', "struct RejectedStatisticsScreen")
    assert (
        "Task { await model.switchMac(offer) }" in dialog
        and "offer.summary.text(device: PudgeTheme.device)" in dialog
    )
    # The new Mac's catalog takes over the last unsent places by the same audio.
    assert "try moveCarriedPlaces(catalog)" in _swift(
        MODEL, "func refresh() async {", "private func prefetchCovers("
    )


def test_pairing_shows_the_code_and_waits_for_allow_on_the_mac() -> None:
    flow = _swift(MODEL, "private func pairWithMac(", "func cancelPairing()")
    assert "var mac = try await reachableMac(payload)" in flow
    assert "if let token = result.token, !token.isEmpty { mac.token = token; break }" in flow
    assert 'guard result.status == "pending", let code = result.confirmationCode else' in flow
    assert "pairingCode = code" in flow and "if pairingCancelled {" in flow
    screen = _swift(SETTINGS)
    assert (
        "if let code = model.pairingCode {" in screen
        and 'Button("Cancel pairing", role: .cancel) { model.cancelPairing() }' in screen
    )


def test_pairing_tries_every_mac_address_of_the_invitation_in_order() -> None:
    reach = _swift(MODEL, "private func reachableMac(", "func disconnect()")
    assert "for address in payload.candidates {" in reach
    assert "if await client.answers() { return mac }" in reach
    parse = _swift(
        "ios/Pudge/Networking/MacConnection.swift", "var candidates: [String] {", "struct PairedMac"
    )
    assert "payload.candidates.allSatisfy(plainHTTPS)" in parse


def test_statistics_the_mac_rejects_are_kept_with_their_reason() -> None:
    send = _swift(MODEL, "private func sendActivity(", "func download(")
    assert "ActivityBatch(items: batch, sentAt: Date().timeIntervalSince1970)" in send
    assert 'for result in answered where result.status == "rejected" {' in send
    assert "try store.rejectActivity(rejected)" in send
    assert 'try store.acknowledgeActivity(answered.filter { $0.status != "rejected" }.map(\\.id))' in send
    assert "RejectedStatisticsScreen(model: model)" in _swift(SETTINGS)
    assert 'reason: "Not sent to the Mac within 30 days"' in _swift(
        STORE, "public func pruneActivity(", "public func rejectedActivity("
    )


def test_start_over_asks_first_and_mark_unfinished_is_separate() -> None:
    # Audit 1A: Start over (a "reset" checkpoint, place 0:00) asks before it moves the place
    # and leaves Finished alone; Mark unfinished ("unfinish") is its own button, offered only
    # when the Mac announces it (an older Mac refuses the reason and would stall the queue).
    book = _swift(LIBRARY, "struct BookScreen", ".navigationTitle(")
    start = book[book.index('Button("Start over") {') :]
    assert start.startswith('Button("Start over") { startOverConfirmation = true }')
    dialog = start[start.index(".confirmationDialog(") : start.index("} else if let active")]
    assert "isPresented: $startOverConfirmation" in dialog
    assert 'model.player.save("reset")' in dialog
    assert book.count('save("reset")') == 1  # only behind the confirmation
    at = book.index("state.finished {")
    finished = book[at : book.index("Section {", at)]
    assert 'if model.macSupports("progress-unfinish") {' in finished
    assert 'Button("Mark unfinished") { model.markUnfinished(book) }' in finished
    mark = _swift(MODEL, "func markUnfinished(", "func readAlong(")
    assert 'guard macSupports("progress-unfinish") else { return }' in mark
    assert 'player.save("unfinish")' in mark and 'reason: "unfinish")' in mark
    assert "try store.enqueue(checkpoint)" in mark
    refresh = _swift(MODEL, "func refresh() async {", "private func moveCarriedPlaces(")
    assert 'connection.send("/api/v2/capabilities", as: Capabilities.self)' in refresh
    assert (
        'UserDefaults.standard.set(Array(macCapabilities).sorted(), forKey: "pudge.macCapabilities")'
        in refresh
    )


def test_reader_look_is_asked_on_every_open_and_the_saved_one_shows_meanwhile() -> None:
    # The Mac's word colours and mark style may change after the phone saved them: every
    # reader (light novel, look-up, episode) asks the Mac again; the saved look shows until it answers.
    for relative in (READ, STUDY):
        text = _swift(relative)
        assert (
            "let look = Task { await media.loadReaderLook() } if media.readerLook == nil { await look.value }"
            in text
        )
        assert "if media.readerLook == nil { await media.loadReaderLook() }" not in text
    watch = _swift(WATCH)
    assert "Task { await media.loadReaderLook() }" in watch and "if media.readerLook == nil" not in watch
    load = _swift(MEDIA, "func loadReaderLook(", "// MARK:")
    assert "guard look != readerLook else { return } readerLook = look" in load
