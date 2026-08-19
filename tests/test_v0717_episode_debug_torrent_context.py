from __future__ import annotations

from pathlib import Path

import httpx

from pudge.first_experience import mpv_study_status
from pudge.models import SubtitleCandidate
from pudge.providers.qbittorrent import QBittorrentClient
from pudge.syncing import _exact_jimaku_audio_clock_consensus


ROOT = Path(__file__).resolve().parents[1]


def test_ansatsu_exact_audio_consensus_selects_netflix_srt(tmp_path: Path) -> None:
    items = []
    for name, score, offset in (
        ("FFF.ass", 93.0, -0.04),
        ("Kamigami.ass", 82.6, 0.01),
        ("Netflix.ja.srt", 81.0, -0.08),
        ("TV.srt", 83.5, -44.32),
    ):
        path = tmp_path / name
        path.write_text("subtitle", encoding="utf-8")
        candidate = SubtitleCandidate(
            path,
            "jimaku",
            score,
            name,
            details={"episode_match": "exact", "entry_anilist_match": True},
        )
        items.append(
            (
                candidate,
                path,
                {"sync_was_successful": True, "offset_seconds": offset},
            )
        )

    selected, payload = _exact_jimaku_audio_clock_consensus(items)

    assert selected is not None and selected[0].name == "Netflix.ja.srt"
    assert payload["offsets_seconds"] == [-0.08, -0.04, 0.01]


def test_jpdb_detection_accepts_versioned_macos_layout(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plugin = (
        tmp_path
        / "Library"
        / "Application Support"
        / "mpv"
        / "scripts"
        / "jpdb-player"
    )
    plugin.mkdir(parents=True)
    script = plugin / "jpdb.lua"
    script.write_text("-- jpdb", encoding="utf-8")
    server = plugin / "jpdb-mpv-plugin-0.11.0"
    server.write_text("binary", encoding="utf-8")
    server.chmod(0o755)

    status = mpv_study_status(jpdb_api_token="token", selected_plugin="jpdb")

    assert status["jpdb_mpv"]["available"] is True
    assert status["jpdb_mpv"]["script_path"] == str(script)


def test_qbittorrent_stop_falls_back_to_legacy_pause() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404 if request.url.path.endswith("/stop") else 200)

    client = QBittorrentClient("http://qbt.local", api_key="test")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local", transport=httpx.MockTransport(handler)
    )
    try:
        client.pause("abc")
    finally:
        client.close()

    assert seen == ["/api/v2/torrents/stop", "/api/v2/torrents/pause"]


def test_episode_debug_translation_and_download_controls_are_exposed() -> None:
    debug = (ROOT / "pudge" / "web" / "debug.js").read_text(encoding="utf-8")
    lua = (ROOT / "pudge" / "mpv_scripts" / "pudge_anilist.lua").read_text(
        encoding="utf-8"
    )
    web = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")

    assert "pudgeDebugEpisodeSelect" in debug
    assert "study_subtitle_history" in lua and "secondary-sub-text" in lua
    assert "open_subtitle_study" not in lua
    assert "torrent-stop-all" not in web and "backend_id" in web
