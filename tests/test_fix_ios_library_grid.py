"""iPhone/iPad libraries as grids of cover cards like the Mac library (structural guards).

The Swift itself is compiled and its layout rules tested on the Mac (`swift test` runs
ios/PudgeCore/Tests/PudgeCoreTests/LibraryCardsTests.swift); here we check that every
library screen uses the shared grid and keeps the actions, refresh, search and badges.
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
READ = "ios/Pudge/UI/ReadScreen.swift"
LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"
GRID = "ios/Pudge/UI/LibraryGrid.swift"
CORE = "ios/PudgeCore/Sources/PudgeCore/LibraryCards.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/LibraryCardsTests.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_layout_rules_live_in_pudgecore_with_xctest() -> None:
    core = _swift(CORE)
    assert "public enum LibraryGrid" in core and "public static func columns(width: Double) -> Int" in core
    assert "public enum CardStatusText" in core and "public enum AnimeSection" in core
    tests = _swift(CORE_TESTS)
    for name in (
        "testPhonePortraitHasTwoOrThreeColumns",
        "testLandscapeAndIPadShowMoreColumnsOfAbout150To180Points",
        "testAnimeStatus",
        "testReadingStatus",
        "testAudiobookStatus",
    ):
        assert f"func {name}()" in tests, name


def test_the_new_view_file_is_in_the_xcode_project() -> None:
    pbx = source("ios/Pudge.xcodeproj/project.pbxproj")
    assert '"path" = "Pudge/UI/LibraryGrid.swift";' in pbx


def test_every_library_is_a_grid_of_cards_not_a_list() -> None:
    screens = {
        "anime": _swift(WATCH, "struct WatchScreen: View", "extension MediaModel {"),
        "light novels": _swift(READ, "struct NovelsScreen: View", "struct NovelCard: View"),
        "novel shelf": _swift(READ, "struct NovelShelfScreen: View", "struct MangaScreen: View"),
        "manga": _swift(READ, "struct MangaScreen: View", "extension MangaItem {"),
        "manga shelf": _swift(READ, "struct MangaShelfScreen: View", "/// Settings → Storage"),
        "audiobooks": _swift(LIBRARY, "struct LibraryScreen: View", "struct BookSeriesRoute"),
        "audiobook series": _swift(LIBRARY, "struct BookSeriesScreen: View", "struct BookCardCover: View"),
    }
    for name, screen in screens.items():
        assert "LazyVGrid(columns: layout.columns" in screen, name
        assert "LibraryLayout(width: width)" in screen and ".libraryWidth($width)" in screen, name
        assert "List {" not in screen, name


def test_pull_to_refresh_search_and_empty_states_stay() -> None:
    watch = _swift(WATCH, "struct WatchScreen: View", "extension MediaModel {")
    assert (
        ".refreshable { media.app.retryMacNow(); await media.refreshEpisodes(); await media.refreshPlanning() }"
        in watch
    )
    assert 'ContentUnavailableView("No episodes"' in watch
    read = _swift(READ)
    assert read.count(".refreshable { await media.refreshReading(forceUpdates: true) }") == 2
    # The pull-down header with search and "Only on iPhone/iPad" (IOS4, tests/test_fix_ios4_phone_screens.py).
    assert read.count('.libraryFilterHeader($filter, prompt: "Title", device: PudgeTheme.device)') == 2
    library = _swift(LIBRARY)
    assert ".refreshable { await model.refreshNow() }" in library
    assert '.libraryFilterHeader($filter, prompt: "Book title", device: PudgeTheme.device)' in library


def test_anime_cards_keep_every_row_action() -> None:
    card = _swift(WATCH, "struct AnimeGridCard: View", "struct PlanningCard: View")
    for action in (
        "launcher.start(local, media: media)",  # play / continue a downloaded episode
        "launcher.watch(item, media: media)",  # watch from the Mac
        "Task { await media.download(item) }",
        "media.cancelDownload(item.id)",
        "media.cancelStream(item.id)",
        "media.downloadAll(missing)",
        "media.deleteEpisode(item.id)",
    ):
        assert action in card, action
    # The Mac's home sections and its status line come from PudgeCore.
    watch = _swift(WATCH)
    assert "AnimeShelves.arrange(" in watch and "AnimeSection.allCases.compactMap" in _swift(CORE)
    assert "CardStatusText.anime(" in card
    assert "accessibilityLabel(label)" in _swift(
        GRID, "struct CardPlayButton: View", "extension MediaModel {"
    )


def test_card_menu_offline_badges_and_accessibility() -> None:
    grid = _swift(GRID, "struct LibraryCard<", "extension LibraryCard where")
    assert (
        'Image(systemName: "ellipsis")' in grid
        and '.accessibilityLabel("More actions for \\(title)")' in grid
    )
    assert "card.contextMenu { actions() }" in grid
    assert (
        ".accessibilityLabel(Text(title))" in grid and ".accessibilityValue(Text(accessibilityValue))" in grid
    )
    assert "badge?.text" in grid
    badges = _swift(GRID, "extension MediaModel {", "struct DeletedOnMacNote")
    assert ".deletedOnMac : .onDevice(nil)" in badges
    read = _swift(READ)
    assert (
        'badge: media.cardBadge("ln", book.id)' in read and 'badge: media.cardBadge("manga", book.id)' in read
    )
    assert 'DeletedOnMacNote(media: media, kind: "ln", id: book.id)' in read
    library = _swift(LIBRARY, "struct BookCard: View", "struct BookSeriesCard: View")
    assert "badge: ready ? .onDevice(nil) : nil" in library and "model.download(book)" in library


def test_covers_come_from_the_cover_caches() -> None:
    grid = _swift(GRID, "struct CardCover: View", "struct LibraryCard<")
    assert ".task(id: path) { image = await media.image(path) }" in grid
    read = _swift(READ)
    assert 'CardCover(media: media, path: "cover/ln/\\(book.id)", symbol: "book")' in read
    assert 'CardCover(media: media, path: "cover/manga/\\(book.id)", symbol: "book.pages")' in read
    assert (
        'CardCover(media: media, path: "cover/anime/\\(group.mediaId)", symbol: "play.rectangle")'
        in _swift(WATCH)
    )
    library = _swift(LIBRARY, "enum BookCoverImages", "struct BookCover: View")
    assert "guard let local = try? store.resource(resourceId) else { return nil }" in library


def test_grids_follow_rotation_back_to_portrait() -> None:
    grid = _swift(GRID)
    # The scroll view's own frame is measured (not its content, which stayed landscape-wide),
    # and a grid is rebuilt when its column layout changes.
    assert "onGeometryChange(for: CGFloat.self)" in grid and "LibraryWidthKey" not in grid
    # Measured at the root tab view (scroll views followed their wide content after a rotation).
    assert ".libraryContainer($contentWidth)" in _swift("ios/Pudge/App/PudgeApp.swift")
    assert "container > 0 ? container : own" in grid
    for screen in (WATCH, READ, LIBRARY):
        text = _swift(screen)
        assert text.count("LazyVGrid(columns: layout.columns") == text.count(".libraryGridIdentity(layout)")


def test_anime_shelves_split_ongoing_from_completed_like_the_mac() -> None:
    core = _swift(CORE)
    assert 'case .completedReady: return "Completed & ready"' in core
    assert "airing == false ? .completedReady : .newReady" in core
    assert "airing: group.airing" in _swift(WATCH)


def test_episode_delete_confirmation_points_at_its_trash_button() -> None:
    watch = _swift(WATCH)
    # A popover anchored to the trash can (a list-row confirmation dialog showed mid-screen on iPad).
    assert (
        '.deleteConfirmation("Delete this episode from the \\(PudgeTheme.device)?", isPresented: $deletePrompt)'
        in watch
    )
    assert "attachmentAnchor: .rect(.bounds)" in watch and ".presentationCompactAdaptation(.popover)" in watch
