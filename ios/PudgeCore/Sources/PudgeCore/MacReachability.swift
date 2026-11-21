import Foundation

/// How the iPhone/iPad stands with its Mac, for Settings → Connection and the screens' notes.
public enum MacLink: Equatable, Sendable {
    case notPaired
    /// Paired, not asked yet since launch.
    case checking
    case available
    /// On Wi-Fi (or wired), and the Mac does not answer.
    case offline
    /// No Wi-Fi or wired network (e.g. only cellular): the Mac is not asked at all.
    case noLocalNetwork

    public static func evaluate(paired: Bool, localNetwork: Bool, checked: Bool, available: Bool) -> MacLink {
        guard paired else { return .notPaired }
        if available { return .available }
        if !localNetwork { return .noLocalNetwork }
        return checked ? .offline : .checking
    }

    /// The short state (Settings → Connection).
    public var status: String {
        switch self {
        case .notPaired: return "Not connected"
        case .checking: return "Checking…"
        case .available: return "Available"
        case .offline: return "Offline"
        case .noLocalNetwork: return "No Wi-Fi"
        }
    }

    /// Why nothing syncs, when it is not the Mac's fault.
    public func explanation(device: String) -> String? {
        self == .noLocalNetwork ? Self.noWiFi(device: device) : nil
    }

    public static func noWiFi(device: String) -> String {
        "No Wi-Fi — syncing with the Mac waits until this \(device) is on the Mac's network"
    }

    /// A screen's note while the Mac cannot be reached: "Mac unavailable · <what still works>",
    /// or without Wi-Fi the reason instead of "Mac unavailable".
    public static func unavailable(_ note: String, noLocalNetwork: Bool, device: String) -> String {
        let head = noLocalNetwork ? noWiFi(device: device) : "Mac unavailable"
        return note.isEmpty ? head : head + " · " + note
    }

    /// A network interface the Mac can be reached over: Wi-Fi, wired, or this device's Personal
    /// Hotspot (a `bridge` interface) with the Mac connected to it. `type` as NWInterface names it.
    public static func localInterface(type: String, name: String) -> Bool {
        type == "wifi" || type == "wiredEthernet" || name.hasPrefix("bridge")
    }
}

/// When to ask the Mac whether it answers. A screen never waits for it on every tab switch:
/// an answer is reused for `reuse` seconds, and after no answer the Mac is asked again only
/// after 5 s, 15 s, then every 60 s. A network change, coming back to the foreground, pull to
/// refresh and Refresh start over (`reset`): the next check asks at once.
public struct ProbeSchedule: Equatable, Sendable {
    public static let reuse: TimeInterval = 5
    public static let backoff: [TimeInterval] = [5, 15, 60]
    /// Checks in a row the Mac did not answer.
    public private(set) var failures = 0
    /// When (uptime) the last answer or failure was seen; nil: ask at once.
    public private(set) var lastAt: TimeInterval?
    public private(set) var lastAnswered = false

    public init() {}

    /// How long the last result stands.
    public var wait: TimeInterval {
        lastAnswered ? Self.reuse : Self.backoff[max(0, min(failures, Self.backoff.count) - 1)]
    }

    /// True: ask the Mac now. False: take `lastAnswered`.
    public func shouldProbe(now: TimeInterval) -> Bool {
        guard let lastAt else { return true }
        // An uptime that went back (another boot) is no reason to wait.
        return now < lastAt || now - lastAt >= wait
    }

    public mutating func record(answered: Bool, at now: TimeInterval) {
        lastAt = now
        lastAnswered = answered
        failures = answered ? 0 : failures + 1
    }

    public mutating func reset() {
        failures = 0
        lastAt = nil
    }
}

/// The addresses the Mac may answer at. The saved one is usually its Bonjour host name
/// ("MacBook-Pro.local."), which stops answering when macOS renames the Mac after a name
/// conflict ("…-3395"); its last resolved IP addresses and the pairing invitation's are tried
/// next, each with the same certificate pin and server ID check.
public enum MacAddresses {
    public static let limit = 8

    /// Plain HTTPS only: no login, query or fragment that could carry or leak a secret.
    public static func plainHTTPS(_ address: String) -> Bool {
        guard let url = URL(string: address) else { return false }
        return url.scheme == "https" && url.host != nil && url.user == nil && url.password == nil && url.query == nil && url.fragment == nil
    }

    /// In order: the saved address, then the known ones (newest first); each once, at most `limit`.
    public static func candidates(saved: String, known: [String]) -> [String] {
        var seen = Set<String>()
        return Array(([saved] + known).filter { plainHTTPS($0) && seen.insert($0.lowercased()).inserted }.prefix(limit))
    }

    /// `known` with `addresses` (just seen) in front.
    public static func remember(_ addresses: [String], in known: [String]) -> [String] {
        var seen = Set<String>()
        return Array((addresses + known).filter { plainHTTPS($0) && seen.insert($0.lowercased()).inserted }.prefix(limit))
    }

    /// A local-network address: a private, link-local or loopback IP, or a Bonjour ".local" name.
    /// Another one (a VPN or DNS name the user entered) may answer without Wi-Fi.
    public static func isLocalNetwork(_ address: String) -> Bool {
        guard let raw = URL(string: address)?.host?.lowercased(), !raw.isEmpty else { return true }
        let host = raw.trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if host == "localhost" || host.hasSuffix(".local") || host.hasSuffix(".local.") { return true }
        if host.contains(":") {
            return host.hasPrefix("fc") || host.hasPrefix("fd") || host.hasPrefix("fe80") || host == "::1"
        }
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        let numbers = parts.compactMap { UInt8($0) }
        guard parts.count == 4, numbers.count == 4 else { return false }
        switch (numbers[0], numbers[1]) {
        case (10, _), (127, _), (192, 168), (169, 254), (172, 16...31): return true
        default: return false
        }
    }

    /// The URL of a resolved numeric address: "https://192.168.1.20:8766", "https://[fd00::5]:8766".
    /// Nil for a link-local IPv6 address (it needs the interface in the URL) and anything else.
    public static func url(host: String, port: Int) -> String? {
        guard (1...65535).contains(port), !host.isEmpty else { return nil }
        if host.contains(":") {
            let lower = host.lowercased()
            guard !lower.hasPrefix("fe80"), !host.contains("%"), lower != "::1",
                  host.allSatisfy({ $0.isHexDigit || $0 == ":" || $0 == "." }) else { return nil }
            return "https://[\(lower)]:\(port)"
        }
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4, parts.allSatisfy({ !$0.isEmpty && $0.count <= 3 && UInt8($0) != nil }) else { return nil }
        return "https://\(host):\(port)"
    }
}

/// What a failed sync says about the Mac: only a network failure means it is not there. An HTTP
/// error or an answer this app cannot read came from a Mac that answered; marking it unavailable
/// for that switched off grades and Mac actions while the Mac was there.
public enum MacFailure {
    public static func unreachable(_ error: Error) -> Bool {
        (error as NSError).domain == NSURLErrorDomain
    }
}
