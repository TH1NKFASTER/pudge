from anime_mpv.config import LLMConfig
from anime_mpv.llm import build_chat_payload


def test_chat_payload_contains_stable_json_options():
    config = LLMConfig(
        enabled=True,
        model="qwen3.5:9b-q8_0",
        think=False,
        keep_alive="10m",
        temperature=0.0,
        num_ctx=8192,
    )

    payload = build_chat_payload(config, "system", "user")

    assert payload["format"] == "json"
    assert payload["think"] is False
    assert payload["keep_alive"] == "10m"
    assert payload["options"] == {"temperature": 0.0, "num_ctx": 8192}

from pathlib import Path

from anime_mpv.llm import OllamaClient, build_subtitle_semantic_samples


def _write_srt(path: Path, prefix: str) -> None:
    path.write_text(
        "1\n00:00:05,000 --> 00:00:07,000\n" + prefix + " one\n\n"
        "2\n00:01:00,000 --> 00:01:02,000\n" + prefix + " two\n\n"
        "3\n00:02:00,000 --> 00:02:02,000\n" + prefix + " three\n\n"
        "4\n00:03:00,000 --> 00:03:02,000\n" + prefix + " four\n",
        encoding="utf-8",
    )


def test_build_semantic_samples_uses_requested_groups_and_phrases(tmp_path: Path):
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    _write_srt(japanese, "日本語")
    _write_srt(english, "English")

    samples = build_subtitle_semantic_samples(
        japanese,
        english,
        sample_count=3,
        phrases_per_sample=2,
    )

    assert len(samples) == 3
    assert all(len(sample["japanese"]) == 2 for sample in samples)
    assert all(len(sample["english"]) == 2 for sample in samples)


def test_llm_semantic_validation_accepts_same_episode(tmp_path: Path, monkeypatch):
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    _write_srt(japanese, "日本語")
    _write_srt(english, "English")
    client = OllamaClient(
        LLMConfig(
            enabled=True,
            embedded_reference_sample_count=3,
            embedded_reference_phrases_per_sample=2,
            embedded_reference_min_similarity=0.65,
        )
    )
    monkeypatch.setattr(
        client,
        "_json_chat",
        lambda *_args, **_kwargs: {
            "same_episode": True,
            "usable_for_timing": True,
            "similarity": 0.82,
            "matched_samples": 3,
            "total_samples": 3,
            "sample_scores": [0.8, 0.9, 0.76],
            "reason": "same scenes with extra sound cues",
        },
    )
    try:
        result = client.compare_subtitle_semantics(japanese, english)
    finally:
        client.close()

    assert result["accepted"] is True
    assert result["similarity"] == 0.82
    assert result["matched_samples"] == 3


def test_llm_semantic_validation_rejects_different_episode(tmp_path: Path, monkeypatch):
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    _write_srt(japanese, "日本語")
    _write_srt(english, "English")
    client = OllamaClient(LLMConfig(enabled=True, embedded_reference_sample_count=3, embedded_reference_phrases_per_sample=2, embedded_reference_min_similarity=0.65))
    monkeypatch.setattr(
        client,
        "_json_chat",
        lambda *_args, **_kwargs: {
            "same_episode": False,
            "usable_for_timing": False,
            "similarity": 0.21,
            "matched_samples": 1,
            "total_samples": 6,
            "sample_scores": [0.1, 0.2],
            "reason": "different episode",
        },
    )
    try:
        result = client.compare_subtitle_semantics(japanese, english)
    finally:
        client.close()

    assert result["accepted"] is False
    assert result["reason"] == "different episode"


def test_llm_semantic_validation_accepts_one_title_card_outlier(tmp_path: Path, monkeypatch):
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    def write_many(path: Path, prefix: str) -> None:
        blocks = []
        for index in range(1, 13):
            minute = index - 1
            blocks.append(
                f"{index}\n00:{minute:02d}:00,000 --> 00:{minute:02d}:02,000\n{prefix} {index}\n"
            )
        path.write_text("\n".join(blocks), encoding="utf-8")
    write_many(japanese, "日本語")
    write_many(english, "English")
    client = OllamaClient(
        LLMConfig(
            enabled=True,
            embedded_reference_sample_count=6,
            embedded_reference_phrases_per_sample=2,
            embedded_reference_min_similarity=0.65,
        )
    )
    monkeypatch.setattr(
        client,
        "_json_chat",
        lambda *_args, **_kwargs: {
            "same_episode": False,
            "usable_for_timing": False,
            "similarity": 0.15,
            "matched_samples": 2,
            "total_samples": 6,
            "sample_scores": [0.1, 0.91, 0.86, 0.9, 0.84, 0.88],
            "reason": "one title card differs, remaining scenes match",
        },
    )
    try:
        result = client.compare_subtitle_semantics(japanese, english)
    finally:
        client.close()

    assert result["accepted"] is True
    assert result["strict_semantic_acceptance"] is False
    assert result["robust_semantic_acceptance"] is True
    assert result["matched_samples"] == 5
    assert result["reason"].startswith("accepted_with_one_semantic_outlier")


def test_llm_semantic_validation_rejects_two_outliers(tmp_path: Path, monkeypatch):
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    def write_many(path: Path, prefix: str) -> None:
        blocks = []
        for index in range(1, 13):
            minute = index - 1
            blocks.append(
                f"{index}\n00:{minute:02d}:00,000 --> 00:{minute:02d}:02,000\n{prefix} {index}\n"
            )
        path.write_text("\n".join(blocks), encoding="utf-8")
    write_many(japanese, "日本語")
    write_many(english, "English")
    client = OllamaClient(
        LLMConfig(
            enabled=True,
            embedded_reference_sample_count=6,
            embedded_reference_phrases_per_sample=2,
            embedded_reference_min_similarity=0.65,
        )
    )
    monkeypatch.setattr(
        client,
        "_json_chat",
        lambda *_args, **_kwargs: {
            "same_episode": False,
            "usable_for_timing": False,
            "similarity": 0.4,
            "matched_samples": 4,
            "total_samples": 6,
            "sample_scores": [0.1, 0.2, 0.91, 0.86, 0.9, 0.84],
            "reason": "two sections differ",
        },
    )
    try:
        result = client.compare_subtitle_semantics(japanese, english)
    finally:
        client.close()

    assert result["accepted"] is False
    assert result["robust_semantic_acceptance"] is False
