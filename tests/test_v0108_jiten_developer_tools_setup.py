"""JitenMPV needs Pudge and mpv in macOS Privacy & Security → Developer Tools: the setup
guide and Settings explain it, open the pane, remember the confirmation, and playback warns
in the log until it is confirmed (behaviour; audit 71Б rewrite)."""

import logging
import sys
import types
from pathlib import Path

import pudge.cli as cli
import pudge.first_experience as first_experience
import pudge.web_app as web_app
from pudge.branding import APP_SLUG
from pudge.config import AppConfig, load_config, write_config
from test_web_app import make_api
from tests.i18n_support import en, ru


def test_jiten_developer_tools_confirmation_is_persisted(tmp_path: Path) -> None:
    assert AppConfig().ui.jiten_developer_tools_confirmed is False
    old = tmp_path / "old.toml"
    old.write_text('[ui]\nlanguage = "en"\n', encoding="utf-8")
    assert load_config(old).ui.jiten_developer_tools_confirmed is False
    api = make_api(tmp_path)
    assert api.get_state()["settings"]["jiten_developer_tools_confirmed"] is False
    assert (
        api.save_settings({"jiten_developer_tools_confirmed": True})["settings"][
            "jiten_developer_tools_confirmed"
        ]
        is True
    )
    assert (
        load_config(api.config.config_path, resolve_secrets=False).ui.jiten_developer_tools_confirmed is True
    )
    # A save from a window that does not send the field keeps it.
    assert api.save_settings({"language": "en"})["settings"]["jiten_developer_tools_confirmed"] is True
    cfg = AppConfig()
    cfg.ui.jiten_developer_tools_confirmed = True
    write_config(cfg, tmp_path / "written.toml")
    assert load_config(tmp_path / "written.toml").ui.jiten_developer_tools_confirmed is True


