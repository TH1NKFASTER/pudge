from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

from PIL import Image


def _single(source: Path, output: Path) -> int:
    from manga_ocr import MangaOcr  # type: ignore[import-not-found]

    image = Image.open(source).convert("RGB")
    model = MangaOcr()
    text = str(model(image) or "").strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
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
                row["text"] = str(model(image) or "").strip()
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
        if len(sys.argv) == 5 and sys.argv[1] == "--batch":
            return _batch(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        if len(sys.argv) == 3:
            return _single(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]).expanduser())
        print(
            "usage: python -m pudge.manga_ocr_worker INPUT_IMAGE OUTPUT_JSON\n"
            "   or: python -m pudge.manga_ocr_worker --batch MANIFEST RESULTS_JSONL PROGRESS_JSON",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
