"""Requested features of v0.6.55 (behaviour; audit 71Б rewrite): slower polychrome covers that
never restart twice, automatic "watched" on by default, only mpv shortcuts configurable,
relative episode numbers, the highest-resolution choice and energy diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.providers.nyaa import SubsPleaseClient, _quality_score
from pudge.web_app import WebAppApi
from tests.css_support import css_rules
from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario
from test_v0647_startup_jobs_and_watch_cap import _tracker_env


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def bleach_graph() -> dict:
    return {
        "root_id": 5,
        "nodes": [
            {"media_id": 1, "title": "BLEACH", "format": "TV", "episodes": 366, "start_date": "2004-10-05"},
            {
                "media_id": 2,
                "title": "BLEACH: Sennen Kessen-hen",
                "format": "TV",
                "episodes": 13,
                "start_date": "2022-10-11",
            },
            {
                "media_id": 3,
                "title": "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan",
                "format": "TV",
                "episodes": 13,
                "start_date": "2023-07-08",
            },
            {
                "media_id": 4,
                "title": "BLEACH: Sennen Kessen-hen - Soukoku-tan",
                "format": "TV",
                "episodes": 14,
                "start_date": "2024-10-05",
            },
            {
                "media_id": 5,
                "title": "BLEACH: Sennen Kessen-hen - Kashin-tan",
                "format": "TV",
                "episodes": 13,
                "start_date": "2026-07-25",
            },
        ],
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
            {"source": 2, "target": 3, "relation_type": "SEQUEL"},
            {"source": 3, "target": 4, "relation_type": "SEQUEL"},
            {"source": 4, "target": 5, "relation_type": "SEQUEL"},
        ],
    }


# A ready episode on Home: its cover is polychrome. `changes` records every class change.
READY = {
    "media_id": 7,
    "title": "Show",
    "site_url": "https://anilist.co/anime/7",
    "episodes": 12,
    "local": {"video_path": "/v/Show - 03.mkv", "episode": 3, "state": "ready"},
}
POLYCHROME = """
setPage('current', true);
const selector = '#current .cover-shell.polychrome';
const cover = () => document.querySelector(selector);
const changes = [];
const watch = () => new MutationObserver(records => records.forEach(() => changes.push(cover().className.split(' ')
  .filter(name => name.startsWith('polychrome-')).join(' ') || 'idle'))).observe(cover(), {attributes: true, attributeFilter: ['class']});
