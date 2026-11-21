import Foundation
import XCTest
@testable import PudgeCore

/// Reaching the Mac without making screens wait (`swift test` in ios/PudgeCore on the Mac).
final class MacReachabilityTests: XCTestCase {
    func testTheSavedAddressFirstThenTheKnownOnesEachOnce() {
        let saved = "https://MacBook-Pro-Maksim-3391.local.:8766"
        let known = ["https://192.168.1.20:8766", "https://macbook-pro-maksim-3391.local.:8766", "https://10.0.0.5:8766",
                     "http://192.168.1.21:8766", "https://user:pw@192.168.1.22:8766", "https://192.168.1.20:8766"]
        XCTAssertEqual(MacAddresses.candidates(saved: saved, known: known),
                       [saved, "https://192.168.1.20:8766", "https://10.0.0.5:8766"])
        let many = (1...20).map { "https://192.168.1.\($0):8766" }
        XCTAssertEqual(MacAddresses.candidates(saved: saved, known: many).count, MacAddresses.limit)
        XCTAssertEqual(MacAddresses.candidates(saved: saved, known: many).first, saved)
    }

    func testAddressesJustSeenComeFirst() {
        let known = ["https://192.168.1.20:8766", "https://10.0.0.5:8766"]
        XCTAssertEqual(MacAddresses.remember(["https://10.0.0.5:8766", "https://192.168.1.30:8766"], in: known),
                       ["https://10.0.0.5:8766", "https://192.168.1.30:8766", "https://192.168.1.20:8766"])
        XCTAssertEqual(MacAddresses.remember([], in: known), known)
    }

    func testResolvedAddressesBecomeURLs() {
        XCTAssertEqual(MacAddresses.url(host: "192.168.1.20", port: 8766), "https://192.168.1.20:8766")
        XCTAssertEqual(MacAddresses.url(host: "FD00::5", port: 8766), "https://[fd00::5]:8766")
        XCTAssertNil(MacAddresses.url(host: "fe80::1", port: 8766))
        XCTAssertNil(MacAddresses.url(host: "fe80::1%en0", port: 8766))
        XCTAssertNil(MacAddresses.url(host: "192.168.1", port: 8766))
        XCTAssertNil(MacAddresses.url(host: "192.168.1.300", port: 8766))
        XCTAssertNil(MacAddresses.url(host: "example.com", port: 8766))
        XCTAssertNil(MacAddresses.url(host: "192.168.1.20", port: 0))
    }

    func testLocalNetworkAddressesAndInterfaces() {
        for address in ["https://192.168.1.20:8766", "https://10.0.0.5:8766", "https://172.20.1.1:8766", "https://169.254.3.4:8766",
                        "https://127.0.0.1:8766", "https://MacBook-Pro-Maksim-3391.local.:8766", "https://mac.local:8766",
                        "https://[fd00::5]:8766"] {
            XCTAssertTrue(MacAddresses.isLocalNetwork(address), address)
        }
        // A VPN address (Tailscale's 100.64/10) or a DNS name may answer without Wi-Fi.
        for address in ["https://100.101.102.103:8766", "https://172.32.0.1:8766", "https://mac.example.ts.net:8766", "https://8.8.8.8:8766"] {
            XCTAssertFalse(MacAddresses.isLocalNetwork(address), address)
        }
        XCTAssertTrue(MacLink.localInterface(type: "wifi", name: "en0"))
        XCTAssertTrue(MacLink.localInterface(type: "wiredEthernet", name: "en5"))
        // Personal Hotspot: the Mac is connected to this iPhone.
        XCTAssertTrue(MacLink.localInterface(type: "other", name: "bridge100"))
        XCTAssertFalse(MacLink.localInterface(type: "cellular", name: "pdp_ip0"))
        XCTAssertFalse(MacLink.localInterface(type: "other", name: "utun3"))
    }

