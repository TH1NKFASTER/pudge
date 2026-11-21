import Foundation

/// When a cut download of a file the Mac prepared (an episode, a reading volume) is tried again.
///
/// A Wi-Fi drop, a phone that stopped reading for a while or a Mac that is busy or restarting
/// cuts the connection; the next attempt resumes where the last one stopped (the Mac serves
/// byte ranges) instead of failing the whole download with a network error.
public enum TransferRetry {
    /// Attempts in all, the first one included.
    public static let attempts = 6

    /// URL error codes of a dropped, stalled or not yet answering connection.
    static let transientCodes: Set<Int> = [
        NSURLErrorNetworkConnectionLost, NSURLErrorTimedOut, NSURLErrorCannotConnectToHost,
        NSURLErrorNotConnectedToInternet, NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed,
    ]

    /// A connection problem worth another try (not a refusal, a cancel or a file error).
    public static func isTransient(_ error: Error) -> Bool {
        let value = error as NSError
        return value.domain == NSURLErrorDomain && transientCodes.contains(value.code)
    }

    /// Whether attempt `attempt` (1-based) that failed with `error` is followed by another one.
    public static func shouldRetry(_ error: Error, attempt: Int) -> Bool {
        attempt < attempts && isTransient(error)
    }

    /// How long a download that keeps its bytes waits for a Mac that does not answer (quit for an
    /// update, restarting) before it gives up, counted from the last byte received. The six quick
    /// attempts alone (about 30 s) gave up while the Mac app updated, and the download started over.
    public static let patience: Double = 20 * 60

    /// The same for such a download: after the quick attempts it goes on trying while nothing
    /// has arrived for less than `patience` seconds (`stalledFor`).
    public static func shouldRetry(_ error: Error, attempt: Int, stalledFor: Double) -> Bool {
        isTransient(error) && (attempt < attempts || stalledFor < patience)
    }

    /// Seconds to wait before the attempt after `attempt`.
    public static func delay(after attempt: Int) -> Double { min(15, 2 * Double(max(1, attempt))) }
}
