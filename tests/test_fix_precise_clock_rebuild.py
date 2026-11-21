"""Saved pipeline results name their timeline version and language purity (sync benchmark).

Results timed on the old 0.25-1 s window grid, or saved before these fields existed, are
prepared again once instead of being handed out from the cache forever.
"""

from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig
from pudge.pipeline_cache import _manifest_path, load_final_pipeline_result, save_final_pipeline_result
from pudge.subtitles.timeline_alignment import _ALGORITHM_VERSION


def _saved(tmp_path: Path, **kwargs) -> tuple[AppConfig, Path]:
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"v")
    subtitle = tmp_path / "Show - 01.ja.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n台詞\n", encoding="utf-8")
    save_final_pipeline_result(
        video, config, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="external", **kwargs
    )
    return config, video


def test_a_current_timeline_result_is_reused(tmp_path: Path) -> None:
    config, video = _saved(
        tmp_path,
        alignment={"engine": "embedded-reference+timeline", "timeline_algorithm": _ALGORITHM_VERSION},
        candidate_details={"language_purity": "japanese_only"},
    )
    payload = load_final_pipeline_result(video, config)
    assert payload is not None and payload["timeline_algorithm"] == _ALGORITHM_VERSION
    assert payload["language_purity"] == "japanese_only"


def test_an_older_timeline_result_is_prepared_again(tmp_path: Path) -> None:
    config, video = _saved(
        tmp_path,
        alignment={"engine": "embedded-reference+timeline", "timeline_algorithm": "timeline-v6.23-x"},
    )
    assert load_final_pipeline_result(video, config) is None
    assert not _manifest_path(video, config).exists()


def test_a_result_saved_before_the_fields_existed_is_stale_once(tmp_path: Path) -> None:
    config, video = _saved(tmp_path, alignment={"engine": "alass"})
    manifest = _manifest_path(video, config)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop("timeline_algorithm")
    payload.pop("language_purity")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert load_final_pipeline_result(video, config) is None
    # the new run saves the fields: reused from then on, also for an ALASS result
    config, video = _saved(tmp_path, alignment={"engine": "alass"})
    assert load_final_pipeline_result(video, config) is not None
