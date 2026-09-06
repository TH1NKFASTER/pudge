from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pudge.web_app as web_app_module
from pudge.reading_audio_alignment import light_novel_position_for_audio
from pudge.web_app import WebAppApi


def test_anchor_window_activity_covers_dense_lookahead_path() -> None:
    alignment = {
        "duration": 100.0,
        "chapters": [
            {
                "chapter_index": 0,
                "start": 0.0,
                "end": 20.0,
                "normalized_length": 20,
                "punctuation_pause_count": 1,
                "activity_clock": True,
                "speech_regions": [
                    {"start": 1.0, "end": 1.2},
                    {"start": 1.4, "end": 1.6},
                    {"start": 1.8, "end": 2.0},
                ],
                "anchors": [
                    {"time": 1.0, "offset": 0},
                    {"time": 1.2, "offset": 1},
                    {"time": 1.4, "offset": 2},
                    {"time": 1.6, "offset": 3},
                    {"time": 1.8, "offset": 4},
                    {"time": 2.0, "offset": 5},
                ],
            }
        ],
    }
    state = light_novel_position_for_audio(alignment, 1.1)
    assert state is not None
    window = state["anchor_window"]
    assert float(window["right_time"]) == 1.2
    assert float(window["path"][-1]["time"]) >= 2.0
    # v187 only returned activity through right_time=1.2. v188 must carry VAD
    # for the whole lookahead path so the browser can cross short word anchors
    # without hold/jump artifacts between 250ms polls.
    assert any(float(row["start"]) >= 1.8 for row in window["activity"])


def test_manual_retry_recovers_history_preserves_selection_and_schedules_background(
    tmp_path: Path, monkeypatch
) -> None:
    video = (tmp_path / "ep9.mkv").resolve()
    video.write_bytes(b"video")
    subtitle = (tmp_path / "ep9-aligned.srt").resolve()
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nテスト\n", encoding="utf-8")

    item_missing = SimpleNamespace(
        media_id=200637,
        episode=9,
        state="waiting_subtitles",
        subtitle_path=None,
        embedded_subtitle_id=None,
        subtitle_origin="",
    )
    item_ready = SimpleNamespace(
        media_id=200637,
        episode=9,
        state="ready",
        subtitle_path=subtitle,
        embedded_subtitle_id=None,
        subtitle_origin="jimaku",
    )

    class DB:
        def __init__(self):
            self.item = item_missing
            self.clear_calls = 0
            self.jobs = []
            self.states = {}

        def episode_by_path(self, _video):
            return self.item

        def latest_selected_subtitle(self, _video):
            return {"score": 64.797, "source": "jimaku", "details": {"quality": {"score": 64.797}}}

        def set_state(self, key, value):
            self.states[key] = value

        def clear_subtitle_selection(self, _video):
            self.clear_calls += 1

        def queue_subtitle_job(self, *args, **kwargs):
            self.jobs.append((args, kwargs))

    db = DB()

    class Manager:
        def __init__(self):
            self.db = db

        def _recover_selected_text_subtitle_from_history(self, _video, **_kwargs):
            db.item = item_ready
            return subtitle

        def _subtitle_upgrade_state_key(self, _video):
            return "subtitle_upgrade:test"

        def process_subtitle_jobs(self, **_kwargs):
            return 1

    started = []
    api = WebAppApi.__new__(WebAppApi)
    api.manager = Manager()
    api.config = SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path))
    api.logger = SimpleNamespace(exception=lambda *args, **kwargs: None)
    api.task_supervisor = SimpleNamespace(start=lambda **kwargs: started.append(kwargs))
    api.get_state = lambda: {"ok": True}

    @contextmanager
    def busy_lock(*_args, **_kwargs):
        yield False

    monkeypatch.setattr(web_app_module, "maintenance_lock", busy_lock)
    result = api.retry_episode_preparation(str(video))

    assert result["ok"] is True
    assert db.clear_calls == 0
    assert db.jobs and db.jobs[-1][1]["error"] == "Manual retry"
    assert "subtitle_upgrade:test" in db.states
    assert '"manual_retry": true' in db.states["subtitle_upgrade:test"]
    assert started and "manual-subtitle-retry" in started[0]["name"]
