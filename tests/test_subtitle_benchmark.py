from __future__ import annotations

from pudge.subtitles.benchmark import (
    aggregate_oracle_reports,
    evaluate_subtitle_cues,
    normalize_benchmark_text,
)


def _cues(*rows):
    return [(float(start), float(end), text) for start, end, text in rows]


def test_normalization_ignores_styling_and_punctuation() -> None:
    assert normalize_benchmark_text(r"{\\an8}<i>「テスト！」</i> ABC-12") == "テストabc12"


def test_oracle_evaluator_handles_cue_resegmentation_and_constant_shift() -> None:
    oracle = _cues(
        (0, 2, "これは長い最初の台詞です"),
        (3, 5, "次の台詞もここにあります"),
        (6, 8, "最後まで同じ文章を使います"),
    )
    candidate = _cues(
        (1, 2, "これは長い"),
        (2, 3, "最初の台詞です"),
        (4, 6, "次の台詞もここにあります"),
        (7, 9, "最後まで同じ文章を使います"),
    )
    report = evaluate_subtitle_cues(candidate, oracle, ngram=4)
    assert report.evaluable is True
    assert report.same_episode is True
    assert report.anchor_count >= 20
    assert report.signed_median_error_seconds is not None
    assert 0.8 <= report.signed_median_error_seconds <= 1.2
    assert report.p90_abs_error_seconds is not None
    assert report.p90_abs_error_seconds <= 1.3


def test_oracle_evaluator_rejects_different_episode_even_when_timing_looks_similar() -> None:
    oracle = _cues(
        (0, 2, "今日は学校へ行って友達と勉強します"),
        (3, 5, "放課後には図書館で本を読みます"),
        (6, 8, "帰り道で新しい店を見つけました"),
    )
    candidate = _cues(
        (0, 2, "宇宙船は静かな惑星へ着陸しました"),
        (3, 5, "隊長は未知の生命体を探しています"),
        (6, 8, "最後に基地へ戻って報告しました"),
    )
    report = evaluate_subtitle_cues(candidate, oracle, ngram=4)
    assert report.evaluable is False
    assert report.same_episode is False
    assert report.reason == "insufficient_text_match"


def test_oracle_evaluator_exposes_early_catastrophic_region() -> None:
    phrases = [f"これは番号{i}の十分に長い日本語台詞です" for i in range(40)]
    oracle = []
    candidate = []
    for index, phrase in enumerate(phrases):
        start = index * 10.0
        oracle.append((start, start + 3.0, phrase))
        shift = 8.0 if index < 12 else 0.1
        candidate.append((start + shift, start + 3.0 + shift, phrase))

    report = evaluate_subtitle_cues(candidate, oracle, ngram=6)
    assert report.evaluable is True
    assert report.good_alignment is False
    assert report.longest_bad_2s_span_seconds is not None
    assert report.longest_bad_2s_span_seconds >= 90.0
    assert report.regions["first_120s"]["p90_abs_error_seconds"] > 7.0
    assert report.regions["middle"]["p90_abs_error_seconds"] < 1.0


def test_oracle_evaluator_marks_tight_alignment_good() -> None:
    oracle = []
    candidate = []
    for index in range(35):
        phrase = f"ここは重複しない番号{index}の日本語文章です"
        start = index * 8.0
        oracle.append((start, start + 2.5, phrase))
        candidate.append((start + 0.15, start + 2.65, phrase))
    report = evaluate_subtitle_cues(candidate, oracle, ngram=6)
    assert report.evaluable is True
    assert report.good_alignment is True
    assert report.within_100_ratio is not None and report.within_100_ratio >= 0.95
    assert report.p99_abs_error_seconds is not None and report.p99_abs_error_seconds < 0.5


def test_aggregate_reports_uses_episode_level_quality() -> None:
    good = {
        "evaluable": True,
        "same_episode": True,
        "good_alignment": True,
        "reason": "good",
        "p90_abs_error_seconds": 0.2,
        "p99_abs_error_seconds": 0.4,
        "oracle_coverage": 0.9,
        "longest_bad_2s_span_seconds": 0.0,
    }
    bad = {
        "evaluable": True,
        "same_episode": True,
        "good_alignment": False,
        "reason": "catastrophic_region",
        "p90_abs_error_seconds": 3.0,
        "p99_abs_error_seconds": 8.0,
        "oracle_coverage": 0.8,
        "longest_bad_2s_span_seconds": 90.0,
    }
    missing = {
        "evaluable": False,
        "same_episode": False,
        "good_alignment": False,
        "reason": "insufficient_text_match",
    }
    summary = aggregate_oracle_reports([good, bad, missing])
    assert summary["total"] == 3
    assert summary["evaluable"] == 2
    assert summary["good"] == 1
    assert summary["good_ratio"] == 0.5
    assert summary["reason_counts"]["catastrophic_region"] == 1
