import XCTest
@testable import Pudge

final class PairingTests: XCTestCase {
    func payload(address: String = "https://192.168.1.2:8766", major: Int = 2) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["protocolMajor": major, "serverId": UUID().uuidString,
            "serverName": "Mac", "baseURL": address, "certificateSHA256": String(repeating: "a", count: 64),
            "invitation": String(repeating: "x", count: 40), "expiresAt": 2000000000000])
    }
    func testQRRoundTrip() throws {
        let data = try payload()
        let encoded = data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        let result = try PairingPayload.parse("pudge-audio://pair?data=" + encoded)
        XCTAssertEqual(result.baseURL, "https://192.168.1.2:8766")
        XCTAssertEqual(result.protocolMajor, 2)
    }
    func testRejectsUntrustedInvitationURLs() throws {
        for address in ["http://192.168.1.2", "https://user:password@host", "https://host?token=x", "https://host#fragment"] {
            let data = try payload(address: address)
            XCTAssertThrowsError(try PairingPayload.parse(String(decoding: data, as: UTF8.self)))
        }
        XCTAssertThrowsError(try PairingPayload.parse(String(decoding: payload(major: 1), as: UTF8.self)))
    }
    func testInvitationListsEveryMacAddressAndRefusesAnUnsafeOne() throws {
        var object = try XCTUnwrap(try JSONSerialization.jsonObject(with: payload()) as? [String: Any])
        object["baseURLs"] = ["https://192.168.1.2:8766", "https://10.0.0.4:8766"]
        let both = try PairingPayload.parse(String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self))
        XCTAssertEqual(both.candidates, ["https://192.168.1.2:8766", "https://10.0.0.4:8766"])
        object["baseURLs"] = ["https://192.168.1.2:8766", "http://10.0.0.4:8766"]
        XCTAssertThrowsError(try PairingPayload.parse(String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)))
        // Older Macs send one address.
        XCTAssertEqual(try PairingPayload.parse(String(decoding: try payload(), as: UTF8.self)).candidates, ["https://192.168.1.2:8766"])
    }
    func testKeychainCredentialCanBeUpdatedWithoutAnotherEntry() throws {
        let key = "test-" + UUID().uuidString
        defer { try? KeychainStore.delete(key) }
        try KeychainStore.save(key, "first")
        try KeychainStore.save(key, "second")
        XCTAssertEqual(try KeychainStore.load(key, as: String.self), "second")
        try KeychainStore.delete(key)
        XCTAssertNil(try KeychainStore.load(key, as: String.self))
    }
}
