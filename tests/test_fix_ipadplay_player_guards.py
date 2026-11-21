"""iPad: a downloaded episode played sound, the clock stayed at 0:00 without a picture or
subtitles, Close did nothing and the sound went on after Pudge was swiped away (reboot).

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (PlaybackHealthTests: the watchdog, the main-thread stall, the download
check, the H.264 level; RangedResumeTests: a resumed answer for other bytes starts over). The
AVFoundation / SwiftUI wiring is guarded here by reading the Swift source:

1. [player] lines in the diagnostics: open, item status, tracks, picture size, errors, stalls,
   the first video frame, time control, close requested / finished, a main thread that stops
   answering (logged from the background).
2. The watchdog stops a downloaded episode that does not move or shows no picture after 8 s
   ("This episode can't be played on this device: …", Re-download / Close).
3. Close never waits: dismissal, pause, no item, the audio session released off the main thread
   (notifyOthersOnDeactivation), Now Playing cleared; the session is also activated off it.
4. A finished download is checked (size + SHA-256, once more from 0 for other bytes; then
   AVFoundation: playable, video + audio, decodable, duration) before it is listed.
5. The subtitles are read before playing, independent of the clock.
"""

from __future__ import annotations

import re

from source_policy import source

WATCH = "ios/Pudge/UI/WatchScreen.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
CORE = "ios/PudgeCore/Sources/PudgeCore/PlaybackHealth.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/PlaybackHealthTests.swift"
RESUME = "ios/PudgeCore/Sources/PudgeCore/RangedResume.swift"
RESUME_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/RangedResumeTests.swift"


def _flat(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))] if end else text[at:]
    return re.sub(r"\s+", " ", text)


def _controller() -> str:
    return _flat(WATCH, "final class VideoController: ObservableObject {", "enum PlayerOrientationGate {")


def _player() -> str:
    return _flat(WATCH, "struct VideoPlayerScreen: View {", "struct SeekBar: View {")


def test_the_player_writes_what_it_does_into_the_diagnostics() -> None:
    controller = _controller()
    begin = controller[controller.index("func begin(episodeId: Int, local: Bool") :]
    assert 'AppLog.write("player", "open \\(label): \\(bytes), \\(expectedMs / 1000) s expected' in begin
    for needle in (
        "AVPlayerItem.failedToPlayToEndTimeNotification",
        "AVPlayerItem.newErrorLogEntryNotification",
        "errorLog()?.events.last",
        "AVPlayerItem.playbackStalledNotification",
        "item.observe(\\.presentationSize",
        "item.observe(\\.status",
        "player.observe(\\.timeControlStatus",
        "item.asset.load(.tracks)",
        '"close requested (\\(label)) at \\(timeMs) ms"',
        '"close finished in %.0f ms (%@)"',
        '"first video frame after %.1f s (%@)"',
    ):
        assert needle in controller, needle
    # Every item (also a stream opened again) is logged.
    assert "watchEnd(current) logItem(current)" in controller
    assert "watchEnd(item) logItem(item)" in controller
    # The first frame: the layer's isReadyForDisplay.
    surface = _flat(WATCH, "struct VideoSurface: UIViewRepresentable {", "/// A subtitle line with words")
    assert "view.playerLayer.observe(\\.isReadyForDisplay, options: [.initial, .new])" in surface
    assert "VideoSurface(player: video.player, onFirstFrame: { video.firstFrame() })" in _player()
    # A frozen main thread still leaves a line (written from a background timer).
    monitor = _flat(
        WATCH, "final class MainThreadMonitor: @unchecked Sendable {", "final class VideoController"
    )
    assert "DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))" in monitor
    assert "MainThreadStall.shouldLog(lastAnswer: last, now: now, logged: logged)" in monitor
    assert "DispatchQueue.main.async { [weak self] in self?.answered() }" in monitor
    assert "let watch = MainThreadMonitor(label: label) watch.start()" in begin
    assert "monitor?.stop(); monitor = nil" in controller


