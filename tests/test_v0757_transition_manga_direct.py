from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.database import Database

ROOT = Path(__file__).parents[1]


def test_audiobook_pause_uses_explicit_mpv_property(tmp_path: Path, monkeypatch) -> None:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    process = SimpleNamespace(poll=lambda: None)
    socket_path = tmp_path / "mpv.sock"
    service._players[7] = process
    service._ipc_paths[7] = socket_path

    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: False if name == "pause" else None)
    assert service.is_paused(7) is False

    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: True if name == "pause" else None)
    assert service.is_paused(7) is True


def test_live_player_never_falls_back_to_reader_progress() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")

    marker = "if(state.alignment?.ready&&state.player_running&&state.audiobook_id){"
    assert marker in html
    assert "if(appliedDesired!==wanted){" in html
    running_branch = html.split(marker, 1)[1].split("}else{", 1)[0]
    assert "light_novel_play_paired(" not in running_branch
    assert "lnReaderAudioProgress()" not in running_branch
    assert "ui.lnPairedTransportPromise" in html


def test_paired_state_distinguishes_pause_from_transition() -> None:
    source = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")

    assert "paused = self.is_paused(audiobook_id) if player_running else False" in source
    assert "playing = player_running and not paused" in source
    assert '"playback_active": playback_active' in source
    assert '"paused": player_running and not playing' not in source


def test_manga_regions_follow_top_down_then_right_to_left_order() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "function mangaRegionReadingOrder(regions)" in js
    assert "if (Math.abs(leftTop - rightTop) > .045) return leftTop - rightTop;" in js
    assert "return rightEdge - leftEdge;" in js
    assert "const regions = mangaRegionReadingOrder(" in js


def test_manga_text_is_directly_actionable_without_region_activation() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    reading_tools = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")

    assert "activateTextRegion" not in js
    assert "deactivateTextRegion" not in js
    assert "closeRegionsOutsidePointer" not in js
    assert "manga-v2-text-region.active" not in css
    assert "manga-v2-text-region:hover" not in css
    assert "data-pudge-study-token" in reading_tools
    assert "await openStudyCard" in reading_tools
    assert "pointer-events:none" in css
    assert "user-select:none" in css