def test_backend_can_open_macos_developer_tools(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    opened: list[list[str]] = []
    monkeypatch.setattr(web_app.subprocess, "Popen", lambda args, *a, **k: opened.append(list(args)))
    monkeypatch.setattr(
        web_app,
        "dependency_status",
        lambda **_: {"mpv": {"installed": True, "path": "/opt/homebrew/bin/mpv"}},
    )
    monkeypatch.setattr(web_app.sys, "platform", "darwin")
    result = api.open_developer_tools_settings()
    assert opened == [
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_DeveloperTool"]
    ]
    # The two apps the user has to switch on there.
    assert result == {
        "ok": True,
        "supported": True,
        "pudge_app": str(Path.home() / "Applications" / f"{APP_SLUG}.app"),
        "mpv": "/opt/homebrew/bin/mpv",
    }
    monkeypatch.setattr(web_app, "dependency_status", lambda **_: {"mpv": {"installed": False}})
    assert api.open_developer_tools_settings()["mpv"] == str(api.config.tools.mpv)
    monkeypatch.setattr(web_app.sys, "platform", "linux")
    opened.clear()
    assert api.open_developer_tools_settings() == {"ok": False, "supported": False}
    assert opened == []


SAVE = "__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});"
PLUGINS = {"mpv_study": {"effective": "jiten"}}


def test_settings_explain_both_required_apps_and_remember_the_confirmation(page) -> None:
    assert en("ui.enablePudgeAndMpvIn") == "Enable Pudge and mpv in Developer Tools and confirm it"
    assert ru("ui.enablePudgeAndMpvIn") == "Включите Pudge и mpv в Developer Tools и подтвердите это"
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('settings'); await __tick(20);
            const out = {shown: __visible('#s_jiten_devtools'), text: __text('#s_jiten_devtools'), status: __text('#s_mpv_study_status'),
                         expected: [t('uiB.settings.developerTools'), PudgeI18n.t('ui.forJitenmpvEnableBothPudge'),
                                    PudgeI18n.t('ui.openDeveloperTools'), PudgeI18n.t('ui.iEnabledPudgeAndMpv')],
                         reminder: PudgeI18n.t('ui.developerToolsConfirmPudgeMpv').trim()};
            await __click('#openJitenDeveloperTools'); await __tick(10);
            await __input('#s_jiten_devtools_confirmed', true);
            out.statusConfirmed = __text('#s_mpv_study_status');
            await __input('#s_mpv_study_plugin', 'jpdb'); out.shownForJpdb = __visible('#s_jiten_devtools');
            await __input('#s_mpv_study_plugin', 'jiten');
            await __click('#saveSettings'); await __tick(10);
            return out;
            """,
            lang=lang,
            state={
                "settings": {
                    "mpv_study_plugin": "jiten",
                    "mpv_study_plugins": PLUGINS,
                    "jiten_developer_tools_confirmed": False,
                }
            },
            before=SAVE,
        )
        out = result.value
        assert out["shown"] is True and out["text"].split("\n") == out["expected"]
        assert out["status"].endswith(out["reminder"]) and out["reminder"] not in out["statusConfirmed"]
        assert out["shownForJpdb"] is False
        assert result.calls_of("open_developer_tools_settings") == [[]]
        [[values]] = result.calls_of("save_settings")
        assert values["jiten_developer_tools_confirmed"] is True


# First start, step "Video and interactive subtitles": mpv and ffmpeg are there, JitenMPV is not.
FIRST_START = {
    "state": {"settings": {"onboarding_completed": False}},
    "api": {
        "first_experience_dependencies": {
            "mpv": {"installed": True},
            "ffmpeg": {"installed": True},
            "manga_ocr": {"installed": False},
            "jiten_mpv": {"installed": False},
            "jpdb_mpv": {"installed": False},
        },
        "install_first_experience_dependencies": {
            "mpv": {"installed": True},
            "ffmpeg": {"installed": True},
            "manga_ocr": {"installed": False},
            "jiten_mpv": {"installed": True, "key_configured": True},
            "jpdb_mpv": {"installed": False},
        },
    },
    "before": "__api.complete_onboarding = async draft => ({settings: {...draft, onboarding_completed: true, light_novels: {}}});",
}
GUIDE = """
const next = async () => { await __click('#onboardingNext'); await __tick(20); };
await next();
"""


def test_setup_guide_installs_jitenmpv_then_opens_developer_tools_until_confirmed(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            GUIDE
            + """
            const out = {callout: __visible('#o_jiten_devtools'), text: __text('#o_jiten_devtools'),
                         expected: [t('uiB.settings.developerTools'), PudgeI18n.t('ui.jitenmpvIsLaunchedThroughMpv'),
                                    PudgeI18n.t('ui.openDeveloperTools'), PudgeI18n.t('ui.iEnabledPudgeAndMpv')],
                         reminder: t('ui.enablePudgeAndMpvIn')};
            await __input('#o_jiten_mpv', true);
            await __click('#onboardingInstallDependencies'); await __tick(20);
            out.openedAfterInstall = __apiCalls.filter(c => c.name === 'open_developer_tools_settings').length;
            await next();
            out.waits = {step: ui.onboardingStep, toast: __toasts().at(-1)};
            await __click('#onboardingOpenJitenDeveloperTools'); await __tick(10);
            await __input('#o_jiten_devtools_confirmed', true);
            await next();
            out.confirmedStep = ui.onboardingStep;
            while (ui.onboardingStep < 5) await next();
            await next();
            return out;
            """,
            lang=lang,
            **FIRST_START,
        )
        out = result.value
        assert out["callout"] is True and out["text"].split("\n") == out["expected"]
        assert out["openedAfterInstall"] == 1  # right after JitenMPV was installed
        assert out["waits"] == {"step": 1, "toast": out["reminder"]}
        assert out["confirmedStep"] == 2
        assert len(result.calls_of("open_developer_tools_settings")) == 2
        [[draft]] = result.calls_of("complete_onboarding")
        assert draft["install_jiten_mpv"] is True and draft["jiten_developer_tools_confirmed"] is True


def test_setup_guide_does_not_reopen_developer_tools_once_confirmed(page) -> None:
    result = page.run(
        GUIDE
        + """
        await __input('#o_jiten_devtools_confirmed', true);
        await __input('#o_jiten_mpv', true);
        await __click('#onboardingInstallDependencies'); await __tick(20);
        await next();
        return ui.onboardingStep;
        """,
        **FIRST_START,
    )
    assert result.value == 2
    assert result.called("install_first_experience_dependencies")
    assert not result.called("open_developer_tools_settings")


def _play(tmp_path: Path, monkeypatch, *, confirmed: bool, plugin: str) -> list[str]:
    from pudge.cli import build_parser, process_video
    from pudge.pipeline_cache import save_final_pipeline_result

    tmp_path.mkdir(parents=True)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    cfg.ui.jiten_developer_tools_confirmed = confirmed
    video = tmp_path / "Anime - 05.mkv"
    subtitle = tmp_path / "cached.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    save_final_pipeline_result(
        video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="external"
    )
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *_a, **_k: [])
    monkeypatch.setattr(
        first_experience,
        "mpv_study_script_plan",
        lambda *a, **k: ({"exclusive": True, "selected": plugin, "scripts": [f"/x/{plugin}/main.lua"]}),
    )
    monkeypatch.setattr("pudge.cli.run_mpv", lambda command, **kwargs: 0)
    monkeypatch.setattr(
        cli, "sys", types.SimpleNamespace(platform="darwin", stderr=sys.stderr, stdout=sys.stdout)
    )
    lines: list[str] = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    logger = logging.getLogger("pudge")
    logger.addHandler(handler)
    try:
        args = build_parser().parse_args([str(video)])
        args.config = tmp_path / "config.toml"
        assert process_video(video, args, cfg, None) == 0
    finally:
        logger.removeHandler(handler)
    return [line for line in lines if "jiten.developer_tools" in line]


def test_playback_logs_actionable_warning_when_permission_unconfirmed(tmp_path: Path, monkeypatch) -> None:
    assert _play(tmp_path / "a", monkeypatch, confirmed=False, plugin="jiten") == [
        "ACTION_REQUIRED step=jiten.developer_tools detail='Enable both Pudge and mpv in Privacy & Security > Developer Tools'"
    ]
    assert _play(tmp_path / "b", monkeypatch, confirmed=True, plugin="jiten") == []
    assert _play(tmp_path / "c", monkeypatch, confirmed=False, plugin="jpdb") == []
