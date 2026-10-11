from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.local_search import find_local_subtitles
from pudge.models import VideoIdentity
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru


def test_new_config_does_not_implicitly_scan_downloads(tmp_path: Path):
    loaded = load_config(tmp_path / "missing.toml")
    assert loaded.paths.download_dirs == []
    assert loaded.paths.subtitle_dirs == []


def test_old_download_dirs_migrate_to_visible_subtitle_dirs(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    configured = tmp_path / "configured"
    config_path.write_text(
        f'[paths]\ndownload_dirs = ["{configured}"]\n',
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    assert loaded.paths.subtitle_dirs == [configured.resolve()]
    assert loaded.paths.download_dirs == []


def test_empty_subtitle_folder_round_trips(tmp_path: Path):
    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.paths.subtitle_dirs = []
    write_config(config, path)
    loaded = load_config(path)
    assert loaded.paths.subtitle_dirs == []
    # Written explicitly, so the next load does not migrate old download folders into it.
    assert tomllib.loads(path.read_text(encoding="utf-8"))["paths"]["subtitle_dirs"] == []


def test_local_search_ignores_unconfigured_downloads(tmp_path: Path):
    video_dir = tmp_path / "library" / "Anime"
    video_dir.mkdir(parents=True)
    video = video_dir / "Example - 05.mkv"
    video.write_bytes(b"video")

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "Example - 05.ja.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n日本語です\n",
        encoding="utf-8",
    )

    identity = VideoIdentity(title="Example", episode=5)
    candidates = find_local_subtitles(
        video,
        identity,
        subtitle_dirs=[],
        cache_dir=tmp_path / "cache",
        max_files=100,
    )
    assert candidates == []

    configured = find_local_subtitles(
        video,
        identity,
        subtitle_dirs=[downloads],
        cache_dir=tmp_path / "cache",
        max_files=100,
    )
    assert configured and configured[0].path.parent == downloads


def test_permission_preflight_is_not_repeated_after_first_run(monkeypatch, tmp_path: Path):
    library = tmp_path / "library"
    subtitles = tmp_path / "subs"
    library.mkdir()
    subtitles.mkdir()
    captured: list[Path] = []

    def fake_request(paths):
        captured.extend(Path(path) for path in paths)
        return {str(path): True for path in paths}

    monkeypatch.setattr("pudge.web_app.request_folder_access", fake_request)

    api = WebAppApi.__new__(WebAppApi)
    api.config = AppConfig()
    api.config.library.root_dir = library
    api.config.paths.download_dirs = [tmp_path / "Downloads-legacy"]
    api.config.paths.subtitle_dirs = [subtitles]
    api.config.ui.permissions_requested = True
    api.config_path = tmp_path / "config.toml"
    api.logger = logging.getLogger("test-permissions")
    api._settings_payload = lambda: {"permissions_requested": True}

    result = api.request_permissions()

    assert result["ok"] is True
    assert captured == []
    assert result["folders"] == {}


def test_settings_edit_optional_subtitle_folders(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            const label = id => document.querySelector(`label[for=${id}]`)?.innerText;
            const out = {subtitles: $('s_subtitle_folders').value, watched: $('s_watched_folders').value,
                         labels: [label('s_subtitle_folders'), label('s_watched_folders')],
                         expected: [t('settings.subtitleFolders'), t('settings.watchedFolders')]};
            await __click('#chooseSubtitleFolder'); await __tick(10);
            out.chosen = $('s_subtitle_folders').value;
            await __click('#saveSettings'); await __tick(10);
            await __click('#disableSubtitleFolder');
            out.disabled = $('s_subtitle_folders').value;
            return out;
            """,
            lang=lang,
            state={"settings": {"subtitle_folders": "/Users/me/Subs", "watched_folders": "/Users/me/Anime"}},
            api={"choose_subtitle_folder": {"path": "/Volumes/Disk/Subs", "access": True}},
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        out = result.value
        assert out["labels"] == out["expected"]
        assert (out["subtitles"], out["watched"]) == ("/Users/me/Subs", "/Users/me/Anime")
        assert out["chosen"] == "/Users/me/Subs\n/Volumes/Disk/Subs"
        [[saved]] = result.calls_of("save_settings")
        assert saved["subtitle_folders"] == out["chosen"] and saved["watched_folders"] == "/Users/me/Anime"
        assert out["disabled"] == ""  # optional: can be switched off


def test_first_start_guide_offers_an_optional_subtitle_folder(page) -> None:
    assert en("settings.subtitleFolder") == "Subtitle Inbox"
    assert ru("settings.subtitleFolder") == "Subtitle Inbox"
    assert en("onboarding.subtitleFolderTitle") == "Choose a subtitle files folder (optional)"
    assert ru("onboarding.subtitleFolderTitle") == "Выберите папку с файлами субтитров (необязательно)"
    code = """
    const next = async () => { await __click('#onboardingNext'); await __tick(20); };
    while (ui.onboardingStep < 4) await next();
    const out = {label: __text('label[for=o_subtitle_folder]'),
                 expected: t('settings.subtitleFolder'), shown: __visible('#o_subtitle_fields')};
    await __input('#o_subtitle_folder', '/Users/me/Subs');
    if (UNCHECK) { await __input('#o_subtitle_enabled', false); out.hidden = !__visible('#o_subtitle_fields'); }
    while (ui.onboardingStep < 5) await next();
    await next();
    return out;
    """
    first_start = {
        "state": {"settings": {"onboarding_completed": False}},
        "api": {"first_experience_dependencies": {"mpv": {"installed": True}, "ffmpeg": {"installed": True}}},
        "before": "__api.complete_onboarding = async draft => ({settings: {...draft, onboarding_completed: true, light_novels: {}}});",
    }
    for lang in ("en", "ru"):
        kept = page.run(code.replace("UNCHECK", "false"), lang=lang, **first_start)
        assert kept.value["shown"] is True and kept.value["label"] == kept.value["expected"]
        [[draft]] = kept.calls_of("complete_onboarding")
        assert draft["subtitle_folder"] == "/Users/me/Subs"
    skipped = page.run(code.replace("UNCHECK", "true"), **first_start)
    assert skipped.value["hidden"] is True
    [[draft]] = skipped.calls_of("complete_onboarding")
    assert draft["subtitle_folder"] == ""


ASK = "await __tick(50); return {toasts: __toasts(), asked: t('toast.permissionsRequested')};"


def test_first_start_asks_for_folder_access_before_any_other_work(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(ASK, lang=lang, state={"settings": {"permissions_requested": False}})
        names = [call["name"] for call in result.calls]
        assert (
            names.index("request_permissions")
            < names.index("startup_sync_anilist")
            < names.index("startup_maintenance")
        )
        assert result.value["asked"] in result.value["toasts"]
    # Later starts: the backend skips the preflight (test above) and the window says nothing.
    later = page.run(ASK, state={"settings": {"permissions_requested": True}})
    assert later.value["asked"] not in later.value["toasts"]
    # The setup guide opens only after the access request.
    guide = page.run(
        "await __tick(50); return window.guideOpenAtRequest;",
        state={"settings": {"onboarding_completed": False, "permissions_requested": False}},
        before="""__api.request_permissions = async () => {
          window.guideOpenAtRequest = document.getElementById('onboardingBackdrop').classList.contains('open');
          return {settings: {...(await __api.get_state()).settings, permissions_requested: true}, folders: {}};
        };""",
    )
    assert guide.value is False and guide.called("request_permissions")
