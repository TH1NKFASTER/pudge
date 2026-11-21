"""mpv is optional: install.sh no longer installs it, the first-run guide offers it.

Homebrew's mpv pulls about 1 GB of dependencies, which someone who only reads manga or light
novels never needs. Before, install.sh always installed mpv, the guide refused to go on
without it, and a missing mpv surfaced as raw errors. Now:

* install.sh installs ffmpeg, alass, aria2 and Python only. It writes the mpv path to the
  config only when Homebrew's mpv is there (an existing install keeps its mpv and its path);
  without it the config keeps its own value instead of a path that does not exist.
* The guide's media step: no Homebrew -> a short explanation, a brew.sh link and "Check
  again"; Homebrew but no mpv -> what mpv is for, the ~1 GB download and "Install mpv"
  (brew install mpv through install_media_tools; "Install missing" no longer pulls mpv in);
  the guide can be continued without mpv, with a note on what will not play.
* Starting an anime episode or an audiobook without mpv offers the guide's media step
  (the fast-play launcher exits with a dedicated code; the audiobook player raises a known
  message) instead of "mpv exited with code 1" or "No such file or directory".
* An mpv installed later in Terminal is picked up (an app opened from the Dock has no
  Homebrew on PATH, and the config still said "mpv").
* `pudge --doctor` reports a missing mpv as optional, not as a broken install; the Settings
  JitenMPV action says mpv comes first.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

import pudge.cli as cli
import pudge.first_experience as first
from pudge import web_app
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.player import MPV_MISSING_EXIT_CODE, run_mpv
from t71g00_support import FORMULAS, TOOLS, run_install
from test_web_app import _write_config, make_api
from tests.i18n_support import en, ru

WITHOUT_MPV = {
    "installed": tuple(name for name in FORMULAS if name != "mpv"),
    "tools": tuple(tool for tool in TOOLS if not tool.startswith("mpv/")),
}


# ---------------------------------------------------------------------------------------
# install.sh
def test_install_without_mpv_does_not_install_it_and_keeps_the_config_value(tmp_path: Path) -> None:
    result = run_install(tmp_path, **WITHOUT_MPV)
    assert result.returncode == 0, result.output
    brew = result.calls_of("brew")
    assert ["install", "mpv"] not in brew and ["list", "--versions", "mpv"] not in brew
    config = tomllib.loads(result.config.read_text(encoding="utf-8"))
    # No path under a Homebrew folder that has no mpv: the default stays.
    assert config["tools"]["mpv"] == "mpv"
    assert config["tools"]["ffmpeg"] == result.prefix("ffmpeg/bin/ffmpeg")

    # An mpv the user set up another way survives an update without Homebrew's mpv.
    custom = tmp_path / "Applications" / "mpv.app" / "Contents" / "MacOS" / "mpv"
    custom.parent.mkdir(parents=True)
    custom.write_text("#!/bin/sh\n", encoding="utf-8")
    custom.chmod(0o755)
    cfg = load_config(result.config)
    cfg.tools.mpv = str(custom)
    write_config(cfg, result.config)
    updated = run_install(tmp_path, update=True, **WITHOUT_MPV)
    assert updated.returncode == 0, updated.output
    assert tomllib.loads(updated.config.read_text(encoding="utf-8"))["tools"]["mpv"] == str(custom)


def test_install_keeps_an_installed_homebrew_mpv(tmp_path: Path) -> None:
    result = run_install(tmp_path)
    assert result.returncode == 0, result.output
    assert ["install", "mpv"] not in result.calls_of("brew")
    config = tomllib.loads(result.config.read_text(encoding="utf-8"))
    assert config["tools"]["mpv"] == result.prefix("mpv/bin/mpv")


# ---------------------------------------------------------------------------------------
# Backend: install_media_tools, the guide's API, mpv found later.
def _fake_brew(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A brew that records its calls and 'installs' formulas into a bin folder on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "brew.log"
    brew = tmp_path / "brew"
    brew.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'shift\nfor name in "$@"; do\n'
        f'  printf \'#!/bin/sh\\necho "%s 1.0"\\n\' "$name" > "{bin_dir}/$name"; /bin/chmod 755 "{bin_dir}/$name"\n'
        "done\n",
        encoding="utf-8",
    )
    brew.chmod(0o755)
    monkeypatch.setattr(first, "_brew_path", lambda: str(brew))
    monkeypatch.setattr(first, "_homebrew_tool", lambda name: "")
    monkeypatch.setenv("PATH", str(bin_dir))  # no system ffmpeg either
    return bin_dir, log


def test_install_missing_skips_mpv_and_install_mpv_installs_it(tmp_path: Path, monkeypatch) -> None:
    bin_dir, log = _fake_brew(tmp_path, monkeypatch)
    missing = str(tmp_path / "nothing" / "mpv")
    # "Install missing": only ffmpeg is installed, mpv (about 1 GB) is left alone.
    status = first.install_media_tools(mpv=missing, ffmpeg="ffmpeg", include_mpv=False)
    assert log.read_text(encoding="utf-8").splitlines() == ["install ffmpeg"]
    assert status["mpv"]["installed"] is False and status["ffmpeg"]["installed"] is True
    # "Install mpv": brew install mpv.
    status = first.install_media_tools(mpv=missing, ffmpeg="ffmpeg", include_mpv=True)
    assert log.read_text(encoding="utf-8").splitlines() == ["install ffmpeg", "install mpv"]
    assert status["mpv"] == {"installed": True, "path": str(bin_dir / "mpv"), "version": "mpv 1.0"}


def test_guide_api_installs_mpv_on_request_and_never_writes_an_empty_path(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    calls: list[dict] = []
    found = {"mpv": ""}

    def install(**kwargs):
        calls.append(kwargs)
        mpv = found["mpv"] if kwargs["include_mpv"] else ""
        return {
            "mpv": {"installed": bool(mpv), "path": mpv},
            "ffmpeg": {"installed": True, "path": "/hb/ffmpeg"},
        }

    monkeypatch.setattr(web_app, "install_media_tools", install)
    monkeypatch.setattr(web_app, "configure_mpv_study_keys", lambda **_kw: None)
    monkeypatch.setattr(api, "manga_ocr_status", lambda: {"installed": True, "model_ready": True})
    api.config.tools.mpv = "mpv"
    # "Install missing" does not ask for mpv and keeps the configured value.
    api.install_first_experience_dependencies({})
    assert calls[-1]["include_mpv"] is False
    assert load_config(api.config_path).tools.mpv == "mpv"
    # "Install mpv": the installed path reaches the config and the audiobook player.
    found["mpv"] = "/opt/homebrew/bin/mpv"
    status = api.install_first_experience_mpv()
    assert calls[-1]["include_mpv"] is True
    assert load_config(api.config_path).tools.mpv == "/opt/homebrew/bin/mpv"
    assert api.audiobooks.mpv == "/opt/homebrew/bin/mpv"
    assert {"mpv", "ffmpeg", "homebrew", "manga_ocr", "jiten_mpv"} <= set(status)


def test_mpv_installed_later_in_terminal_is_used(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.config.tools.mpv = "mpv"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # an app opened from the Dock
    homebrew_mpv = tmp_path / "homebrew" / "mpv"
    homebrew_mpv.parent.mkdir()
    homebrew_mpv.write_text("#!/bin/sh\necho mpv 0.39\n", encoding="utf-8")
    homebrew_mpv.chmod(0o755)
    monkeypatch.setattr(first, "_homebrew_tool", lambda name: str(homebrew_mpv) if name == "mpv" else "")
    status = api.first_experience_dependencies()
    assert status["mpv"]["installed"] is True
    assert load_config(api.config_path).tools.mpv == str(homebrew_mpv)
    assert api.audiobooks.mpv == str(homebrew_mpv)
    # A configured mpv that works is never replaced.
    other = tmp_path / "custom-mpv"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(0o755)
    api.config.tools.mpv = str(other)
    api.first_experience_dependencies()
    assert api.config.tools.mpv == str(other)


# ---------------------------------------------------------------------------------------
# Playback without mpv.
def test_fast_play_without_mpv_exits_with_the_mpv_missing_code(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(cli, "process_video", lambda *_a: run_mpv([str(tmp_path / "no-mpv"), str(video)]))
    assert cli.main(["--config", str(config_path), "--fast-play", "--no-sync", str(video)]) == 127


def test_play_status_reports_a_missing_mpv(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    key = str(video.resolve())
    launcher = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({MPV_MISSING_EXIT_CODE})"])
    launcher.wait(timeout=30)
    api._play_processes[key] = launcher
    api._play_started_at[key] = time.time()
    status = api.play_status(str(video))
    assert (status["status"], status["reason"], status["error"]) == (
        "failed",
        "mpv_missing",
        en("error.media.mpvMissing"),
    )
    assert "аниме" in ru("error.media.mpvMissing") and "аудиокниг" in ru("error.media.mpvMissing")


def test_audiobook_without_mpv_raises_the_known_message(tmp_path: Path) -> None:
    service = AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv=str(tmp_path / "no-mpv"),
        cache_dir=tmp_path / "cache",
    )
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    chapter = {"index": 0, "title": "Book", "start": 0.0, "end": 60.0}
    book = service._upsert(
        path=source,
        title="Book",
        duration=60.0,
        files=[{**chapter, "path": str(source), "duration": 60.0}],
        chapters=[chapter],
    )
    try:
        with pytest.raises(RuntimeError) as error:
            service.play(int(book["id"]))
    finally:
        service.close(timeout=2.0)
    assert str(error.value) == en("error.media.mpvMissing")


def test_doctor_reports_a_missing_mpv_as_optional(tmp_path: Path, monkeypatch, capsys) -> None:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.qbittorrent.enabled = config.aria2.enabled = config.llm.enabled = False
    working = tmp_path / "tool"
    working.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    working.chmod(0o755)
    config.tools.ffmpeg = config.tools.ffprobe = str(working)
    config.tools.mpv = str(tmp_path / "no-mpv")
    monkeypatch.setitem(sys.modules, "ffsubsync", object())
    status = cli.doctor(config)
    out = capsys.readouterr().out
    assert en("cli.doctorMpvOptional", path=config.tools.mpv) in out.splitlines()
    assert "anime and audiobook" in en("cli.doctorMpvOptional", path="mpv")
    assert status == 0


# ---------------------------------------------------------------------------------------
# The window: the guide's media step and playback.
def _deps(*, mpv: bool, homebrew: bool) -> dict:
    return {
        "mpv": {"installed": mpv, "path": "/opt/homebrew/bin/mpv" if mpv else ""},
        "ffmpeg": {"installed": True},
        "homebrew": {"installed": homebrew},
        "manga_ocr": {"installed": True},
        "jiten_mpv": {"installed": False},
        "jpdb_mpv": {"installed": False},
    }


FIRST_START = {
    "state": {"settings": {"onboarding_completed": False}},
    "before": "__api.complete_onboarding = async draft => ({settings: {...draft, onboarding_completed: true, light_novels: {}}});",
}
NEXT = "const next = async () => { await __click('#onboardingNext'); await __tick(20); };\n"


def test_guide_without_homebrew_links_brew_sh_and_checks_again(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            NEXT
            + r"""
            let answers = [NO_BREW, READY];
            __api.first_experience_dependencies = async () => answers.shift() || READY;
            await next();
            const box = document.querySelector('#o_mpv_setup');
            const out = {kind: box?.dataset.mpvSetup, text: __text('#o_mpv_setup'),
                         expected: [PudgeI18n.t('ui.onboardingHomebrewMissing'), PudgeI18n.t('ui.onboardingWithoutMpv')],
                         link: box.querySelector('[data-action="url"]').dataset.url};
            await __click('#o_mpv_setup [data-action="url"]'); await __tick(10);
            await __click('#onboardingCheckDependencies'); await __tick(20);
            out.after = document.querySelector('#o_mpv_setup');
            return out;
            """.replace("NO_BREW", json.dumps(_deps(mpv=False, homebrew=False))).replace(
                "READY", json.dumps(_deps(mpv=True, homebrew=True))
            ),
            lang=lang,
            **FIRST_START,
        )
        out = result.value
        assert out["kind"] == "homebrew" and all(text in out["text"] for text in out["expected"])
        assert out["link"] == "https://brew.sh/"
        assert result.calls_of("open_url") == [["https://brew.sh/"]]
        # Nothing is run in Terminal: the link opens and "Check again" only checks.
        assert len(result.calls_of("first_experience_dependencies")) == 2
        assert out["after"] is None
        assert not result.called("install_first_experience_mpv")
    assert "brew.sh" in ru("ui.onboardingHomebrewMissing")


def test_guide_installs_mpv_with_homebrew_on_request(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            NEXT
            + r"""
            __api.first_experience_dependencies = async () => (NO_MPV);
            __api.install_first_experience_mpv = async () => (READY);
            await next();
            const row = name => [...document.querySelectorAll('#onboardingBody .dependency-row')]
              .find(node => node.querySelector('strong').innerText === name)?.querySelector('.dependency-state').innerText;
            const out = {kind: document.querySelector('#o_mpv_setup')?.dataset.mpvSetup, text: __text('#o_mpv_setup'),
                         expected: PudgeI18n.t('ui.onboardingMpvMissing'), button: __text('#onboardingInstallMpv'),
                         jitenDisabled: $('o_jiten_mpv').disabled, before: row('mpv')};
            await __click('#onboardingInstallMpv'); await __tick(20);
            out.after = row('mpv'); out.box = document.querySelector('#o_mpv_setup');
            out.toast = __toasts().at(-1); out.ready = PudgeI18n.t('ui.mpvIsReady');
            out.jitenAfter = $('o_jiten_mpv').disabled;
            out.texts = {installed: PudgeI18n.t('ui.installed'), missing: PudgeI18n.t('ui.missing'), install: PudgeI18n.t('ui.installMpv')};
            return out;
            """.replace("NO_MPV", json.dumps(_deps(mpv=False, homebrew=True))).replace(
                "READY", json.dumps(_deps(mpv=True, homebrew=True))
            ),
            lang=lang,
            **FIRST_START,
        )
        out = result.value
        assert out["kind"] == "install" and out["expected"] in out["text"]
        assert out["button"] == out["texts"]["install"]
        assert result.calls_of("install_first_experience_mpv") == [[]]
        assert (out["before"], out["after"], out["box"]) == (
            out["texts"]["missing"],
            out["texts"]["installed"],
            None,
        )
        assert out["toast"] == out["ready"]
        # JitenMPV runs inside mpv: its install waits for mpv.
        assert (out["jitenDisabled"], out["jitenAfter"]) == (True, False)
    assert "1 GB" in en("ui.onboardingMpvMissing") and "1 ГБ" in ru("ui.onboardingMpvMissing")


def test_guide_continues_without_mpv(page) -> None:
    result = page.run(
        NEXT
        + r"""
        __api.first_experience_dependencies = async () => (NO_MPV);
        await next();
        const note = __text('#o_mpv_setup').includes(PudgeI18n.t('ui.onboardingWithoutMpv'));
        await next();
        const step = ui.onboardingStep;
        while (ui.onboardingStep < 5) await next();
        await next();
        return {note, step, toasts: __toasts()};
        """.replace("NO_MPV", json.dumps(_deps(mpv=False, homebrew=True))),
        **FIRST_START,
    )
    assert result.value["note"] is True and result.value["step"] == 2
    assert result.called("complete_onboarding")
    assert not result.called("install_first_experience_mpv")


MPV_SETUP_FLOW = r"""
const dialog = () => ({open: $('pudgeConfirmBackdrop')?.classList.contains('open') || false,
                       text: $('pudgeConfirmMessage')?.textContent || '', ok: document.querySelector('[data-pudge-confirm="ok"]')?.textContent || ''});
