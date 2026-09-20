from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from .database import Database
from .manga import MangaService, _finalize_recognized_regions
from .manga_benchmark import benchmark_metadata, edit_distance, greedy_match, normalize_text, write_json


@dataclass(frozen=True)
class Manga109Page:
    book: str
    page_index: int
    image_path: Path
    width: int
    height: int
    regions: list[dict[str, Any]]

    @property
    def key(self) -> str:
        return f"{self.book}:{self.page_index}"


def _annotation_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.xml"))


def _resolve_page_image(images_root: Path, book: str, page_index: int) -> Path:
    book_dir = images_root / book
    if not book_dir.is_dir():
        matches = [path for path in images_root.rglob(book) if path.is_dir() and path.name == book]
        if len(matches) == 1:
            book_dir = matches[0]
    for stem in (f"{page_index:03d}", f"{page_index:04d}", str(page_index)):
        for suffix in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = book_dir / f"{stem}{suffix}"
            if candidate.is_file():
                return candidate
    images = sorted(
        path
        for path in book_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
    ) if book_dir.is_dir() else []
    if 0 <= page_index < len(images):
        return images[page_index]
    raise FileNotFoundError(f"Missing Manga109 page image: book={book!r} page={page_index}")


def _pixel_box_to_pudge(element: ET.Element, page_width: int, page_height: int) -> dict[str, float]:
    xmin = float(element.attrib["xmin"])
    ymin = float(element.attrib["ymin"])
    xmax = float(element.attrib["xmax"])
    ymax = float(element.attrib["ymax"])
    return {
        "x": xmin / page_width,
        "y": (page_height - ymax) / page_height,
        "width": max(0.0, xmax - xmin) / page_width,
        "height": max(0.0, ymax - ymin) / page_height,
    }


def load_manga109_pages(
    images_root: Path,
    annotations_root: Path,
    *,
    books: set[str] | None = None,
    max_pages: int | None = None,
) -> list[Manga109Page]:
    images_root = Path(images_root).expanduser().resolve()
    annotations_root = Path(annotations_root).expanduser().resolve()
    output: list[Manga109Page] = []
    for xml_path in _annotation_files(annotations_root):
        root = ET.parse(xml_path).getroot()
        book = str(root.attrib.get("title") or xml_path.stem)
        if books and book not in books:
            continue
        pages_node = root.find("pages")
        if pages_node is None:
            continue
        for page_node in pages_node.findall("page"):
            page_index = int(page_node.attrib["index"])
            width = int(page_node.attrib["width"])
            height = int(page_node.attrib["height"])
            regions: list[dict[str, Any]] = []
            for text_node in page_node.findall("text"):
                text = str(text_node.text or "").strip()
                if not text:
                    continue
                region: dict[str, Any] = {
                    "id": str(text_node.attrib.get("id") or ""),
                    "text": text,
                    **_pixel_box_to_pudge(text_node, width, height),
                }
                regions.append(region)
            if not regions:
                continue
            output.append(
                Manga109Page(
                    book=book,
                    page_index=page_index,
                    image_path=_resolve_page_image(images_root, book, page_index),
                    width=width,
                    height=height,
                    regions=regions,
                )
            )
            if max_pages is not None and len(output) >= max_pages:
                return output
    return output


def _ratio(numerator: int, denominator: int, *, empty_value: float = 0.0) -> float:
    return numerator / denominator if denominator else empty_value


