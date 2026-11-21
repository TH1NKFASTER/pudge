"""A finished anime still downloaded on the phone gets no card (not under Caught up).

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The shelf rule is
PudgeCore code with XCTest (LibraryCardsTests); it is guarded here by reading the Swift source.
"""

from __future__ import annotations

from source_policy import source


def test_groups_the_mac_does_not_list_get_no_card_with_a_new_mac() -> None:
    cards = source("ios/PudgeCore/Sources/PudgeCore/LibraryCards.swift")
    assert "for group in idle where !used.contains(group.id) && mac == nil {" in cards
    tests = source("ios/PudgeCore/Tests/PudgeCoreTests/LibraryCardsTests.swift")
    assert "XCTAssertEqual(shelves[2].entries.map(\\.mediaId), [2, 3])" in tests


def test_the_anime_list_follows_the_macs_downloads_while_the_app_is_in_front() -> None:
    app = source("ios/Pudge/App/PudgeApp.swift")
    assert ".task(id: phase) { if phase == .active { await model.media.liveRefreshLoop() } }" in app
    media = source("ios/Pudge/Features/Media/MediaModel.swift")
    loop = media.split("func liveRefreshLoop() async {", 1)[1].split("\n    }\n", 1)[0]
    assert "AnimeLiveRefresh.interval(home: macHome)" in loop
    # IOSPERF: the conditions moved to AnimeLiveRefresh.shouldRefresh (also no reader open or
    # just touched), and a tick is the quiet "changed since" refresh.
    assert (
        "AnimeLiveRefresh.shouldRefresh(macAvailable: app.macAvailable, playerOnScreen: playerOnScreen,"
        in loop
    )
    assert "await refreshEpisodes(live: true)" in loop
