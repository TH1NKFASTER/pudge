"""v0.10.3: simpler Settings with fixed policies, listen-together button states, MangaOCR in the
first setup (behaviour; audit 71Б rewrite).

Also covered elsewhere: the auto-watched hint with the configured shortcut and the LN progress
tooltip with exact character counts (tests/test_v0104_settings_ux_cleanup.py), listen-together
holding the spoken word during silence (same file)."""

from __future__ import annotations

from pathlib import Path

import pytest

import pudge.web_app as web_app
from css_support import css_rules
from pudge.config import load_config
from pudge.i18n import t
from t71g18_support import NO_LN_GATE, READER_VIEW, paired_ln
from test_web_app import make_api
from tests.i18n_support import en, ru


def test_hidden_policy_settings_are_fixed_even_for_old_configs(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[anilist]
watched_threshold = 0.2
watched_max_remaining_minutes = 99

[nyaa]
require_japanese_audio = false
avoid_upscaled = false
upgrade_min_score_gain = 1

[matching]
auto_upgrade_subtitles = false

[sync]
use_container_chapters = false
japanese_stt_fallback = false
""",
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.anilist.watched_threshold == 0.85
    assert cfg.anilist.watched_max_remaining_minutes == 10.0
    assert cfg.nyaa.require_japanese_audio is True
    assert cfg.nyaa.avoid_upscaled is True
    assert cfg.nyaa.upgrade_min_score_gain == 30.0
    assert cfg.matching.auto_upgrade_subtitles is True
    assert cfg.sync.use_container_chapters is True
    assert cfg.sync.japanese_stt_fallback is True


REMOVED = [
    "s_anilist_threshold",
    "s_anilist_max_remaining",
    "s_use_container_chapters",
    "s_japanese_stt_fallback",
    "s_auto_upgrade_subtitles",
    "s_aria2_port",
    "s_upgrade_min_gain",
    "s_require_japanese",
    "s_avoid_upscaled",
]
# What an old configuration may still say, and the fixed values used instead.
OLD = {
    "anilist_threshold": 20,
    "anilist_max_remaining_minutes": 99,
    "use_container_chapters": False,
    "japanese_stt_fallback": False,
    "auto_upgrade_subtitles": False,
    "upgrade_min_score_gain": 1,
    "require_japanese_audio": False,
    "avoid_upscaled": False,
    "aria2_rpc_port": 6802,
}
FIXED = {
    "anilist_threshold": 85,
    "anilist_max_remaining_minutes": 10,
    "use_container_chapters": True,
    "japanese_stt_fallback": True,
    "auto_upgrade_subtitles": True,
    "upgrade_min_score_gain": 30,
    "require_japanese_audio": True,
    "avoid_upscaled": True,
}


def test_simplified_settings_are_not_shown_and_saving_sends_the_fixed_values(page, tmp_path: Path) -> None:
    result = page.run(
        f"""
        setPage('settings', true); await __tick(50);
        const present = {REMOVED}.filter(id => document.getElementById(id));
        await __click('#saveSettings'); await __tick(10);
        return {{present}};
        """,
        state={"settings": OLD},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    assert result.value["present"] == []
    [[values]] = result.calls_of("save_settings")
    assert {key: values[key] for key in FIXED} == FIXED
    assert values["aria2_rpc_port"] == 6802  # not a control any more: the configured port is kept
    # The backend keeps the policies fixed whatever the window sends.
    settings = make_api(tmp_path).save_settings(dict(OLD))["settings"]
    assert {key: settings[key] for key in FIXED} == FIXED


def test_anilist_checkboxes_are_one_vertical_stack(page) -> None:
    result = page.run(
        """
        setPage('settings', true); await __tick(50);
        const row = id => { const label = $(id).closest('label'); return {type: $(id).type, label: label.className, stack: label.parentElement.className}; };
        return {add: row('s_anilist_add_missing'), relations: row('s_relations_release'),
                same: $('s_anilist_add_missing').closest('.setting-stack') === $('s_relations_release').closest('.setting-stack')};
        """
    )
    assert result.value["add"] == {"type": "checkbox", "label": "check", "stack": "setting-stack"}
    assert result.value["relations"] == result.value["add"] and result.value["same"] is True
    assert css_rules("index.html").get(".setting-stack", "display") == "grid"


def test_ln_progress_tooltip_falls_back_to_percent_without_character_counts(page) -> None:
    book = {"id": 4, "title": "Novel", "volume": 1, "reading_progress": 0.4}
    result = page.run(
        """
        await __fire('#lightNovelsContent [data-ln-book="4"] .ln-card-progress', 'mouseover', {}, 'MouseEvent');
        const tip = document.getElementById('titleTooltip');
        return tip.classList.contains('show') ? tip.innerText : null;
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": [book]}},
    )
    assert result.value == "40%"


def test_listen_button_is_green_to_listen_and_red_to_pause(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            READER_VIEW
            + r"""
            await __settle(openLightNovel(6)); await __tick(300);
            const out = {idle: transport()};
            await __click('#lnPairedAudio'); window.__release(); await __tick(300);
            out.playing = transport();
            out.labels = {listen: PudgeI18n.t('ui.listen'), pause: PudgeI18n.t('ui.pause')};
            return out;
            """,
            lang=lang,
            before=paired_ln(False),
            state=NO_LN_GATE,
        )
        out = result.value
        assert out["idle"]["label"] == out["labels"]["listen"]
        assert (
            "ln-paired-audio-listen" in out["idle"]["classes"]
            and "ln-paired-audio-stop" not in out["idle"]["classes"]
        )
        assert out["playing"]["label"] == out["labels"]["pause"]
        assert (
            "ln-paired-audio-stop" in out["playing"]["classes"]
            and "ln-paired-audio-listen" not in out["playing"]["classes"]
        )
    rules = css_rules("index.html")
    assert rules.get(".ln-paired-audio-listen", "background") == "var(--good)!important"
    assert rules.get(".ln-paired-audio-stop", "background") == "var(--danger)!important"


