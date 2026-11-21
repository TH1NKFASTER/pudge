"""Mokuro dot runs: an ellipsis read as separate dots is served as "…".

MangaOCR writes an ellipsis as two or more dots ("．．．", "・・・", "。。。", "..."). Wherever the
manga text is served (the Mac reader, the iPhone/iPad text and packages, the look-up parse and
the volume word counts) such a run is "…" ("……" for four dots or more) and a text box of only
dots is "…". The stored OCR stays as it was read: no re-OCR, the cache keeps the dots.
Invented pages and text only.
"""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import mokuro_page_regions
from pudge.manga_text_cleanup import clean_dot_runs, clean_region_dots


# ------------------------------------------------------------------------- the rule
@pytest.mark.parametrize(
    ("text", "served"),
    [
        ("あ．．．い", "あ…い"),
        ("そうか・・・", "そうか…"),
        ("えっ。。。", "えっ…"),
        ("まって...", "まって…"),
        ("なに．．", "なに…"),
        ("なに．．．．", "なに……"),
        ("なに・・・・・・", "なに……"),
        ("そ‥‥", "そ……"),
        ("そうか・・・。", "そうか…。"),  # the full stop after the ellipsis ends the sentence
        ("そうか…。", "そうか…。"),
        ("まさか……", "まさか……"),  # already ellipses
        ("ジョン・スミス", "ジョン・スミス"),  # one middle dot is not an ellipsis
        ("終わり。", "終わり。"),
        ("3.14", "3.14"),
        ("．．．", "…"),  # a box of only dots
        ("・・・・・・", "…"),
        ("。", "。"),  # a box of one full stop or one middle dot stays as it is
        ("・", "・"),
        ("‥", "…"),  # a two-dot leader alone is an ellipsis
        ("そう‥", "そう…"),
        (" ．． ", "…"),
        ("", ""),
    ],
)
def test_dot_runs_become_an_ellipsis(text: str, served: str) -> None:
    assert clean_dot_runs(text) == served


def test_one_line_of_a_box_keeps_a_lone_full_stop() -> None:
    assert clean_dot_runs("。", whole_box=False) == "。"
    assert clean_dot_runs("・・・", whole_box=False) == "…"


def _segments(text: str, *, top: float = 0.9, cell: float = 0.1) -> list[dict]:
    # One box per character down a vertical column (bottom-left origin, like the Mac's OCR).
    result = []
    y = top
    for character in text:
        height = cell / 3 if character in "．・" else cell
        y -= height
        result.append(
            {
                "text": character,
                "x": 0.5,
                "y": round(y, 6),
                "width": 0.1,
                "height": round(height, 6),
                "source": "s",
            }
        )
    return result


def test_per_character_boxes_follow_the_text() -> None:
    region = {
        "text": "あ．．．い",
        "x": 0.5,
        "y": 0.6,
        "width": 0.1,
        "height": 0.3,
        "segments": _segments("あ．．．い"),
        "word_geometry": "mapped_segments",
    }
    before = copy.deepcopy(region)
    served = clean_region_dots(region)
    assert region == before  # the stored region is not changed
    assert served["text"] == "あ…い" and served["raw_text"] == "あ．．．い"
    segments = served["segments"]
    assert [segment["text"] for segment in segments] == ["あ", "…", "い"]
    # The three dots' boxes became one cell: from the bottom of the last to the top of the first.
    dots = before["segments"][1:4]
    assert segments[1]["y"] == pytest.approx(dots[2]["y"])
    assert segments[1]["y"] + segments[1]["height"] == pytest.approx(dots[0]["y"] + dots[0]["height"])
    assert segments[1]["source"] == "s" and segments[0] == before["segments"][0]


def test_two_ellipses_from_six_dots_get_three_dots_each() -> None:
    region = {"text": "ああ・・・・・・", "segments": _segments("ああ・・・・・・")}
    served = clean_region_dots(region)
    assert served["text"] == "ああ……"
    first, second = served["segments"][2:]
    dots = region["segments"][2:]
    assert first["text"] == second["text"] == "…"
    assert first["y"] == pytest.approx(dots[2]["y"]) and second["y"] == pytest.approx(dots[5]["y"])


def test_a_word_box_keeps_its_place_with_its_new_text_and_lines_are_cleaned() -> None:
    region = {
        "text": "そうか．．．",
        "segments": [{"text": "そうか．．．", "x": 0, "y": 0, "width": 1, "height": 1}],
        "provenance": {
            "source": "mokuro",
            "line_boxes": [{"text": "そうか．．．", "x": 0, "y": 0, "width": 1, "height": 1}, {"text": "。"}],
        },
    }
    served = clean_region_dots(region)
    assert served["text"] == "そうか…"
    # The word box spells the served text, so the reader still finds a box for the "…".
    assert served["segments"] == [{"text": "そうか…", "x": 0, "y": 0, "width": 1, "height": 1}]
    assert [line["text"] for line in served["provenance"]["line_boxes"]] == ["そうか…", "。"]
    assert region["provenance"]["line_boxes"][0]["text"] == "そうか．．．"


