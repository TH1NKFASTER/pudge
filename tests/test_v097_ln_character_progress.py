from pathlib import Path
import time

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    return LightNovelService(cfg)


def test_book_progress_is_weighted_by_characters(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.time()
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO ln_books(
                title,file_path,file_type,current_chapter,current_offset,
                finished,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("Uneven Book", str(tmp_path / "book.txt"), "txt", 1, 0.5, 0, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "Short", "a" * 100, "short"),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 1, "Long", "b" * 900, "long"),
        )

    book = next(item for item in service.books() if int(item["id"]) == book_id)
    assert int(book["character_count"]) == 1000
    assert float(book["read_character_count"]) == 550.0
    assert float(book["reading_progress"]) == 0.55
    assert float(book["reading_progress_percent"]) == 55.0


def test_start_of_long_second_chapter_is_ten_percent_not_half(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.time()
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO ln_books(
                title,file_path,file_type,current_chapter,current_offset,
                finished,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("Uneven Book", str(tmp_path / "book2.txt"), "txt", 1, 0.0, 0, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "Short", "a" * 100, "short2"),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 1, "Long", "b" * 900, "long2"),
        )

    book = next(item for item in service.books() if int(item["id"]) == book_id)
    assert float(book["reading_progress_percent"]) == 10.0


def test_ln_card_uses_backend_character_progress() -> None:
    html = (
        Path(__file__).parents[1] / "pudge" / "web" / "index.html"
    ).read_text(encoding="utf-8")
    assert "Number(book.reading_progress||0)" in html
    assert "chapterProgress/chapterCount" not in html
