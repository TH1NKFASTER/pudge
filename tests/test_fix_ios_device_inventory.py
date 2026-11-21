"""DEV: the iPhone/iPad reports what it has downloaded, so the Mac's library shows it.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The payload and
when to send it are pure code in PudgeCore with XCTest cases (ios/PudgeCore/Tests/
PudgeCoreTests/DeviceInventoryTests.swift, `swift test --package-path ios/PudgeCore` on the
Mac); the Mac side is behaviour-tested (tests/test_fix_iphone_device_inventory.py, the window in
tests/test_fix_device_copies_page.py). Guarded here by reading the Swift source: the body's keys
are the ones the Mac reads, and the app sends it after a refresh and after downloads change.
"""

from __future__ import annotations

import re

from source_policy import source

from pudge.iphone_inventory import parse_report

CORE = "ios/PudgeCore/Sources/PudgeCore/DeviceInventory.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/DeviceInventoryTests.swift"
MODEL = "ios/Pudge/App/PudgeModel.swift"


def _swift(relative: str, start: str = "", end: str = "") -> str:
    text = source(relative)
    if start:
        at = text.index(start)
        text = text[at : text.index(end, at + len(start))]
    return re.sub(r"\s+", " ", text)


def test_the_phone_sends_the_keys_the_mac_reads() -> None:
    struct = _swift(CORE, "public struct DeviceInventory", "public init(")
    keys = re.findall(r"public let (\w+):", struct)
    assert keys == ["idiom", "episodes", "lightNovels", "manga", "audiobooks"]
    assert 'public static let route = "/api/v2/inventory"' in struct
    # The same body (as JSONEncoder writes it) is a valid report on the Mac.
    body = {"idiom": "pad", "episodes": [3], "lightNovels": [2], "manga": [1], "audiobooks": []}
    assert parse_report(body)["idiom"] == "pad"
    limit = re.search(r"public static let limit = (\d+)", struct)
    from pudge.iphone_inventory import ITEM_LIMIT

    assert limit is not None and int(limit.group(1)) == ITEM_LIMIT


def test_the_payload_and_policy_have_xctest_cases() -> None:
    tests = source(CORE_TESTS)
    for case in (
        "testIdsAreTheMacsOnlySortedOnceEach",
        "testListsStopAtTheMacsLimit",
        "testTheBodyHasTheKeysTheMacReads",
        "testAChangeIsSentAndTheSameInventoryOnlyAfterTheRefreshInterval",
        "testAnotherMacOrPairingAgainGetsTheInventory",
    ):
        assert f"func {case}()" in tests, case


def test_the_app_reports_after_a_refresh_and_after_downloads_change() -> None:
    refresh = _swift(MODEL, "func refresh() async {", "private func moveCarriedPlaces")
    assert (
        "await sync() // What is downloaded here, for the Mac's library (after connecting or refreshing). watchInventory() await reportInventory()"
        in refresh
    )
    configure = _swift(MODEL, "private func configure(_ mac: PairedMac)", "discovery.onAddress")
    assert "inventoryPolicy.reset()" in configure
    report = _swift(MODEL, "func reportInventory() async {", "func watchInventory()")
    assert 'macSupports("inventory")' in report and "macAvailable" in report
    assert "connection.post(DeviceInventory.route, value: inventory" in report
    assert report.index("needsSending") < report.index("connection.post") < report.index("markSent")
    watch = _swift(MODEL, "func watchInventory() {", "\n}\n")
    for publisher in ("media.$localEpisodes", "media.$readingDownloads", "$transfers"):
        assert publisher in watch, publisher
    assert watch.count("inventoryChanged()") == 3
    inventory = _swift(MODEL, "func currentInventory() -> DeviceInventory? {", "func inventoryChanged()")
    # Only the paired Mac's items: a previous Mac's downloads and phone-only books stay out.
    assert inventory.count("isOtherMac") == 2 and "store.isRemote(book, serverId: server)" in inventory
    assert "userInterfaceIdiom == .pad" in inventory
    offload = _swift(MODEL, "func offload(_ book: AudioManifest) async {", "func resolve(")
    assert "inventoryChanged()" in offload
