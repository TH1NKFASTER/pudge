"""Offline regressions for UI languages, WordDto readings and GUI-free workers."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.config import AppConfig
from pudge.jiten_words import JitenWordRepository, normalize_card
from pudge.runtime import mac_app_instances, worker_environment
from pudge.ui_localization import ERROR_TRANSLATIONS, localize_ui_message

WEB = Path(__file__).resolve().parents[1] / "pudge" / "web"


def node(script: str) -> str:
    return subprocess.check_output(["node", "-e", script], text=True, timeout=15)


def test_drop_uses_lexical_ui_language_without_window_ui():
    setup = r"""
const assert=require('node:assert/strict'), listeners={}, classes=new Set(), notices=[];
global.window=global; const ui={lang:'en'}; assert.equal(window.ui,undefined);
const box={dataset:{},classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
const page={classList:{contains:x=>x==='active'}};
global.document={documentElement:{lang:'en'},getElementById:id=>id==='lightnovels'?page:box,
  addEventListener:(name,fn)=>listeners[name]=fn};
window.addEventListener=(name,fn)=>listeners[name]=fn;window.toast=text=>notices.push(text);
const drag=name=>({type:'dragenter',dataTransfer:{types:['Files'],files:[{name}]},preventDefault(){}});
const bad=()=>({...drag('a.pdf'),preventDefault(){},stopImmediatePropagation(){},stopPropagation(){}});
"""
    checks = r"""
listeners.dragenter(drag('a.epub'));assert.equal(box.dataset.dropLabel,'Drop EPUB/TXT here');
listeners.drop(bad());assert.equal(notices.pop(),'EPUB and TXT files are supported');
ui.lang='ru';listeners.dragenter(drag('a.txt'));assert.equal(box.dataset.dropLabel,'Бросьте файл сюда · EPUB/TXT');
listeners.drop(bad());assert.equal(notices.pop(),'Поддерживаются EPUB и TXT');
assert(!classes.has('ln-file-drag'));
"""
    node(setup + (WEB / "ln_drop.js").read_text() + checks)


def test_known_error_catalog_is_identical_in_python_and_ui_and_preserves_source_text():
    values = ["/tmp/Русская книга [01].mkv", "исходный ответ провайдера", "3"]
    samples = [ru.format(*values) for ru, _ in ERROR_TRANSLATIONS]
    expected = [en.format(*values) for _, en in ERROR_TRANSLATIONS]
    assert [localize_ui_message(text, "en") for text in samples] == expected
    assert [localize_ui_message(text, "ru") for text in samples] == samples
    setup = "global.window=global;const ui={lang:'en'};"
    checks = f"""
const input={json.dumps(samples, ensure_ascii=False)};
const catalog=window.PudgeUiLanguage.ERROR_TRANSLATIONS;
const english=input.map(text=>window.PudgeUiLanguage.message(text));ui.lang='ru';
console.log(JSON.stringify({{catalog,english,russian:input.map(text=>window.PudgeUiLanguage.message(text))}}));
"""
    result = json.loads(node(setup + (WEB / "ui_localization.js").read_text() + checks))
    assert result["catalog"] == [list(row) for row in ERROR_TRANSLATIONS]
    assert result["english"] == expected
    assert result["russian"] == samples
    for text in ("Название книги: Видео не найдено", "/tmp/Не найден mpv: книга.mkv", "Свободный текст"):
        assert localize_ui_message(text, "en") == text
    nested = "Error: Не удалось запустить Pudge: Не найден mpv: /tmp/Русская книга"
    assert (
        localize_ui_message(nested, "en") == "Error: Could not start Pudge: mpv not found: /tmp/Русская книга"
    )


def powder_dto():
    # Generated minimal WordDto in the public API's actual shape. No media/pages.
    return {
        "wordId": 1504770,
        "mainReading": {"text": "粉[こな]", "readingIndex": 0, "readingType": 0},
        "alternativeReadings": [
            {"text": "粉", "readingIndex": 0, "readingType": 0},
            {"text": "こな", "readingIndex": 1, "readingType": 1},
            {"text": "こ", "readingIndex": 2, "readingType": 1},
        ],
        "definitions": [{"meanings": ["generated definition"]}],
        "knownStates": [{"state": "mutable"}],
    }


def test_actual_word_dto_populates_both_readings_in_background_and_persistent_cache(tmp_path):
    from pudge.light_novels import LightNovelService

    repository = JitenWordRepository(tmp_path / "words.sqlite")
    service = LightNovelService.__new__(LightNovelService)
    service.study_provider_capabilities = lambda backend: {"account_key": "account"}
    service._word_repository = lambda: repository
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(route):
        calls.append(route)
        entered.set()
        assert release.wait(2)
        return powder_dto()

    service._jiten_get = fetch
    card = {"wordId": 1504770, "readingIndex": 0, "reading": "こな", "state": "live"}
    begin = time.monotonic()
    initial = service.jiten_word(card)
    assert initial["all_readings"] == ["こな"]
    assert time.monotonic() - begin < 0.3
    assert entered.wait(1)
    release.set()
    repository.pool.shutdown()
    complete = service.jiten_word(card)
    assert complete["primary_reading"] == "こな"
    assert complete["all_readings"] == ["こな", "こ"]
    assert complete["dictionary_complete"]
    assert complete["state"] == "live" and "knownStates" not in complete
    assert calls == ["vocabulary/1504770/0/info"]
    reopened = JitenWordRepository(repository.path)
    try:
        cached, _ = reopened.get("account", card)
        assert cached["all_readings"] == ["こな", "こ"]
        assert reopened.get("other-account", card) == (None, 0)
    finally:
        reopened.pool.shutdown()


def test_old_incomplete_lexical_cache_is_retired_without_removing_data(tmp_path):
    repository = JitenWordRepository(tmp_path / "words.sqlite")
    card = {"wordId": 1504770, "readingIndex": 0}
    with sqlite3.connect(repository.path) as conn:
        conn.execute(
            "INSERT INTO jiten_word_cache VALUES(?,?,?,?,?)",
            ("account", repository.key(card), 1, '{"all_readings":["こな"]}', time.time()),
        )
    try:
        assert repository.get("account", card) == (None, 0)
        assert repository.put("account", {**powder_dto(), **card})["all_readings"] == ["こな", "こ"]
    finally:
        repository.pool.shutdown()


def test_javascript_readings_match_backend_and_jiten_click_does_not_grade():
    payload = {**powder_dto(), "readingIndex": 0}
    expected = normalize_card(payload)
    source = (
        (WEB / "review_gate.js")
        .read_text()
        .replace(
            "window.PudgeReviewGate = {",
            "window.PudgeReviewGate = {ensureUiForTest:ensureUi, jitenWordHtmlForTest:jitenWordHtml,",
        )
    )
    setup = r"""
global.window=global;const assert=require('node:assert/strict'),opened=[],listeners={};let graded=0;
window.addEventListener=()=>{};window.pywebview={api:{open_url:async url=>opened.push(url),review_gate_grade:()=>graded++}};
const overlay={isConnected:true,classList:{},setAttribute(){},addEventListener:(k,fn)=>listeners[k]=fn};
global.document={documentElement:{lang:'en'},body:{appendChild(){}},createElement:()=>overlay,addEventListener(){}};
"""
    checks = f"""
(async()=>{{
const card={json.dumps(payload, ensure_ascii=False)};
const normalized=window.PudgeJitenWords.normalize(card);
assert.equal(normalized.primary_reading,{json.dumps(expected["primary_reading"])});
assert.deepEqual(normalized.all_readings,{json.dumps(expected["all_readings"])});
assert.equal(window.PudgeJitenWords.readingsLine(card),'こ');
assert.equal(window.PudgeJitenWords.url({{wordId:'1/evil',readingIndex:0}}),'');
assert.equal(window.PudgeJitenWords.url({{wordId:1,readingIndex:-1}}),'');
const html=window.PudgeReviewGate.jitenWordHtmlForTest(card,'粉');
assert(html.includes('data-word-id="1504770"')&&html.includes('data-reading-index="0"'));
assert(html.includes('Open word on Jiten'));
window.PudgeReviewGate.ensureUiForTest();let prevented=false;
listeners.click({{preventDefault(){{prevented=true;}},target:{{closest:selector=>selector==='[data-review-gate-jiten]'?{{dataset:{{wordId:'1504770',readingIndex:'0'}}}}:null}}}});
await Promise.resolve();assert(prevented);assert.equal(graded,0);
assert.deepEqual(opened,['https://jiten.moe/vocabulary/1504770/0']);
}})().catch(error=>{{console.error(error);process.exitCode=1;}});
"""
    node(setup + (WEB / "jiten_words.js").read_text() + source + checks)


@pytest.mark.parametrize("language", ["en", "ru"])
def test_advanced_settings_language_and_no_model_save(language, tmp_path, monkeypatch):
    from pudge import settings_ui

    texts, buttons, combos, errors, saved = [], [], [], [], []

    class Variable:
        def __init__(self, value=None, **kw):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

        def trace_add(self, *args):
            pass

    class Widget:
        def __init__(self, *args, **kw):
            self.kw = kw
            if "text" in kw:
                texts.append(kw["text"])
            if "command" in kw and "text" in kw:
                buttons.append(self)
            if "textvariable" in kw and kw.get("state") == "readonly":
                combos.append(self)

        def __setitem__(self, key, value):
            self.kw[key] = value
            if key == "values" and self not in combos:
                combos.append(self)

        def __getattr__(self, name):
            return lambda *args, **kw: None

        def title(self, text):
            texts.append(text)

        def winfo_screenwidth(self):
            return 1440

        def winfo_screenheight(self):
            return 900

        def add(self, widget, **kw):
            texts.append(kw.get("text", ""))

        def grid_slaves(self, **kw):
            return []

    ttk = SimpleNamespace(
        **{
            name: Widget
            for name in (
                "Frame",
                "Label",
                "Notebook",
                "Scrollbar",
                "Style",
                "Entry",
                "Button",
                "Combobox",
                "Checkbutton",
                "Separator",
            )
        }
    )
    dialogs = SimpleNamespace(
        showinfo=lambda *a, **kw: texts.extend(a[:2]), showerror=lambda *a, **kw: errors.append(a)
    )
    tk = SimpleNamespace(
        Tk=Widget,
        Canvas=Widget,
        StringVar=Variable,
        BooleanVar=Variable,
        TclError=RuntimeError,
        ttk=ttk,
        messagebox=dialogs,
        filedialog=SimpleNamespace(askdirectory=lambda **kw: ""),
    )
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setattr(settings_ui, "SmoothScrollController", lambda *a: Widget())
    monkeypatch.setattr(settings_ui, "enable_edit_shortcuts", lambda *a: None)
    cfg = AppConfig()
    cfg.ui.language = language
    cfg.llm.enabled = False
    cfg.llm.model = "previous-real-model"
    monkeypatch.setattr(settings_ui, "load_config", lambda path: cfg)
    monkeypatch.setattr(settings_ui, "write_config", lambda config, path: saved.append(config))
    assert settings_ui.launch_settings(tmp_path / "config.toml") == 0
    no_model = "No model" if language == "en" else "Без модели"
    assert any(no_model in widget.kw.get("values", ()) for widget in combos)
    if language == "en":
        assert not any(any("А" <= c <= "я" or c in "Ёё" for c in text) for text in texts)
        assert "Create Client ID" in texts
    else:
        assert "Модель" in texts and "Сохранить" in texts
        assert "Создать Client ID" in texts
    assert f"{'Version' if language == 'en' else 'Версия'}: {settings_ui.__version__}" in texts
    save = next(
        widget for widget in buttons if widget.kw["text"] == ("Save" if language == "en" else "Сохранить")
    )
    save.kw["command"]()
    assert errors == [] and saved == [cfg]
    assert not cfg.llm.enabled and cfg.llm.model == "previous-real-model"


def test_worker_environment_removes_gui_identity_without_changing_parent(monkeypatch):
    for key in ("__PYVENV_LAUNCHER__", "__CFBundleIdentifier", "PUDGE_APP_ICON"):
        monkeypatch.setenv(key, "GUI-only")
    monkeypatch.setenv("PUDGE_PYTHON", "/tmp/venv/bin/python")
    env = worker_environment({"PUDGE_CONFIG": "/tmp/config.toml", "PUDGE_APP_NAME": "pudge"})
    assert env["PUDGE_PYTHON"] == "/tmp/venv/bin/python"
    assert env["PUDGE_CONFIG"] == "/tmp/config.toml" and env["PUDGE_APP_NAME"] == "pudge"
    for key in ("__PYVENV_LAUNCHER__", "__CFBundleIdentifier", "PUDGE_APP_ICON"):
        assert key not in env and os.environ[key] == "GUI-only"


def test_cli_passes_venv_python_to_mpv_helpers_when_gui_executable_is_app(tmp_path, monkeypatch):
    from pudge.cli import build_parser, process_video
    from pudge.pipeline_cache import save_final_pipeline_result

    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    video, subtitle = tmp_path / "Generated - 01.mkv", tmp_path / "generated.srt"
    video.write_bytes(b"generated video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕テスト\n")
    save_final_pipeline_result(
        video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="external"
    )
    monkeypatch.setenv("PUDGE_PYTHON", "/tmp/venv/bin/python")
    monkeypatch.setattr(sys, "executable", "/Applications/pudge.app/Contents/MacOS/pudge")
    captured = []
    monkeypatch.setattr("pudge.cli.run_mpv", lambda command, **kw: captured.append(kw["env_overrides"]) or 0)
    args = build_parser().parse_args([str(video)])
    assert process_video(video, args, cfg, None) == 0
    assert captured[0]["PUDGE_PYTHON"] == "/tmp/venv/bin/python"


@pytest.mark.parametrize("language,expected", [("en", "Entry counted"), ("ru", "Серия засчитана")])
def test_successful_anilist_update_has_short_osd(language, expected, tmp_path, monkeypatch, capsys):
    from pudge import cli

    cfg = AppConfig()
    cfg.ui.language = language
    cfg.anilist.access_token = "test-token"
    monkeypatch.setattr(
        cli,
        "read_tracking_file",
        lambda path: SimpleNamespace(
            video=str(tmp_path / "generated.mkv"), media_id=1, episode=2, total_episodes=12
        ),
    )
    monkeypatch.setattr(
        cli,
        "AniListClient",
        lambda *a, **kw: SimpleNamespace(
            update_progress=lambda *a, **kw: {"updated": True, "progress": 2, "status": "CURRENT"},
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(cli, "Database", lambda path: SimpleNamespace(schedule_cleanup=lambda *a, **kw: True))
    args = SimpleNamespace(tracking_file=tmp_path / "tracking.json", anilist_action="count", manual=True)
    assert cli._run_anilist_action(args, cfg) == 0
    osd = [line for line in capsys.readouterr().out.splitlines() if line.startswith("OSD:")]
    assert osd == ["OSD:" + expected]


def test_mpv_does_not_inherit_gui_bundle_environment(monkeypatch):
    from pudge.player import run_mpv

    captured = {}
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "/Applications/pudge.app/Contents/MacOS/pudge")
    monkeypatch.setenv("__CFBundleIdentifier", "com.pudge.app")
    monkeypatch.setenv("PUDGE_APP_ICON", "/tmp/icon.png")
    monkeypatch.setattr("pudge.process_cleanup.process_snapshot", dict)

    def spawn(command, env):
        captured.update(env)
        return SimpleNamespace(pid=123456789, poll=lambda: 0, wait=lambda timeout=None: 0)

    monkeypatch.setattr("pudge.player.subprocess.Popen", spawn)
    assert run_mpv(["mpv", "--", "generated.mkv"], env_overrides={"PUDGE_PYTHON": "/tmp/venv/python"}) == 0
    assert captured["PUDGE_PYTHON"] == "/tmp/venv/python"
    assert not {"__PYVENV_LAUNCHER__", "__CFBundleIdentifier", "PUDGE_APP_ICON"}.intersection(captured)


def test_mac_diagnostic_identifies_two_app_registrations_and_ignores_other_apps(monkeypatch):
    def app(pid, name, bundle):
        return SimpleNamespace(
            localizedName=lambda: name,
            bundleIdentifier=lambda: bundle,
            processIdentifier=lambda: pid,
            activationPolicy=lambda: 0,
            executableURL=lambda: SimpleNamespace(
                path=lambda: f"/Applications/{name}.app/Contents/MacOS/{name}"
            ),
        )

    apps = [
        app(12, "pudge", "com.pudge.app"),
        app(13, "pudge", "com.pudge.app"),
        app(14, "Mail", "com.apple.mail"),
    ]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(
            NSWorkspace=SimpleNamespace(
                sharedWorkspace=lambda: SimpleNamespace(runningApplications=lambda: apps)
            )
        ),
    )
    assert [row["pid"] for row in mac_app_instances()] == [12, 13]
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())
    assert mac_app_instances() == []