def test_the_spoken_word_moves_between_backend_samples(page) -> None:
    # While the narration plays, frames move the highlight with the clock; the backend's
    # samples only correct it. No word is "pre-painted" (a finishing state) ahead of the voice.
    result = page.run(
        READER_VIEW
        + r"""
        await __settle(openLightNovel(6)); await __tick(150);
        __api.light_novel_paired_state = () => new Promise(() => {});      // no newer sample arrives
        const words = [];
        for (let step = 0; step < 4; step++) { words.push(spoken()); await __tick(1000); }
        return {words, finishing: document.querySelectorAll('#lnReader .ln-paired-word-finishing').length};
        """,
        before=paired_ln(True),
        state=NO_LN_GATE,
    )
    assert result.value["words"] == ["一", "二", "三", "四"]
    assert result.value["finishing"] == 0


DEPENDENCIES = {
    "mpv": {"installed": True, "version": "0.38"},
    "ffmpeg": {"installed": True, "version": "7.0"},
    "manga_ocr": {"installed": False, "version": "Model not ready"},
    "jiten_mpv": {"installed": False},
    "jpdb_mpv": {"installed": False},
}


def test_first_setup_lists_manga_ocr_and_reports_an_unfinished_install(page) -> None:
    assert en("error.setup.mangaOcrIncomplete") == "Mokuro installation did not complete"
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await __click('#onboardingNext'); await __tick(20);
            const rows = [...document.querySelectorAll('#onboardingBody .dependency-row')].map(row => row.querySelector('strong').textContent);
            const ocr = [...document.querySelectorAll('#onboardingBody .dependency-row')].find(row => row.querySelector('strong').textContent === 'Mokuro');
            await __click('#onboardingInstallDependencies'); await __tick(50);
            return {rows, ocr: ocr.innerText, missing: PudgeI18n.t('ui.missing'), toasts: __toasts(),
                    error: t('error.generic', {error: PudgeI18n.t('error.setup.mangaOcrIncomplete')})};
            """,
            lang=lang,
            state={"settings": {"onboarding_completed": False}},
            api={"first_experience_dependencies": DEPENDENCIES},
            before="__api.install_first_experience_dependencies = async () => { throw new Error(ERROR); };".replace(
                "ERROR", repr(en("error.setup.mangaOcrIncomplete"))
            ),
        )
        out = result.value
        assert out["rows"][:3] == ["mpv", "ffmpeg", "Mokuro"]
        assert out["ocr"].split("\n")[-1] == out["missing"]
        assert out["toasts"] == [out["error"]]
    assert "Установка Mokuro" in ru("error.setup.mangaOcrIncomplete")


@pytest.mark.parametrize(
    ("package", "model", "ready", "version"),
    [
        (False, False, False, "web.notInstalled"),
        (True, False, False, "web.modelNotReady"),
        (True, True, True, "web.mangaOcrPackageReady"),
    ],
)
def test_manga_ocr_counts_as_installed_only_with_its_model(
    tmp_path: Path, monkeypatch, package, model, ready, version
) -> None:
    api = make_api(tmp_path)
    monkeypatch.setattr(
        web_app,
        "dependency_status",
        lambda **_kw: {"mpv": {"installed": True}, "ffmpeg": {"installed": True}},
    )
    monkeypatch.setattr(api, "manga_ocr_status", lambda: {"installed": package, "model_ready": model})
    status = api.first_experience_dependencies()
    assert status["manga_ocr"]["installed"] is ready
    assert status["manga_ocr"]["package_installed"] is package
    assert status["manga_ocr"]["version"] == t(version)


def test_first_setup_installs_manga_ocr_and_fails_when_it_is_still_missing(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    monkeypatch.setattr(
        web_app,
        "install_media_tools",
        lambda **_kw: {"mpv": {"path": "/opt/mpv"}, "ffmpeg": {"path": "/opt/ffmpeg"}},
    )
    monkeypatch.setattr(web_app, "configure_mpv_study_keys", lambda **_kw: None)
    monkeypatch.setattr(
        web_app,
        "dependency_status",
        lambda **_kw: {
            "mpv": {"installed": True},
            "ffmpeg": {"installed": True},
            "jiten_mpv": {"installed": False},
        },
    )
    installs: list[bool] = []
    state = {"installed": False, "model_ready": False}
    monkeypatch.setattr(api, "manga_ocr_status", lambda: dict(state))
    monkeypatch.setattr(api, "_run_mokuro_ocr_install", lambda: installs.append(True))
    with pytest.raises(RuntimeError, match=en("error.setup.mangaOcrIncomplete")):
        api.install_first_experience_dependencies({})
    assert installs == [True]
    # An install that finishes reports manga OCR (Mokuro) as ready.
    monkeypatch.setattr(
        api, "_run_mokuro_ocr_install", lambda: state.update(installed=True, model_ready=True)
    )
    status = api.install_first_experience_dependencies({})
    assert status["manga_ocr"]["installed"] is True and status["manga_ocr"]["version"] == t(
        "web.mangaOcrPackageReady"
    )