def test_a_downloaded_episode_that_does_not_play_stops_with_its_reason() -> None:
    controller = _controller()
    watchdog = controller[controller.index("private func runWatchdog(stops: Bool) async {") :]
    watchdog = watchdog[: watchdog.index("/// Item status, tracks")]
    assert "switch PlaybackWatchdog.judge(sample) {" in watchdog
    assert "guard UIApplication.shared.applicationState == .active else { continue }" in watchdog
    assert "if player.timeControlStatus != .paused { wanted += 1 }" in watchdog
    assert "firstFrameShown: firstFrameAt != nil" in watchdog
    assert "guard stops else { return }" in watchdog
    assert "player.pause(); playing = false" in watchdog
    assert (
        "unplayable = PlaybackWatchdog.message(problem, detail: detail, device: PudgeTheme.device)"
        in watchdog
    )
    # Only a downloaded file stops; from the Mac its own messages do.
    assert "watchdog = Task { [weak self] in await self?.runWatchdog(stops: local) }" in controller
    player = _player()
    assert "video.begin(episodeId: episode.id, local: stream == nil, fileBytes: fileBytes" in player
    assert "if let problem = video.unplayable { unplayableView(problem) }" in player
    view = player[player.index("private func unplayableView(_ text: String) -> some View {") :]
    view = view[: view.index("/// `quiet`: the automatic reconnect")]
    assert 'Button("Re-download") {' in view and "media.redownload(id)" in view
    assert 'Button("Close") { closePlayer() }' in view
    redownload = _flat(MEDIA, "    func redownload(_ id: Int) {", "/// Downloads episodes one after another")
    assert "deleteEpisode(id) Task { await download(item) }" in redownload
    # Without the Mac's list the file stays (nothing to download it from).
    assert redownload.index(
        "guard let item = episodes.first(where: { $0.id == id }) else {"
    ) < redownload.index("deleteEpisode(id)")
    core = _flat(CORE)
    assert "public static let grace: Double = 8" in core
    assert "This episode can't be played on this \\(device): \\(reason)." in core
    tests = source(CORE_TESTS)
    for name in (
        "func testTheClockAtTheStartAfterTheGraceStopsTheEpisode()",
        "func testTimeMovingWithoutAPictureStopsTheEpisode()",
        "func testAMainThreadStallIsLoggedOncePerStall()",
    ):
        assert name in tests, name


def test_close_never_waits_and_releases_the_sound() -> None:
    controller = _controller()
    init = controller[
        controller.index("private init(item: AVPlayerItem?, stream: MacVideoStream?, startMs: Int64) {") :
    ]
    init = init[: init.index("observer = player.addPeriodicTimeObserver")]
    # The audio session is never set on the main thread.
    assert "AVAudioSession" not in init and "PlayerAudioSession.activate()" in init
    session = _flat(WATCH, "enum PlayerAudioSession {", "/// While the player is open")
    assert 'DispatchQueue(label: "pudge.player-audio-session"' in session
    assert session.count("queue.async {") == 2
    assert "setActive(false, options: .notifyOthersOnDeactivation)" in session
    close = controller[controller.index("func close(releaseAudio: Bool = true) {") :]
    close = close[: close.index("// MARK: Log and watchdog")]
    order = [
        "guard !closed else { return }",
        "watchdog?.cancel()",
        "player.pause()",
        "stream?.close()",
        "player.replaceCurrentItem(with: nil)",
        "MPNowPlayingInfoCenter.default().nowPlayingInfo = nil",
        "PlayerAudioSession.deactivate()",
    ]
    assert [close.index(step) for step in order] == sorted(close.index(step) for step in order)
    assert "await" not in close and ".sync" not in close
    player = _player()
    # Close: dismissal first, then the pause; onDisappear stops the sound before saving.
    closing = player[player.index("private func closePlayer() {") :]
    closing = closing[: closing.index("/// A downloaded episode that does not play here")]
    assert closing.index("dismiss()") < closing.index("video.pause()")
    assert 'Button { closePlayer() } label: { Image(systemName: "xmark")' in player
    assert 'Button("Close") { dismiss() }' not in player
    gone = player[player.index(".onDisappear {") :]
    assert gone.index("video.close(releaseAudio: media.app.player.book == nil)") < gone.index(
        "media.saveVideoPosition(episode.id, positionMs: video.timeMs)"
    )
    assert gone.count("video.close(") == 1
    # The layer lets the player go with the view.
    surface = _flat(WATCH, "struct VideoSurface: UIViewRepresentable {", "/// A subtitle line with words")
    assert "static func dismantleUIView(_ view: PlayerLayerView, coordinator: ()) {" in surface
    assert "view.playerLayer.player = nil" in surface


