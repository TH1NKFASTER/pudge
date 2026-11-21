"""The window's Settings state carries no credential (JITEN-10 leftover).

Every state build used to send the Jiten and jpdb tokens, the TTS server key and the
proxy password to the WebView. Now a saved secret is the mask (plus jiten_configured /
jpdb_configured); saving the mask keeps the saved value, an emptied field clears it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.secrets_store import SECRET_MASK
from test_web_app import make_api

SECRETS = {
    "qbt": "qbt-password-0123",
    "qbt_key": "qbt-api-key-0123",
    "anilist": "anilist-token-0123",
    "jimaku": "jimaku-personal-0123",
    "llm": "llm-key-0123",
    "proxy": "proxy-password-0123",
    "jiten": "jiten-key-0123",
    "jpdb": "jpdb-token-0123",
    "tts": "tts-key-0123",
    "tts_url": "tts-url-password-0123",
}
PROXY = f"socks5://alice:{SECRETS['proxy']}@127.0.0.1:1080"
TTS_URL = f"https://bob:{SECRETS['tts_url']}@tts.example:8443"


@pytest.fixture
def api(tmp_path: Path):
    instance = make_api(tmp_path)
    cfg = instance.config
    cfg.qbittorrent.password = SECRETS["qbt"]
    cfg.qbittorrent.api_key = SECRETS["qbt_key"]
    cfg.anilist.access_token = SECRETS["anilist"]
    cfg.jimaku.personal_api_key = cfg.jimaku.api_key = SECRETS["jimaku"]
    cfg.llm.api_key = SECRETS["llm"]
    cfg.nyaa.proxy_url = PROXY
    instance.light_novels.save_settings(
        {
            "jiten_api_key": SECRETS["jiten"],
            "jpdb_api_token": SECRETS["jpdb"],
            "irodori_tts_api_key": SECRETS["tts"],
            "irodori_tts_url": TTS_URL,
        }
    )
    try:
        yield instance
    finally:
        instance.close()


def _leaks(payload) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return [name for name, secret in SECRETS.items() if secret in text]


def test_the_settings_state_holds_masks_not_secrets(api) -> None:
    settings = api._settings_payload()
    assert _leaks(settings) == []
    ln = settings["light_novels"]
    assert ln["jiten_api_key"] == ln["jpdb_api_token"] == ln["irodori_tts_api_key"] == SECRET_MASK
    assert ln["jiten_configured"] is True and ln["jpdb_configured"] is True
    assert ln["irodori_tts_url"] == f"https://bob:{SECRET_MASK}@tts.example:8443"
    assert settings["proxy_url"] == f"socks5://alice:{SECRET_MASK}@127.0.0.1:1080"
    assert settings["qbt_password"] == settings["anilist_token"] == SECRET_MASK
    # The whole window state and the other pages' settings too.
    assert _leaks(api.get_state()) == []
    assert _leaks(api.light_novel_state()) == []
    assert _leaks(api.screen_text_settings()) == []


def test_saving_the_form_unchanged_keeps_every_secret(api) -> None:
    form = api._settings_payload()
    ln = form["light_novels"]
    api.save_settings(
        {
            "proxy_url": form["proxy_url"],
            "qbt_password": form["qbt_password"],
            "qbt_api_key": form["qbt_api_key"],
            "anilist_token": form["anilist_token"],
            "ln_jiten_api_key": ln["jiten_api_key"],
            "ln_jpdb_api_token": ln["jpdb_api_token"],
            "ln_irodori_tts_api_key": ln["irodori_tts_api_key"],
            "ln_irodori_tts_url": ln["irodori_tts_url"],
        }
    )
    saved = api.light_novels.settings()
    assert (saved.jiten_api_key, saved.jpdb_api_token, saved.irodori_tts_api_key) == (
        SECRETS["jiten"],
        SECRETS["jpdb"],
        SECRETS["tts"],
    )
    assert saved.irodori_tts_url == TTS_URL
    assert api.config.nyaa.proxy_url == PROXY
    assert api.config.qbittorrent.password == SECRETS["qbt"]
    assert api.config.anilist.access_token == SECRETS["anilist"]


def test_a_typed_value_replaces_and_an_emptied_field_clears(api) -> None:
    api.save_settings({"ln_jiten_api_key": "new-jiten-key", "ln_jpdb_api_token": "", "proxy_url": ""})
    saved = api.light_novels.settings()
    assert saved.jiten_api_key == "new-jiten-key" and saved.jpdb_api_token == ""
    assert api.config.nyaa.proxy_url == ""
    assert api._settings_payload()["light_novels"]["jpdb_configured"] is False


def test_the_reader_settings_patch_keeps_the_tokens(api) -> None:
    # The reader saves its whole settings object back (with the masks in it).
    api.light_novel_save_settings({**api.light_novels.settings_payload(), "reader_font_size": 21})
    saved = api.light_novels.settings()
    assert saved.reader_font_size == 21
    assert (saved.jiten_api_key, saved.irodori_tts_api_key) == (SECRETS["jiten"], SECRETS["tts"])


def test_connection_tests_use_the_saved_secret_for_a_masked_field(api, monkeypatch) -> None:
    from pudge import web_app

    seen: dict[str, object] = {}

    class Client:
        def __init__(self, url, user, password, api_key, **_kwargs):
            seen.update(url=url, password=password, api_key=api_key)
            self.base_url = url

        def version(self):
            return "v5.0.0"

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", Client)
    form = api._settings_payload()
    api.test_qbittorrent(
        {"qbt_url": form["qbt_url"], "qbt_password": SECRET_MASK, "qbt_api_key": SECRET_MASK}
    )
    assert seen["password"] == SECRETS["qbt"] and seen["api_key"] == SECRETS["qbt_key"]

    class Nyaa:
        def __init__(self, *_args, proxy_url="", proxy_mode="", **_kwargs):
            seen["proxy"] = proxy_url
            self.proxy_url, self.proxy_mode = proxy_url, proxy_mode

        def search(self, _query):
            return []

    monkeypatch.setattr(web_app, "NyaaClient", Nyaa)
    result = api.test_nyaa_proxy({"proxy_mode": "proxy_only", "proxy_url": form["proxy_url"]})
    assert seen["proxy"] == PROXY
    assert SECRETS["proxy"] not in json.dumps(result)

    runtime = api._audiobook_generation_runtime(
        {"provider": "external", "url": form["light_novels"]["irodori_tts_url"], "api_key": SECRET_MASK}
    )
    assert runtime["api_key"] == SECRETS["tts"] and runtime["base_url"] == TTS_URL


# ------------------------------------------- connection tests never echo a URL password


def _refusing_get(status: int):
    import httpx

    def get(url, **_kwargs):
        request = httpx.Request("GET", url)
        response = httpx.Response(status, request=request, text="nope")
        response.raise_for_status()
        return response

    return get


def test_tts_connection_tests_return_and_report_the_address_without_its_password(api, monkeypatch) -> None:
    from pudge import web_app

    form = api._settings_payload()["light_novels"]
    masked = form["irodori_tts_url"]
    monkeypatch.setattr(web_app.httpx, "get", _refusing_get(200))
    ok = api.test_audiobook_generation({"provider": "external", "url": masked, "api_key": SECRET_MASK})
    assert ok["url"] == masked and _leaks(ok) == []
    health = api.test_irodori_tts({"url": masked, "api_key": SECRET_MASK})
    assert health["url"] == masked and _leaks(health) == []

    monkeypatch.setattr(web_app.httpx, "get", _refusing_get(401))
    with pytest.raises(Exception) as caught:
        api.test_audiobook_generation({"provider": "external", "url": masked, "api_key": SECRET_MASK})
    message = str(caught.value)
    assert "tts.example" in message and SECRETS["tts_url"] not in message
    # pywebview sends the traceback too: no chained exception with the original text.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_proxy_and_qbittorrent_test_errors_are_redacted(api, monkeypatch) -> None:
    from pudge import web_app

    class Nyaa:
        def __init__(self, *_args, proxy_url="", **_kwargs):
            self.proxy_url = proxy_url

        def search(self, _query):
            raise RuntimeError(f"unreachable through {self.proxy_url}")

    monkeypatch.setattr(web_app, "NyaaClient", Nyaa)
    with pytest.raises(RuntimeError) as caught:
        api.test_nyaa_proxy({"proxy_mode": "proxy_only", "proxy_url": api._settings_payload()["proxy_url"]})
    assert SECRETS["proxy"] not in str(caught.value) and "127.0.0.1:1080" in str(caught.value)

    class Failing:
        def __init__(self, url, *_args, **_kwargs):
            self.base_url = url

        def version(self):
            raise RuntimeError(
                "401 for url 'http://admin:qbt-password-0123@127.0.0.1:8080/api/v2/app/version'"
            )

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", Failing)
    with pytest.raises(RuntimeError) as caught:
        api.test_qbittorrent({"qbt_url": "http://127.0.0.1:8080", "qbt_password": SECRET_MASK})
    assert SECRETS["qbt"] not in str(caught.value)


def test_qbittorrent_test_sends_saved_secrets_only_to_the_saved_address(api, monkeypatch) -> None:
    from pudge import web_app

    api.config.qbittorrent.base_url = "http://127.0.0.1:8080"
    seen: list[tuple[str, str, str]] = []

    class Client:
        def __init__(self, url, _user, password, api_key, **_kwargs):
            seen.append((url, password, api_key))
            self.base_url = url

        def version(self):
            return "v5.0.0"

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", Client)
    masked = {"qbt_password": SECRET_MASK, "qbt_api_key": SECRET_MASK}
    api.test_qbittorrent({"qbt_url": "http://evil.example:8080", **masked})
    api.test_qbittorrent({"qbt_url": "http://127.0.0.1:9090", **masked})
    api.test_qbittorrent({"qbt_url": "http://localhost:8080/", **masked})
    api.test_qbittorrent({"qbt_url": "http://evil.example:8080", "qbt_password": "typed-now"})
    assert seen == [
        ("http://evil.example:8080", "", ""),
        ("http://127.0.0.1:9090", "", ""),
        ("http://localhost:8080/", SECRETS["qbt"], SECRETS["qbt_key"]),
        ("http://evil.example:8080", "typed-now", ""),
    ]


def test_an_edited_proxy_host_does_not_get_the_saved_password(api) -> None:
    edited = api._settings_payload()["proxy_url"].replace("127.0.0.1", "other.example")
    api.save_settings({"proxy_url": edited})
    assert api.config.nyaa.proxy_url == "socks5://alice@other.example:1080"
