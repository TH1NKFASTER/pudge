"""Tabs, watch-order card status and resolver retries of v0.5.69 (behaviour; audit 71Б rewrite).
Window checks run the real index.html (tests/page_harness.py). The installer's replacement of
the runtime with the bundled wheel is tested in test_install_script.py."""

from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager


def test_clicking_the_active_navigation_tab_does_nothing(page) -> None:
    result = page.run(
        r"""
        await __click('.nav button[data-page="lightnovels"]'); await __tick(50);
        $('scroll').scrollTop = 480;
        await __click('.nav button[data-page="lightnovels"]'); await __tick(50);
        const again = {page: ui.page, scroll: $('scroll').scrollTop};
        await __click('.nav button[data-page="current"]'); await __tick(50);
        return {again, switched: {page: ui.page, scroll: $('scroll').scrollTop}};
        """
    )
    # The active tab keeps its place (it used to jump to the top and reload); another tab
    # starts at the top.
    assert result.value == {
        "again": {"page": "lightnovels", "scroll": 480},
        "switched": {"page": "current", "scroll": 0},
    }


SEQUENCE = {
    "kind": "watch_sequence",
    "title": "Series",
    "items": [
        {"media_id": 2, "title": "Part A", "local": {"video_path": "/v/a.mkv"}, "ready_episodes": [1, 2]},
        {"media_id": 3, "title": "Movie B", "local": {"video_path": "/v/b.mkv"}, "ready_episodes": []},
    ],
}


def test_completed_watch_order_card_shows_the_ready_state_of_the_selected_entry(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const card = document.querySelector('#currentContent .ready-sequence-card');
            const status = () => card.querySelector('.ready-sequence-meta .airing-state').innerText;
            const lead = status();
            await __click('#currentContent .ready-sequence-card [data-action="ready-sequence-next"]');
            const movie = status();
            await __click('#currentContent .ready-sequence-card [data-action="select-ready-entry"][data-entry-index="0"]');
            return {lead, movie, back: status(), expected: [t('label.readyEpisodes', {episodes: '1-2'}), t('label.readyMovie')]};
            """,
            lang=lang,
            state={"home": {"completed_ready": [SEQUENCE]}},
        )
        value = result.value
        assert [value["lead"], value["movie"]] == value["expected"]
        assert value["back"] == value["lead"]


def test_resolver_upgrade_retries_delayed_jobs_immediately_with_agent_enabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.agent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = tmp_path / "Boku no Hero Academia - I am a Hero too.mkv"
    video.write_bytes(b"video")
    manager.db.queue_subtitle_job(video, 211711, None, delay_seconds=21600)
    manager.db.set_state("subtitle_resolver_generation", "8")

    monkeypatch.setattr(manager, "sync_downloads", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "cleanup_duplicate_torrents", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "refresh_anilist_if_due", lambda: 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "reconcile_duplicate_versions", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)
    monkeypatch.setattr(manager, "enforce_disk_limit", lambda: 0)

    calls: list[int] = []
    monkeypatch.setattr(
        manager,
        "process_subtitle_jobs",
        lambda limit=4: calls.append(limit) or 1,
    )

    stats = manager.run_startup_once()

    assert calls == []
    assert stats["subs"] == 0
    job = next(row for row in manager.db.subtitle_jobs() if str(row["video_path"]) == str(video))
    assert int(job["priority"]) >= 200
    assert manager.db.get_state("subtitle_resolver_generation", "") == "9"
    row = manager.db.subtitle_jobs()[0]
    assert float(row["next_check"]) <= __import__("time").time() + 2
