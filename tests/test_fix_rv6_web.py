"""Review round 6, the window's Python side and the app lifecycle.

- the first paint after a relaunch: the Home snapshot saved by the previous launch names covers
  on that launch's page server (another port, another page key): they are pointed at this
  launch's server before the page paints them;
- the review-gate warm-up at launch counted no ready title when an episode was numbered past its
  title's count (the numbering cache it reads was set later in start-up);
- the Light novels state read every audiobook-generation manifest once per book (one walk now);
- a Home build looked each card's relations up one query at a time (the build has every row);
- every Home build walked the mpv script folders, reading each script, twice (once now, and only
  the part of a script that is checked);
- the review-gate warm-up asked a title's subtitle policy once per episode, every minute.

Invented titles only.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.first_experience as first_experience
import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi
from pudge.web_state import UIStateSnapshot, read_snapshot_file, rebase_asset_urls, write_snapshot_file

OLD_BASE = "http://127.0.0.1:41111/k/old-launch-key"
NEW_BASE = "http://127.0.0.1:52222/k/new-launch-key"


def make_config(tmp_path: Path, *, review_gate: bool = False, low_ram: bool = False) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.power.low_ram_mode = low_ram
    cfg.ui.review_gate_enabled = review_gate
    write_config(cfg, cfg.config_path)
    return cfg


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setattr(web_app_module, "DATA_DIR", path)
    return path


def add_episode(
    db: Database, folder: Path, media_id: int, title: str, episode: int, state: str = "ready"
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{title} - {episode:02d}.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(LibraryEpisode(media_id, title, episode, video, state=state))
    return video


# ------------------------------------------------------------------ relaunch first paint


def test_rebase_asset_urls_points_only_page_server_urls_at_the_new_server() -> None:
    payload = {
        "current": [
            {"cover": f"{OLD_BASE}/covers/501.list.jpg", "cover_remote": "https://img.example/501.jpg"},
            {"cover": f"{OLD_BASE}x/covers/502.jpg"},  # another key that only starts alike
        ],
        "home": {"waiting": [[f"{OLD_BASE}/covers/503.jpg", 7, None]]},
        "title": OLD_BASE,
    }
    assert rebase_asset_urls(payload, OLD_BASE, NEW_BASE) == 2
    assert payload["current"][0] == {
        "cover": f"{NEW_BASE}/covers/501.list.jpg",
        "cover_remote": "https://img.example/501.jpg",
    }
    assert payload["current"][1]["cover"] == f"{OLD_BASE}x/covers/502.jpg"
    assert payload["home"]["waiting"][0] == [f"{NEW_BASE}/covers/503.jpg", 7, None]
    assert payload["title"] == OLD_BASE
    # The same launch, or a snapshot that does not say which server it had: nothing changes.
    assert rebase_asset_urls(payload, NEW_BASE, NEW_BASE) == 0
    assert rebase_asset_urls(payload, "", NEW_BASE) == 0


def test_snapshot_file_keeps_the_page_server_it_was_built_for(tmp_path: Path) -> None:
    path = tmp_path / "window-state.json.gz"
    stamp = {"app": "1", "code": ["1:2"], "db": "/x/library.sqlite3", "db_id": [1, 2]}
    write_snapshot_file(
        path, UIStateSnapshot("3", {"settings": {}}, None, 1000.0), stamp, asset_base=OLD_BASE
    )
    loaded = read_snapshot_file(path, stamp, now=1060.0)
    assert loaded is not None and loaded.asset_base == OLD_BASE


def test_first_paint_after_a_relaunch_shows_covers_from_this_launchs_server(
    tmp_path: Path, data_dir: Path
) -> None:
    cfg = make_config(tmp_path, low_ram=True)
    api = WebAppApi(cfg.config_path)
    try:
        api.manager.db.upsert_anime(
            LibraryAnime(
                501,
                "Hoshizora Teapot",
                cover_url="https://img.example/501.jpg",
                status="CURRENT",
                progress=1,
                episodes=12,
                media_status="FINISHED",
            )
        )
        cfg.library.cover_cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.library.cover_cache_dir / "501.jpg").write_bytes(b"cover")
        api._bump_ui_state_version()
        api.asset_base = OLD_BASE
        built = api.get_state()
        assert built["current"][0]["cover"].startswith(OLD_BASE + "/covers/")
        api._log_memory_window_unloaded()  # saved to disk; Low RAM mode lets the memory copy go
        assert api._ui_state_cache.latest() is None

        # The same launch paints it as it was saved.
        same = api.get_state_snapshot()
        assert same["source"] == "disk"
        assert same["state"]["current"][0]["cover"].startswith(OLD_BASE + "/covers/")

        # The next launch serves the window from another port under another key.
        api.asset_base = NEW_BASE
        answer = api.get_state_snapshot()
        assert answer["source"] == "disk"
        card = answer["state"]["current"][0]
        assert card["cover"] == f"{NEW_BASE}/covers/501.jpg"
        assert card["cover_remote"] == "https://img.example/501.jpg"
        assert OLD_BASE not in json.dumps(answer)
    finally:
        api.close()


# ------------------------------------------------------------------ review gate warm-up at launch


def test_launch_warm_up_counts_a_title_whose_episode_is_numbered_past_its_count(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    db = Database(cfg.library.database_path)
    db.upsert_anime(
        LibraryAnime(
            601, "Kumo no Ressha", status="CURRENT", progress=0, episodes=12, media_status="FINISHED"
        )
    )
    # Absolute numbering kept: episode 13 of a 12-episode season (the numbering offset is read).
    add_episode(db, tmp_path / "library" / "Kumo no Ressha", 601, "Kumo no Ressha", 13)
    cfg.ui.review_gate_enabled = True
    write_config(cfg, cfg.config_path)

    monkeypatch.setattr(
        LightNovelService,
        "study_provider_capabilities",
        lambda self, backend: {"configured": True, "strict_gate_supported": True, "account_key": "acct-1"},
    )
    fetched = threading.Event()

    def candidates(self, **_kwargs):
        fetched.set()
        return {"cards": [], "batch_rounds": 0}

    monkeypatch.setattr(LightNovelService, "strict_review_candidates", candidates)
    api = WebAppApi(cfg.config_path)
    try:
        # It used to fail on the numbering cache (set later in start-up): 0 titles, no warm-up.
        assert api._review_gate_ready_anime_count == 1
        assert api._review_gate_prefetch_target_count == int(cfg.ui.review_gate_count)
        assert fetched.wait(10)
    finally:
        api.close()


def test_warm_up_reads_a_titles_subtitle_policy_once(tmp_path: Path, data_dir: Path) -> None:
    cfg = make_config(tmp_path)
    api = WebAppApi(cfg.config_path)
    try:
        for media_id, title in ((701, "Yoru no Ame"), (702, "Asa no Kaze")):
            api.manager.db.upsert_anime(
                LibraryAnime(
                    media_id, title, status="CURRENT", progress=0, episodes=6, media_status="FINISHED"
                )
            )
            for episode in range(1, 5):
                add_episode(api.manager.db, tmp_path / "library" / title, media_id, title, episode)
        asked: list[int] = []
        real = api.manager.japanese_subtitles_required

        def counting(media_id):
            asked.append(int(media_id))
            return real(media_id)

        api.manager.japanese_subtitles_required = counting
        assert api._review_gate_count_ready_anime() == 2
        assert sorted(asked) == [701, 702]  # one per title, not one per episode (eight)
    finally:
        api.close()


# ------------------------------------------------------------------ Light novels: generation manifests


def write_manifest(folder: Path, **payload) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".pudge-audiobook-generation.json").write_text(
        json.dumps({"schema": 1, **payload}), encoding="utf-8"
    )


def manifest_api(tmp_path: Path) -> tuple[WebAppApi, Path]:
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(library=SimpleNamespace(root_dir=tmp_path / "library"))
    root = api._audiobook_generation_root()
    # Book 5: two unfinished runs, the later one wins.
    write_manifest(
        root / "Book Five [Generated 5]", book_id=5, state="running", updated_at=100.0, mode="generate"
    )
    write_manifest(
        root / "Book Five [Regenerating 5-1]", book_id=5, state="paused", updated_at=200.0, mode="regenerate"
    )
    # Book 6: its run finished; an older (v92) folder of it was interrupted.
    write_manifest(root / "Book Six [Generated 6]", book_id=6, state="succeeded", updated_at=300.0)
    (root / "Book Six [Irodori 6]").mkdir(parents=True)
    (root / "Book Six [Irodori 6]" / ".pudge-audiobook-generating").write_text("", encoding="utf-8")
    # Book 7: its output folder is gone; "[Generated 007]" is not book 7.
    write_manifest(
        root / "Book Seven [Generated 7]",
        book_id=7,
        state="running",
        updated_at=1.0,
        output_dir=str(tmp_path / "gone"),
    )
    (root / "Padding [Generated 007]" / ".pudge-audiobook-parts").mkdir(parents=True)
    # Book 8: an unreadable manifest; its parts folder still offers a resume.
    (root / "Book Eight [Generated 8]" / ".pudge-audiobook-parts").mkdir(parents=True)
    (root / "Book Eight [Generated 8]" / ".pudge-audiobook-generation.json").write_text("{", encoding="utf-8")
    return api, root


def test_generation_manifests_of_every_book_come_from_one_walk(tmp_path: Path) -> None:
    api, root = manifest_api(tmp_path)
    index = api._audiobook_generation_manifests()
    assert sorted(index) == [5, 6, 8]
    assert index[5]["state"] == "paused" and index[5]["output_dir"] == str(
        root / "Book Five [Regenerating 5-1]"
    )
    assert index[6]["state"] == "interrupted" and index[6]["output_dir"] == str(root / "Book Six [Irodori 6]")
    assert index[8]["state"] == "interrupted"
    # A book on its own reads the same answer (and gets its own copy).
    single = api._audiobook_generation_manifest(5)
    assert single == index[5] and single is not index[5]
    assert api._audiobook_generation_manifest(7) is None
    assert api._audiobook_generation_manifest(6, index)["output_dir"] == str(root / "Book Six [Irodori 6]")


def test_light_novel_state_walks_the_generation_folder_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _root = manifest_api(tmp_path)
    api.light_novels = SimpleNamespace(state=lambda: {"books": [{"id": book_id} for book_id in (5, 6, 7, 8)]})
    api.audiobooks = SimpleNamespace(link_for_light_novel=lambda *_args, **_kwargs: None)
    api.job_center = SimpleNamespace(get=lambda _job_id: None)
    api.safe_mode = SimpleNamespace(active=False)
    api._ln_audiobook_last_recovery_at = time.monotonic()  # no recovery thread from this call
    api._irodori_tts_lock = threading.RLock()
    api._irodori_tts_job_ids = {}
    api._irodori_tts_threads = {}
    api._audiobook_generation_controls = {}
    walks: list[int] = []
    real = WebAppApi._audiobook_generation_manifests

    def counting(self):
        walks.append(1)
        return real(self)

    monkeypatch.setattr(WebAppApi, "_audiobook_generation_manifests", counting)
    state = api.light_novel_state()
    assert walks == [1]
    status = {int(book["id"]): book["irodori_tts"] for book in state["books"]}
    assert status[5]["state"] == "paused" and status[5]["resumable"] is True
    assert status[6]["state"] == "interrupted" and status[6]["resumable"] is True
    assert status[7]["state"] == "idle" and status[7]["resumable"] is False
    assert status[8]["state"] == "interrupted"


# ------------------------------------------------------------------ Home build: relations of a card


def test_home_build_finds_card_relations_without_a_query_each(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    api = WebAppApi(cfg.config_path)
    try:
        db = api.manager.db
        # A sequel in the library with a ready episode, and relations that are not in it.
        db.upsert_anime(
            LibraryAnime(
                802, "Tsuki no Fune 2", status="PLANNING", progress=0, episodes=12, media_status="FINISHED"
            )
        )
        add_episode(db, tmp_path / "library" / "Tsuki no Fune 2", 802, "Tsuki no Fune 2", 1)
        missing_ids = list(range(990001, 990011))
        relations = [
            {"media_id": 802, "relation_type": "SEQUEL", "title": "Tsuki no Fune 2", "relations": []}
        ] + [
            {"media_id": value, "relation_type": "SIDE_STORY", "title": f"Side {value}", "relations": []}
            for value in missing_ids
        ]
        for media_id, title in ((801, "Tsuki no Fune"), (803, "Hana no Michi")):
            db.upsert_anime(
                LibraryAnime(
                    media_id,
                    title,
                    status="CURRENT",
                    progress=0,
                    episodes=12,
                    media_status="FINISHED",
                    relations=relations if media_id == 801 else relations[1:],
                )
            )
        api._bump_ui_state_version()
        asked: list[int] = []
        real = Database.get_anime

        def counting(self, media_id):
            asked.append(int(media_id))
            return real(self, media_id)

        monkeypatch.setattr(Database, "get_anime", counting)
        state = api.get_state()
        assert not set(asked) & {802, *missing_ids}
        cards = {int(card["media_id"]): card for card in state["current"]}
        assert cards[801]["queue_franchise_available"] is True  # the sequel's ready episode
        assert cards[803]["queue_franchise_available"] is False
        # Outside a build the relations are still read from the database.
        asked.clear()
        items = api._ready_queue_items([801, 802], limit=1)
        assert [item["media_id"] for item in items] == [802] and 802 in asked
    finally:
        api.close()


# ------------------------------------------------------------------ mpv study plugins (every Home build)


def test_mpv_study_status_walks_the_script_folders_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin = tmp_path / ".config" / "mpv" / "scripts" / "jpdb-mpv-plugin"
    plugin.mkdir(parents=True)
    (plugin / "main.lua").write_text("-- jpdb plugin\n", encoding="utf-8")
    (plugin.parent / "other.lua").write_text("-- unrelated\n", encoding="utf-8")
    walks: list[int] = []
    real = first_experience._jpdb_installations

    def counting():
        walks.append(1)
        return real()

    monkeypatch.setattr(first_experience, "_jpdb_installations", counting)
    status = first_experience.mpv_study_status()
    assert walks == [1]
    assert status["jpdb_mpv"]["installed"] is True
    assert status["jpdb_mpv"]["script_path"] == str(plugin / "main.lua")
    assert status["jpdb_mpv"]["detected_variants"] == [str(plugin / "main.lua")]


def test_a_generic_script_is_checked_on_its_first_characters_only(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    early = scripts / "client.lua"
    early.write_text("-- talks to jpdb\n" + "x" * 300_000, encoding="utf-8")
    late = scripts / "main.lua"
    late.write_text("y" * 300_000 + "\n-- jpdb\n", encoding="utf-8")
    assert first_experience._looks_like_jpdb_script(early, scripts) is True
    assert first_experience._looks_like_jpdb_script(late, scripts) is False
