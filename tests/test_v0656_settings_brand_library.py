"""Settings, branding and library details of v0.6.56 (behaviour; audit 71Б rewrite).
Window checks run the real index.html (tests/page_harness.py)."""

from __future__ import annotations

import json
from pathlib import Path

from pudge.branding import APP_BUNDLE_ID, APP_NAME, APP_SLUG
from pudge.database import Database
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor
from pudge.library import scan_library
from pudge.manager_models import LibraryAnime, LibraryEpisode

from tests.i18n_support import has_text

ROOT = Path(__file__).parents[1]

SETTINGS = r"""
setPage('settings', true); await __tick(100);
const recorder = $('s_shortcut_mpv_watched');
const out = {kind: recorder.tagName + ':' + recorder.type, initial: recorder.innerText,
  expected: {disabled: t('label.disabled'), press: t('action.pressShortcut')}};
await __click('#s_shortcut_mpv_watched'); out.recording = recorder.innerText;
await __key('w', {metaKey: true, shiftKey: true}); out.captured = [recorder.innerText.split('\n').join(''), recorder.dataset.shortcutValue];
await __click('#s_shortcut_mpv_watched'); await __key('Backspace'); out.cleared = [recorder.innerText, recorder.dataset.shortcutValue];
await __click('#s_shortcut_mpv_watched'); await __key('k', {metaKey: true});
out.recorders = [...document.querySelectorAll('#settingsContent .shortcut-recorder:not([data-review-binding])')].map(el => el.id);
out.upgradeToggle = Boolean($('s_auto_upgrade_subtitles'));
out.text = $('settingsContent').textContent;
await __click('#saveSettings'); await __tick(50);
return out;
"""


