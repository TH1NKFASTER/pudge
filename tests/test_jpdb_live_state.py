"""S3: live jpdb card state (lookup-vocabulary) instead of the parse snapshot."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.web_app import WebAppApi


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


def _fake_lookup(calls: list[Any], info: list[Any]):
    def fake(action, payload=None):
        calls.append((action, payload))
        assert action == "lookup-vocabulary"
        return {"vocabulary_info": info[: len(payload["list"])]}

    return fake


def test_jpdb_live_states_distinguish_unknown_id_outside_deck_and_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    calls: list[Any] = []
    info = [[["redundant", "known"]], [None], None, [["due"]]]
    monkeypatch.setattr(service, "_jpdb_request", _fake_lookup(calls, info))
    rows = service.jpdb_live_states([(1, 10), (2, 20), (1, 10), (0, 5), (3, 30), (4, 40)])
    assert calls == [("lookup-vocabulary", {"list": [[1, 10], [2, 20], [3, 30], [4, 40]], "fields": ["card_state"]})]
    by_id = {row["wordId"]: row for row in rows}
    assert by_id[1]["ok"] is True and by_id[1]["normalizedState"] == "known"
    assert by_id[1]["states"] == ["redundant", "known"] and by_id[1]["inDeck"] is True
    assert by_id[2]["ok"] is True and by_id[2]["inDeck"] is False and by_id[2]["normalizedState"] == "new"
    # Unknown vid/sid is never reported as "New".
    assert by_id[3] == {"wordId": 3, "readingIndex": 30, "idNamespace": "jpdb", "provider": "jpdb", "ok": False, "reason": "unknown_id"}
    assert by_id[4]["normalizedState"] == "due"
    assert all(row["idNamespace"] == "jpdb" for row in rows)


def test_jpdb_live_states_require_token(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(LightNovelError):
        service.jpdb_live_states([(1, 1)])


def _api(service: LightNovelService) -> Any:
    api = types.SimpleNamespace(light_novels=service)
    for name in ("_jpdb_account_scope", "_jpdb_study_states", "_jpdb_study_state"):
        setattr(api, name, types.MethodType(getattr(WebAppApi, name), api))
    return api


def test_bridge_routes_jpdb_states_with_account_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})
    monkeypatch.setattr(service, "_jpdb_request", _fake_lookup([], [[["known"]]]))
    monkeypatch.setattr(service, "jiten_live_states", lambda *a, **k: pytest.fail("Jiten must not be used"))
    api = _api(service)
    scope = service.study_provider_capabilities("jpdb")["account_key"]
    assert scope

    batch = WebAppApi.study_states(api, {"backend": "jpdb", "words": [[7, 1]]})
    assert batch["ok"] is True and batch["provider"] == "jpdb" and batch["account_scope"] == scope
    assert batch["states"][0]["normalizedState"] == "known"

    one = WebAppApi.study_state(api, {"backend": "jpdb", "word_id": 7, "reading_index": 1})
    assert one["ok"] is True and one["account_scope"] == scope and one["wordId"] == 7


def test_bridge_jpdb_failure_is_not_a_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    service.save_settings({"jpdb_api_token": "tok-a", "study_backend": "jpdb"})

    def boom(*_a, **_k):
        raise LightNovelError("JPDB request failed: timeout")

    monkeypatch.setattr(service, "_jpdb_request", boom)
    api = _api(service)
    batch = WebAppApi.study_states(api, {"backend": "jpdb", "words": [[7, 1]]})
    assert batch["ok"] is False and batch["states"] == []
    assert WebAppApi.study_state(api, {"backend": "jpdb", "word_id": 7, "reading_index": 1})["ok"] is False


def test_jpdb_live_state_js() -> None:
    import subprocess

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", str(root / "tests/js/jpdb_live_state_s3.cjs"), str(root / "pudge/web/reading_tools.js")],
        cwd=root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "jpdb live state S3: PASS" in result.stdout


def test_review_and_add_refresh_jpdb_native_state_after_mutation() -> None:
    js = (Path(__file__).resolve().parents[1] / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "String(current.backend || '').toLowerCase() === 'jiten') invalidateJitenStatePair" not in js
    assert js.count("if (liveStateBackend(current)) invalidateJitenStatePair(") == 2
    assert "lookupLiveStates([[id, reading]], {force:true, backend})" in js


def test_why_not_ready_has_no_dead_open_maintenance_button() -> None:
    html = (Path(__file__).resolve().parents[1] / "pudge/web/index.html").read_text(encoding="utf-8")
    assert 'data-action="open-activity"' not in html
    assert "action==='open-activity'" not in html
    assert 'data-action="find-diagnostic-release"' in html
