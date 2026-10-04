from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pudge.config import LLMConfig, SyncConfig
from pudge.llm import OllamaClient
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import convert_to_plain_srt, parse_srt
from pudge.subtitles.verification import verification_failure


def _semantic_tracks(tmp_path: Path) -> tuple[Path, Path]:
    paths = tmp_path / "ja.srt", tmp_path / "en.srt"
    for path, prefix in zip(paths, ("日本語", "English")):
        path.write_text("\n".join(
            f"{i + 1}\n00:0{i}:05,000 --> 00:0{i}:07,000\n{prefix} {i}\n"
            for i in range(4)
        ), encoding="utf-8")
    return paths


@pytest.mark.parametrize("bad", [
    {"same_episode": "false"}, {"usable_for_timing": "true"},
    {"same_episode": None}, {"similarity": "NaN"},
    {"similarity": float("nan")}, {"similarity": float("inf")},
    {"similarity": -0.1}, {"similarity": 1.1}, {"similarity": True},
    {"matched_samples": "3"}, {"matched_samples": False},
    {"total_samples": 6}, {"sample_scores": [0.9, 0.9]},
    {"sample_scores": [0.9, "NaN", 0.9]},
    {"sample_scores": [0.9, float("inf"), 0.9]},
    {"sample_scores": [0.9, True, 0.9]},
    {"matched_samples": 0, "sample_scores": [0.1, 0.1, 0.1]},
    {"sample_scores": [0.6, 0.6, 0.6]},
    {"similarity": 10 ** 400},
])
def test_invalid_semantic_evidence_is_rejected_without_poisoning_cache(tmp_path, monkeypatch, bad):
    japanese, english = _semantic_tracks(tmp_path)
    client = OllamaClient(LLMConfig(enabled=True, embedded_reference_sample_count=3, embedded_reference_phrases_per_sample=2), tmp_path / "cache")
    valid = {"same_episode": True, "usable_for_timing": True, "similarity": 0.9,
             "matched_samples": 3, "total_samples": 3, "sample_scores": [0.9, 0.9, 0.9], "reason": "matching dialogue"}
    replies = iter([{**valid, **bad}, valid])
    monkeypatch.setattr(client, "_json_chat", lambda *_a: next(replies))
    try:
        result = client.compare_subtitle_semantics(japanese, english)
        assert result["accepted"] is False
        assert result["reason"] == "invalid_llm_response"
        assert list((tmp_path / "cache").glob("llm-semantic/*.json")) == []
        assert client.compare_subtitle_semantics(japanese, english)["accepted"] is True
    finally:
        client.close()


