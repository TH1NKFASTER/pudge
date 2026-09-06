#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _reexec_with_pudge_runtime_if_needed() -> None:
    """Make ``python3 scripts/...`` work on a stock macOS shell.

    The system interpreter often has no Pillow/ffmpeg-facing Pudge dependencies.
    Prefer the checkout test venv, then the installed Pudge runtime, without
    requiring the user to remember which Python owns the application.
    """
    if importlib.util.find_spec("PIL") is not None:
        return
    if os.environ.get("PUDGE_SUBTITLE_AB_REEXEC") == "1":
        raise RuntimeError(
            "Pudge subtitle A/B benchmark needs Pillow, but the selected Pudge Python does not provide it."
        )
    candidates = [
        PROJECT_ROOT / ".venv-patch-v0725" / "bin" / "python",
        PROJECT_ROOT / ".venv" / "bin" / "python",
        Path.home() / ".local" / "share" / "pudge" / "venv" / "bin" / "python",
    ]
    current = Path(sys.executable).resolve()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            if candidate.resolve() == current:
                continue
            probe = subprocess.run(
                [str(candidate), "-c", "import PIL"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode != 0:
            continue
        env = os.environ.copy()
        env["PUDGE_SUBTITLE_AB_REEXEC"] = "1"
        os.execve(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]], env)
    raise RuntimeError(
        "Could not find a Pudge Python with Pillow. Expected .venv-patch-v0725, .venv, "
        "or ~/.local/share/pudge/venv/bin/python."
    )


_reexec_with_pudge_runtime_if_needed()

from pudge.config import DEFAULT_CONFIG_PATH, load_config
from pudge.database import Database
from pudge.filename import normalize_title, parse_anime_filename, title_similarity
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import parse_srt
from pudge.subtitles.benchmark import evaluate_subtitle_files
from pudge.subtitles.benchmark_corpus import (
    CORPUS_CASE_SCHEMA,
    DIAGNOSTIC_CASE_SCHEMA,
    _stored_replay_media_path,
    _stored_subtitle_candidate,
)
from pudge.syncing import optimize_candidates, subtitle_quality_accepted

TEXT_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "A/B compare Pudge subtitle timing on local episodes and stored benchmark cases: "
            "pre-v138 STT fallback vs the same pipeline plus the STT text clock."
        )
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument("--corpus", type=Path, action="append", default=[], help="benchmark corpus root; repeatable")
    p.add_argument("--search-root", type=Path, action="append", default=[], help="extra root to search for stored benchmark case.json files")
    p.add_argument("--output", type=Path, help="default: ~/Downloads/pudge-subtitle-alignment-ab-<timestamp>")
    p.add_argument("--limit", type=int, default=0, help="0 = all cases")
    p.add_argument("--force", action="store_true", help="ignore A/B method caches (shared Whisper STT is still reused)")
    p.add_argument("--local-only", action="store_true", help="skip benchmark corpora")
    p.add_argument("--benchmark-only", action="store_true", help="skip Library database episodes")
    p.add_argument("--no-open", action="store_true", help="do not reveal the result folder in Finder")
    return p


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _candidate_from_history(db: Database, episode: Any) -> SubtitleCandidate | None:
    video = Path(episode.video_path)
    history = db.latest_selected_subtitle_for_media_or_filename(
        video_path=video, media_id=episode.media_id, episode=episode.episode
    )
    if isinstance(history, dict):
        raw = str(history.get("candidate_path") or "").strip()
        path = Path(raw).expanduser() if raw else None
        if path is not None and path.is_file() and path.suffix.casefold() in TEXT_SUFFIXES:
            details = history.get("details") if isinstance(history.get("details"), dict) else {}
            return SubtitleCandidate(
                path=path,
                source=str(history.get("source") or "history"),
                score=float(history.get("score") or 100.0),
                name=str(history.get("candidate_name") or path.name),
                episode=episode.episode,
                verified_japanese=bool(details.get("verified_japanese")),
                details=dict(details),
            )
    selected = Path(episode.subtitle_path).expanduser() if episode.subtitle_path else None
    if selected is not None and selected.is_file() and selected.suffix.casefold() in TEXT_SUFFIXES:
        return SubtitleCandidate(
            path=selected,
            source=str(episode.subtitle_origin or "selected"),
            score=100.0,
            name=selected.name,
            episode=episode.episode,
            verified_japanese=False,
            details={},
        )
    for suffix in (".srt", ".ass", ".ssa", ".vtt"):
        sibling = video.with_suffix(suffix)
        if sibling.is_file():
            return SubtitleCandidate(
                path=sibling,
                source="sibling",
                score=100.0,
                name=sibling.name,
                episode=episode.episode,
                verified_japanese=False,
                details={},
            )
    return None


