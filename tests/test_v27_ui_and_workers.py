"""Offline regressions for UI languages, WordDto readings and GUI-free workers."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pudge.config import AppConfig
from pudge.jiten_words import JitenWordRepository, normalize_card
from pudge.runtime import mac_app_instances, worker_environment
from pudge.i18n import message
from page_harness import web_module
from tests.i18n_support import i18n_source


# (Russian, English) pairs of the former pudge/ui_localization catalog. Audit 68Б
# replaced it with the error.* entries of pudge/web/locales.<lang>.js, read by
# pudge.i18n.message and PudgeI18n.message; the same cases must still translate.
# The torrent-not-listed message names one backend twice ({backend} in the catalog).
LEGACY_ERRORS = (
    (
        "Нужен AniList ID или ссылка вида https://anilist.co/anime/12345",
        "An AniList ID or a URL such as https://anilist.co/anime/12345 is required",
    ),
    ("AniList ID должен быть положительным", "AniList ID must be positive"),
    ("Файл субтитров не найден: {0}", "Subtitle file not found: {0}"),
    ("Видео не найдено: {0}", "Video not found: {0}"),
    ("Все torrent backend отключены", "All torrent backends are disabled"),
    ("AniList id={0} отсутствует в локальной базе", "AniList id={0} is missing from the local database"),
    ("Поиск релизов отключён", "Release search is disabled"),
    ("Torrent-загрузки отключены в настройках", "Torrent downloads are disabled in Settings"),
    ("AniList id={0} отсутствует в базе", "AniList id={0} is missing from the database"),
    (
        "{0} принял запрос, но торрент не появился в списке загрузок. Повтори попытку; если ошибка сохранится, открой Diagnostics — там будет причина проверки {0}.",
        "{0} accepted the request, but the torrent did not appear in Downloads. Retry; if the error persists, open Diagnostics for the {0} check details.",
    ),
    ("Не удалось прочитать контейнер через ffprobe: {0}", "Could not read the container with ffprobe: {0}"),
    ("Не найден mpv: {0}", "mpv not found: {0}"),
    ("Ошибка AniList: HTTP {0} для {1}", "AniList error: HTTP {0} for {1}"),
    ("Ошибка AniList: некорректный JSON: {0}", "AniList error: invalid JSON: {0}"),
    ("AniList вернул ошибку: {0}", "AniList returned an error: {0}"),
    ("AniList вернул ответ без data", "AniList returned a response without data"),
    ("Ошибка AniList: {0}", "AniList error: {0}"),
    ("Не удалось определить пользователя AniList", "Could not identify the AniList user"),
    ("Аниме AniList id={0} не найдено", "Anime AniList id={0} was not found"),
    ("AniList не вернул обновлённую запись списка", "AniList did not return the updated list entry"),
    ("Недопустимый статус AniList: {0}", "Invalid AniList status: {0}"),
    ("Оценка AniList должна быть от 1 до 10", "AniList score must be between 1 and 10"),
    ("AniList не вернул сохранённую оценку", "AniList did not return the saved score"),
    ("Номер серии для AniList должен быть положительным", "AniList episode number must be positive"),
    (
        "Встроенный torrent backend не найден: aria2c не установлен. Повторно запусти install.sh или выполни: brew install aria2",
        "The built-in torrent backend was not found: aria2c is not installed. Run install.sh again or use: brew install aria2",
    ),
    ("aria2 RPC недоступен: {0}{1}", "aria2 RPC unavailable: {0}{1}"),
    ("aria2 RPC недоступен: {0}", "aria2 RPC unavailable: {0}"),
    (
        "Kill switch включён, но VPN interface не указан. Укажи активный интерфейс VPN (обычно utun…)",
        "Kill switch is enabled, but the VPN interface is not set. Enter the active VPN interface (usually utun…)",
    ),
    (
        "VPN interface {0} сейчас недоступен; torrent traffic заблокирован",
        "VPN interface {0} is unavailable; torrent traffic is blocked",
    ),
    ("Встроенный aria2 backend отключён", "The built-in aria2 backend is disabled"),
    ("Не удалось запустить aria2c: {0}", "Could not start aria2c: {0}"),
    ("aria2c запущен, но RPC не ответил на порту {0}", "aria2c started, but RPC did not respond on port {0}"),
    ("Не удалось остановить старый Pudge aria2 sidecar", "Could not stop the previous Pudge aria2 sidecar"),
    ("aria2 RPC не запущен", "aria2 RPC is not running"),
    (
        "Релиз не содержит ни magnet-ссылки, ни torrent URL",
        "The release contains neither a magnet link nor a torrent URL",
    ),
    ("Не задан JIMAKU_API_KEY", "JIMAKU_API_KEY is not set"),
    ("Ошибка Jimaku API: {0}", "Jimaku API error: {0}"),
    ("Ошибка Jimaku API: неизвестная сетевая ошибка", "Jimaku API error: unknown network error"),
    ("Не удалось скачать {0}: {1}", "Could not download {0}: {1}"),
    ("Nyaa вернул некорректный RSS: {0}", "Nyaa returned invalid RSS: {0}"),
    (
        "Команда перед поиском Nyaa завершилась с ошибкой: {0}",
        "The command before the Nyaa search failed: {0}",
    ),
    (
        "Для SOCKS-прокси не установлена зависимость socksio. Повторно запустите ./install.sh.",
        "The SOCKS proxy dependency socksio is not installed. Run ./install.sh again.",
    ),
    ("Nyaa недоступен{0}: {1}", "Nyaa unavailable{0}: {1}"),
    (
        "Команда VPN перед скачиванием завершилась с ошибкой: {0}",
        "The VPN command before downloading failed: {0}",
    ),
    ("qBittorrent Web API недоступен: {0}", "qBittorrent Web API unavailable: {0}"),
    ("qBittorrent не принял логин: HTTP {0}", "qBittorrent rejected the login: HTTP {0}"),
    ("qBittorrent не принял логин: {0}", "qBittorrent rejected the login: {0}"),
    ("Не удалось получить версию qBittorrent: {0}", "Could not get the qBittorrent version: {0}"),
    (
        "qBittorrent найден, но API key отклонён. Проверьте, что ключ создан в qBittorrent 5.2 → Settings → Web UI → API keys и вставлен полностью.",
        "qBittorrent was found, but the API key was rejected. Check that the key was created in qBittorrent 5.2 → Settings → Web UI → API keys and pasted in full.",
    ),
    ("Не удалось получить версию qBittorrent: HTTP {0}", "Could not get the qBittorrent version: HTTP {0}"),
    ("Не удалось получить категории qBittorrent: {0}", "Could not get qBittorrent categories: {0}"),
    ("Не удалось получить теги qBittorrent: {0}", "Could not get qBittorrent tags: {0}"),
    ("Не удалось снять теги qBittorrent: {0}", "Could not remove qBittorrent tags from the torrent: {0}"),
    ("Не удалось удалить теги qBittorrent: {0}", "Could not delete qBittorrent tags: {0}"),
    (
        "Не удалось создать категорию qBittorrent '{0}': {1}",
        "Could not create qBittorrent category '{0}': {1}",
    ),
    (
        "Торрент добавлен, но не удалось назначить категорию/теги: {0}",
        "The torrent was added, but its category/tags could not be assigned: {0}",
    ),
    (
        "Не удалось обновить путь торрента qBittorrent: {0}",
        "Could not update the qBittorrent torrent path: {0}",
    ),
    ("Не удалось перепроверить торрент qBittorrent: {0}", "Could not recheck the qBittorrent torrent: {0}"),
    ("Не удалось добавить торрент: {0}", "Could not add the torrent: {0}"),
    (
        "qBittorrent не добавил торрент (409 Conflict). Торрент не найден среди существующих; возможны некорректная magnet-ссылка или недоступная папка загрузки: {0}.{1}",
        "qBittorrent did not add the torrent (409 Conflict). It was not found among existing torrents; the magnet link may be invalid or the download folder unavailable: {0}.{1}",
    ),
    (
        "qBittorrent принял запрос, но не смог добавить торрент (failure_count={0})",
        "qBittorrent accepted the request, but could not add the torrent (failure_count={0})",
    ),
    ("qBittorrent вернул неожиданный ответ: {0}", "qBittorrent returned an unexpected response: {0}"),
    (
        "Не удалось получить metadata торрента через qBittorrent: {0}",
        "Could not get torrent metadata with qBittorrent: {0}",
    ),
    ("Не удалось запустить торрент: {0}", "Could not start the torrent: {0}"),
    ("Не удалось остановить торрент: {0}", "Could not stop the torrent: {0}"),
    ("Не удалось получить загрузки qBittorrent: {0}", "Could not get qBittorrent downloads: {0}"),
    ("Не удалось получить файлы торрента: {0}", "Could not get torrent files: {0}"),
    ("Не удалось выбрать файлы торрента: {0}", "Could not select torrent files: {0}"),
    ("Не удалось удалить торрент: {0}", "Could not delete the torrent: {0}"),
    ("Некорректный timestamp субтитров: {0}", "Invalid subtitle timestamp: {0}"),
    (" напрямую", " directly"),
    (" через {0}", " through {0}"),
    (" Ответ qBittorrent: {0}", " qBittorrent response: {0}"),
    (
        "qBittorrent установлен, но Web UI API не отвечает по адресу {0}. Откройте qBittorrent → Settings → Web UI, включите «Web User Interface (Remote control)» и укажите в {1} тот же порт. API key отвечает только за авторизацию и сам Web UI не запускает. Техническая ошибка: {2}",
        "qBittorrent is installed, but its Web UI API does not respond at {0}. Open qBittorrent → Settings → Web UI, enable “Web User Interface (Remote control)” and set the same port in {1}. The API key only handles authorization and does not start the Web UI. Technical error: {2}",
    ),
    ("Не удалось запустить {0}: {1}", "Could not start {0}: {1}"),
    (
        "AniList сохранил изменение, но не отдал обновлённый список; используются локальные данные",
        "AniList saved the change, but did not return the updated list; using local data",
    ),
    (
        "Jimaku: найден архив .7z/.rar, но 7-Zip не установлен",
        "Jimaku: found a .7z/.rar archive, but 7-Zip is not installed",
    ),
    ("Jimaku: не удалось распаковать {0}", "Jimaku: could not extract {0}"),
    (
        "AniList вернул HTTP 500 на расширенный запрос; использован упрощённый запрос, данные графа оставлены из кэша",
        "AniList returned HTTP 500 for the extended query; used a simplified query and kept cached graph data",
    ),
    # Subtitle quality-gate reasons (shown in Diagnostics/debug views).
    ("синхронизация не удалась", "synchronization failed"),
    ("тайминг разрушен при выравнивании: {0}", "timing was broken by alignment: {0}"),
    (
        "встроенная дорожка: ALASS дал неподтверждённый большой скачок (spread={0}s)",
        "embedded track: ALASS produced an unconfirmed large jump (spread={0}s)",
    ),
    (
        "точный одноэпизодный SPECIAL Jimaku; текстовая дорожка совпадает по структуре и таймингу",
        "exact single-episode Jimaku SPECIAL; the text track matches in structure and timing",
    ),
    (
        "точный фильм AniList/Jimaku; текстовая дорожка совпадает по структуре и таймингу",
        "exact AniList/Jimaku movie; the text track matches in structure and timing",
    ),
    (
        "точная серия найдена, но выбранный тайминг нестабилен: {0}",
        "exact episode found, but the selected timing is unstable: {0}",
    ),
    (
        "точная серия AniList/Jimaku; номер и полная структура подтверждены таймингом",
        "exact AniList/Jimaku episode; number and full structure confirmed by timing",
    ),
    ("содержание не совпало с серией", "content does not match the episode"),
    (
        "изменение часов timeline требует проверки по японской аудиодорожке",
        "a timeline clock change needs verification against the Japanese audio track",
    ),
    (
        "timeline-remap ухудшил исходный тайминг по matched/coverage/F1/error; нужна другая временная опора",
        "timeline remap worsened the original timing (matched/coverage/F1/error); another timing reference is needed",
    ),
    ("надёжная встроенная временная дорожка", "reliable embedded timing track"),
    ("нестабильные метрики в разных участках серии", "unstable metrics across parts of the episode"),
    (
        "точная серия Jimaku; различаются только границы эфира",
        "exact Jimaku episode; only broadcast boundaries differ",
    ),
)


def node(script: str) -> str:
    # stdin, not -e: scripts that include the interface catalogs exceed the argument limit.
    return subprocess.check_output(["node", "-"], input=script, text=True, timeout=15)


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
    node(setup + i18n_source() + web_module("ln_drop.js") + checks)


def test_known_errors_translate_both_ways_in_python_and_ui_and_preserve_source_text():
    # One catalog now serves Python and the window, so there is no parity check of two copies.
    values = ["/tmp/Русская книга [01].mkv", "исходный ответ провайдера", "3"]
    russian = [ru.format(*values) for ru, _ in LEGACY_ERRORS]
    english = [en.format(*values) for _, en in LEGACY_ERRORS]
    assert [message(text, "en") for text in russian] == english
    assert [message(text, "ru") for text in russian] == russian
    assert [message(text, "ru") for text in english] == russian
    assert [message(text, "en") for text in english] == english
    setup = "global.window=global;const ui={lang:'en'};"
    checks = f"""
