"""Seeded synthetic episodes with known truth for the subtitle timeline aligner.

Ground truth model
------------------
A "content" timeline holds every block of material either master can contain
(cold open, OP, parts, eyecatch, inserted scenes, ED, preview ...).  Each master
(reference = the video with its embedded EN subtitle, source = the JP subtitle's
master) lays out the blocks it contains back to back, optionally time-scaled
(framerate), and shifted by a base offset.  Dialogue "lines" live inside blocks;
each side derives cues from the lines with its own segmentation, jitter, end
policy, missing lines and one-sided extras (signs, song lyrics, previews).

Truth for a source cue starting at s (source master time) is f(s): map s back
to content time through the block it came from, then forward through the
reference master.  Cues from blocks that the reference master lacks have no
truth and are excluded from the metric.

``evaluate(params, seed, work_dir)`` builds one episode, runs
``align_subtitle_timelines`` and scores every source cue against its truth.
``sample_params(seed)`` draws a random scenario (20 families) for fuzzing.
Used by tests/test_timeline_synthetic.py.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from pudge.subtitles.timeline_alignment import align_subtitle_timelines

# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------


@dataclass
class Block:
    name: str
    length: float
    in_ref: bool = True
    in_src: bool = True
    lines: list = field(default_factory=list)  # (start, end) relative to block start
    kind: str = "dialogue"  # dialogue | song | silent
    c0: float = 0.0
    ref0: float | None = None
    src0: float | None = None


def _geom(rng: random.Random, mean: float) -> int:
    p = 1.0 / max(1.0, mean)
    n = 1
    while rng.random() > p and n < 400:
        n += 1
    return n


def gen_dialogue(rng: random.Random, length: float, mode: str, rhythm_frac: float = 0.0) -> list:
    lines: list[tuple[float, float]] = []
    t = rng.uniform(0.4, 2.5)
    while t < length - 1.0:
        rhythmic = mode == "rhythmic" and rng.random() < rhythm_frac
        if rhythmic:
            n = rng.randint(25, 70)
            period = rng.uniform(1.6, 3.0)
            for _ in range(n):
                dur = period - rng.uniform(0.10, 0.22)
                if t + dur > length - 0.3:
                    break
                lines.append((t, t + dur))
                t += period + rng.gauss(0.0, 0.03)
        else:
            n = _geom(rng, 4.0 if mode == "sparse" else 16.0)
            for _ in range(n):
                dur = min(7.5, max(0.6, rng.lognormvariate(math.log(2.3), 0.42)))
                if t + dur > length - 0.3:
                    break
                lines.append((t, t + dur))
                if mode == "sparse":
                    gap = rng.uniform(2.5, 14.0)
                else:
                    r = rng.random()
                    gap = (
                        rng.uniform(0.05, 0.7)
                        if r < 0.62
                        else rng.uniform(0.7, 3.0)
                        if r < 0.92
                        else rng.uniform(3.0, 8.0)
                    )
                t += dur + gap
        t += rng.uniform(8.0, 40.0) if mode == "sparse" else rng.uniform(1.5, 12.0)
    return lines


def gen_song(rng: random.Random, length: float) -> list:
    lines = []
    t = rng.uniform(2.0, 9.0)
    while t < length - 3.0:
        for _ in range(rng.randint(3, 8)):
            dur = rng.uniform(2.6, 5.6)
            if t + dur > length - 1.0:
                break
            lines.append((t, t + dur))
            t += dur + rng.uniform(0.1, 0.7)
        t += rng.uniform(4.0, 14.0)
    return lines


DEFAULTS = {
    "family": "const",
    "base_offset": 0.0,
    "duration": 1420.0,
    "cold_open": 90.0,  # 0 = no cold open
    "op_len": 89.0,
    "ed_len": 89.0,
    "preview": 25.0,  # 0 = none
    "preview_side": "both",  # both | src_only_sub | ref_only_sub | src_master_only
    "eyecatch": 6.0,
    "mode": "normal",  # normal | rhythmic | sparse
    "rhythm_frac": 0.0,
    "silences": 0,  # long mid-episode silences (both masters)
    # edits: list of {"at": fraction of content | "seconds": content time, "len": X, "side": "src"|"ref", "dialogue": bool}
    "edits": [],
    "op_missing": None,  # None | "src" | "ref"  (master lacking the OP)
    "recap": None,  # None | {"side": "src"|"ref", "len": 60}
    "eyecatch_only": None,  # None | "src" | "ref" (eyecatch present in one master only)
    "scale": 1.0,  # source master time = content * (1/scale): ref = src * scale
    # cue derivation
    "src_split": 0.15,
    "src_merge": 0.04,
    "src_drop": 0.03,
    "ref_split": 0.04,
    "ref_merge": 0.04,
    "ref_drop": 0.03,
    "src_jitter": 0.15,
    "ref_jitter": 0.06,
    "src_end_policy": "chain",  # chain | raw | extend
    "ref_end_policy": "raw",
    "ref_overlap": 0.0,  # probability a ref cue is extended to overlap the next
    "signs": 0,  # number of reference sign cues
    "sign_bursts": 0,  # number of bursts of 3-10 overlapping signs
    "karaoke_ref": False,  # per-syllable karaoke cues in OP/ED on the reference
    "song_src": "none",  # none | op | ed | both : JP ♪ lyric cues
    "song_ref": "none",  # none | op | ed | both : EN lyric cues
    "watermark": False,
}


def _apply_side(rng, lines, *, split, merge, drop, jitter, end_policy, overlap=0.0):
    out = []
    i = 0
    lines = sorted(lines)
    while i < len(lines):
        s, e = lines[i]
        if rng.random() < drop:
            i += 1
            continue
        if i + 1 < len(lines) and lines[i + 1][0] - e < 0.8 and rng.random() < merge:
            e = lines[i + 1][1]
            i += 1
        if e - s >= 1.8 and rng.random() < split:
            u = rng.uniform(0.35, 0.65)
            mid = s + u * (e - s)
            out.append([s, mid - rng.uniform(0.0, 0.1)])
            out.append([mid, e])
        else:
            out.append([s, e])
        i += 1
    for cue in out:
        cue[0] += rng.gauss(0.0, jitter)
        cue[1] += rng.gauss(0.0, jitter * 0.8)
    out.sort()
    for idx, cue in enumerate(out):
        nxt = out[idx + 1][0] if idx + 1 < len(out) else None
        if end_policy == "chain" and nxt is not None and 0 < nxt - cue[1] < 2.5:
            cue[1] = nxt - 0.1
        elif end_policy == "extend":
            cue[1] += rng.uniform(0.2, 0.6)
            if nxt is not None:
                cue[1] = min(cue[1], nxt - 0.05)
        if overlap and nxt is not None and rng.random() < overlap:
            cue[1] = max(cue[1], nxt + rng.uniform(0.3, 2.0))
        if cue[1] - cue[0] < 0.3:
            cue[1] = cue[0] + 0.3
    return out


def build_scenario(params: dict, seed: int):
    p = {**DEFAULTS, **params}
    rng = random.Random(seed)
    blocks: list[Block] = []
    dur = float(p["duration"])
    if p["recap"]:
        r = p["recap"]
        blocks.append(
            Block(
                "recap",
                float(r.get("len", 60.0)),
                in_ref=r["side"] == "ref",
                in_src=r["side"] == "src",
                lines=gen_dialogue(rng, float(r.get("len", 60.0)), "normal"),
            )
        )
    if p["cold_open"]:
        blocks.append(
            Block(
                "cold",
                float(p["cold_open"]),
                lines=gen_dialogue(rng, float(p["cold_open"]), p["mode"], p["rhythm_frac"]),
            )
        )
    blocks.append(Block("title", rng.uniform(2.0, 5.0), kind="silent"))
    op = Block("op", float(p["op_len"]), kind="song", lines=gen_song(rng, float(p["op_len"])))
    if p["op_missing"] == "src":
        op.in_src = False
    elif p["op_missing"] == "ref":
        op.in_ref = False
    blocks.append(op)
    fixed = (
        sum(b.length for b in blocks) + float(p["ed_len"]) + float(p["preview"]) + float(p["eyecatch"]) + 4.0
    )
    body = max(300.0, dur - fixed)
    part_a_len = body * rng.uniform(0.42, 0.52)
    part_b_len = body - part_a_len
    part_a = Block("partA", part_a_len, lines=gen_dialogue(rng, part_a_len, p["mode"], p["rhythm_frac"]))
    part_b = Block("partB", part_b_len, lines=gen_dialogue(rng, part_b_len, p["mode"], p["rhythm_frac"]))
    blocks.append(part_a)
    eye = Block("eyecatch", float(p["eyecatch"]), kind="silent")
    if p["eyecatch_only"] == "src":
        eye.in_ref = False
    elif p["eyecatch_only"] == "ref":
        eye.in_src = False
    if p["eyecatch"]:
        blocks.append(eye)
    blocks.append(part_b)
    blocks.append(Block("gap_ed", rng.uniform(1.0, 4.0), kind="silent"))
    if p["ed_len"]:
        blocks.append(Block("ed", float(p["ed_len"]), kind="song", lines=gen_song(rng, float(p["ed_len"]))))
    if p["preview"]:
        pv = Block("preview", float(p["preview"]), lines=gen_dialogue(rng, float(p["preview"]), "normal"))
        if p["preview_side"] == "src_master_only":
            pv.in_ref = False
        blocks.append(pv)

    # Long silences (both masters) split a part at a gap.
    def split_block(index: int, at: float) -> int:
        b = blocks[index]
        # snap to a line gap >= 0.3 s near `at`
        best = None
        for (s1, e1), (s2, e2) in zip(b.lines, b.lines[1:]):
            if s2 - e1 >= 0.3:
                mid = (e1 + s2) / 2.0
                if best is None or abs(mid - at) < abs(best - at):
                    best = mid
        cut = best if best is not None else at
        cut = min(max(cut, 0.5), b.length - 0.5)
        left = Block(b.name + "_1", cut, b.in_ref, b.in_src, [(s, e) for s, e in b.lines if e <= cut], b.kind)
        right = Block(
            b.name + "_2",
            b.length - cut,
            b.in_ref,
            b.in_src,
            [(s - cut, e - cut) for s, e in b.lines if s >= cut],
            b.kind,
        )
        blocks[index : index + 1] = [left, right]
        return index + 1

    for _ in range(int(p["silences"])):
        idx = max(
            (i for i, b in enumerate(blocks) if b.kind == "dialogue" and b.in_ref and b.in_src),
            key=lambda i: blocks[i].length,
        )
        at = blocks[idx].length * rng.uniform(0.3, 0.7)
        ridx = split_block(idx, at)
        blocks.insert(ridx, Block("silence", rng.uniform(20.0, 60.0), kind="silent"))

    # Picture edits: a chunk present in one master only.
    for k, edit in enumerate(p["edits"]):
        total = sum(b.length for b in blocks)
        target = float(edit["seconds"]) if "seconds" in edit else float(edit["at"]) * total
        # locate block containing target
        acc = 0.0
        idx = len(blocks) - 1
        for i, b in enumerate(blocks):
            if acc + b.length > target:
                idx = i
                break
            acc += b.length
        b = blocks[idx]
        if b.kind == "dialogue" and b.lines:
            ridx = split_block(idx, target - acc)
        else:
            ridx = idx + 1 if target - acc > b.length / 2 else idx
        length = float(edit["len"])
        chunk = Block(
            f"edit{k}",
            length,
            in_ref=edit["side"] == "ref",
            in_src=edit["side"] == "src",
            lines=gen_dialogue(rng, length, "normal") if edit.get("dialogue") else [],
            kind="dialogue" if edit.get("dialogue") else "silent",
        )
        blocks.insert(ridx, chunk)

    # Layout
    c = 0.0
    for b in blocks:
        b.c0 = c
        c += b.length
    ref_t0 = rng.uniform(0.0, 3.0)
    t = ref_t0
    for b in blocks:
        if b.in_ref:
            b.ref0 = t
            t += b.length
    ref_end = t
    scale = float(p["scale"])  # ref = src * scale  =>  src lengths = length / scale
    t = 0.0
    for b in blocks:
        if b.in_src:
            b.src0 = t
            t += b.length / scale
    first_shared = next(b for b in blocks if b.in_ref and b.in_src)
    # choose shift so that at the first shared block ref - src = base_offset
    shift = (first_shared.ref0 - float(p["base_offset"])) - first_shared.src0
    for b in blocks:
        if b.src0 is not None:
            b.src0 += shift

    def ref_of_src(b: Block, s: float) -> float | None:
        if b.ref0 is None or b.src0 is None:
            return None
        rel = (s - b.src0) * scale
        return b.ref0 + rel

    # Cues
    src_cues: list[tuple[float, float, float | None, str]] = []  # (start, end, truth, origin)
    ref_cues: list[tuple[float, float, str]] = []
    src_args = dict(
        split=p["src_split"],
        merge=p["src_merge"],
        drop=p["src_drop"],
        jitter=p["src_jitter"],
        end_policy=p["src_end_policy"],
    )
    ref_args = dict(
        split=p["ref_split"],
        merge=p["ref_merge"],
        drop=p["ref_drop"],
        jitter=p["ref_jitter"],
        end_policy=p["ref_end_policy"],
        overlap=p["ref_overlap"],
    )

    def song_on(flag: str, name: str) -> bool:
        return flag == "both" or flag == name

    for b in blocks:
        if b.kind == "silent":
            continue
        if b.kind == "song":
            src_lines = b.lines if song_on(p["song_src"], b.name) else []
            ref_lines = b.lines if song_on(p["song_ref"], b.name) else []
            sa = dict(src_args, split=0.0, merge=0.0, drop=0.0)
            ra = dict(ref_args, split=0.0, merge=0.0, drop=0.0, overlap=0.0)
        else:
            src_lines = b.lines
            ref_lines = b.lines
            sa, ra = src_args, ref_args
            if b.name.startswith("preview"):
                if p["preview_side"] == "src_only_sub":
                    ref_lines = []
                elif p["preview_side"] == "ref_only_sub":
                    src_lines = []
        if b.in_src and src_lines:
            for s, e in _apply_side(rng, src_lines, **sa):
                ss = b.src0 + s / scale
                ee = b.src0 + e / scale
                src_cues.append((ss, ee, ref_of_src(b, ss), b.name))
        if b.in_ref and ref_lines:
            for s, e in _apply_side(rng, ref_lines, **ra):
                ref_cues.append((b.ref0 + s, b.ref0 + e, "en"))
        if b.kind == "song" and p["karaoke_ref"] and b.in_ref:
            t = b.ref0 + 2.0
            while t < b.ref0 + b.length - 2.0:
                for _ in range(rng.randint(8, 20)):
                    d = rng.uniform(0.15, 0.6)
                    ref_cues.append((t, t + d, "kara"))
                    t += d
                t += rng.uniform(1.0, 6.0)
    # Signs on the reference
    for _ in range(int(p["signs"])):
        s = rng.uniform(ref_t0, ref_end - 5.0)
        ref_cues.append(
            (s, s + rng.uniform(1.0, 7.0) if rng.random() < 0.9 else s + rng.uniform(8.0, 40.0), "sign")
        )
    for _ in range(int(p["sign_bursts"])):
        s0 = rng.uniform(ref_t0, ref_end - 10.0)
        for _k in range(rng.randint(3, 10)):
            s = s0 + rng.uniform(0.0, 2.0)
            ref_cues.append((s, s + rng.uniform(2.0, 8.0), "sign"))
    if p["watermark"]:
        ref_cues.append((0.0, 35999.0, "wm"))

    def r3(x: float) -> float:
        return round(x * 1000.0) / 1000.0

    src = []
    for s, e, tr, origin in src_cues:
        if s < 0.0:
            continue
        s3, e3 = r3(s), r3(max(e, s + 0.3))
        src.append((s3, e3, tr, origin))
    src.sort(key=lambda row: row[0])
    ref = sorted(((r3(max(0.0, s)), r3(e), kind) for s, e, kind in ref_cues if e > 0.0), key=lambda r: r[0])

    clocks = []
    for b in blocks:
        if b.src0 is not None and b.ref0 is not None:
            off = round(b.ref0 - b.src0, 3)
            if not clocks or abs(clocks[-1][1] - off) > 1e-6:
                clocks.append((round(b.src0, 3), off))
    meta = {
        "blocks": [
            (
                b.name,
                round(b.length, 2),
                b.in_ref,
                b.in_src,
                None if b.src0 is None else round(b.src0, 2),
                None if b.ref0 is None else round(b.ref0, 2),
            )
            for b in blocks
        ],
        "true_clocks": clocks,
        "src_count": len(src),
        "ref_count": len(ref),
        "src_end": src[-1][1] if src else 0.0,
    }
    return src, ref, meta, p


def write_srt(path: Path, cues, prefix: str) -> None:
    """cues: (start, end) or (start, end, text_prefix)."""

    def stamp(v: float) -> str:
        ms = int(round(v * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = [
        f"{i}\n{stamp(c[0])} --> {stamp(c[1])}\n{c[2] if len(c) > 2 else prefix} {i}\n"
        for i, c in enumerate(cues, 1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")


# --------------------------------------------------------------------------
# Scenario sampling
# --------------------------------------------------------------------------

FAMILIES = [
    "const",
    "const_small",
    "edit1",
    "edit2",
    "op_change",
    "recap",
    "eyecatch",
    "cold_open_op",
    "ed_songs",
    "preview_one_side",
    "rhythmic",
    "sparse",
    "segmentation",
    "signs",
    "framerate",
    "framerate_ntsc",
    "tail_real",
    "tail_fake",
    "head_real",
    "head_fake",
]


def _offset_ok(clocks_offsets: list[float]) -> bool:
    return all(abs(v) <= 115.0 for v in clocks_offsets)


def sample_params(seed: int, family: str | None = None) -> dict:
    rng = random.Random(seed * 7919 + 17)
    fam = family or FAMILIES[seed % len(FAMILIES)]
    p: dict = {"family": fam}
    p["duration"] = rng.uniform(1360.0, 1470.0)
    p["cold_open"] = rng.choice([0.0, rng.uniform(40.0, 200.0), rng.uniform(40.0, 200.0)])
    p["preview"] = rng.choice([0.0, rng.uniform(18.0, 35.0)])
    p["eyecatch"] = rng.choice([0.0, rng.uniform(4.0, 10.0)])
    p["src_jitter"] = rng.uniform(0.05, 0.3)
    p["src_split"] = rng.uniform(0.05, 0.25)
    p["src_end_policy"] = rng.choice(["chain", "chain", "raw", "extend"])
    p["ref_end_policy"] = rng.choice(["raw", "raw", "extend"])
    p["signs"] = rng.choice([0, 0, rng.randint(5, 40)])
    p["song_src"] = rng.choice(["none", "none", "op", "ed", "both"])
    p["song_ref"] = rng.choice(["none", "none", "none", "both"])
    p["silences"] = rng.choice([0, 0, 1])
    sign = lambda: rng.choice([-1.0, 1.0])  # noqa: E731
    p["base_offset"] = round(rng.uniform(-90.0, 90.0), 2)

    if fam == "const":
        pass
    elif fam == "const_small":
        p["base_offset"] = round(rng.uniform(-1.0, 1.0), 2)
    elif fam == "edit1":
        side = rng.choice(["src", "ref"])
        p["edits"] = [
            {
                "at": rng.uniform(0.12, 0.9),
                "len": round(
                    rng.choice([rng.uniform(1.0, 6.0), rng.uniform(6.0, 30.0), rng.uniform(30.0, 90.0)]), 2
                ),
                "side": side,
                "dialogue": side == "ref" and rng.random() < 0.5,
            }
        ]
    elif fam == "edit2":
        p["edits"] = []
        for at in sorted([rng.uniform(0.1, 0.5), rng.uniform(0.5, 0.92)]):
            side = rng.choice(["src", "ref"])
            p["edits"].append(
                {
                    "at": at,
                    "len": round(rng.choice([rng.uniform(1.0, 6.0), rng.uniform(6.0, 40.0)]), 2),
                    "side": side,
                    "dialogue": side == "ref" and rng.random() < 0.4,
                }
            )
    elif fam == "op_change":
        p["op_missing"] = rng.choice(["src", "ref"])
        p["cold_open"] = rng.choice([0.0, rng.uniform(40.0, 200.0)])
    elif fam == "recap":
        p["recap"] = {"side": rng.choice(["src", "ref"]), "len": rng.uniform(30.0, 90.0)}
    elif fam == "eyecatch":
        p["eyecatch"] = rng.uniform(4.0, 12.0)
        p["eyecatch_only"] = rng.choice(["src", "ref"])
    elif fam == "cold_open_op":
        p["cold_open"] = rng.uniform(40.0, 200.0)
        p["edits"] = [
            {
                "seconds": None,
                "len": round(rng.uniform(1.0, 20.0), 2),
                "side": rng.choice(["src", "ref"]),
                "dialogue": False,
                "after_op": True,
            }
        ]
    elif fam == "ed_songs":
        p["song_src"] = rng.choice(["ed", "both"])
        p["song_ref"] = "none"
        if rng.random() < 0.3:
            p["song_src"], p["song_ref"] = "none", rng.choice(["ed", "both"])
        p["preview"] = rng.uniform(18.0, 35.0)
    elif fam == "preview_one_side":
        p["preview"] = rng.uniform(18.0, 35.0)
        p["preview_side"] = rng.choice(["src_only_sub", "ref_only_sub", "src_master_only"])
    elif fam == "rhythmic":
        p["mode"] = "rhythmic"
        p["rhythm_frac"] = rng.uniform(0.5, 1.0)
        if rng.random() < 0.5:
            p["edits"] = [
                {
                    "at": rng.uniform(0.15, 0.85),
                    "len": round(rng.uniform(2.0, 30.0), 2),
                    "side": rng.choice(["src", "ref"]),
                    "dialogue": False,
                }
            ]
    elif fam == "sparse":
        p["mode"] = "sparse"
        p["silences"] = rng.randint(1, 3)
        if rng.random() < 0.4:
            p["edits"] = [
                {
                    "at": rng.uniform(0.15, 0.85),
                    "len": round(rng.uniform(2.0, 40.0), 2),
                    "side": rng.choice(["src", "ref"]),
                    "dialogue": False,
                }
            ]
    elif fam == "segmentation":
        p["src_split"] = rng.uniform(0.15, 0.4)
        p["src_merge"] = rng.uniform(0.0, 0.15)
        p["src_drop"] = rng.uniform(0.0, 0.12)
        p["ref_drop"] = rng.uniform(0.0, 0.12)
        p["ref_merge"] = rng.uniform(0.0, 0.15)
        p["src_jitter"] = rng.uniform(0.1, 0.5)
        p["ref_overlap"] = rng.choice([0.0, 0.2])
    elif fam == "signs":
        p["signs"] = rng.randint(100, 500)
        p["sign_bursts"] = rng.randint(10, 60)
        p["karaoke_ref"] = rng.random() < 0.5
        p["watermark"] = rng.random() < 0.3
        if rng.random() < 0.4:
            p["edits"] = [
                {
                    "at": rng.uniform(0.15, 0.85),
                    "len": round(rng.uniform(2.0, 30.0), 2),
                    "side": rng.choice(["src", "ref"]),
                    "dialogue": False,
                }
            ]
    elif fam == "framerate":
        p["scale"] = rng.choice([25.0 / 23.976, 23.976 / 25.0])
        p["base_offset"] = rng.choice([0.0, round(rng.uniform(-60.0, 60.0), 2)])
    elif fam == "framerate_ntsc":
        p["scale"] = rng.choice([24.0 / 23.976, 23.976 / 24.0])
        p["base_offset"] = rng.choice([0.0, round(rng.uniform(-60.0, 60.0), 2)])
    elif fam == "tail_real":
        p["edits"] = [
            {
                "tail": rng.uniform(60.0, 120.0),
                "len": round(sign() * rng.uniform(2.0, 30.0), 2),
                "dialogue": False,
            }
        ]
    elif fam == "tail_fake":
        p["song_src"] = rng.choice(["ed", "both"])
        p["song_ref"] = rng.choice(["none", "ed"])
        p["preview"] = rng.uniform(18.0, 35.0)
        p["preview_side"] = rng.choice(["src_only_sub", "ref_only_sub", "both"])
        p["tail_signs"] = rng.randint(5, 25)
    elif fam == "head_real":
        p["cold_open"] = rng.uniform(60.0, 120.0)
        p["edits"] = [{"head": True, "len": round(sign() * rng.uniform(2.0, 30.0), 2), "dialogue": False}]
    elif fam == "head_fake":
        p["cold_open"] = rng.uniform(60.0, 120.0)
        p["head_signs"] = rng.randint(5, 25)
        p["song_src"] = rng.choice(["op", "both"])
    return p


def resolve_params(p: dict, rng_seed: int) -> dict:
    """Turn symbolic edits (tail/head/after_op) into concrete edits."""
    p = json.loads(json.dumps(p))
    edits = []
    for e in p.get("edits", []) or []:
        e = dict(e)
        if "tail" in e:
            length = float(e["len"])
            e = {
                "tail_seconds": float(e["tail"]),
                "len": abs(length),
                "side": "ref" if length > 0 else "src",
                "dialogue": False,
            }
        elif e.get("head") or e.get("after_op"):
            length = float(e["len"])
            side = e.get("side") or ("ref" if length > 0 else "src")
            e = {
                "after_cold" if e.get("head") else "after_op": True,
                "len": abs(length),
                "side": side,
                "dialogue": False,
            }
        edits.append(e)
    p["edits"] = edits
    return p


def build(params: dict, seed: int):
    p = resolve_params(params, seed)
    concrete = []
    # Two-pass: build once without edits to measure content positions for tail/head edits
    if any("tail_seconds" in e or e.get("after_cold") or e.get("after_op") for e in p["edits"]):
        probe_src, probe_ref, probe_meta, _ = build_scenario(
            {**p, "edits": [e for e in p["edits"] if "at" in e or "seconds" in e]}, seed
        )
        total = sum(b[1] for b in probe_meta["blocks"])
        for e in p["edits"]:
            if "tail_seconds" in e:
                concrete.append(
                    {
                        "seconds": total - e["tail_seconds"],
                        "len": e["len"],
                        "side": e["side"],
                        "dialogue": False,
                    }
                )
            elif e.get("after_cold") or e.get("after_op"):
                # after_cold: right after the cold open (before title/OP);
                # after_op: right after the OP (the OP/title differs in length)
                acc = 0.0
                stop = "title" if e.get("after_cold") else "partA"
                for b in probe_meta["blocks"]:
                    if b[0].startswith(stop):
                        break
                    acc += b[1]
                concrete.append(
                    {"seconds": max(0.0, acc - 0.01), "len": e["len"], "side": e["side"], "dialogue": False}
                )
            else:
                concrete.append(e)
        p["edits"] = concrete
    src, ref, meta, full = build_scenario(p, seed)
    # extra one-sided material at the head/tail on the reference (fake clocks)
    rng = random.Random(seed + 99991)
    extra = []
    if p.get("tail_signs"):
        ref_end = max(e for _s, e, _k in ref)
        for _ in range(int(p["tail_signs"])):
            s = rng.uniform(ref_end - 120.0, ref_end - 2.0)
            extra.append((round(s, 3), round(s + rng.uniform(1.0, 5.0), 3), "sign"))
    if p.get("head_signs"):
        ref_start = min(s for s, _e, _k in ref)
        for _ in range(int(p["head_signs"])):
            s = rng.uniform(max(0.0, ref_start - 5.0), ref_start + 120.0)
            extra.append((round(s, 3), round(s + rng.uniform(1.0, 5.0), 3), "sign"))
    if extra:
        ref = sorted(ref + extra, key=lambda r: r[0])
        meta["ref_count"] = len(ref)
    full["edits"] = p["edits"]
    return src, ref, meta, full


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def mapped_offsets(src, segments):
    bounds = [float(seg["source_end"]) for seg in segments[:-1]]
    offs = [float(seg["offset_seconds"]) for seg in segments]
    out = []
    for s, e, _tr, _o in src:
        mid = (s + e) / 2.0
        k = 0
        while k < len(bounds) and mid >= bounds[k]:
            k += 1
        out.append(offs[k])
    return out


def _bad_groups(bad):
    groups: dict = {}
    for s, err, origin, true_off, mapped_off in bad:
        key = (round(true_off, 1), round(mapped_off, 2))
        g = groups.setdefault(
            key, {"true_off": key[0], "mapped_off": key[1], "n": 0, "first": s, "last": s, "origins": {}}
        )
        g["n"] += 1
        g["first"] = min(g["first"], s)
        g["last"] = max(g["last"], s)
        g["origins"][origin.split("_")[0]] = g["origins"].get(origin.split("_")[0], 0) + 1
    return sorted(groups.values(), key=lambda g: -g["n"])


def is_easy(p: dict, meta: dict) -> bool:
    return bool(
        p["family"] in {"const", "const_small"}
        and abs(float(p.get("scale", 1.0)) - 1.0) < 1e-9
        and float(p.get("src_jitter", 0.15)) <= 0.3
        and p.get("mode", "normal") == "normal"
        and meta["src_count"] >= 200
        and int(p.get("signs", 0)) <= 60
        and not p.get("karaoke_ref")
    )


def evaluate(params: dict, seed: int, work_dir: Path, keep: bool = False) -> dict:
    t_start = time.time()
    src, ref, meta, p = build(params, seed)
    work = Path(tempfile.mkdtemp(prefix=f"s{seed}_", dir=work_dir))
    try:
        sp, rp = work / "source.srt", work / "reference.srt"
        write_srt(sp, [(s, e, "♪" if o in ("op", "ed") else "ja") for s, e, _t, o in src], "ja")
        write_srt(rp, [(s, e) for s, e, _k in ref], "en")
        t0 = time.time()
        try:
            _out, res = align_subtitle_timelines(sp, rp, work / "cache", force=True)
        except Exception as exc:  # noqa: BLE001
            return {
                "seed": seed,
                "params": p,
                "meta": meta,
                "crash": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        elapsed = time.time() - t0
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    row: dict = {
        "seed": seed,
        "family": p["family"],
        "params": params,
        "meta": {k: meta[k] for k in ("true_clocks", "src_count", "ref_count", "src_end")},
        "accepted": bool(res.get("accepted")),
        "reason": res.get("reason"),
        "elapsed": round(elapsed, 3),
        "easy": is_easy(p, meta),
    }
    if keep:
        row["work"] = str(work)
    if res.get("accepted"):
        segs = res["timeline_segments"]
        offs = mapped_offsets(src, segs)
        errs = []
        where = []
        for (s, e, tr, origin), off in zip(src, offs):
            if tr is None:
                continue
            err = abs(s + off - tr) if s + off >= 0 else abs(max(0.0, s + off) - tr)
            errs.append(err)
            where.append((round(s, 2), round(err, 2), origin, round(tr - s, 2), off))
        errs_sorted = sorted(errs)
        n = len(errs)
        bad = [w for w in where if w[1] > 1.0]
        row.update(
            {
                "n_eval": n,
                "p50": round(errs_sorted[n // 2], 3) if n else None,
                "p95": round(errs_sorted[min(n - 1, int(0.95 * n))], 3) if n else None,
                "max": round(errs_sorted[-1], 3) if n else None,
                "frac_bad": round(len(bad) / n, 4) if n else 0.0,
                "segments": [(s["source_start"], s["offset_seconds"], s["support"], s["kind"]) for s in segs],
                "risk_required": bool(
                    (res.get("timeline_early_edit_audio_verification") or {}).get("required")
                ),
                "risk_reasons": (res.get("timeline_early_edit_audio_verification") or {}).get("reasons"),
                "guards": sorted(
                    k
                    for k in (
                        "timeline_weak_opening_path_guard",
                        "timeline_transient_path_guard",
                        "timeline_opening_path_boundary_anchor",
                        "timeline_opening_preclock_refinement",
                        "timeline_weak_opening_bridge_guard",
                        "timeline_sparse_edge_guard",
                        "timeline_weak_tail_guard",
                        "timeline_weak_post_opening_tail_guard",
                        "timeline_opening_gap_reacquire",
                        "timeline_cold_start",
                        "false_tail_jump_guard",
                    )
                    if isinstance(res.get(k), dict) and res[k].get("applied")
                ),
                "monotonic": [
                    r.get("reason")
                    for r in res.get("timeline_monotonic_refinements") or []
                    if r.get("applied")
                ],
                "transition": [list(r.keys()) for r in res.get("timeline_transition_refinements") or []],
                "fine_tuning": [r for r in res.get("timeline_segment_fine_tuning") or [] if r.get("applied")],
                "bad_sample": bad[:6],
                "bad_groups": _bad_groups(bad),
                "bad_span": [bad[0][0], bad[-1][0]] if bad else None,
                "validation": {
                    k: res["timeline_validation"]["after"].get(k)
                    for k in ("matched", "coverage", "f1", "mean_error_seconds")
                },
            }
        )
        row["status"] = (
            ("FAIL_SILENT" if not row["risk_required"] else "FAIL_FLAGGED")
            if row["frac_bad"] > 0.03
            else "MARGINAL"
            if row["frac_bad"] > 0.01
            else "OK"
        )
    else:
        row["status"] = "FAIL_EASY_REJECT" if row["easy"] else "REJECT"
        row["reject_detail"] = {
            k: res.get(k)
            for k in (
                "segment_count",
                "mean_score",
                "mean_coverage",
                "activity_f1",
                "onset_count_ratio",
                "previous_start",
                "current_start",
            )
            if k in res
        }
        if "segments" in res:
            row["reject_detail"]["segments"] = res["segments"]
        if "after" in res:
            row["reject_detail"]["after"] = {
                k: res["after"].get(k) for k in ("matched", "coverage", "f1", "mean_error_seconds")
            }
        if "holdout" in res:
            row["reject_detail"]["holdout_p90"] = res["holdout"].get("p90_abs_residual_seconds")
        if "timeline_segments" in res:
            row["reject_detail"]["timeline_segments"] = res["timeline_segments"]
    row["total_elapsed"] = round(time.time() - t_start, 3)
    return row


# --------------------------------------------------------------------------
# Real-fixture perturbations
# --------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"
REAL_FIXTURES = {
    "doro": "timeline_dorohedoro_e05_timings.json",
    "kurumi": "timeline_kurumi_e03_timings.json",
    "rezero": "rezero_ep19_timing_v17.json",
}
# The clocks the unchanged fixtures align to (asserted by their own tests):
# (source time from which the clock holds, offset).
REAL_CLOCKS = {
    "doro": [(0.0, -1.0)],
    "kurumi": [(0.0, -5.0), (1022.5, 1.5)],
    "rezero": [(0.0, -31.5), (253.935, -41.75)],
}


def real_fixture(name: str) -> tuple[list[tuple[float, float, str]], list[tuple[float, float, str]]]:
    """Cue timings of a real episode, each cue with a unique text (anchor texts kept)."""
    data = json.loads((_FIXTURES / REAL_FIXTURES[name]).read_text())

    def cues(rows: list, prefix: str, placeholder: str) -> list[tuple[float, float, str]]:
        return [
            (
                float(row[0]),
                float(row[1]),
                f"{prefix}{i}" if len(row) < 3 or row[2] == placeholder else row[2],
            )
            for i, row in enumerate(rows)
        ]

    return cues(data["source"], "ja", "日本語"), cues(data["reference"], "en", "Translation")


def real_truth(name: str, source: list[tuple[float, float, str]]) -> dict[str, float]:
    """True reference start of every source cue of an unchanged fixture."""
    clocks = REAL_CLOCKS[name]
    truth = {}
    for start, end, text in source:
        offset = [value for since, value in clocks if (start + end) / 2.0 >= since][-1]
        truth[text] = start + offset
    return truth


def _gap_near(cues: list[tuple[float, float, str]], at: float) -> float:
    ordered = sorted(cues)
    gaps = [(a[1] + b[0]) / 2.0 for a, b in zip(ordered, ordered[1:]) if b[0] - a[1] >= 0.4]
    return min(gaps, key=lambda mid: abs(mid - at))


def shift_after_gap(
    cues: list[tuple[float, float, str]], at: float, seconds: float
) -> tuple[list[tuple[float, float, str]], float]:
    """Insert ``seconds`` of picture at the cue gap nearest ``at`` (cues after it move)."""
    cut = _gap_near(cues, at)
    return [(s + seconds, e + seconds, t) if s >= cut else (s, e, t) for s, e, t in cues], cut


def align_cues(
    source: list[tuple[float, float, str]], reference: list[tuple[float, float, str]], work_dir: Path
) -> tuple[dict, dict[str, float]]:
    """Run the aligner on in-memory cues; returns the result and each output cue's start by text."""
    from pudge.subtitle_formats import parse_srt

    def write_exact(path: Path, cues: list[tuple[float, float, str]]) -> None:
        def stamp(v: float) -> str:
            ms = int(round(max(0.0, v) * 1000))
            return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

        rows = sorted(cues, key=lambda cue: cue[0])
        path.write_text(
            "\n".join(f"{i}\n{stamp(s)} --> {stamp(e)}\n{t}\n" for i, (s, e, t) in enumerate(rows, 1)),
            encoding="utf-8",
        )

    work = Path(tempfile.mkdtemp(prefix="real_", dir=work_dir))
    try:
        source_path, reference_path = work / "source.srt", work / "reference.srt"
        write_exact(source_path, source)
        write_exact(reference_path, reference)
        output, result = align_subtitle_timelines(source_path, reference_path, work / "cache", force=True)
        starts = {}
        if result.get("accepted"):
            starts = {text.strip(): start for start, _end, text in parse_srt(output)}
        return result, starts
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cues_off(starts: dict[str, float], truth: dict[str, float], limit: float = 1.0) -> list[str]:
    """Texts of output cues more than ``limit`` seconds from their true start."""
    return [text for text, want in truth.items() if text in starts and abs(starts[text] - want) > limit]


def lyric_lines(
    rng: random.Random, start: float, end: float, text: str = "lyric"
) -> list[tuple[float, float, str]]:
    """Song lyric cues ("♪ text ♪", or the bare "♪～" marker when ``text`` is empty)."""
    out, t, k = [], start + rng.uniform(1.0, 4.0), 0
    while t < end - 3.0:
        for _ in range(rng.randint(3, 8)):
            dur = rng.uniform(2.6, 5.6)
            if t + dur > end - 0.5:
                break
            out.append((round(t, 3), round(t + dur, 3), f"♪ {text}{k} ♪" if text else "♪～"))
            k += 1
            t += dur + rng.uniform(0.1, 0.7)
        t += rng.uniform(4.0, 14.0)
    return out