    func testAfterNoAnswerTheMacIsAskedAfter5Then15ThenEvery60Seconds() {
        var schedule = ProbeSchedule()
        XCTAssertTrue(schedule.shouldProbe(now: 100))
        schedule.record(answered: false, at: 100)
        XCTAssertFalse(schedule.shouldProbe(now: 104))
        XCTAssertTrue(schedule.shouldProbe(now: 105))
        schedule.record(answered: false, at: 105)
        XCTAssertFalse(schedule.shouldProbe(now: 119))
        XCTAssertTrue(schedule.shouldProbe(now: 120))
        schedule.record(answered: false, at: 120)
        XCTAssertFalse(schedule.shouldProbe(now: 179))
        XCTAssertTrue(schedule.shouldProbe(now: 180))
        schedule.record(answered: false, at: 180)
        XCTAssertEqual(schedule.wait, 60)
        XCTAssertFalse(schedule.lastAnswered)
        // An uptime that went back does not block the next check.
        XCTAssertTrue(schedule.shouldProbe(now: 10))
    }

    func testAnAnswerIsReusedBrieflyAndAResetAsksAtOnce() {
        var schedule = ProbeSchedule()
        schedule.record(answered: false, at: 0)
        schedule.record(answered: false, at: 5)
        schedule.record(answered: true, at: 20)
        XCTAssertEqual(schedule.failures, 0)
        XCTAssertTrue(schedule.lastAnswered)
        XCTAssertFalse(schedule.shouldProbe(now: 24))
        XCTAssertTrue(schedule.shouldProbe(now: 25))
        schedule.record(answered: false, at: 30)
        XCTAssertFalse(schedule.shouldProbe(now: 31))
        // Network change, foreground, pull to refresh: the next check asks now, backoff from the start.
        schedule.reset()
        XCTAssertTrue(schedule.shouldProbe(now: 31))
        schedule.record(answered: false, at: 31)
        XCTAssertEqual(schedule.wait, 5)
    }

    func testTheConnectionStateSaysWhyNothingSyncs() {
        XCTAssertEqual(MacLink.evaluate(paired: false, localNetwork: true, checked: true, available: false), .notPaired)
        XCTAssertEqual(MacLink.evaluate(paired: true, localNetwork: true, checked: false, available: false), .checking)
        XCTAssertEqual(MacLink.evaluate(paired: true, localNetwork: true, checked: true, available: true), .available)
        XCTAssertEqual(MacLink.evaluate(paired: true, localNetwork: true, checked: true, available: false), .offline)
        XCTAssertEqual(MacLink.evaluate(paired: true, localNetwork: false, checked: true, available: false), .noLocalNetwork)
        XCTAssertEqual(MacLink.offline.status, "Offline")
        XCTAssertNil(MacLink.offline.explanation(device: "iPhone"))
        XCTAssertEqual(MacLink.noLocalNetwork.status, "No Wi-Fi")
        XCTAssertEqual(MacLink.noLocalNetwork.explanation(device: "iPad"),
                       "No Wi-Fi — syncing with the Mac waits until this iPad is on the Mac's network")
        XCTAssertEqual(MacLink.unavailable("downloaded episodes play offline", noLocalNetwork: false, device: "iPhone"),
                       "Mac unavailable · downloaded episodes play offline")
        XCTAssertEqual(MacLink.unavailable("local audio works", noLocalNetwork: true, device: "iPhone"),
                       "No Wi-Fi — syncing with the Mac waits until this iPhone is on the Mac's network · local audio works")
    }

    /// REVIEW r2 ios-app #1: an HTTP error or an unreadable answer is no unavailable Mac.
    func testOnlyANetworkFailureMeansTheMacIsNotThere() {
        XCTAssertTrue(MacFailure.unreachable(URLError(.timedOut)))
        XCTAssertTrue(MacFailure.unreachable(URLError(.cannotConnectToHost)))
        XCTAssertFalse(MacFailure.unreachable(NSError(domain: "PudgeError", code: 1)))
        XCTAssertFalse(MacFailure.unreachable(DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "x"))))
    }
}