def test_mpv_shortcuts_are_recorded_by_pressing_them(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(SETTINGS, lang=lang)
        out = result.value
        # A press-to-record button, not a text field; Backspace/Delete clears the shortcut.
        assert out["kind"] == "BUTTON:button"
        assert (out["initial"], out["recording"]) == (out["expected"]["disabled"], out["expected"]["press"])
        assert out["captured"] == ["⌘⇧W", "Meta+Shift+w"]
        assert out["cleared"] == [out["expected"]["disabled"], ""]
        # mpv's own shortcuts and the system-wide Screen text shortcut are configurable;
        # Pudge's standard app shortcuts are not.
        assert out["recorders"] == [
            "s_shortcut_mpv_watched",
            "s_shortcut_mpv_translate",
            "s_shortcut_mpv_explain",
            "s_shortcut_mpv_skip",
            "s_shortcut_screen_text",
        ]
        [[saved]] = result.calls_of("save_settings")
        assert saved["shortcut_mpv_mark_watched"] == "Meta+k"
        assert not [key for key in saved if key.startswith("shortcut_app_")]
        # The subtitle upgrade search has no switch any more: it is always on.
        assert out["upgradeToggle"] is False and saved["auto_upgrade_subtitles"] is True
        # The qBittorrent help no longer explains API keys and ports.
        for removed in ("An API key does not start the server", "API key не включает сервер"):
            assert removed not in out["text"]
    assert not has_text("An API key does not start the server or choose its port.", "en")
    assert not has_text("API key не включает сервер и не задаёт его порт", "ru")


def test_cmd_digit_opens_the_nth_visible_tab(page) -> None:
    result = page.run(
        r"""
        const press = async digit => { await __key(digit, {metaKey: true}, 'body'); return ui.page; };
        const pages = [await press('2'), await press('1'), await press('8')];
        // A hidden tab is skipped: the shortcuts follow the tabs on screen.
        document.querySelector('.nav button[data-page="manga"]').hidden = true;
        pages.push(await press('3'));
        return pages;
        """
    )
    assert result.value == ["lightnovels", "current", "settings", "audiobooks"]


GRAPH = {
    "root_id": 1,
    "nodes": [
        {
            "media_id": 1,
            "title": "Show",
            "format": "TV",
            "season_year": 2019,
            "episodes": 12,
            "studio": "Studio A",
        },
        {
            "media_id": 2,
            "title": "Show: Recap Movie",
            "format": "MOVIE",
            "season_year": 2020,
            "episodes": 1,
            "studio": "Studio B",
            "cover_url": "https://img.test/2.jpg",
        },
        {"media_id": 3, "title": "Show 2", "format": "TV", "season_year": 2021, "episodes": 12},
    ],
    "edges": [
        {"source": 1, "target": 2, "relation_type": "ALTERNATIVE"},
        {"source": 1, "target": 3, "relation_type": "SEQUEL"},
    ],
}


def test_alternative_movie_covers_show_the_relation_preview_above_them(page) -> None:
    result = page.run(
        r"""
        const host = document.createElement('div'); host.id = 'graphHost'; document.body.appendChild(host);
        host.innerHTML = fullRelationGraphDiagram(window.graph);
        const preview = $('relationPreview');
        const hover = async (selector, rect) => {
          document.querySelector(selector).__rect = rect;
          await __fire(selector, 'mouseover', {}, 'MouseEvent');
          preview.__rect = {x: 0, y: 0, width: 220, height: 90};
          await __tick(20);
          return {title: __text('#relationPreview .relation-preview-title'), meta: __text('#relationPreview .relation-preview-meta'),
                  left: preview.style.left, top: preview.style.top};
        };
        return {
          shelf: [...host.querySelectorAll('.full-relation-alternatives .full-relation-alt')].map(button => button.title),
          nodes: [...host.querySelectorAll('[data-relation-node="1"]:not(.full-relation-alt)')].map(node => node.dataset.title),
          alternative: await hover('#graphHost .full-relation-alt', {x: 400, y: 300, width: 30, height: 42}),
          node: await hover('#graphHost [data-relation-node="1"]:not(.full-relation-alt)', {x: 100, y: 300, width: 80, height: 110}),
          meta: [t('label.episodesTooltip', {count: 1}), t('label.yearTooltip', {year: 2020}), t('label.studio', {studio: 'Studio B'})].join('\n'),
        };
        """,
        before=f"window.graph = {json.dumps(GRAPH)};",
    )
    out = result.value
    # The recap movie sits on the small Alternative shelf, not as a graph node.
    assert out["shelf"] == ["Show: Recap Movie"] and out["nodes"] == ["Show", "Show 2"]
    # Hovering its small cover shows the same preview as a node, centred above the cover
    # (a node's preview opens to its right).
    assert out["alternative"] == {
        "title": "Show: Recap Movie",
        "meta": out["meta"],
        "left": "305px",
        "top": "201px",
    }
    assert (out["node"]["title"], out["node"]["left"], out["node"]["top"]) == ("Show", "189px", "310px")


def test_window_takes_the_brand_name_from_the_backend(page) -> None:
    shown = "return {title: document.title, brand: __text('#appBrandName')};"
    assert page.run(shown, state={"branding": {"name": "Kappa", "slug": "kappa"}}).value == {
        "title": "Kappa",
        "brand": "Kappa",
    }
    # Before the state arrives (or without branding) the name the asset server rendered stays.
    rendered = "document.getElementById('appBrandName').textContent = 'pudge'; document.title = 'pudge';"
    assert page.run(shown, before=rendered).value == {"title": "pudge", "brand": "pudge"}


def test_managed_library_detaches_stale_catmahjong_false_match(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "Library"
    root.mkdir()
    video = root / "catmahjong.mp4"
    video.write_bytes(b"video")
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=999, title="Mahoutsukai no Yoru", synonyms=["Mahoyo"]))
    db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Mahoutsukai no Yoru",
            episode=None,
            video_path=video,
            state="local",
        )
    )
    monkeypatch.setattr(
        "pudge.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    rows = scan_library(root, db)
    assert len(rows) == 1
    assert rows[0].media_id is None
    persisted = db.episode_by_path(video)
    assert persisted is not None
    assert persisted.media_id is None
    assert persisted.title == "catmahjong"


def test_energy_log_separates_app_and_external_context(monkeypatch) -> None:
    rows = [
        {
            "pid": 100,
            "ppid": 1,
            "cpu_percent": 5.0,
            "memory_percent": 1.0,
            "rss_mb": 40.0,
            "elapsed": "02:00",
            "command": "/Applications/pudge.app/Contents/MacOS/pudge",
        },
        {
            "pid": 101,
            "ppid": 100,
            "cpu_percent": 7.0,
            "memory_percent": 1.0,
            "rss_mb": 60.0,
            "elapsed": "01:59",
            "command": "python worker.py",
        },
        {
            "pid": 102,
            "ppid": 1,
            "cpu_percent": 3.0,
            "memory_percent": 1.0,
            "rss_mb": 30.0,
            "elapsed": "01:59",
            "command": "/System/Library/Frameworks/WebKit.framework/com.apple.WebKit.GPU",
        },
        {
            "pid": 200,
            "ppid": 1,
            "cpu_percent": 11.0,
            "memory_percent": 1.0,
            "rss_mb": 80.0,
            "elapsed": "10:00",
            "command": "/Applications/qBittorrent.app/Contents/MacOS/qbittorrent",
        },
        {
            "pid": 300,
            "ppid": 1,
            "cpu_percent": 50.0,
            "memory_percent": 1.0,
            "rss_mb": 20.0,
            "elapsed": "2-00:00:00",
            "command": "/System/Library/Frameworks/WebKit.framework/com.apple.WebKit.GPU",
        },
    ]
    monkeypatch.setattr("pudge.energy_diagnostics.os.getpid", lambda: 100)
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "_process_rows", staticmethod(lambda: rows))
    sample = EnergyDiagnosticsMonitor(interval_seconds=30).sample()
    assert sample["app_cpu_percent"] == 15.0
    assert sample["context_cpu_percent"] == 11.0
    assert sample["related_cpu_percent"] == 26.0
    scopes = {row["pid"]: row["scope"] for row in sample["processes"]}
    assert scopes == {100: "app", 101: "app", 102: "app", 200: "context"}


def test_branding_has_one_source_and_no_rename_script() -> None:
    # The installer, the release build and the window all follow pudge/brand.env (see
    # test_install_script.py, test_release_bundle_v066.py and the window test above).
    assert (APP_NAME, APP_SLUG, APP_BUNDLE_ID) == ("pudge", "pudge", "com.pudge.app")
    assert not (ROOT / "rename_brand.py").exists()
    assert (ROOT / "pudge" / "legacy_install.py").is_file()
