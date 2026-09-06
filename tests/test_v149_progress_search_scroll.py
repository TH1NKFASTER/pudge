from pathlib import Path
import time

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService

ROOT = Path(__file__).resolve().parents[1]


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    return LightNovelService(cfg)


def test_bookmark_progress_summary_moves_back_immediately_by_characters(tmp_path: Path) -> None:
    service = _service(tmp_path)
    now = time.time()
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO ln_books(
                title,file_path,file_type,current_chapter,current_offset,
                finished,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("Book", str(tmp_path / "book.txt"), "txt", 1, 0.9, 0, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "One", "a" * 100, "one"),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 1, "Two", "b" * 900, "two"),
        )

    service.save_bookmark(book_id, 0, 0.1, source="manual")
    summary = service.progress_summary(book_id)
    assert summary["current_chapter"] == 0
    assert summary["current_offset"] == 0.1
    assert summary["character_count"] == 1000
    assert summary["read_character_count"] == 10.0
    assert summary["reading_progress_percent"] == 1.0


def test_read_up_to_here_uses_character_offset_and_repaints_library_immediately() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    backend = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "function lnReaderCharacterOffset(target=null)" in html
    assert "const offset=lnReaderCharacterOffset(target)" in html
    assert "renderLightNovels();if(source==='manual')" in html
    assert "progress = self.light_novels.progress_summary(int(book_id))" in backend
    assert '"reading_progress_percent"' in (ROOT / "pudge/light_novels.py").read_text(encoding="utf-8")


def test_anime_polychrome_remains_active_while_scrolling() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "html.ui-anime-scrolling #current .cover-shell.polychrome { box-shadow:none!important; }" not in html
    assert "html.ui-anime-scrolling #current .cover-shell.polychrome img { filter:none!important; }" not in html
    assert "document.documentElement.classList.contains('ui-anime-scrolling')||cover.dataset.polychromeHoverActive" not in html
    assert ".cover-shell.polychrome.polychrome-wake::before" in html


def test_global_search_never_waits_for_anilist_alias_network() -> None:
    backend = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "allow_network: bool = True" in backend
    assert "aliases = self._global_anilist_aliases(linked_ids, allow_network=False)" in backend


def test_ln_find_indexes_tokens_and_dom_once_per_query() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    block = html.split("function updateLnFindResults(){", 1)[1].split("function moveLnFind(step){", 1)[0]
    assert "const nodeByKey=new Map()" in block
    assert "const tokensByParagraph=new Map(),allTokens=[]" in block
    assert "const rows=tokensByParagraph.get(pIndex)||[]" in block
    assert "reader.querySelector(`[data-ln-token=" not in block
    assert "[LN perf] find" in block
