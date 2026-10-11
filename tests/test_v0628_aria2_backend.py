from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, NyaaRelease
from pudge.providers.aria2 import Aria2Client
from source_policy import source
from tests.i18n_support import en, ru
from test_v0711_gui_homebrew_path import FORMULAS, brew_calls, run_installer


def release() -> NyaaRelease:
    return NyaaRelease(
        title="[Group] Example - 01 [1080p]",
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="0123456789abcdef0123456789abcdef01234567",
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=20,
        leechers=2,
        downloads=100,
        trusted=True,
        remake=False,
        score=96.0,
    )


def test_aria2_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig()
    config.aria2.enabled = True
    config.aria2.binary = "/opt/homebrew/bin/aria2c"
    config.aria2.rpc_port = 6812
    write_config(config, path)

    loaded = load_config(path)
    assert loaded.aria2.enabled is True
    assert loaded.aria2.binary == "aria2c"
    assert loaded.aria2.rpc_port == 6812


def test_manager_uses_aria2_when_qbittorrent_is_disabled(tmp_path: Path) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    config.aria2.binary = "/tmp/aria2c"
    manager = AnimeManager(config)

    client = manager.qbt_client()
    try:
        assert isinstance(client, Aria2Client)
        assert manager.downloads_enabled() is True
        assert manager.torrent_backend_name() == "aria2"
    finally:
        client.close()


