"""Generate the synthetic caption/ASR corpus of tests/test_v19_asr_review_repairs.py.

Everything is invented: the vocabulary is made of random syllables and kanji pairs, the
names are made up, and the timings come from a seeded random generator. Nothing is taken
from a real episode. The output is byte-for-byte deterministic (fixed seeds, rounded
numbers, gzip without a timestamp), so the tests can pin it.

Three cases, named after the historical regressions they stand for:

* ``seirei``: one constant clock. The speech in the ASR transcript starts SHIFT seconds
  before the captions (plus small jitter); some captions are timed badly (outliers), some
  are misheard, missing, spoken with low confidence or inside flagged ASR segments, some
  repeat (names, catchphrases), and the ASR has extra speech the captions do not have.
* ``solo``: a piecewise clock (an edit in the video: the speech moves by EDIT_JUMP seconds
  after EDIT_AT). Its captions also come as a positioned ASS file (fragments, ruby
  readings above kanji, outline layers, unpositioned and multi-line events) and as the
  plain SRT the ASS must convert to.
* ``tokyo``: the transcript of another episode of the same invented show: the same names
  and frequent words, but none of these captions is ever spoken.

Run ``python generate.py`` to rewrite the fixtures next to this file, or
``python generate.py --output DIR``. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SHIFT = -1.16  # seirei: the speech starts this long before its caption
SOLO_SHIFT = -0.3  # solo: the clock before the edit
EDIT_AT = 700.0  # solo: the video edit (caption time)
EDIT_JUMP = 2.6  # solo: how far the speech moves at the edit
DURATION = 1425.0  # every case is one ~24-minute episode
WINDOW = 180.0  # the ASR runs in windows of this length

HIRAGANA = (
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわん"
    "がぎぐげござじずぜぞだでどばびぶべぼぱぴぷぺぽ"
)
KATAKANA = "アイウエオカキクケコサシスセソタチツテトナニヌネハヒフヘホマミムメモヤユヨラリルレロワン"
KANJI = (
    "山川海空風火水木金土星雲雨雪花草森林石岩道町村橋門家窓机紙筆歌声音色光影夢心手足目耳顔頭"
    "力気天地王国城塔船車馬鳥魚犬猫虫竜剣盾弓矢旗鐘灯炎氷霧霜雷嵐波砂泥鉄銀銅玉珠宝箱鍵扉壁床柱"
    "庭畑麦米茶塩薬毒兵将軍隊敵友兄弟姉妹父母祖孫店市港湾島岬丘谷峠坂湖沼泉滝畔岸崖洞穴雀鷺楼"
)
OKURIGANA = ("る", "った", "って", "ます", "ない", "ろう", "れば", "い", "く", "した")
PARTICLES = ("の", "が", "を", "に", "は", "と", "で", "も", "へ")
ENDINGS = ("", "", "", "よ", "ね", "か", "ぞ", "わ")
PUNCTUATION = ("。", "。", "。", "！", "？", "…", "")
SOUNDS = ("（足音）", "（物音）", "（ため息）", "（鐘の音）", "（風の音）", "（扉の音）")


def _syllables(rng: random.Random, alphabet: str, low: int, high: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(low, high)))


def lexicon(rng: random.Random, size: int) -> list[str]:
    """Invented words, most frequent first."""
    words: list[str] = []
    seen: set[str] = set()
    while len(words) < size:
        kind = rng.random()
        if kind < 0.40:
            word = rng.choice(KANJI) + rng.choice(KANJI)
        elif kind < 0.50:
            word = rng.choice(KANJI)
        elif kind < 0.70:
            word = rng.choice(KANJI) + rng.choice(OKURIGANA)
        elif kind < 0.90:
            word = _syllables(rng, HIRAGANA, 2, 4)
        else:
            word = _syllables(rng, KATAKANA, 3, 4)
        if word not in seen:
            seen.add(word)
            words.append(word)
    return words


class Show:
    """An invented series: names and a Zipf-distributed vocabulary shared by its episodes."""

    def __init__(self, seed: int) -> None:
        rng = random.Random(seed)
        self.names = ["".join(rng.choice(KATAKANA) for _ in range(rng.randint(3, 4))) for _ in range(5)]
        self.words = lexicon(rng, 900)
        # Zipf-like: the word of rank r is drawn with weight 1 / r^1.07, and the names
        # are among the most frequent tokens.
        self.weights = [1.0 / (rank + 1) ** 1.07 for rank in range(len(self.words))]
        self.name_weights = [8.0, 5.0, 3.0, 2.0, 1.0]

    def token(self, rng: random.Random) -> str:
        if rng.random() < 0.12:
            return rng.choices(self.names, self.name_weights)[0]
        return rng.choices(self.words, self.weights)[0]

    def sentence(self, rng: random.Random) -> list[str]:
        """Spoken tokens of one line: content words with particles between them."""
        tokens: list[str] = []
        for index in range(rng.randint(2, 4)):
            if index:
                tokens.append(rng.choice(PARTICLES))
            tokens.append(self.token(rng))
        ending = rng.choice(ENDINGS)
        if ending:
            tokens.append(ending)
        return tokens


def caption_text(rng: random.Random, tokens: list[str]) -> str:
    """How the caption writes the tokens: final punctuation, sometimes on two lines."""
    text = "".join(tokens)
    if len(tokens) >= 5 and rng.random() < 0.3:
        cut = rng.randrange(2, len(tokens) - 1)
        text = "".join(tokens[:cut]) + "\n" + "".join(tokens[cut:])
    return text + rng.choice(PUNCTUATION)


def episode(show: Show, rng: random.Random, *, special: dict[int, str] | None = None) -> list[dict]:
    """Caption rows: start, end, text, spoken tokens (None: nothing is said), kind."""
    special = special or {}
    rows: list[dict] = []
    t = 1.5 + rng.random() * 2.0
    catchphrases = [show.sentence(rng) for _ in range(3)]
    shouts = [name + "！" for name in show.names[:3]] + ["はい", "えっ？", "ああ"]
    while t < DURATION - 6.0:
        index = len(rows)
        if 90.0 <= t < 180.0:  # the opening song: music marks only
            rows.append(
                {
                    "start": round(t, 3),
                    "end": round(t + 4.0, 3),
                    "text": "♪～",
                    "tokens": None,
                    "kind": "music",
                }
            )
            t += 4.5 + rng.random() * 2.0
            continue
        duration = round(1.0 + rng.random() * 3.6, 3)
        if index in special:
            text, tokens, kind = special[index], None, "special"
        else:
            roll = rng.random()
            if roll < 0.08:
                text, kind = rng.choice(shouts), "shout"
                tokens = [text.rstrip("！？")]
            elif roll < 0.12:
                tokens = list(rng.choice(catchphrases))
                text, kind = "".join(tokens) + "！", "catchphrase"
            elif roll < 0.16:
                text, tokens, kind = rng.choice(SOUNDS), None, "sound"
            elif roll < 0.26:
                tokens = show.sentence(rng)
                speaker = rng.choice(show.names)
                text, kind = f"（{speaker}）" + caption_text(rng, tokens), "labelled"
            else:
                tokens = show.sentence(rng)
                text, kind = caption_text(rng, tokens), "line"
        rows.append(
            {
                "start": round(t, 3),
                "end": round(t + duration, 3),
                "text": text,
                "tokens": tokens,
                "kind": kind,
            }
        )
        if kind == "line" and rng.random() < 0.03 and len(rows) not in special:
            # The same caption twice, touching: one cue broken by extraction.
            rows.append(
                {
                    "start": round(t + duration, 3),
                    "end": round(t + duration + 1.2, 3),
                    "text": text,
                    "tokens": None,
                    "kind": "split",
                }
            )
            duration += 1.2
        pause = rng.random()
        t += duration + (0.1 if pause < 0.35 else 0.3 + rng.random() * (6.0 if pause > 0.93 else 1.4))
    assert all(rows[index]["text"] == text for index, text in special.items()), "move the special captions"
    return rows


def _word_rows(rng: random.Random, tokens: list[str], start: float, span: float) -> list[dict]:
    weights = [len(token) + 0.6 for token in tokens]
    total = sum(weights)
    rows = []
    cursor = start
    for token, weight in zip(tokens, weights):
        length = span * weight / total
        rows.append(
            {
                "start": round(cursor, 3),
                "end": round(cursor + length * 0.92, 3),
                "probability": round(0.80 + rng.random() * 0.199, 4),
                "word": token,
            }
        )
        cursor += length
    return rows


def transcript(
    show: Show,
    rng: random.Random,
    rows: list[dict],
    clock: Callable[[float], float],
    *,
    speech: bool = True,
) -> dict:
    """An ASR payload for the episode whose captions are ``rows``.

    ``clock(t)`` is where the speech of a caption starting at ``t`` begins. With
    ``speech`` false the transcript is of another episode: the same show, other lines.
    """
    spoken: list[tuple[float, list[dict], str]] = []
    for row in rows:
        tokens = row["tokens"]
        if not speech:
            tokens = show.sentence(rng) if row["kind"] in {"line", "labelled", "catchphrase"} else None
        if not tokens:
            continue
        variant = rng.random()
        start = clock(row["start"]) + rng.gauss(0.0, 0.06)
        if speech and row["kind"] == "line" and variant < 0.07:
            # A badly timed caption: the speech is far from where the caption says.
            start += rng.choice((-1.0, 1.0)) * (0.9 + rng.random() * 1.1)
        span = max(0.4, (row["end"] - row["start"]) * (0.65 + rng.random() * 0.25))
        words = _word_rows(rng, list(tokens), start, span)
        kind = "speech"
        if speech and 0.07 <= variant < 0.17 and len(tokens) > 1:
            # Misheard: one word comes out as another word of the show.
            index = rng.randrange(len(tokens))
            words[index]["word"] = show.token(rng)
            words[index]["probability"] = round(0.55 + rng.random() * 0.3, 4)
        elif speech and 0.17 <= variant < 0.22:
            continue  # not recognised at all (music over the line, a whisper)
        elif speech and 0.22 <= variant < 0.27:
            words[0]["probability"] = round(0.25 + rng.random() * 0.3, 4)  # unsure first word
        elif speech and 0.27 <= variant < 0.30:
            words[0]["end"] = round(words[0]["start"] + 0.9 + rng.random() * 0.6, 3)  # dragged onset
        elif 0.30 <= variant < 0.34:
            kind = "repetition"
        elif 0.34 <= variant < 0.36:
            kind = "low"
        spoken.append((float(words[0]["start"]), words, kind))
    # Speech the captions do not have (background voices, a mumbled aside).
    for _ in range(45):
        at = rng.uniform(4.0, DURATION - 4.0)
        spoken.append((at, _word_rows(rng, show.sentence(rng), at, 0.6 + rng.random()), "extra"))
    spoken.sort(key=lambda item: item[0])

    segments = []
    counters: dict[int, int] = {}
    for start, words, kind in spoken:
        window = int(max(0.0, start) // WINDOW)
        number = counters.get(window, 0)
        counters[window] = number + 1
        if kind == "repetition":
            # Whisper looping on one word: flagged, and the recovery must not use it.
            first = words[0]
            words = [
                {
                    **first,
                    "start": round(float(first["start"]) + 0.2 * i, 3),
                    "end": round(float(first["start"]) + 0.2 * i + 0.18, 3),
                }
                for i in range(12)
            ]
        probability = sum(float(word["probability"]) for word in words) / len(words)
        metrics = {
            "avg_logprob": round(math.log(probability) - 0.05 - rng.random() * 0.25, 4),
            "no_speech_prob": round(rng.random() * 0.3, 4),
            "compression_ratio": round(1.2 + rng.random() * 0.6, 4),
            "temperature": 0.0,
        }
        flags = []
        if kind == "repetition":
            metrics["compression_ratio"] = round(2.6 + rng.random() * 0.6, 4)
            flags.append("possible_repetition")
        elif kind == "low":
            metrics["avg_logprob"] = round(-1.1 - rng.random() * 0.4, 4)
            flags.append("low_log_probability")
        elif rng.random() < 0.3:
            metrics["no_speech_prob"] = round(0.6 + rng.random() * 0.25, 4)
            flags.append("high_no_speech_probability")
        end = float(words[-1]["end"])
        if int(end // WINDOW) != window:
            flags.append("crosses_window_boundary")
        segments.append(
            {
                "id": f"w{window:04d}-s{number:04d}",
                "start": round(float(words[0]["start"]), 3),
                "end": round(end, 3),
                "words": words,
                "owned_by_window": True,
                "metrics": metrics,
                "review_flags": flags,
                "text": "".join(str(word["word"]) for word in words),
            }
        )
    return {"segments": segments, "synthetic": True}


def _timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def srt(rows: list[dict]) -> str:
    blocks = [
        f"{index}\n{_timestamp(row['start'])} --> {_timestamp(row['end'])}\n{row['text']}"
        for index, row in enumerate(rows, 1)
    ]
    return "﻿" + "\n\n".join(blocks) + "\n"


# --- the positioned ASS of the solo case ---------------------------------------------------

FONT = 46
NARROW = "（）「」『』。、！？…♪～"
ASS_HEADER = [
    "﻿[Script Info]",
    "ScriptType: v4.00+",
    "Collisions: Normal",
    "ScaledBorderAndShadow: Yes",
    "PlayResX: 960",
    "PlayResY: 540",
    "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
    "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    f"Style: Default,Sans,{FONT},&H00FFFFFF,&H000000FF,&H00000000,&H7F000000,1,0,0,0,100,100,4,0,1,2,2,1,0,0,0,1",
    "",
    "[Events]",
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
]
LINE_BREAK = "\\N"


def _ass_time(seconds: float) -> str:
    centis = int(round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _kind(piece: str) -> str:
    return "narrow" if piece[0] in NARROW else "kanji" if piece[0] in KANJI else "kana"


def _pieces(text: str) -> list[str]:
    """A caption line as the typesetter split it: each bracket or final mark narrow and on
    its own, then runs of kanji and runs of kana."""
    pieces: list[str] = []
    for ch in text:
        if pieces and _kind(ch) != "narrow" and _kind(pieces[-1]) == _kind(ch):
            pieces[-1] += ch
        else:
            pieces.append(ch)
    return pieces


def _reading(rng: random.Random, piece: str) -> str:
    return "".join(rng.choice(HIRAGANA) for _ in range(2 * len(piece)))


def ass(rows: list[dict], rng: random.Random) -> tuple[str, list[dict]]:
    """Positioned ASS events for ``rows`` and the captions they display."""
    events: list[str] = []
    shown: list[dict] = []

    def event(start: str, end: str, body: str, layer: int = 0) -> None:
        events.append(f"Dialogue: {layer},{start},{end},Default,,0000,0000,0000,,{body}")

    for row in rows:
        start, end = _ass_time(row["start"]), _ass_time(row["end"])
        lines = row["text"].split("\n")
        colour = "\\c&H0000FFFF" if rng.random() < 0.3 else ""
        style = rng.random()
        if style < 0.05:
            # A plain caption without a position (a sign, a song mark).
            event(start, end, LINE_BREAK.join(lines))
        elif style < 0.08 and len(lines) == 2:
            # One positioned event holding both lines: not a fragment of anything.
            event(start, end, "{\\pos(192,407)}" + LINE_BREAK.join(lines))
        else:
            top = 497 - 90 * (len(lines) - 1)
            for number, line in enumerate(lines):
                y = top + 90 * number
                x = 192 + 20 * rng.randint(0, 6)
                for piece in _pieces(line):
                    narrow = _kind(piece) == "narrow"
                    scale = "\\fscx50" if narrow else ""
                    event(start, end, f"{{\\pos({x},{y}){colour}{scale}}}{piece}")
                    if not narrow and rng.random() < 0.06:
                        # An outline layer: the same fragment again at the same place.
                        event(start, end, f"{{\\pos({x},{y}){colour}{scale}\\bord4}}{piece}", layer=1)
                    if _kind(piece) == "kanji" and (row["kind"] == "special" or rng.random() < 0.55):
                        # Ruby: a half-size reading above the kanji.
                        reading = _reading(rng, piece)
                        event(start, end, f"{{\\pos({x},{y - 54}){colour}\\fscx50\\fscy50}}{reading}")
                    x += (FONT // 2 if narrow else FONT) * len(piece)
        # The ASS stores centiseconds; the plain SRT shows the same times.
        shown.append(
            {
                "start": int(round(row["start"] * 100)) / 100,
                "end": int(round(row["end"] * 100)) / 100,
                "text": row["text"],
            }
        )
    return "\n".join([*ASS_HEADER, *events]) + "\n", shown


# --- the three cases ----------------------------------------------------------------------


def corpus() -> dict[str, bytes]:
    show = Show(seed=1901)

    rng = random.Random(1902)
    seirei = episode(show, rng)
    seirei_asr = transcript(show, rng, seirei, lambda t: t + SHIFT)

    rng = random.Random(1903)
    # Two regression captions at fixed places: a short kanji-only caption and a bracketed
    # kanji word, both with ruby; neither may disappear with its reading.
    solo = episode(show, rng, special={57: "霜雀。", 253: "「鷺橋灯楼」？"})
    solo_ass, solo_shown = ass(solo, rng)
    solo_asr = transcript(show, rng, solo, lambda t: t + SOLO_SHIFT + (EDIT_JUMP if t >= EDIT_AT else 0.0))

    rng = random.Random(1904)
    tokyo = episode(show, rng)
    tokyo_asr = transcript(show, rng, tokyo, lambda t: t - 0.8, speech=False)

    def payload(data: dict) -> str:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"

    texts = {
        "seirei-source.srt": srt(seirei),
        "seirei-asr.json": payload(seirei_asr),
        "solo-native.srt": srt(solo_shown),
        "solo-original.ass": solo_ass,
        "solo-asr.json": payload(solo_asr),
        "tokyo-source.srt": srt(tokyo),
        "tokyo-asr.json": payload(tokyo_asr),
    }
    return {name: text.encode("utf-8") for name, text in texts.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, data in corpus().items():
        packed = bytearray(gzip.compress(data, mtime=0))
        packed[9] = 3  # gzip header OS byte: fixed (macOS zlib writes 19)
        (args.output / (name + ".gz")).write_bytes(bytes(packed))


if __name__ == "__main__":
    main()
