"""SEQPRE on the phone: the next entry is asked when the final episode's player opens, kept for a
while per title, and the sheet shows it at once (or reuses the request still on its way).

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The cache rule is
PudgeCore code with XCTest (AnimeFinaleTests.testNextEntriesFetchedAheadServeTheSheetForAWhile);
the model's use of it is guarded here by reading the Swift source.
"""

from __future__ import annotations

from source_policy import source

SHEET = "ios/Pudge/UI/FinaleSheet.swift"


def test_the_player_opening_a_final_episode_asks_ahead() -> None:
    model = source("ios/Pudge/Features/Media/MediaModel.swift")
    assert (
        "func playerOpened(_ id: Int) { playingEpisodes.insert(id); app.videoOnScreen = true; finales.playerOpened(id) }"
        in model
    )
    sheet = source(SHEET)
    opened = sheet.split("func playerOpened(_ id: Int) {", 1)[1].split("\n    }\n", 1)[0]
    assert "AnimeFinaleRule.local(" in opened
    assert "sequelRequest(mediaId)" in opened
    assert "sequelTasks[mediaId] == nil" in opened


def test_the_sheet_uses_the_answer_or_the_running_request() -> None:
    sheet = source(SHEET)
    load = sheet.split("private func loadSequel(_ finale: AnimeFinale) async {", 1)[1].split("\n    }\n", 1)[
        0
    ]
    # Offline: the message as before, first.
    assert load.index("!media.app.macOffline") < load.index("sequels.lookup(")
    assert "The next entry shows when the Mac answers." in load
    assert "if case .hit(let ready) = sequels.lookup(finale.mediaId" in load
    assert "sequelRequest(finale.mediaId)" in load
    assert "try await request.value" in load
    request = sheet.split("private func sequelRequest(_ mediaId: Int)", 1)[1].split("\n    }\n", 1)[0]
    assert "if let running = sequelTasks[mediaId] { return running }" in request
    assert "self.sequels.store(mediaId, sequel: value" in request


def test_an_action_replaces_the_answer_asked_ahead() -> None:
    sheet = source(SHEET)
    act = sheet.split("func act(_ action: String) async {", 1)[1].split("\n    }\n", 1)[0]
    assert "sequels.store(finale.mediaId, sequel: next" in act
    assert "sequels.forget(finale.mediaId)" in act
    core = source("ios/PudgeCore/Sources/PudgeCore/AnimeFinale.swift")
    assert "public struct FinaleSequelCache: Sendable {" in core
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/AnimeFinaleTests.swift")
    assert "func testNextEntriesFetchedAheadServeTheSheetForAWhile()" in tests
