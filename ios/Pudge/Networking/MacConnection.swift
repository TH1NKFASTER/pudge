import Foundation
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

struct PairedMac: Codable {
    var deviceId: String
    var serverId: String
    var serverName: String
    var baseURL: String
    var certificateSHA256: String
    var token: String
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
    let session: URLSession
    /// Short timeouts: telling quickly whether the Mac answers at all (away from home the
    /// long timeout would keep downloaded media waiting).
    let probeSession: URLSession
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
        if data != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return request
    }
    func send<T: Decodable>(_ route: String, method: String = "GET", data: Data? = nil, as: T.Type) async throws -> T {
        let (body, response) = try await session.data(for: request(route, method: method, data: data))
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            if (response as? HTTPURLResponse)?.statusCode == 401 { throw PudgeError.invalid("Access was revoked on the Mac. Connect the Mac again.") }
            if (response as? HTTPURLResponse)?.statusCode == 422, let reason = Self.reason(body) { throw PudgeError.invalid(reason) }
            if (response as? HTTPURLResponse)?.statusCode == 404, route.hasPrefix("/api/v2/media/") {
                throw PudgeError.invalid("Update Pudge on the Mac to use this on the iPhone or iPad")
            }
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            AppLog.write("http", "\(method) \(route.split(separator: "?").first.map(String.init) ?? route) → HTTP \(code)")
            throw PudgeError.invalid("The Mac did not confirm the operation (HTTP \(code)). Data is kept for a retry.")
        }
        return try JSONDecoder().decode(T.self, from: body)
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
}

@MainActor
final class MacDiscovery: NSObject, NetServiceBrowserDelegate, NetServiceDelegate {
    private let browser = NetServiceBrowser()
    /// Every Pudge Mac seen on the network (by its server ID): switching Macs needs no new search.
    private var services: [String: NetService] = [:]
    private var searching = false
    var target: String?
    var onAddress: ((String) -> Void)?
    func start(serverId: String) {
        target = serverId; browser.delegate = self
        if let service = services[serverId] { service.delegate = self; service.resolve(withTimeout: 5) }
        guard !searching else { return }
        searching = true
        browser.searchForServices(ofType: "_pudge-audio._tcp.", inDomain: "local.")
    }
    func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
        services[service.name] = service
        guard service.name == target else { return }
        service.delegate = self; service.resolve(withTimeout: 5)
    }
    func netServiceBrowser(_ browser: NetServiceBrowser, didRemove service: NetService, moreComing: Bool) {
        services[service.name] = nil
    }
    func netServiceDidResolveAddress(_ sender: NetService) {
        guard sender.name == target else { return }
        if let host = sender.hostName { onAddress?("https://\(host):\(sender.port)") }
    }
    func stop() { browser.stop(); searching = false; services.values.forEach { $0.stop() }; services.removeAll() }
}