const russian={json.dumps(russian, ensure_ascii=False)}, english={json.dumps(english, ensure_ascii=False)};
const out={{english:russian.map(text=>PudgeI18n.message(text)), englishSame:english.map(text=>PudgeI18n.message(text)),
  legacy:russian.map(text=>window.PudgeUiLanguage.message(text))}};
ui.lang='ru';
out.russian=russian.map(text=>PudgeI18n.message(text));out.russianFromEnglish=english.map(text=>PudgeI18n.message(text));
console.log(JSON.stringify(out));
"""
    result = json.loads(node(setup + i18n_source() + checks))
    assert result["english"] == english
    assert result["englishSame"] == english
    assert result["legacy"] == english
    assert result["russian"] == russian
    assert result["russianFromEnglish"] == russian
    for text in ("Название книги: Видео не найдено", "/tmp/Не найден mpv: книга.mkv", "Свободный текст"):
        assert message(text, "en") == text
        assert message(text, "ru") == text
    nested = "Error: Не удалось запустить Pudge: Не найден mpv: /tmp/Русская книга"
    nested_en = "Error: Could not start Pudge: mpv not found: /tmp/Русская книга"
    assert message(nested, "en") == nested_en
    assert message(nested_en, "ru") == "Ошибка: Не удалось запустить Pudge: Не найден mpv: /tmp/Русская книга"
    probe = f"""
