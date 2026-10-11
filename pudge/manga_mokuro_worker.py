"""Standalone runner for an isolated Mokuro environment.

pudge never imports Mokuro (GPL-3.0) into its own
process. This script is executed by the Python interpreter of a separately
installed Mokuro environment and only exchanges JSON with pudge:

    MOKURO_PYTHON manga_mokuro_worker.py MODE MANIFEST OUTPUT_JSONL [PROGRESS_JSON]
    MOKURO_PYTHON manga_mokuro_worker.py serve

"serve" keeps the models loaded and answers one JSON request per stdin line
({"id", "op": "ocr"|"ping", "archive"+"name" or "path"}) with one JSON
line on stdout ({"id", ...result} or {"id", "error"}). It exits when stdin closes.

MODE "ocr" runs Mokuro's page OCR (text detector + MangaOCR) with Mokuro's
defaults; each output line is {"key", "page": {img_width, img_height, blocks}}
in the .mokuro page format. Failed pages are {"key", "error"}.

MANIFEST is either {"images": [{"key": "...", "path": "..."}]} or
{"archive": "...cbz", "pages": [{"key": "...", "name": "..."}]}.
Coordinates are image pixels. The script deliberately imports nothing from pudge.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path


def _json_default(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class _PageOcr:
    def __init__(self) -> None:
        from mokuro.manga_page_ocr import MangaPageOcr  # type: ignore[import-not-found]

        self.ocr = MangaPageOcr()

    def __call__(self, data: bytes) -> dict[str, object]:
        # Mokuro reads pages through PIL, which accepts a file object.
        result = self.ocr(io.BytesIO(data))
        return {"page": json.loads(json.dumps(result, ensure_ascii=False, default=_json_default))}


def _read_page(request: dict) -> bytes:
    if request.get("archive"):
        with zipfile.ZipFile(str(request["archive"])) as archive:
            return archive.read(str(request["name"]))
    return Path(str(request["path"])).read_bytes()


def serve() -> int:
    if os.environ.get("PUDGE_BACKGROUND_PRIORITY") == "1":
        # Background library OCR runs at low CPU priority (nice 15), like other
        # background work; this interpreter cannot import pudge's helper.
        try:
            os.nice(max(0, 15 - os.nice(0)))
        except OSError:
            pass
    # Only replies may reach the protocol stream: whatever Mokuro, torch or
    # transformers print goes to stderr instead of the real stdout.
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    engines: dict[str, object] = {}

    def engine(op: str):
        if op not in engines:
            started = time.time()
            engines[op] = _PageOcr()
            sys.stderr.write(f"mokuro {op} ready in {time.time() - started:.1f}s\n")
            sys.stderr.flush()
        return engines[op]

    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        reply: dict[str, object] = {"id": request.get("id")}
        op = request.get("op")
        try:
            if op == "ping":
                reply["ok"] = True
            elif op == "ocr":
                reply.update(engine(str(op))(_read_page(request)))
            else:
                reply["error"] = f"unknown op {op!r}"
        except Exception as exc:  # noqa: BLE001
            reply["error"] = f"{type(exc).__name__}: {exc}"
        protocol.write(json.dumps(reply, ensure_ascii=False) + "\n")
        protocol.flush()
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "serve":
        return serve()
    if len(sys.argv) not in {4, 5} or sys.argv[1] != "ocr":
        print(
            "usage: manga_mokuro_worker.py ocr MANIFEST OUTPUT_JSONL [PROGRESS_JSON] | serve",
            file=sys.stderr,
        )
        return 2
    mode = sys.argv[1]
    manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    output_path = Path(sys.argv[3])
    progress_path = Path(sys.argv[4]) if len(sys.argv) == 5 else None
    started = time.time()
    run = _PageOcr()
    sys.stderr.write(f"mokuro {mode} ready in {time.time() - started:.1f}s\n")
    archive = zipfile.ZipFile(manifest["archive"]) if manifest.get("archive") else None
    items = manifest.get("pages") if archive is not None else manifest.get("images")
    failures = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("w", encoding="utf-8") as output:
            for done, item in enumerate(items or [], start=1):
                key = str(item.get("key"))
                row: dict[str, object] = {"key": key}
                try:
                    data = (
                        archive.read(str(item["name"]))
                        if archive is not None
                        else Path(str(item["path"])).read_bytes()
                    )
                    row.update(run(data))
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    row["error"] = f"{type(exc).__name__}: {exc}"
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                if progress_path is not None:
                    progress_path.write_text(
                        json.dumps({"done": done, "total": len(items)}), encoding="utf-8"
                    )
    finally:
        if archive is not None:
            archive.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
