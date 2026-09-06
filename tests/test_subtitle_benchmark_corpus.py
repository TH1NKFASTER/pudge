from __future__ import annotations

import json

from pudge.subtitles.benchmark_corpus import select_benchmark_audio_stream


def test_benchmark_audio_prefers_explicit_japanese_track() -> None:
    probe = {
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "tags": {"language": "eng", "title": "English"},
                "disposition": {"default": 1},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "tags": {"language": "jpn", "title": "Japanese"},
                "disposition": {"default": 0},
            },
        ]
    }
    selected = select_benchmark_audio_stream(probe)
    assert selected is not None
    assert selected["index"] == 2


def test_benchmark_audio_falls_back_to_default_track() -> None:
    probe = {
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "channels": 2,
                "tags": {},
                "disposition": {"default": 0},
            },
            {
                "index": 1,
                "codec_type": "audio",
                "channels": 2,
                "tags": {},
                "disposition": {"default": 1},
            },
        ]
    }
    selected = select_benchmark_audio_stream(probe)
    assert selected is not None
    assert selected["index"] == 1


def test_benchmark_audio_returns_none_without_audio() -> None:
    assert select_benchmark_audio_stream({"streams": [{"index": 4, "codec_type": "subtitle"}]}) is None


def test_bitmap_candidate_uses_existing_ocr_pipeline(monkeypatch, tmp_path) -> None:
    from pudge.config import AppConfig
    from pudge.models import SubtitleCandidate
    from pudge.subtitles import benchmark_corpus

    source = tmp_path / "candidate.sup"
    source.write_bytes(b"fake-pgs")
    converted = tmp_path / "candidate.srt"
    converted.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    media = tmp_path / "proxy.mkv"
    media.write_bytes(b"proxy")
    seen = {}

    def fake_ocr(video, cache_dir, **kwargs):
        seen["video"] = video
        seen["cache_dir"] = cache_dir
        seen.update(kwargs)
        return converted, {"reason": "accepted", "quality": {"accepted": True}}

    monkeypatch.setattr(benchmark_corpus, "image_subtitle_to_srt", fake_ocr)
    candidate = SubtitleCandidate(
        path=source,
        source="jimaku",
        score=1.0,
        name="candidate.sup",
        episode=1,
        verified_japanese=True,
    )
    path, metadata = benchmark_corpus._candidate_plain_srt(
        candidate,
        tmp_path / "cache",
        AppConfig(),
        media=media,
    )

    assert path == converted
    assert metadata["reason"] == "bitmap_ocr"
    assert seen["video"] == media
    assert seen["subtitle_path"] == source


def test_bitmap_ocr_failure_is_recorded_without_aborting_case(monkeypatch, tmp_path) -> None:
    from pudge.config import AppConfig
    from pudge.models import SubtitleCandidate
    from pudge.subtitles import benchmark_corpus

    source = tmp_path / "candidate.sup"
    source.write_bytes(b"fake-pgs")
    media = tmp_path / "proxy.mkv"
    media.write_bytes(b"proxy")

    def fake_ocr(*_args, **_kwargs):
        raise RuntimeError("Vision unavailable")

    monkeypatch.setattr(benchmark_corpus, "image_subtitle_to_srt", fake_ocr)
    candidate = SubtitleCandidate(source, "jimaku", 1.0, "candidate.sup")
    path, metadata = benchmark_corpus._candidate_plain_srt(
        candidate,
        tmp_path / "cache",
        AppConfig(),
        media=media,
    )

    assert path is None
    assert metadata["reason"] == "bitmap_ocr_failed"
    assert "Vision unavailable" in metadata["error"]


def test_create_diagnostic_case_preserves_forensic_metadata(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace
    from pudge.subtitles import benchmark_corpus

    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")

    probe = {
        "streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn"}},
            {"index": 1, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "eng"}},
        ]
    }
    embedded = [
        {
            "stream_index": 1,
            "codec": "ass",
            "language": "eng",
            "title": "English",
            "path": str(tmp_path / "eng.srt"),
            "bytes": 123,
        }
    ]
    monkeypatch.setattr(
        benchmark_corpus,
        "_extract_embedded_text_tracks",
        lambda *_args, **_kwargs: (probe, embedded),
    )

    def fake_compact(_video, destination, _cache, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"compact")
        return {"path": str(destination), "compact_bytes": 7, "timing_reference_included": True}

    monkeypatch.setattr(benchmark_corpus, "build_compact_benchmark_media", fake_compact)
    config = SimpleNamespace(
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe", alass="alass"),
        paths=SimpleNamespace(cache_dir=tmp_path / "global-cache"),
        jimaku=SimpleNamespace(base_url="https://example.invalid", api_key=""),
        matching=SimpleNamespace(
            prefer_srt=True,
            srt_alignment_tolerance_ratio=0.1,
            srt_alignment_tolerance_absolute=1,
        ),
        sync=SimpleNamespace(),
    )

    case_dir = benchmark_corpus.create_diagnostic_case_from_video(
        video=video,
        corpus_dir=tmp_path / "corpus",
        config=config,
        media_id=7,
        title="Show",
        episode=1,
        reason="no_embedded_japanese_text_subtitle",
        source_release={"info_hash": "abc", "title": "[Group] Show - 01 [JPN?]"},
        fetch_jimaku=False,
        run_pudge=False,
    )

    payload = json.loads((case_dir / "diagnostic.json").read_text(encoding="utf-8"))
    assert payload["tier"] == "diagnostic"
    assert payload["reason"] == "no_embedded_japanese_text_subtitle"
    assert payload["source_release"]["info_hash"] == "abc"
    assert payload["embedded_text_tracks"][0]["language"] == "eng"
    assert (case_dir / "ffprobe.json").is_file()
    assert payload["compact_media"]["compact_bytes"] == 7