const free={json.dumps(["Название книги: Видео не найдено", "/tmp/Не найден mpv: книга.mkv", "Свободный текст"], ensure_ascii=False)};
console.log(JSON.stringify({{free:free.map(text=>PudgeI18n.message(text,'en')),
  nested:PudgeI18n.message({json.dumps(nested, ensure_ascii=False)},'en'),
  nestedRu:PudgeI18n.message({json.dumps(nested_en, ensure_ascii=False)},'ru')}}));
"""
    result = json.loads(node(setup + i18n_source() + probe))
    assert result["free"] == [
        "Название книги: Видео не найдено",
        "/tmp/Не найден mpv: книга.mkv",
        "Свободный текст",
    ]
    assert result["nested"] == nested_en
    assert result["nestedRu"] == "Ошибка: Не удалось запустить Pudge: Не найден mpv: /tmp/Русская книга"


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


def test_javascript_readings_match_backend(page):
    # The window's Jiten word helpers read a card like the backend does. Opening the word on
    # Jiten from the review gate (open_url, no grade) is tested in
    # test_review_gate_undo_and_layout.py::test_revealing_the_answer_keeps_the_word_in_its_slot_and_adds_the_reading.
    payload = {**powder_dto(), "readingIndex": 0}
    expected = normalize_card(payload)
    result = page.run(
        f"""
        const card = {json.dumps(payload, ensure_ascii=False)};
        const normalized = PudgeJitenWords.normalize(card);
        return {{primary: normalized.primary_reading, all: normalized.all_readings,
                line: PudgeJitenWords.readingsLine(card),
                badUrls: [PudgeJitenWords.url({{wordId: '1/evil', readingIndex: 0}}), PudgeJitenWords.url({{wordId: 1, readingIndex: -1}})],
                url: PudgeJitenWords.url({{wordId: 1504770, readingIndex: 0}})}};
        """
    )
    out = result.value
    assert (out["primary"], out["all"]) == (expected["primary_reading"], expected["all_readings"])
    assert out["line"] == "こ"
    assert out["badUrls"] == ["", ""]
    assert out["url"] == "https://jiten.moe/vocabulary/1504770/0"


# test_advanced_settings_language_and_no_model_save: the Tk settings window (settings_ui.py) was deleted on purpose (68Б).


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
