"""The wrong-audio gate measures anchors over the text the audio reads (behaviour).

It divided the anchor count by the whole book: one volume's audio against a
6-volume omnibus (合本), or an audiobook of part of a book, was refused as "reads
another book" although it matched densely where it reads. The density is now taken
over the matched span (stray anchors trimmed); audio of an unrelated book, or of
another volume of the same series, spreads chance anchors over the whole span and
is still refused.
"""

from __future__ import annotations

import random

import pytest

from pudge.i18n import err
from pudge.reading_audio_alignment import MIN_ANCHORS_PER_100_CHARACTERS, align_light_novel_to_transcript

NAMES = ["ハルヒ", "キョン", "長門", "朝比奈", "古泉", "鶴屋", "谷口", "国木田"]
KANJI = [chr(code) for code in range(0x4E00, 0x4E00 + 3000)]
KANA = [chr(code) for code in range(0x3042, 0x3093)]


def _vocabulary(seed: int) -> tuple[list[str], list[str]]:
    rng = random.Random(seed)
    nouns = ["".join(rng.sample(KANJI, 2)) for _ in range(800)]
    verbs = ["".join(rng.sample(KANJI, 1)) + "".join(rng.sample(KANA, 2)) + "った" for _ in range(120)]
    return nouns, verbs


def _volume(seed: int, series: int) -> list[dict]:
    """Four chapters; volumes of one series share names, nouns and verbs."""
    nouns, verbs = _vocabulary(series)
    rng = random.Random(seed * 7919 + 1)

    def sentence() -> str:
        return f"{rng.choice(NAMES)}は{rng.choice(nouns)}で{rng.choice(NAMES)}と{rng.choice(nouns)}について{rng.choice(verbs)}。"

    return [
        {
            "chapter_index": index,
            "title": f"第{index + 1}章",
            "text": "\n".join(sentence() for _ in range(120)),
        }
        for index in range(4)
    ]


def _speech(chapters: list[dict], noise: float) -> tuple[list[dict], float]:
    """The chapters read aloud, with `noise` of the characters misrecognized."""
    rng = random.Random(5)
    segments, clock = [], 0.0
    for chapter in chapters:
        for line in chapter["text"].splitlines():
            heard = []
            for character in line:
                roll = rng.random()
                if roll < noise * 0.6:
                    heard.append(rng.choice(KANA))
                elif roll < noise * 0.8:
                    continue
                elif roll < noise:
                    heard.append(character + rng.choice(KANA))
                else:
                    heard.append(character)
            seconds = len(line) / 8.0
            segments.append({"start": clock, "end": clock + seconds, "text": "".join(heard)})
            clock += seconds + 0.3
    return segments, clock


def _align(book: list[dict], speech: tuple[list[dict], float]) -> dict:
    segments, duration = speech
    return align_light_novel_to_transcript(
        book, segments, duration=duration, model="x", minimum_anchor_density=MIN_ANCHORS_PER_100_CHARACTERS
    )


SERIES = [_volume(10 + number, series=1) for number in range(6)]


@pytest.mark.parametrize("noise", [0.0, 0.5])
def test_one_volumes_audio_aligns_to_its_part_of_an_omnibus(noise: float) -> None:
    omnibus = [
        {**chapter, "chapter_index": index}
        for index, chapter in enumerate(chapter for volume in SERIES for chapter in volume)
    ]
    speech = _speech(SERIES[2], noise)
    result = _align(omnibus, speech)
    assert [row["chapter_index"] for row in result["chapters"]] == [8, 9, 10, 11]
    assert result["anchor_density"] >= 4 * MIN_ANCHORS_PER_100_CHARACTERS


def test_an_audiobook_of_the_first_half_aligns_to_that_half() -> None:
    book = [{**chapter, "chapter_index": index} for index, chapter in enumerate(SERIES[0] + SERIES[1])]
    result = _align(book, _speech(SERIES[0], 0.5))
    assert [row["chapter_index"] for row in result["chapters"]] == [0, 1, 2, 3]


@pytest.mark.parametrize("noise", [0.0, 0.5])
def test_audio_of_an_unrelated_book_is_still_refused(noise: float) -> None:
    with pytest.raises(ValueError) as error:
        _align(_volume(99, series=99), _speech(SERIES[2], noise))
    assert str(error.value) == err("audiobook.audioDoesNotMatchText")


def test_another_volume_of_the_same_series_is_still_refused() -> None:
    # Shared names and vocabulary give more chance matches; at this recognition
    # quality the whole-book gate refused it, and the span gate still does.
    speech = _speech(SERIES[2], 0.5)
    loose = align_light_novel_to_transcript(SERIES[1], speech[0], duration=speech[1], model="x")
    assert loose["anchor_density"] < MIN_ANCHORS_PER_100_CHARACTERS
    with pytest.raises(ValueError):
        _align(SERIES[1], speech)
