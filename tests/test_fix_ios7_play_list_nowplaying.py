"""iPhone/iPad (IOS7): the anime card's Play opens the nearest episode (never the episode list),
the episode list emphasises the next episode and greys watched ones (Cancel on the left), and the
audiobook's Now Playing / remote commands (lock screen, headphones, Apple Watch): chapter title,
cover, chapter progress, chapter jumps, the app's speeds, bookmark, skip intervals.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (AnimeEpisodeChoiceTests, NowPlayingChaptersTests); the SwiftUI and
MediaPlayer wiring is guarded here by reading the Swift source.
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
GRID = "ios/Pudge/UI/LibraryGrid.swift"
AUDIO = "ios/Pudge/Features/Audio/AudioPlayerService.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"
CHOICE = "ios/PudgeCore/Sources/PudgeCore/AnimeEpisodeChoice.swift"
CHOICE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/AnimeEpisodeChoiceTests.swift"
CHAPTERS = "ios/PudgeCore/Sources/PudgeCore/NowPlayingChapters.swift"
CHAPTERS_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/NowPlayingChaptersTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_card_play_opens_the_nearest_episode_never_the_list() -> None:
    card = _flat(WATCH, "struct AnimeGridCard: View {", "/// What EpisodeAction offers for the next episode")
    assert "accessory: playButton()," in card
    play = _flat(WATCH, "    private func playButton() -> AnyView? {", "/// What EpisodeAction offers")
    assert "AnimeCardPlay.action(group.episodes.map(media.choiceItem), macAvailable: macAvailable)" in play
    # The same launcher calls as the episode's own Play / Watch in the list (same resume place).
    assert "case .playHere:" in play and "launcher.start(local, media: media)" in play
    assert "case .watchFromMac:" in play and "launcher.watch(item, media: media)" in play
    # A disabled button passed the tap through to the card (the episode list).
    assert ".disabled(" not in play
    assert "dimmed: dimmed" in play
    button = _flat(GRID, "struct CardPlayButton: View {", "extension MediaModel {")
    assert "var dimmed = false" in button and ".disabled(" not in button
    # The card itself still opens the list.
    assert 'identifier: "anime-card", open: open' in card
    next_episode = _flat(WATCH, "    func nextEpisode(_ group: AnimeGroup) -> EpisodeItem? {", "\n}")
    assert "AnimeCardPlay.next(group.episodes.map(choiceItem))" in next_episode


def test_episode_list_emphasises_next_and_greys_watched() -> None:
    detail = _flat(WATCH, "struct AnimeDetailScreen: View {", "struct EpisodeDownloadSheet: View {")
    assert "EpisodeRowStyle.rows(value.episodes.map(media.choiceItem))" in detail
    assert "style: styles[item.id] ?? .plain" in detail
    row = _flat(WATCH, "struct EpisodeRow: View {", "// MARK: Player")
    assert "var style: EpisodeRowStyle = .plain" in row
    assert "case .next: return .title3.bold()" in row and "case .watched: return .subheadline" in row
    assert "style == .watched ? Color.secondary : Color.primary" in row
    assert "EpisodeAction(media: media, launcher: launcher, item: item, style: style)" in row
    action = _flat(WATCH, "struct EpisodeAction: View {", "struct DownloadQualityChoices: View {")
    assert "compact: style == .watched" in action
    # Cancel sits on the left of each download / stream line (before its text and the Spacer).
    for cancel in ("media.cancelDownload(item.id)", "media.cancelStream(item.id)"):
        for at in [m.start() for m in re.finditer(re.escape(cancel), action)]:
            stack = action.rindex("HStack {", 0, at)
            assert "Spacer()" not in action[stack:at], cancel
    assert action.count('Button("Cancel")') == 3


def test_now_playing_and_remote_commands() -> None:
    audio = _flat(AUDIO)
    now = _flat(AUDIO, "    private func nowPlaying() {", "    /// Lock screen / Dynamic Island artwork")
    assert (
        "NowPlayingChapters.progress(positionMs: positionMs, durationMs: book.durationMs, chapters: book.chapters)"
        in now
    )
    assert "MPMediaItemPropertyPlaybackDuration: Double(shown.durationMs) / 1000" in now
    assert "MPNowPlayingInfoPropertyElapsedPlaybackTime: Double(shown.elapsedMs) / 1000" in now
    assert "MPMediaItemPropertyTitle: book.title" in now and "MPMediaItemPropertyArtist: chapter" in now
    assert "info[MPMediaItemPropertyArtwork] = art" in now
    assert "MPNowPlayingInfoPropertyDefaultPlaybackRate: Double(rate)" in now
    system = _flat(AUDIO, "    private func configureSystem() {", "systemObservers.append(")
    assert "commands.nextTrackCommand.addTarget" in system and "self?.nextChapter()" in system
    assert "commands.previousTrackCommand.addTarget" in system and "self?.previousChapter()" in system
    assert "NowPlayingChapters.rates.map { NSNumber(value: $0) }" in system
    assert "self?.setRate(rate)" in system
    assert "commands.bookmarkCommand.addTarget" in system and "self.onBookmark?()" in system
    assert "preferredIntervals = [NSNumber(value: skip)]" in system
    # Play / pause / ±skip / scrub keep working.
    for command in (
        "playCommand",
        "pauseCommand",
        "togglePlayPauseCommand",
        "skipForwardCommand",
        "skipBackwardCommand",
        "changePlaybackPositionCommand",
    ):
        assert f"commands.{command}.addTarget" in system, command
    assert "NowPlayingChapters.scrubTarget(" in system
    assert "NowPlayingChapters.nextStart(positionMs: positionMs, chapters: book.chapters)" in audio
    assert "NowPlayingChapters.previousStart(positionMs: positionMs, chapters: book.chapters)" in audio
    model = _flat(MODEL)
    assert "player.onBookmark = { [weak self] in" in model and 'self.addBookmark(book, title: "")' in model


def test_core_decisions_have_xctests() -> None:
    choice = _flat(CHOICE)
    for name in (
        "public enum AnimeCardPlay {",
        "public enum EpisodeRowStyle:",
        "public struct EpisodeChoiceItem:",
    ):
        assert name in choice, name
    chapters = _flat(CHAPTERS)
    assert "public static let restartThresholdMs: Int64 = 3000" in chapters
    for name in ("nextStart", "previousStart", "progress", "scrubTarget", "supportedRate"):
        assert f"public static func {name}(" in chapters, name
    choice_tests = source(CHOICE_TESTS)
    for name in (
        "testPlayOpensTheNearestEpisodeNotTheList",
        "testTheCopyHereWinsAndOrderIsByNumber",
        "testNothingLeftToWatchHasNoPlay",
        "testRowsEmphasiseTheNextAndGreyTheWatched",
    ):
        assert f"func {name}()" in choice_tests, name
    chapter_tests = source(CHAPTERS_TESTS)
    for name in (
        "testNextChapterGoesToTheNextStartAndStopsAtTheLast",
        "testPreviousRestartsTheChapterAfterThreeSeconds",
        "testNowPlayingShowsTheChapterProgress",
        "testAScrubIsChapterTime",
        "testRatesAreTheAppsSpeeds",
    ):
        assert f"func {name}()" in chapter_tests, name
