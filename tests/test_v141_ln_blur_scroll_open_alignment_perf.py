"""Light novels v1.41: blurred illustrations, a re-render that keeps loaded pictures, opening
behind the review gate, native trackpad scrolling, light paired-audio polling (behaviour;
audit 71Б rewrite).

Window checks run index.html in node (tests/page_harness.py); set-ups are in
tests/t71g22_support.py and tests/t71g19_support.py. Blur on/off and click-to-reveal-then-zoom
are covered by test_v137_pair_blur_images.py."""

from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.reading_audio_alignment import _inject_punctuation_pause_anchors
from t71g19_support import LN_PAIRED, OPEN
from t71g21_support import computed_style
from t71g22_support import ILLUSTRATED, NO_LN_GATE, USER_SCROLL
from test_web_app import make_api


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _paired_services(tmp_path: Path) -> tuple[AudiobookService, int, int]:
    cfg = _cfg(tmp_path)
    ln = LightNovelService(cfg)
    source = tmp_path / "book.txt"
    source.write_text("第一章\n狼と商人の物語です。\n\n第二章\n市場へ向かいます。", encoding="utf-8")
    book = ln.import_file(source)
    audio_file = tmp_path / "audio.m4b"
    audio_file.write_bytes(b"audio")
    audio = AudiobookService(
        Database(cfg.library.database_path),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=cfg.paths.cache_dir,
    )
    audiobook = audio._upsert(
        path=audio_file,
        title="Book 1",
        duration=100.0,
        files=[
            {
                "index": 0,
                "path": str(audio_file),
                "title": "audio",
                "duration": 100.0,
                "start": 0.0,
                "end": 100.0,
            }
        ],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 100.0}],
    )
    ln_id, audio_id = int(book["id"]), int(audiobook["id"])
    audio.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    return audio, ln_id, audio_id


ILLUSTRATION_LOOK = (
    computed_style(
        "filter",
        "visibility",
        "cursor",
        "overflow",
        "contain",
        "transform",
        "backface-visibility",
        "-webkit-backface-visibility",
        "will-change",
    )
    + USER_SCROLL
    + r"""
await __settle(openLightNovel(6)); await __tick(100);
const figure = document.querySelector('#lnReader .ln-inline-image'), img = figure.querySelector('img'), frame = figure.querySelector('.ln-inline-image-frame');
const look = () => ({
  image: Object.fromEntries(['filter', 'visibility', 'cursor', 'transform', 'will-change'].map(p => [p, __css(img, p)])),
  frame: Object.fromEntries(['overflow', 'contain', 'transform', 'backface-visibility', '-webkit-backface-visibility', 'will-change'].map(p => [p, __css(frame, p)])),
  label: __visible('#lnReader .ln-inline-image-spoiler-label'),
  markup: [...figure.querySelectorAll('*')].map(n => n.tagName.toLowerCase()),
  sameImage: figure.querySelector('img') === img,
});
"""
)


def test_blurred_illustration_is_the_eager_image_itself_clipped_by_its_frame(page) -> None:
    # As ッツ (ttsu) does it: the real <img> is loaded at once and blurred by CSS; the frame
    # clips the blur's halo. No canvas copy of the picture, no forced compositor layer on
    # the frame (contain / translateZ / backface-visibility made scrolling stutter), and
    # the blur stays while the reader scrolls (pictures used to vanish mid-scroll).
    for lang in ("en", "ru"):
        result = page.run(
            ILLUSTRATION_LOOK
            + r"""
            const out = {still: look(), loading: img.getAttribute('loading'), decoding: img.getAttribute('decoding'),
                         label: __text('#lnReader .ln-inline-image-spoiler-label'), expected: t('ui.showIllustration')};
            wheel(); scroller.dispatchEvent(new Event('scroll'));
            out.scrolling = {on: scrolling(), ...look()};
            await __tick(300);
            await __click('#lnReader .ln-inline-image-spoiler-label'); await __tick(20);    // a click anywhere on the picture
            out.revealed = {...look(), revealed: figure.classList.contains('revealed')};
            return out;
            """,
            lang=lang,
            before="window.LN_SETTINGS = {blur_images: true};" + ILLUSTRATED,
            state=NO_LN_GATE,
        )
        out = result.value
        plain_frame = {
            "overflow": "hidden",
            "contain": None,
            "transform": None,
            "backface-visibility": None,
            "-webkit-backface-visibility": None,
            "will-change": None,
        }
        blurred = {
            "filter": "blur(44px)",
            "visibility": None,
            "cursor": "pointer",
            "transform": None,
            "will-change": None,
        }
        assert out["still"] == {
            "image": blurred,
            "frame": plain_frame,
            "label": True,
            "markup": ["span", "span", "img"],
            "sameImage": True,
        }
        assert (out["loading"], out["decoding"]) == ("eager", "async")
        assert out["label"] == out["expected"]
        assert out["scrolling"] == {"on": True, **out["still"]}
        # Revealing only lifts the blur of the same picture.
        assert out["revealed"]["revealed"] is True and out["revealed"]["sameImage"] is True
        assert out["revealed"]["image"]["filter"] is None and out["revealed"]["label"] is False
        assert out["revealed"]["frame"] == plain_frame and out["revealed"]["markup"] == [
            "span",
            "span",
            "img",
        ]


