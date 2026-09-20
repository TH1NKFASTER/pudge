# Manga OCR Standardized Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add standardized JMangaBench_Mixed recognition benchmarking and Manga109-v2026 full-page detection+recognition benchmarking to Pudge.

**Architecture:** Keep benchmark logic isolated from the reader: one common metrics/schema module, one JMangaBench adapter, one Manga109 parser/evaluator, and one CLI. Production OCR is reused through MangaOCR for crop recognition and MangaService's existing Vision + worker path for full pages.

**Tech Stack:** Python 3.11+, Pillow, rapidfuzz, stdlib argparse/json/xml, existing Pudge MangaService/MangaOCR runtime.

**Spec:** `docs/superpowers/specs/2026-09-16-manga-ocr-benchmark-design.md`

## Global Constraints

- No benchmark image pixels or private Manga109 data may be committed or included in patch ZIPs.
- Default external benchmark root is `~/Downloads/pudge-bench-data/`.
- JMangaBench scoring remains upstream-authoritative; Pudge writes compatible predictions and wrappers only.
- Full-page benchmark default IoU threshold is exactly `0.50`.
- Tests must run without Apple Vision, MangaOCR weights, network, or private datasets.

---

### Task 1: Common benchmark schema and metrics

**Files:**
- Create: `pudge/manga_benchmark.py`
- Test: `tests/test_manga_benchmark.py`

**Interfaces:**
- Produces: `normalize_text(str) -> str`, `edit_distance(str, str) -> int`, `box_iou(dict, dict) -> float`, `greedy_match(...)`, `benchmark_metadata(...)`, `compare_reports(...)`.

- [ ] Write failing tests for normalization, IoU, one-to-one greedy matching, CER aggregation, and incompatible report comparison.
- [ ] Run the focused test and verify RED.
- [ ] Implement minimal deterministic helpers and frozen schema metadata.
- [ ] Run focused test and verify GREEN.

### Task 2: JMangaBench_Mixed predictor adapter

**Files:**
- Create: `pudge/manga_jmangabench.py`
- Extend: `tests/test_manga_benchmark.py`

**Interfaces:**
- Produces: `run_jmanga_predictions(benchmark_root, output, predict, limit=None, progress_every=25)` and `wrap_jmanga_report(...)`.

- [ ] Write failing test using two fake benchmark images and a fake predictor.
- [ ] Verify RED.
- [ ] Implement canonical annotations ordering/output contract and no-overwrite guard.
- [ ] Add wrapper metadata around upstream `report.json` without reimplementing its metrics.
- [ ] Verify GREEN.

### Task 3: Manga109 parser + full-page evaluator

**Files:**
- Create: `pudge/manga109_benchmark.py`
- Extend: `tests/test_manga_benchmark.py`

**Interfaces:**
- Produces: `load_manga109_pages(...)`, `evaluate_page(...)`, `run_manga109_benchmark(...)`, `run_pudge_full_page(image_path, python, cache_dir)`.

- [ ] Write failing XML parsing test including top-left → Pudge bottom-left coordinate conversion.
- [ ] Write failing matching/metrics test with matched, missed, and false-positive regions.
- [ ] Verify RED.
- [ ] Implement parser, deterministic page/image resolution, IoU matching, metrics, JSON + JSONL outputs.
- [ ] Implement production backend using `MangaService._vision_text_regions`, `_ocr_regions`, and `_finalize_recognized_regions`.
- [ ] Verify GREEN.

### Task 4: CLI, Makefile, docs

**Files:**
- Create: `pudge/manga_benchmark_cli.py`
- Modify: `Makefile`
- Create: `docs/MANGA_OCR_BENCHMARK.md`
- Extend: `tests/test_manga_benchmark.py`

**Interfaces:**
- Commands: `jmanga-predict`, `jmanga-wrap-report`, `manga109`, `compare`.

- [ ] Write failing parser/CLI smoke tests.
- [ ] Verify RED.
- [ ] Implement argparse CLI and stable output directories.
- [ ] Add Makefile targets using `PUDGE_BENCH_DATA ?= $(HOME)/Downloads/pudge-bench-data`.
- [ ] Document authorized data setup and upstream JMangaBench build/evaluate commands.
- [ ] Verify GREEN.

### Task 5: Verification and patch packaging

**Files:**
- Package all changed source/tests/docs/Makefile in installer ZIP.

- [ ] Run `python -m pytest -q tests/test_manga_benchmark.py`.
- [ ] Run current manga contract suite.
- [ ] Run `make test-batches BATCHES=4` if environment supports authoritative dependencies; otherwise put it in installer.
- [ ] Run `make quality` if environment supports authoritative dependencies; otherwise put it in installer.
- [ ] Run `python -m py_compile` on new modules.
- [ ] Run `git diff --check` equivalent where possible and validate shell scripts with `bash -n`.
- [ ] Build installer/reviewer-independent benchmark patch ZIP + handoff.
