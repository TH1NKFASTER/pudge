from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.review_actions import (
    ReviewActionError,
    action_set_payload,
    actions_for,
    binding_conflicts,
    effective_bindings,
    normalize_shortcuts,
    resolve_wire_grade,
)
from pudge.review_providers import JITEN_API_BASE, JPDB_API_BASE

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    ("jiten", "native"): [("again", "again"), ("hard", "hard"), ("good", "good"), ("easy", "easy")],
    ("jiten", "binary"): [("fail", "again"), ("pass", "good")],
    ("jpdb", "native"): [
        ("nothing", "nothing"), ("something", "something"), ("hard", "hard"),
        ("okay", "okay"), ("easy", "easy"),
    ],
    ("jpdb", "binary"): [("fail", "fail"), ("pass", "pass")],
}


@pytest.mark.parametrize(("profile", "expected"), EXPECTED.items())
def test_action_sets_have_exact_buttons_and_wire_grades(profile, expected) -> None:
    actions = actions_for(*profile)
    assert [(a.id, a.wire) for a in actions] == expected
    assert [a.default_key for a in actions] == [str(i) for i in range(1, len(actions) + 1)]


@pytest.mark.parametrize(
    ("provider", "mode", "requested", "wire"),
    [(p, m, action_id, wire) for (p, m), rows in EXPECTED.items() for action_id, wire in rows],
)
def test_resolve_wire_grade_for_every_profile(provider, mode, requested, wire) -> None:
    assert resolve_wire_grade(provider, mode, requested) == wire


@pytest.mark.parametrize(
    ("provider", "mode", "requested"),
    [
        ("jiten", "binary", "hard"),
        ("jiten", "binary", "again"),  # wire grade of Fail, not an action id
        ("jiten", "native", "nothing"),
        ("jpdb", "native", "good"),
        ("jpdb", "native", "fail"),
        ("jpdb", "binary", "okay"),
        ("jpdb", "binary", ""),
    ],
)
def test_hidden_or_foreign_actions_are_rejected(provider, mode, requested) -> None:
    with pytest.raises(ReviewActionError):
        resolve_wire_grade(provider, mode, requested)


def test_normalize_shortcuts_keeps_only_known_valid_bindings() -> None:
    raw = {
        "jiten:native:again": "q",
        "jpdb:native:something": "",
        "jiten:binary:pass": "Shift+2",
        "jiten:native:bogus": "x",
        "other:native:again": "x",
        "jpdb:binary:fail": "Space",  # reserved for confirm
        "jpdb:binary:pass": "Esc",
        "jiten:native:easy": "a b",
    }
    assert normalize_shortcuts(json.dumps(raw)) == {
        "jiten:native:again": "q",
        "jpdb:native:something": "",
        "jiten:binary:pass": "Shift+2",
    }
    assert normalize_shortcuts("not json") == {}


def test_conflicts_only_inside_one_active_profile() -> None:
    # 1 is used by both native Again and binary Fail: different profiles, fine.
    assert binding_conflicts({}) == []
    clash = binding_conflicts({"jiten:native:hard": "1"})
    assert clash == [{"provider": "jiten", "mode": "native", "shortcut": "1", "actions": ["again", "hard"]}]
    # Disabling one side resolves the clash.
    assert binding_conflicts({"jiten:native:hard": "1", "jiten:native:again": ""}) == []


def test_effective_bindings_and_payload_use_defaults_and_overrides() -> None:
    shortcuts = {"jpdb:native:nothing": "z", "jpdb:native:easy": ""}
    assert effective_bindings("jpdb", "native", shortcuts) == {
        "nothing": "z", "something": "2", "hard": "3", "okay": "4", "easy": "",
    }
    payload = action_set_payload("jpdb", "binary", shortcuts)
    assert [a["id"] for a in payload["actions"]] == ["fail", "pass"]
    assert [a["shortcut"] for a in payload["actions"]] == ["1", "2"]