const pointer = (type, init = {}) => __fire(selector, type, {bubbles: true, ...init}, 'PointerEvent');
"""


def test_polychrome_is_50_percent_slower_and_does_not_double_restart(page) -> None:
    rules = css_rules("index.html")
    for state in ("polychrome-wake", "polychrome-hover-wake"):
        assert (
            rules.get(f".cover-shell.polychrome.{state}::before", "animation")
            == "polychrome-flow 1.875s ease-out 1 both"
        )
        assert (
            rules.get(f".cover-shell.polychrome.{state}::after", "animation")
            == "polychrome-glint 1.425s ease-out 1 both"
        )
    # Coming back to the window starts the shine in the next frames (it used to wait 180 ms);
    # hovering the cover while it runs does not restart it (a visible false start).
    result = page.run(
        POLYCHROME
        + """
        watch();
        await __setFocused(false); await __setFocused(true);
        await __tick(40);
        const woke = [...changes];
        await pointer('pointerover'); await pointer('pointermove');
        await __tick(2100);
        return {woke, all: changes};
        """,
        state={"home": {"new_ready": [READY]}},
    )
    assert result.value["woke"] == ["polychrome-wake"]
    assert result.value["all"] == ["polychrome-wake", "idle"]


def test_polychrome_hover_has_delegated_rerender_safe_fallback(page) -> None:
    result = page.run(
        POLYCHROME
        + """
        const out = {};
        await pointer('pointerover');
        out.hover = cover().className.includes('polychrome-hover-wake');
        await __tick(2100);
        // The card moves to another section and is drawn anew under the pointer. The new cover
        // gets no pointerover; the first pointermove starts the shine once, more moves do not restart it.
        const old = cover();
        ui.state.home = {...ui.state.home, new_ready: [], completed_ready: ui.state.home.new_ready};
        renderCurrent();
        out.replaced = cover() !== old;
        watch();
        await pointer('pointermove');
        await __tick(2100);
        await pointer('pointermove'); await pointer('pointermove');
        out.redrawn = [...changes];
        // Leaving and entering again is a new hover.
        await pointer('pointerout', {relatedTarget: document.body});
        await pointer('pointerover', {relatedTarget: document.body});
        out.again = cover().className.includes('polychrome-hover-wake');
        return out;
        """,
        state={"home": {"new_ready": [READY]}},
    )
    assert result.value["hover"] is True and result.value["replaced"] is True
    assert result.value["redrawn"] == ["polychrome-hover-wake", "idle"]
    assert result.value["again"] is True


def test_auto_watch_is_enabled_by_default_and_groups_threshold_fields(tmp_path: Path, page) -> None:
    cfg = AppConfig()
    assert cfg.anilist.auto_update_progress is True
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    assert load_config(path).anilist.auto_update_progress is True
    assert en("settings.autoProgress") == "Automatically mark episode watched"
    assert ru("settings.autoProgress") == "Автоматически засчитывать серию"

    # A fresh install: the switch is on; the "watched" rule below it belongs to it.
    settings = json.loads(json.dumps(make_api(tmp_path / "app")._settings_payload(), default=str))
    for lang in ("en", "ru"):
        result = page.run(
            """
            const group = () => $('settings-anilist-auto-fields');
            const out = {label: $('s_anilist_auto').closest('label').innerText.trim(), expected: t('settings.autoProgress'),
                         checked: $('s_anilist_auto').checked, rule: group().innerText.trim(),
                         ruleText: PudgeI18n.t('ui.episodeIsMarkedWatchedOnce', {shortcut: '⌃A'}),
                         nested: $('s_anilist_auto').closest('.setting-subgroup')?.contains(group()),
                         available: group().dataset.settingsAvailable};
            await __input('#s_anilist_auto', false);
            out.off = group().dataset.settingsAvailable;
            await __click('#saveSettings'); await __tick(10);
            return out;
            """,
            lang=lang,
            state={"settings": {**settings, "language": lang, "anilist_enabled": True}},
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        out = result.value
        assert out["label"] == out["expected"] and out["checked"] is True
        assert out["rule"] == out["ruleText"] and out["nested"] is True
        assert (out["available"], out["off"]) == ("1", "0")
        [[values]] = result.calls_of("save_settings")
        assert values["anilist_auto_progress"] is False


SHORTCUTS = r"""
const block = $('s_shortcut_mpv_watched').closest('.setting-block');
const shown = id => $(id).innerText.replace(/\s+/g, '');
const out = {heading: block.querySelector('h3').innerText, expected: t('settings.shortcuts'),
             recorders: [...document.querySelectorAll('#settingsContent .shortcut-recorder')].map(b => b.id),
             translate: shown('s_shortcut_mpv_translate'), explain: shown('s_shortcut_mpv_explain')};
