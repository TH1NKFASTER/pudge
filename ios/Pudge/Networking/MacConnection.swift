import Foundation
import Darwin
import Security
import CryptoKit
import PudgeCore

struct PairingPayload: Codable {
    let protocolMajor: Int
    let serverId: String
    let serverName: String
    let baseURL: String
    let certificateSHA256: String
    let invitation: String
    let expiresAt: Int64
    /// Every LAN address of the Mac, best first (older Macs send only baseURL).
    var baseURLs: [String]? = nil
    /// Addresses to try in order.
    var candidates: [String] {
        var seen = Set<String>()
        return ([baseURL] + (baseURLs ?? [])).filter { seen.insert($0).inserted }
    }
    /// HTTPS only, and no login, query or fragment parts that could carry or leak secrets.
    static func plainHTTPS(_ address: String) -> Bool {
        guard let url = URL(string: address) else { return false }
        return url.scheme == "https" && url.host != nil && url.user == nil && url.password == nil && url.query == nil && url.fragment == nil
    }
    static func parse(_ text: String) throws -> Self {
        let data: Data
        if let components = URLComponents(string: text), components.scheme == "pudge-audio",
           let value = components.queryItems?.first(where: { $0.name == "data" })?.value {
            var base = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
            base += String(repeating: "=", count: (4 - base.count % 4) % 4)
            guard let decoded = Data(base64Encoded: base) else { throw PudgeError.invalid("Invalid invitation") }
            data = decoded
        } else { data = Data(text.utf8) }
        let payload = try JSONDecoder().decode(Self.self, from: data)
        guard payload.protocolMajor == 2, UUID(uuidString: payload.serverId) != nil,
              payload.certificateSHA256.count == 64,
              payload.certificateSHA256.allSatisfy({ "0123456789abcdef".contains($0) }),
              payload.candidates.count <= 8, payload.candidates.allSatisfy(plainHTTPS) else {
            throw PudgeError.invalid("Unsupported invitation")
        }
        return payload
    }
}

/// The Mac answered and refused the request itself (HTTP 409 or 422): sending the same
/// request again gets the same answer, so a queued item that gets it is set aside instead
/// of stopping everything queued after it.
struct MacRefusal: LocalizedError {
    let status: Int
    let text: String
    var errorDescription: String? { text }
}

struct PairedMac: Codable {
    var deviceId: String
    var serverId: String
    var serverName: String
    var baseURL: String
    var certificateSHA256: String
    var token: String
    /// Other addresses the Mac answered at or was resolved to (its IPs, newest first; the pairing
    /// invitation's): tried when `baseURL` (usually its Bonjour host name, which macOS changes
    /// after a name conflict) does not answer. Nil in pairings saved before this was kept.
    var knownAddresses: [String]? = nil
}

enum KeychainStore {
    private static func query(_ key: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "Pudge.NativeAudio", kSecAttrAccount as String: key]
    }
    static func load<T: Decodable>(_ key: String, as: T.Type) throws -> T? {
        var request = query(key); request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw PudgeError.invalid("Keychain unavailable") }
        return try JSONDecoder().decode(T.self, from: data)
    }
    static func save<T: Encodable>(_ key: String, _ value: T) throws {
        let data = try JSONEncoder().encode(value)
        let request = query(key)
        let update = [kSecValueData as String: data]
        let result = SecItemUpdate(request as CFDictionary, update as CFDictionary)
        if result == errSecItemNotFound {
            var insert = request; insert[kSecValueData as String] = data
            insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            guard SecItemAdd(insert as CFDictionary, nil) == errSecSuccess else { throw PudgeError.invalid("Could not save the connection") }
        } else if result != errSecSuccess { throw PudgeError.invalid("Could not update the connection") }
    }
    static func delete(_ key: String) throws {
        let status = SecItemDelete(query(key) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw PudgeError.invalid("Could not remove the connection") }
    }
}