def test_aria2_add_and_list_preserves_anime_metadata(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[tuple[str, list[object]]] = []
    gid = "0123456789abcdef"

    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_probe", lambda: True)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: None)

    def rpc(method: str, params=None):
        params = list(params or [])
        calls.append((method, params))
        if method == "aria2.addUri":
            return gid
        if method == "aria2.tellActive":
            return [
                {
                    "gid": gid,
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "250",
                    "downloadSpeed": "100",
                    "dir": str(tmp_path / "downloads"),
                    "infoHash": release().info_hash,
                    "files": [
                        {
                            "path": str(tmp_path / "downloads" / "Example 01.mkv"),
                            "length": "1000",
                            "completedLength": "250",
                            "selected": "true",
                        }
                    ],
                    "bittorrent": {"info": {"name": "Example 01.mkv"}},
                }
            ]
        if method in {"aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.add_release(
        release(),
        save_path=tmp_path / "downloads",
        category="pudge",
        tags=["anime: Example", "anilist: 123", "episode: 1", "score: 96"],
    )
    items = client.torrents()

    assert len(items) == 1
    item = items[0]
    assert item.torrent_hash == release().info_hash
    assert item.progress == 0.25
    assert item.media_id == 123
    assert item.episode == 1
    assert item.raw["backend"] == "aria2"
    assert item.raw["_release_score_tag"] == 96.0
    assert any(method == "aria2.addUri" for method, _ in calls)
    client.close()


def test_aria2_prefers_tracker_rich_torrent_payload(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: b"d4:infode")

    def rpc(method: str, params=None):
        values = list(params or [])
        calls.append((method, values))
        if method == "aria2.addTorrent":
            return "0123456789abcdef"
        raise AssertionError((method, values))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.add_release(
        release(),
        save_path=tmp_path / "downloads",
        category="pudge",
        tags=["anime: Example", "anilist: 123"],
    )

    assert [method for method, _params in calls] == ["aria2.addTorrent"]
    metadata = client._load_metadata()["0123456789abcdef"]
    assert metadata["listed_seeders"] == 20
    assert metadata["listed_leechers"] == 2
    client.close()


def test_aria2_large_torrent_avoids_default_rpc_request_limit(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(
        client,
        "_torrent_payload",
        lambda _url: b"d" + b"x" * (client._SAFE_RPC_TORRENT_BYTES + 1),
    )

    def rpc(method: str, params=None):
        values = list(params or [])
        calls.append((method, values))
        if method == "aria2.addUri":
            return "0123456789abcdef"
        raise AssertionError((method, values))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.add_release(
        release(),
        save_path=tmp_path / "downloads",
        category="pudge",
        tags=["anime: Example", "anilist: 123"],
    )

    assert [method for method, _params in calls] == ["aria2.addUri"]
    assert str(calls[0][1][0][0]).startswith("magnet:?")
    client.close()


def test_aria2_start_and_pause_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[str] = []
    states = iter(("active", "paused", "paused", "active"))
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_probe", lambda: True)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: "0123456789abcdef")

    def rpc(method: str, params=None):
        calls.append(method)
        if method == "aria2.tellStatus":
            return {"status": next(states)}
        if method in {"aria2.unpause", "aria2.pause"}:
            return "OK"
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.start("hash")
    client.start("hash")
    client.pause("hash")
    client.pause("hash")

    assert calls == [
        "aria2.tellStatus",
        "aria2.tellStatus",
        "aria2.unpause",
        "aria2.tellStatus",
        "aria2.tellStatus",
        "aria2.pause",
    ]
    client.close()


def test_aria2_reconnect_force_pauses_and_unpauses_without_removing_data(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[str] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: "0123456789abcdef")

    def rpc(method: str, params=None):
        calls.append(method)
        if method == "aria2.tellStatus":
            return {"status": "active", "totalLength": "1000", "completedLength": "532"}
        if method in {"aria2.forcePause", "aria2.unpause"}:
            return "OK"
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    assert client.reconnect("hash") is True
    assert calls == ["aria2.tellStatus", "aria2.forcePause", "aria2.unpause"]
    assert "aria2.forceRemove" not in calls
    client.close()


def test_manager_reconnects_aria2_after_progress_is_stalled(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    manager = AnimeManager(config)

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconnect(self, torrent_hash: str) -> bool:
            self.calls.append(torrent_hash)
            return True

    client = Client()
    item = DownloadItem(
        torrent_hash="abc",
        name="Akiba",
        state="active",
        progress=0.532,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Akiba"),
        raw={
            "backend": "aria2",
            "downloaded": 532,
            "total_size": 1000,
            "download_speed": 0,
            "num_connections": 0,
            "listed_seeders": 22,
        },
    )
    assert manager._recover_stalled_aria2_downloads(client, [item], now=1000) == 0
    assert manager._recover_stalled_aria2_downloads(client, [item], now=1901) == 1
    assert client.calls == ["abc"]


def test_manager_does_not_reconnect_aria2_during_piece_verification(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    manager = AnimeManager(config)

    class Client:
        def reconnect(self, torrent_hash: str) -> bool:
            raise AssertionError(torrent_hash)

    item = DownloadItem(
        torrent_hash="abc",
        name="Akiba",
        state="verifying",
        progress=1.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Akiba"),
        raw={
            "backend": "aria2",
            "downloaded": 1000,
            "total_size": 1000,
            "download_speed": 0,
            "num_connections": 0,
            "verifying": True,
        },
    )

    assert manager._recover_stalled_aria2_downloads(Client(), [item], now=1000) == 0
    assert manager._recover_stalled_aria2_downloads(Client(), [item], now=5000) == 0
    assert manager._download_is_complete(item) is False


def test_manager_eventually_reconnects_zero_progress_aria2_download(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    manager = AnimeManager(config)

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconnect(self, torrent_hash: str) -> bool:
            self.calls.append(torrent_hash)
            return True

    client = Client()
    item = DownloadItem(
        torrent_hash="abc",
        name="Akiba",
        state="active",
        progress=0.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Akiba"),
        raw={
            "backend": "aria2",
            "downloaded": 0,
            "total_size": 1000,
            "download_speed": 0,
            "num_connections": 0,
        },
    )

    assert manager._recover_stalled_aria2_downloads(client, [item], now=1000) == 0
    assert manager._recover_stalled_aria2_downloads(client, [item], now=1901) == 1
    assert client.calls == ["abc"]


def test_aria2_exposes_verification_without_publishing_completion(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    gid = "0123456789abcdef"
    client._save_metadata(
        {
            gid: {
                "info_hash": release().info_hash,
                "title": release().title,
                "save_path": str(tmp_path / "downloads"),
                "media_id": 123,
            }
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_probe", lambda: True)

    def rpc(method: str, params=None):
        if method == "aria2.tellActive":
            return [
                {
                    "gid": gid,
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "1000",
                    "verifiedLength": "750",
                    "verifyIntegrityPending": "true",
                    "dir": str(tmp_path / "downloads"),
                    "infoHash": release().info_hash,
                    "files": [],
                }
            ]
        if method in {"aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)

    item = client.torrents()[0]

    assert item.state == "verifying"
    assert item.progress == 1.0
    assert item.completed_on == 0
    assert item.raw["verification_progress"] == 0.75
    client.close()


def test_aria2_repairs_only_metadata_only_magnet(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    gid = "0123456789abcdef"
    client._save_metadata(
        {
            gid: {
                "info_hash": release().info_hash,
                "title": release().title,
                "save_path": str(tmp_path / "downloads"),
                "media_id": 123,
            }
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: gid)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: b"d4:infode")

    def rpc(method: str, params=None):
        calls.append(method)
        if method == "aria2.tellStatus":
            return {
                "status": "active",
                "totalLength": "0",
                "completedLength": "0",
                "files": [],
            }
        if method in {"aria2.forceRemove", "aria2.removeDownloadResult"}:
            return "OK"
        if method == "aria2.addTorrent":
            return gid
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)

    assert client.repair_stalled_release(release().info_hash, release()) is True
    assert calls == [
        "aria2.tellStatus",
        "aria2.forceRemove",
        "aria2.removeDownloadResult",
        "aria2.addTorrent",
    ]
    metadata = client._load_metadata()[gid]
    assert metadata["media_id"] == 123
    assert metadata["listed_seeders"] == 20
    client.close()


def test_aria2_delete_removes_owned_metadata(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    gid = "0123456789abcdef"
    metadata = {
        gid: {
            "info_hash": release().info_hash,
            "title": release().title,
            "save_path": str(tmp_path),
            "added_on": 1,
        }
    }
    client._save_metadata(metadata)
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_probe", lambda: True)

    def rpc(method: str, params=None):
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        if method == "aria2.tellStatus":
            return {"status": "paused", "files": [], "dir": str(tmp_path)}
        if method in {"aria2.forceRemove", "aria2.removeDownloadResult", "aria2.saveSession"}:
            return "OK"
        raise AssertionError(method)

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.delete(release().info_hash, delete_files=False)
    assert client._load_metadata() == {}
    client.close()


ARIA2_SETTINGS = r"""
const out = {label: $('s_aria2_enabled').closest('label').innerText.trim(), expected: t('settings.useAria2'),
             checked: $('s_aria2_enabled').checked};
await __input('#s_aria2_seed_mode', 'ratio');
await __click('#testAria2');
await __tick(10);
out.toast = __toasts().at(-1);
await __input('#s_aria2_enabled', false);
out.testWhenOff = $('testAria2').disabled;
await __click('#saveSettings');
await __tick(10);
return out;
"""


def test_settings_switch_and_test_the_built_in_downloader(page) -> None:
    # The window offers the built-in downloader (aria2) and a check of it with the current form.
    for lang in ("en", "ru"):
        result = page.run(
            ARIA2_SETTINGS,
            lang=lang,
            state={"settings": {"aria2_enabled": True}},
            api={"test_aria2": {"version": "1.37.0", "url": "http://127.0.0.1:6800/jsonrpc"}},
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        out = result.value
        assert out["label"] == out["expected"] and out["checked"] is True
        [[form]] = result.calls_of("test_aria2")
        assert (form["aria2_enabled"], form["aria2_seed_mode"]) == (True, "ratio")  # the form as it is now
        assert out["toast"] == "aria2 1.37.0; http://127.0.0.1:6800/jsonrpc"
        assert out["testWhenOff"] is True
        assert result.calls_of("save_settings")[0][0]["aria2_enabled"] is False


def test_installer_installs_aria2_and_finds_aria2c(tmp_path: Path) -> None:
    # Homebrew installs the aria2 formula when it is missing; the app gets the path of its aria2c.
    completed, lines = run_installer(
        tmp_path / "missing", installed=tuple(name for name in FORMULAS if name != "aria2")
    )
    assert completed.returncode == 0, completed.stderr
    assert "brew install aria2" in [call for call, _ran, _path in brew_calls(lines)]
    assert lines[-1].split("|")[2] == str(
        tmp_path / "missing" / "installer" / "prefix" / "aria2" / "bin" / "aria2c"
    )
    # A broken formula without aria2c stops the install with a clear message.
    completed, lines = run_installer(tmp_path / "broken", aria2c=False)
    assert completed.returncode == 1
    assert "aria2 was installed but aria2c was not found" in completed.stderr
    assert not [line for line in lines if line.startswith("stop|")]
    # policy: the compiled launcher and the LaunchAgent get PUDGE_ARIA2C deep in the macOS-only
    # part of install.sh (venv, clang, launchctl); there is nothing to run here.
    assert source("install.sh").count("PUDGE_ARIA2C") >= 2


def test_aria2_client_uses_the_installed_binary(tmp_path: Path, monkeypatch) -> None:
    installed = tmp_path / "Cellar" / "aria2" / "bin" / "aria2c"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PUDGE_ARIA2C", str(installed))
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    try:
        assert client._binary_path() == str(installed)
    finally:
        client.close()


def test_aria2_hides_magnet_metadata_parent_and_keeps_payload(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    parent = "0123456789abcdef"
    child = "fedcba9876543210"
    client._save_metadata(
        {
            parent: {
                "info_hash": release().info_hash,
                "title": release().title,
                "save_path": str(tmp_path),
                "media_id": 123,
                "episode": 1,
                "is_batch": False,
                "added_on": 1,
                "release_score": 96.0,
            }
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_probe", lambda: True)

    def rpc(method: str, params=None):
        if method == "aria2.tellActive":
            return [
                {
                    "gid": parent,
                    "status": "complete",
                    "totalLength": "0",
                    "completedLength": "0",
                    "followedBy": [child],
                    "files": [],
                },
                {
                    "gid": child,
                    "status": "active",
                    "totalLength": "100",
                    "completedLength": "50",
                    "infoHash": release().info_hash,
                    "dir": str(tmp_path),
                    "files": [
                        {"path": str(tmp_path / "episode.mkv"), "length": "100", "completedLength": "50"}
                    ],
                    "bittorrent": {"info": {"name": "episode.mkv"}},
                },
            ]
        if method in {"aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        raise AssertionError(method)

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    items = client.torrents()
    assert len(items) == 1
    assert items[0].raw["gid"] == child
    assert items[0].media_id == 123
    assert client._resolve_gid(release().info_hash) == child
    client.close()


ONBOARDING = r"""
const next = async () => { await __click('#onboardingNext'); await __tick(20); };
await __input('#o_profile', PROFILE);
while (ui.onboardingStep < 4) await next();
const step4 = {text: [...document.querySelectorAll('#onboardingBody > p')].map(p => p.innerText.trim()),
               callout: __text('#onboardingBody .setup-callout')};
if (USE_QBT) await __input('#o_qbt_enabled', true);
await next();
return {step4, summary: __text('#onboardingBody .setup-callout'), torrent: t('uiB.onboarding.torrent'),
        qbtText: t('onboarding.qbtText'), aria2Notice: t('onboarding.aria2Notice')};
"""
FIRST_START = {
    "state": {"settings": {"onboarding_completed": False}},
    "api": {"first_experience_dependencies": {"mpv": {"installed": True}, "ffmpeg": {"installed": True}}},
}


def test_first_experience_explains_qbittorrent_is_optional(page) -> None:
    # Audit 68Б: the onboarding texts are catalog entries (their final wording:
    # downloads work through Pudge's built-in aria2 without a torrent client).
    assert en("onboarding.qbtText") == "Automatic downloads work through Pudge with no torrent-client setup."
    assert (
        ru("onboarding.qbtText")
        == "Автоматические загрузки работают через Pudge без настройки торрент-клиента."
    )
    assert (
        en("onboarding.aria2Notice")
        == "Downloads are ready. Pudge will start its built-in downloader when needed."
    )
    assert "встроенный загрузчик" in ru("onboarding.aria2Notice")
    assert en("uiB.onboarding.torrent") == "Torrent"
    # The downloads step says so; the last step names the downloader that was chosen.
    for lang, profile, use_qbt, downloader in (
        ("en", "full", False, "Pudge"),
        ("ru", "full", False, "Pudge"),
        ("en", "full", True, "qBittorrent"),
        ("en", "subtitles", False, "—"),
    ):
        code = ONBOARDING.replace("PROFILE", repr(profile)).replace("USE_QBT", "true" if use_qbt else "false")
        out = page.run(code, lang=lang, **FIRST_START).value
        assert out["step4"]["text"][0] == out["qbtText"]
        assert out["step4"]["callout"] == out["aria2Notice"]
        assert f"{out['torrent']}: {downloader}" in out["summary"]


def test_aria2_set_file_priority_uses_one_based_select_file(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(
        client,
        "files",
        lambda _hash: [
            {"index": 0, "priority": 1},
            {"index": 1, "priority": 1},
            {"index": 2, "priority": 1},
        ],
    )
    monkeypatch.setattr(client, "_resolve_gid", lambda _hash: "0123456789abcdef")

    def rpc(method: str, params=None):
        calls.append((method, list(params or [])))
        return "OK"

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.set_file_priority("hash", [0, 2], 0)

    assert calls == [("aria2.changeOption", ["0123456789abcdef", {"select-file": "2"}])]
    client.close()
