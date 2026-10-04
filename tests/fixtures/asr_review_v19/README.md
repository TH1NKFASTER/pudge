Caption and ASR word text consists of anonymous synthetic glyphs. No original
dialogue, audio, or original-to-glyph mapping is included. Historical filenames
identify the regression cases only.

`geometry.json.gz` retains the numeric/layout records from snapshot `37cd773`,
with text represented by anonymous Unicode glyph slots. Corresponding slots
share the same glyph across every caption, ASR segment and word. The assignment
preserves normalized equality/inequality, character counts, kana/kanji classes,
wide/narrow glyph widths, punctuation and line breaks. It therefore retains
word-anchor matching and ruby geometry without retaining readable dialogue.

Run `python generate.py` to recreate all seven compressed fixtures, or use
`python generate.py --output /tmp/corpus` to generate them elsewhere. Generation
needs only the public geometry snapshot and is byte-for-byte deterministic.

| Case | Subtitle/layout records | ASR segments | ASR words |
|---|---:|---:|---:|
| Seirei | 489 cues | 400 | 3,680 |
| Solo | 298 SRT cues; 1,319 ASS events | 285 | 2,850 |
| Tokyo | 331 cues | 384 | 3,052 |

Original times, durations, per-word probabilities, metrics, flags, segment
ownership and ASS style/override records are unchanged. Solo conversion retains
1,261 positioned events, removes 468 ruby fragments, joins 495 fragments, and
produces all 298 independently stored captions, including short kanji-only
captions. Seirei yields the original 141 distributed anchors, quarter counts
`[34, 35, 48, 24]`, a -1.162 s median shift, and 0.9362 inlier fraction. Solo's
68 anchors reject its inconsistent clock; Tokyo has no exact word anchors.
Sparse, late-only, oversized-shift and missing-word mutations remain rejected.

Tests independently fingerprint all seven numeric geometries against the
pre-sanitization snapshot and verify behavior through the real conversion and
clock-recovery implementations. These fixtures test timing/layout behavior;
synthetic glyphs are not natural-language or acoustic ground truth.
