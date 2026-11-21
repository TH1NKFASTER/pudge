import Foundation

/// What the player's watchdog sees, about once a second while the player is open and the app is
/// in front (a video in the background has no picture on purpose).
public struct PlaybackSample: Equatable, Sendable {
    /// Seconds the player was asked to play (playing or waiting to play; never while paused by
    /// the user or in the background) since the player opened.
    public var wantedSeconds: Double
    /// How far the clock moved in that time (seconds, absolute).
    public var advancedSeconds: Double
    /// The item has a video track (an audio-only file never shows a picture).
    public var hasVideoTrack: Bool
    /// AVPlayerItem.presentationSize.
    public var presentationWidth: Double
    public var presentationHeight: Double
    /// AVPlayerLayer.isReadyForDisplay became true (the first video frame is there).
    public var firstFrameShown: Bool
    /// AVPlayerItem.status == .failed.
    public var failed: Bool

    public init(wantedSeconds: Double, advancedSeconds: Double, hasVideoTrack: Bool, presentationWidth: Double,
                presentationHeight: Double, firstFrameShown: Bool, failed: Bool = false) {
        self.wantedSeconds = wantedSeconds; self.advancedSeconds = advancedSeconds; self.hasVideoTrack = hasVideoTrack
        self.presentationWidth = presentationWidth; self.presentationHeight = presentationHeight
        self.firstFrameShown = firstFrameShown; self.failed = failed
    }
}

/// The player opened a downloaded episode but nothing moves (iPad: sound without a picture, the
/// clock at 0:00): after `grace` seconds of playing it stops with a clear message instead.
public enum PlaybackWatchdog {
    /// Seconds of "playing" before the player gives up.
    public static let grace: Double = 8
    /// The clock must have moved at least this much in that time.
    public static let minimumAdvance: Double = 1

    public enum Problem: String, Equatable, Sendable {
        /// The clock stayed where it opened.
        case clockStopped
        /// A video track, but no frame was shown (or its size is zero).
        case noPicture
        /// AVPlayerItem failed.
        case failed
    }

    public enum Verdict: Equatable, Sendable {
        /// Not decided yet: look again in a second.
        case wait
        /// Playing with a picture: the watchdog stops looking.
        case healthy
        case stuck(Problem)
    }

    public static func judge(_ sample: PlaybackSample) -> Verdict {
        if sample.failed { return .stuck(.failed) }
        let moved = sample.advancedSeconds >= minimumAdvance
        let picture = !sample.hasVideoTrack
            || (sample.firstFrameShown && sample.presentationWidth > 0 && sample.presentationHeight > 0)
        if moved && picture { return .healthy }
        guard sample.wantedSeconds >= grace else { return .wait }
        if !moved { return .stuck(.clockStopped) }
        return .stuck(.noPicture)
    }

    /// The player's message ("This episode can't be played on this device: …").
    public static func message(_ problem: Problem, detail: String? = nil, device: String = "device") -> String {
        let reason: String
        switch problem {
        case .clockStopped: reason = "playback does not start (the time stays at the start)"
        case .noPicture: reason = "the video shows no picture"
        case .failed: reason = detail.map { $0.isEmpty ? "the player failed" : $0 } ?? "the player failed"
        }
        return "This episode can't be played on this \(device): \(reason)."
    }
}

/// The main thread did not answer the player's background check: logged once per stall (the log
/// is written off the main thread, so a frozen player still leaves a line).
public enum MainThreadStall {
    /// Seconds without an answer before it counts as a stall.
    public static let threshold: Double = 3

    /// Whether a stall is to be logged now (`logged`: already logged for this stall).
    public static func shouldLog(lastAnswer: Double, now: Double, logged: Bool) -> Bool {
        !logged && now - lastAnswer >= threshold
    }

    /// The monitor's own timer did not run for a while (`lastTick` long before `now`): the app
    /// was suspended, not its main thread stuck; the wait starts over instead of being logged.
    public static func resumed(lastTick: Double, now: Double) -> Bool {
        now - lastTick >= threshold
    }
}

/// What AVFoundation says about a downloaded episode's file (AVURLAsset, read off the main thread).
public struct MediaProbe: Equatable, Sendable {
    public var isPlayable: Bool
    public var hasVideo: Bool
    public var hasAudio: Bool
    /// The video track can be decoded on this device (AVAssetTrack.isDecodable).
    public var videoDecodable: Bool
    public var durationSeconds: Double?

    public init(isPlayable: Bool, hasVideo: Bool, hasAudio: Bool, videoDecodable: Bool = true, durationSeconds: Double?) {
        self.isPlayable = isPlayable; self.hasVideo = hasVideo; self.hasAudio = hasAudio
        self.videoDecodable = videoDecodable; self.durationSeconds = durationSeconds
    }
}

/// Checks a finished episode download before it is listed as downloaded.
public enum DownloadCheck {
    public enum Verdict: Equatable, Sendable {
        case ok
        case bad(String)
    }

    /// Why a check failed: the bytes differ from the Mac's copy, or they are the Mac's copy but
    /// AVFoundation can't play them here.
    public enum Failure: Equatable, Sendable {
        case integrity
        case playability
    }

    public enum Next: Equatable, Sendable {
        case redownload
        case giveUp
    }

    /// Shorter than the Mac's duration by more than this is a cut file. The Mac's duration is the
    /// source's (an mkv's may count its subtitle track a little longer), so a longer copy is fine.
    public static func durationTolerance(_ expected: Double) -> Double { max(2, expected * 0.03) }

    public static func judge(_ probe: MediaProbe, expectedSeconds: Double?) -> Verdict {
        guard probe.isPlayable else { return .bad("AVFoundation can't play the file") }
        guard probe.hasVideo else { return .bad("no video track") }
        guard probe.hasAudio else { return .bad("no audio track") }
        guard probe.videoDecodable else { return .bad("this device can't decode its video") }
        guard let duration = probe.durationSeconds, duration.isFinite, duration > 0 else { return .bad("no duration") }
        if let expected = expectedSeconds, expected > 0, duration < expected - durationTolerance(expected) {
            return .bad("it lasts \(Int(duration)) s instead of \(Int(expected)) s")
        }
        return .ok
    }

    /// After a failed check (`attempt` 1 = the first download): different bytes are downloaded
    /// once more; the Mac's own bytes that don't play would only come again the same.
    public static func next(after failure: Failure, attempt: Int) -> Next {
        switch failure {
        case .integrity: return attempt < 2 ? .redownload : .giveUp
        case .playability: return .giveUp
        }
    }
}

/// The H.264 profile and level of a video track (its `avcC` box), for the player's log: an iPad
/// that can't decode a level shows no picture.
public enum VideoFormatInfo {
    /// avcC: version 1, profile, compatibility, level, ...
    public static func avcProfileLevel(_ avcC: [UInt8]) -> (profile: Int, level: Int)? {
        guard avcC.count >= 4, avcC[0] == 1 else { return nil }
        return (Int(avcC[1]), Int(avcC[3]))
    }

    /// "High@4.1" (unknown profiles by number).
    public static func describe(profile: Int, level: Int) -> String {
        let names = [66: "Baseline", 77: "Main", 88: "Extended", 100: "High", 110: "High10", 122: "High422", 244: "High444"]
        let name = names[profile] ?? "profile \(profile)"
        return "\(name)@\(level / 10).\(level % 10)"
    }
}
