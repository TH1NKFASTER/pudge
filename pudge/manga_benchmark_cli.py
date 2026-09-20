from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from .manga109_benchmark import (
    load_manga109_pages,
    make_pudge_full_page_runner,
    run_manga109_benchmark,
)
from .manga_benchmark import compare_reports, load_json, write_json
from .manga_jmangabench import mangaocr_predictor, run_jmanga_predictions, wrap_jmanga_report
from .manga_review_diff import write_review_diff_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pudge.manga_benchmark_cli")
    sub = parser.add_subparsers(dest="command", required=True)

    jmanga = sub.add_parser("jmanga-predict", help="write upstream-compatible JMangaBench predictions")
    jmanga.add_argument("--benchmark", type=Path, required=True)
    jmanga.add_argument("--output", type=Path, required=True)
    jmanga.add_argument("--limit", type=int)
    jmanga.add_argument("--progress-every", type=int, default=25)

    wrap = sub.add_parser(
        "jmanga-wrap-report",
        help="wrap an upstream JMangaBench report with Pudge metadata",
    )
    wrap.add_argument("--report", type=Path, required=True)
    wrap.add_argument("--output", type=Path, required=True)
    wrap.add_argument("--predictions", type=Path)
    wrap.add_argument("--benchmark-root", type=Path)

    manga109 = sub.add_parser("manga109", help="run Pudge full-page OCR against Manga109 annotations")
    manga109.add_argument("--images-root", type=Path, required=True)
    manga109.add_argument("--annotations-root", type=Path, required=True)
    manga109.add_argument("--output-dir", type=Path, required=True)
    manga109.add_argument("--python", default=str(Path.home() / ".local/share/pudge/venv/bin/python"))
    manga109.add_argument("--cache-dir", type=Path, default=Path.home() / "Library/Caches/pudge-benchmark")
    manga109.add_argument("--book", action="append", default=[])
    manga109.add_argument("--max-pages", type=int)
    manga109.add_argument("--iou-threshold", type=float, default=0.5)

    compare = sub.add_parser("compare", help="compare two frozen Pudge benchmark reports")
    compare.add_argument("old", type=Path)
    compare.add_argument("new", type=Path)
    compare.add_argument("--output", type=Path)

    review_diff = sub.add_parser("review-diff", help="compare two Pudge manga OCR review directories")
    review_diff.add_argument("old", type=Path)
    review_diff.add_argument("new", type=Path)
    review_diff.add_argument("--output-dir", type=Path, required=True)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.command == "jmanga-predict":
        result = run_jmanga_predictions(
            args.benchmark,
            args.output,
            mangaocr_predictor(),
            limit=args.limit,
            progress_every=args.progress_every,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["errors"] == 0 else 1
    if args.command == "jmanga-wrap-report":
        result = wrap_jmanga_report(
            args.report,
            args.output,
            predictions_path=args.predictions,
            benchmark_root=args.benchmark_root,
        )
        print(json.dumps(result.get("metrics") or {}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "manga109":
        if sys.platform != "darwin":
            raise RuntimeError("Manga109 full-page Pudge benchmark requires macOS Apple Vision")
        pages = load_manga109_pages(
            args.images_root,
            args.annotations_root,
            books=set(args.book) or None,
            max_pages=args.max_pages,
        )
        if not pages:
            raise RuntimeError("No Manga109 text pages matched the requested filters")
        runner = make_pudge_full_page_runner(python=args.python, cache_dir=args.cache_dir)
        report = run_manga109_benchmark(
            pages,
            args.output_dir,
            recognize_page=runner,
            iou_threshold=args.iou_threshold,
        )
        print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
        return 0
    if args.command == "compare":
        result = compare_reports(load_json(args.old), load_json(args.new))
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "review-diff":
        result = write_review_diff_report(args.old, args.new, args.output_dir)
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        return 0
    raise AssertionError(args.command)


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports benchmark failures uniformly
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
