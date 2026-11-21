"""iPhone/iPad: one copy per episode, the subtitle timing wheel, Skip opening/ending, the
"Episodes on iPad" row.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The rules are
PudgeCore code with XCTest (EpisodePlaybackTests: EpisodeCopies, SubtitleDelays, SkipSegments);
the Mac side is tested in test_fix_iphone_duplicate_episode_timing_skip.py. What only the
SwiftUI wiring does is guarded here by reading the Swift source:

1. The anime lists keep one item per episode number, and Download of a second copy of an
   episode already here or downloading does nothing (both copies were downloaded).
2. The timing wheel is a small panel at the top right of the player (the video plays on behind
   it, a tap outside closes it), not a sheet. It has its own state and takes the store object, not a binding to the
   player's value: every player redraw set the turning wheel back (it snapped back to 0).
   The timing is kept per episode and sent to the Mac.
3. The player shows the Skip button from the Mac's segments, kept with a download for offline.
4. "Episodes on iPad · 3.2 GB" sits next to its label and opens the downloaded episodes.
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/EpisodePlaybackTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def test_one_copy_per_episode_and_no_second_download() -> None:
    watch = _flat(WATCH)
    groups = _flat(WATCH, "var groups: [AnimeGroup] {", "var body: some View {")
    assert "result[index].episodes = media.oneCopyEach(result[index].episodes)" in groups
    detail = _flat(WATCH, "struct AnimeDetailScreen: View {", "var body: some View {")
    assert "merged.episodes = media.oneCopyEach(merged.episodes)" in detail
    assert "EpisodeCopies.unique(items) {" in watch
    download = _flat(
        MEDIA,
        "func download(_ episode: EpisodeItem, quality chosen: VideoQuality? = nil) async {",
        "videoCancelled.remove(episode.id)",
    )
    assert "if let other = otherCopy(of: episode) {" in download and "return" in download
    after = _flat(
        MEDIA,
        "func download(_ episode: EpisodeItem, quality chosen: VideoQuality? = nil) async {",
        "let background",
    )
    assert after.index("otherCopy(of: episode)") < after.index("videoJobItems[episode.id] = episode")
    assert "defer { videoJobs[episode.id] = nil videoJobItems[episode.id] = nil" in _flat(MEDIA)
    all_ = _flat(MEDIA, "func downloadAll(_ items: [EpisodeItem]) {", "guard !pending.isEmpty")
    assert "EpisodeCopies.unique(items)" in all_ and "otherCopy(of: item) == nil" in all_


def test_timing_wheel_has_its_own_state_and_is_kept_per_episode() -> None:
    watch = source(WATCH)
    assert '@AppStorage("pudge.subtitleDelayMs")' not in watch
    sheet = _flat(WATCH, "struct SubtitleTimingSheet: View {", "/// Jiten reviews")
    assert "@ObservedObject var delays: SubtitleDelayStore" in sheet
    assert "@State private var tenths: Int" in sheet
    # A UIKit wheel set from outside only on a real change (CompactWheel, WheelSync): SwiftUI's
    # wheel picker was set back mid-flick (tests/test_fix_ios_player_handles_wheels_size_watched.py).
    assert "CompactWheel(values: SubtitleDelayWheel.values, value: $tenths," in sheet
    assert (
        ".onChange(of: tenths) { _, value in delays.set(SubtitleDelayWheel.ms(fromTenths: value), for: episodeId) }"
        in sheet
    )
    assert "Binding(get:" not in sheet
    # A small panel at the top right (no sheet over the video); a tap outside closes it.
    assert ".sheet(isPresented: $timing)" not in watch
    assert "NavigationStack" not in sheet and ".frame(width: 130, height: 120)" in sheet
    body = _flat(WATCH, "var body: some View {\n        ZStack {", ".statusBarHidden(")
    # One panel at a time, timing or text size (IOS4, tests/test_fix_ios4_phone_screens.py).
    assert "if let panel = wheelPanel { wheelPanelView(panel) }" in body
    panel = _flat(
        WATCH,
        "private func wheelPanelView(_ panel: PlayerWheelPanel) -> some View {",
        "// MARK: Skip opening / ending",
    )
    assert "ZStack(alignment: .topTrailing)" in panel
    assert ".onTapGesture { withAnimation(.easeOut(duration: 0.15)) { wheelPanel = nil } }" in panel
    player = _flat(WATCH, "struct VideoPlayerScreen: View {", "var body: some View {")
    assert "private var delayMs: Int { delays.ms(episode.id) }" in player
    media = _flat(MEDIA)
    assert "await sendSubtitleDelays(client)" in media
    assert (
        'client.media("episodes/\\(id)/subtitle-delay", post: SubtitleDelayRequest(ms: ms), as: OkResponse.self)'
        in media
    )
    assert "subtitleDelays.adopt(mac: item.subtitleDelayMs, for: item.id)" in media
    for name in (
        "testTimingIsKeptPerEpisodeAndTheMacValueOnlyWhenNothingWaits",
        "testOneItemPerEpisodeNumberKeepsTheDownloadedCopy",
    ):
        assert f"func {name}()" in source(CORE_TESTS)


def test_skip_button_from_the_macs_segments_offline_too() -> None:
    body = _flat(WATCH, "var body: some View {\n        ZStack {", ".statusBarHidden(")
    assert "if let segment = skipSegment { skipButton(segment) }" in body
    player = _flat(WATCH, "// MARK: Skip opening / ending", "private func resume()")
    assert "media.localEpisodes[episode.id]?.skipSegments" in player
    assert (
        "SkipSegments.button(segments, atMs: video.timeMs, durationMs: video.durationMs, controlsShown: chrome)"
        in player
    )
    assert "video.seek(SkipSegments.targetMs(segment))" in player
    assert '"Skip opening" : "Skip ending"' in player
    assert "local.skipSegments = episode.skipSegments" in _flat(MEDIA)
    assert "func testSkipButtonLikeMpv()" in source(CORE_TESTS)


def test_episodes_on_device_row_opens_the_downloaded_episodes() -> None:
    ready = _flat(WATCH, "@ViewBuilder private var ready: some View {", "@ViewBuilder private var planning")
    assert 'LabeledContent("Episodes on' not in ready
    # A route in the tab's path (IOS3: tapping the Anime tab again goes back from it too).
    assert "Button { path.append(DownloadedEpisodesRoute()) } label:" in ready
    assert (
        ".navigationDestination(for: DownloadedEpisodesRoute.self) { _ in DownloadedEpisodesScreen(media: media) }"
        in (_flat(WATCH))
    )
    assert 'Text("Episodes on \\(PudgeTheme.device) · \\(PudgeTheme.size(media.videoBytes))")' in ready
