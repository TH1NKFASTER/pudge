"""Media pages unified, torrent switch, custom dropdowns, macOS identity, refresh order and
listening recovery of v0.7.22 (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from pudge.branding import APP_BUNDLE_ID, APP_NAME
from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi
from tests.css_support import css_rules


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.qbittorrent.enabled = True
    cfg.aria2.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


AUDIOBOOK = {"id": 1, "title": "Book", "series_key": "", "duration": 100, "position": 0, "progress": 0}
IMPORT = r"""
const out = {visible: {}};
for (const name of ['current', 'lightnovels', 'manga', 'audiobooks', 'planned', 'settings']) {
  setPage(name, true); await __tick(30); out.visible[name] = __visible('#pageImportButton');
}
// Light novels and manga: the top button opens their file / folder choosers.
setPage('lightnovels', true); await __tick(30); await __click('#pageImportButton'); await __tick(30);
setPage('manga', true); await __tick(30); await __click('#pageImportButton'); await __tick(30);
// Audiobooks: no header or buttons of their own; the top button offers a file or a folder.
setPage('audiobooks', true); await __tick(30);
out.page = {headers: document.querySelectorAll('#audiobooksContent h1, #audiobooksContent h2').length,
            buttons: [...document.querySelectorAll('#audiobooksContent > button')].filter(button => button.checkVisibility()).length,
            folderButton: Boolean($('audiobookImportFolder'))};
await __click('#pageImportButton'); await __tick(10);
out.menu = [...document.querySelectorAll('#audiobookImportMenu [data-audio-import-kind]')].filter(b => b.checkVisibility()).map(b => b.innerText);
out.texts = [t('media.file'), t('media.folder')];
await __click('#audiobookImportMenu [data-audio-import-kind="folder"]'); await __tick(50);
out.menuAfter = __visible('#audiobookImportMenu');
out.imported = __text('#audiobooksContent').includes('Book');
await __click('#pageImportButton'); await __tick(10);
await __click('#audiobookImportMenu [data-audio-import-kind="file"]'); await __tick(50);
return out;
"""


def test_audiobooks_share_top_import_and_have_no_duplicate_header_or_folder_button(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            IMPORT,
            lang=lang,
            api={
                "choose_light_novel_file": {"cancelled": True},
                "choose_manga_folder": {"cancelled": True},
                "choose_audiobook_folder": {"cancelled": False, "state": {"books": [AUDIOBOOK]}},
                "choose_audiobook_file": {"cancelled": True},
            },
        )
        out = result.value
        assert out["visible"] == {
            "current": False,
            "lightnovels": True,
            "manga": True,
            "audiobooks": True,
            "planned": False,
            "settings": False,
        }
        assert result.called("choose_light_novel_file") and result.called("choose_manga_folder")
        assert out["page"] == {"headers": 0, "buttons": 0, "folderButton": False}
        assert out["menu"] == out["texts"]
        assert out["menuAfter"] is False and out["imported"] is True
        assert [call["name"] for call in result.calls if call["name"].startswith("choose_audiobook")] == [
            "choose_audiobook_folder",
            "choose_audiobook_file",
        ]


# The local endpoints behind the sidebar torrent switch.
TORRENT_FETCH = """
let torrentsOn = false;
window.fetch = async (url, init) => {
  if (String(url).endsWith('/api/torrents/enabled')) {
    torrentsOn = JSON.parse(init.body).enabled;
    return new Response(JSON.stringify({ok: true, enabled: torrentsOn, transition: torrentsOn ? 'on' : 'off_requested'}), {status: 200});
  }
  return new Response(JSON.stringify({ok: true, enabled: torrentsOn, desired_enabled: torrentsOn}), {status: 200});
};
"""


def test_torrent_toggle_is_red_off_green_on_and_download_modal_has_no_duplicate_global_controls(page) -> None:
    rules = css_rules("index.html")
    on, off = (
        rules.declarations("#torrentToggleButton.torrent-on"),
        rules.declarations("#torrentToggleButton.torrent-off"),
    )
    assert (on["background"], on["border-color"]) == ("#173d2b", "#46c77e")  # green
    assert (off["background"], off["border-color"]) == ("#4a2028", "#ef6575")  # red
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const classes = () => $('torrentToggleButton').className.split(/\s+/).filter(name => name.startsWith('torrent-'));
            const out = {off: classes()};
            await __click('#torrentToggleButton'); await __tick(10);
            out.on = classes();
            // The downloads window lists torrents only; the switch lives in the sidebar.
            await openDownloadCenter(); await __tick(10);
            out.actions = [...new Set([...document.querySelectorAll('#modalBody [data-action]')].map(b => b.dataset.action))];
            out.text = __text('#modalBody');
            return out;
            """,
            lang=lang,
            before=TORRENT_FETCH,
            api={
                "torrent_downloads": {
                    "backend": "aria2",
                    "warning": "",
                    "downloads": [
                        {
                            "name": "Show 03",
                            "torrent_hash": "abc",
                            "state": "downloading",
                            "progress": 0.4,
                            "media_id": 7,
                        }
                    ],
                }
            },
        )
        out = result.value
        assert (out["off"], out["on"]) == (["torrent-off"], ["torrent-on"])
        assert not {"torrent-toggle", "torrent-stop-all"} & set(out["actions"])
        assert {"torrent-pause", "torrent-remove"} <= set(out["actions"])
        assert "Client" not in out["text"] and "Клиент" not in out["text"]


