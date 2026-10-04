from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pudge import syncing
from pudge.config import SyncConfig
from pudge.manager_models import LibraryAnime
from pudge.models import SubtitleCandidate
from pudge.providers.nyaa import release_identity_mismatch_reason, release_title_is_plausible
from pudge.subtitle_formats import convert_to_plain_srt, parse_srt
from pudge.subtitles.speech_recovery import recover_constant_speech_clock

FIXTURES = Path(__file__).parent / "fixtures" / "asr_review_v19"


@pytest.mark.parametrize("name,count,digest", [
    ("seirei-asr.json", 400, "ffa3242c126e4998ce61d0e4a4795e128fdd9e136c08cefcc5f588f7496fd0e4"),
    ("solo-asr.json", 285, "81f1cfa4c31f2dd8775767d5b4c5d9bb44616d76a32988889ed48264b9aba222"),
    ("tokyo-asr.json", 384, "db920c8f48a1d5aa96f0653b52d1013d2fad8bf17a719918df578c8962364291"),
    ("seirei-source.srt", 489, "6a1268b7f77406ec65087ba867c2a2b9b1ae4b8043870b1d734013c4f13db9ca"),
    ("solo-native.srt", 298, "d65b19f92d9243bc71ffa5eeb7c981e26681267e601a1ffc33a0ca0699fcbfce"),
    ("tokyo-source.srt", 331, "a31e584cbce9777647d28d38af779db57b2aee199158bd1ac7664a48011f615d"),
    ("solo-original.ass", 1319, "0ff42d8fa393dc995f6ff212f38a01c8d223c960d450d67bcbd609b34990ce99"),
])
def test_sanitized_corpus_retains_independent_regression_geometry(name, count, digest):
    # These fingerprints were measured independently from snapshot 37cd773;
    # caption and word text are deliberately absent from the signature.
    text = gzip.decompress((FIXTURES / (name + ".gz")).read_bytes()).decode()
    if name.endswith(".json"):
        geometry = [
            [s.get("id"), s.get("start"), s.get("end"), s.get("metrics"), s.get("review_flags"),
             [(w.get("start"), w.get("end"), w.get("probability")) for w in s.get("words", [])]]
            for s in json.loads(text)["segments"]
        ]
    elif name.endswith(".ass"):
        geometry = [
            [*line.split(",", 9)[:9], re.findall(r"\{[^}]*\}", line.split(",", 9)[-1])]
            for line in text.splitlines() if line.startswith("Dialogue:")
        ]
    else:
        geometry = re.findall(r"^.*? --> .*?$", text.replace("\r", ""), re.MULTILINE)
    assert len(geometry) == count
    encoded = json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == digest


def test_generator_recreates_corpus_without_the_original_sources(tmp_path):
    generator = tmp_path / "generator"
    generator.mkdir()
    for name in ("generate.py", "geometry.json.gz"):
        shutil.copy2(FIXTURES / name, generator / name)
    output = tmp_path / "output"
    subprocess.run([sys.executable, "-I", str(generator / "generate.py"), "--output", str(output)],
                   check=True, capture_output=True, timeout=30)
    expected = {path.name for path in FIXTURES.glob("*.gz") if path.name != "geometry.json.gz"}
    assert {path.name for path in output.iterdir()} == expected
    for name in expected:
        # Compare the content: the gzip header OS byte and deflate stream depend
        # on the platform's zlib (macOS writes OS=19), the corpus does not.
        assert gzip.decompress((output / name).read_bytes()) == gzip.decompress((FIXTURES / name).read_bytes())


