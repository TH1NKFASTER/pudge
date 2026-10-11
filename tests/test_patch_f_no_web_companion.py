"""Patch F (audit 27Б): the browser companion is removed; the iPhone app is the mobile client."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import tomllib
from pathlib import Path

from pudge import config as config_module
from pudge.config import AppConfig, load_config, write_config
from pudge.web_app import WebAppApi

ROOT = Path(__file__).parents[1]


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_companion_code_and_assets_are_gone(tmp_path: Path, page) -> None:
    for module in ("pudge.mobile_sync", "pudge.mobile_sync_http", "pudge.companion_streaming"):
        assert importlib.util.find_spec(module) is None
    assert not (ROOT / "pudge" / "web" / "companion").exists()
    assert not [name for name in dir(WebAppApi) if name.startswith("companion_")]
    # Settings has no companion block (any category) and never asks the backend about one.
    for lang in ("en", "ru"):
        result = page.run(
            "setPage('settings', true); await __tick(100); return $('settings').textContent", lang=lang
        )
        assert not re.search(r"compan|компаньон", result.value, re.IGNORECASE)
        assert "iPhone" in result.value
        assert not [call for call in result.calls if call["name"].startswith("companion")]
    # A first start copies the bundled example config: no [companion] section in it.
    config_path = config_module.write_default_config(tmp_path / "config.toml")
    assert "companion" not in tomllib.loads(config_path.read_text(encoding="utf-8"))


def test_old_companion_section_is_ignored_and_dropped_on_save(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    cfg = AppConfig()
    cfg.config_path = config_path
    write_config(cfg, config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[companion]\nenabled = true\nbind_host = "0.0.0.0"\nport = 47821\n',
        encoding="utf-8",
    )
    config_module.CONFIG_WARNINGS.clear()

    loaded = load_config(config_path, resolve_secrets=False)

    assert not any("companion" in message for message in config_module.CONFIG_WARNINGS)
    assert not hasattr(loaded, "companion")
    write_config(loaded, config_path)
    assert "[companion]" not in config_path.read_text(encoding="utf-8")


def test_leftover_companion_video_cache_is_removed(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    segments = tmp_path / "cache" / "companion-hls" / "entity"
    segments.mkdir(parents=True)
    (segments / "index.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    keep = tmp_path / "cache" / "intro"
    keep.mkdir(parents=True, exist_ok=True)
    api.cache_registry.register("hls", segments)
    api.cache_registry.register("intro", keep)

    api._remove_web_companion_leftovers()

    assert not (tmp_path / "cache" / "companion-hls").exists()
    assert keep.exists()
    with api.manager.db.connect() as conn:
        categories = {row[0] for row in conn.execute("SELECT category FROM cache_registry")}
    assert "hls" not in categories and "intro" in categories


def test_iphone_pairing_still_finds_the_lan_address(tmp_path: Path, monkeypatch) -> None:
    # The pairing QR code carries the Mac's LAN address (never 127.0.0.1) and the
    # phone pairs through it with the HTTPS server.
    import http.client
    import ssl
    import uuid

    from pudge import iphone_http, iphone_network

    real_start = iphone_http.start_iphone_server
    monkeypatch.setattr(
        iphone_http, "start_iphone_server", lambda service: real_start(service, host="127.0.0.1", port=0)
    )
    api = make_api(tmp_path)

    try:
        assert api.iphone_enable()["enabled"] is True
        with monkeypatch.context() as patched:
            # The Mac's Wi-Fi address (also the one it reaches the internet through).
            patched.setattr(
                iphone_network, "interface_addresses", lambda: [("lo0", "127.0.0.1"), ("en0", "192.168.1.20")]
            )
            patched.setattr(iphone_network, "default_route_address", lambda: "192.168.1.20")
            pairing = api.iphone_start_pairing()
        encoded = pairing["invitation"].split("data=", 1)[1]
        invitation = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        port = api._iphone_server.server_port
        assert invitation["baseURL"] == f"https://192.168.1.20:{port}"
        assert pairing["qrDataURL"].startswith("data:image/svg+xml;base64,")

        context = ssl.create_default_context(cafile=str(api._iphone_service.root / "tls-cert.pem"))
        context.check_hostname = False
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
        body = {
            "invitation": invitation["invitation"],
            "deviceId": str(uuid.uuid4()),
            "attemptId": str(uuid.uuid4()),
            "name": "Phone",
        }
        connection.request("POST", "/api/v2/pair", json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())["status"] == "pending"
        assert api.iphone_status()["paired"] is False  # waits for Allow on the Mac
        api.iphone_approve_pairing(body["deviceId"])
        connection.request("POST", "/api/v2/pair", json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())["token"]
        connection.close()
        assert api.iphone_status()["paired"] is True
        assert api.iphone_disable()["enabled"] is False  # in-flight requests drained, server closed

        # No route out: a LAN address of an interface, not the loopback one.
        with monkeypatch.context() as patched:
            patched.setattr(
                iphone_network, "interface_addresses", lambda: [("lo0", "127.0.0.1"), ("en0", "10.0.0.5")]
            )
            patched.setattr(iphone_network, "default_route_address", lambda: "")
            assert api._lan_host() == "10.0.0.5"
    finally:
        api.close()
