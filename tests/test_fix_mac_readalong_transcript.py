"""Read-along text in the Mac audiobook player (sidebar and float).

The backend turns the read-along document the iPhone already gets (anime subtitles with
the OP/ED cut applied, or a linked light novel's alignment) into timed lines
(``WebAppApi.audiobook_read_along``); the player's "Text" panel draws them once,
moves a highlight by binary search, seeks on click and parses words for the popup.
All text and timings below are invented.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import anime_audiobooks as aa
from pudge import read_along
from pudge.bridge_methods import PAGE_METHODS
from pudge.database import Database
from pudge.i18n import catalog

# ---------------------------------------------------------------- backend


def test_lines_follow_cue_times_and_the_chapter_offset() -> None:
    cues = [(1.0, 2.5, "ねこがいる。"), (4.0, 5.0, "いぬも\nいる。"), (7.0, 8.0, "  ")]
    first = aa.read_along_chapter(cues, index=0, title="Ep 1", duration=10)
    second = aa.shift_chapter(
        aa.read_along_chapter([(0.5, 1.5, "さよなら！")], index=0, title="", duration=3),
        index=1,
        title="Ep 2",
        offset_ms=10_000,
    )
    payload = read_along.transcript({"schema": 1, "chapters": [first, second]})
    assert [(line["start"], line["end"], line["text"], line["chapter"]) for line in payload["lines"]] == [
        (1.0, 2.5, "ねこがいる。", 0),
        (4.0, 5.0, "いぬも いる。", 0),
        (10.5, 11.5, "さよなら！", 1),
    ]
    assert payload["chapters"] == [{"title": "Ep 1", "start": 0.0}, {"title": "Ep 2", "start": 10.0}]


def test_light_novel_sentences_are_timed_between_anchors() -> None:
    chapter = {
        "title": "One",
        "startMs": 0,
        "anchors": [[0, 0], [2000, 8], [4000, 15], [4000, 15]],
        "sentences": [
            {"p": 0, "text": "あ", "start": 0, "end": 4},
            {"p": 0, "text": "い", "start": 4, "end": 8},
            {"p": 1, "text": "う", "start": 8, "end": 15},
        ],
    }
    lines = read_along.transcript({"chapters": [chapter, {"anchors": [[0, 0]], "sentences": [{}]}]})["lines"]
    assert [(line["start"], line["end"]) for line in lines] == [(0.0, 1.0), (1.0, 2.0), (2.0, 4.0)]
    assert read_along.transcript(None) == {"chapters": [], "lines": []}


def test_iphone_sentence_split_still_works_after_the_move() -> None:
    from pudge.iphone_service import IPhoneService

    assert IPhoneService._split_sentences("「やあ。」と言った！本当？") == [
        "「やあ。」",
        "と言った！",
        "本当？",
    ]
    assert read_along.split_sentences("ひとつ。ふたつ") == ["ひとつ。", "ふたつ"]


def _anime_book(db: Database, tmp_path: Path) -> int:
    folder = tmp_path / aa.ROOT_NAME / "Show"
    folder.mkdir(parents=True)
    (folder / aa.MARKER).write_text("{}", encoding="utf-8")
    files = []
    for number, cues in ((1, [(1.0, 2.0, "はじめ。"), (3.0, 4.0, "つぎ。")]), (2, [(0.5, 1.0, "おわり。")])):
        audio = folder / aa.episode_file_name(number)
        audio.write_bytes(b"\0")
        chapter = aa.read_along_chapter(cues, index=0, title=f"Ep {number}", duration=6)
        aa.read_along_sidecar(audio).write_text(json.dumps(chapter, ensure_ascii=False), encoding="utf-8")
        files.append(audio)
    with db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,12,0,0)",
            (str(folder), "Show"),
        ).lastrowid
        for index, audio in enumerate(files):
            conn.execute(
                "INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,?,?,?,6,?,?)",
                (book, index, str(audio), audio.stem, index * 6.0, index * 6.0 + 6),
            )
            conn.execute(
                "INSERT INTO audiobook_chapters(book_id,chapter_index,title,start,end) VALUES(?,?,?,?,?)",
                (book, index, f"Episode {index + 1}", index * 6.0, index * 6.0 + 6),
            )
    return int(book)


def _api(db: Database, audiobooks: object | None = None):
    from pudge.web_app import WebAppApi

    api = WebAppApi.__new__(WebAppApi)
    api.audiobooks = audiobooks or SimpleNamespace(db=db)
    api.logger = logging.getLogger("test")
    return api


def test_window_api_gives_an_anime_audiobook_its_subtitle_lines(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite")
    book = _anime_book(db, tmp_path)
    result = _api(db).audiobook_read_along(book)
    assert result["ok"] and result["source"] == "anime" and result["book_id"] == book
    assert [(line["start"], line["text"], line["chapter"]) for line in result["lines"]] == [
        (1.0, "はじめ。", 0),
        (3.0, "つぎ。", 0),
        (6.5, "おわり。", 1),
    ]
    assert [row["title"] for row in result["chapters"]] == ["Episode 1", "Episode 2"]
    # An ordinary audiobook without a linked novel has no text (and no error).
    with db.connect() as conn:
        plain = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,5,0,0)",
            (str(tmp_path / "plain.m4b"), "Plain"),
        ).lastrowid
    assert _api(db).audiobook_read_along(plain) == {
        "ok": True,
        "book_id": plain,
        "source": "",
        "chapters": [],
        "lines": [],
    }
    assert "audiobook_read_along" in PAGE_METHODS


def test_window_api_reads_a_linked_light_novel_on_the_audiobook_clock(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,4,0,0)",
            (str(tmp_path / "novel.m4b"), "Novel audio"),
        ).lastrowid
        conn.execute("CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_chapters(id INTEGER PRIMARY KEY AUTOINCREMENT,book_id INT,"
            "chapter_index INT,title TEXT,text TEXT)"
        )
        conn.execute("INSERT INTO ln_books VALUES(7,'Novel')")
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text) VALUES(7,1,'One',?)",
            ("「おはよう。」と言った。\n彼は笑った！本当？",),
        )
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) "
            "VALUES(7,?,'stt',1,1)",
            (book,),
        )
    aligned = tmp_path / "alignment.json"
    aligned.write_text("{}")
    alignment = {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "normalized_length": 15,
                "start": 0.0,
                "end": 4.0,
                "anchors": [
                    {"offset": 0, "time": 0.0},
                    {"offset": 8, "time": 2.0},
                    {"offset": 15, "time": 4.0},
                ],
            }
        ],
    }
    audiobooks = SimpleNamespace(
        db=db, _alignment_path=lambda ln, ab: aligned, _load_alignment=lambda ln, ab: alignment
    )
    result = _api(db, audiobooks).audiobook_read_along(book)
    assert result["source"] == "light_novel"
    assert [line["text"] for line in result["lines"]] == [
        "「おはよう。」",
        "と言った。",
        "彼は笑った！",
        "本当？",
    ]
    starts = [line["start"] for line in result["lines"]]
    assert starts == sorted(starts) and starts[0] == 0.0 and result["lines"][-1]["end"] == 4.0
    # Not aligned yet: no text, the alignment is not even loaded.
    aligned.unlink()
    audiobooks._load_alignment = lambda ln, ab: pytest.fail("loaded without an alignment file")
    assert _api(db, audiobooks).audiobook_read_along(book)["lines"] == []


def test_book_payload_says_when_read_along_exists(tmp_path) -> None:
    from pudge.audiobooks import AudiobookService

    db = Database(tmp_path / "db.sqlite")
    anime = _anime_book(db, tmp_path)
    with db.connect() as conn:
        plain = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,5,0,0)",
            (str(tmp_path / "plain.m4b"), "Plain"),
        ).lastrowid
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    try:
        assert service.book(anime, include_transcription=False)["read_along"] is True
        assert service.book(plain, include_transcription=False)["read_along"] is False
    finally:
        service.close()


def test_strings_exist_in_both_languages() -> None:
    keys = [
        "sidebar.readAlongToggle",
        "sidebar.readAlongTitle",
        "sidebar.readAlongFollow",
        "sidebar.readAlongLoading",
        "sidebar.readAlongEmpty",
        "sidebar.readAlongError",
        "sidebar.readAlongSeek",
    ]
    for lang in ("en", "ru"):
        strings = catalog(lang)
        for key in keys:
            assert strings[key], (lang, key)
    assert catalog("ru")["sidebar.readAlongToggle"] == "Текст"
    assert catalog("en")["sidebar.readAlongToggle"] == "Text"


# ---------------------------------------------------------------- the player


BOOK = {
    "id": 7,
    "title": "Show",
    "duration": 60,
    "position": 3.8,
    "speed": 1,
    "playing": True,
    "player_running": True,
    "read_along": True,
    "chapters": [],
}
LINES = {
    "ok": True,
    "book_id": 7,
    "source": "anime",
    "chapters": [{"title": "Ep 1", "start": 0}],
    "lines": [
        {"start": 1.0, "end": 2.0, "text": "あさです。", "chapter": 0},
        {"start": 4.0, "end": 5.0, "text": "ひるです。", "chapter": 0},
        {"start": 6.0, "end": 7.0, "text": "よるです。", "chapter": 0},
        {"start": 30.0, "end": 31.0, "text": "おやすみ。", "chapter": 0},
    ],
}
PARSE = r"""
__api.study_parse_text = async text => {
  const paragraphs = text.split('\n');
  return {text, paragraphs, vocabulary: [], settings: {study_backend: 'jiten', show_furigana: false},
    tokens: paragraphs.map(line => [{start: 0, end: 2, wordId: 1, readingIndex: 0}])};
};
"""


def _float(page, code: str, *, book=None, lang: str = "en", lines=None):
    # The player's position moves with the (fake) clock while it plays; a seek moves it.
    before = (
        "window.floatBooks = BOOKS; window.playFrom = [window.floatBooks[0].position, Date.now()];"
        "__api.audiobook_seek_to = async (id, at) => { window.playFrom = [at, Date.now()]; return {ok: true}; };"
        "__api.sidebar_audio_state = async () => ({books: window.floatBooks.map(b => ({...b, position: "
        "window.playFrom[0] + (b.playing ? (Date.now() - window.playFrom[1]) / 1000 : 0)}))});"
    ).replace("BOOKS", json.dumps([book or BOOK])) + PARSE
    return page.run(
        code,
        name="audiobook_float.html",
        lang=lang,
        api={
            "audiobook_float_settings": {"language": lang},
            "audiobook_read_along": lines or LINES,
        },
        before=before,
    )


STATE = r"""
const state = () => ({
  current: [...document.querySelectorAll('.abt-line.current')].map(row => Number(row.dataset.abtLine)),
  rows: document.querySelectorAll('.abt-line').length,
  panel: __visible('[data-sc-transcript]'),
  follow: __visible('[data-abt-follow]'),
  words: document.querySelectorAll('.abt-line [data-pudge-study-token]').length,
});
"""


def test_text_toggle_shows_lines_follows_the_audio_and_seeks(page) -> None:
    result = _float(
        page,
        STATE
        + r"""
        await __tick(1500);
        const label = __text('[data-sc-audio="text"]');
        const closed = state();
        await __click('[data-sc-audio="text"]'); await __tick(50); await __flush(); await __tick(50);
        const opened = state();
        await __tick(900); await __flush();
        const later = state();
        await __click('.abt-line[data-abt-line="3"] .abt-time'); await __tick(50); await __flush();
        const sought = state();
        return {label, closed, opened, later, sought};
        """,
    )
    value = result.value
    assert value["label"] == "Text"
    assert value["closed"]["panel"] is False and value["closed"]["rows"] == 0
    assert value["opened"]["panel"] is True and value["opened"]["rows"] == 4
    assert value["opened"]["current"] == [1]  # ~5.4 s: the line that started at 4 s
    assert value["later"]["current"] == [2]  # the local clock moved past 6 s between polls
    assert value["opened"]["words"] >= 4  # the visible block got the shared word markup
    assert value["sought"]["current"] == [3]
    assert result.calls_of("audiobook_seek_to") == [[7, 30.0]]
    assert len(result.calls_of("audiobook_read_along")) == 1  # once per book
    parsed = result.calls_of("study_parse_text")
    assert parsed and parsed[0][0].split("\n")[:2] == ["あさです。", "ひるです。"]


def test_manual_scroll_pauses_following_until_the_button_or_a_timeout(page) -> None:
    value = _float(
        page,
        STATE
        + r"""
        await __tick(1500);
        await __click('[data-sc-audio="text"]'); await __tick(50); await __flush();
        await __fire('[data-abt-list]', 'wheel', {deltaY: 40}, 'WheelEvent');
        const paused = state().follow;
        const following = PudgeAudiobookTranscript.following();
        await __tick(5200);
        const resumed = state().follow;
        await __fire('[data-abt-list]', 'wheel', {deltaY: 40}, 'WheelEvent');
        await __click('[data-abt-follow]');
        return {paused, following, resumed, button: state().follow, again: PudgeAudiobookTranscript.following()};
        """,
    ).value
    assert value == {"paused": True, "following": False, "resumed": False, "button": False, "again": True}


def test_toggle_is_hidden_without_text_and_localized(page) -> None:
    hidden = _float(
        page,
        "await __tick(1500); return __visible('[data-sc-audio=\"text\"]');",
        book={**BOOK, "read_along": False},
    )
    assert hidden.value is False and not hidden.called("audiobook_read_along")
    russian = _float(
        page,
        r"""
        await __tick(1500);
        await __click('[data-sc-audio="text"]'); await __tick(50); await __flush();
        return [__text('[data-sc-audio="text"]'), __text('.abt-status')];
        """,
        lang="ru",
        lines={"ok": True, "book_id": 7, "source": "", "chapters": [], "lines": []},
    )
    assert russian.value == ["Текст", "Для этой аудиокниги пока нет текста"]


def test_binary_search_finds_the_line_that_is_playing(page) -> None:
    value = page.run(
        "const s = Float64Array.from([1, 4, 6, 30]);"
        "return [0, 1, 3.9, 4, 29.9, 30, 99].map(t => PudgeAudiobookTranscript.lineAt(s, t));",
        name="audiobook_float.html",
        api={"audiobook_float_settings": {"language": "en"}},
    ).value
    assert value == [-1, 0, 0, 1, 2, 3, 3]


def test_main_window_sidebar_has_the_panel_and_leaves_it_to_an_open_float(page) -> None:
    code = (
        STATE
        + r"""
        await __tick(1500);
        await __click('[data-sc-audio="text"]'); await __tick(50); await __flush();
        return state();
        """
    )
    api = {"audiobook_read_along": LINES, "audiobook_seek_to": {"ok": True}}
    before = "__api.sidebar_audio_state = async () => ({books: [BOOK], float_open: FLOAT});" + PARSE
    main = page.run(
        code,
        api=api,
        before=before.replace("BOOK", json.dumps({**BOOK, "playing": False})).replace("FLOAT", "false"),
    ).value
    assert main["panel"] is True and main["rows"] == 4 and main["current"] == [0]
    floated = page.run(
        code,
        api=api,
        before=before.replace("BOOK", json.dumps({**BOOK, "playing": False})).replace("FLOAT", "true"),
    )
    assert floated.value["panel"] is False  # the float shows the text
    assert not floated.called("audiobook_read_along")