def test_parsed_chapter_keeps_the_loaded_picture_and_waits_until_scrolling_stops(page) -> None:
    # Jiten's parse arrives while the user scrolls: the reader swaps in the parsed words only
    # once the scrolling has stopped (never mid-scroll, however long it lasts; it used to
    # force the swap after 1.8 s), and the illustration already on screen is kept as it is
    # (it used to be replaced by a new <img> that loaded and flashed again).
    result = page.run(
        USER_SCROLL
        + r"""
        await __settle(openLightNovel(6)); await __tick(100);
        const img = () => document.querySelector('#lnReader .ln-inline-image img');
        const parsedWords = () => document.querySelectorAll('#lnReader .ln-word:not(.ln-word-fallback)').length;
        const first = img();
        first.__naturalWidth = 800; first.dispatchEvent(new Event('load'));
        const out = {unparsed: parsedWords(), loaded: first.classList.contains('ln-image-ready')};
        PARSED = true;
        const whileScrolling = [];
        for (let k = 0; k < 6; k++) { await userScroll(500, 50); whileScrolling.push(parsedWords()); }
        out.whileScrolling = whileScrolling;
        out.polled = __apiCalls.some(c => c.name === 'light_novel_chapter_parse_status');
        const afterStop = [];
        for (let k = 0; k < 4; k++) { await __tick(50); afterStop.push(parsedWords()); }
        out.afterStop = afterStop;
        out.sameImage = img() === first;
        out.stillLoaded = img().classList.contains('ln-image-ready');
        return out;
        """,
        before=ILLUSTRATED,
        state=NO_LN_GATE,
    )
    out = result.value
    assert out["unparsed"] == 0 and out["loaded"] is True and out["polled"] is True
    assert out["whileScrolling"] == [0] * 6
    assert out["afterStop"][0] == 0 and out["afterStop"][-1] == 2  # within 200 ms of the last scroll
    assert out["sameImage"] is True and out["stillLoaded"] is True


def test_reader_opens_only_after_the_review_gate_and_asks_it_for_the_saved_chapter(page) -> None:
    # The book's metadata comes first (the gate needs the chapter to be read), the reader
    # appears only once the gate lets the user in; declined, it never opens.
    result = page.run(
        r"""
        const asked = [];
        window.PudgeReviewGate.require = (...args) => new Promise(resolve => {
          asked.push({args, before: __apiCalls.map(c => c.name).filter(n => n.startsWith('light_novel_'))}); window.__answer = resolve; });
        const isOpen = () => $('lnReaderShell').classList.contains('open');
        const first = openLightNovel(6); await __tick(200);
        const out = {waiting: isOpen(), asked: asked.map(a => a.args), before: asked[0].before};
        window.__answer(false); await __settle(first); await __tick(100);
        out.declined = {open: isOpen(), chapters: __apiCalls.filter(c => c.name === 'light_novel_chapter').length};
        const second = openLightNovel(6); await __tick(100);
        window.__answer(true); await __settle(second); await __tick(200);
        out.approved = {open: isOpen(), chapters: __apiCalls.filter(c => c.name === 'light_novel_chapter').map(c => c.args), text: __text('#lnReader')};
        return out;
        """,
        before=r"""
        __api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 2, current_offset: 0, chapters: [0, 1, 2].map(i => ({chapter_index: i, title: 'C' + i}))});
        __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['猫が好きです。'], tokens: [[]], vocabulary: []});
        """,
    )
    out = result.value
    assert out["waiting"] is False
    assert out["asked"] == [["ln", 6, 2]] and out["before"][-1] == "light_novel_open"
    assert out["declined"] == {"open": False, "chapters": 0}
    assert out["approved"] == {"open": True, "chapters": [[6, 2]], "text": "猫が好きです。"}