def test_collect_diagnostic_reports_ignores_incomplete_directories(tmp_path) -> None:
    from pudge.subtitles import benchmark_corpus

    root = tmp_path / "corpus" / "diagnostics"
    complete = root / "complete"
    incomplete = root / "incomplete"
    complete.mkdir(parents=True)
    incomplete.mkdir(parents=True)
    (complete / "diagnostic.json").write_text(
        json.dumps({"schema": benchmark_corpus.DIAGNOSTIC_CASE_SCHEMA, "reason": "no_jp"}),
        encoding="utf-8",
    )
    (incomplete / "junk.txt").write_text("partial", encoding="utf-8")

    rows = benchmark_corpus.collect_diagnostic_reports(tmp_path / "corpus")
    assert rows == [{"schema": benchmark_corpus.DIAGNOSTIC_CASE_SCHEMA, "reason": "no_jp"}]



def test_create_case_no_oracle_removes_empty_case_directory(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace
    from pudge.subtitles import benchmark_corpus

    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        benchmark_corpus,
        "extract_embedded_japanese_oracle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            benchmark_corpus.SubtitleBenchmarkError("video has no embedded Japanese text subtitle")
        ),
    )
    config = SimpleNamespace(
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe", alass="alass"),
    )
    corpus = tmp_path / "corpus"
    try:
        benchmark_corpus.create_case_from_video(
            video=video,
            corpus_dir=corpus,
            config=config,
            media_id=7,
            title="Show",
            episode=1,
            fetch_jimaku=False,
            run_pudge=False,
        )
    except benchmark_corpus.SubtitleBenchmarkError:
        pass
    else:
        raise AssertionError("expected no-oracle failure")
    cases_root = corpus / "cases"
    assert not cases_root.exists() or list(cases_root.iterdir()) == []


def test_v106_replay_stored_gold_forces_current_pipeline(monkeypatch, tmp_path) -> None:
    import json
    from pathlib import Path
    from types import SimpleNamespace
    import pudge.subtitles.benchmark_corpus as corpus_mod

    case_dir = tmp_path / "cases" / "case"
    case_dir.mkdir(parents=True)
    media = case_dir / "compact.mkv"
    candidate = case_dir / "candidate.srt"
    oracle = case_dir / "oracle.srt"
    media.write_bytes(b"media")
    candidate.write_text("1\n00:00:00,000 --> 00:00:01,000\nあ\n")
    oracle.write_text("1\n00:00:00,000 --> 00:00:01,000\nあ\n")
    report = case_dir / "case.json"
    report.write_text(
        json.dumps(
            {
                "schema": corpus_mod.CORPUS_CASE_SCHEMA,
                "media_id": 1,
                "episode": 1,
                "compact_media": {"path": str(media)},
                "candidates": [
                    {
                        "path": str(candidate),
                        "source": "jimaku",
                        "score": 100,
                        "name": candidate.name,
                        "episode": 1,
                        "verified_japanese": True,
                        "details": {},
                        "oracle": {"same_episode": True},
                    }
                ],
            }
        )
    )
    calls = []
    def fake_run(**kwargs):
        calls.append(kwargs)
        return {
            "selected": {"path": str(candidate), "name": candidate.name},
            "accepted": True,
            "oracle": {"same_episode": True, "good_alignment": True},
        }
    monkeypatch.setattr(corpus_mod, "run_current_pudge_benchmark", fake_run)
    result = corpus_mod.replay_stored_benchmark_case(
        report,
        config=SimpleNamespace(),
        force=True,
    )
    assert result["status"] == "replayed"
    assert result["accepted"] is True
    assert calls and calls[0]["force"] is True
    updated = json.loads(report.read_text())
    assert updated["replay_generation"] == "v109-source-basename-preserved"
    assert updated["current_pudge"]["alignment_failure"] is False