@pytest.mark.parametrize("message,status,want_calls", [
    ("response_format.type must be 'json_schema' or 'text'", 400, 2),
    ("response_format.type: Input should be 'text' or 'json_schema'", 422, 2),
    ("response_format json_object: invalid API key", 401, 1),
    ("response_format json_object: upstream timed out", 500, 1),
    ("messages: input should be a valid list", 422, 1),
])
def test_json_chat_retries_only_unsupported_response_format(tmp_path, message, status, want_calls):
    import pudge.llm as module

    module._MODEL_ADAPTATIONS.clear()
    payloads = []
    def respond(request):
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(status, json={"error": {"message": message}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    client = OllamaClient(LLMConfig(enabled=True, provider="openai", base_url="http://local-model.invalid", model="local"))
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        result = client.json_chat("Return JSON", "hello")
    finally:
        client.close()
        module._MODEL_ADAPTATIONS.clear()
    assert len(payloads) == want_calls
    if want_calls == 2:
        assert result == {"ok": True}
        assert "response_format" not in payloads[1]
        assert payloads[1]["reasoning_effort"] == "low"


def test_semantic_cache_revalidates_positive_sample_evidence(tmp_path, monkeypatch):
    japanese, english = _semantic_tracks(tmp_path)
    client = OllamaClient(LLMConfig(enabled=True, embedded_reference_sample_count=3, embedded_reference_phrases_per_sample=2), tmp_path / "cache")
    valid = {"same_episode": True, "usable_for_timing": True, "similarity": 0.9,
             "matched_samples": 3, "total_samples": 3, "sample_scores": [0.9, 0.9, 0.9], "reason": "matching dialogue"}
    monkeypatch.setattr(client, "_json_chat", lambda *_a: valid)
    try:
        assert client.compare_subtitle_semantics(japanese, english)["accepted"] is True
        [cache] = (tmp_path / "cache" / "llm-semantic").glob("*.json")
        payload = json.loads(cache.read_text())
        payload["result"]["sample_scores"] = [0.6, 0.6, 0.6]
        cache.write_text(json.dumps(payload))
        result = client.compare_subtitle_semantics(japanese, english)
        assert result["cached"] is False
        assert result["sample_scores"] == [0.9, 0.9, 0.9]
    finally:
        client.close()


def test_gateway_adaptations_do_not_iterate_a_live_mutating_set():
    import pudge.llm as module

    class UpdatingSet(set):
        def __iter__(self):
            iterator = super().__iter__()
            yield next(iterator)
            self.add("temperature")  # A second request learns a new rejection.
            yield from iterator
    client = OllamaClient(LLMConfig(enabled=True, provider="openai", base_url="http://race.invalid", model="local"))
    memory_key = client.base_url, client.model
    module._MODEL_ADAPTATIONS[memory_key] = {"drop": UpdatingSet({"response_format", "prompt_cache_key"}), "effort": ""}
    payloads = []
    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert client.json_chat("Return JSON", "hello") == {"ok": True}
        assert "response_format" not in payloads[0]
    finally:
        client.close()
        module._MODEL_ADAPTATIONS.pop(memory_key, None)


def test_ass_shadow_layers_do_not_duplicate_positioned_text(tmp_path):
    source = tmp_path / "layers.ass"
    source.write_text(
        "[Script Info]\n[V4+ Styles]\nFormat: Name,Fontname,Fontsize,Alignment\nStyle: Default,Arial,40,7\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        r"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\pos(100,100)\blur3}生徒会室" "\n"
        r"Dialogue: 1,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\pos(100,100)}生徒会室" "\n"
        r"Dialogue: 1,0:00:04.00,0:00:06.00,Default,,0,0,0,,{\pos(100,100)}はい" "\n"
        r"Dialogue: 1,0:00:04.00,0:00:06.00,Default,,0,0,0,,{\pos(300,100)}はい" "\n",
        encoding="utf-8",
    )
    output, _result = convert_to_plain_srt(source, tmp_path / "cache", ffmpeg_path="/missing/ffmpeg")
    assert [text for _start, _end, text in parse_srt(output)] == ["生徒会室", "はいはい"]


def test_force_resync_analyzes_audio_once_across_guard_rejections(tmp_path, monkeypatch):
    import pudge.syncing as syncing

    first = SubtitleCandidate(path=tmp_path / "bad.srt", name="bad", source="jimaku", score=90)
    second = SubtitleCandidate(path=tmp_path / "good.srt", name="good", source="jimaku", score=90)
    for candidate in (first, second):
        candidate.path.write_text("1\n00:00:01,000 --> 00:00:03,000\n日本語\n")
    forces = []
    def choose(video, candidates, cache, config, **kwargs):
        forces.append(kwargs.get("force", False))
        candidate = list(candidates)[0]
        return candidate, candidate.path, {"sync_was_successful": True, "reference_alignment_reliable": True}
    def guard(source, path, result, cache):
        if source == first.path:
            result["alignment_output_guard"] = {"status": "rejected", "reason": "scattered_time_map"}
    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded", choose)
    monkeypatch.setattr(syncing, "_guard_alignment_result", guard)
    monkeypatch.setattr(syncing, "_recover_rejected_clock", lambda v, s, p, r, c, cfg, **k: (p, r))
    selected, path, result = syncing.optimize_candidates(tmp_path / "video.mkv", [first, second], tmp_path / "cache", SyncConfig(), force=True)
    assert selected is second and path == second.path
    assert len(result["guard_rejected_candidates"]) == 1
    assert forces == [True, False]


@pytest.mark.parametrize("reason,retryable", [("stt_unavailable", False), ("stt_disabled", False), ("stt_timeout", True), ("stt_worker_failed", True)])
def test_verification_distinguishes_permanent_and_transient_failures(reason, retryable):
    failure = verification_failure({"reason": "stt_reference_unavailable", "stt": {"reason": reason, "error": "worker detail"}})
    assert failure == {"reason": reason, "retryable": retryable, "error": "worker detail"}


@pytest.mark.parametrize("returncode,want_reason", [(3, "stt_unavailable"), (1, "stt_worker_failed")])
def test_stt_worker_failure_is_retryable_unless_dependency_is_missing(tmp_path, monkeypatch, returncode, want_reason):
    import subprocess
    from pudge.subtitles.stt import prepare_japanese_stt_reference

    video = tmp_path / "video.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr("pudge.subtitles.stt.subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, "", "worker detail"))
    reference, result = prepare_japanese_stt_reference(video, tmp_path / "cache", ffmpeg_path="ffmpeg", model="local", timeout_seconds=60)
    assert reference is None
    assert result["reason"] == want_reason


