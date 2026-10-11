from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from pudge.config import AppConfig, SyncConfig
from pudge.database import Database
from pudge.library import scan_library
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime
from pudge.providers.jimaku import JimakuClient
from pudge.providers.nyaa import _quality_score
from pudge.syncing import parse_srt, repair_with_embedded_reference_piecewise, write_srt
from tests.i18n_support import has_text


def test_preferred_resolution_gets_ten_extra_points():
    assert _quality_score("Show S01E01 1080p WEB-DL", "1080p")[0] == 52.0
    assert _quality_score("Show S01E01 1080p WEB-DL", "2160p")[0] == 30.0
    assert _quality_score("Show S01E01 2160p WEB-DL", "2160p")[0] == 52.0


def test_jimaku_dns_failure_retries_then_uses_stale_positive_cache(monkeypatch, tmp_path: Path):
    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path, cache_ttl_seconds=1)
    path = "/api/entries/search"
    params = {"anime": "true", "anilist_id": 123}
    raw = json.dumps(
        {"base_url": client.base_url, "path": path, "params": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    cached = tmp_path / "jimaku-api" / f"{digest}.json"
    cached.parent.mkdir(parents=True)
    payload = [{"id": 9, "name": "Cached anime"}]
    cached.write_text(json.dumps(payload), encoding="utf-8")
    old = time.time() - 60
    os.utime(cached, (old, old))

    attempts = 0

    def fail(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        request = httpx.Request("GET", "https://jimaku.cc/api/entries/search")
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided", request=request)

    monkeypatch.setattr(client.client, "get", fail)
    monkeypatch.setattr("pudge.providers.jimaku.time.sleep", lambda _seconds: None)
    try:
        assert client._get_json(path, params) == payload
        assert attempts == 1
    finally:
        client.close()


def test_external_scan_uses_identity_resolver_before_title_only_match(monkeypatch, tmp_path: Path):
    root = tmp_path / "Downloads"
    root.mkdir()
    video = root / "Example Anime S03E05 1080p.mkv"
    video.write_bytes(b"x")
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Example Anime", titles=["Example Anime"], progress=0))
    resolved = LibraryAnime(
        media_id=3,
        title="Example Anime Season 3",
        titles=["Example Anime Season 3"],
        progress=0,
    )
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    rows = scan_library(
        root,
        db,
        anime_resolver=lambda identity: resolved if identity.season == 3 else None,
        require_anime_match=True,
    )
    assert len(rows) == 1
    assert rows[0].media_id == 3
    assert db.get_anime(3) is not None


def test_cleanup_removes_only_empty_immediate_anime_folder(tmp_path: Path):
    root = tmp_path / "Library"
    anime_dir = root / "Anime"
    anime_dir.mkdir(parents=True)
    video = anime_dir / "Episode 01.mkv"
    video.write_bytes(b"x")
    video.unlink()

    manager = AnimeManager.__new__(AnimeManager)
    manager.config = AppConfig()
    manager.config.library.root_dir = root
    manager.config.paths.download_dirs = []
    manager.logger = logging.getLogger("v0637-empty-folder")

    assert manager._remove_empty_episode_parent(video) is True
    assert not anime_dir.exists()
    assert root.exists()

    nonempty = root / "Keep"
    nonempty.mkdir()
    (nonempty / "note.txt").write_text("keep", encoding="utf-8")
    assert manager._remove_empty_episode_parent(nonempty / "Episode 02.mkv") is False
    assert nonempty.exists()


def test_cold_open_duplicate_sfx_anchors_unique_dialogue(monkeypatch, tmp_path: Path):
    candidate = tmp_path / "candidate.srt"
    reference = tmp_path / "reference.srt"
    cues = [
        (5.575, 8.578, "first"),
        (11.581, 13.583, "second"),
    ]
    # Keep a large opening/title-card gap then enough cues for the repair gate.
    for index in range(10):
        start = 119.622 + index * 30.0
        cues.append((start, start + 3.0, f"line {index}"))
    refs = [
        (11.490, 14.490, "Krrrack!!"),
        (15.240, 20.210, "Krrrack!!"),
        (15.760, 18.520, "A krrrack to open the episode?!"),
    ]
    for index in range(10):
        start = 119.972 + index * 30.0
        refs.append((start, start + 3.0, f"eng {index}"))
    write_srt(cues, candidate, preserve_order=True)

    # Keep the intentional overlapping SFX cues: write_srt protects normal
    # playback files by trimming overlaps, while embedded references may contain
    # them exactly like Crunchyroll's ASS track does.
    def stamp(value: float) -> str:
        millis = int(round(value * 1000))
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        seconds, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    reference.write_text(
        "\n\n".join(
            f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}"
            for index, (start, end, text) in enumerate(refs, start=1)
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_window(_candidate, _reference, *, region_start, **_kwargs):
        return {
            "available": True,
            "confident": True,
            "shift_seconds": 4.45 if region_start < 25.0 else 0.35,
            "coverage": 0.95,
            "onset_ratio": 0.95,
        }

    monkeypatch.setattr("pudge.syncing._windowed_reference_shift", fake_window)

    def fake_compare(path, _reference, priority_seconds=None):
        improved = "reference-piecewise" in str(path)
        return {
            "available": True,
            "start": 0.90 if improved else 0.80,
            "middle": 0.90,
            "weighted": 0.90 if improved else 0.80,
        }

    monkeypatch.setattr("pudge.syncing.compare_timing_activity", fake_compare)
    output, result = repair_with_embedded_reference_piecewise(
        candidate,
        reference,
        tmp_path / "cache",
        SyncConfig(),
        force=True,
    )
    assert result["applied"] is True
    out = parse_srt(output)
    assert out[0][0] == pytest.approx(15.760, abs=0.002)
    assert out[2][0] == pytest.approx(119.972, abs=0.01)
    anchor = result["sequence_safety"]["cold_open_reference_anchor"]
    assert anchor["duplicate_reference_cues"] == 2


# Window changes of v0.6.37 (behaviour, page harness). Multiple watched/subtitle folders in
# Settings: test_subtitle_folder_permissions_v0605.py; "Queue next" gone, the franchise queue
# kept: test_v0720_second_six_contract.py; the Alternative shelf for ALTERNATIVE movies:
# test_v0656_settings_brand_library.py.
def _menu(page_name: str) -> str:
    """Right-click Show (media 7) on a page; the context-menu actions."""
    content = "plannedContent" if page_name == "planned" else "currentContent"
    return f"""
    setPage('{page_name}', true); await __tick(20);
    await __fire('#{content} [data-media-id="7"]', 'contextmenu', {{bubbles: true, cancelable: true, clientX: 10, clientY: 10}}, 'MouseEvent');
    return [...document.querySelectorAll('#contextMenu button')].map(b => b.dataset.contextAction);
    """


SHOW = {
    "media_id": 7,
    "title": "Show",
    "episodes": 12,
    "next_episode": 3,
    "progress": 2,
    "site_url": "https://anilist.co/anime/7",
    "media_status": "RELEASING",
    "local": None,
}


def test_settings_have_no_long_integration_hint(page) -> None:
    text = page.run(
        "setPage('settings', true); await __tick(20); return $('settingsContent').textContent;"
    ).value
    assert "Configure only the features you use" not in text
    assert not has_text("Configure only the features you use")


def test_planning_cards_never_ask_why_not_ready(page) -> None:
    planned = {**SHOW, "list_status": "PLANNING"}
    in_planning = page.run(_menu("planned"), state={"planned": [planned]}).value
    assert "open" in in_planning and "diagnose" not in in_planning
    waiting = page.run(_menu("current"), state={"current": [SHOW], "home": {"waiting": [SHOW]}}).value
    assert "diagnose" in waiting


def test_franchise_queue_is_offered_only_when_the_backend_has_one(page) -> None:
    menus = {}
    for available, status in ((True, "RELEASING"), (False, "RELEASING"), (True, "NOT_YET_RELEASED")):
        show = {**SHOW, "queue_franchise_available": available, "media_status": status}
        menus[(available, status)] = page.run(
            _menu("current"), state={"current": [show], "home": {"waiting": [show]}}
        ).value
    assert "queue-franchise" in menus[(True, "RELEASING")]
    assert "queue-franchise" not in menus[(False, "RELEASING")]
    assert "queue-franchise" not in menus[(True, "NOT_YET_RELEASED")]


def test_recap_and_compilation_movies_go_to_the_alternative_shelf(page) -> None:
    def node(media_id: int, title: str, kind: str, year: int) -> dict:
        return {"media_id": media_id, "title": title, "format": kind, "season_year": year}

    graph = {
        "root_id": 1,
        "nodes": [
            node(1, "Show", "TV", 2019),
            node(2, "Show: Recap", "MOVIE", 2020),
            node(3, "Show: Compilation", "MOVIE", 2021),
            node(4, "Show 2", "TV", 2022),
            node(5, "Show: The Movie", "MOVIE", 2023),
        ],
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SUMMARY"},
            {"source": 3, "target": 1, "relation_type": "COMPILATION"},
            {"source": 1, "target": 4, "relation_type": "SEQUEL"},
            {"source": 4, "target": 5, "relation_type": "SEQUEL"},
        ],
    }
    result = page.run(
        r"""
        const host = document.createElement('div'); document.body.appendChild(host);
        host.innerHTML = fullRelationGraphDiagram(window.graph);
        return {shelf: [...host.querySelectorAll('.full-relation-alternatives .full-relation-alt')].map(b => b.title),
                label: __text('.full-relation-alternatives-label'), expected: relationTypeLabel('ALTERNATIVE'),
                nodes: [...host.querySelectorAll('.full-relation-node [data-title]')].map(n => n.dataset.title)};
        """,
        before=f"window.graph = {json.dumps(graph)};",
    )
    out = result.value
    # Recap/compilation movies of a season sit on its small Alternative shelf; a sequel movie
    # stays a full node.
    assert out["shelf"] == ["Show: Recap", "Show: Compilation"] and out["label"] == out["expected"]
    assert out["nodes"] == ["Show", "Show 2", "Show: The Movie"]


def test_score_wheel_runs_from_red_for_low_to_blue_for_high_scores(page) -> None:
    result = page.run(
        r"""
        showScoreModal({media_id: 7, title: 'Show'});
        const background = $('scoreWheel').style.background;
        return {background, labels: [...document.querySelectorAll('#scoreWheel .score-wheel-label')].map(l => l.textContent)};
        """
    )
    colors = re.findall(r"#([0-9a-f]{6})", result.value["background"])
    assert result.value["labels"] == [str(score) for score in range(1, 11)] and len(colors) == 10
    rgb = [tuple(int(color[i : i + 2], 16) for i in (0, 2, 4)) for color in colors]
    low, high = rgb[0], rgb[-1]
    assert low[0] > low[1] and low[0] > low[2]  # 1 is red
    assert high[2] > high[0] and high[2] > high[1]  # 10 is blue (high scores used to be red)
    assert all(red > blue for red, _green, blue in rgb[:4]) and all(
        blue > red for red, _green, blue in rgb[-2:]
    )
