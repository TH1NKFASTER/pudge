"""Light-novel progress counts characters, not chapters (behaviour; audit 71Б rewrite)."""

import json
import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    return LightNovelService(cfg)


def _uneven_book(service: LightNovelService, path: Path, *, current_offset: float) -> int:
    """A book of a 100-character and a 900-character chapter, read up to `current_offset`
    of the second one."""
    now = time.time()
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO ln_books(
                title,file_path,file_type,current_chapter,current_offset,
                finished,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("Uneven Book", str(path), "txt", 1, current_offset, 0, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "Short", "a" * 100, f"short-{path.name}"),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 1, "Long", "b" * 900, f"long-{path.name}"),
        )
    return book_id


def test_book_progress_is_weighted_by_characters(tmp_path: Path) -> None:
    service = _service(tmp_path)
    book_id = _uneven_book(service, tmp_path / "book.txt", current_offset=0.5)
    book = next(item for item in service.books() if int(item["id"]) == book_id)
    assert int(book["character_count"]) == 1000
    assert float(book["read_character_count"]) == 550.0
    assert float(book["reading_progress"]) == 0.55
    assert float(book["reading_progress_percent"]) == 55.0


def test_start_of_long_second_chapter_is_ten_percent_not_half(tmp_path: Path) -> None:
    service = _service(tmp_path)
    book_id = _uneven_book(service, tmp_path / "book2.txt", current_offset=0.0)
    book = next(item for item in service.books() if int(item["id"]) == book_id)
    assert float(book["reading_progress_percent"]) == 10.0


def test_ln_card_shows_the_character_weighted_progress(page, tmp_path: Path) -> None:
    # At the start of the long second chapter of two: 10 % read (by characters), not the
    # 50 % that "chapter 2 of 2" would suggest.
    service = _service(tmp_path)
    book_id = _uneven_book(service, tmp_path / "book3.txt", current_offset=0.0)
    books = json.loads(json.dumps(service.books(), default=str))
    result = page.run(
        r"""
        setPage('lightnovels', true); await __tick(100);
        const card = document.querySelector('#lightNovelsContent [data-ln-book="BOOK"]');
        return {bar: card.querySelector('.ln-card-progress span').style.width,
                facts: [...card.querySelectorAll('.ln-card-meta span')].map(n => n.textContent)};
        """.replace("BOOK", str(book_id)),
        api={"light_novel_state": {"version": 1, "settings": {}, "books": books}},
    )
    assert result.value["bar"] == "10%"
    assert "10%" in result.value["facts"] and "50%" not in result.value["facts"]
