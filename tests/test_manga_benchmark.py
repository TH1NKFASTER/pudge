from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image


def _write_image(path: Path, *, size: tuple[int, int] = (100, 200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)


def test_normalize_iou_and_greedy_match_are_deterministic() -> None:
    from pudge.manga_benchmark import box_iou, greedy_match, normalize_text

    assert normalize_text(" Ａ\n B\t") == "AB"
    assert box_iou(
        {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5},
        {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5},
    ) == pytest.approx(1 / 7)

    gt = [
        {"x": 0.0, "y": 0.0, "width": 0.4, "height": 0.4},
        {"x": 0.5, "y": 0.0, "width": 0.4, "height": 0.4},
    ]
    pred = [
        {"x": 0.51, "y": 0.0, "width": 0.39, "height": 0.4},
        {"x": 0.01, "y": 0.0, "width": 0.39, "height": 0.4},
    ]
    assert [(g, p) for g, p, _ in greedy_match(gt, pred, iou_threshold=0.5)] == [(0, 1), (1, 0)]


def test_compare_reports_rejects_incompatible_benchmarks_and_counts_page_changes() -> None:
    from pudge.manga_benchmark import compare_reports

    old = {
        "schema": "pudge_manga_benchmark/v1",
        "benchmark": "manga109_fullpage",
        "parameters": {"iou_threshold": 0.5},
        "metrics": {"detection_f1": 0.8, "end_to_end_cer": 0.2},
        "pages": [
            {"key": "A:0", "end_to_end_edit_distance": 4},
            {"key": "A:1", "end_to_end_edit_distance": 2},
        ],
    }
    new = {
        "schema": "pudge_manga_benchmark/v1",
        "benchmark": "manga109_fullpage",
        "parameters": {"iou_threshold": 0.5},
        "metrics": {"detection_f1": 0.9, "end_to_end_cer": 0.1},
        "pages": [
            {"key": "A:0", "end_to_end_edit_distance": 3},
            {"key": "A:1", "end_to_end_edit_distance": 5},
        ],
    }
    comparison = compare_reports(old, new)
    assert comparison["metric_deltas"]["detection_f1"] == pytest.approx(0.1)
    assert comparison["pages"]["improved"] == 1
    assert comparison["pages"]["regressed"] == 1

    bad = dict(new, benchmark="jmangabench_mixed")
    with pytest.raises(ValueError, match="benchmark"):
        compare_reports(old, bad)


def test_jmangabench_predictions_follow_upstream_jsonl_contract(tmp_path: Path) -> None:
    from pudge.manga_jmangabench import run_jmanga_predictions

    root = tmp_path / "JMangaBench_Mixed"
    _write_image(root / "images" / "real_crops" / "a.png")
    _write_image(root / "images" / "synthetic" / "b.png")
    rows = [
        {"id": "jmm_real_1", "image": "images/real_crops/a.png"},
        {"id": "jmm_synth_1", "image": "images/synthetic/b.png"},
    ]
    root.mkdir(parents=True, exist_ok=True)
    (root / "annotations.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    output = tmp_path / "predictions.jsonl"

    seen: list[str] = []

    def predict(image_path: Path) -> str:
        seen.append(image_path.name)
        return {"a.png": "やあ坊主", "b.png": "ドン"}[image_path.name]

    result = run_jmanga_predictions(root, output, predict, progress_every=100)
    assert result == {"samples": 2, "errors": 0}
    assert seen == ["a.png", "b.png"]
    predictions = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["id"] for row in predictions] == ["jmm_real_1", "jmm_synth_1"]
    assert [row["prediction"] for row in predictions] == ["やあ坊主", "ドン"]
    assert all(isinstance(row["latency_seconds"], float) for row in predictions)
    assert all(row["error"] is None for row in predictions)
    sidecar = Path(str(output) + ".meta.json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    assert metadata["schema"] == "pudge_manga_benchmark/v1"
    assert metadata["benchmark"] == "jmangabench_mixed_predictions"
    assert metadata["samples"] == 2
    with pytest.raises(FileExistsError):
        run_jmanga_predictions(root, output, predict)


def test_wrap_jmanga_report_preserves_upstream_report_and_metadata(tmp_path: Path) -> None:
    from pudge.manga_jmangabench import wrap_jmanga_report

    native = {
        "protocol": "jmangabench_mixed_ocr_metrics/v1",
        "overall": {"cer": 0.04683, "exact_match": 0.73524},
    }
    native_path = tmp_path / "report.json"
    native_path.write_text(json.dumps(native), encoding="utf-8")
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text('{"id":"x","prediction":"a"}\n', encoding="utf-8")
    Path(str(predictions) + ".meta.json").write_text(
        json.dumps({
            "schema": "pudge_manga_benchmark/v1",
            "benchmark": "jmangabench_mixed_predictions",
            "pudge": {
                "version": "0.7.27",
                "pipeline_worker": "worker-from-prediction-run",
                "pipeline_generation": "generation-from-prediction-run",
                "git_commit": "abc",
            },
            "runtime": {"python": "3.13", "platform": "test", "machine": "arm64"},
            "parameters": {},
        }),
        encoding="utf-8",
    )
    output = tmp_path / "pudge-report.json"

    wrapped = wrap_jmanga_report(native_path, output, predictions_path=predictions)
    assert wrapped["schema"] == "pudge_manga_benchmark/v1"
    assert wrapped["benchmark"] == "jmangabench_mixed"
    assert wrapped["metrics"] == native["overall"]
    assert wrapped["native_report"] == native
    assert wrapped["pudge"]["pipeline_generation"] == "generation-from-prediction-run"
    assert wrapped["artifacts"]["predictions_sha256"]
    assert json.loads(output.read_text(encoding="utf-8")) == wrapped


def test_manga109_xml_parser_converts_top_left_bbox_to_pudge_coordinates(tmp_path: Path) -> None:
    from pudge.manga109_benchmark import load_manga109_pages

    images = tmp_path / "images"
    annotations = tmp_path / "annotations"
    _write_image(images / "ARMS" / "003.jpg", size=(100, 200))
    annotations.mkdir(parents=True)
    (annotations / "ARMS.xml").write_text(
        """<?xml version='1.0' encoding='utf-8'?>
<book title="ARMS"><pages><page index="3" width="100" height="200">
<text id="0001" xmin="10" ymin="20" xmax="30" ymax="60">キャーッ</text>
</page></pages></book>""",
        encoding="utf-8",
    )

    pages = load_manga109_pages(images, annotations)
    assert len(pages) == 1
    page = pages[0]
    assert page.book == "ARMS"
    assert page.page_index == 3
    assert page.image_path == images / "ARMS" / "003.jpg"
    region = page.regions[0]
    assert region["text"] == "キャーッ"
    assert region["x"] == pytest.approx(0.10)
    assert region["y"] == pytest.approx(0.70)
    assert region["width"] == pytest.approx(0.20)
    assert region["height"] == pytest.approx(0.20)


def test_manga109_page_metrics_include_detection_text_and_unmatched_penalties() -> None:
    from pudge.manga109_benchmark import evaluate_page

    gt = [
        {"id": "g1", "text": "こんにちは", "x": 0.0, "y": 0.0, "width": 0.4, "height": 0.4},
        {"id": "g2", "text": "ドン", "x": 0.5, "y": 0.0, "width": 0.4, "height": 0.4},
    ]
    pred = [
        {"text": "こんにちわ", "x": 0.0, "y": 0.0, "width": 0.4, "height": 0.4},
        {"text": "余計", "x": 0.0, "y": 0.6, "width": 0.2, "height": 0.2},
    ]
    result = evaluate_page(gt, pred, iou_threshold=0.5)
    assert result["matched_count"] == 1
    assert result["missed_count"] == 1
    assert result["false_positive_count"] == 1
    assert result["detection_precision"] == pytest.approx(0.5)
    assert result["detection_recall"] == pytest.approx(0.5)
    assert result["matched_edit_distance"] == 1
    assert result["matched_reference_chars"] == 5
    # 1 substitution + 2 missed GT chars + 2 false-positive chars.
    assert result["end_to_end_edit_distance"] == 5
    assert result["end_to_end_reference_chars"] == 7


def test_run_manga109_benchmark_writes_frozen_report_and_pages_jsonl(tmp_path: Path) -> None:
    from pudge.manga109_benchmark import Manga109Page, run_manga109_benchmark

    image = tmp_path / "page.jpg"
    _write_image(image)
    page = Manga109Page(
        book="ARMS",
        page_index=3,
        image_path=image,
        width=100,
        height=200,
        regions=[{"id": "g1", "text": "ドン", "x": 0.1, "y": 0.7, "width": 0.2, "height": 0.2}],
    )

    def recognize(_path: Path) -> list[dict[str, object]]:
        return [{"text": "ドン", "x": 0.1, "y": 0.7, "width": 0.2, "height": 0.2}]

    output = tmp_path / "results"
    report = run_manga109_benchmark([page], output, recognize_page=recognize)
    assert report["schema"] == "pudge_manga_benchmark/v1"
    assert report["benchmark"] == "manga109_fullpage"
    assert report["metrics"]["detection_f1"] == pytest.approx(1.0)
    assert report["metrics"]["end_to_end_cer"] == pytest.approx(0.0)
    assert (output / "pages.jsonl").is_file()
    assert json.loads((output / "report.json").read_text(encoding="utf-8"))["metrics"] == report["metrics"]


def test_benchmark_cli_parser_exposes_all_commands() -> None:
    from pudge.manga_benchmark_cli import build_parser

    parser = build_parser()
    for argv, expected in [
        (["jmanga-predict", "--benchmark", "/tmp/b", "--output", "/tmp/p.jsonl"], "jmanga-predict"),
        (["jmanga-wrap-report", "--report", "/tmp/r.json", "--output", "/tmp/w.json"], "jmanga-wrap-report"),
        (
            [
                "manga109",
                "--images-root",
                "/tmp/i",
                "--annotations-root",
                "/tmp/a",
                "--output-dir",
                "/tmp/o",
            ],
            "manga109",
        ),
        (["compare", "/tmp/old.json", "/tmp/new.json"], "compare"),
        (
            ["review-diff", "/tmp/old-review", "/tmp/new-review", "--output-dir", "/tmp/diff"],
            "review-diff",
        ),
    ]:
        assert parser.parse_args(argv).command == expected