def _local_cases(config: Any) -> list[dict[str, Any]]:
    db = Database(config.library.database_path)
    rows: list[dict[str, Any]] = []
    for episode in db.episodes():
        video = Path(episode.video_path).expanduser()
        if not video.is_file():
            continue
        candidate = _candidate_from_history(db, episode)
        if candidate is None:
            continue
        rows.append(
            {
                "kind": "library",
                "id": f"library:{episode.media_id or 0}:{episode.episode or 0}:{video}",
                "title": str(episode.title or video.stem),
                "media_id": episode.media_id,
                "episode": episode.episode,
                "video": video,
                "candidates": [candidate],
                "oracle": None,
                "source": str(candidate.source),
            }
        )
    return rows


def _discover_report_paths(config: Any, explicit_corpora: Iterable[Path], search_roots: Iterable[Path]) -> list[Path]:
    reports: set[Path] = set()
    corpora = [Path(value).expanduser() for value in explicit_corpora]
    roots = [Path(value).expanduser() for value in search_roots]
    roots.extend(
        [
            Path(config.library.root_dir).expanduser(),
            Path(config.paths.cache_dir).expanduser(),
            Path.home() / "Downloads",
        ]
    )
    for corpus in corpora:
        reports.update(corpus.glob("cases/*/case.json"))
        reports.update(corpus.glob("diagnostics/*/diagnostic.json"))
    # Search only for the exact corpus layout; metadata traversal is cheap and no
    # media bytes are read here.
    for root in dict.fromkeys(path.resolve() for path in roots if path.exists()):
        try:
            reports.update(path for path in root.rglob("case.json") if path.parent.parent.name == "cases")
            reports.update(path for path in root.rglob("diagnostic.json") if path.parent.parent.name == "diagnostics")
        except OSError:
            continue
    return sorted(path.resolve() for path in reports)


BENCHMARK_IDENTITY_REJECTIONS: list[dict[str, Any]] = []


def _benchmark_identity_check(
    payload: dict[str, Any],
    replay_media: Path,
    candidate_paths: Iterable[str | Path] = (),
) -> tuple[bool, dict[str, Any]]:
    """Conservative hard gate for stored benchmark video identity.

    This intentionally prefers false negatives over false positives: translated
    and abbreviated release titles are common.  We reject only when episode
    evidence conflicts, a short expected title is merely embedded in another
    longer title (``Another`` vs ``16bit Sensation - Another Layer``), or the
    video title is unrelated and none of the subtitle candidates corroborate it.
    """
    identity = parse_anime_filename(replay_media)
    expected_title = str(payload.get("title") or payload.get("anime_title") or "").strip()
    expected_episode_raw = payload.get("episode")
    try:
        expected_episode = int(expected_episode_raw) if expected_episode_raw is not None else None
    except (TypeError, ValueError):
        expected_episode = None

    candidate_identities = [parse_anime_filename(Path(value).name) for value in candidate_paths]
    candidate_episodes = {int(row.episode) for row in candidate_identities if row.episode is not None}
    candidate_titles = [normalize_title(row.title) for row in candidate_identities if normalize_title(row.title)]
    details: dict[str, Any] = {
        "expected_media_id": payload.get("media_id"),
        "expected_title": expected_title,
        "expected_episode": expected_episode,
        "video": str(replay_media),
        "parsed_title": identity.title,
        "parsed_episode": identity.episode,
        "parsed_season": identity.season,
        "candidate_episode_values": sorted(candidate_episodes),
    }
    if expected_episode is not None and identity.episode is not None and int(identity.episode) != expected_episode:
        # Absolute episode numbering exists in real releases. Reject only when
        # subtitle-candidate evidence explicitly supports the stored episode and
        # does not also support the video's conflicting number.
        if expected_episode in candidate_episodes and int(identity.episode) not in candidate_episodes:
            details["reason"] = "episode_identity_mismatch"
            return False, details

    expected_norm = normalize_title(expected_title)
    parsed_norm = normalize_title(identity.title)
    generic_title = bool(
        not parsed_norm
        or re.match(r"^s\d{1,2}e\d{1,4}(?:\b|\s)", parsed_norm, flags=re.IGNORECASE)
        or re.match(r"^(?:episode|ep|e)\s*\d+\b", parsed_norm, flags=re.IGNORECASE)
    )
    details["title_identity_available"] = not generic_title
    if not expected_norm or generic_title:
        details["reason"] = "episode_identity_only" if generic_title else "no_expected_title"
        return True, details

    expected_tokens = expected_norm.split()
    parsed_tokens = parsed_norm.split()
    expected_phrase_match = (
        parsed_norm == expected_norm
        or parsed_norm.startswith(expected_norm + " ")
        or parsed_norm.endswith(" " + expected_norm)
        or (len(expected_tokens) >= 2 and f" {expected_norm} " in f" {parsed_norm} ")
    )
    overlap = len(set(expected_tokens) & set(parsed_tokens)) / max(1, len(set(expected_tokens)))
    similarity = title_similarity(expected_title, identity.title)
    video_supported_by_candidate = any(
        candidate == parsed_norm
        or candidate.startswith(parsed_norm + " ")
        or parsed_norm.startswith(candidate + " ")
        for candidate in candidate_titles
        if len(candidate) >= 3
    )
    details["normalized_expected_title"] = expected_norm
    details["normalized_parsed_title"] = parsed_norm
    details["title_token_overlap"] = round(overlap, 4)
    details["title_similarity"] = round(float(similarity), 2)
    details["video_title_supported_by_candidate"] = video_supported_by_candidate

    # The classic false-positive shape from token-set matching: the entire
    # expected one-word title occurs inside a different, longer series title.
    if (
        len(expected_tokens) == 1
        and parsed_norm != expected_norm
        and expected_norm in parsed_tokens
        and len(parsed_tokens) >= 3
    ):
        details["reason"] = "title_identity_mismatch"
        return False, details

    title_ok = (
        expected_phrase_match
        or similarity >= 85.0
        or video_supported_by_candidate
        or (overlap >= 0.60 and similarity >= 65.0)
    )
    if not title_ok:
        details["reason"] = "title_identity_mismatch"
        return False, details
    details["reason"] = "identity_ok"
    return True, details