def test_a_finished_download_is_checked_before_it_is_listed() -> None:
    download = _flat(
        MEDIA,
        "    func download(_ episode: EpisodeItem, quality chosen: VideoQuality? = nil) async {",
        "/// Asks the Mac to prepare the episode",
    )
    verify = download.index("try FileIntegrity.verify(partial, sha256: sha, sizeBytes: size)")
    again = download.index(
        "guard DownloadCheck.next(after: .integrity, attempt: 1) == .redownload else { throw integrity }"
    )
    moved = download.index("try FileManager.default.moveItem(at: partial, to: target)")
    probe = download.index("let (probe, format) = await VideoFileProbe.probe(target)")
    judged = download.index(
        "if case .bad(let reason) = DownloadCheck.judge(probe, expectedSeconds: expectedSeconds) {"
    )
    listed = download.index(
        "try JSONEncoder().encode(local).write(to: metaURL(episode.id), options: .atomic)"
    )
    assert verify < again < moved < probe < judged < listed
    # Other bytes: once more from 0 and verified again.
    retry = download[again:moved]
    assert "Self.removePartial(partial)" in retry and retry.count("FileIntegrity.verify(") == 1
    bad = download[judged : download.index('AppLog.write("video", "episode \\(episode.id) checked:')]
    assert "try? FileManager.default.removeItem(at: target)" in bad
    assert (
        'throw PudgeError.invalid("This episode can\'t be played on this \\(PudgeTheme.device): \\(reason).")'
        in bad
    )
    probe_code = _flat(MEDIA, "enum VideoFileProbe {", "/// A resumed download answered")
    for needle in (
        "asset.load(.isPlayable, .duration)",
        "asset.loadTracks(withMediaType: .video)",
        "asset.loadTracks(withMediaType: .audio)",
        "track.load(.isDecodable)",
        "kCMFormatDescriptionExtension_SampleDescriptionExtensionAtoms",
        "VideoFormatInfo.avcProfileLevel(",
    ):
        assert needle in probe_code, needle
    tests = source(CORE_TESTS)
    assert "func testADownloadThatDoesNotPlayIsRefused()" in tests
    assert "func testOnlyOtherBytesAreDownloadedOnceMore()" in tests


def test_a_resumed_answer_for_other_bytes_starts_from_zero() -> None:
    core = _flat(RESUME)
    assert "case startOver" in core
    assert "range.end == total - 1 else { return .startOver }" in core
    delegate = _flat(MEDIA, "private final class RangedFileDelegate", "struct VideoJob: Equatable {")
    start_over = delegate[delegate.index("case .startOver:") :]
    start_over = start_over[: start_over.index("}")]
    assert "failure = RangeMismatch(" in start_over and "completionHandler(.cancel); return" in start_over
    assert "handle" not in start_over  # nothing written
    transfer = _flat(MEDIA, "    private func transfer(", "    private func rangedAttempt(")
    caught = transfer[transfer.index("} catch let mismatch as RangeMismatch {") :]
    caught = caught[: caught.index("} catch {")]
    assert "guard RangedResume.startsOver(alreadyRestarted: restarted), !cancelled() else {" in caught
    assert "PartialFileShare.shared.reset(sha256, url: target)" in caught
    assert "try? FileManager.default.removeItem(at: target)" in caught and "offset = 0" in caught
    assert "throw MacRefusedFile(status: mismatch.status)" in caught
    tests = _flat(RESUME_TESTS)
    assert (
        'decide(status: 206, contentRange: "bytes 0-999/1000", offset: 600, total: 1_000), .startOver)'
        in tests
    )


def test_subtitles_are_read_before_playing_whatever_the_clock() -> None:
    player = _player()
    appear = player[player.index(".onAppear {") :]
    # Read off the main thread (MBs of JSON), still before the player begins and plays.
    track = appear.index(
        "if let streamed = stream?.track { track = streamed } else { track = await media.subtitles(episode.id) }"
    )
    begin = appear.index("video.begin(episodeId: episode.id")
    play = appear.index("video.player.play()")
    assert track < begin < play
    assert "subtitles: track?.cues.count)" in appear
    # The line on screen follows the player's time only.
    assert "private var cueIndex: Int? { track?.cue(atMs: video.timeMs - Int64(delayMs)) }" in player