def test_js_default_table_mirrors_python() -> None:
    script = (
        "globalThis.document={documentElement:{lang:'en'}};"
        f"require({json.dumps(str(ROOT / 'pudge/web/review_actions.js'))});"
        "process.stdout.write(JSON.stringify(globalThis.PudgeReviewActions.DEFAULT_SETS));"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    js = json.loads(result.stdout)
    for (provider, mode), actions in {k: actions_for(*k) for k in EXPECTED}.items():
        assert js[f"{provider}:{mode}"] == [
            [a.id, a.wire, a.label_en, a.label_ru, a.tone, a.default_key] for a in actions
        ]


def test_review_actions_js_behaviour() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/review_actions_s2.cjs"), str(ROOT / "pudge/web")],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "review actions S2: PASS" in result.stdout


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


def test_mode_and_all_profiles_persist_across_restart(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.settings_payload()["review_mode"] == "native"  # default keeps old workflow
    service.save_settings({
        "review_mode": "binary",
        "review_shortcuts": json.dumps({"jiten:binary:fail": "f", "jpdb:native:easy": "e"}),
    })
    reopened = LightNovelService(service.config).settings_payload()
    assert reopened["review_mode"] == "binary"
    assert reopened["review_shortcuts"] == {"jiten:binary:fail": "f", "jpdb:native:easy": "e"}
    sets = reopened["review_action_sets"]
    assert [a["shortcut"] for a in sets["jiten:binary"]["actions"]] == ["f", "2"]
    # Hidden profile is kept while binary is active.
    assert sets["jpdb:native"]["actions"][-1]["shortcut"] == "e"
    assert len(sets["jpdb:native"]["actions"]) == 5
    # Unrelated save keeps mode and bindings.
    service.save_settings({"show_furigana": False})
    assert LightNovelService(service.config).settings_payload()["review_mode"] == "binary"


def test_conflicting_shortcuts_are_rejected_on_save(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(LightNovelError, match="assigned to both"):
        service.save_settings({"review_shortcuts": {"jpdb:native:okay": "1"}})
    assert service.settings_payload()["review_shortcuts"] == {}


class _Response:
    def __init__(self, payload=None) -> None:
        self.status_code = 200
        self._payload = payload or {}
        self.content = b"{}"

    def json(self):
        return self._payload


@pytest.mark.parametrize(
    ("backend", "mode", "action", "expected"),
    [
        ("jiten", "native", "again", {"rating": 1}),
        ("jiten", "native", "easy", {"rating": 4}),
        ("jiten", "binary", "fail", {"rating": 1}),
        ("jiten", "binary", "pass", {"rating": 3}),
        ("jpdb", "native", "nothing", {"grade": "nothing"}),
        ("jpdb", "native", "something", {"grade": "something"}),
        ("jpdb", "native", "okay", {"grade": "okay"}),
        ("jpdb", "binary", "fail", {"grade": "fail"}),
        ("jpdb", "binary", "pass", {"grade": "pass"}),
    ],
)
def test_review_payload_per_provider_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend, mode, action, expected
) -> None:
    service = _service(tmp_path)
    service.save_settings({
        "jiten_api_key": "jk", "jpdb_api_token": "jt", "study_backend": backend, "review_mode": mode,
    })
    sent: list[tuple[str, dict]] = []

    def fake_post(url, *, headers, json, timeout):
        sent.append((url, dict(json)))
        return _Response({"success": True})

    monkeypatch.setattr("pudge.review_providers.httpx.post", fake_post)
    result = service.study_action(backend, "review", 11, 22, grade=action, attempt_id="a1", id_namespace=backend)
    assert result["outcome"] == "confirmed"
    assert len(sent) == 1
    url, body = sent[0]
    assert url == (f"{JITEN_API_BASE}/srs/review" if backend == "jiten" else f"{JPDB_API_BASE}/review")
    for key, value in expected.items():
        assert body[key] == value


@pytest.mark.parametrize(("backend", "mode", "action"), [("jiten", "binary", "hard"), ("jpdb", "binary", "okay"), ("jpdb", "native", "good")])
def test_hidden_action_never_reaches_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend, mode, action) -> None:
    service = _service(tmp_path)
    service.save_settings({"jiten_api_key": "jk", "jpdb_api_token": "jt", "study_backend": backend, "review_mode": mode})
    monkeypatch.setattr("pudge.review_providers.httpx.post", lambda *a, **k: pytest.fail("must not send"))
    with pytest.raises(LightNovelError, match="not available"):
        service.study_action(backend, "review", 11, 22, grade=action, attempt_id="a1", id_namespace=backend)
