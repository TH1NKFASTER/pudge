"""Right-click menus of the sidebar Refresh and Torrents buttons, and the focused backend
actions behind them (behaviour; audit 71Б rewrite: page harness instead of source text)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


# The torrent switch talks to the local HTTP endpoint (see test_torrent_toggle_ui.py).
FETCH = """
let torrentsOn = false;
window.fetch = async (url, init) => {
  __fetches.push({url: String(url), method: init?.method || 'GET', body: init?.body ?? null});
  if (init?.body) torrentsOn = JSON.parse(init.body).enabled;
  return new Response(JSON.stringify({ok: true, enabled: torrentsOn, desired_enabled: torrentsOn, transition: torrentsOn ? 'on' : 'off'}), {status: 200});
};
"""
MENU = r"""
const menu = async selector => {
  await __fire(selector, 'contextmenu', {clientX: 20, clientY: 20, button: 2}, 'MouseEvent');
  return [...document.querySelectorAll('#contextMenu [data-sidebar-context-action]')]
    .filter(button => button.checkVisibility()).map(button => [button.dataset.sidebarContextAction, button.innerText]);
};
const choose = async (selector, action) => { await menu(selector); await __click(`[data-sidebar-context-action="${action}"]`); await __tick(50); };
"""


def test_refresh_and_torrent_buttons_open_focused_right_click_menus(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            MENU
            + r"""
            const out = {refresh: await menu('#refreshAll'), open: __visible('#contextMenu')};
            out.torrents = await menu('#torrentToggleButton');
            for (const action of ['subtitles-new', 'anilist', 'scan-local', 'releases']) await choose('#refreshAll', action);
            out.menuClosed = !__visible('#contextMenu');
            await choose('#torrentToggleButton', 'downloads');
            out.downloads = {open: __visible('#modalBackdrop'), title: __text('#modalTitle')};
            out.labels = {refresh: ['ui.findSubtitles', 'ui.updateAnilist', 'ui.updateJitenData', 'ui.rescanLocalFiles', 'ui.findNewEpisodes'].map(k => t(k)),
                          torrents: ['ui.enableTorrentTraffic', 'ui.openDownloads'].map(k => t(k)), downloads: t('ui.downloads')};
            return out;
            """,
            lang=lang,
            api={
                "search_new_subtitles": {"eligible": 0, "processed": 0},
                "sync_anilist": {"skipped": True},
                "search_new_releases": {"started": 0},
                "torrent_downloads": {"downloads": []},
            },
            before=FETCH,
        )
        out = result.value
        assert out["open"] is True
        assert [label for _action, label in out["refresh"]] == out["labels"]["refresh"]
        assert [action for action, _label in out["refresh"]] == [
            "subtitles-new",
            "anilist",
            "jiten",
            "scan-local",
            "releases",
        ]
        assert [label for _action, label in out["torrents"]] == out["labels"]["torrents"]
        assert out["menuClosed"] is True
        # Each item runs only its own focused job; the right click itself never starts a full refresh.
        assert [
            name
            for name in ("search_new_subtitles", "sync_anilist", "scan_library", "search_new_releases")
            if result.called(name)
        ] == ["search_new_subtitles", "sync_anilist", "scan_library", "search_new_releases"]
        assert not result.called("refresh_local")
        assert result.calls_of("torrent_downloads")[0] == [None, False]
        assert out["downloads"] == {"open": True, "title": out["labels"]["downloads"]}


def test_torrent_menu_switch_follows_the_current_state(page) -> None:
    result = page.run(
        MENU
        + r"""
        const before = await menu('#torrentToggleButton');
        await __click('[data-sidebar-context-action="torrent-toggle"]'); await __tick(50);
        const after = await menu('#torrentToggleButton');
        return {before: before[0][1], after: after[0][1], posts: __fetches.filter(f => f.method === 'POST').map(f => f.body),
                labels: [t('ui.enableTorrentTraffic'), t('ui.disableTorrentTraffic')]};
        """,
        before=FETCH,
    )
    out = result.value
    assert [out["before"], out["after"]] == out["labels"]
    assert out["posts"] == ['{"enabled":true}']


def _job_rows(api: WebAppApi) -> list[tuple]:
    return [
        (row["video_path"], row["state"], row["attempts"], row["next_check"], row["priority"])
        for row in api.manager.db.subtitle_jobs()
    ]


def test_new_subtitle_action_only_runs_never_attempted_jobs_and_changes_no_retry_state(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    videos = {name: tmp_path / f"{name}.mkv" for name in ("old", "new", "failed", "later")}
    for index, path in enumerate(videos.values(), 1):
        path.write_bytes(path.stem.encode())
        api.manager.db.queue_subtitle_job(path.resolve(), index, index)
    with api.manager.db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET next_check=0")
        conn.execute(
            "UPDATE subtitle_jobs SET attempts=2 WHERE video_path=?", (str(videos["old"].resolve()),)
        )
        conn.execute(
            "UPDATE subtitle_jobs SET state='failed' WHERE video_path=?", (str(videos["failed"].resolve()),)
        )
        conn.execute(
            "UPDATE subtitle_jobs SET next_check=? WHERE video_path=?",
            (4_000_000_000, str(videos["later"].resolve())),
        )
    before = _job_rows(api)

    @contextmanager
    def acquired(*_args, **_kwargs):
        yield True

    observed: list[Path] = []

    def process(*, limit: int = 4, preferred_paths=()):
        del limit
        observed.extend(Path(item).resolve() for item in preferred_paths)
        return len(preferred_paths)

    monkeypatch.setattr("pudge.web_app.maintenance_lock", acquired)
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", process)
    monkeypatch.setattr(api, "get_state", lambda: {"ok": True})

    result = api.search_new_subtitles()

    assert (result["eligible"], result["processed"], result["queued"]) == (1, 1, False)
    assert observed == [videos["new"].resolve()]
    # Old attempts are neither retried nor re-queued nor postponed: the queue is as it was.
    assert _job_rows(api) == before


def test_new_subtitle_action_waits_for_running_maintenance(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = tmp_path / "new.mkv"
    video.write_bytes(b"new")
    api.manager.db.queue_subtitle_job(video.resolve(), 1, 1)
    with api.manager.db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET next_check=0")

    @contextmanager
    def busy(*_args, **_kwargs):
        yield False

    monkeypatch.setattr("pudge.web_app.maintenance_lock", busy)
    monkeypatch.setattr(
        api.manager,
        "process_subtitle_jobs",
        lambda **_k: (_ for _ in ()).throw(AssertionError("ran during maintenance")),
    )
    monkeypatch.setattr(api, "get_state", lambda: {"ok": True})
    assert api.search_new_subtitles() == {
        "eligible": 1,
        "processed": 0,
        "queued": True,
        "state": {"ok": True},
    }


def test_release_search_reuses_existing_auto_discovery(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(api.manager, "auto_search_current", lambda: calls.append("search") or 2)
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: calls.append("sync") or 2)
    monkeypatch.setattr(api, "_downloads_enabled", lambda: True)
    monkeypatch.setattr(api, "get_state", lambda: {"ok": True})

    assert api.search_new_releases() == {"started": 2, "downloads": 2, "warning": "", "state": {"ok": True}}
    assert calls == ["search", "sync"]

    monkeypatch.setattr(
        api.manager, "auto_search_current", lambda: (_ for _ in ()).throw(RuntimeError("nyaa down"))
    )
    calls.clear()
    result = api.search_new_releases()
    assert (result["started"], result["warning"], calls) == (0, "nyaa down", [])