def _benchmark_cases(config: Any, explicit_corpora: Iterable[Path], search_roots: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    BENCHMARK_IDENTITY_REJECTIONS.clear()
    for report_path in _discover_report_paths(config, explicit_corpora, search_roots):
        payload = _json(report_path)
        if payload is None or str(payload.get("schema") or "") not in {CORPUS_CASE_SCHEMA, DIAGNOSTIC_CASE_SCHEMA}:
            continue
        compact = payload.get("compact_media") if isinstance(payload.get("compact_media"), dict) else {}
        media = Path(str(compact.get("path") or "")).expanduser()
        if not media.is_file():
            continue
        replay_media, _preserved = _stored_replay_media_path(report_path, payload, media)
        candidate_rows = [row for row in payload.get("candidates") or [] if isinstance(row, dict)]
        candidates = [value for row in candidate_rows if (value := _stored_subtitle_candidate(row)) is not None]
        if not candidates:
            continue
        identity_ok, identity = _benchmark_identity_check(
            payload, replay_media, [candidate.path for candidate in candidates]
        )
        if not identity_ok:
            BENCHMARK_IDENTITY_REJECTIONS.append({"report_path": str(report_path), **identity})
            continue
        oracle = report_path.parent / "oracle.srt"
        if not oracle.is_file():
            oracle = None
        rows.append(
            {
                "kind": "benchmark",
                "id": f"benchmark:{report_path}",
                "title": str(payload.get("title") or payload.get("anime_title") or replay_media.stem),
                "media_id": payload.get("media_id"),
                "episode": payload.get("episode"),
                "video": replay_media,
                "candidates": candidates,
                "oracle": oracle,
                "report_path": report_path,
                "benchmark_identity": identity,
                "source": "benchmark",
            }
        )
    return rows


def _case_key(case: dict[str, Any]) -> str:
    raw = f"{case['id']}\0{case['video']}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _link_shared_stt(method_cache: Path, shared: Path) -> None:
    method_cache.mkdir(parents=True, exist_ok=True)
    shared.mkdir(parents=True, exist_ok=True)
    target = method_cache / "stt"
    if target.exists() or target.is_symlink():
        return
    try:
        target.symlink_to(shared, target_is_directory=True)
    except OSError:
        # Symlinks can be unavailable on unusual filesystems.  The comparison
        # remains correct; it may simply transcribe twice.
        pass


def _run_method(case: dict[str, Any], config: Any, cache: Path, *, text_clock: bool, force: bool) -> dict[str, Any]:
    method_config = replace(config.sync, japanese_stt_text_clock=bool(text_clock))
    started = time.monotonic()
    try:
        selected, final_path, result = optimize_candidates(
            Path(case["video"]),
            list(case["candidates"]),
            cache,
            method_config,
            ffmpeg_path=config.tools.ffmpeg,
            ffprobe_path=config.tools.ffprobe,
            alass_path=config.tools.alass,
            force=force,
            verbose=False,
            prefer_srt=config.matching.prefer_srt,
            srt_tolerance_ratio=config.matching.srt_alignment_tolerance_ratio,
            srt_tolerance_absolute=config.matching.srt_alignment_tolerance_absolute,
        )
        accepted, quality_reason = subtitle_quality_accepted(result)
        return {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "accepted": bool(accepted),
            "quality_reason": quality_reason,
            "selected_name": selected.name if selected is not None else None,
            "selected_path": str(selected.path) if selected is not None else None,
            "final_path": str(final_path) if final_path is not None else None,
            "result": result,
        }
    except Exception as exc:
        return {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _timing_diff(old_path: str | None, new_path: str | None) -> dict[str, Any]:
    if not old_path or not new_path:
        return {"comparable": False, "reason": "missing_output"}
    left, right = Path(old_path), Path(new_path)
    if not left.is_file() or not right.is_file() or left.suffix.casefold() != ".srt" or right.suffix.casefold() != ".srt":
        return {"comparable": False, "reason": "non_srt_or_missing"}
    try:
        old = parse_srt(left)
        new = parse_srt(right)
    except OSError as exc:
        return {"comparable": False, "reason": "read_error", "error": str(exc)}
    if len(old) != len(new):
        return {"comparable": True, "same_structure": False, "old_cues": len(old), "new_cues": len(new)}
    deltas: list[float] = []
    changed = 0
    text_changed = 0
    for a, b in zip(old, new):
        ds = abs(float(a[0]) - float(b[0]))
        de = abs(float(a[1]) - float(b[1]))
        deltas.extend((ds, de))
        if max(ds, de) > 0.04:
            changed += 1
        if str(a[2]).strip() != str(b[2]).strip():
            text_changed += 1
    ordered = sorted(deltas)
    p95 = ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))] if ordered else 0.0
    return {
        "comparable": True,
        "same_structure": True,
        "cues": len(old),
        "changed_cues": changed,
        "text_changed_cues": text_changed,
        "max_abs_timing_delta_seconds": round(max(deltas, default=0.0), 4),
        "p95_abs_timing_delta_seconds": round(p95, 4),
    }


