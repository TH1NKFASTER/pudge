"""Tab activation stays local and coalesced (Manga/Audiobooks/VN off the critical path)."""

from __future__ import annotations

import json
import subprocess
import threading
import types
from pathlib import Path

from pudge.web_app import WebAppApi

ROOT = Path(__file__).resolve().parents[1]


def _api() -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    api.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    return api


def test_manga_state_never_waits_for_anilist() -> None:
    api = _api()
    gate = threading.Event()
    calls: list[str] = []
    api.config = types.SimpleNamespace(anilist=types.SimpleNamespace(enabled=True, access_token="t"))
    api.manga = types.SimpleNamespace(state=lambda: {"books": [{"id": 1, "anilist_id": 5, "mean_score": None}]}, set_mean_scores=lambda s: True)

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
    api.audiobooks = types.SimpleNamespace(state=lambda *, queue_missing=True: seen.append(queue_missing) or {"books": []})
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


def test_rapid_tab_switching_starts_one_loader_per_page_and_skips_left_pages() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    start = html.index("function loadActivatedPage(page){")
    load_fn = html[start : html.index("\nfunction setPage(", start)]
    sp = html.index("function setPage(page,force=false){")
    set_page = html[sp : html.index("\nfunction updateCount(", sp)]
    script = f"""
const frames=[];global.requestAnimationFrame=fn=>frames.push(fn);global.performance={{now:()=>0}};
const calls={{}};const pending={{}};
const mk=name=>()=>{{calls[name]=(calls[name]||0)+1;return new Promise(r=>{{pending[name]=r;}});}};
global.window={{PudgeMangaReaderV2:{{renderLibrary:mk('manga')}},PudgeMedia:{{loadAudio:mk('audio')}},PudgeVisualNovels:{{load:mk('vn')}},PudgeStatistics:{{load:mk('stats')}}}};
global.loadLightNovels=mk('ln');global.logUiEvent=()=>{{}};
global.ui={{page:'current'}};global.document={{querySelectorAll:()=>[]}};global.$=()=>({{textContent:'',scrollTop:0}});global.t=x=>x;
global.clearLnSelection=()=>{{}};global.updatePageSelectionActions=()=>{{}};global.updateCount=()=>{{}};global.queuePolychromeWake=()=>{{}};
{load_fn}
{set_page}
(async()=>{{
  setPage('manga');setPage('visualnovels');setPage('audiobooks');   // left before their frames
  frames.splice(0).forEach(fn=>fn());
  setPage('manga');frames.splice(0).forEach(fn=>fn());
  setPage('statistics');frames.splice(0).forEach(fn=>fn());
  setPage('manga');frames.splice(0).forEach(fn=>fn());               // manga load still running -> reused
  process.stdout.write(JSON.stringify({{calls,page:ui.page}}));
}})();
"""
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert result["page"] == "manga"
    # manga/vn left before their frame never loaded; audiobooks was active at the
    # frame; the third manga visit reused the still-running manga load.
    assert result["calls"] == {"audio": 1, "manga": 1, "stats": 1}, result
