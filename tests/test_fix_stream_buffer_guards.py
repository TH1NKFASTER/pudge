"""The position bar shows what is already here; play while downloading (Swift side).

policy: the app cannot be built here (Xcode on the Mac compiles it and runs PudgeCore's
PlaybackBufferTests: the bar's ranges, the download's part read by the player). These guards
tie the player, the stream loader and the download to that logic.
"""

from __future__ import annotations

from source_policy import source

CORE = "ios/PudgeCore/Sources/PudgeCore/PlaybackBuffer.swift"
CACHE = "ios/PudgeCore/Sources/PudgeCore/StreamCache.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PlaybackBufferTests.swift"
STREAM = "ios/Pudge/Networking/MacVideoStream.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"


def _between(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


def test_the_position_bar_draws_what_is_already_here() -> None:
    watch = source(WATCH)
    controls = _between(watch, "private var controls: some View {", "/// Previous/next subtitle line")
    assert "SeekBar(timeMs: video.timeMs, durationMs: video.durationMs, buffered: video.buffered," in controls
    assert "Slider(" not in controls
    bar = _between(watch, "struct SeekBar: View {", "private struct ChromeIdle")
    assert "ForEach(Array(buffered.enumerated())" in bar
    assert "accessibilityAdjustableAction" in bar
    update = _between(watch, "private func updateBuffered() {", "func toggle()")
    assert "item.loadedTimeRanges" in update and "BufferedBar.fromTimes(" in update
    assert "BufferedBar.fromBytes(stream.availableBytes(), fileSize: stream.video.sizeBytes)" in update
    assert "self.updateBuffered()" in watch
    assert "func availableBytes() -> [ClosedRange<Int64>]" in source(STREAM)


def test_the_player_reads_the_downloaded_part_before_the_cache_and_the_mac() -> None:
    cache = source(CACHE)
    block = _between(cache, "public func block(_ index: Int) async throws -> Data {", "public func warm(")
    assert block.index("local?.read(key, range)") < block.index("cache?.data(key, index, length: length)")
    assert "|| self.downloaded(index)" in cache
    assert "local: PartialFileShare.shared" in source(STREAM)


def test_the_download_tells_the_player_what_is_written_and_when_it_starts_over() -> None:
    media = source(MEDIA)
    delegate = _between(media, "private final class RangedFileDelegate", "struct VideoJob")
    restart = _between(delegate, "case .restart:", "case .complete:")
    assert restart.index("PartialFileShare.shared.reset(key, url: target)") < restart.index("createFile(")
    assert "PartialFileShare.shared.advance(key, url: target, to: written)" in delegate
    assert "key: sha256, report:" in media
    finish = _between(
        media, "try FileManager.default.moveItem(at: partial, to: target)", "Self.removePartial(partial)"
    )
    assert "PartialFileShare.shared.moved(sha, to: target, sizeBytes: size)" in finish
    assert "PartialFileShare.shared.forget(url: url)" in media
    # Watching while it downloads asks the Mac for the copy being downloaded.
    assert "downloadingCopies[episode.id]?.quality ?? videoQuality(nil, key: VideoQuality.streamKey)" in media


def test_play_is_offered_while_downloading_and_download_and_play_now() -> None:
    watch = source(WATCH)
    assert "func downloadAndWatch(_ item: EpisodeItem, media: MediaModel" in watch
    assert watch.count("media.downloadingCopies[item.id] != nil") >= 3
    # "Download and play now" was removed (user: not needed); play-while-downloading stays.
    assert "launcher.downloadAndWatch(item, media: media)" not in watch


def test_the_core_logic_has_its_xctests() -> None:
    tests = source(CORE_TESTS)
    for name in (
        "testPartsAreJoinedSortedAndKeptOnTheBar",
        "testByteRangesMapToTheBarInProportion",
        "testOnlyBytesTheDownloadWroteAreRead",
        "testAFileStartingOverOrGoneIsNotRead",
        "testThePlayerTakesTheDownloadedPartAndOnlyTheRestFromTheMac",
    ):
        assert f"func {name}()" in tests
    assert "public final class PartialFileShare" in source(CORE)
