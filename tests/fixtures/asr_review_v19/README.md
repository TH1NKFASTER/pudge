A synthetic corpus: every caption, ASR word, name and timing here was produced by
`generate.py` from fixed random seeds. The vocabulary is invented (random kana
syllables, kanji pairs and katakana names, drawn with Zipf-like frequencies), so no
line of any real episode is included, in any encoding. The file names only recall the
historical regression each case stands for.

Run `python generate.py` to recreate all seven compressed fixtures, or
`python generate.py --output /tmp/corpus` to generate them elsewhere. It needs only the
Python standard library and is byte-for-byte deterministic (the gzip header is fixed).

| Case | Captions | ASR segments | ASR words | What it is |
|---|---:|---:|---:|---|
| Seirei | 376 cues | 360 | 1,997 | one constant speech clock: speech 1.16 s before the captions |
| Solo | 392 SRT cues; 1,971 ASS events | 381 | 2,105 | a piecewise clock: the speech moves by 2.6 s at a video edit (700 s) |
| Tokyo | 379 cues | 354 | 1,983 | the transcript of another episode of the same show |

Each episode is ~24 minutes (1,425 s) with an opening song (90-180 s, music marks only).
Captions include speaker labels, sound descriptions, shouted names and catchphrases that
repeat, two-line captions and touching duplicates. The ASR side has 60 ms onset jitter,
badly timed captions (0.9-2 s off), misheard words, lines it did not recognise, unsure or
dragged first words, looping segments flagged `possible_repetition`, segments flagged
`low_log_probability` or `high_no_speech_probability`, segments crossing a 180 s window,
and extra speech the captions do not have.

The solo ASS positions every caption fragment (brackets and final marks half-width),
puts half-size ruby readings above kanji runs, repeats some fragments on an outline
layer, and keeps some captions unpositioned or as one positioned two-line event; its SRT
is the caption stream the ASS must convert to. Two regression captions sit at fixed
places: a short kanji-only caption (57) and a bracketed kanji word (253), both with ruby.

What the tests check (tests/test_v19_asr_review_repairs.py): the solo ASS converts to all
392 captions (1,946 positioned events, 272 readings removed, 1,242 fragments joined); seirei
yields 174 distributed anchors, quarter counts `[30, 44, 47, 53]`, a -1.159 s median shift
and a 0.9195 inlier fraction, and is recovered; solo's 160 anchors are rejected as two
clocks; tokyo's 3 chance matches are too few; sparse, late-only, oversized-shift and
missing-word mutations are rejected. The tests also pin the generated geometry (times,
metrics, flags, ASS fields and tags). These fixtures test timing and layout behaviour; the
text is not natural language or acoustic ground truth.

The earlier corpus here (a glyph-substituted copy of real captions) was replaced;
tests/test_no_copyrighted_text.py keeps its files from coming back.
