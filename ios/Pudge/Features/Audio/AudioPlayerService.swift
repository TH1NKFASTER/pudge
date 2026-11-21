import Foundation
import AVFoundation
import MediaPlayer
import Combine
import UIKit
import PudgeCore

@MainActor
final class AudioPlayerService: ObservableObject {
    @Published var book: AudioManifest?
    @Published var positionMs: Int64 = 0
    @Published var playing = false
    @Published var rate: Float = 1
    @Published var message: String? { didSet { if let message { AppLog.write("player", message) } } }
    @Published var sleepRemaining: Int?
    /// Screens with their own audio controls (the reader); the mini player hides meanwhile.
    @Published var embeddedControls = 0
    let store: LocalStore
    let deviceId: String
    var onCheckpoint: ((String) -> Void)?
    /// The app's Bookmark action (PudgeModel.addBookmark), for the lock screen / Watch bookmark command.
    var onBookmark: (() -> Void)?
    private(set) var sessionId = ""
    private var sequence: Int64 = 0
    private var baseRevision: Int64 = 0
    private var player: AVPlayer?
    private var observer: Any?
    private var ended: NSObjectProtocol?
    private var statusObservation: NSKeyValueObservation?
    private var systemObservers: [NSObjectProtocol] = []
    private var partIndex = 0
    private var generation = 0
    private var seeking = false
    private var lastSaved = 0.0
    private var sleepDeadline: Double?
    private var sleepChapterEnd: Int64?
    private var resumeAfterInterruption = false
    private var handedOff = false
    private var confirmedPositionMs: Int64 = 0
    private var artwork: MPMediaItemArtwork?
    /// The cover the artwork was made of (its SHA-256: a new cover of the same book is another).
    private var artworkKey: String?
    /// The cover being decoded off the main thread (one at a time).
    private var artworkLoading: String?
    private var lastNowPlaying = 0.0
    private var lastChapterTitle: String?
    init(store: LocalStore, deviceId: String) {
        self.store = store; self.deviceId = deviceId
        configureSystem()
    }
    func play(_ book: AudioManifest, position: Int64? = nil) async throws {
        guard try store.ready(book) else { throw PudgeError.invalid("Download the whole audiobook first") }
        let start = try position ?? store.localPosition(assetId: book.assetId, editionId: book.editionId)
        stop(releaseSession: false)
        self.book = book; sessionId = UUID().uuidString.lowercased(); sequence = 0
        handedOff = false; confirmedPositionMs = min(book.durationMs, max(0, start))
        baseRevision = try store.revision(assetId: book.assetId)
        var protected: [String] = []
        do {
            for part in book.parts {
                try store.setProtection(resourceId: part.resourceId, active: true)
                protected.append(part.resourceId)
            }
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try AVAudioSession.sharedInstance().setActive(true)
            AppLog.write("player", "play \(book.title) at \(start) ms")
            playing = true
            try await seek(min(book.durationMs, max(0, start)), reason: "seek")
        } catch {
            generation += 1
            removeObservers()
            player?.pause(); player = nil; playing = false
            for resourceId in protected { try? store.setProtection(resourceId: resourceId, active: false) }
            self.book = nil; sessionId = ""
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
            throw error
        }
    }
    func seek(_ target: Int64, reason: String = "seek") async throws {
        guard let book else { return }
        reclaimSession()
        let location = try AudioTimeline(book).locate(min(book.durationMs, max(0, target)))
        generation += 1; let ticket = generation
        seeking = true
        defer { if ticket == generation { seeking = false } }
        positionMs = min(book.durationMs, max(0, target))
        if player == nil || partIndex != location.index {
            removeObservers()
            guard let copy = try store.resource(book.parts[location.index].resourceId), copy.state.verified else { throw PudgeError.invalid("Local file unavailable") }
            let item = AVPlayerItem(url: copy.url)
            player = AVPlayer(playerItem: item); partIndex = location.index
            statusObservation = item.observe(\.status, options: [.new]) { [weak self] item, _ in
                Task { @MainActor in
                    guard let self, self.player?.currentItem === item, item.status == .failed else { return }
                    self.message = item.error?.localizedDescription ?? "The audio file cannot be played"
                    self.pause(reason: "interruption")
                }
            }
            observer = player?.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 1000), queue: .main) { [weak self] time in
                Task { @MainActor in self?.tick(time) }
            }
            ended = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.player?.currentItem === item else { return }
                    await self.nextPart()
                }
            }
        }
        let part = book.parts[location.index]
        let playbackDuration = part.playbackDurationMs ?? part.durationMs
        let local = Double(location.localMs) * Double(playbackDuration) / Double(part.durationMs) / 1000
        let success = await player?.seek(to: CMTime(seconds: local, preferredTimescale: 1000), toleranceBefore: .zero, toleranceAfter: .zero) ?? false
        guard ticket == generation else { return }
        seeking = false
        guard success else { playing = false; throw PudgeError.invalid("The player did not confirm the jump") }
        capturePosition()
        guard abs(positionMs - min(book.durationMs, max(0, target))) <= 1000 else {
            player?.pause(); playing = false; throw PudgeError.invalid("The player did not confirm the exact position")
        }
        confirmedPositionMs = positionMs
        if playing { player?.playImmediately(atRate: rate) }
        save(reason); nowPlaying()
    }
    func jump(_ seconds: Int64) { Task { try? await seek(positionMs + seconds * 1000) } }
    /// Next chapter (lock screen, headphones, Watch): the next chapter's start; nothing on the last one.
    func nextChapter() {
        guard let book, let target = NowPlayingChapters.nextStart(positionMs: positionMs, chapters: book.chapters) else { return }
        Task { try? await seek(target, reason: "chapter") }
    }
    /// Previous chapter: restarts the current chapter when more than 3 s of it is played, else the previous one.
    func previousChapter() {
        guard let book else { return }
        let target = NowPlayingChapters.previousStart(positionMs: positionMs, chapters: book.chapters)
        Task { try? await seek(target, reason: "chapter") }
    }
    func pause(reason: String = "pause") {
        resumeAfterInterruption = false; player?.pause(); playing = false
        if seeking { generation += 1; player?.currentItem?.cancelPendingSeeks(); seeking = false }
        capturePosition(); save(reason); nowPlaying()
    }
    func resume() { guard player != nil else { return }; reclaimSession(); playing = true; save("periodic"); player?.playImmediately(atRate: rate); nowPlaying() }
    private func reclaimSession() {
        guard handedOff, let book else { return }
        handedOff = false; sessionId = UUID().uuidString.lowercased(); sequence = 0
        baseRevision = (try? store.revision(assetId: book.assetId)) ?? baseRevision
    }
    func pauseForHandoff(operationId: String, assetId: String, sourceSessionId: String?) throws -> AudioCheckpoint {
        if let saved = try store.handoffCheckpoint(operationId: operationId) { return saved }
        guard let book, book.assetId == assetId, sessionId == sourceSessionId, !seeking,
              player?.currentItem?.status == .readyToPlay else { throw PudgeError.invalid("The listening source changed or is not ready yet") }
        player?.pause(); playing = false; capturePosition()
        sequence += 1
        let checkpoint = try AudioCheckpoint.make(book: book, deviceId: deviceId, sessionId: sessionId,
            sequence: sequence, revision: baseRevision, positionMs: positionMs, reason: "handoff")
        try store.bindHandoff(operationId: operationId, checkpoint: checkpoint)
        handedOff = true; resumeAfterInterruption = false
        sleepDeadline = nil; sleepChapterEnd = nil; sleepRemaining = nil
        nowPlaying()
        return checkpoint
    }
    func setRate(_ rate: Float) {
        self.rate = min(3, max(0.5, rate)); if playing { player?.rate = self.rate }; nowPlaying()
    }
    func setSleep(minutes: Int?, chapterEnd: Bool = false) {
        sleepDeadline = minutes.map { ProcessInfo.processInfo.systemUptime + Double($0 * 60) }
        sleepChapterEnd = chapterEnd ? book?.chapters.first(where: { $0.endMs > positionMs })?.endMs : nil
        sleepRemaining = minutes.map { $0 * 60 }
    }
    func stop(releaseSession: Bool = true) {
        if book != nil { pause(reason: "stop") }
        generation += 1; removeObservers(); player = nil
        if let book { for part in book.parts { try? store.setProtection(resourceId: part.resourceId, active: false) } }
        sleepDeadline = nil; sleepChapterEnd = nil; sleepRemaining = nil
        book = nil; playing = false
        artwork = nil; artworkKey = nil; artworkLoading = nil; lastChapterTitle = nil
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        // Release the audio session so the Dynamic Island / lock screen player disappears.
        if releaseSession { try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation) }
    }
    private func removeObservers() {
        statusObservation = nil
        if let observer { player?.removeTimeObserver(observer) }; observer = nil
        if let ended { NotificationCenter.default.removeObserver(ended) }; ended = nil
    }
    private func capturePosition() {
        guard let player, player.currentItem?.status == .readyToPlay, player.currentTime().seconds.isFinite else {
            positionMs = confirmedPositionMs; return
        }
        updatePosition(player.currentTime()); confirmedPositionMs = positionMs
    }
    private func updatePosition(_ time: CMTime) {
        guard let book, book.parts.indices.contains(partIndex), time.seconds.isFinite else { return }
        let part = book.parts[partIndex]
        let canonical = time.seconds * Double(part.durationMs) / Double(part.playbackDurationMs ?? part.durationMs) * 1000
        positionMs = min(book.durationMs, max(part.offsetMs, part.offsetMs + Int64(canonical.rounded())))
    }
    private func tick(_ time: CMTime) {
        guard !seeking else { return }
        capturePosition()
        let now = ProcessInfo.processInfo.systemUptime
        if playing && now - lastSaved >= 5 { save("periodic") }
        if let sleepDeadline { sleepRemaining = max(0, Int(ceil(sleepDeadline - now))) }
        if playing && ((sleepDeadline.map { now >= $0 } ?? false) || (sleepChapterEnd.map { positionMs >= $0 } ?? false)) {
            pause(); self.sleepDeadline = nil; sleepChapterEnd = nil; sleepRemaining = nil
        }
        // iOS extrapolates elapsed time from the rate; refresh on chapter change
        // or every 5 s to correct drift instead of rebuilding it twice a second.
        let chapter = book?.chapters.last(where: { $0.startMs <= positionMs })?.title
        if chapter != lastChapterTitle || now - lastNowPlaying >= 5 { nowPlaying() }
    }
    func snapshot(_ reason: String) { if !seeking { capturePosition() }; save(reason) }
    func save(_ reason: String) {
        guard let book, !sessionId.isEmpty, !handedOff else { return }
        do {
            sequence += 1
            let checkpoint = try AudioCheckpoint.make(book: book, deviceId: deviceId, sessionId: sessionId,
                                                       sequence: sequence, revision: baseRevision, positionMs: positionMs, reason: reason)
            try store.enqueue(checkpoint); lastSaved = ProcessInfo.processInfo.systemUptime
            onCheckpoint?(reason)
        } catch { message = "Position not saved: " + error.localizedDescription; player?.pause(); playing = false }
    }
    private func nextPart() async {
        guard let book, playing, !handedOff else { return }
        if partIndex + 1 < book.parts.count {
            do { try await seek(book.parts[partIndex + 1].offsetMs, reason: "chapter") } catch { message = error.localizedDescription; pause() }
        } else { positionMs = book.durationMs; player?.pause(); playing = false; save("finish"); nowPlaying() }
    }
    private func nowPlaying() {
        guard let book else { return }
        lastNowPlaying = ProcessInfo.processInfo.systemUptime
        let chapter = book.chapters.last(where: { $0.startMs <= positionMs })?.title
        lastChapterTitle = chapter
        // The chapter's progress (the whole book without chapters); a scrub there is chapter time.
        let shown = NowPlayingChapters.progress(positionMs: positionMs, durationMs: book.durationMs, chapters: book.chapters)
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: book.title,
            MPMediaItemPropertyArtist: chapter ?? "Pudge",
            MPMediaItemPropertyAlbumTitle: book.seriesTitle ?? book.title,
            MPMediaItemPropertyPlaybackDuration: Double(shown.durationMs) / 1000,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: Double(shown.elapsedMs) / 1000,
            MPNowPlayingInfoPropertyPlaybackRate: playing ? Double(rate) : 0.0,
            MPNowPlayingInfoPropertyDefaultPlaybackRate: Double(rate),
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue,
            MPNowPlayingInfoPropertyChapterCount: shown.count,
        ]
        if let number = shown.number { info[MPNowPlayingInfoPropertyChapterNumber] = number - 1 }
        if let art = currentArtwork(book) { info[MPMediaItemPropertyArtwork] = art }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        // Chapter buttons only for a book with chapters; the speed shown is the app's.
        let commands = MPRemoteCommandCenter.shared()
        let hasChapters = !book.chapters.isEmpty
        commands.nextTrackCommand.isEnabled = hasChapters && NowPlayingChapters.nextStart(positionMs: positionMs, chapters: book.chapters) != nil
        commands.previousTrackCommand.isEnabled = hasChapters
        commands.changePlaybackRateCommand.isEnabled = true
    }
    /// Lock screen / Dynamic Island artwork from the downloaded cover, made once per cover (by
    /// its SHA-256: the Mac's new cover of the same book replaced nothing when kept by resource)
    /// and decoded off the main thread (a large cover stalled the player's tick). Until it is
    /// ready Now Playing shows none; then it is set again.
    private func currentArtwork(_ book: AudioManifest) -> MPMediaItemArtwork? {
        guard let cover = book.cover else { return nil }
        if artworkKey == cover.sha256, let artwork { return artwork }
        guard artworkLoading != cover.sha256 else { return nil }
        // A cover not downloaded yet is tried again on the next update, not never.
        guard let local = try? store.resource(cover.resourceId), local.state.verified, local.state.sha256 == cover.sha256 else { return nil }
        let url = local.url, key = cover.sha256, asset = book.assetId
        artworkLoading = key
        Task {
            let image = await Task.detached(priority: .utility) {
                DownsampledImage.load(url, maxPixels: AudioPlayerService.artworkPixels)
            }.value
            guard self.artworkLoading == key else { return }
            self.artworkLoading = nil
            // Another book or another cover meanwhile: this one is not shown.
            guard let image, self.book?.assetId == asset, self.book?.cover?.sha256 == key else { return }
            self.artworkKey = key
            self.artwork = Self.makeArtwork(image)
            self.nowPlaying()
        }
        return nil
    }
    nonisolated private static let artworkPixels = 1024
    /// MediaPlayer calls the artwork handler off the main thread: build it outside
    /// the main actor so the closure is not main-actor isolated.
    nonisolated private static func makeArtwork(_ image: UIImage) -> MPMediaItemArtwork {
        MPMediaItemArtwork(boundsSize: image.size) { _ in image }
    }
    private func configureSystem() {
        let commands = MPRemoteCommandCenter.shared()
        commands.playCommand.addTarget { [weak self] _ in Task { @MainActor in self?.resume() }; return .success }
        commands.pauseCommand.addTarget { [weak self] _ in Task { @MainActor in self?.pause() }; return .success }
        commands.togglePlayPauseCommand.addTarget { [weak self] _ in Task { @MainActor in guard let self else { return }; if self.playing { self.pause() } else { self.resume() } }; return .success }
        // ±skip as the app's buttons (lock screen / Dynamic Island / Watch show these).
        let skip = NowPlayingChapters.skipSeconds
        commands.skipForwardCommand.preferredIntervals = [NSNumber(value: skip)]
        commands.skipBackwardCommand.preferredIntervals = [NSNumber(value: skip)]
        // Next / previous track are chapter jumps (headphones' double / triple press, Watch, CarPlay).
        commands.nextTrackCommand.isEnabled = false; commands.previousTrackCommand.isEnabled = false
        commands.nextTrackCommand.addTarget { [weak self] _ in Task { @MainActor in self?.nextChapter() }; return .success }
        commands.previousTrackCommand.addTarget { [weak self] _ in Task { @MainActor in self?.previousChapter() }; return .success }
        // The app's speeds (its speed menus); a change here is the app's speed (setRate), and back.
        commands.changePlaybackRateCommand.supportedPlaybackRates = NowPlayingChapters.rates.map { NSNumber(value: $0) }
        commands.changePlaybackRateCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackRateCommandEvent else { return .commandFailed }
            let rate = NowPlayingChapters.supportedRate(event.playbackRate)
            Task { @MainActor in self?.setRate(rate) }; return .success
        }
        // Bookmark: the player's Bookmark (PudgeModel.addBookmark through onBookmark).
        commands.bookmarkCommand.isEnabled = true
        commands.bookmarkCommand.localizedTitle = "Bookmark"
        commands.bookmarkCommand.addTarget { [weak self] _ in
            Task { @MainActor in guard let self, self.book != nil else { return }; self.onBookmark?() }; return .success
        }
        commands.skipForwardCommand.addTarget { [weak self] _ in Task { @MainActor in self?.jump(skip) }; return .success }
        commands.skipBackwardCommand.addTarget { [weak self] _ in Task { @MainActor in self?.jump(-skip) }; return .success }
        commands.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            let seconds = event.positionTime
            Task { @MainActor in
                // Now Playing shows the chapter: the scrub is a place in it.
                guard let self, let book = self.book else { return }
                let target = NowPlayingChapters.scrubTarget(seconds: seconds, positionMs: self.positionMs,
                                                            durationMs: book.durationMs, chapters: book.chapters)
                try? await self.seek(target)
            }
            return .success
        }
        systemObservers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            Task { @MainActor in
                guard let self, let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                      let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
                if type == .began { let wasPlaying = self.playing; self.pause(reason: "interruption"); self.resumeAfterInterruption = wasPlaying }
                else {
                    let options = AVAudioSession.InterruptionOptions(rawValue: note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
                    if self.resumeAfterInterruption && options.contains(.shouldResume) { try? AVAudioSession.sharedInstance().setActive(true); self.resume() }
                    self.resumeAfterInterruption = false
                }
            }
        })
        systemObservers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] note in
            Task { @MainActor in if note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue { self?.pause(reason: "interruption") } }
        })
    }
}
