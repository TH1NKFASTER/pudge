from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.reading_audio_alignment import light_novel_position_for_audio
from pudge.web_app import WebAppApi, _plain_anilist_description


def _audiobook_service(tmp_path: Path) -> tuple[AudiobookService, int]:
    database = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(
        database,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    book = service._upsert(
        path=tmp_path / "book.m4b",
        title="Book",
        duration=120.0,
        files=[
            {
                "index": 0,
                "path": str(tmp_path / "book.m4b"),
                "title": "Book",
                "duration": 120.0,
                "start": 0.0,
                "end": 120.0,
            }
        ],
        chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": 120.0}],
    )
    return service, int(book["id"])


def test_audio_position_exposes_exact_interpolation_window() -> None:
    alignment = {
        "chapters": [
            {
                "chapter_index": 2,
                "normalized_length": 100,
                "start": 10.0,
                "end": 30.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 40, "time": 18.0},
                    {"offset": 100, "time": 30.0},
                ],
            }
        ]
    }
    result = light_novel_position_for_audio(alignment, 14.0)
    assert result is not None
    assert result["chapter_char_offset_exact"] == 20.0
    assert result["anchor_window"] == {
        "left_time": 10.0,
        "left_offset": 0.0,
        "right_time": 18.0,
        "right_offset": 40.0,
    }


def test_live_audiobook_seek_and_slowdown_use_existing_mpv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, book_id = _audiobook_service(tmp_path)
    process = SimpleNamespace(poll=lambda: None)
    ipc_path = tmp_path / "book.sock"
    service._players[book_id] = process
    service._ipc_paths[book_id] = ipc_path
    live = {"speed": 3.0, "time-pos": 12.0, "playlist-pos": 0}
    commands: list[list[object]] = []

    def command(_path: Path, payload: list[object]) -> dict[str, str]:
        commands.append(payload)
        if payload[:2] == ["set_property", "speed"]:
            live["speed"] = float(payload[2])
        if payload[:1] == ["seek"]:
            live["time-pos"] = float(payload[1])
        return {"error": "success"}

    monkeypatch.setattr(service, "_ipc_command", command)
    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: live.get(name))
    monkeypatch.setattr(
        service,
        "play",
        lambda *_args, **_kwargs: pytest.fail("live same-file controls must not restart mpv"),
    )

    service.seek_to(book_id, 54.0)
    service.set_speed(book_id, 1.0)

    assert ["seek", 54.0, "absolute", "exact"] in commands
    assert ["set_property", "speed", 1.0] in commands
    assert service.book(book_id)["position"] == 54.0
    assert service.book(book_id)["speed"] == 1.0


def test_planning_episode_uses_full_ranked_search_after_local_precheck() -> None:
    calls: list[dict[str, object]] = []
    anime = SimpleNamespace(
        media_status="RELEASING",
        released_episodes=4,
        episodes=12,
        next_airing_episode=5,
    )
    manager = SimpleNamespace(
        db=SimpleNamespace(get_anime=lambda _media_id: anime),
        search_and_add_best=lambda media_id, **kwargs: calls.append(
            {"media_id": media_id, **kwargs}
        )
        or None,
    )
    api = object.__new__(WebAppApi)
    api.manager = manager
    api._local_episode_for_relative = lambda *_args: None

    result = api.add_best_planning_episode(99, 3)

    assert result == {
        "ok": False,
        "episode": 3,
        "local": False,
        "release": None,
    }
    assert calls == [
        {"media_id": 99, "episode": 3, "batch": False, "automatic": False}
    ]


def test_planning_description_is_plain_text() -> None:
    value = "Story<br><br>1. <b>First</b> &amp; second<br/>2. <i>Next</i>"
    assert _plain_anilist_description(value) == "Story\n1. First & second\n2. Next"


def test_frontend_contracts_for_listen_together_and_planning_fallback() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    reading = (root / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    media = (root / "pudge/web/media.js").read_text(encoding="utf-8")

    assert 'data-ln-audio-seek="15"' in html
    assert 'data-ln-audio-seek="30"' not in html
    assert "ArrowLeft:-5,ArrowRight:5,ArrowUp:15,ArrowDown:-15" in html
    assert "await showLnPopup(target.dataset.lnToken,target)" in html
    assert "Play from here" in html
    assert "startLnPairedInterpolation" in html
    assert "lnPairedPollGeneration" in html
    assert "data-pudge-study-extra-action" in reading
    assert "planning-episodes-auto" in html
    assert "start_planning_episode_download" in html
    assert "Click the card to open AniList" not in html
    assert "Нажмите карточку, чтобы открыть AniList" not in html
    assert "saveAudioSpeed" not in media
    assert "await pywebview.api.audiobook_set_speed(id,speed)" in media