class PinnedDelegate: NSObject, URLSessionDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    let pin: String
    let origin: URL
    init(pin: String, origin: URL) { self.pin = pin; self.origin = origin }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let certificate = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        let data = SecCertificateCopyData(certificate) as Data
        let actual = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        guard actual == pin else { completionHandler(.cancelAuthenticationChallenge, nil); return }
        // The QR supplies the exact local server identity. A private anchor
        // avoids a global TLS override and remains valid when its LAN IP changes.
        SecTrustSetPolicies(trust, SecPolicyCreateBasicX509())
        SecTrustSetAnchorCertificates(trust, [certificate] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        guard SecTrustEvaluateWithError(trust, nil) else { completionHandler(.cancelAuthenticationChallenge, nil); return }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        urlSession(session, didReceive: challenge, completionHandler: completionHandler)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, url.scheme == "https", url.host == origin.host, url.port == origin.port else {
            completionHandler(nil); return
        }
        completionHandler(request)
    }
}

final class MacConnection: @unchecked Sendable {
    let mac: PairedMac
    let delegate: PinnedDelegate
    /// Only this connection's methods use it (self stays alive for each call): no task can be
    /// created on it after deinit or close() invalidated it (that raises an exception).
    private let session: URLSession
    /// Short timeouts: telling quickly whether the Mac answers at all (away from home the
    /// long timeout would keep downloaded media waiting).
    private let probeSession: URLSession
    private let closeLock = NSLock()
    private var closed = false
    var taskIdentity: String { SHA256.hash(data: Data((mac.baseURL + ":" + mac.certificateSHA256 + ":" + mac.token).utf8)).map { String(format: "%02x", $0) }.joined() }
    init(_ mac: PairedMac) throws {
        guard let url = URL(string: mac.baseURL), url.scheme == "https", url.host != nil else { throw PudgeError.invalid("Invalid Mac address") }
        self.mac = mac; delegate = PinnedDelegate(pin: mac.certificateSHA256, origin: url)
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 60; config.timeoutIntervalForResource = 1200
        session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        let probe = URLSessionConfiguration.ephemeral
        probe.timeoutIntervalForRequest = 6; probe.timeoutIntervalForResource = 8
        probeSession = URLSession(configuration: probe, delegate: delegate, delegateQueue: nil)
    }
    /// A replaced connection (another address, pairing again) ends with its last user: the
    /// model, TransferManager, a MacVideoStream or a request still running keep it alive.
    /// finishTasksAndInvalidate lets requests already started finish (a URLSession with a
    /// delegate is otherwise never released: two sessions leaked per address change).
    deinit { close() }
    /// A request over the pinned session (archives, read markers, page alignment).
    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }
    /// True when this Mac answers within a few seconds.
    func answers() async -> Bool {
        struct Identity: Decodable { let serverId: String }
        guard let probe = try? request("/api/v2/capabilities"),
              let answer = try? await probeSession.data(for: probe),
              (answer.1 as? HTTPURLResponse)?.statusCode == 200,
              let identity = try? JSONDecoder().decode(Identity.self, from: answer.0) else { return false }
        return identity.serverId == mac.serverId
    }
    func request(_ route: String, method: String = "GET", data: Data? = nil) throws -> URLRequest {
        guard let url = URL(string: mac.baseURL + route), url.scheme == "https" else { throw PudgeError.invalid("Invalid request address") }
        var request = URLRequest(url: url); request.httpMethod = method; request.httpBody = data
        if !mac.token.isEmpty { request.setValue("Bearer " + mac.token, forHTTPHeaderField: "Authorization") }
        // This build finds the Mac under any Bonjour name: once every paired device says so, the
        // Mac stops advertising its server ID as the name (older builds accept only that).
        request.setValue("any-name", forHTTPHeaderField: "X-Pudge-Discovery")
        if data != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return request
    }
    func send<T: Decodable>(_ route: String, method: String = "GET", data: Data? = nil, as: T.Type) async throws -> T {
        let (body, response) = try await session.data(for: request(route, method: method, data: data))
        try Self.check(body, response, route: route, method: method)
        return try JSONDecoder().decode(T.self, from: body)
    }
    /// A refused or failed answer as an error (its reason, a log line); nothing for a 2xx.
    private static func check(_ body: Data, _ response: URLResponse, route: String, method: String) throws {
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            if (response as? HTTPURLResponse)?.statusCode == 401 { throw PudgeError.invalid("Access was revoked on the Mac. Connect the Mac again.") }
            if (response as? HTTPURLResponse)?.statusCode == 422, let reason = Self.reason(body) { throw MacRefusal(status: 422, text: reason) }
            if (response as? HTTPURLResponse)?.statusCode == 404, route.hasPrefix("/api/v2/media/") {
                throw PudgeError.invalid("Update Pudge on the Mac to use this on the iPhone or iPad")
            }
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            AppLog.write("http", "\(method) \(route.split(separator: "?").first.map(String.init) ?? route) → HTTP \(code)")
            let text = "The Mac did not confirm the operation (HTTP \(code)). Data is kept for a retry."
            if code == 409 || code == 422 { throw MacRefusal(status: code, text: text) }
            // Temporary on the Mac (a locked database, a service down): its reason, sent again later.
            if code == 503, let reason = Self.reason(body) { throw PudgeError.invalid(reason) }
            throw PudgeError.invalid(text)
        }
    }
    /// A GET asked again and again (the anime list while the app is in front): with the ETag of
    /// the last answer, a Mac whose answer did not change says "304" and nothing is decoded or
    /// applied again (nil). An older Mac always answers in full. Returns the value and its ETag.
    func sendIfChanged<T: Decodable>(_ route: String, etag: String?, as: T.Type) async throws -> (value: T?, etag: String?) {
        let answer = try await dataIfChanged(route, etag: etag)
        guard let body = answer.body else { return (nil, answer.etag) }
        return (try JSONDecoder().decode(T.self, from: body), answer.etag)
    }
    /// The same as sendIfChanged, undecoded (the catalog keeps the body on disk): nil on 304.
    func dataIfChanged(_ route: String, etag: String?) async throws -> (body: Data?, etag: String?) {
        var asked = try request(route)
        if let etag {
            asked.setValue(etag, forHTTPHeaderField: "If-None-Match")
            asked.cachePolicy = .reloadIgnoringLocalCacheData
        }
        let (body, response) = try await session.data(for: asked)
        if let http = response as? HTTPURLResponse, http.statusCode == 304, etag != nil { return (nil, etag) }
        try Self.check(body, response, route: route, method: "GET")
        return (body, (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "ETag"))
    }
    /// A resource straight into a file (read-along texts are up to 50 MB: never held in memory).
    /// `target` is replaced only by a complete 200 answer.
    func download(_ route: String, to target: URL) async throws {
        let (location, response) = try await session.download(for: request(route))
        defer { try? FileManager.default.removeItem(at: location) }
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            if (response as? HTTPURLResponse)?.statusCode == 422,
               let reason = Self.reason((try? Data(contentsOf: location, options: .mappedIfSafe)) ?? Data()) {
                throw PudgeError.invalid(reason)
            }
            throw PudgeError.invalid("The Mac did not send the file")
        }
        let expected = response.expectedContentLength
        if expected >= 0, let size = (try? FileManager.default.attributesOfItem(atPath: location.path))?[.size] as? NSNumber,
           size.int64Value != expected {
            throw PudgeError.invalid("The file from the Mac was cut off")
        }
        try? FileManager.default.removeItem(at: target)
        try FileManager.default.moveItem(at: location, to: target)
    }
    /// Raw bytes of a small resource (covers); large audio uses TransferManager.
    func bytes(_ route: String) async throws -> Data {
        let (body, response) = try await session.data(for: request(route))
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            if (response as? HTTPURLResponse)?.statusCode == 422, let reason = Self.reason(body) { throw PudgeError.invalid(reason) }
            throw PudgeError.invalid("The Mac did not send the file")
        }
        return body
    }
    /// Readable reason the Mac gave for a refused request ({"error": "..."}).
    static func reason(_ body: Data) -> String? {
        struct Failure: Decodable { let error: String }
        guard let text = try? JSONDecoder().decode(Failure.self, from: body).error, !text.isEmpty else { return nil }
        return text
    }
    func post<T: Decodable, V: Encodable>(_ route: String, value: V, as: T.Type) async throws -> T {
        try await send(route, method: "POST", data: JSONEncoder().encode(value), as: T.self)
    }
    /// The same check at another address of this Mac: the same pinned session (no new one per
    /// address), no token sent. True when the Mac with this certificate and server ID answers there.
    func answers(at address: String) async -> Bool {
        struct Identity: Decodable { let serverId: String }
        guard MacAddresses.plainHTTPS(address), let url = URL(string: address + "/api/v2/capabilities") else { return false }
        guard let answer = try? await probeSession.data(for: URLRequest(url: url)),
              (answer.1 as? HTTPURLResponse)?.statusCode == 200,
              let identity = try? JSONDecoder().decode(Identity.self, from: answer.0) else { return false }
        return identity.serverId == mac.serverId
    }
    /// A connection only tried (another address of the Mac): its sessions end with it.
    /// Once only (deinit calls it too); started requests finish, nothing is cancelled.
    func close() {
        closeLock.lock(); let first = !closed; closed = true; closeLock.unlock()
        guard first else { return }
        session.finishTasksAndInvalidate(); probeSession.finishTasksAndInvalidate()
    }
}

