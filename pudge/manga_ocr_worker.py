from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

from PIL import Image


def _crop_region(image: Image.Image, region: dict[str, object]) -> Image.Image:
    width, height = image.size
    x = max(0.0, min(1.0, float(region.get("x") or 0.0)))
    y = max(0.0, min(1.0, float(region.get("y") or 0.0)))
    region_width = max(0.0, min(1.0 - x, float(region.get("width") or 0.0)))
    region_height = max(0.0, min(1.0 - y, float(region.get("height") or 0.0)))
    pad_x = max(4, round(width * 0.008))
    pad_y = max(4, round(height * 0.008))
    left = max(0, round(x * width) - pad_x)
    right = min(width, round((x + region_width) * width) + pad_x)
    top = max(0, round((1.0 - y - region_height) * height) - pad_y)
    bottom = min(height, round((1.0 - y) * height) + pad_y)
    if right <= left or bottom <= top:
        raise ValueError("empty OCR region")
    return image.crop((left, top, right, bottom)).convert("RGB")


def _recognize_regions(model: object, image: Image.Image, regions: list[dict[str, object]]) -> list[dict[str, object]]:
    recognized: list[dict[str, object]] = []
    for region in regions:
        item = dict(region)
        try:
            item["text"] = str(model(_crop_region(image, region)) or "").strip()  # type: ignore[operator]
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            item["text"] = str(region.get("text") or "").strip()
        if str(item.get("text") or "").strip():
            recognized.append(item)
    return recognized


def _single(source: Path, output: Path) -> int:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    image = Image.open(source).convert("RGB")
    model = MangaOcr()
    text = str(model(image) or "").strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    return 0


def _regions(source: Path, manifest_path: Path, output: Path) -> int:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    image = Image.open(source).convert("RGB")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    regions = payload.get("regions") if isinstance(payload, dict) else []
    normalized = [dict(item) for item in regions if isinstance(item, dict)]
    recognized = _recognize_regions(MangaOcr(), image, normalized)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"regions": recognized}, ensure_ascii=False), encoding="utf-8")
    return 0


def _batch(manifest_path: Path, output_path: Path, progress_path: Path) -> int:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_path = Path(str(manifest["archive"])).expanduser()
    pages = manifest.get("pages") or []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    model = MangaOcr()
    failures = 0
    with zipfile.ZipFile(archive_path) as archive, output_path.open("a", encoding="utf-8") as output:
        for done, item in enumerate(pages, start=1):
            page_index = int(item["page_index"])
            name = str(item["name"])
            row: dict[str, object] = {"page_index": page_index}
            try:
                image = Image.open(io.BytesIO(archive.read(name))).convert("RGB")
                regions = [dict(region) for region in item.get("regions") or [] if isinstance(region, dict)]
                recognized = _recognize_regions(model, image, regions)
                row["regions"] = recognized
                row["text"] = "\n".join(str(region.get("text") or "") for region in recognized).strip()
            except Exception as exc:
                failures += 1
                row["error"] = f"{type(exc).__name__}: {exc}"
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            progress_path.write_text(
                json.dumps(
                    {"done": done, "total": len(pages), "page_index": page_index},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    return 1 if failures else 0


def main() -> int:
    try:
        if len(sys.argv) == 5 and sys.argv[1] == "--regions":
            return _regions(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        if len(sys.argv) == 5 and sys.argv[1] == "--batch":
            return _batch(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        if len(sys.argv) == 3:
            return _single(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser())
        print(
            "usage: python -m pudge.manga_ocr_worker INPUT_IMAGE OUTPUT_JSON\n"
            "   or: python -m pudge.manga_ocr_worker --regions INPUT_IMAGE MANIFEST OUTPUT_JSON\n"
            "   or: python -m pudge.manga_ocr_worker --batch MANIFEST RESULTS_JSONL PROGRESS_JSON",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