SELECTS = r"""
const select = $('s_resolution'), shell = select.closest('.pudge-select');
if (!shell) return {wrapped: false};
const button = shell.querySelector('.pudge-select-button');
const openMenu = () => document.querySelector('.pudge-select-menu.open');
const menusShown = () => [...document.querySelectorAll('.pudge-select-menu')].filter(menu => menu.checkVisibility()).length;
const out = {wrapped: true, label: button.innerText.replace(/\s*⌄$/, ''), nativeShown: __visible('#s_resolution'), menusShown: menusShown()};
button.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, button: 0}));
out.options = [...openMenu().querySelectorAll('.pudge-select-option')].map(option => [option.innerText, option.classList.contains('selected')]);
out.menusShownOpen = menusShown();
let changed = null;
select.addEventListener('change', () => { changed = select.value; });
button.focus();
openMenu().querySelectorAll('.pudge-select-option')[5].dispatchEvent(new MouseEvent('click', {bubbles: true}));
await __tick(10);
out.chosen = {value: select.value, changed, label: button.innerText.replace(/\s*⌄$/, ''), open: Boolean(openMenu()),
              focused: document.activeElement === button};
// A select drawn later is wrapped too; the LN chapter picker keeps its own control.
const late = document.createElement('select');
late.innerHTML = '<option value="a">A</option><option value="b" selected>B</option>';
$('settingsContent').appendChild(late);
await __tick(10);
out.late = late.closest('.pudge-select')?.querySelector('.pudge-select-button')?.innerText.replace(/\s*⌄$/, '') ?? null;
out.chapterWrapped = Boolean($('lnChapterSelect').closest('.pudge-select'));
return out;
"""


def test_system_selects_are_wrapped_in_pudge_dropdown_without_replacing_ln_chapter_picker(page) -> None:
    result = page.run(SELECTS, state={"settings": {"nyaa_enabled": True, "preferred_resolution": "1080p"}})
    out = result.value
    assert out["wrapped"] is True
    assert (out["label"], out["nativeShown"], out["menusShown"]) == ("1080p", False, 0)
    assert [label for label, _selected in out["options"]][:5] == [
        "480p",
        "720p",
        "1080p",
        "1440p",
        "2160p / 4K",
    ]
    assert [selected for _label, selected in out["options"]] == [False, False, True, False, False, False]
    assert out["menusShownOpen"] == 1
    # Choosing works like the native control, closes the menu and leaves no focus ring behind.
    assert out["chosen"] == {
        "value": "highest",
        "changed": "highest",
        "label": out["options"][5][0],
        "open": False,
        "focused": False,
    }
    assert out["late"] == "B"
    assert out["chapterWrapped"] is False
    rules = css_rules("pudge_select.css")
    assert rules.get(".pudge-select-option.selected", "background") == "#29496f"


