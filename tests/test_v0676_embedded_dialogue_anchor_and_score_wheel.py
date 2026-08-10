from __future__ import annotations

from pathlib import Path

import anime_mpv.syncing as syncing
from anime_mpv.config import LLMConfig, SyncConfig
from anime_mpv.llm import OllamaClient
from anime_mpv.subtitle_formats import parse_srt, write_srt


class _SemanticAnchorLLM:
    def match_subtitle_anchor_regions(
        self,
        regions: list[dict[str, object]],
        *,
        force: bool = False,
    ) -> dict[str, object]:
        output = []
        for region in regions:
            japanese = region["japanese"]
            english = region["english"]
            assert isinstance(japanese, list)
            assert isinstance(english, list)
            matches = []
            for ja in japanese:
                assert isinstance(ja, dict)
                token = str(ja["text"]).replace("JP ", "")
                en = next(
                    (
                        row
                        for row in english
                        if isinstance(row, dict)
                        and str(row.get("text") or "") == f"EN {token}"
                    ),
                    None,
                )
                if en is None:
                    continue
                matches.append(
                    {
                        "japanese": [int(ja["index"])],
                        "english": [int(en["index"])],
                        "confidence": 0.98,
                    }
                )
            output.append({"name": region["name"], "matches": matches})
        return {
            "accepted": True,
            "reason": "matched",
            "regions": output,
            "total_matches": sum(len(item["matches"]) for item in output),
        }


def _episode_cues() -> tuple[
    list[tuple[float, float, str]],
    list[tuple[float, float, str]],
]:
    candidate: list[tuple[float, float, str]] = []
    reference: list[tuple[float, float, str]] = []

    before_times = [20, 31, 43, 56, 69, 82, 94, 104]
    for index, start in enumerate(before_times):
        candidate.append((float(start), float(start + 2), f"JP B{index}"))
        reference.append((start - 6.4, start - 4.4, f"EN B{index}"))

    for index, start in enumerate([116, 130, 145, 160, 176, 191]):
        reference.append((float(start), float(start + 5), f"opening lyric {index}"))

    after_times = [210, 223, 236, 249, 263, 277, 291, 304]
    middle_times = [690, 704, 718, 732, 746, 760, 774, 788]
    late_times = [1090, 1104, 1118, 1132, 1146, 1160, 1174, 1188]
    for prefix, values in (
        ("A", after_times),
        ("M", middle_times),
        ("L", late_times),
    ):
        for index, start in enumerate(values):
            candidate.append((float(start), float(start + 2), f"JP {prefix}{index}"))
            reference.append((start - 5.5, start - 3.5, f"EN {prefix}{index}"))

    candidate.sort(key=lambda cue: cue[0])
    reference.sort(key=lambda cue: cue[0])
    return candidate, reference


def test_reference_only_opening_is_detected_from_english_lyrics() -> None:
    candidate, reference = _episode_cues()
    gap = syncing._reference_only_opening_gap(candidate, reference)
    assert gap is not None
    assert float(gap["gap_seconds"]) > 90
    assert int(gap["reference_cues"]) >= 4


def test_direct_english_dialogue_anchors_recover_bleach_like_two_clocks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate, reference = _episode_cues()
    candidate_path = tmp_path / "ja.srt"
    reference_path = tmp_path / "en.srt"
    write_srt(candidate, candidate_path)
    write_srt(reference, reference_path)

    def activity(path: Path, _reference: Path, **_kwargs):
        return {
            "available": True,
            "weighted": 0.62 if path == candidate_path else 0.72,
            "start": 0.55 if path == candidate_path else 0.75,
            "middle": 0.80 if path == candidate_path else 0.82,
            "full": 0.70 if path == candidate_path else 0.76,
        }

    monkeypatch.setattr(syncing, "compare_timing_activity", activity)

    output, result = syncing._repair_with_embedded_reference_dialogue_anchors(
        candidate_path,
        reference_path,
        candidate,
        reference,
        tmp_path / "cache",
        SyncConfig(),
        _SemanticAnchorLLM(),
        force=True,
        verbose=False,
    )

    assert result["applied"] is True
    assert result["strategy"] == "embedded_reference_dialogue_anchors"
    assert abs(float(result["before_offset_seconds"]) + 6.4) < 0.15
    assert abs(float(result["after_offset_seconds"]) + 5.5) < 0.15

    repaired = parse_srt(output)
    first = next(cue for cue in repaired if cue[2] == "JP B0")
    first_after = next(cue for cue in repaired if cue[2] == "JP A0")
    late = next(cue for cue in repaired if cue[2] == "JP L0")
    assert abs(first[0] - 13.6) < 0.05
    assert abs(first_after[0] - 204.5) < 0.05
    assert abs(late[0] - 1084.5) < 0.05


