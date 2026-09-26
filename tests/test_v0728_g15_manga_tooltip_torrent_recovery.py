from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import web_app
from pudge.config import AppConfig
from pudge.providers.aria2 import Aria2Client, Aria2Error


ROOT = Path(__file__).resolve().parents[1]


def test_nplus1_tooltip_is_above_study_card() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/reading_tools.css").read_text(encoding="utf-8")
    assert ".pudge-study-card,.pudge-translation-pop{position:fixed;z-index:13050" in css
    assert ".title-tooltip { position:fixed; z-index:13120;" in html


def test_manga_status_overlay_has_image_space_css() -> None:
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert ".manga-v2-status-layer{" in css
    assert "position:absolute" in css
    assert "z-index:1" in css
    assert ".manga-v2-status-word.state-new{fill:var(--manga-status-new" in css
    assert ".manga-v2-status-word.state-learning{fill:var(--manga-status-learning" in css
    assert ".manga-v2-status-word.state-known{fill:var(--manga-status-known" in css
    assert ".manga-v2-status-word.is-due{" in css
    assert "layer.classList.add('manga-v2-status-layer')" in js


def _enabled_aria_api(tmp_path: Path, monkeypatch, client) -> web_app.WebAppApi:
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path)
    (tmp_path / "aria2").mkdir(parents=True, exist_ok=True)
    api = object.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.qbittorrent.enabled = False
    api.config.aria2.enabled = True
    api.config.nyaa.torrents_enabled = True
    api.manager = SimpleNamespace(
        config=api.config,
        torrent_clients=lambda: [("aria2", client)],
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_authoritative = True
    api._torrent_session_enabled = True
    api._torrent_toggle_generation = 1
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._torrent_off_evidence = {}
    api._downloads_configured = lambda: True
    api.logger = logging.getLogger("test-g15-aria-status")
    return api


def test_enabled_but_absent_lazy_aria_is_fresh_zero(tmp_path, monkeypatch) -> None:
    class MissingAria:
        def traffic_stats(self):
            raise Aria2Error("aria2 RPC не запущен")

        def close(self):
            pass

    client = MissingAria()
    monkeypatch.setattr(web_app, "Aria2Client", MissingAria)
    api = _enabled_aria_api(tmp_path, monkeypatch, client)
    api._observe_aria2_stopped = lambda **_kwargs: True
    result = api.torrent_traffic_status()
    assert result["enabled"] is True
    assert result["stale"] is False
    assert result["observed_state"] == "observed"
    assert result["download_speed"] == 0
    assert result["upload_speed"] == 0
    assert result["backend_errors"] == []


def test_stale_managed_aria_is_not_fresh_zero(tmp_path, monkeypatch) -> None:
    class MissingAria:
        def traffic_stats(self):
            raise Aria2Error("aria2 RPC не запущен")

        def close(self):
            pass

    client = MissingAria()
    monkeypatch.setattr(web_app, "Aria2Client", MissingAria)
    api = _enabled_aria_api(tmp_path, monkeypatch, client)
    api._observe_aria2_stopped = lambda **_kwargs: False
    result = api.torrent_traffic_status()
    assert result["stale"] is True
    assert result["download_speed"] is None
    assert result["backend_errors"] == ["aria2"]


def test_aria_process_ownership_requires_exact_pudge_session(tmp_path, monkeypatch) -> None:
    state = tmp_path / "aria2"
    client = Aria2Client(enabled=True, state_dir=state, auto_start=False)
    session = str((state / "session.txt").resolve())
    output = (
        f"  101 /opt/homebrew/bin/aria2c --enable-rpc=true --input-file={session} --save-session={session}\n"
        "  202 /opt/homebrew/bin/aria2c --enable-rpc=true --input-file=/tmp/foreign/session.txt\n"
        "  303 python something.py\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=output))
    try:
        assert client._managed_sidecar_pids() == [101]
        assert client.managed_sidecar_running() is True
    finally:
        client.close()


def test_ensure_running_repairs_owned_unreachable_sidecar(tmp_path, monkeypatch) -> None:
    client = Aria2Client(enabled=True, state_dir=tmp_path / "aria2", auto_start=True)
    calls: list[str] = []
    monkeypatch.setattr(client, "_validate_network_guard", lambda: None)
    monkeypatch.setattr(client, "_probe", lambda: False)
    monkeypatch.setattr(client, "_stop_unreachable_managed_sidecars", lambda: calls.append("stop") or True)
    monkeypatch.setattr(client, "_start", lambda: calls.append("start"))
    try:
        client.ensure_running()
        assert calls == ["stop", "start"]
    finally:
        client.close()


def test_shutdown_treats_no_managed_sidecar_as_stopped(tmp_path, monkeypatch) -> None:
    client = Aria2Client(enabled=True, state_dir=tmp_path / "aria2", auto_start=False)
    monkeypatch.setattr(client, "_probe", lambda: False)
    monkeypatch.setattr(client, "_stop_unreachable_managed_sidecars", lambda: True)
    try:
        assert client.shutdown() is True
    finally:
        client.close()