def test_macos_identity_is_set_before_nsapplication_registration(monkeypatch) -> None:
    # Registering Cocoa first under Homebrew's interpreter name makes Force Quit show "Python".
    from pudge import app_entry

    events: list[tuple] = []

    class Info:
        def setObject_forKey_(self, value, key):
            events.append(("info", key, value))

    class Process:
        def setProcessName_(self, name):
            events.append(("process name", name))

    class Application:
        def setActivationPolicy_(self, policy):
            events.append(("activation policy", policy))

        def setApplicationIconImage_(self, image):
            events.append(("icon", image))

    class Image:
        @staticmethod
        def alloc():
            return Image()

        def initWithContentsOfFile_(self, path):
            return f"image:{path}"

    appkit = types.SimpleNamespace(
        NSApplication=types.SimpleNamespace(
            sharedApplication=lambda: events.append(("NSApplication",)) or Application()
        ),
        NSApplicationActivationPolicyRegular="regular",
        NSImage=Image,
    )
    foundation = types.SimpleNamespace(
        NSBundle=types.SimpleNamespace(
            mainBundle=lambda: types.SimpleNamespace(infoDictionary=lambda: Info())
        ),
        NSProcessInfo=types.SimpleNamespace(processInfo=lambda: Process()),
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PUDGE_APP_ICON", "/app/icon.icns")

    app_entry._set_macos_app_icon()

    assert events == [
        ("process name", APP_NAME),
        ("info", "CFBundleName", APP_NAME),
        ("info", "CFBundleDisplayName", APP_NAME),
        ("info", "CFBundleIdentifier", APP_BUNDLE_ID),
        ("NSApplication",),
        ("activation policy", "regular"),
        ("icon", "image:/app/icon.icns"),
    ]


def test_torrent_off_still_reconciles_completed_episode_and_pauses_backend(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    manager.config.nyaa.torrents_enabled = False
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            status="CURRENT",
            progress=6,
            episodes=13,
        )
    )
    target = manager.config.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    target.mkdir(parents=True)
    video = target / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")
    item = DownloadItem(
        torrent_hash="f3a099e5c9a9519b1b406a23202f94e1e9d07157",
        name=video.name,
        state="complete",
        progress=1.0,
        save_path=str(target),
        content_path=str(video),
        media_id=210031,
        episode=7,
        media_episode=7,
        release_episode=19,
        completed_on=1234,
        raw={"category": manager.config.qbittorrent.category, "_tag_set": ["pudge"]},
    )
    paused: list[str] = []

    class Client:
        def torrents(self, *, category: str = ""):
            return [item]

        def pause(self, torrent_hash: str) -> None:
            paused.append(torrent_hash)

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "torrent_clients", lambda: [("qbittorrent", Client())])
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_args, **_kwargs: ("none", None))

    assert manager.downloads_enabled() is False
    assert manager.sync_downloads() == 1
    episodes = manager.db.episodes(210031)
    assert len(episodes) == 1
    assert episodes[0].episode == 7
    assert episodes[0].release_episode == 19
    assert episodes[0].video_path == video.resolve()
    assert paused == [item.torrent_hash]


def test_diagnosis_repairs_stale_waiting_row_when_prepared_text_subtitle_exists(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config.qbittorrent.enabled = False
    manager.db.upsert_anime(LibraryAnime(media_id=200637, title="100 Girlfriends S3", status="CURRENT"))
    video = tmp_path / "episode6.mkv"
    subtitle = tmp_path / "prepared.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=6,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="waiting_subtitles",
        )
    )

    diagnosis = manager.diagnose_episode(200637, 6)
    assert diagnosis["ready"] is True
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None and repaired.state == "ready"
    assert repaired.subtitle_path == subtitle