def evaluate_page(
    ground_truth: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    matches = greedy_match(ground_truth, predictions, iou_threshold=iou_threshold)
    matched_gt = {gt_index for gt_index, _, _ in matches}
    matched_pred = {pred_index for _, pred_index, _ in matches}
    matched_edit = 0
    matched_reference_chars = 0
    exact = 0
    rows: list[dict[str, Any]] = []
    for gt_index, pred_index, iou in matches:
        gt = ground_truth[gt_index]
        pred = predictions[pred_index]
        reference = normalize_text(gt.get("text"))
        prediction = normalize_text(pred.get("text"))
        distance = edit_distance(reference, prediction)
        matched_edit += distance
        matched_reference_chars += len(reference)
        exact += int(reference == prediction)
        rows.append(
            {
                "gt_index": gt_index,
                "pred_index": pred_index,
                "gt_id": str(gt.get("id") or ""),
                "reference": str(gt.get("text") or ""),
                "prediction": str(pred.get("text") or ""),
                "iou": round(iou, 6),
                "edit_distance": distance,
            }
        )

    missed = [index for index in range(len(ground_truth)) if index not in matched_gt]
    false_positive = [index for index in range(len(predictions)) if index not in matched_pred]
    missed_chars = sum(len(normalize_text(ground_truth[index].get("text"))) for index in missed)
    false_positive_chars = sum(
        len(normalize_text(predictions[index].get("text"))) for index in false_positive
    )
    total_reference_chars = sum(len(normalize_text(item.get("text"))) for item in ground_truth)
    end_to_end_edit = matched_edit + missed_chars + false_positive_chars
    matched_count = len(matches)
    precision = _ratio(matched_count, len(predictions), empty_value=1.0 if not ground_truth else 0.0)
    recall = _ratio(matched_count, len(ground_truth), empty_value=1.0 if not predictions else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        "gt_count": len(ground_truth),
        "predicted_count": len(predictions),
        "matched_count": matched_count,
        "missed_count": len(missed),
        "false_positive_count": len(false_positive),
        "detection_precision": precision,
        "detection_recall": recall,
        "detection_f1": f1,
        "matched_edit_distance": matched_edit,
        "matched_reference_chars": matched_reference_chars,
        "matched_cer": _ratio(matched_edit, matched_reference_chars),
        "matched_exact_count": exact,
        "matched_exact_match": _ratio(exact, matched_count, empty_value=1.0),
        "end_to_end_edit_distance": end_to_end_edit,
        "end_to_end_reference_chars": total_reference_chars,
        "end_to_end_cer": _ratio(end_to_end_edit, total_reference_chars),
        "matches": rows,
        "missed_gt_indices": missed,
        "false_positive_indices": false_positive,
    }


def _aggregate_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    gt_count = sum(int(page["gt_count"]) for page in pages)
    predicted_count = sum(int(page["predicted_count"]) for page in pages)
    matched_count = sum(int(page["matched_count"]) for page in pages)
    missed_count = sum(int(page["missed_count"]) for page in pages)
    false_positive_count = sum(int(page["false_positive_count"]) for page in pages)
    matched_edit = sum(int(page["matched_edit_distance"]) for page in pages)
    matched_reference_chars = sum(int(page["matched_reference_chars"]) for page in pages)
    matched_exact_count = sum(int(page["matched_exact_count"]) for page in pages)
    end_to_end_edit = sum(int(page["end_to_end_edit_distance"]) for page in pages)
    end_to_end_reference_chars = sum(int(page["end_to_end_reference_chars"]) for page in pages)
    precision = _ratio(matched_count, predicted_count, empty_value=1.0 if gt_count == 0 else 0.0)
    recall = _ratio(matched_count, gt_count, empty_value=1.0 if predicted_count == 0 else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    return {
        "pages": len(pages),
        "gt_count": gt_count,
        "predicted_count": predicted_count,
        "matched_count": matched_count,
        "missed_count": missed_count,
        "false_positive_count": false_positive_count,
        "detection_precision": precision,
        "detection_recall": recall,
        "detection_f1": f1,
        "matched_cer": _ratio(matched_edit, matched_reference_chars),
        "matched_exact_match": _ratio(matched_exact_count, matched_count, empty_value=1.0),
        "end_to_end_cer": _ratio(end_to_end_edit, end_to_end_reference_chars),
        "matched_edit_distance": matched_edit,
        "matched_reference_chars": matched_reference_chars,
        "end_to_end_edit_distance": end_to_end_edit,
        "end_to_end_reference_chars": end_to_end_reference_chars,
    }


def run_manga109_benchmark(
    pages: Iterable[Manga109Page],
    output_dir: Path,
    *,
    recognize_page: Callable[[Path], list[dict[str, Any]]],
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    output_dir = Path(output_dir).expanduser()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Refusing to overwrite non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    page_results: list[dict[str, Any]] = []
    for page in pages:
        started = time.perf_counter()
        predictions = recognize_page(page.image_path)
        elapsed = time.perf_counter() - started
        metrics = evaluate_page(page.regions, predictions, iou_threshold=iou_threshold)
        page_results.append(
            {
                "key": page.key,
                "book": page.book,
                "page_index": page.page_index,
                "image": str(page.image_path),
                "latency_seconds": elapsed,
                **metrics,
            }
        )
    with (output_dir / "pages.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in page_results:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    report = benchmark_metadata(
        "manga109_fullpage",
        parameters={"iou_threshold": iou_threshold},
    )
    report["metrics"] = _aggregate_pages(page_results)
    report["pages"] = [
        {
            "key": row["key"],
            "end_to_end_edit_distance": row["end_to_end_edit_distance"],
            "end_to_end_reference_chars": row["end_to_end_reference_chars"],
            "detection_f1": row["detection_f1"],
            "latency_seconds": row["latency_seconds"],
        }
        for row in page_results
    ]
    report["artifacts"] = {"pages": "pages.jsonl"}
    write_json(output_dir / "report.json", report)
    return report


def make_pudge_full_page_runner(
    *,
    python: str,
    cache_dir: Path,
) -> Callable[[Path], list[dict[str, Any]]]:
    cache_dir = Path(cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    database = Database(cache_dir / "benchmark.sqlite3")
    service = MangaService(database, cache_dir=cache_dir, python=python)

    def recognize(image_path: Path) -> list[dict[str, Any]]:
        image = Image.open(image_path).convert("RGB")
        try:
            detected = service._vision_text_regions(image)
            regions = detected
            if detected:
                if not service.ocr_available(refresh=False):
                    raise RuntimeError(f"manga_ocr unavailable in benchmark Python: {python}")
                regions = service._ocr_regions(image, detected)
            return _finalize_recognized_regions(
                [dict(item) for item in regions if isinstance(item, dict)]
            )
        finally:
            image.close()

    return recognize
