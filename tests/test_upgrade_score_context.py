"""Task 5: upgrade decisions compare scores only within one formula/context."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from pudge.database import LATEST_SCHEMA_VERSION, Database
from pudge.providers.nyaa import SCORE_FORMULA_VERSION, score_context_key, seadex_fingerprint
from pudge.providers.seadex import SeaDexRecommendation

from tests.test_manager import _upgrade_manager, _upgrade_release


BASE_KWARGS = {
    "alternative_episodes": (5,),
    "alternative_titles": ("Show",),
    "negative_titles": (),
    "episode": 5,
    "batch": False,
    "trusted_groups": ["SubsPlease"],
    "preferred_groups": ["EMBER"],
    "blocked_groups": [],
    "preferred_resolution": "1080p",
    "preferred_video_codecs": ["HEVC", "AVC"],
    "preferred_sources": ["BluRay"],
    "require_japanese_audio": True,
    "avoid_upscaled": True,
    "min_seeders": 1,
    "target_episode_min_bytes": 1,
    "target_episode_max_bytes": 2,
}
HASH_A = "a" * 40


def _found(*preferred: str) -> SeaDexRecommendation:
    return SeaDexRecommendation(anilist_id=1, status="found", preferred_hashes=frozenset(preferred))


def test_context_key_is_stable_and_versioned() -> None:
    key = score_context_key(dict(BASE_KWARGS))
    assert key == score_context_key(dict(BASE_KWARGS))
    assert key.startswith(SCORE_FORMULA_VERSION + ":")


@pytest.mark.parametrize(
    "change",
    [
        {"preferred_resolution": "720p"},
        {"preferred_groups": ["Judas"]},
        {"alternative_episodes": (5, 17)},
        {"preferred_video_codecs": ["AVC", "HEVC"]},  # order is a ranking
        {"seadex": _found(HASH_A)},
    ],
)
def test_context_key_changes_with_any_scoring_input(change) -> None:
    assert score_context_key({**BASE_KWARGS, **change}) != score_context_key(dict(BASE_KWARGS))


def test_seadex_fingerprint() -> None:
    assert seadex_fingerprint(None) == "none"
    assert seadex_fingerprint(SeaDexRecommendation(anilist_id=1, status="unavailable")) == "none"
    assert seadex_fingerprint(_found(HASH_A)) == seadex_fingerprint(_found(HASH_A))
    assert seadex_fingerprint(_found(HASH_A)) != seadex_fingerprint(_found("b" * 40))
    # Same facts, another stale flag: still the same scoring input.
    assert seadex_fingerprint(replace(_found(HASH_A), stale=True)) == seadex_fingerprint(_found(HASH_A))


def test_schema_has_score_context_columns(tmp_path: Path) -> None:
    db = Database(tmp_path / "lib.sqlite3")
    with sqlite3.connect(db.path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(release_history)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert {"score_formula", "score_context"} <= columns
    assert version == LATEST_SCHEMA_VERSION == 13


def test_v12_database_migrates_release_history(tmp_path: Path) -> None:
    path = tmp_path / "lib.sqlite3"
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE release_history DROP COLUMN score_context")
        conn.execute("ALTER TABLE release_history DROP COLUMN score_formula")
        conn.execute("PRAGMA user_version=12")
    db = Database(path)
    db.record_release("abc", 1, 5, "t", 90.0, score_formula="f", score_context="c")
    assert db.release_score_record(1, 5, "ABC") == (90.0, "f", "c")
    assert (tmp_path / "lib.sqlite3.pre-v13.backup").is_file()


def test_release_score_record_is_exact_hash_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "lib.sqlite3")
    db.record_release("other", 1, 5, "t", 150.0, score_context="c")
    assert db.release_score_record(1, 5, "current") is None
    db.record_release("current", 1, 5, "t", 80.0)
    assert db.release_score_record(1, 5, "current") == (80.0, "", "")
    assert db.release_score_record(1, 6, "current") is None


def _stamp(release, context: str = "ctx-1"):
    return replace(release, score_formula=SCORE_FORMULA_VERSION, score_context=context)


def _run(manager, monkeypatch, releases, caplog) -> list[str]:
    added: list[str] = []
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: list(releases))
    monkeypatch.setattr(
        manager, "add_release", lambda _media_id, release, **_kwargs: added.append(release.info_hash)
    )
    manager.logger.addHandler(caplog.handler)
    manager.logger.setLevel(logging.INFO)
    manager.auto_upgrade_downloaded()
    return added


def test_stored_score_used_only_in_same_context(tmp_path: Path, monkeypatch, caplog) -> None:
    manager = _upgrade_manager(tmp_path)
    manager.db.record_release(
        "oldhash", 700, 5, "old", 80.0, score_formula=SCORE_FORMULA_VERSION, score_context="ctx-1"
    )
    added = _run(manager, monkeypatch, [_stamp(_upgrade_release("newhash", 115.0))], caplog)
    assert added == ["newhash"]
    assert "basis=stored" in caplog.text


def test_context_mismatch_skips_with_reason(tmp_path: Path, monkeypatch, caplog) -> None:
    manager = _upgrade_manager(tmp_path)
    manager.db.record_release(
        "oldhash", 700, 5, "old", 10.0, score_formula=SCORE_FORMULA_VERSION, score_context="ctx-old"
    )
    added = _run(manager, monkeypatch, [_stamp(_upgrade_release("newhash", 115.0))], caplog)
    assert added == []
    assert not manager.db.has_pending_upgrade(700, 5)
    assert "reason=score_context_mismatch" in caplog.text


def test_legacy_score_without_context_is_not_compared(tmp_path: Path, monkeypatch, caplog) -> None:
    manager = _upgrade_manager(tmp_path)  # records oldhash=80 without context
    added = _run(manager, monkeypatch, [_stamp(_upgrade_release("newhash", 500.0))], caplog)
    assert added == []
    assert "reason=score_context_missing" in caplog.text


def test_fresh_score_of_current_release_wins_over_stored(tmp_path: Path, monkeypatch, caplog) -> None:
    manager = _upgrade_manager(tmp_path)  # stored 80 (legacy); fresh says 100
    releases = [
        _stamp(_upgrade_release("newhash", 115.0)),
        _stamp(_upgrade_release("oldhash", 100.0)),
    ]
    added = _run(manager, monkeypatch, releases, caplog)
    assert added == []  # gain 15 < 30 in one context
    assert "reason=gain_too_small" in caplog.text

    manager2 = _upgrade_manager(tmp_path / "b")
    releases = [
        _stamp(_upgrade_release("newhash", 140.0)),
        _stamp(_upgrade_release("oldhash", 100.0)),
    ]
    assert _run(manager2, monkeypatch, releases, caplog) == ["newhash"]
    assert "basis=fresh" in caplog.text


def test_same_hash_is_never_an_upgrade(tmp_path: Path, monkeypatch, caplog) -> None:
    manager = _upgrade_manager(tmp_path)
    added = _run(manager, monkeypatch, [_stamp(_upgrade_release("oldhash", 999.0))], caplog)
    assert added == []


def test_add_release_records_context(tmp_path: Path) -> None:
    manager = _upgrade_manager(tmp_path)
    release = _stamp(_upgrade_release("c" * 40, 120.0), "ctx-9")
    manager.db.record_release(
        release.info_hash, 700, 5, release.title, release.score,
        score_formula=release.score_formula, score_context=release.score_context,
    )
    assert manager.db.release_score_record(700, 5, release.info_hash) == (120.0, SCORE_FORMULA_VERSION, "ctx-9")


class _FakeSeaDex:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def recommendations(self, media_id, *, allow_network, timeout):
        self.calls.append(allow_network)
        return _found(HASH_A)


def test_automatic_seadex_lookup_is_cache_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_SEADEX", "1")
    manager = _upgrade_manager(tmp_path)
    fake = manager._seadex_client = _FakeSeaDex()
    assert manager._seadex_for_search(700, automatic=True) is not None
    assert manager._seadex_for_search(700, automatic=False) is not None
    assert fake.calls == [False, True]
    manager.config.nyaa.seadex_enabled = False
    assert manager._seadex_for_search(700, automatic=True) is None


def test_release_card_shows_seadex_label_only_for_exact_match() -> None:
    html = (Path(__file__).resolve().parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "function releaseSeadexBadge(r)" in html
    assert "reasons.includes('seadex-exact')" in html
    assert "${releaseSeadexBadge(r)}<strong" in html
    assert "'release.seadexPreferred':'Рекомендован SeaDex'" in html
    assert "'release.seadexPreferred':'SeaDex recommended'" in html


def test_search_results_are_stamped_with_one_context(tmp_path: Path, monkeypatch) -> None:
    import pudge.manager as manager_module

    manager = _upgrade_manager(tmp_path)
    captured: dict = {}

    def fake_subsplease(_client, _anime, **kwargs):
        captured.update(kwargs)
        return [_upgrade_release("n1", 120.0), _upgrade_release("n2", 110.0)]

    monkeypatch.setattr(manager_module, "search_subsplease_ranked", fake_subsplease)
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)
    releases = manager.search_releases(700, episode=5, automatic=True)
    assert {item.score_context for item in releases} == {score_context_key(captured)}
    assert {item.score_formula for item in releases} == {SCORE_FORMULA_VERSION}
