import Foundation

/// The time of a slow piece of work, step by step, for a "[perf]" line in the app log
/// ("refresh 3.2 s: catalog 2.1 s, sync 0.9 s"). Times are seconds of a monotonic clock.
public struct PerfSteps {
    public let name: String
    public private(set) var steps: [(name: String, seconds: Double)] = []
    private let start: Double
    private var last: Double

    public init(_ name: String, at now: Double) { self.name = name; start = now; last = now }

    /// The step that just ended (from the previous mark, or the start).
    public mutating func mark(_ step: String, at now: Double) {
        steps.append((name: step, seconds: max(0, now - last)))
        last = now
    }

    public func total(at now: Double) -> Double { max(0, now - start) }

    /// The log line when the whole took `threshold` seconds or more (nil otherwise); steps
    /// under 0.05 s are left out, the slowest step comes first.
    public func report(at now: Double, threshold: Double = 1) -> String? {
        let total = total(at: now)
        guard total >= threshold else { return nil }
        let shown = steps.filter { $0.seconds >= 0.05 }.sorted { $0.seconds > $1.seconds }
            .map { "\($0.name) " + Self.seconds($0.seconds) }
        return name + " " + Self.seconds(total) + (shown.isEmpty ? "" : ": " + shown.joined(separator: ", "))
    }

    static func seconds(_ value: Double) -> String { String(format: "%.1f s", value) }
}

/// The launch line of the app log: how long until the first screen showed.
public enum LaunchPerf {
    /// `sinceProcessStart`: from the process start (nil when unknown); `model`: the app's data
    /// (store, downloads) made ready before the first screen.
    public static func line(sinceProcessStart: Double?, sinceAppInit: Double, model: Double) -> String {
        var parts = ["first screen " + (sinceProcessStart.map { PerfSteps.seconds($0) + " after launch" } ?? "shown")]
        parts.append(PerfSteps.seconds(sinceAppInit) + " after the app started")
        parts.append("data " + PerfSteps.seconds(model))
        return "launch: " + parts.joined(separator: ", ")
    }
}