@pytest.mark.parametrize("worker_ready", [False, True])
def test_requeue_preserves_ready_subtitles_when_speech_worker_is_unavailable(tmp_path, monkeypatch, worker_ready):
    import subprocess
    from types import SimpleNamespace
    from pudge.manager import AnimeManager

    video, subtitle = tmp_path / "episode.mkv", tmp_path / "prepared.srt"
    video.write_bytes(b"video")
    subtitle.write_text("ready subtitles")
    class DB:
        def __init__(self):
            self.state = {}
            self.invalidated = []
        def episodes(self):
            return [SimpleNamespace(video_path=video, subtitle_path=subtitle, media_id=1, episode=1)]
        def latest_selected_subtitle(self, _video):
            return {"id": 10, "details": {"alignment": {"unverified_timeline_edit": True}}}
        def get_state(self, key, default=""):
            return self.state.get(key, default)
        def set_state(self, key, value):
            self.state[key] = value
        def invalidate_subtitle(self, *args):
            self.invalidated.append(args)
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = DB()
    manager.config = SimpleNamespace(sync=SyncConfig())
    monkeypatch.setattr("pudge.subtitles.stt.subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0 if worker_ready else 3, "", "mlx-whisper absent"))
    monkeypatch.setattr("pudge.manager.invalidate_final_pipeline_result", lambda *_a: None)
    assert manager._requeue_unverified_timeline_edits() == int(worker_ready)
    assert bool(manager.db.invalidated) is worker_ready
    assert subtitle.read_text() == "ready subtitles"


def test_transient_speech_verification_stops_after_three_jobs(tmp_path, monkeypatch):
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(config, log=lambda *_a: None)
    video = tmp_path / "Anime - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(LibraryEpisode(media_id=1, title="Anime", episode=1, video_path=video.resolve(), state="waiting_subtitles"))
    manager.db.queue_subtitle_job(video.resolve(), 1, 1)
    class TimeoutProcess:
        returncode = 4
        def __init__(self, *_a, **_k):
            pass
        def poll(self):
            return 4
        def communicate(self):
            return 'PREPARED_SUBTITLE_META={"alignment":{"stt":{"reason":"stt_timeout"}}}\nPREPARE_STATUS=waiting_verification\n', ""
    monkeypatch.setattr("pudge.manager.subprocess.Popen", TimeoutProcess)
    for want_state in ("pending", "pending", "needs_action"):
        assert manager.process_subtitle_jobs(limit=1) == 0
        job = manager.db.subtitle_jobs()[0]
        assert job["state"] == want_state
        if want_state == "pending":
            with manager.db.connect() as conn:
                conn.execute("UPDATE subtitle_jobs SET next_check=0")
    assert manager.db.subtitle_jobs()[0]["attempts"] == 3


def test_missing_speech_verification_can_offer_identified_raw_source(tmp_path, monkeypatch, capsys):
    from pudge.cli import build_parser, process_video
    from pudge.config import AppConfig

    video, source = tmp_path / "Anime - 01.mkv", tmp_path / "Anime - 01.srt"
    video.write_bytes(b"video")
    source.write_text("1\n00:00:01,000 --> 00:00:03,000\n字幕です\n")
    candidate = SubtitleCandidate(path=source, name=source.name, source="local", score=95, episode=1, verified_japanese=True)
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.paths.subtitle_dirs = []
    config.anilist.enabled = False
    config.matching.local_min_score = 0
    config.matching.evaluate_all_jimaku = True
    failure = {"sync_was_successful": False, "unverified_timeline_edit": True,
               "reason": "timeline_edit_requires_audio_verification", "speech_verification": {"stt": {"reason": "stt_unavailable"}}}
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *a, **k: [])
    monkeypatch.setattr("pudge.cli.find_local_subtitles", lambda **k: [candidate])
    monkeypatch.setattr("pudge.cli.optimize_candidates", lambda *a, **k: (None, None, {**failure, "guard_rejected_candidates": [{"path": str(source), "raw_fallback_disqualified": False}]}))
    monkeypatch.setattr("pudge.cli._sync_one", lambda *a, **k: (source, failure))
    args = build_parser().parse_args(["--prepare-only", "--offline", str(video)])
    assert process_video(video, args, config, None) == 4
    stdout = capsys.readouterr().out
    assert f"PREPARED_RAW_SUBTITLE={source}" in stdout
    assert "PREPARE_STATUS=couldnt_sync" in stdout


def test_absent_speech_worker_does_not_disqualify_raw_identity():
    from pudge.syncing import raw_fallback_disqualified

    result = {"unverified_timeline_edit": True, "speech_verification": {"reason": "stt_reference_unavailable", "stt": {"reason": "stt_unavailable"}}}
    assert raw_fallback_disqualified(result) is False
    result["alignment_output_guard"] = {"status": "rejected", "reason": "scattered_time_map"}
    assert raw_fallback_disqualified(result) is True
