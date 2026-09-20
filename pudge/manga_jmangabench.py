from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from .manga_benchmark import benchmark_metadata, write_json


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_jmanga_predictions(
    benchmark_root: Path,
    output: Path,
    predict: Callable[[Path], str],
    *,
    limit: int | None = None,
    progress_every: int = 25,
) -> dict[str, int]:
    benchmark_root = Path(benchmark_root).expanduser().resolve()
    output = Path(output).expanduser()
    if progress_every < 1:
        raise ValueError("progress_every must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    annotation_path = benchmark_root / "annotations.jsonl"
    if not annotation_path.is_file():
        raise FileNotFoundError(f"Missing benchmark annotations: {annotation_path}")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite output: {output}")
    annotations = [
        json.loads(line)
        for line in annotation_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit is not None:
        annotations = annotations[:limit]
    output.parent.mkdir(parents=True, exist_ok=True)
    errors = 0
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for index, sample in enumerate(annotations, start=1):
            sample_id = str(sample["id"])
            image_path = benchmark_root / str(sample["image"])
            started = time.perf_counter()
            error: str | None = None
            try:
                prediction = predict(image_path)
                if not isinstance(prediction, str):
                    raise TypeError("predict() must return str")
            except Exception as exc:  # noqa: BLE001 - benchmark output must stay row-complete
                prediction = ""
                error = f"{type(exc).__name__}: {exc}"
                errors += 1
            elapsed = time.perf_counter() - started
            stream.write(
                json.dumps(
                    {
                        "id": sample_id,
                        "prediction": prediction,
                        "latency_seconds": elapsed,
                        "error": error,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            stream.flush()
            if index % progress_every == 0 or index == len(annotations):
                print(f"{index}/{len(annotations)} latest={elapsed:.3f}s", flush=True)
    metadata = benchmark_metadata(
        "jmangabench_mixed_predictions",
        parameters={
            "benchmark_root": str(benchmark_root),
            "limit": limit,
            "progress_every": progress_every,
        },
    )
    metadata["samples"] = len(annotations)
    metadata["errors"] = errors
    metadata["artifacts"] = {
        "predictions": output.name,
        "predictions_sha256": _sha256(output),
    }
    write_json(Path(str(output) + ".meta.json"), metadata)
    return {"samples": len(annotations), "errors": errors}


def mangaocr_predictor() -> Callable[[Path], str]:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    model = MangaOcr()

    def predict(image_path: Path) -> str:
        image = Image.open(image_path).convert("RGB")
        try:
            return str(model(image) or "").strip()
        finally:
            image.close()

    return predict


def _extract_metrics(native_report: dict[str, Any]) -> dict[str, Any]:
    for key in ("overall", "metrics"):
        value = native_report.get(key)
        if isinstance(value, dict):
            return dict(value)
    known = {
        key: native_report[key]
        for key in ("cer", "exact_match", "text_only_cer", "text_only_exact_match")
        if isinstance(native_report.get(key), (int, float))
    }
    return known


def wrap_jmanga_report(
    native_report_path: Path,
    output_path: Path,
    *,
    predictions_path: Path | None = None,
    benchmark_root: Path | None = None,
) -> dict[str, Any]:
    native_report_path = Path(native_report_path).expanduser().resolve()
    native = json.loads(native_report_path.read_text(encoding="utf-8"))
    if not isinstance(native, dict):
        raise TypeError("JMangaBench report must be a JSON object")
    result = benchmark_metadata(
        "jmangabench_mixed",
        parameters={
            "benchmark_root": str(Path(benchmark_root).expanduser().resolve()) if benchmark_root else None,
        },
    )
    if predictions_path is not None:
        predictions = Path(predictions_path).expanduser().resolve()
        sidecar = Path(str(predictions) + ".meta.json")
        if sidecar.is_file():
            prediction_meta = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(prediction_meta, dict):
                for key in ("pudge", "runtime"):
                    if isinstance(prediction_meta.get(key), dict):
                        result[key] = dict(prediction_meta[key])
                result["prediction_run"] = {
                    "parameters": dict(prediction_meta.get("parameters") or {}),
                    "samples": prediction_meta.get("samples"),
                    "errors": prediction_meta.get("errors"),
                }
    result["metrics"] = _extract_metrics(native)
    result["native_report"] = native
    artifacts: dict[str, Any] = {"native_report_sha256": _sha256(native_report_path)}
    if predictions_path is not None:
        predictions = Path(predictions_path).expanduser().resolve()
        artifacts["predictions_sha256"] = _sha256(predictions)
    result["artifacts"] = artifacts
    write_json(Path(output_path).expanduser(), result)
    return result