def materialize(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(gzip.decompress((FIXTURES / (name + ".gz")).read_bytes()))
    return path


def test_synthetic_ass_keeps_all_native_dialogue(tmp_path: Path) -> None:
    ass = materialize(tmp_path, "solo-original.ass")
    native = materialize(tmp_path, "solo-native.srt")
    converted, metadata = convert_to_plain_srt(ass, tmp_path / "cache")
    assert metadata["method"] == "ass-geometry"
    assert metadata["bilingual_removed"] == 0
    assert metadata["geometry"]["ruby_removed"] == 468
    assert metadata["geometry"]["positioned_events"] == 1261
    assert metadata["geometry"]["fragments_joined"] == 495
    actual, expected = parse_srt(converted), parse_srt(native)
    assert len(actual) == len(expected) == 298
    # The independently stored SRT is the expected ruby-free caption stream.
    assert [re.sub(r"\s+", "", c[2]) for c in actual] == [re.sub(r"\s+", "", c[2]) for c in expected]
    # Retain the original two whole-kanji/ruby regression cases as synthetic
    # literals; short base captions must not disappear with their readings.
    assert actual[57][2] == "当峰。"
    assert actual[253][2] == "「痈擈螁脜鷊」？"
    cached, result = convert_to_plain_srt(ass, tmp_path / "cache")
    assert cached == converted and result["reason"] == "cached"


def test_small_unrelated_kana_and_whole_kanji_caption_survive(tmp_path: Path) -> None:
    source = tmp_path / "positioned.ass"
    source.write_text(
        "\n".join(  # noqa: FLY002 - fixture is a list of ASS records
            [
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                r"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\pos(100,100)\fs20}はい",
                r"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\pos(100,400)\fs40}当然",
                r"Dialogue: 0,0:00:05.00,0:00:07.00,Default,,0,0,0,,{\pos(100,400)\fs40}現在",
            ]
        ),
        encoding="utf-8",
    )
    converted, _ = convert_to_plain_srt(source, tmp_path / "cache")
    assert [c[2] for c in parse_srt(converted)] == ["はい\n当然", "現在"]


def test_seirei_constant_clock_is_distributed_and_structurally_safe(tmp_path: Path) -> None:
    source = materialize(tmp_path, "seirei-source.srt")
    asr = materialize(tmp_path, "seirei-asr.json")
    output, result = recover_constant_speech_clock(source, asr, tmp_path / "cache")
    assert output is not None and result["accepted"]
    assert result["anchor_count"] == 141
    assert -1.25 < result["shift_seconds"] < -1.10
    assert result["inlier_fraction"] > 0.9
    assert min(result["quarter_anchor_counts"]) >= 20
    assert result["quarter_anchor_counts"] == [34, 35, 48, 24]
    assert result["inlier_fraction"] == 0.9362
    assert result["quarter_shift_spread_seconds"] == 0.049
    anchors = [{key: value for key, value in anchor.items() if key != "mean_word_probability"}
               for anchor in result["anchors"]]
    encoded = json.dumps(anchors, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "15160f9d71555b1c77fdd7775424ec0b1b7e6167598216e952351349313262cb"
    assert result["alignment_output_guard"]["status"] == "accepted"
    assert result["alignment_output_guard"]["diagnostics"]["new_overlaps"] == 0
    before, after = parse_srt(source), parse_srt(output)
    assert len(before) == len(after) == 489
    assert [c[2] for c in before] == [c[2] for c in after]
    assert max(abs((b[1] - b[0]) - (a[1] - a[0])) for a, b in zip(before, after)) < 0.002


@pytest.mark.parametrize(
    "source_name,asr_name,reason,anchor_count",
    [
        ("solo-native.srt", "solo-asr.json", "speech_clock_not_constant", 68),
        ("tokyo-source.srt", "tokyo-asr.json", "insufficient_distributed_word_anchors", 0),
    ],
)
def test_recovery_refuses_piecewise_clock_and_wrong_content(
    tmp_path: Path, source_name: str, asr_name: str, reason: str, anchor_count: int
) -> None:
    source = materialize(tmp_path, source_name)
    transcript = materialize(tmp_path, asr_name)
    output, result = recover_constant_speech_clock(source, transcript, tmp_path / "cache")
    assert output is None and not result["accepted"]
    assert result["reason"] == reason
    assert result["anchor_count"] == anchor_count


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("sparse", "insufficient_distributed_word_anchors"),
        ("late_only", "insufficient_distributed_word_anchors"),
        ("shift_40", "speech_clock_not_constant"),
        ("no_words", "insufficient_distributed_word_anchors"),
    ],
)
def test_recovery_requires_broad_evidence_not_a_small_cluster(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    source = materialize(tmp_path, "seirei-source.srt")
    transcript = materialize(tmp_path, "seirei-asr.json")
    payload = json.loads(transcript.read_text())
    if mutation == "sparse":
        payload["segments"] = payload["segments"][:10]
    elif mutation == "late_only":
        payload["segments"] = [s for s in payload["segments"] if s["start"] > 1080]
    elif mutation == "shift_40":
        for s in payload["segments"]:
            s["start"] += 40
            s["end"] += 40
            for w in s["words"]:
                w["start"] += 40
                w["end"] += 40
    else:
        for s in payload["segments"]:
            s.pop("words", None)
    transcript.write_text(json.dumps(payload))
    output, result = recover_constant_speech_clock(source, transcript, tmp_path / "cache")
    assert output is None and result["reason"] == reason


def test_guard_wrapper_recovers_using_independent_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = materialize(tmp_path, "seirei-source.srt")
    asr = materialize(tmp_path, "seirei-asr.json")
    # Reproduce a transformed output introducing many new overlaps.
    damaged = tmp_path / "damaged.srt"
    from pudge.alignment_guard import _write_exact_srt

    cues = parse_srt(source)
    _write_exact_srt([(a, b + 3.0, t) for a, b, t in cues], damaged)
    monkeypatch.setattr(
        syncing,
        "_optimize_subtitle_unguarded",
        lambda *a, **k: (damaged, {"sync_was_successful": True, "reference_alignment_reliable": True}),
    )
    calls = []

    def prepare(*args, **kwargs):
        calls.append(kwargs)
        return asr, {"available": True, "transcript_path": str(asr)}

    monkeypatch.setattr(syncing, "prepare_japanese_stt_reference", prepare)
    output, result = syncing.optimize_subtitle(
        tmp_path / "video.mkv", source, tmp_path / "cache", SyncConfig()
    )
    assert output != damaged and output is not None
    assert syncing.subtitle_quality_accepted(result)[0]
    assert result["recovered_from"]["alignment_output_rejected"]
    assert result["alignment_output_guard"]["status"] == "accepted"
    assert calls[0]["force"] is False


def test_recovery_failure_keeps_guard_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        syncing, "prepare_japanese_stt_reference", lambda *a, **k: (None, {"reason": "stt_unavailable"})
    )
    original = {
        "alignment_output_rejected": True,
        "sync_was_successful": False,
        "reason": "alignment_output_guard: new_overlaps",
    }
    path, result = syncing._recover_rejected_clock(
        tmp_path / "v.mkv", tmp_path / "s.srt", None, original, tmp_path / "cache", SyncConfig()
    )
    assert path is None and not syncing.subtitle_quality_accepted(result)[0]
    assert syncing.raw_fallback_disqualified(result)
    assert result["speech_constant_recovery"]["reason"] == "word_transcript_unavailable"