def _oracle_report(path: str | None, oracle: Path | None) -> dict[str, Any] | None:
    if not path or oracle is None or not oracle.is_file():
        return None
    candidate = Path(path)
    if not candidate.is_file() or candidate.suffix.casefold() != ".srt":
        return None
    try:
        return evaluate_subtitle_files(candidate, oracle).as_dict()
    except Exception as exc:
        return {"evaluable": False, "reason": f"evaluation_error:{type(exc).__name__}"}


def _classify(old: dict[str, Any], new: dict[str, Any], timing: dict[str, Any], old_oracle: dict[str, Any] | None, new_oracle: dict[str, Any] | None) -> tuple[bool, str]:
    changed = any(
        old.get(key) != new.get(key)
        for key in ("accepted", "quality_reason", "selected_name")
    )
    changed = changed or bool(timing.get("comparable") and (not timing.get("same_structure", True) or int(timing.get("changed_cues") or 0) > 0))
    old_result = old.get("result") if isinstance(old.get("result"), dict) else {}
    new_result = new.get("result") if isinstance(new.get("result"), dict) else {}
    changed = changed or any(
        old_result.get(key) != new_result.get(key)
        for key in ("engine", "reason", "selection_reason")
    )
    if old_oracle and new_oracle and old_oracle.get("evaluable") and new_oracle.get("evaluable"):
        old_good, new_good = bool(old_oracle.get("good_alignment")), bool(new_oracle.get("good_alignment"))
        if new_good and not old_good:
            return changed, "improved"
        if old_good and not new_good:
            return changed, "worse"
        try:
            old_p90 = float(old_oracle.get("p90_abs_error_seconds"))
            new_p90 = float(new_oracle.get("p90_abs_error_seconds"))
        except (TypeError, ValueError):
            old_p90 = new_p90 = 0.0
        if old_p90 - new_p90 >= 0.25:
            return changed, "improved"
        if new_p90 - old_p90 >= 0.25:
            return changed, "worse"
    return changed, "changed" if changed else "same"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "kind", "title", "media_id", "episode", "video", "classification",
        "old_accepted", "new_accepted", "old_engine", "new_engine",
        "old_reason", "new_reason", "old_selected", "new_selected",
        "changed_cues", "p95_delta_s", "max_delta_s", "old_oracle_p90_s", "new_oracle_p90_s",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            old, new = row["old"], row["new"]
            old_result = old.get("result") if isinstance(old.get("result"), dict) else {}
            new_result = new.get("result") if isinstance(new.get("result"), dict) else {}
            timing = row.get("timing_diff") or {}
            oo, no = row.get("old_oracle") or {}, row.get("new_oracle") or {}
            writer.writerow({
                "kind": row.get("kind"), "title": row.get("title"), "media_id": row.get("media_id"),
                "episode": row.get("episode"), "video": row.get("video"), "classification": row.get("classification"),
                "old_accepted": old.get("accepted"), "new_accepted": new.get("accepted"),
                "old_engine": old_result.get("engine"), "new_engine": new_result.get("engine"),
                "old_reason": old_result.get("reason"), "new_reason": new_result.get("reason"),
                "old_selected": old.get("selected_name"), "new_selected": new.get("selected_name"),
                "changed_cues": timing.get("changed_cues"), "p95_delta_s": timing.get("p95_abs_timing_delta_seconds"),
                "max_delta_s": timing.get("max_abs_timing_delta_seconds"),
                "old_oracle_p90_s": oo.get("p90_abs_error_seconds"), "new_oracle_p90_s": no.get("p90_abs_error_seconds"),
            })


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_config(args.config)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output = (args.output or (Path.home() / "Downloads" / f"pudge-subtitle-alignment-ab-{stamp}")).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / "run.log"

    cases: list[dict[str, Any]] = []
    if not args.benchmark_only:
        cases.extend(_local_cases(config))
    if not args.local_only:
        cases.extend(_benchmark_cases(config, args.corpus, args.search_root))
    # Dedupe exact video/candidate sets when a benchmark file is also registered in Library.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for case in cases:
        key = (str(Path(case["video"]).resolve()), tuple(sorted(str(Path(c.path).resolve()) for c in case["candidates"])))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(case)
    cases = deduped[: args.limit or None]

    results: list[dict[str, Any]] = []
    with log_path.open("w", encoding="utf-8") as log:
        print(f"Cases: {len(cases)}", file=log, flush=True)
        for index, case in enumerate(cases, 1):
            key = _case_key(case)
            case_root = output / "cache" / key
            shared_stt = case_root / "shared-stt"
            old_cache = case_root / "old"
            new_cache = case_root / "new"
            _link_shared_stt(old_cache, shared_stt)
            _link_shared_stt(new_cache, shared_stt)
            line = f"[{index}/{len(cases)}] {case['kind']} {case.get('title')} E{case.get('episode')} :: {Path(case['video']).name}"
            print(line)
            print(line, file=log, flush=True)
            old = _run_method(case, config, old_cache, text_clock=False, force=args.force)
            new = _run_method(case, config, new_cache, text_clock=True, force=args.force)
            timing = _timing_diff(old.get("final_path"), new.get("final_path"))
            old_oracle = _oracle_report(old.get("final_path"), case.get("oracle"))
            new_oracle = _oracle_report(new.get("final_path"), case.get("oracle"))
            changed, classification = _classify(old, new, timing, old_oracle, new_oracle)
            row = {
                "id": case["id"], "kind": case["kind"], "title": case.get("title"),
                "media_id": case.get("media_id"), "episode": case.get("episode"),
                "video": str(case["video"]), "source": case.get("source"),
                "candidate_paths": [str(c.path) for c in case["candidates"]],
                "report_path": str(case.get("report_path") or ""),
                "old": old, "new": new, "timing_diff": timing,
                "old_oracle": old_oracle, "new_oracle": new_oracle,
                "different": changed, "classification": classification,
            }
            results.append(row)
            print(f"    => {classification} timing={timing}", file=log, flush=True)

    differences = [row for row in results if row["different"]]
    counts = {name: sum(1 for row in results if row["classification"] == name) for name in ("same", "changed", "improved", "worse")}
    summary = {
        "schema": "pudge-subtitle-alignment-ab-v1",
        "created_at": time.time(),
        "cases": len(results),
        "differences": len(differences),
        "counts": counts,
        "old_method": "production pipeline with Japanese STT fallback, STT text clock disabled (pre-v138 behavior)",
        "new_method": "same production pipeline with STT text clock enabled",
        "output": str(output),
    }
    (output / "all-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "differences.json").write_text(json.dumps(differences, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output / "differences.csv", differences)
    _write_csv(output / "all-results.csv", results)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Log: {log_path}")
    if sys.platform == "darwin" and not args.no_open:
        subprocess.run(["open", "-R", str(log_path)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
