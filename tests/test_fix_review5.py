"""Review leftovers (review 5).

1. A rejected Settings save left the live config half-changed.
2. A saved API key behind the mask went to an address the page sent.
3. An unexpected error from the review gate's submit left the card on offer (a second grade).
4. The Settings "Test" of Irodori stopped the managed server under a running generation.
5. A re-import of a volume deleted the hard-link view an earlier import's audiobook used.
6. Watched parts from the phone and mpv at once: one device's part was lost.
7. An empty hash matched an aria2 task without one: delete("") removed another download.
8. downloads/check answered a malformed list or a huge id with a temporary 503.
9. A request on a closed release-search client escaped the search's error handling.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge import web_app
from pudge.config import AppConfig, load_config, write_config
from pudge.secrets_store import SECRET_MASK
from pudge.web_app import WebAppApi


def _config_path(tmp_path: Path) -> Path:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    write_config(cfg, cfg.config_path)
    return cfg.config_path


# ------------------------------------------------------------------ 1. settings


@pytest.mark.parametrize(
    "bad",
    [
        {"disk_limit_gb": "abc"},
        {"review_gate_count": ""},
        {"sidebar_review_interval": "7"},
        {"aria2_rpc_port": "x"},
    ],
)
def test_a_rejected_settings_save_leaves_the_live_config_unchanged(tmp_path, monkeypatch, bad) -> None:
    monkeypatch.setattr(web_app, "request_folder_access", lambda paths: {})
    api = WebAppApi(_config_path(tmp_path))
    try:
        before_language = api.config.ui.language
        with pytest.raises(ValueError):
            api.save_settings(
                {
                    "language": "ru" if before_language != "ru" else "en",
                    "subtitle_folders": str(tmp_path / "subs"),
                    "library_root": str(tmp_path / "elsewhere"),
                    **bad,
                }
            )
        assert api.config.ui.language == before_language
        assert api.config.paths.subtitle_dirs == []
        assert api.config.library.root_dir == tmp_path / "library"
        # The next save of one unrelated value does not persist the rejected half.
        api.save_settings({"notifications_enabled": False})
        saved = load_config(api.config_path)
        assert saved.ui.language == before_language
        assert saved.paths.subtitle_dirs == []
        assert saved.library.root_dir == tmp_path / "library"
    finally:
        api.close()


# ------------------------------------------------------------------ 2. masked secrets


def test_the_saved_llm_key_goes_only_to_the_saved_server(monkeypatch) -> None:
    api = object.__new__(WebAppApi)
    api.config = AppConfig()
    api.config.llm.base_url = "https://llm.example.test/v1"
    api.config.llm.api_key = "sk-saved"
    api.config.llm.model = ""
    sent: list[tuple[str, str]] = []

    def fake_list_models(url, key, **_kwargs):
        sent.append((url, key))
        return ["model-a"]

    monkeypatch.setattr(web_app, "list_models", fake_list_models)
    api.test_llm_provider(
        {"provider": "openai", "url": "https://other.example.test/v1", "api_key": SECRET_MASK}
    )
    api.test_llm_provider(
        {"provider": "openai", "url": "https://LLM.example.test/v1", "api_key": SECRET_MASK}
    )
    assert sent == [
        ("https://other.example.test/v1", ""),
        ("https://LLM.example.test/v1", "sk-saved"),
    ]


def _irodori_api(settings: SimpleNamespace) -> tuple[WebAppApi, list[tuple[str, str]], list[str]]:
    api = object.__new__(WebAppApi)
    api.light_novels = SimpleNamespace(settings=lambda: settings)
    health: list[tuple[str, str]] = []
    stopped: list[str] = []
    api._irodori_health = lambda url, key, timeout=5.0: (
        health.append((url, key)) or SimpleNamespace(json=lambda: {"status": "ok"})
    )
    api._stop_managed_irodori_server = lambda: stopped.append("stop")
    api._irodori_tts_lock = threading.RLock()
    api._irodori_tts_threads = {}
    return api, health, stopped


def test_the_saved_tts_key_goes_only_to_the_saved_server() -> None:
    settings = SimpleNamespace(irodori_tts_url="https://tts.example.test", irodori_tts_api_key="tts-saved")
    api, health, _stopped = _irodori_api(settings)
    api.test_irodori_tts({"url": "https://other.example.test", "api_key": SECRET_MASK})
    api.test_irodori_tts({"url": "https://other.example.test", "api_key": ""})
    api.test_irodori_tts({"url": "https://other.example.test", "api_key": "typed-key"})
    api.test_irodori_tts({"url": "https://tts.example.test", "api_key": SECRET_MASK})
    api.test_irodori_tts({})
    assert [key for _url, key in health] == ["", "", "typed-key", "tts-saved", "tts-saved"]


def test_the_external_audiobook_test_sends_the_saved_key_only_to_the_saved_server(monkeypatch) -> None:
    settings = SimpleNamespace(
        audiobook_generation_provider="external",
        irodori_tts_url="https://tts.example.test",
        irodori_tts_api_key="tts-saved",
        audiobook_tts_model="tts-1",
        irodori_tts_voice="alloy",
        irodori_tts_speed=1.0,
        irodori_tts_caption="",
    )
    api, _health, _stopped = _irodori_api(settings)
    sent: list[tuple[str, str]] = []

    def fake_get(url, headers=None, timeout=None):
        sent.append((url, str((headers or {}).get("Authorization", ""))))
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(web_app.httpx, "get", fake_get)
    api.test_audiobook_generation(
        {"provider": "external", "url": "https://other.example.test", "api_key": ""}
    )
    api.test_audiobook_generation({"provider": "external", "url": "https://tts.example.test"})
    assert sent == [
        ("https://other.example.test/v1/models", ""),
        ("https://tts.example.test/v1/models", "Bearer tts-saved"),
    ]


# ------------------------------------------------------------------ 3. review gate


def test_an_unexpected_submit_error_makes_the_card_an_unknown_outcome(tmp_path: Path) -> None:
    from tests.test_v227_r5_review_gate_cover_escape import FakeStudyService, _api

    class BrokenAfterPost(FakeStudyService):
        def strict_review_submit(self, word_id, reading_index, grade, *, attempt_id):
            self.submit_calls.append((word_id, reading_index, grade, attempt_id))
            raise KeyError("result")  # e.g. a 200 answer that did not parse

    api, video, service = _api(tmp_path, count=2, service=BrokenAfterPost())
    api.logger = SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)
    api.review_gate_begin(str(video))

    first = api.review_gate_review(str(video), 10, 0, "good", "attempt-1")
    assert first["ok"] is False and first["outcome"] == "unknown"
    again = api.review_gate_review(str(video), 10, 0, "good", "attempt-2")
    assert again["outcome"] == "unknown"
    assert len(service.submit_calls) == 1  # never graded a second time


# ------------------------------------------------------------------ 4. Irodori test vs generation


def test_the_irodori_test_keeps_the_server_a_generation_uses() -> None:
    settings = SimpleNamespace(irodori_tts_url="http://127.0.0.1:8088", irodori_tts_api_key="")
    api, _health, stopped = _irodori_api(settings)
    calls = {"n": 0}
    ok = SimpleNamespace(json=lambda: {})

    def health(url, key, timeout=5.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        return ok

    api._irodori_health = health
    api._start_managed_irodori_server = lambda url: True
    api._irodori_server_failure_detail = lambda: ""
    release = threading.Event()
    generation = threading.Thread(target=release.wait, daemon=True)
    generation.start()
    api._irodori_tts_threads = {7: generation}
    try:
        api.test_irodori_tts({}, start_timeout=5.0)
        assert stopped == []
    finally:
        release.set()
        generation.join(5)
    calls["n"] = 0
    api.test_irodori_tts({}, start_timeout=5.0)
    assert stopped == ["stop"]  # no generation: the test's own server is stopped


# ------------------------------------------------------------------ 5. volume import view


def test_a_reimport_keeps_the_view_an_earlier_audiobook_uses(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    tracks = []
    for name in ("disc1/01.mp3", "disc2/02.mp3", "other/03.mp3"):
        path = destination / "pack" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 16)
        tracks.append(path)
    imported: list[list[Path]] = []

    class FakeAudiobooks:
        def import_folder(self, folder, **_kwargs):
            imported.append(sorted(Path(folder).rglob("*.mp3")))
            return {"id": len(imported)}

    api = object.__new__(WebAppApi)
    api.audiobooks = FakeAudiobooks()
    book = {"title": "Volume", "volume": 1}
    api._import_recovered_light_novel_audiobook(book, destination, tracks[:2])
    api._import_recovered_light_novel_audiobook(book, destination, tracks[:2])
    assert len(imported) == 2 and imported[0] != imported[1]
    assert all(path.is_file() for path in imported[0])  # the first audiobook's files are still there


# ------------------------------------------------------------------ 6. watched parts


def test_watched_parts_sent_at_once_from_two_devices_are_both_kept(tmp_path: Path) -> None:
    from pudge import watch_coverage
    from pudge.database import Database

    db = Database(tmp_path / "library.sqlite3")
    video = str(tmp_path / "Show - 01.mkv")
    other_done = threading.Event()

    class SlowReader:
        """The phone's update pauses between its read and its write; mpv's arrives meanwhile."""

        def __init__(self) -> None:
            self.paused = False

        def __getattr__(self, name):
            return getattr(db, name)

        def get_state(self, key, default=""):
            value = db.get_state(key, default)
            if not self.paused:
                self.paused = True
                other = threading.Thread(
                    target=lambda: (
                        watch_coverage.record(db, video, [(600.0, 900.0)], duration=1400.0),
                        other_done.set(),
                    ),
                    daemon=True,
                )
                other.start()
                other_done.wait(0.5)
            return value

    watch_coverage.record(SlowReader(), video, [(0.0, 300.0)], duration=1400.0)
    assert other_done.wait(10)
    assert watch_coverage.load(db, video)["intervals"] == [(0.0, 300.0), (600.0, 900.0)]


# ------------------------------------------------------------------ 7. aria2 empty hash


def test_an_empty_hash_never_names_an_aria2_download() -> None:
    from pudge.providers.aria2 import Aria2Client

    client = object.__new__(Aria2Client)
    calls: list[tuple[str, list]] = []
    client._require_observable = lambda: None
    client._all_statuses = lambda: [{"gid": "1111222233334444", "infoHash": "", "status": "active"}]
    client._load_metadata = lambda: {"1111222233334444": {"info_hash": "", "save_path": "/tmp/x"}}
    client._rpc_raw = lambda method, params=None: calls.append((method, params)) or {}
    client._remove_result = lambda gid: calls.append(("remove_result", [gid]))

    assert client._resolve_gid("") == ""
    assert client._resolve_gid("   ") == ""
    client.delete("", delete_files=True)
    assert calls == []


# ------------------------------------------------------------------ 8. downloads/check


@pytest.mark.parametrize(
    "body",
    [
        {"items": {"kind": "ln", "id": 1}},
        {"items": "ln"},
        {"items": [{"kind": "ln", "id": 10**30}]},
        {"items": [{"kind": "manga", "id": float("inf")}]},
    ],
)
def test_a_malformed_downloads_check_is_a_final_client_error(body) -> None:
    from pudge.iphone_http import media_refusal
    from pudge.iphone_media import MediaError
    from pudge.iphone_reading import ReadingPackages

    reading = object.__new__(ReadingPackages)
    reading._watch_deletions = lambda: None
    reading.revisions = lambda kind, item: {"revision": "r"}
    with pytest.raises(MediaError) as caught:
        reading.check(body)
    assert media_refusal(caught.value)  # HTTP 422, not a 503 the phone sends again


def test_a_well_formed_downloads_check_still_answers() -> None:
    from pudge.iphone_reading import ReadingPackages

    reading = object.__new__(ReadingPackages)
    reading._watch_deletions = lambda: None
    reading.revisions = lambda kind, item: {"revision": f"{kind}-{item}"}
    result = reading.check({"items": [{"kind": "ln", "id": 3}, {"kind": "x", "id": 4}, "junk"]})
    assert result == {"items": [{"kind": "ln", "id": 3, "revision": "ln-3"}]}
    assert reading.check({}) == {"items": []}


# ------------------------------------------------------------------ 9. closed search client


def test_a_closed_search_client_is_a_search_error() -> None:
    from pudge.providers.nyaa import NyaaClient, NyaaError

    class Closed:
        def __getattr__(self, name):
            def call(*_args, **_kwargs):
                raise RuntimeError("Cannot send a request, as the client has been closed.")

            return call

    client = NyaaClient("https://search.example.test", proxy_mode="direct")
    client._client_for_proxy = lambda proxy: Closed()
    with pytest.raises(NyaaError):
        client._get("https://search.example.test/?q=x", None)
    with pytest.raises(NyaaError):
        client._get_bytes("https://search.example.test/x.torrent", None)
    client.close()