def test_a_region_without_dots_is_returned_as_it_is() -> None:
    region = {"text": "何だ", "segments": _segments("何だ")}
    assert clean_region_dots(region) is region


# ------------------------------------------------------------- where the text is served
def _mokuro_page() -> list[dict]:
    page = {
        "img_width": 400,
        "img_height": 600,
        "blocks": [
            {
                "box": [300, 100, 340, 300],
                "vertical": True,
                "font_size": 40,
                "lines": ["あ．．．い"],
                "lines_coords": [[[300, 100], [340, 100], [340, 220], [300, 220]]],
            },
            {
                "box": [100, 100, 140, 220],
                "vertical": True,
                "font_size": 40,
                "lines": ["．．．．．．"],
                "lines_coords": [[[100, 100], [140, 100], [140, 220], [100, 220]]],
            },
        ],
    }
    return mokuro_page_regions(page, 400, 600, "01.png")


def _volume(tmp_path: Path) -> tuple[Database, MangaService, int]:
    archive = tmp_path / "book.cbz"
    image = tmp_path / "page.png"
    Image.new("RGB", (400, 600), "white").save(image)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(image, "01.png")
    db = Database(tmp_path / "pudge.db")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    book_id = int(service.import_file(archive)["id"])
    fingerprint, generation, _revision = service._ocr_context(book_id)
    assert service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[(0, _mokuro_page(), "ready", "", False)],
    )
    return db, service, book_id


def test_the_reader_the_look_up_parse_and_word_counts_get_ellipses_the_cache_keeps_dots(
    tmp_path: Path,
) -> None:
    db, service, book_id = _volume(tmp_path)
    page = service.text_regions(book_id, 0, cached_only=True)
    texts = [region["text"] for region in page["regions"]]
    assert texts == ["あ…い", "…"]
    first = page["regions"][0]
    assert [segment["text"] for segment in first["segments"]] == ["あ", "…", "い"]
    assert first["raw_text"] == "あ．．．い"
    # Background study parsing and the volume's word counts read the same text.
    assert service.cached_region_texts(book_id) == [(0, "あ…い"), (0, "…")]
    # The stored OCR is as recognized.
    with db.connect() as conn:
        stored = json.loads(
            conn.execute("SELECT text FROM manga_ocr_cache WHERE book_id=?", (book_id,)).fetchone()[0]
        )
    assert [region["text"] for region in stored] == ["あ．．．い", "．．．．．．"]
    # Served twice: the same answer (nothing served was written back).
    assert [
        region["text"] for region in service.text_regions(book_id, 0, cached_only=True)["regions"]
    ] == texts


def test_iphone_text_and_package_text_get_ellipses(tmp_path: Path) -> None:
    from pudge.iphone_service import IPhoneService

    db, manga, book_id = _volume(tmp_path)
    phone = IPhoneService(db, tmp_path / "mobile")
    phone.api = SimpleNamespace(manga=manga)
    text = phone.media.handle("GET", f"/api/v2/media/manga/{book_id}/pages/0/text", {}, lambda: {})
    assert [region["text"] for region in text["regions"]] == ["あ…い", "…"]
    # The downloaded volume's text-<n>.json comes from the same answer.
    assert phone.media.manga_text(book_id, 0)["regions"] == text["regions"]


# ------------------------------------------------------------ lines and boxes (review follow-up)
def _lines(*texts: str) -> list[dict]:
    return [
        {"text": text, "x": 0.5 - 0.1 * number, "y": 0.2, "width": 0.1, "height": 0.6}
        for number, text in enumerate(texts)
    ]


def test_dots_of_two_lines_are_never_joined() -> None:
    # Mokuro read two columns: "そうか・・" and "・まさか". Joined, the text has "・・・" across
    # the line break, which is no ellipsis of either line.
    region = {
        "text": "そうか・・・まさか",
        "provenance": {"source": "mokuro", "line_boxes": _lines("そうか・・", "・まさか")},
    }
    served = clean_region_dots(region)
    assert served["text"] == "そうか…・まさか"
    assert [line["text"] for line in served["provenance"]["line_boxes"]] == ["そうか…", "・まさか"]
    # A run inside each line still becomes an ellipsis; the lines spell the served text.
    region = {"text": "あ．．．い．．．．", "provenance": {"line_boxes": _lines("あ．．．", "い．．．．")}}
    served = clean_region_dots(region)
    assert served["text"] == "あ…い……"
    assert "".join(line["text"] for line in served["provenance"]["line_boxes"]) == served["text"]