def test_watched_media_folders_route_ln_manga_and_audiobooks_and_are_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    watched = tmp_path / "watched"
    audio = watched / "Audio Book"
    watched.mkdir(parents=True)
    audio.mkdir()
    (watched / "novel.txt").write_text(
        "これは日本語のライトノベル本文です。物語が続きます。" * 30, encoding="utf-8"
    )
    (watched / "comic.cbz").write_bytes(b"not-an-archive-because-import-is-mocked")
    (watched / "single.m4b").write_bytes(b"audio")
    (audio / "01.mp3").write_bytes(b"one")
    (audio / "02.mp3").write_bytes(b"two")
    cfg.paths.download_dirs = [watched]
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        api.light_novels, "import_file", lambda path, **_kwargs: calls.append(("ln", Path(path))) or {}
    )
    monkeypatch.setattr(api.manga, "import_file", lambda path: calls.append(("manga", Path(path))) or {})
    monkeypatch.setattr(
        api.audiobooks, "import_file", lambda path: calls.append(("audio-file", Path(path))) or {}
    )
    monkeypatch.setattr(
        api.audiobooks, "import_folder", lambda path: calls.append(("audio-folder", Path(path))) or {}
    )

    first = api.scan_watched_media_folders()
    second = api.scan_watched_media_folders()

    assert first["light_novels"] == 1
    assert first["manga"] == 1
    assert first["audiobooks"] == 2
    assert second["light_novels"] == second["manga"] == second["audiobooks"] == 0
    assert ("audio-folder", audio.resolve()) in calls
    assert ("audio-file", (watched / "single.m4b").resolve()) in calls


def test_refresh_scans_watched_and_video_sources_before_heavy_refresh(tmp_path: Path, monkeypatch) -> None:
    # Refresh first picks up what is in the watched folders (books, then videos), then
    # runs the slow part (release search, torrents, subtitles) with the videos already known.
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "novel.txt").write_text("これは日本語のライトノベル本文です。" * 40, encoding="utf-8")
    (watched / "[Group] Show - 01 (1080p).mkv").write_bytes(b"video")
    cfg.paths.download_dirs = [watched]
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    steps: list[str] = []
    monkeypatch.setattr(
        api.light_novels,
        "import_file",
        lambda path, **_kwargs: steps.append(f"import {Path(path).name}") or {},
    )
    scan = api.manager.scan_library
    monkeypatch.setattr(
        api.manager, "scan_library", lambda **kwargs: steps.append(f"videos {kwargs}") or scan(**kwargs)
    )
    monkeypatch.setattr(
        api.manager,
        "run_interactive_refresh",
        lambda **kwargs: steps.append(f"heavy {kwargs}") or {"auto": 0},
    )

    stats = api.refresh_local()["stats"]

    assert steps == [
        "import novel.txt",
        "videos {'reuse_unchanged': True, 'user_requested': True}",
        "heavy {'pre_scanned_library_count': 0}",
    ]
    assert stats["watched_light_novels"] == 1


def _stale_waiting_episode(tmp_path: Path):
    """A prepared subtitle exists, but the row still says "waiting for subtitles"."""
    from test_web_app import make_api

    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=200637, title="100 Girlfriends S3", status="CURRENT", progress=5, episodes=12)
    )
    video = tmp_path / "library" / "episode6.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    subtitle = tmp_path / "prepared.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語\n", encoding="utf-8")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="100 Girlfriends S3",
            episode=6,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="waiting_subtitles",
        )
    )
    return api


