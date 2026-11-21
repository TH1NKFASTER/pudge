"""The window's first state failed with "interface error" at start-up: PyObjC resolves
Quartz functions lazily, two threads resolving CGPreflightScreenCaptureAccess at once made
the second one raise KeyError (a getattr default does not catch it), and get_state failed.
The lookup is now done under a lock with one retry, and a failing lookup means "unknown".
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from pudge import visual_novels
from test_web_app import make_api


class _LazyQuartz(types.ModuleType):
    def __init__(self, failures: int) -> None:
        super().__init__("Quartz")
        self.failures = failures

    def __getattr__(self, name: str):
        if name != "CGPreflightScreenCaptureAccess":
            raise AttributeError(name)
        if self.failures > 0:
            self.failures -= 1
            raise KeyError(name)  # what PyObjC's lazy import raises in the race
        return lambda: True


def _darwin_with(monkeypatch, quartz: types.ModuleType) -> None:
    monkeypatch.setattr(visual_novels.platform, "system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "Quartz", quartz)


def test_a_lookup_that_loses_the_race_is_retried(monkeypatch) -> None:
    _darwin_with(monkeypatch, _LazyQuartz(failures=1))
    assert visual_novels.VisualNovelService._screen_recording_access() is True


def test_a_lookup_that_keeps_failing_is_unknown_and_the_state_still_loads(
    tmp_path: Path, monkeypatch
) -> None:
    _darwin_with(monkeypatch, _LazyQuartz(failures=10))
    assert visual_novels.VisualNovelService._screen_recording_access() is None
    api = make_api(tmp_path)
    state = api.get_state()
    assert state["settings"]["screen_recording_allowed"] is None
