from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_subtitle_alignment_stt_policies.py"


def _load():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("pudge_stt_policy_matrix", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current(*, accepted: bool = True, engine: str = "embedded-reference+timeline", reliable: bool = True, activity: float = 0.90, local_reliable: bool = True):
    return {
        "accepted": accepted,
        "selected_name": "jp.srt",
        "selected_path": "/tmp/jp.srt",
        "final_path": "/tmp/current.srt",
        "result": {
            "reason": "applied" if accepted else "bad",
            "engine": engine,
            "sync_was_successful": True,
            "reference_alignment_reliable": reliable,
            "reference_activity": {"available": True, "weighted": activity},
            "reference_output_structure": {"reason": "ok"},
            "segment_diagnostics": {"reliable": local_reliable},
        },
    }


def _probe(*, accepted: bool = True, score: float = 0.80, coverage: float = 0.70, activity: float = 0.70, baseline_rank: float = 0.60):
    return {
        "available": True,
        "accepted": accepted,
        "output": "/tmp/stt.srt" if accepted else None,
        "text_alignment": {"available": True, "score": score, "ranking_score": score, "coverage": coverage},
        "activity": {"available": True, "weighted": activity},
        "baseline_text_alignment": {"available": True, "score": baseline_rank, "ranking_score": baseline_rank, "coverage": 0.70},
        "baseline_activity": {"available": True, "weighted": 0.80},
    }


def test_has_ten_monotonic_influence_levels() -> None:
    m = _load()
    assert [row["level"] for row in m.POLICIES] == list(range(10))
    assert [row["key"] for row in m.POLICIES] == [
        "off", "last_resort", "reject_rescue", "suspicious_rescue", "audio_competitor",
        "weak_reference_competitor", "semantic_advantage", "strict_peer",
        "preferred_semantic", "primary_text_clock",
    ]


def test_reject_rescue_is_first_new_authority() -> None:
    m = _load()
    current = _current(accepted=False, reliable=False, activity=0.2)
    probe = _probe()
    assert m._decision(1, current, probe)[0] is False
    use, reason, _ = m._decision(2, current, probe)
    assert use is True and reason == "rejected_result_rescue"


def test_audio_competitor_starts_at_level_four() -> None:
    m = _load()
    current = _current(engine="ffsubsync", reliable=False, activity=0.8)
    probe = _probe(baseline_rank=0.78)
    assert m._decision(3, current, probe)[0] is False
    use, reason, _ = m._decision(4, current, probe)
    assert use is True and reason == "audio_engine_competitor"


def test_weak_reference_competitor_starts_at_level_five() -> None:
    m = _load()
    current = _current(reliable=False, activity=0.5)
    probe = _probe(baseline_rank=0.79)
    assert m._decision(4, current, probe)[0] is False
    use, reason, _ = m._decision(5, current, probe)
    assert use is True and reason == "weak_embedded_reference_competitor"


def test_semantic_advantage_cannot_override_high_trust_reference() -> None:
    m = _load()
    probe = _probe(score=0.86, baseline_rank=0.60)
    high = _current(reliable=True, activity=0.90)
    low = _current(reliable=True, activity=0.75)
    assert m._decision(6, high, probe)[0] is False
    use, reason, _ = m._decision(6, low, probe)
    assert use is True and reason == "semantic_score_advantage"


def test_strict_peer_and_preferred_semantic_expand_authority() -> None:
    m = _load()
    probe = _probe(score=0.80, coverage=0.65, activity=0.65, baseline_rank=0.79)
    ordinary = _current(reliable=False, activity=0.80)
    trusted = _current(reliable=True, activity=0.88)
    # Ordinary non-high-trust result can be replaced by the strict peer level.
    use, reason, _ = m._decision(7, ordinary, probe)
    assert use is True and reason in {"weak_embedded_reference_competitor", "strict_peer_candidate"}
    # A trusted reference survives P7 but not P8 when STT is very strong.
    assert m._decision(7, trusted, probe)[0] is False
    use, reason, _ = m._decision(8, trusted, probe)
    assert use is True and reason == "preferred_semantic_clock"


def test_exceptional_reference_survives_p8_but_not_p9() -> None:
    m = _load()
    current = _current(reliable=True, activity=0.96)
    probe = _probe(score=0.82, coverage=0.70, activity=0.70, baseline_rank=0.80)
    assert m._decision(8, current, probe)[0] is False
    use, reason, _ = m._decision(9, current, probe)
    assert use is True and reason == "primary_accepted_text_clock"


def test_missing_raw_stt_output_never_gains_authority_even_at_p9() -> None:
    m = _load()
    use, reason, _ = m._decision(9, _current(), _probe(accepted=False))
    assert use is False and reason == "stt_raw_candidate_not_eligible"


def test_matrix_outputs_frontier_and_downloads_files() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'f"pudge-subtitle-stt-policy-matrix-{stamp}"' in source
    assert '"policy-summary.csv"' in source
    assert '"case-frontier.csv"' in source
    assert '"first_policy_level"' in source
    assert '"stt_accepted"' in source
    assert '["open", "-R", str(output / "policy-summary.csv")]' in source
    assert "experimental offline arbitration" in source
    assert "--case-timeout-seconds" in source
    assert "case-frontier.partial.csv" in source
    bootstrap = source.index("_reexec_with_pudge_runtime_if_needed()")
    ab_import = source.index("import compare_subtitle_alignment as ab")
    assert bootstrap < ab_import
    assert "PUDGE_STT_POLICY_MATRIX_REEXEC" in source


def test_light_novel_open_uses_webapp_logger_without_global_logging_name() -> None:
    from types import SimpleNamespace
    from pudge.web_app import WebAppApi

    records: list[tuple[object, ...]] = []

    class _Logger:
        def info(self, *args: object) -> None:
            records.append(args)

    api = object.__new__(WebAppApi)
    api.logger = _Logger()
    api.light_novels = SimpleNamespace(
        open_book=lambda book_id: {
            "id": int(book_id),
            "title": "test",
            "chapters": [{"chapter_index": 0, "title": "Chapter 1"}],
        }
    )
    api.audiobooks = SimpleNamespace(
        link_for_light_novel=lambda book_id, **kwargs: {
            "book_id": int(book_id),
            "playing": False,
        }
    )

    result = api.light_novel_open(7)
    assert result["id"] == 7
    assert result["paired_audio"]["book_id"] == 7
    assert records and records[0][0] == "LN open book=%s chapters=%s paired=%s elapsed=%.3fs"


def test_rejected_raw_stt_artifact_is_eligible_for_offline_matrix() -> None:
    m = _load()
    probe = _probe(accepted=False, score=0.73, coverage=0.99, activity=0.89)
    probe["rejected_output"] = "/tmp/rejected-stt.srt"
    assert m._raw_stt_eligible(probe) is True
    use, reason, facts = m._decision(9, _current(reliable=True, activity=0.96), probe)
    assert use is True
    assert reason == "primary_accepted_text_clock"
    assert facts["production_gate_accepted"] is False
    assert facts["raw_stt_eligible"] is True


def test_short_matrix_runs_interleave_benchmark_and_library_cases() -> None:
    m = _load()
    rows = [
        {"kind": "library", "id": "l1"},
        {"kind": "library", "id": "l2"},
        {"kind": "library", "id": "l3"},
        {"kind": "benchmark", "id": "b1"},
        {"kind": "benchmark", "id": "b2"},
    ]
    ordered = m._interleave_case_kinds(rows)
    assert [row["id"] for row in ordered] == ["b1", "l1", "b2", "l2", "l3"]
