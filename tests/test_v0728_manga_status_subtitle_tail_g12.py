from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _write_srt(path: Path, end_seconds: int) -> None:
    minutes, seconds = divmod(end_seconds, 60)
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nstart\n\n"
        f"2\n00:{minutes:02d}:{max(0, seconds-1):02d},000 --> 00:{minutes:02d}:{seconds:02d},000\nend\n",
        encoding="utf-8",
    )


def test_assassination_classroom_singleton_tail_clock_is_rejected() -> None:
    from pudge.subtitles.timeline_alignment import _suppress_weak_singleton_tail_transition

    source = [(0.0, 10.0, "start"), (1216.298, 1222.429, "late"), (1369.910, 1374.915, "song")]
    reference = [(0.0, 10.0, "start"), (1385.0, 1390.950, "credits")]
    segments = [
        {"offset_seconds": 0.0, "support": 22, "kind": "stable"},
        {"offset_seconds": 59.0, "support": 1, "kind": "stable"},
    ]

    result, boundaries, diagnostics = _suppress_weak_singleton_tail_transition(
        source, segments, [1216.0], reference
    )

    assert [row["offset_seconds"] for row in result] == [0.0]
    assert boundaries == []
    assert diagnostics["applied"] is True
    assert diagnostics["reason"] == "weak_singleton_tail_clock_reference_overshoot"
    assert diagnostics["tail_duration_seconds"] == 158.915
    assert diagnostics["reference_overshoot_seconds"] == 42.965


def test_embedded_reference_gate_rejects_59_second_tail_growth(tmp_path: Path) -> None:
    from pudge.syncing import _validate_embedded_reference_output

    reference = tmp_path / "reference.srt"
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    _write_srt(reference, 1391)
    _write_srt(source, 1375)
    _write_srt(aligned, 1434)

    ok, reason, details = _validate_embedded_reference_output(source, aligned, reference)
    assert ok is False
    assert reason == "aligned_too_long"
    assert details["alignment_extension_seconds"] == 59
    assert details["reference_overshoot_seconds"] == 43


def test_manga_jiten_status_overlay_contract() -> None:
    manga = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    reading = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")

    assert "statusHighlight: false" in manga
    for key in ("statusNewColor", "statusLearningColor", "statusKnownColor", "statusDueColor"):
        assert f'data-manga-setting="{key}"' in manga
    assert ".manga-v2-status-layer{position:absolute;z-index:1;pointer-events:none" in css
    assert "mangaTokenHitboxes(regionNode, region)" in manga
    assert "['observed','approximate'].includes(geometryStatus)" in manga
    assert "String(region.word_geometry || '') !== 'mapped_segments'" in manga
    assert "String(box.source || '') === 'region-single-token'" in manga
    assert "region?.provenance?.source === 'mokuro'" in manga
    assert "verifiedJitenStateForElement" in manga
    assert "node.dataset.pudgeStudyScope !== studyStateScope" in reading
    assert "!payload.stale" in reading
    assert "accountScope !== String(caps.account_key || '')" in reading
    assert "Due is an orthogonal review flag" in reading
    assert "['blacklisted','redundant','ignored']" in reading
    assert "invalidateJitenStatePair(token)" in reading
    assert "refreshJitenPairAfterMutation(token, attempt = 0, before = null)" in reading
    assert "pudge-study-states-changed" in manga
    assert "data-occurrence-id" in manga
    assert "alignment_revision" in manga


def test_g12_javascript_syntax() -> None:
    for rel in ("pudge/web/reading_tools.js", "pudge/web/manga_reader_v2.js"):
        result = subprocess.run(
            ["node", "--check", str(ROOT / rel)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
