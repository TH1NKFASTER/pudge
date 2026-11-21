"""A light novel linked to the wrong audiobook is not "ready"; a doubtful alignment says so (behaviour).

The only gate was "at least 8 anchors": volume 1's text against volume 2's audio
gave a ready alignment with confidence 0.94 (chance n-gram matches, ~0.4 anchors per
100 characters; the right audio has tens), and highlighting and seeking jumped to
wrong places silently. The audiobook pairing now refuses such audio, leaves out long
chapters the audio does not read, and the reader offers to link another audiobook
when the alignment is poor or the audio reads another book.
"""

from __future__ import annotations

import random

import pytest

from pudge.i18n import err
from pudge.reading_audio_alignment import MIN_ANCHORS_PER_100_CHARACTERS, align_light_novel_to_transcript
from test_v224_alignment_vn_generation import _paired_audio, _wait_until
from test_v0722_unified_audiobook_controls import LN

NAMES = ["ハルヒ", "キョン", "長門", "朝比奈", "古泉", "鶴屋", "谷口", "国木田"]


def _volume(seed: int, chapters: int = 4, sentences: int = 120) -> list[dict]:
    rng = random.Random(seed)
    kanji = [chr(code) for code in range(0x4E00, 0x4E00 + 3000)]
    kana = [chr(code) for code in range(0x3042, 0x3093)]
    nouns = ["".join(rng.sample(kanji, 2)) for _ in range(800)]
    verbs = ["".join(rng.sample(kanji, 1)) + "".join(rng.sample(kana, 2)) + "った" for _ in range(120)]

    def sentence() -> str:
        return f"{rng.choice(NAMES)}は{rng.choice(nouns)}で{rng.choice(NAMES)}と{rng.choice(nouns)}について{rng.choice(verbs)}。"

    return [
        {
            "chapter_index": index,
            "title": f"第{index + 1}章",
            "text": "\n".join(sentence() for _ in range(sentences)),
        }
        for index in range(chapters)
    ]


def _read_aloud(chapters: list[dict]) -> tuple[list[dict], float]:
    segments, clock = [], 0.0
    for chapter in chapters:
        for line in chapter["text"].splitlines():
            seconds = len(line) / 8.0
            segments.append({"start": clock, "end": clock + seconds, "text": line})
            clock += seconds + 0.3
    return segments, clock


def test_audio_of_another_volume_is_refused() -> None:
    first, second = _volume(1), _volume(2)
    segments, duration = _read_aloud(second)
    # Before: a confident alignment of the wrong book (and still so without the gate).
    loose = align_light_novel_to_transcript(first, segments, duration=duration, model="x")
    assert loose["anchor_density"] < MIN_ANCHORS_PER_100_CHARACTERS
    with pytest.raises(ValueError) as error:
        align_light_novel_to_transcript(
            first,
            segments,
            duration=duration,
            model="x",
            minimum_anchor_density=MIN_ANCHORS_PER_100_CHARACTERS,
        )
    assert str(error.value) == err("audiobook.audioDoesNotMatchText")

    right = align_light_novel_to_transcript(
        second, segments, duration=duration, model="x", minimum_anchor_density=MIN_ANCHORS_PER_100_CHARACTERS
    )
    assert len(right["chapters"]) == 4 and right["anchor_density"] > 10 * MIN_ANCHORS_PER_100_CHARACTERS


def test_audiobook_of_part_of_the_book_aligns_only_the_chapters_it_reads() -> None:
    book = _volume(3)
    segments, duration = _read_aloud(book[:2])
    result = align_light_novel_to_transcript(
        book, segments, duration=duration, model="x", minimum_anchor_density=MIN_ANCHORS_PER_100_CHARACTERS
    )
    assert [row["chapter_index"] for row in result["chapters"]] == [0, 1]


def test_alignment_job_marks_audio_of_another_book(tmp_path, monkeypatch) -> None:
    audio, ln_id, _audio_id = _paired_audio(tmp_path)
    monkeypatch.setattr(
        audio,
        "_load_transcript",
        lambda _audio_id: {"segments": [{"start": 0.0, "end": 2.0, "text": "狼と商人"}]},
    )
    monkeypatch.setattr(audio, "_load_or_analyze_activity", lambda *_args, **_kwargs: ([], "test"))
    monkeypatch.setattr(audio, "_cached_chapter_start_reading_hints", lambda _ln_id: {})
    seen: list[dict] = []

    def refuse(arguments, **_kwargs):
        seen.append(arguments)
        raise RuntimeError(err("audiobook.audioDoesNotMatchText"))

    monkeypatch.setattr(audio, "_align_reading_audio", refuse)
    audio.prepare_alignment(ln_id)
    _wait_until(lambda: audio.alignment_status(ln_id).get("status") == "error")
    status = audio.alignment_status(ln_id)
    assert status["mismatch"] is True and status["ready"] is False
    assert seen[0]["minimum_anchor_density"] == MIN_ANCHORS_PER_100_CHARACTERS


PAIRED = """
window.__state = {audiobook_id: 75, ln_book_id: 6, ln_chapter_index: 0, alignment: ALIGNMENT, linked: true,
  playing: false, player_running: false, paused: false, position: 0, speed: 1, duration: 3600};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment: ALIGNMENT, book: {id: 75}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['猫が好きです。'], tokens: [[]]});
__api.light_novel_paired_state = () => Promise.resolve({...window.__state});
__api.light_novel_audiobook_candidates = async () => [];
"""

SHOW = """
await __settle(openLightNovel(6)); await __tick(300);
const hint = document.getElementById('lnPairedQuality');
const out = {visible: Boolean(hint && !hint.hidden), text: hint ? hint.textContent : '', title: hint ? hint.title : '',
             label: PudgeI18n.t('ui.audioMayNotMatchText'), hint: PudgeI18n.t('ui.audioMayNotMatchTextHint')};
if (out.visible) { await __click('#lnPairedQuality'); await __tick(200); out.dialog = document.getElementById('modalTitle').textContent;
  out.pairTitle = PudgeI18n.t('ui.pairWithAudiobook'); }
return out;
"""


@pytest.mark.parametrize(
    ("alignment", "shown"),
    [
        ("{ready: true, status: 'ready', quality_grade: 'poor', confidence: 0.4}", True),
        # Poor only because the audio reads part of the book (an omnibus): no hint.
        ("{ready: true, status: 'ready', quality_grade: 'poor', confidence: 0.95}", False),
        ("{ready: false, status: 'error', error: 'x', mismatch: true}", True),
        ("{ready: true, status: 'ready', quality_grade: 'good'}", False),
        ("{ready: true, status: 'ready', quality_grade: 'review'}", False),
        ("{ready: false, status: 'error', error: 'ffmpeg missing'}", False),
    ],
)
def test_reader_offers_to_link_another_audiobook_when_the_audio_may_not_match(page, alignment, shown) -> None:
    for lang in ("en", "ru"):
        result = page.run(SHOW, lang=lang, **{**LN, "before": PAIRED.replace("ALIGNMENT", alignment)})
        out = result.value
        assert out["visible"] is shown
        if shown:
            assert out["label"] in out["text"] and out["title"] == out["hint"]
            assert out["dialog"] == out["pairTitle"]
            assert result.called("light_novel_audiobook_candidates")
