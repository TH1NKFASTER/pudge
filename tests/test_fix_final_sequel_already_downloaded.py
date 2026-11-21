from __future__ import annotations

from types import SimpleNamespace

from pudge.web_app import WebAppApi


def _api(local_files: dict[int, int]) -> WebAppApi:
    graph = {
        "nodes": [
            {"media_id": 1, "title": "Root"},
            {
                "media_id": 2,
                "title": "Sequel",
                "start_date": "2026-04-01",
                "media_status": "FINISHED",
                "list_status": "PLANNING",
            },
        ],
        "edges": [{"source": 1, "target": 2, "relation_type": "SEQUEL"}],
    }
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(relation_graph=lambda *_a, **_k: graph, downloads_enabled=lambda: True)
    api._local_file_count = lambda media_id: local_files.get(int(media_id), 0)
    return api


def test_sequel_already_in_library_offers_no_download() -> None:
    sequel = _api({2: 12}).final_sequel_option(1)["sequel"]

    assert sequel["can_download"] is False
    assert sequel["downloaded"] is True
    assert sequel["can_watching"] is True


def test_sequel_without_files_still_offers_download() -> None:
    sequel = _api({}).final_sequel_option(1)["sequel"]

    assert sequel["can_download"] is True
    assert "downloaded" not in sequel


def test_download_click_on_downloaded_sequel_is_refused() -> None:
    result = _api({2: 3}).download_final_sequel(1)

    assert result["ok"] is False
    assert result["reason"] == "already_downloaded"


def test_files_landing_after_warmup_hide_download() -> None:
    files: dict[int, int] = {}
    api = _api(files)
    assert api.final_sequel_option(1)["sequel"]["can_download"] is True
    files[2] = 1
    assert api.final_sequel_option(1)["sequel"]["can_download"] is False
