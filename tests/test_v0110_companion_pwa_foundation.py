from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pudge.database import Database
from pudge.mobile_sync import MobileSyncService
from pudge.mobile_sync_http import start_mobile_sync_server


ROOT = Path(__file__).parents[1]
WEB = ROOT / "pudge" / "web" / "companion"


def test_companion_pwa_assets_and_lazy_storage_policy() -> None:
    for name in ("index.html", "app.js", "styles.css", "manifest.webmanifest", "sw.js", "icon.svg"):
        assert (WEB / name).is_file()

    manifest = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "/companion/"
    assert manifest["scope"] == "/companion/"
    assert manifest["display"] == "standalone"

    app = (WEB / "app.js").read_text(encoding="utf-8")
    for contract in ("PUDGE_COMPANION_PWA_V1", "navigator.storage.estimate", "indexedDB.open", "lastUsed", "pinned", "evictUnusedMedia"):
        assert contract in app

    worker = (WEB / "sw.js").read_text(encoding="utf-8")
    assert "url.pathname.startsWith('/api/')" in worker
    assert "event.respondWith(fetch(event.request))" in worker


def test_companion_shell_public_library_private(tmp_path: Path) -> None:
    service = MobileSyncService(Database(tmp_path / "pudge.sqlite3"))
    server, thread = start_mobile_sync_server(service, host="127.0.0.1", port=0)
    host, port = server.server_address
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/companion/", timeout=2) as response:
            assert response.status == 200
            assert "Connect to Pudge" in response.read().decode("utf-8")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://{host}:{port}/api/v1/library", timeout=2)
        assert exc.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_desktop_companion_bridge_contract() -> None:
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert "def companion_enable_lan(" in source
    assert 'cfg.bind_host = "0.0.0.0"' in source
    assert "def _companion_effective_base_url(" in source
    assert 'payload["companion_url"]' in source
    settings = (ROOT / "pudge" / "web" / "settings.js").read_text(encoding="utf-8")
    assert "companion_start_pairing" in settings
    assert "companionPairUrl" in settings