await __click('#s_shortcut_mpv_watched');
out.recording = $('s_shortcut_mpv_watched').classList.contains('recording');
await __key('w', {metaKey: true, shiftKey: true, code: 'KeyW'}, '#s_shortcut_mpv_watched');
out.recorded = shown('s_shortcut_mpv_watched');
await __click('#saveSettings'); await __tick(10);
// App shortcuts are the standard macOS ones: Cmd+1... open the sidebar pages, Cmd+F searches.
setPage('current', true);
const pages = [...document.querySelectorAll('.nav button[data-page]')].map(b => b.dataset.page);
await __key('2', {metaKey: true}); out.second = ui.page === pages[1];
await __key('1', {metaKey: true}); out.first = ui.page;
await __key('f', {metaKey: true}); await __tick(20);
out.search = document.activeElement?.id;
setPage('settings', true);
await __key('f', {metaKey: true}); await __tick(20);
out.settingsSearch = document.activeElement?.id;
return out;
"""

# mpv with the recorded shortcut, and the variables of two removed shortcuts set.
MPV_SHORTCUTS = """
local function updates() return #sim.commands('%-%-anilist%-action update') end
sim.set('duration', 1440); sim.set('time-pos', 100); sim.fire('file-loaded')
assert(sim.press('Meta+Shift+w'), 'mark watched is not bound')
assert(updates() == 1 and #sim.commands('%-%-manual') == 1, 'mark watched did not count the episode')
assert(sim.bindings.pudge_anilist_update.forced, 'mark watched can be shadowed by input.conf')
assert(not sim.press('Ctrl+a'), 'the default shortcut is still bound')
assert(sim.bindings.pudge_subtitle_translate.key == 'Ctrl+t' and sim.bindings.pudge_subtitle_translate.forced)
assert(not sim.press('Ctrl+b') and not sim.press('c'), 'removed AniList shortcuts are bound')
"""


def test_only_mpv_shortcuts_are_configurable_and_app_shortcuts_are_standard(tmp_path: Path, page) -> None:
    cfg = AppConfig()
    cfg.shortcuts.mpv_mark_watched = "Ctrl+w"
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded.shortcuts.mpv_mark_watched == "Ctrl+w"
    assert not hasattr(loaded.shortcuts, "app_watching")
    assert en("settings.shortcuts") == "Shortcuts"

    result = page.run(
        SHORTCUTS,
        state={
            "settings": {
                "shortcut_mpv_mark_watched": "Ctrl+a",
                "shortcut_mpv_translate_subtitle": "control+option+t",
                "shortcut_mpv_explain_subtitle": "Cmd+Alt+Shift+e",
            }
        },
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    out = result.value
    assert out["heading"] == out["expected"]
    # Only mpv actions, the review buttons and the system-wide Screen text panel are
    # recorded; nothing for the app's own standard shortcuts.
    assert [id for id in out["recorders"] if not id.startswith("s_review_sc_")] == [
        "s_shortcut_mpv_watched",
        "s_shortcut_mpv_translate",
        "s_shortcut_mpv_explain",
        "s_shortcut_mpv_skip",
        "s_shortcut_screen_text",
    ]
    assert (out["translate"], out["explain"]) == ("⌃⌥T", "⌘⌥⇧E")
    assert out["recording"] is True and out["recorded"] == "⌘⇧W"
    [[values]] = result.calls_of("save_settings")
    assert values["shortcut_mpv_mark_watched"] == "Meta+Shift+w"
    assert not [key for key in values if key.startswith("shortcut_app")]
    assert out["second"] is True and out["first"] == "current"
    assert (out["search"], out["settingsSearch"]) == ("globalSearchInput", "settingsSearch")


@needs_lua
def test_recorded_shortcut_is_the_only_mark_watched_key_in_mpv(tmp_path: Path, monkeypatch) -> None:
    # "Meta+Shift+w" is what Settings records for ⌘⇧W (test above); cli.py passes it to mpv.
    def configure(config: AppConfig) -> None:
        config.shortcuts.mpv_mark_watched = "Meta+Shift+w"

    env = _tracker_env(tmp_path / "mpv", monkeypatch, configure)
    assert env["PUDGE_SHORTCUT_MARK_WATCHED"] == "Meta+Shift+w"
    env.update({"PUDGE_SHORTCUT_OPEN_ANILIST": "Ctrl+b", "PUDGE_SHORTCUT_CORRECT_MATCH": "c"})
    run_mpv_scenario(tmp_path, MPV_SHORTCUTS, scripts=[ANILIST], env=env)


def test_library_uses_relative_episode_number_and_singular_label(tmp_path: Path, page) -> None:
    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=5,
        title="BLEACH: Sennen Kessen-hen - Kashin-tan",
        status="CURRENT",
        progress=2,
        episodes=13,
        format="TV",
        media_status="RELEASING",
    )
    api.manager.db.upsert_anime(anime)
    api.manager.db.store_relation_graph(bleach_graph(), refreshed_at=1.0, next_refresh_at=9999999999.0)
    video = tmp_path / "bleach-43.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=5, title=anime.title, episode=43, video_path=video, state="waiting_subtitles")
    )

    library = api._library_payloads({5: anime})
    assert library[0]["episodes"][0]["episode"] == 3
    assert library[0]["episodes"][0]["stored_episode"] == 43

    payload = api._anime_payload(anime)
    assert payload["local"] is not None
    assert payload["local"]["episode"] == 3
    assert payload["local"]["state"] == "waiting_subtitles"

    assert en("label.libraryRangeSingle", episode=3) == "Episode: 3"
    assert ru("label.libraryRangeSingle", episode=3) == "Серия: 3"
    # The library card of this title (the backend's payload) names its one episode in the singular.
    group = json.loads(json.dumps(library[0], default=str))
    for lang in ("en", "ru"):
        card = page.run(
            """
            const box = document.createElement('div');
            box.innerHTML = libraryCard(GROUP);
            const several = document.createElement('div');
            several.innerHTML = libraryCard({...GROUP, episodes: [1, 2, 3, 5].map(episode => ({...GROUP.episodes[0], episode}))});
            return {one: box.innerText, single: t('label.libraryRangeSingle', {episode: 3}),
                    several: several.innerText, ranges: t('label.libraryRanges', {episodes: formatEpisodeRanges([1, 2, 3, 5])})};
            """.replace("GROUP", json.dumps(group)),
            lang=lang,
        ).value
        assert card["single"] in card["one"]
        assert card["ranges"] in card["several"] and card["single"] not in card["several"]


def test_highest_resolution_prefers_higher_standard_resolution(page) -> None:
    score_2160, _ = _quality_score("Anime 2160p WEB-DL", "highest")
    score_1440, _ = _quality_score("Anime 1440p WEB-DL", "highest")
    score_1080, _ = _quality_score("Anime 1080p WEB-DL", "highest")
    score_720, _ = _quality_score("Anime 720p WEB-DL", "highest")
    assert score_2160 > score_1440 > score_1080 > score_720
    assert SubsPleaseClient.feed_url("highest").endswith("?r=1080")

    # Settings offers every standard resolution and "highest"; the choice is saved.
    result = page.run(
        """
        const options = [...$('s_resolution').options].map(option => [option.value, option.textContent]);
        await __input('#s_resolution', 'highest');
        await __click('#saveSettings'); await __tick(10);
        return {options, highest: t('settings.resolutionHighest')};
        """,
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    assert result.value["options"] == [
        ["480p", "480p"],
        ["720p", "720p"],
        ["1080p", "1080p"],
        ["1440p", "1440p"],
        ["2160p", "2160p / 4K"],
        ["highest", result.value["highest"]],
    ]
    assert result.calls_of("save_settings")[0][0]["preferred_resolution"] == "highest"


def test_energy_diagnostics_is_opt_in_and_collects_related_process_activity(monkeypatch) -> None:
    rows = [
        {
            "pid": 100,
            "ppid": 1,
            "cpu_percent": 4.0,
            "memory_percent": 1.0,
            "rss_mb": 50.0,
            "elapsed": "1:00",
            "command": "pudge",
        },
        {
            "pid": 101,
            "ppid": 100,
            "cpu_percent": 6.0,
            "memory_percent": 2.0,
            "rss_mb": 70.0,
            "elapsed": "0:40",
            "command": "WebKit WebContent",
        },
        {
            "pid": 202,
            "ppid": 1,
            "cpu_percent": 10.0,
            "memory_percent": 3.0,
            "rss_mb": 100.0,
            "elapsed": "4:00",
            "command": "/opt/homebrew/bin/mpv /tmp/a.mkv",
        },
        {
            "pid": 303,
            "ppid": 100,
            "cpu_percent": 99.0,
            "memory_percent": 0.0,
            "rss_mb": 2.0,
            "elapsed": "0:00",
            "command": "ps axo pid=,ppid=,%cpu=,%mem=,rss=,etime=,command=",
        },
    ]
    monkeypatch.setattr("pudge.energy_diagnostics.os.getpid", lambda: 100)
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "_process_rows", staticmethod(lambda: rows))
    monitor = EnergyDiagnosticsMonitor(interval_seconds=30)
    sample = monitor.sample()
    assert sample["energy_metric"] == "cpu_activity_proxy"
    assert sample["related_cpu_percent"] == 20.0
    assert {row["pid"] for row in sample["processes"]} == {100, 101, 202}
    assert en("settings.energyDiagnostics") == "Energy diagnostics"
