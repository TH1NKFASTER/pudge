"""Tab activation stays local and coalesced (Manga/Audiobooks/VN off the critical path)."""

from __future__ import annotations

import threading
import types
from pudge.web_app import WebAppApi


def _api() -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    api.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    return api


def test_manga_state_never_waits_for_anilist() -> None:
    api = _api()
    gate = threading.Event()
    calls: list[str] = []
    api.config = types.SimpleNamespace(anilist=types.SimpleNamespace(enabled=True, access_token="t"))
    api.manga = types.SimpleNamespace(
        state=lambda: {"books": [{"id": 1, "anilist_id": 5, "mean_score": None}]},
        set_mean_scores=lambda s: True,
    )

    def slow_post(*_a, **_k):
        calls.append("anilist")
        gate.wait(5)
        return {"Page": {"media": []}}

    api.light_novels = types.SimpleNamespace(_anilist_post=slow_post)
    api._manga_state_payload = lambda state: {"books": state["books"]}
    result = api.manga_state()  # returns while AniList is still "in flight"
    assert result["books"][0]["id"] == 1
    api._manga_backfill_thread.join(0.2)
    assert api._manga_backfill_thread.is_alive(), "backfill runs in the background"
    gate.set()
    api._manga_backfill_thread.join(2)
    assert calls == ["anilist"]


def test_audiobook_state_reads_display_state_and_queues_maintenance_off_call() -> None:
    api = _api()
    seen: list[bool] = []
    api.audiobooks = types.SimpleNamespace(
        state=lambda *, queue_missing=True: seen.append(queue_missing) or {"books": []}
    )
    api.audiobook_state()
    api.audiobook_state()
    for thread in threading.enumerate():
        if thread.name.endswith("audiobook-queue-missing"):
            thread.join(2)
    assert seen.count(False) == 2 and seen.count(True) == 1, "maintenance at most once a minute"


def test_visual_novel_windows_are_cached_briefly_and_forced_on_refresh() -> None:
    api = _api()
    calls: list[int] = []
    api.visual_novels = types.SimpleNamespace(windows=lambda: calls.append(1) or [{"id": 7, "label": "VN"}])
    assert api.visual_novel_windows() == [{"id": 7, "label": "VN"}]
    api.visual_novel_windows()
    assert len(calls) == 1
    api.visual_novel_windows(True)
    assert len(calls) == 2


def test_rapid_tab_switching_starts_one_loader_per_page_and_skips_left_pages(page) -> None:
    # The backend is slow: no Manga/Audiobooks/VN/Statistics state ever arrives.
    result = page.run(
        r"""
        await __tick(3000);
        const mark = __apiCalls.length;
        const go = page => __click(`.nav button[data-page="${page}"]`);
        await go('manga'); await go('visualnovels'); await go('audiobooks');     // left before the next frame
        await __tick(50);
        await go('manga'); await __tick(50);
        await go('statistics'); await __tick(50);
        await go('manga'); await __tick(50);                                     // its first load is still running
        const loads = {};
        for (const call of __apiCalls.slice(mark)) loads[call.name] = (loads[call.name] || 0) + 1;
        return {page: ui.page, loads};
        """,
        before="""
        for (const name of ['manga_state', 'audiobook_state', 'visual_novel_windows', 'visual_novel_state', 'consumption_statistics_query'])
          __api[name] = () => new Promise(() => {});
        """,
    )
    assert result.value["page"] == "manga"
    # Manga and Visual Novels, left before their frame, loaded nothing then; Audiobooks was
    # open at the frame; the third Manga visit reused the still-running Manga load.
    loads = result.value["loads"]
    assert {
        name: loads.get(name, 0)
        for name in (
            "manga_state",
            "audiobook_state",
            "visual_novel_windows",
            "visual_novel_state",
            "consumption_statistics_query",
        )
    } == {
        "manga_state": 1,
        "audiobook_state": 1,
        "visual_novel_windows": 0,
        "visual_novel_state": 0,
        "consumption_statistics_query": 1,
    }