def test_a_box_of_only_dots_over_two_lines_is_one_ellipsis_line() -> None:
    region = {
        "text": "．．．．．．",
        "segments": [{"text": "．", "x": 0.1 * k, "y": 0.5, "width": 0.05, "height": 0.05} for k in range(6)],
        "provenance": {"line_boxes": _lines("．．．", "．．．")},
    }
    served = clean_region_dots(region)
    assert served["text"] == "…"
    assert [line["text"] for line in served["provenance"]["line_boxes"]] == ["…"]
    assert len(served["segments"]) == 1 and served["segments"][0]["text"] == "…"
    assert served["segments"][0]["x"] == 0 and served["segments"][0]["width"] == pytest.approx(0.55)


def test_a_run_across_two_word_boxes_merges_them_and_keeps_a_hitbox() -> None:
    region = {
        "text": "あ・・・",
        "segments": [
            {"text": "あ・", "x": 0.0, "y": 0.5, "width": 0.2, "height": 0.1},
            {"text": "・・", "x": 0.2, "y": 0.5, "width": 0.1, "height": 0.1},
        ],
    }
    served = clean_region_dots(region)
    assert served["text"] == "あ…"
    assert served["segments"] == [{"text": "あ…", "x": 0.0, "y": 0.5, "width": 0.3, "height": 0.1}]


def test_boxes_that_do_not_spell_the_text_leave_the_box_as_it_was_read() -> None:
    region = {
        "text": "そうか・・・",
        "segments": [{"text": "そうか", "x": 0, "y": 0, "width": 1, "height": 1}],
    }
    assert clean_region_dots(region) is region


# ------------------------------------------------------------------ look-up parses (review follow-up)
def test_a_parse_of_the_text_as_recognized_serves_the_cleaned_text() -> None:
    from pudge.manga_text_cleanup import offset_map, remap_parse

    assert offset_map("あ．．．い", "あ…い") == [0, 1, 1, 1, 2, 3]
    assert offset_map("そうか・・・。", "そうか…。") == [0, 1, 2, 3, 3, 3, 4, 5]
    assert offset_map("あ．．．い", "か…い") is None  # not the same text
    parsed = {
        "paragraphs": ["猫．．．犬"],
        "tokens": [
            [
                {"start": 0, "end": 1, "wordId": 101, "readingIndex": 0},
                {"start": 4, "end": 5, "wordId": 102, "readingIndex": 0, "surface": "犬"},
            ]
        ],
        "vocabulary": [{"wordId": 101}, {"wordId": 102}],
    }
    remapped = remap_parse(parsed, "猫．．．犬", "猫…犬")
    assert remapped["paragraphs"] == ["猫…犬"]
    assert remapped["tokens"] == [
        [
            {"start": 0, "end": 1, "wordId": 101, "readingIndex": 0},
            {"start": 2, "end": 3, "wordId": 102, "readingIndex": 0, "surface": "犬"},
        ]
    ]
    assert remapped["vocabulary"] == parsed["vocabulary"]
    assert remap_parse({**parsed, "parseProvider": "jpdb"}, "猫．．．犬", "猫…犬") is None


def test_serving_a_cleaned_box_gives_it_the_cached_parse_without_a_request(tmp_path: Path) -> None:
    import hashlib

    from pudge.config import AppConfig
    from pudge.light_novels import LightNovelService
    from pudge.database_storage import pack_json
    from pudge.web_app import WebAppApi

    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.db"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    service = LightNovelService(config)
    _db, manga, book_id = _volume(tmp_path)
    key = lambda text: hashlib.sha256(("study-v1\0" + text).encode("utf-8")).hexdigest()  # noqa: E731
    parsed = {
        "paragraphs": ["あ．．．い"],
        "tokens": [[{"start": 4, "end": 5, "wordId": 7, "readingIndex": 0}]],
        "vocabulary": [{"wordId": 7}],
    }
    with service._connection() as conn:
        conn.execute(
            "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at,parse_version,used_at) VALUES(?,?,?,?,?,?)",
            (key("あ．．．い"), pack_json(parsed), "jiten-v1", 0, "v9", 0),
        )
    manga.parse_alias = lambda text, raw: WebAppApi._alias_manga_text_parse(
        SimpleNamespace(light_novels=service), text, raw
    )
    manga.text_regions(book_id, 0, cached_only=True)
    versions: list[str] = []
    cached = service._cached_parse(key("あ…い"), provider="jiten", versions=versions)
    assert cached["paragraphs"] == ["あ…い"]
    assert cached["tokens"] == [[{"start": 2, "end": 3, "wordId": 7, "readingIndex": 0}]]
    assert versions == ["v9"]  # as old as the parse it came from
    # A text with a parse of its own keeps it.
    assert service.alias_study_parse("あ…い", "あ．．．い", lambda value: {"other": True}) is False