def test_ready_diagnosis_refreshes_stale_card_state(tmp_path: Path, page) -> None:
    # "Why not ready?" finds the episode ready (and repairs the row): the card leaves
    # "waiting" at once instead of staying stale until the next poll.
    api = _stale_waiting_episode(tmp_path)
    plain = lambda value: json.loads(json.dumps(value, default=str))  # noqa: E731
    before = plain(api.get_state())
    diagnosis = plain(api.diagnose_episode(200637, 6))
    after = plain(api.get_state_fast())
    assert [item["media_id"] for item in before["home"]["waiting"]] == [200637]
    assert diagnosis["ready"] is True
    # The window's snapshot is older than the prepared subtitle: no subtitle yet, so the card
    # offers the diagnosis.
    before["home"]["waiting"][0]["local"].update(subtitle_source="none", subtitle_path=None)
    backend = (
        f"const AFTER = {json.dumps(after)}, DIAGNOSIS = {json.dumps(diagnosis)};"
        "__api.diagnose_episode = async () => { __api.get_state = __api.get_state_fast = async () => AFTER; return DIAGNOSIS; };"
    )
    result = page.run(
        backend
        + r"""
        setPage('current', true);
        const section = () => document.querySelector('#currentContent [data-media-id="200637"]')?.closest('.home-section')?.className;
        const out = {before: section()};
        await __click('#currentContent [data-media-id="200637"]');
        await __tick(10);
        out.after = section();
        out.title = __text('#modalTitle');
        return out;
        """,
        state=before,
    )
    assert result.calls_of("diagnose_episode") == [[200637, 6]]
    assert "ready-section" not in result.value["before"]
    assert "ready-section" in result.value["after"]
    assert result.value["title"].endswith("100 Girlfriends S3")


# Listen together: three words of 10 characters each; `at` is the audio clock.
LISTEN = r"""
const reader = document.getElementById('lnReader');
reader.innerHTML = '<p data-ln-paragraph="0" data-ln-audio-start="0" data-ln-audio-end="30">'
  + ['一', '二', '三'].map((w, i) => `<span class="ln-word" data-ln-token="${i}" data-ln-audio-start="${i * 10}" data-ln-audio-end="${i * 10 + 10}">${w}</span>`).join('')
  + '</p>';
ui.lnChapter = {chapter_index: 0};
const at = position => ({alignment_mode: 'stt', playing: true, ln_chapter_index: 0, position});
// "*" marks the highlighted word, then its filled part.
const words = () => [...reader.querySelectorAll('.ln-word')].map(word =>
  (word.classList.contains('ln-paired-word-current') ? '*' : '') + (word.style.getPropertyValue('--ln-paired-word-progress') || '-'));
"""


def test_ln_paired_resume_snaps_visual_clock_without_catchup(page) -> None:
    result = page.run(
        LISTEN
        + r"""
        const out = {};
        renderLnPairedPosition(at(1.0), 15, {speechActive: true}); out.playing = words();
        // While speaking, a clock that ticks slightly back does not pull the highlight back.
        renderLnPairedPosition(at(1.2), 14.5, {speechActive: true}); out.jitter = words();
        renderLnPairedPosition(at(1.4), 16, {speechActive: false}); out.paused = words();
        // Speech resumes a little behind the shown position: the highlight snaps to it.
        renderLnPairedPosition(at(1.6), 14.2, {speechActive: true}); out.resumedBehind = words();
        // After a longer pause it jumps straight to where the speech is, with no catch-up run.
        renderLnPairedPosition(at(1.8), 16, {speechActive: true});
        renderLnPairedPosition(at(2.0), 17, {speechActive: false});
        renderLnPairedPosition(at(4.0), 27, {speechActive: true}); out.resumedAhead = words();
        return out;
        """
    )
    out = result.value
    assert out["playing"] == out["jitter"] == out["paused"] == ["-", "*50.0%", "-"]
    assert out["resumedBehind"] == ["-", "*42.0%", "-"]
    assert out["resumedAhead"] == ["-", "100%", "*70.0%"]
