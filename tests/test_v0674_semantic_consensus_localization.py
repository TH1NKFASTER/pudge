from __future__ import annotations

from pathlib import Path

from pudge.config import LLMConfig
from pudge.llm import OllamaClient
from pudge.manager import _localize_preparation_detail
from pudge.syncing import _apply_robust_semantic_activity_gate


def _write_srt(path: Path, prefix: str) -> None:
    rows = []
    for index in range(12):
        start = index * 10
        minute = start // 60
        second = start % 60
        rows.append(
            f"{index + 1}\n"
            f"00:{minute:02d}:{second:02d},000 --> 00:{minute:02d}:{second + 2:02d},000\n"
            f"{prefix} {index}\n"
        )
    path.write_text("\n".join(rows), encoding="utf-8")


def test_otome_profile_uses_sample_score_consensus(tmp_path: Path) -> None:
    ja = tmp_path / "ja.srt"
    en = tmp_path / "en.srt"
    _write_srt(ja, "日本語")
    _write_srt(en, "English")

    cfg = LLMConfig(
        enabled=True,
        embedded_reference_sample_count=6,
        embedded_reference_phrases_per_sample=2,
        embedded_reference_min_similarity=0.65,
    )
    client = OllamaClient(cfg, cache_dir=tmp_path / "cache")
    client._json_chat = lambda _system, _user: {
        "same_episode": False,
        "usable_for_timing": False,
        "similarity": 0.15,
        "matched_samples": 2,
        "total_samples": 6,
        "sample_scores": [0.0, 0.9, 0.85, 0.7, 0.4, 0.9],
        "reason": "Samples 2-6 show strong semantic alignment despite an outlier.",
    }
    try:
        result = client.compare_subtitle_semantics(ja, en, force=True)
    finally:
        client.close()

    assert result["accepted"] is False
    assert result["robust_semantic_acceptance"] is False
    assert result["robust_matches"] == 4
    assert result["robust_required_matches"] == 5
    assert result["matched_samples"] == 2
    assert result["robust_similarity"] >= 0.85

    gated = _apply_robust_semantic_activity_gate(
        dict(result),
        {"available": True, "weighted": 0.8057},
    )
    assert gated["accepted"] is True
    assert gated["activity_assisted_semantic_acceptance"] is True
    assert gated["matched_samples"] == 4
    assert gated["similarity"] >= 0.85
    assert gated["robust_activity_threshold"] == 0.78


def test_strong_six_of_six_semantics_survive_moderate_activity() -> None:
    validation = {
        "accepted": True,
        "strict_semantic_acceptance": False,
        "robust_semantic_acceptance": True,
        "robust_matches": 6,
        "matched_samples": 6,
        "total_samples": 6,
        "robust_similarity": 0.94,
        "similarity": 0.94,
        "sample_scores": [0.85, 0.95, 0.93, 0.94, 0.78, 0.96],
        "reason": "same episode",
    }
    gated = _apply_robust_semantic_activity_gate(
        validation,
        {"available": True, "weighted": 0.5404},
    )
    assert gated["accepted"] is True
    assert gated["robust_activity_threshold"] == 0.50


def test_four_of_six_consensus_still_requires_high_activity() -> None:
    validation = {
        "accepted": True,
        "strict_semantic_acceptance": False,
        "robust_semantic_acceptance": True,
        "robust_matches": 4,
        "matched_samples": 4,
        "total_samples": 6,
        "robust_similarity": 0.87,
        "similarity": 0.87,
        "reason": "majority",
    }
    gated = _apply_robust_semantic_activity_gate(
        validation,
        {"available": True, "weighted": 0.72},
    )
    assert gated["accepted"] is False
    assert gated["robust_activity_threshold"] == 0.78


def test_english_preparation_diagnostics_translate_sync_messages() -> None:
    raw = (
        "Проверка constant-offset эталона: offset=-42.07s, отклонён, activity=0.80 "
        "LLM-проверка встроенного эталона: отклонён, mode=alass-timestamp "
        "alass+ffsubsync: локальная проверка отклонена "
        "Субтитры отклонены проверкой качества: все проверенные варианты отклонены "
        "Японские субтитры пока не найдены"
    )
    translated = _localize_preparation_detail(raw, language="en")
    assert "Constant-offset reference check:" in translated
    assert "Embedded-reference LLM check:" in translated
    assert "local validation rejected" in translated
    assert "Subtitles rejected by quality validation" in translated
    assert "Japanese subtitles not found yet" in translated
    assert "отклон" not in translated
    assert _localize_preparation_detail(raw, language="ru") == raw


def test_handoff_is_not_repo_content() -> None:
    assert not Path("docs/LLM_HANDOFF.md").exists()
