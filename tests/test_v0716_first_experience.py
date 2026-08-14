from pathlib import Path

from pudge.providers.qbittorrent import QBittorrentClient


ROOT = Path(__file__).resolve().parents[1]


def test_anilist_credentials_are_revealed_in_two_steps() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    advanced = (ROOT / "pudge" / "settings_ui.py").read_text(encoding="utf-8")

    client = html.index('clientId=`${prefix}_anilist_client`')
    token_step = html.index('id="${tokenId}_step"', client)
    token = html.index('id="${tokenId}" type="password"', token_step)

    assert client < token_step < token
    assert 'data-anilist-continue' in html
    assert 'data-anilist-back' in html
    assert 'data-anilist-copy="${ANILIST_REDIRECT_URL}"' in html
    assert "syncAniListCredentialFlow('o_anilist_client','o_anilist_token')" in html
    assert "Get the key and paste it into the new field" in html
    assert 'text="Создать Client ID"' in advanced
    assert 'anilist_client_id_var.trace_add("write", update_anilist_token_step)' in advanced


def test_first_experience_uses_managed_downloads_by_default() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    example = (ROOT / "pudge" / "config.example.toml").read_text(encoding="utf-8")

    assert "Automatic downloads work through Pudge with no torrent-client setup." in html
    assert "Use qBittorrent instead (advanced)" in html
    assert "d.aria2_enabled=!d.qbt_enabled&&d.profile==='full'" in html
    assert "aria2_enabled=full&&!ui.onboardingDraft.qbt_enabled" in html
    assert "[aria2]\nenabled = true" in example


def test_qbittorrent_below_52_uses_password_instead_of_api_key() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Versions below 5.2 use username and password" in html
    assert "API-key authentication requires qBittorrent 5.2 or newer" in readme


def test_qbittorrent_add_fields_follow_installed_version(monkeypatch) -> None:
    legacy = QBittorrentClient("http://qbt.local", username="admin", password="secret")
    modern = QBittorrentClient("http://qbt.local", api_key="secret")
    monkeypatch.setattr(legacy, "version", lambda: "v5.1.4")

    try:
        assert legacy._add_state_payload(paused=True, stop_at_metadata=False) == {
            "paused": "true"
        }
        assert modern._add_state_payload(paused=True, stop_at_metadata=False) == {
            "stopped": "true",
            "contentLayout": "Original",
        }
    finally:
        legacy.close()
        modern.close()
