from __future__ import annotations

import subprocess
from pathlib import Path

from pudge.manager_models import LibraryAnime
from pudge.planned_release_discovery import PlannedReleaseDiscovery
from test_web_app import make_api

ROOT = Path(__file__).resolve().parents[1]


def test_later_survives_restart_and_late_worker_without_changing_planning(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    monkeypatch.setattr(api, "_anilist_client", lambda: (_ for _ in ()).throw(AssertionError("Later must remain local")))
    for media_id in (3, 4):
        api.manager.db.upsert_anime(LibraryAnime(media_id, f"Synthetic {media_id}", status="PLANNING"))
        api.manager.planned_release_discovery()._save(media_id, {"phase": "found", "found_at": 100})
    assert api.dismiss_planned_release_offers([3, 3, None, "bad", -1, 999]) == {"ok": True, "dismissed": 1}
    assert api.manager.db.get_anime(3).status == "PLANNING"
    # A worker racing the acknowledgement may write the older found result.
    api.manager.planned_release_discovery()._save(3, {"phase": "found", "found_at": 101})
    restarted = make_api(tmp_path)
    assert [row["media_id"] for row in restarted.poll_planned_release_discovery()["offers"]] == [4]
    assert restarted.dismiss_planned_release_offers([3]) == {"ok": True, "dismissed": 0}
    assert [row["media_id"] for row in PlannedReleaseDiscovery(restarted.manager).offers()] == [4]


def test_later_does_not_dismiss_unshown_or_unreleased_entries(tmp_path):
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(LibraryAnime(5, "Synthetic future", status="PLANNING"))
    discovery = api.manager.planned_release_discovery()
    discovery._save(5, {"phase": "armed", "airing_at": 2_100_000_000})
    assert api.dismiss_planned_release_offers([5]) == {"ok": True, "dismissed": 0}
    assert discovery._state(5)["phase"] == "armed"


def test_later_ui_waits_for_acknowledgement_and_retries_errors():
    subprocess.run(["node", str(ROOT / "tests/js/planned_release_later.cjs"), str(ROOT / "pudge/web/index.html")], check=True, capture_output=True, text=True, timeout=15)


def test_assistant_fits_saved_geometry_window_resize_drag_and_reopen():
    subprocess.run(["node", str(ROOT / "tests/js/assistant_geometry.cjs"), str(ROOT / "pudge/web/reading_assistant.js")], check=True, capture_output=True, text=True, timeout=15)
    css = (ROOT / "pudge/web/reading_assistant.css").read_text()
    assert ".pudge-assistant{position:fixed;box-sizing:border-box" in css
    assert "min-width:300px" not in css and "min-height:260px" not in css
