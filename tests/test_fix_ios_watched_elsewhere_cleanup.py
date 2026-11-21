"""iPhone/iPad: an episode downloaded here but watched on the Mac or another device is deleted
after the usual delay (Swift; runs only in Xcode / `swift test` on the Mac).

The copy took the Mac's watch (adoptMacWatches): marked watched and its place reset to the start,
so it would not be offered as "Continue". The cleanup then read that place as "being watched
again" (under 85 %) and kept the download for good. Now such a copy is marked watched elsewhere
and goes after the delay unless it is opened here again; copies adopted by earlier builds are
recognised (counted on the Mac, nothing played here, place at the start). The cleanup also runs
right after the adoption."""

from __future__ import annotations

from source_policy import source

CORE = "ios/PudgeCore/Sources/PudgeCore/WatchedCleanup.swift"
TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/WatchedCleanupTests.swift"
MEDIA = "ios/Pudge/Features/Media/MediaModel.swift"


def test_the_core_rule_takes_a_watch_from_elsewhere() -> None:
    core = source(CORE)
    assert "public var watchedElsewhere: Bool" in core and "watchedElsewhere: Bool = false" in core
    assert "let untouched = state.watchedElsewhere && state.positionMs == 0" in core
    assert "guard untouched || Double(state.positionMs) >= Double(state.durationMs) * watchingBelow" in core
    assert "func testAnEpisodeWatchedOnAnotherDeviceGoesAfterTheDelay()" in source(TESTS)


def test_the_phone_marks_the_adopted_watch_and_cleans_up_after_it() -> None:
    media = source(MEDIA)
    adopt = media[media.index("private func adoptMacWatches() {") :]
    adopt = adopt[: adopt.index("\n    }\n")]
    assert "local.watchedElsewhere = true" in adopt and "local.positionMs = 0" in adopt
    refresh = media[media.index("            adoptMacWatches()\n") :][:300]
    assert "deleteWatchedEpisodes()" in refresh
    assert "watchedElsewhere: Self.watchedElsewhere(item))" in media
    rule = media[media.index("static func watchedElsewhere(_ item: LocalEpisode) -> Bool {") :][:400]
    assert "if let marked = item.watchedElsewhere { return marked }" in rule
    assert (
        "item.watchedSent == true && item.positionMs == 0 && (item.watchedSeconds?.isEmpty ?? true)" in rule
    )