def test_v106_replay_stored_no_candidates_does_not_touch_network(monkeypatch, tmp_path) -> None:
    import json
    from types import SimpleNamespace
    import pudge.subtitles.benchmark_corpus as corpus_mod

    case_dir = tmp_path / "diagnostics" / "case"
    case_dir.mkdir(parents=True)
    media = case_dir / "compact.mkv"
    media.write_bytes(b"media")
    report = case_dir / "diagnostic.json"
    report.write_text(
        json.dumps(
            {
                "schema": corpus_mod.DIAGNOSTIC_CASE_SCHEMA,
                "media_id": 1,
                "episode": 1,
                "compact_media": {"path": str(media)},
                "candidates": [],
            }
        )
    )
    monkeypatch.setattr(
        corpus_mod,
        "optimize_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    result = corpus_mod.replay_stored_benchmark_case(report, config=SimpleNamespace(), force=True)
    assert result["status"] == "no_candidates"
    assert result["candidate_count"] == 0



def test_v109_replay_preserves_source_release_basename_for_production(monkeypatch, tmp_path) -> None:
    import json
    from types import SimpleNamespace
    import pudge.subtitles.benchmark_corpus as corpus_mod

    case_dir = tmp_path / "diagnostics" / "fma"
    case_dir.mkdir(parents=True)
    compact = case_dir / "compact-063c8111.mkv"
    compact.write_bytes(b"compact-media")
    candidate = case_dir / "[Erai-raws] Fullmetal Alchemist - Brotherhood - 44 [3210D25C].ja.srt"
    candidate.write_text("1\n00:00:00,000 --> 00:00:01,000\nあ\n")
    report = case_dir / "diagnostic.json"
    source_name = "[Erai-raws] Fullmetal Alchemist - Brotherhood - 44 [1080p NF WEB-DL AVC AAC][MultiSub][3210D25C].mkv"
    report.write_text(
        json.dumps(
            {
                "schema": corpus_mod.DIAGNOSTIC_CASE_SCHEMA,
                "media_id": 5114,
                "episode": 44,
                "source_release_name": source_name,
                "compact_media": {"path": str(compact)},
                "candidates": [
                    {
                        "path": str(candidate),
                        "source": "jimaku",
                        "score": 145.32,
                        "name": candidate.name,
                        "episode": 44,
                        "verified_japanese": True,
                        "details": {},
                    }
                ],
            }
        )
    )

    seen = {}
    def fake_optimize(video, candidates, *_args, **_kwargs):
        seen["video"] = video
        assert video.name == source_name
        assert video.resolve() == compact.resolve()
        return candidates[0], candidate, {
            "sync_was_successful": True,
            "timeline_alignment_reliable": True,
            "timeline_exact_release_guard": {"applied": True, "crc": "3210D25C"},
        }

    monkeypatch.setattr(corpus_mod, "optimize_candidates", fake_optimize)
    monkeypatch.setattr(corpus_mod, "subtitle_quality_accepted", lambda _result: (True, "ok"))
    config = SimpleNamespace(
        sync=SimpleNamespace(),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe", alass="alass"),
        matching=SimpleNamespace(
            prefer_srt=True,
            srt_alignment_tolerance_ratio=0.02,
            srt_alignment_tolerance_absolute=0.0,
        ),
    )
    result = corpus_mod.replay_stored_benchmark_case(report, config=config, force=True)
    assert result["status"] == "replayed"
    assert seen["video"].name == source_name
    updated = json.loads(report.read_text())
    assert updated["replay_generation"] == "v109-source-basename-preserved"
    assert updated["replay_media_source_name_preserved"] is True
    assert updated["replay_media_path"].endswith(source_name)


def test_v109_replay_without_source_release_name_keeps_compact_media(monkeypatch, tmp_path) -> None:
    import json
    from types import SimpleNamespace
    import pudge.subtitles.benchmark_corpus as corpus_mod

    case_dir = tmp_path / "diagnostics" / "plain"
    case_dir.mkdir(parents=True)
    compact = case_dir / "compact.mkv"
    compact.write_bytes(b"compact")
    candidate = case_dir / "candidate.srt"
    candidate.write_text("1\n00:00:00,000 --> 00:00:01,000\nあ\n")
    report = case_dir / "diagnostic.json"
    report.write_text(json.dumps({
        "schema": corpus_mod.DIAGNOSTIC_CASE_SCHEMA,
        "media_id": 1,
        "episode": 1,
        "compact_media": {"path": str(compact)},
        "candidates": [{
            "path": str(candidate), "source": "jimaku", "score": 1,
            "name": candidate.name, "episode": 1, "verified_japanese": True, "details": {},
        }],
    }))

    def fake_optimize(video, candidates, *_args, **_kwargs):
        assert video == compact
        return candidates[0], candidate, {"sync_was_successful": True}

    monkeypatch.setattr(corpus_mod, "optimize_candidates", fake_optimize)
    monkeypatch.setattr(corpus_mod, "subtitle_quality_accepted", lambda _result: (True, "ok"))
    config = SimpleNamespace(
        sync=SimpleNamespace(),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe", alass="alass"),
        matching=SimpleNamespace(
            prefer_srt=True,
            srt_alignment_tolerance_ratio=0.02,
            srt_alignment_tolerance_absolute=0.0,
        ),
    )
    corpus_mod.replay_stored_benchmark_case(report, config=config, force=True)
    updated = json.loads(report.read_text())
    assert updated["replay_media_source_name_preserved"] is False
    assert updated["replay_media_path"] == str(compact)