const afterOffer = async () => {
  const out = {dialog: dialog(), expected: {text: PudgeI18n.t('ui.mpvMissingPlayback'), ok: PudgeI18n.t('ui.setUpMpv')}};
  await __click('[data-pudge-confirm="ok"]'); await __tick(20);
  out.guide = {open: $('onboardingBackdrop').classList.contains('open'), step: ui.onboardingStep,
               kind: document.querySelector('#o_mpv_setup')?.dataset.mpvSetup, skip: __text('#onboardingSkip'), close: t('action.close')};
  await __click('#onboardingSkip'); await __tick(20);
  out.closed = !$('onboardingBackdrop').classList.contains('open');
  out.toasts = __toasts();
  return out;
};
"""


def _assert_offer(result) -> None:
    out = result.value
    assert out["dialog"]["open"] is True
    assert (out["dialog"]["text"], out["dialog"]["ok"]) == (out["expected"]["text"], out["expected"]["ok"])
    assert out["guide"] == {
        "open": True,
        "step": 1,
        "kind": "install",
        "skip": out["guide"]["close"],
        "close": out["guide"]["close"],
    }
    # Closing the media step leaves the settings alone (no "skip setup").
    assert out["closed"] is True and not result.called("skip_onboarding")
    assert not [text for text in out["toasts"] if "mpv" in text]


def test_anime_without_mpv_offers_the_guides_media_step(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            MPV_SETUP_FLOW
            + r"""
            __api.play = async () => ({ok: true, duplicate: false, status: 'starting', pid: 7});
            __api.play_status = async () => ({ok: true, status: 'failed', exit_code: 127, reason: 'mpv_missing', error: ERROR});
            __api.first_experience_dependencies = async () => (NO_MPV);
            await startPlay('/lib/Show - 03.mkv'); await __tick(2000);
            return await afterOffer();
            """.replace("ERROR", json.dumps(en("error.media.mpvMissing"))).replace(
                "NO_MPV", json.dumps(_deps(mpv=False, homebrew=True))
            ),
            lang=lang,
        )
        _assert_offer(result)


def test_audiobook_without_mpv_offers_the_guides_media_step(page) -> None:
    book = {
        "id": 4,
        "title": "Audio",
        "duration": 600,
        "position": 30,
        "playing": False,
        "player_running": False,
        "speed": 1,
    }
    result = page.run(
        MPV_SETUP_FLOW
        + r"""
        setPage('audiobooks', true); await __tick(100);
        await __click('[data-audiobook-id="4"] [data-media-action="play-audio"]'); await __tick(50);
        return await afterOffer();
        """,
        lang="ru",
        before=r"""
        __api.audiobook_state = async () => ({books: [BOOK]});
        __api.sidebar_audio_state = async () => ({books: [BOOK]});
        __api.audiobook_play = async () => { throw new Error(ERROR); };
        __api.first_experience_dependencies = async () => (NO_MPV);
        """.replace("BOOK", json.dumps(book))
        .replace("ERROR", json.dumps(en("error.media.mpvMissing")))
        .replace("NO_MPV", json.dumps(_deps(mpv=False, homebrew=True))),
        allow_errors=True,
    )
    _assert_offer(result)


def test_settings_jitenmpv_action_sets_up_mpv_first(page) -> None:
    result = page.run(
        r"""
        __api.first_experience_dependencies = async () => (NO_MPV);
        setPage('settings', true); await __tick(50);
        const out = {button: __text('#jitenMpvAction'), status: __text('#s_jiten_mpv_update_status'),
                     expected: {button: PudgeI18n.t('ui.setUpMpv'), status: PudgeI18n.t('ui.mpvNotInstalledOptional')}};
        await __click('#jitenMpvAction'); await __tick(20);
        out.guide = {open: $('onboardingBackdrop').classList.contains('open'), step: ui.onboardingStep};
        return out;
        """.replace("NO_MPV", json.dumps(_deps(mpv=False, homebrew=True))),
        lang="ru",
        state={"settings": {"mpv_installed": False}},
    )
    out = result.value
    assert (out["button"], out["status"]) == (out["expected"]["button"], out["expected"]["status"])
    assert out["guide"] == {"open": True, "step": 1}
    assert not result.called("jiten_mpv_install")
