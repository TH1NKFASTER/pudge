"""Watching an episode from the Mac on the iPhone/iPad (Swift side; built only in Xcode on the Mac).

policy: the app cannot be built here (Xcode on the Mac compiles it and runs PudgeCore's
VideoStreamTests: range requests, the Mac's answers, chunk planning). These guards tie the phone's
stream requests to the Mac's routes (tests/test_fix_iphone_video_stream.py runs those) and keep the
Xcode project complete.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from pudge.database import Database
from source_policy import source
from test_fix_iphone_video_stream import _Log, _prepared


@pytest.fixture
def mac(tmp_path):
    from types import SimpleNamespace

    from pudge.iphone_service import IPhoneService

    value = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    value.api = SimpleNamespace(logger=_Log())
    return value


STREAM_SWIFT = "ios/Pudge/Networking/MacVideoStream.swift"
CORE_STREAM = "ios/PudgeCore/Sources/PudgeCore/VideoStream.swift"
MEDIA_SWIFT = "ios/Pudge/Features/Media/MediaModel.swift"
WATCH_SWIFT = "ios/Pudge/UI/WatchScreen.swift"


def test_the_phones_stream_requests_are_routes_of_the_mac(mac, tmp_path, monkeypatch) -> None:
    swift = source(STREAM_SWIFT) + source(MEDIA_SWIFT)
    routes = set(
        re.findall(r'media\("((?:episodes|streams)/\\\(\w+(?:\.id)?\)/(?:stream|keepalive|close))"', swift)
    )
    assert routes == {
        r"episodes/\(episode.id)/stream",
        r"episodes/\(episodeId)/stream",
        r"streams/\(current.id)/close",
        r"streams/\(current.id)/keepalive",
        r"streams/\(fresh.id)/close",
        r"streams/\(lease.id)/close",
    }
    episode, _state = _prepared(mac, tmp_path, monkeypatch, b"m" * 64)
    media = mac.media
    opened = media.handle("POST", f"/api/v2/media/episodes/{episode}/stream", {}, dict, device="ipad")
    stream = opened["stream"]["id"]
    # Renewed first, then closed (closing twice changes nothing).
    for route in sorted(routes, key=lambda value: (not value.endswith("/keepalive"), value)):
        path = re.sub(r"\\\(episode(?:\.id|Id)\)", str(episode), route)
        path = re.sub(r"\\\(\w+\.id\)", stream, path)
        if path.endswith("/stream"):
            continue
        assert media.handle("POST", "/api/v2/media/" + path, {}, dict, device="ipad")["ok"] is True
    core = source(CORE_STREAM)
    assert '"/api/v2/resources/\\(resourceId)/content?sha256=\\(sha256)&stream=\\(stream ?? "play")"' in core
    loader = source(STREAM_SWIFT)
    # AVPlayer's requests go through the paired connection: pinned certificate, device token.
    assert "PinnedDelegate(pin: connection.delegate.pin, origin: connection.delegate.origin)" in loader
    assert "try connection.request(VideoStreamAddress.route(" in loader
    assert 'request.setValue(VideoStreamRanges.header(range), forHTTPHeaderField: "Range")' in loader
    assert "info.isByteRangeAccessSupported = true" in loader
    # Both buttons for an episode not on the device; the player lets the Mac's copy go.
    watch = source(WATCH_SWIFT)
    assert '"Watch "' in watch and 'Label("Download", systemImage: "arrow.down.circle")' in watch
    assert "stream?.close()" in watch[watch.index("func close(releaseAudio: Bool = true) {") :]
    assert "if stream != nil { media.endStream(episode.id) }" in watch
    assert "func testChunksStartSmallThenCoverTheRangeExactly()" in source(
        "ios/PudgeCore/Tests/PudgeCoreTests/VideoStreamTests.swift"
    )


def test_the_xcode_project_lists_the_stream_loader() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/check_ios_project.py")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert '"path" = "Pudge/Networking/MacVideoStream.swift";' in source(
        "ios/Pudge.xcodeproj/project.pbxproj"
    )


def _block(text: str, start: str) -> str:
    """From `start` to the end of its braces."""
    at = text.index(start)
    depth, index = 0, text.index("{", at)
    for index in range(index, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[at : index + 1]
    raise AssertionError(start)


def test_closing_mid_stream_never_uses_an_invalidated_session() -> None:
    loader = source(STREAM_SWIFT)
    # Invalidated only once nothing can start a request any more; close() cancels requests.
    assert loader.count("session.invalidateAndCancel()") == 1
    assert "session.invalidateAndCancel()" in _block(loader, "deinit {")
    assert "session.getAllTasks { $0.forEach { $0.cancel() } }" in _block(loader, "func close() {")
    fetch = _block(loader, "private func fetchOnce(")
    assert fetch.index("try Task.checkCancellation()") < fetch.index("session.data(for: request)")
    assert "if stopped { throw CancellationError() }" in fetch
    # Only a cancelled task is a seek or a closed player; any other cancel fails the request.
    assert "VideoStreamFailure.from(error: error, cancelled: Task.isCancelled)" in _block(
        loader, "private func serve("
    )
    # A fresh stream for another copy is closed, not left open on the Mac.
    renew = _block(loader, "private func renew() async {")
    assert "guard sameCopy, !closed else { return true }" in renew
    assert 'connection.media("streams/\\(fresh.id)/close"' in renew


def test_one_player_at_a_time_and_a_cancelled_preparation_keeps_its_row() -> None:
    watch = source(WATCH_SWIFT)
    busy = _block(watch, "func busy(_ media: MediaModel) -> Bool {")
    for part in (
        "checking != nil",
        "!media.streamJobs.isEmpty",
        "playing != nil",
        "streaming != nil",
        "gating != nil",
    ):
        assert part in busy
    assert "guard !busy(media) else { return }" in _block(watch, "func start(")
    assert "guard !busy(media) else { return }" in _block(watch, "func watch(")
    late = _block(watch, "private func streamNow(")
    assert late.index("playback.stream.close()") < late.index("streaming = playback")
    assert "guard observer != nil else { fresh.close(); return }" in _block(
        watch, "func resume(with fresh: MacVideoStream) {"
    )
    cancel = _block(source(MEDIA_SWIFT), "func cancelStream(_ id: Int) {")
    assert 'streamJobs[id] = VideoJob(phase: "Cancelling…", fraction: 0)' in cancel
    assert "streamJobs[id] = nil" not in cancel


def test_the_players_gestures_zoom_the_picture_only() -> None:
    watch = source(WATCH_SWIFT)
    body = _block(watch, "var body: some View {\n        ZStack {")
    # The picture zooms; the subtitles (later in the ZStack) are not scaled.
    surface = body[body.index("VideoSurface(player: video.player, onFirstFrame:") :]
    # The gestures are a UIKit layer now (tests/test_fix_ios_panel_and_video_gestures.py).
    assert surface.index(".scaleEffect(zoom.scale)") < surface.index("VideoGestureLayer(")
    assert body.count(".scaleEffect(") == 1
    assert body.index("VideoGestureLayer(") < body.index("subtitle(cue, index: index)")
    # The wheels are UIKit pickers now (CompactWheel; tests/test_fix_ios_player_handles_wheels_size_watched.py).
    assert "UIPinchGestureRecognizer(" in watch and "let picker = UIPickerView()" in watch
    # Timing and text size in separate panels since IOS4 (tests/test_fix_ios4_phone_screens.py).
    assert "case .timing: SubtitleTimingSheet(delays: delays, episodeId: episode.id)" in watch
    assert 'Button("Later +0.5 s")' not in watch
    for name in (
        "testDoubleTapsSeekOrPauseAndNeverToggleTheControls",
        "testFurtherTapsAddTenSecondsEach",
        "testPinchZoomsAroundTheFingersAndSnapsBackBelowFit",
        "testTheTimingWheel",
    ):
        assert f"func {name}()" in source("ios/PudgeCore/Tests/PudgeCoreTests/PlayerGesturesTests.swift")


# ------------------------------------------------- watching like a video site (block cache)
CORE_CACHE = "ios/PudgeCore/Sources/PudgeCore/StreamCache.swift"
SETTINGS_SWIFT = "ios/Pudge/UI/SettingsScreen.swift"


def test_the_player_reads_through_the_block_cache_and_seeks_fetch_at_the_new_place() -> None:
    text = source(STREAM_SWIFT)
    assert "StreamBlockSource(key: video.sha256" in text
    # A request first moves the read-ahead to where it starts (a seek fetches there at once and
    # stops fetches of the old place), then takes its blocks from the cache or the Mac.
    deliver = text[text.index("private func deliver(") :]
    assert deliver.index("source.moved(to: wanted.lowerBound)") < deliver.index(
        "try await source.block(index)"
    )
    assert "source.warm(" in deliver
    # The minutes ahead go to the cache once the player exists; closing stops every fetch.
    assert "blocks?.startReadAhead()" in text
    close = text[text.index("func close()") : text.index("// MARK: AVAssetResourceLoaderDelegate")]
    assert "source?.stop()" in close
    # didCancel still cancels the request's task (its blocks on their way go on only while the
    # read-ahead still wants them).
    assert "didCancel loadingRequest" in text and "task?.cancel()" in text
    # The cache is bounded (Settings, free space) and lives in Caches, keyed by the copy's SHA.
    assert ".cachesDirectory" in text and "StreamCacheSetting.limitBytes(" in text
    core = source(CORE_CACHE)
    for name in (
        "final class StreamBlockCache",
        "final class StreamBlockSource",
        "enum StreamPrefetch",
        "func trim()",
    ):
        assert name in core, name
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/StreamCacheTests.swift")
    for name in (
        "testTheLeastRecentlyUsedBlocksGoPastTheLimit",
        "testASeekStopsFetchesOfTheOldPlaceAndFetchesTheNewOneAtOnce",
        "testABlockWantedTwiceIsFetchedOnceAndCachedBlocksAreNotFetched",
        "testTheReadAheadFillsTheCacheAheadOfThePlayerOnly",
    ):
        assert name in tests, name


def test_settings_offer_the_watch_cache_and_the_qualities() -> None:
    text = source(SETTINGS_SWIFT)
    for label in ('"Watch cache"', '"Clear watch cache"', '"Download quality"', '"Stream quality"'):
        assert label in text, label
    media = source(MEDIA_SWIFT)
    # The copy asked for: in the prepare/stream body and the state polled; the original's
    # request is the empty body of before (older Macs).
    assert media.count("VideoQualityRequest(quality)") >= 2
    assert '"?quality=\\(quality.rawValue)"' in media
    assert 'app.macSupports("video-quality")' in media
    assert "local.quality = " in media
    watch = source(WATCH_SWIFT)
    assert "DownloadQualityChoices(media: media, item: item)" in watch
    assert "media.download(item, quality: quality)" in watch
