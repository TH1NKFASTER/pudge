"""iOS review round 4: what the phone app decodes and assumes, checked against the Mac.

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac, so the Swift side
is read as text: the contracts both sides share (required JSON fields, limits, the finished
rule) and the wiring fixed in this round.
"""

from __future__ import annotations

import re

from pudge import audiobook_finish
from pudge.iphone_service import IPhoneService
from source_policy import source, sources

CORE = "ios/PudgeCore/Sources/PudgeCore/"
APP = "ios/Pudge/"
MEDIA = "pudge/iphone_media.py"
SERVICE = "pudge/iphone_service.py"


def _read(relative: str) -> str:
    return source(relative)


def _struct(source: str, name: str) -> str:
    start = source.index(f"public struct {name}:")
    end = source.index("\n}", start)
    return source[start:end]


def _required_fields(source: str, name: str) -> set[str]:
    """Stored properties the synthesized decoder needs (not optional, no default)."""
    fields = set()
    for field, kind in re.findall(r"^\s+public (?:let|var) (\w+): ([^=\n{]+)$", _struct(source, name), re.M):
        if not kind.strip().endswith("?"):
            fields.add(field)
    return fields


def _keys(relative: str, name: str) -> set[str]:
    """JSON keys a Mac method writes (its dict literals)."""
    text = source(relative)
    start = text.index(f"    def {name}(")
    end = text.find("\n    def ", start + 1)
    return set(re.findall(r'"(\w+)":', text[start : end if end > 0 else len(text)]))


def test_the_macs_lists_carry_every_field_the_phone_requires() -> None:
    contracts = _read(CORE + "MediaContracts.swift")
    novel = _required_fields(contracts, "LightNovelItem")
    assert {"id", "title", "chapter", "offset", "finished"} <= novel
    assert novel <= _keys(MEDIA, "light_novels")
    # Optional on the phone (older Macs), sent by this Mac: the "Mark light novel read" offer.
    assert "audioAssetIds" in _keys(MEDIA, "light_novels")
    assert "public var audioAssetIds: [String]? = nil" in contracts
    manga = _required_fields(contracts, "MangaItem")
    assert {"id", "pages", "position"} <= manga
    assert manga <= _keys(MEDIA, "manga_list")
    episode = _required_fields(contracts, "EpisodeItem")
    assert {"id", "mediaId", "watched", "prepared"} <= episode
    assert episode <= _keys(MEDIA, "_episode_item")


def test_the_progress_batch_stays_within_the_macs_limit() -> None:
    batch = _read(CORE + "StateBatch.swift")
    limit = int(re.search(r"static let macLimit = (\d+)", batch)[1])
    size = int(re.search(r"static let size = (\d+)", batch)[1])
    assert limit == IPhoneService.STATE_BATCH_LIMIT
    assert 0 < size <= limit
    # The answer the phone decodes (StateAnswer: revision, unchanged, items).
    keys = _keys(SERVICE, "sync_state")
    assert {"revision", "unchanged", "items", "assetId", "progress", "bookmarks"} <= keys
    model = _read(APP + "App/PudgeModel.swift")
    assert (
        "struct StateAnswer: Decodable { let revision: String; let unchanged: Bool; let items: [StateItem] }"
        in model
    )


def test_the_finished_rule_is_the_macs() -> None:
    cards = _read(CORE + "LibraryCards.swift")
    fraction = float(re.search(r"static let fraction = ([\d.]+)", cards)[1])
    tail = int(re.search(r"static let tailMs: Int64 = ([\d_]+)", cards)[1].replace("_", ""))
    assert fraction == audiobook_finish.FINISHED_FRACTION
    assert tail == audiobook_finish.FINISHED_TAIL_SECONDS * 1000
    for position, duration in [(3528.0, 3600.0), (3500.0, 3600.0), (96.0, 100.0), (0.0, 0.0), (10.0, 12.0)]:
        swift = duration > 0 and (position >= duration * fraction or (duration - position) * 1000 <= tail)
        assert swift == audiobook_finish.reached_end(position, duration)


def test_a_finished_flag_set_here_follows_the_mac_also_after_an_unchanged_answer() -> None:
    model = _read(APP + "App/PudgeModel.swift")
    # An unchanged answer carries no items: the Mac's flag already here is its answer.
    assert (
        "if answer.unchanged { for asset in batch where progress[asset] != nil { adoptMacFinished(asset) } }"
        in model
    )
    # Mark unfinished at the very end starts over in the open player too.
    unfinish = model[model.index("func markUnfinished") : model.index("private func adoptMacFinished")]
    assert 'try await player.seek(0, reason: "unfinish")' in unfinish
    assert "AudiobookFinish.reachedEnd(positionMs: player.positionMs" in unfinish


def test_a_damaged_kept_catalog_is_asked_again_in_full() -> None:
    model = _read(APP + "App/PudgeModel.swift")
    refresh = model[model.index("private func refreshOnce") : model.index("private func moveCarriedPlaces")]
    # The closure awaits: its signature says so.
    assert (
        "{ () async throws -> (catalog: CatalogResponse, answer: CatalogCache.Answer, etag: String?) in"
        in refresh
    )
    assert "if !kept.unchanged { throw error }" in refresh
    assert refresh.count("connection.dataIfChanged(route, etag: nil)") == 1
    # Kept only after it was applied, from a full answer.
    assert refresh.index("try store.saveManifest(book)") < refresh.index("cache.keep(route: route")
    # The Mac side of the same flow.
    http = _read("pudge/iphone_http.py")
    assert '"catalog-etag"' in http and "If-None-Match" in http


def test_subtitle_writes_and_reads_off_the_main_thread_survive_a_delete() -> None:
    media = _read(APP + "Features/Media/MediaModel.swift")
    write = media[media.index("private func writeSubtitles(") : media.index("private func writeSubtitlesNow")]
    assert "{ () async -> Bool in" in write
    # A cancelled write of the chain removes what an earlier one left after the delete.
    waited = write.index("_ = await previous?.value")
    assert (
        "if Task.isCancelled { try? FileManager.default.removeItem(at: url); return false }" in write[waited:]
    )
    assert "subtitleWrites.removeValue(forKey: id)?.cancel()" in media
    watch = _read(APP + "UI/WatchScreen.swift")
    appear = watch[watch.index("media.playerOpened(episode.id)") :]
    assert appear.index("playerClosed = false") < appear.index("guard !playerClosed else { return }")
    assert "playerClosed = true" in watch


def test_restored_transfers_started_meanwhile_stay_running() -> None:
    transfers = _read(APP + "Features/Transfers/TransferManager.swift")
    restore = transfers[transfers.index("func restore()") : transfers.index("func updateConnection")]
    assert '["queued", "running", "verifying"].contains(record.status)' in restore
    # Round 5: checked and saved under the record lock a start holds (tests/test_fix_rv5_ios.py).
    assert "if !active.contains(listed.id), !started," in restore


def test_every_app_source_is_in_the_xcode_project() -> None:
    project = _read("ios/Pudge.xcodeproj/project.pbxproj")
    app = sources("ios/Pudge/**/*.swift")
    assert len(app) >= 19
    for path in app:
        assert path.removeprefix("ios/") in project, path
    # The pinned sessions stay inside MacConnection (no task on an invalidated session).
    assert "private let session: URLSession" in app["ios/Pudge/Networking/MacConnection.swift"]
    for path, text in app.items():
        if not path.endswith("MacConnection.swift"):
            assert "connection.session" not in text and "client.session" not in text, path
