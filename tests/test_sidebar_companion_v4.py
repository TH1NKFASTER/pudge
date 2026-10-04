from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading
import subprocess

import pytest

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, load_config, write_config
from pudge.web_app import WebAppApi


def test_sidebar_interval_survives_config_roundtrip_and_missing_keys(tmp_path: Path) -> None:
    config = AppConfig()
    config.ui.sidebar_review_interval = "continuous"
    path = tmp_path / "config.toml"
    write_config(config, path)
    assert load_config(path).ui.sidebar_review_interval == "continuous"
    path.write_text('[ui]\nlanguage="ru"\n')
    assert load_config(path).ui.sidebar_review_interval == ""  # migrate the old window preference


def test_sidebar_preference_endpoint_saves_without_window_storage(tmp_path: Path) -> None:
    api = object.__new__(WebAppApi)
    api.config = AppConfig()
    api.config_path = tmp_path / "config.toml"
    assert api.sidebar_review_settings("continuous") == {"interval": "continuous"}
    assert load_config(api.config_path).ui.sidebar_review_interval == "continuous"
    restarted = object.__new__(WebAppApi)
    restarted.config = load_config(api.config_path)
    assert restarted.sidebar_review_settings() == {"interval": "continuous"}
    with pytest.raises(ValueError):
        api.sidebar_review_settings("bogus")
    assert load_config(api.config_path).ui.sidebar_review_interval == "continuous"


def test_focused_audio_state_avoids_inactive_books_and_transcripts() -> None:
    service = object.__new__(AudiobookService)
    service._lock = threading.RLock()
    service._players = {1: SimpleNamespace(poll=lambda: None), 2: SimpleNamespace(poll=lambda: 0)}
    calls = []

    def book(book_id, *, include_transcription):
        calls.append((book_id, include_transcription))
        return {"id": book_id, "playing": True}

    service.book = book
    assert service.sidebar_state() == {"books": [{"id": 1, "playing": True}]}
    assert calls == [(1, False)]


def test_reader_warmup_only_opens_metadata() -> None:
    api = object.__new__(WebAppApi)
    calls = []
    api.light_novels = SimpleNamespace(open_book=lambda book_id: calls.append(book_id) or {"id": book_id})
    assert api.sidebar_reader_book(7) == {"id": 7}
    assert calls == [7]


def test_interpolation_and_reader_handoff_behaviors() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", str(root / "tests/js/paired_audio_clock.cjs"), str(root / "pudge/web")],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "paired clock and reader handoff: PASS" in result.stdout