def test_opening_a_book_does_not_load_its_alignment_or_transcript(tmp_path: Path, monkeypatch) -> None:
    # Opening must stay fast: the link to the audiobook is enough, the alignment status and
    # the transcript fingerprint (both read big files) come later with the paired polls.
    api = make_api(tmp_path)
    source = tmp_path / "book.txt"
    source.write_text("第一章\n狼と商人の物語です。", encoding="utf-8")
    ln_id = int(api.light_novels.import_file(source)["id"])
    audio_file = tmp_path / "audio.m4b"
    audio_file.write_bytes(b"audio")
    audiobook = api.audiobooks._upsert(
        path=audio_file,
        title="Audio",
        duration=100.0,
        files=[
            {
                "index": 0,
                "path": str(audio_file),
                "title": "audio",
                "duration": 100.0,
                "start": 0.0,
                "end": 100.0,
            }
        ],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 100.0}],
    )
    api.audiobooks.link_light_novel(ln_id, int(audiobook["id"]), prepare_alignment=False)

    def slow(*_args, **_kwargs):
        raise AssertionError("opening a book must not load alignment or transcript data")

    monkeypatch.setattr(api.audiobooks, "alignment_status", slow)
    monkeypatch.setattr(api.audiobooks, "transcription_status", slow)
    monkeypatch.setattr(api.audiobooks, "_load_alignment", slow)
    book = api.light_novel_open(ln_id)
    assert book["paired_audio"]["book"]["id"] == int(audiobook["id"])


def test_lightweight_book_does_not_touch_transcript_fingerprint(tmp_path: Path) -> None:
    audio, _ln_id, audio_id = _paired_services(tmp_path)
    audio.transcription_status = lambda _book_id: (_ for _ in ()).throw(
        AssertionError("transcription should be lazy")
    )  # type: ignore[method-assign]
    book = audio.book(audio_id, include_transcription=False)
    assert book["transcription"] == {"status": "unknown", "ready": False}


def test_paired_state_loads_ready_alignment_once_per_poll(tmp_path: Path) -> None:
    audio, ln_id, _audio_id = _paired_services(tmp_path)
    calls = 0
    alignment = {
        "schema": "reading-audio-v3",
        "model": "test",
        "confidence": 0.9,
        "anchor_count": 0,
        "chapters": [],
        "quality": {"coverage": 1.0, "warning_count": 0},
    }

    def load(_ln: int, _audio: int):
        nonlocal calls
        calls += 1
        return alignment

    audio._load_alignment = load  # type: ignore[method-assign]
    state = audio.paired_state(ln_id)
    assert state["alignment"]["ready"] is True
    assert calls == 1


def test_uncached_audiobook_activity_uses_stt_without_full_audio_fft(tmp_path: Path, monkeypatch) -> None:
    audio, _ln_id, audio_id = _paired_services(tmp_path)
    monkeypatch.setattr(
        "pudge.audiobooks.analyze_audio_activity",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("FFT must not block first alignment")),
    )
    regions, source = audio._load_or_analyze_activity(
        audio_id,
        [{"start": 1.0, "end": 2.0, "words": [{"start": 1.0, "end": 1.4}, {"start": 1.5, "end": 2.0}]}],
    )
    assert source == "stt"
    assert regions


def test_punctuation_pause_clock_keeps_expected_clock() -> None:
    anchors = [{"offset": i * 2, "time": i * 0.1} for i in range(101)]
    boundaries = [
        {"offset": 40, "strength": 3},
        {"offset": 80, "strength": 3},
        {"offset": 120, "strength": 3},
    ]
    regions = [{"start": 0.0, "end": 1.8}, {"start": 2.2, "end": 3.8}, {"start": 4.2, "end": 10.0}]
    output, count = _inject_punctuation_pause_anchors(anchors, boundaries, regions)
    assert count == 2
    assert output == sorted(output, key=lambda row: (float(row["time"]), float(row["offset"])))
    assert any(abs(float(row["time"]) - 1.8) < 1e-6 for row in output)


def test_trackpad_scrolling_is_native_and_pauses_following_the_audio_for_two_seconds(page) -> None:
    # The wheel is left to WebKit (a JS "smooth coarse wheel" used to take it over and
    # felt sluggish): the event is not cancelled and the page does not move scrollTop.
    # While listening, the reader stops scrolling to the spoken word for 2 s after the
    # user's wheel and then follows it again.
    result = page.run(
        OPEN
        + USER_SCROLL
        + r"""
        scroller.__rect = {x: 0, y: 0, width: 800, height: 600};
        document.querySelectorAll('#lnReader .ln-word,#lnReader [data-ln-paragraph]').forEach(node => { node.__rect = {x: 0, y: 2000, width: 20, height: 20}; });
        // Inside the text (at the chapter's end the wheel turns to the next chapter).
        scroller.scrollHeight = 3000; scroller.scrollTop = 1000;
        const follows = () => [...document.querySelectorAll('#lnReader .ln-word,#lnReader [data-ln-paragraph]')].reduce((n, node) => n + (node.__scrolls || []).length, 0);
        await __tick(1000);
        const out = {followed: follows()};
        const top = scroller.scrollTop, event = wheel();
        out.prevented = event.defaultPrevented;
        await __tick(1800);
        out.held = follows() - out.followed;
        out.moved = scroller.scrollTop - top;
        await __tick(1000);
        out.again = follows() - out.followed;
        return out;
        """,
        before=LN_PAIRED,
    )
    assert result.value == {"followed": 1, "prevented": False, "held": 0, "moved": 0, "again": 1}
