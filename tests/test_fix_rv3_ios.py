"""RV3-IOS: review of the iPhone/iPad app's sync, downloads and memory against the Mac's API.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. Its rules are
PudgeCore code with XCTest (StateBatchTests); the app-side wiring is guarded by reading the
Swift source, the contract by comparing it with the Mac's limit and the protocol document.
"""

from __future__ import annotations

import re

import pytest

from pudge.iphone_service import IPhoneService
from source_policy import source

MODEL = "ios/Pudge/App/PudgeModel.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"
TRANSFERS = "ios/Pudge/Features/Transfers/TransferManager.swift"
PLAYER = "ios/Pudge/Features/Audio/AudioPlayerService.swift"
BATCH = "ios/PudgeCore/Sources/PudgeCore/StateBatch.swift"


def _body(text: str, head: str) -> str:
    return text.split(head, 1)[1].split("\n    }\n", 1)[0]


def _swift_int(text: str, name: str) -> int:
    match = re.search(rf"static let {name} = (\d+)", text)
    assert match, name
    return int(match[1])


def test_state_batches_stay_within_the_macs_limit() -> None:
    batch = source(BATCH)
    limit = _swift_int(batch, "macLimit")
    assert limit == IPhoneService.STATE_BATCH_LIMIT
    assert 0 < _swift_int(batch, "size") <= limit
    assert f"up to {IPhoneService.STATE_BATCH_LIMIT} books" in source("ios/PROTOCOL.md")
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/StateBatchTests.swift")
    for name in (
        "testASmallLibraryIsOneRequestWithItsRevision",
        "testALargeLibraryIsAskedInBatchesWithinTheMacsLimit",
        "testABookPlayingHereIsAskedInFullNextTime",
    ):
        assert f"func {name}()" in tests


def test_the_mac_refuses_a_longer_state_list() -> None:
    service = IPhoneService.__new__(IPhoneService)
    assets = ["00000000-0000-4000-8000-000000000001"] * (IPhoneService.STATE_BATCH_LIMIT + 1)
    with pytest.raises(ValueError):
        service.sync_state(assets)


def test_sync_state_is_batched_and_its_revision_kept_after_the_items() -> None:
    body = _body(
        source(MODEL),
        "private func syncState(_ connection: MacConnection, only: [AudioManifest]? = nil) async throws {",
    )
    assert "StateBatch.batches(polled.map(\\.assetId))" in body
    assert "StateRequest(assets: batch, since: since)" in body
    assert "StateRequest(assets: assets" not in body
    # Not recorded before the items were applied (a failure halfway lost them for good).
    assert "stateRevision = answer.revision" not in body
    kept = body.index("StateBatch.kept(revision")
    assert kept > body.index("try store.mergeBookmarks(item.bookmarks)")


def test_a_new_connection_asks_every_place_in_full_and_checks_over() -> None:
    model = source(MODEL)
    configure = _body(model, "private func configure(_ mac: PairedMac) throws {")
    assert "stateRevision = nil; fullStateAt = 0" in configure
    change = _body(model, "func changeAddress(_ address: String) async {")
    assert change.index("startChecksOver()") > change.index("connection = client")


def test_only_the_open_books_read_along_text_stays_in_memory() -> None:
    body = _body(source(MODEL), "func readAlong(_ book: AudioManifest) -> ReadAlongDocument? {")
    assert "readAlongCache = [resource.resourceId: document]" in body
    assert "readAlongCache[resource.resourceId] = document" not in body


def test_an_orphaned_queued_download_is_paused_at_restore() -> None:
    body = _body(source(TRANSFERS), "func restore() {")
    assert '["queued", "running", "verifying"].contains(record.status)' in body


def test_lock_screen_artwork_is_downsampled_and_retried() -> None:
    body = _body(
        source(PLAYER), "private func currentArtwork(_ book: AudioManifest) -> MPMediaItemArtwork? {"
    )
    assert "DownsampledImage.load(url, maxPixels: AudioPlayerService.artworkPixels)" in body
    assert "UIImage(contentsOfFile:" not in body
    # Remembered only once built: a cover that arrives later is shown (round 5: by its SHA-256,
    # decoded off the main thread, tests/test_fix_rv5_ios.py).
    assert body.index("self.artworkKey = key") > body.index("DownsampledImage.load(")


def test_refreshed_subtitles_never_write_back_a_stale_episode_record() -> None:
    body = _body(source(MEDIA), "private func refreshDownloadedSubtitles(words: Bool = true) async {")
    fetched = body.index('client.media("episodes/\\(item.id)/subtitles"')
    reread = body.index("guard var current = localEpisodes[item.id] else { continue }")
    assert fetched < reread < body.index("localEpisodes[item.id] = current")