def test_llm_anchor_matcher_preserves_unmatched_english_lyrics(
    tmp_path: Path,
) -> None:
    client = OllamaClient(LLMConfig(enabled=True), cache_dir=tmp_path / "cache")
    client._json_chat = lambda _system, _user: {
        "regions": [
            {
                "name": "after_opening",
                "matches": [
                    {"japanese": [0], "english": [2], "confidence": 0.95},
                    {"japanese": [1, 2], "english": [3], "confidence": 0.90},
                ],
            }
        ]
    }
    regions = [
        {
            "name": "after_opening",
            "japanese": [
                {"index": 0, "start": 200.0, "end": 202.0, "text": "日本語 1"},
                {"index": 1, "start": 205.0, "end": 206.0, "text": "日本語 2"},
                {"index": 2, "start": 206.0, "end": 208.0, "text": "日本語 3"},
            ],
            "english": [
                {"index": 0, "start": 190.0, "end": 194.0, "text": "opening lyric"},
                {"index": 1, "start": 195.0, "end": 199.0, "text": "opening lyric"},
                {"index": 2, "start": 194.5, "end": 196.5, "text": "English 1"},
                {"index": 3, "start": 199.5, "end": 202.5, "text": "English 2/3"},
            ],
        }
    ]
    try:
        result = client.match_subtitle_anchor_regions(regions, force=True)
    finally:
        client.close()

    assert result["accepted"] is True
    assert result["total_matches"] == 2
    matches = result["regions"][0]["matches"]
    assert matches[0]["english"] == [2]
    assert matches[1]["english"] == [3]


def test_v0676_wires_direct_reference_before_old_piecewise_and_removes_score_copy() -> None:
    syncing_source = Path("anime_mpv/syncing.py").read_text(encoding="utf-8")
    web_source = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")

    direct = syncing_source.index("_repair_with_embedded_reference_dialogue_anchors(")
    fallback = syncing_source.index("_repair_stable_opening_plateaus(", direct)
    assert direct < fallback
    assert "reference-dialogue-anchor-v2" in syncing_source
    assert "syncing-v0.3.23:" in syncing_source
    assert "modal.randomScoreText" not in web_source


def test_direct_reference_supports_single_strong_pre_opening_dialogue_anchor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate, reference = _episode_cues()

    candidate = [
        cue
        for cue in candidate
        if not cue[2].startswith("JP B") or cue[2] == "JP B0"
    ]
    reference = [
        cue
        for cue in reference
        if not cue[2].startswith("EN B") or cue[2] == "EN B0"
    ]

    candidate_path = tmp_path / "ja-short-cold-open.srt"
    reference_path = tmp_path / "en-short-cold-open.srt"
    write_srt(candidate, candidate_path)
    write_srt(reference, reference_path)

    def activity(path: Path, _reference: Path, **_kwargs):
        return {
            "available": True,
            "weighted": 0.62 if path == candidate_path else 0.72,
            "start": 0.55 if path == candidate_path else 0.75,
            "middle": 0.80 if path == candidate_path else 0.82,
            "full": 0.70 if path == candidate_path else 0.76,
        }

    monkeypatch.setattr(syncing, "compare_timing_activity", activity)

    output, result = syncing._repair_with_embedded_reference_dialogue_anchors(
        candidate_path,
        reference_path,
        candidate,
        reference,
        tmp_path / "cache",
        SyncConfig(),
        _SemanticAnchorLLM(),
        force=True,
        verbose=False,
    )

    assert result["applied"] is True
    assert int(result["before_cluster"]["support"]) == 1
    assert abs(float(result["before_offset_seconds"]) + 6.4) < 0.15
    assert abs(float(result["after_offset_seconds"]) + 5.5) < 0.15
    assert output != candidate_path