def test_semantic_reject_never_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("semantic mismatch must not enter recovery")

    monkeypatch.setattr(syncing, "prepare_japanese_stt_reference", forbidden)
    original = {
        "alignment_output_rejected": True,
        "sync_was_successful": False,
        "timing_reference_validation": {"accepted": False, "reason": "semantic_mismatch"},
    }
    _, result = syncing._recover_rejected_clock(
        tmp_path / "v.mkv", tmp_path / "s.srt", None, original, tmp_path / "cache", SyncConfig()
    )
    assert result is original


def test_final_identity_failure_does_not_mask_valid_candidate_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = materialize(tmp_path, "seirei-source.srt")
    from pudge.alignment_guard import _write_exact_srt

    damaged = tmp_path / "damaged.srt"
    _write_exact_srt([(a, b + 3.0, t) for a, b, t in parse_srt(source)], damaged)
    correct = SubtitleCandidate(path=source, name="S1", source="jimaku", score=100, details={})
    wrong = SubtitleCandidate(
        path=tmp_path / "s2.srt",
        name="S2",
        source="jimaku",
        score=100,
        details={"entry_anilist_id": 141182, "requested_anilist_id": 126546},
    )

    def choose(video, candidates, *args, **kwargs):
        if correct in candidates:
            return correct, damaged, {"sync_was_successful": True, "reference_alignment_reliable": True}
        return None, None, {"sync_was_successful": False, "reason": "explicit_anilist_identity_mismatch"}

    monkeypatch.setattr(syncing, "_optimize_candidates_unguarded", choose)
    selected, output, result = syncing.optimize_candidates(
        tmp_path / "v.mkv", [correct, wrong], tmp_path / "cache", SyncConfig(japanese_stt_fallback=False)
    )
    assert selected is output is None
    assert result["reason"] == "all_candidate_quality_checks_failed"
    assert result["last_candidate_failure_reason"] == "explicit_anilist_identity_mismatch"
    assert result["guard_rejected_candidates"][0]["name"] == "S1"
    assert result["identity_rejected"][0]["name"] == "S2"




@pytest.mark.parametrize(
    "title,release",
    [
        ("Tokyo Ghoul", "[HorribleSubs] Tokyo Ghoul - 09 [1080p].mkv"),
        ("Tokyo Ghoul:re", "[HorribleSubs] Tokyo Ghoul re - 09 [1080p].mkv"),
        (
            "Re:Zero kara Hajimeru Isekai Seikatsu",
            "[Erai-raws] Re - Zero kara Hajimeru Isekai Seikatsu - 09 [1080p].mkv",
        ),
    ],
)
def test_real_targets_and_re_prefix_still_accepted(title: str, release: str) -> None:
    anime = LibraryAnime(media_id=1, title=title, episodes=12, format="TV")
    assert release_identity_mismatch_reason(anime, release) is None
    assert release_title_is_plausible(anime, release)




def test_old_final_pipeline_cache_is_not_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pudge import pipeline_cache
    from pudge.config import AppConfig

    video = tmp_path / "video.mkv"
    video.write_bytes(b"fixture")
    subtitle = materialize(tmp_path, "solo-native.srt")
    config = AppConfig(config_path=tmp_path / "config.toml")
    config.paths.cache_dir = tmp_path / "cache"
    current_schema = pipeline_cache._CACHE_SCHEMA
    monkeypatch.setattr(pipeline_cache, "_CACHE_SCHEMA", "final-pipeline-v12-shared-opening-anchors")
    pipeline_cache.save_final_pipeline_result(
        video, config, subtitle=subtitle, subtitle_id=None, source="jimaku"
    )
    assert pipeline_cache.load_final_pipeline_result(video, config) is not None
    monkeypatch.setattr(pipeline_cache, "_CACHE_SCHEMA", current_schema)
    assert pipeline_cache.load_final_pipeline_result(video, config) is None
