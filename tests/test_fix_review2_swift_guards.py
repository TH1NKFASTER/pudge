"""External review round 2, iPhone/iPad app (Swift).

policy: the iPhone/iPad app (Swift) builds and runs only in Xcode on the Mac. The decisions are
PudgeCore code with XCTest (MacReachabilityTests, AnimeFinaleTests); their use is guarded here
by reading the Swift source:

1. A failed sync marks the Mac unavailable only for a network failure (MacFailure.unreachable):
   an HTTP error or an unreadable answer came from a Mac that is there.
2. A finale score is dropped only when the Mac refuses it for good (FinaleRefusal.final), not
   on any 4xx (auth, conflict, rate limit are sent again).
"""

from __future__ import annotations

import re

from source_policy import source


def _flat(relative: str) -> str:
    return re.sub(r"\s+", " ", source(relative))


def test_only_a_network_failure_marks_the_mac_unavailable() -> None:
    model = _flat("ios/Pudge/App/PudgeModel.swift")
    assert "if MacFailure.unreachable(error) { macAvailable = false }" in model
    assert "} catch { macAvailable = false; syncIssue = error.localizedDescription }" not in model
    assert "public enum MacFailure" in source("ios/PudgeCore/Sources/PudgeCore/MacReachability.swift")
    assert "func testOnlyANetworkFailureMeansTheMacIsNotThere()" in source(
        "ios/PudgeCore/Tests/PudgeCoreTests/MacReachabilityTests.swift"
    )


def test_a_score_is_dropped_only_when_refused_for_good() -> None:
    sheet = _flat("ios/Pudge/UI/FinaleSheet.swift")
    assert "catch let refusal as MacRefusal where FinaleRefusal.final(refusal.status)" in sheet
    assert "(400..<500).contains(refusal.status)" not in sheet
    assert "func testAScoreIsDroppedOnlyWhenRefusedForGood()" in source(
        "ios/PudgeCore/Tests/PudgeCoreTests/AnimeFinaleTests.swift"
    )