/// Finds the paired Mac when its address changes. A Mac advertises a random name per network
/// (it used its permanent server ID, which let anyone recognise the same Mac everywhere), so
/// every Pudge service found is tried; only the paired Mac passes the pinned certificate check.
/// Older Macs still advertise their server ID as the name (`named`: that one is ours).
@MainActor
final class MacDiscovery: NSObject, NetServiceBrowserDelegate, NetServiceDelegate {
    private var browser = NetServiceBrowser()
    /// Every Pudge Mac seen on the network (by its advertised name): switching Macs needs no new search.
    private var services: [String: NetService] = [:]
    private var searching = false
    /// A search is running (not failed or stopped): it is kept while the Mac stays away.
    var isSearching: Bool { searching }
    /// Address → when it was last reported: a Mac that still advertises its legacy name for an
    /// older device has two services at one address, reported once.
    private var reported: [String: TimeInterval] = [:]
    var target: String?
    /// The resolved service's host name address, its numeric addresses, and whether its name is the server ID.
    var onAddress: ((String, [String], Bool) -> Void)?
    func start(serverId: String) {
        target = serverId; browser.delegate = self
        for service in services.values { service.delegate = self; service.resolve(withTimeout: 5) }
        guard !searching else { return }
        searching = true
        browser.searchForServices(ofType: "_pudge-audio._tcp.", inDomain: "local.")
    }
    /// Browses and resolves anew: after a network change, back in the foreground, and when the Mac
    /// did not answer. A search that failed or stalled off Wi-Fi was never started again, so a Mac
    /// that macOS had renamed meanwhile ("…-3395.local") was never found under its new name.
    func restart() {
        guard let target else { return }
        browser.delegate = nil; browser.stop()
        services.values.forEach { $0.stop() }; services.removeAll(); reported.removeAll()
        browser = NetServiceBrowser(); searching = false
        start(serverId: target)
    }
    func netServiceBrowser(_ browser: NetServiceBrowser, didNotSearch errorDict: [String: NSNumber]) {
        searching = false
        AppLog.write("pairing", "Bonjour search did not start: \(errorDict)")
    }
    func netServiceBrowserDidStopSearch(_ browser: NetServiceBrowser) { searching = false }
    func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
        services[service.name] = service
        guard target != nil, services.count <= 16 else { return }
        service.delegate = self; service.resolve(withTimeout: 5)
    }
    func netServiceBrowser(_ browser: NetServiceBrowser, didRemove service: NetService, moreComing: Bool) {
        services[service.name] = nil
    }
    func netServiceDidResolveAddress(_ sender: NetService) {
        guard let target, let host = sender.hostName else { return }
        let address = "https://\(host):\(sender.port)"
        let now = ProcessInfo.processInfo.systemUptime
        if let last = reported[address], now - last < 30 { return }
        reported[address] = now
        let numeric = (sender.addresses ?? []).compactMap(Self.numericHost).compactMap { MacAddresses.url(host: $0, port: sender.port) }
        onAddress?(address, numeric, sender.name == target)
    }
    /// "192.168.1.20" or "fd00::5" from a resolved socket address.
    nonisolated static func numericHost(_ address: Data) -> String? {
        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        let capacity = socklen_t(host.count), length = socklen_t(address.count)
        let status = address.withUnsafeBytes { raw -> Int32 in
            guard let socket = raw.baseAddress?.assumingMemoryBound(to: sockaddr.self) else { return -1 }
            return getnameinfo(socket, length, &host, capacity, nil, 0, NI_NUMERICHOST)
        }
        guard status == 0 else { return nil }
        return host.withUnsafeBufferPointer { buffer in buffer.baseAddress.map { String(cString: $0) } }
    }
    func stop() { browser.stop(); searching = false; services.values.forEach { $0.stop() }; services.removeAll(); reported.removeAll() }
}
