from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from rapidfuzz import fuzz

from .branding import APP_SLUG
from .llm import build_chat_payload
from .providers.nyaa import NyaaClient, NyaaRelease
from .providers.qbittorrent import QBittorrentClient


class LightNovelError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article"}
    SKIP_TAGS = {"style", "script", "head", "title", "noscript", "template", "rt", "rp"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._seen_body = False
        self._body_depth = 0
        self._ruby_depth = 0
        self._ruby_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "body":
            self._seen_body = True
            self._body_depth += 1
            return
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "ruby":
            if self._ruby_depth == 0:
                self._ruby_parts = []
            self._ruby_depth += 1
            return
        if tag in self.BLOCK_TAGS and not self._ruby_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "body":
            self._body_depth = max(0, self._body_depth - 1)
            return
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "ruby":
            self._ruby_depth = max(0, self._ruby_depth - 1)
            if self._ruby_depth == 0:
                base = "".join(self._ruby_parts)
                # Some commercial EPUBs contain a plain-text fallback immediately
                # followed by the same <ruby> base. CSS normally hides one copy,
                # but our text extractor intentionally ignores book CSS. Keep one.
                recent = "".join(self.parts[-12:])
                if base and not recent.endswith(base):
                    self.parts.append(base)
                self._ruby_parts = []
            return
        if tag in self.BLOCK_TAGS and not self._ruby_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        # XHTML metadata/CSS before <body> must never become a "chapter".
        if self._seen_body and self._body_depth <= 0:
            return
        if self._ruby_depth:
            self._ruby_parts.append(data)
        else:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).replace("\u3000", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line)


def _safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", " ", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:160] or "Light Novel"


def _plain_html(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    parser = _TextExtractor()
    parser.feed(text)
    return parser.text()


def _volume_from_text(value: str) -> int | None:
    # NFKC makes full-width Japanese digits (３) behave like ordinary digits.
    # Keep the matcher conservative: years and random release numbers must not
    # silently become AniList volume progress.
    original = str(value or "")
    value = unicodedata.normalize("NFKC", original)
    patterns = (
        r"(?i)\b(?:vol(?:ume)?|v)\s*[._ -]*0*(\d{1,3})(?:\.\d+)?\b",
        r"(?i)\b0*(\d{1,3})(?:st|nd|rd|th)\s+volume\b",
        r"第\s*0*(\d{1,3})(?:\.\d+)?\s*巻",
        r"0*(\d{1,3})(?:\.\d+)?\s*巻",
        # Common Japanese sub-series notation, e.g. ３年生編３ -> volume 3
        # within that AniList work.
        r"(?:年生編|編)\s*0*(\d{1,3})(?:\.\d+)?(?:\D|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            number = int(match.group(1))
            if 0 < number <= 300:
                return number
    # Very common Japanese filename convention: title + full-width volume
    # number with no separator, e.g. あそびのかんけい２. Restrict this to an
    # actually full-width suffix so ordinary numeric titles are not mistaken
    # for volume metadata.
    original_match = re.search(r"([０-９]{1,3})\s*$", original)
    if original_match:
        number = int(unicodedata.normalize("NFKC", original_match.group(1)))
        if 0 < number <= 300:
            return number
    return None


def _series_title(value: str) -> str:
    """Return a stable local series title with publisher/volume suffixes removed."""
    original = html.unescape(str(value or "")).strip()
    text = unicodedata.normalize("NFKC", original)
    text = re.sub(r"[（(][^()（）]{0,80}[)）]\s*$", " ", text)
    text = re.sub(r"(?i)\b(?:light[ ._-]*novel|novel|vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}(?:\.\d+)?\b", " ", text)
    text = re.sub(r"第\s*0*\d{1,3}(?:\.\d+)?\s*巻", " ", text)
    text = re.sub(r"\s*0*\d{1,3}(?:\.\d+)?\s*巻\s*$", " ", text)
    # Japanese publishers often append the volume directly to the title with a
    # full-width digit: あそびのかんけい２. Only strip a bare suffix when the
    # title otherwise contains Japanese text, avoiding titles such as "86".
    if re.search(r"[ぁ-ゟ゠-ヿ一-鿿]", text):
        text = re.sub(r"\s*\d{1,3}\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _series_key(value: str) -> str:
    text = _series_title(value)
    text = re.sub(r"[\s\[\](){}._・･:：!！?？'\"“”‘’—–-]+", "", text)
    return text.casefold()


def _epub_metadata(path: Path) -> tuple[str, list[tuple[str, str]], tuple[bytes, str] | None]:
    with zipfile.ZipFile(path) as zf:
        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError) as exc:
            raise LightNovelError(f"Invalid EPUB container: {exc}") from exc
        rootfile = next((node for node in container.iter() if node.tag.endswith("rootfile")), None)
        if rootfile is None:
            raise LightNovelError("EPUB has no rootfile")
        opf_path = rootfile.attrib.get("full-path", "")
        if not opf_path:
            raise LightNovelError("EPUB rootfile path is empty")
        try:
            opf = ET.fromstring(zf.read(opf_path))
        except (KeyError, ET.ParseError) as exc:
            raise LightNovelError(f"Invalid EPUB package: {exc}") from exc

        title = ""
        manifest: dict[str, tuple[str, str, str]] = {}
        spine: list[tuple[str, bool]] = []
        cover_id = ""
        toc_id = ""
        for node in opf.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "title" and not title and (node.text or "").strip():
                title = (node.text or "").strip()
            elif tag == "meta" and str(node.attrib.get("name") or "").casefold() == "cover":
                cover_id = str(node.attrib.get("content") or "")
            elif tag == "item":
                item_id = node.attrib.get("id", "")
                href = node.attrib.get("href", "")
                if item_id and href:
                    manifest[item_id] = (
                        href,
                        str(node.attrib.get("media-type") or ""),
                        str(node.attrib.get("properties") or ""),
                    )
            elif tag == "spine":
                toc_id = str(node.attrib.get("toc") or "")
            elif tag == "itemref":
                idref = node.attrib.get("idref", "")
                if idref:
                    spine.append((idref, str(node.attrib.get("linear") or "yes").casefold() != "no"))

        base = PurePosixPath(opf_path).parent

        def archive_path_for(href: str, *, relative_to: PurePosixPath | None = None) -> str:
            href_path = PurePosixPath(href.split("#", 1)[0])
            parent = relative_to if relative_to is not None else base
            return str((parent / href_path).as_posix())

        # EPUB3 nav / EPUB2 NCX titles.  These are presentation metadata only;
        # chapter text always comes from the spine documents themselves.
        nav_titles: dict[str, str] = {}
        nav_item = next((entry for entry in manifest.values() if "nav" in entry[2].split()), None)
        if nav_item is not None:
            nav_href = nav_item[0]
            nav_path = archive_path_for(nav_href)
            try:
                nav_root = ET.fromstring(zf.read(nav_path))
                nav_parent = PurePosixPath(nav_path).parent
                for node in nav_root.iter():
                    if node.tag.rsplit("}", 1)[-1] != "a":
                        continue
                    href = str(node.attrib.get("href") or "")
                    label = re.sub(r"\\s+", " ", "".join(node.itertext())).strip()
                    if href and label:
                        nav_titles[archive_path_for(href, relative_to=nav_parent)] = label
            except (KeyError, ET.ParseError):
                pass
        ncx_entry = manifest.get(toc_id) if toc_id else next((entry for entry in manifest.values() if entry[1] == "application/x-dtbncx+xml"), None)
        if ncx_entry is not None:
            ncx_path = archive_path_for(ncx_entry[0])
            try:
                ncx_root = ET.fromstring(zf.read(ncx_path))
                ncx_parent = PurePosixPath(ncx_path).parent
                for point in (node for node in ncx_root.iter() if node.tag.rsplit("}", 1)[-1] == "navPoint"):
                    content = next((n for n in point.iter() if n.tag.rsplit("}", 1)[-1] == "content"), None)
                    label_node = next((n for n in point.iter() if n.tag.rsplit("}", 1)[-1] == "text"), None)
                    src = str(content.attrib.get("src") or "") if content is not None else ""
                    label = re.sub(r"\\s+", " ", "".join(label_node.itertext())).strip() if label_node is not None else ""
                    if src and label:
                        nav_titles.setdefault(archive_path_for(src, relative_to=ncx_parent), label)
            except (KeyError, ET.ParseError):
                pass

        chapters: list[tuple[str, str]] = []
        for idref, linear in spine:
            if not linear:
                continue
            entry = manifest.get(idref)
            if not entry:
                continue
            href, media_type, props = entry
            if media_type and "html" not in media_type and "xml" not in media_type:
                continue
            archive_path = archive_path_for(href)
            try:
                chapter_text = _plain_html(zf.read(archive_path))
            except KeyError:
                continue
            if not chapter_text.strip():
                continue
            # Cover/TOC/title pages are commonly marked inconsistently.  After
            # CSS/head/rt removal they contain only a handful of characters;
            # omit those structural pages without dropping a genuinely short
            # prologue/epilogue that has a TOC title.
            lower_hint = f"{idref} {href} {props}".casefold()
            structural = any(x in lower_hint for x in ("cover", "titlepage", "title-page", "toc", "nav"))
            if structural and len(chapter_text) < 120 and archive_path not in nav_titles:
                continue
            chapter_title = nav_titles.get(archive_path) or f"Chapter {len(chapters) + 1}"
            chapters.append((chapter_title, chapter_text))

        if not chapters:
            raise LightNovelError("EPUB contains no readable text chapters")

        cover_entry: tuple[str, str, str] | None = manifest.get(cover_id) if cover_id else None
        if cover_entry is None:
            cover_entry = next((entry for entry in manifest.values() if "cover-image" in entry[2].split()), None)
        if cover_entry is None:
            cover_entry = next((entry for entry in manifest.values() if entry[1].startswith("image/") and "cover" in entry[0].casefold()), None)
        cover: tuple[bytes, str] | None = None
        if cover_entry is not None:
            href, media_type, _props = cover_entry
            archive_path = archive_path_for(href)
            try:
                raw = zf.read(archive_path)
                suffix = PurePosixPath(href).suffix.casefold()
                if not suffix:
                    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(media_type.casefold(), ".jpg")
                cover = (raw, suffix)
            except KeyError:
                pass
        return title or path.stem, chapters, cover

def _txt_metadata(path: Path) -> tuple[str, list[tuple[str, str]]]:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8", "shift_jis", "euc_jp"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        current.append(line)
        current_len += len(line) + 1
        if current_len >= 20000:
            chunks.append("\n".join(current).strip())
            current, current_len = [], 0
    if current:
        chunks.append("\n".join(current).strip())
    return path.stem, [(f"Part {i + 1}", chunk) for i, chunk in enumerate(chunks) if chunk]


@dataclass(slots=True)
class LightNovelSettings:
    jiten_api_key: str = ""
    jpdb_api_token: str = ""
    study_backend: str = "jiten"
    show_furigana: bool = True
    custom_css: str = ""
    parse_ahead: str = "next"
    auto_download_nyaa: bool = False
    nyaa_category: str = "3_3"
    reader_font: str = "mincho"
    reader_theme: str = "night"
    reader_font_size: int = 22
    reader_text_color: str = "#dce7f6"
    reader_background_color: str = "#0b1420"
    reader_width: int = 900
    reader_line_height: float = 1.9
    reader_indent: float = 1.0
    reader_vertical: bool = False
    reader_mode: str = "scroll"
    translation_language: str = "en"


class LightNovelService:
    CONTENT_SCHEMA = 3
    JITEN_BASE = "https://api.jiten.moe/api"
    JPDB_BASE = "https://jpdb.io"

    def __init__(self, config: Any, *, logger: Any = None) -> None:
        self.config = config
        self.db_path = Path(config.library.database_path)
        self.root = Path(config.library.root_dir) / "Light Novels"
        self.root.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self._parse_lock = threading.Lock()
        self._last_parse_at = 0.0
        self._parse_inflight: set[str] = set()
        self._parse_inflight_lock = threading.Lock()
        self._anilist_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._state_refresh_lock = threading.Lock()
        self._state_refreshing = False
        self._state_version = 0
        self._prefetch_lock = threading.Lock()
        self._prefetch_generation = 0
        self._reader_generation = 0
        self._ensure_schema()

    def _log(self, message: str, *args: Any) -> None:
        if self.logger:
            try:
                self.logger.info(message, *args)
            except Exception:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ln_books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    file_type TEXT NOT NULL,
                    volume INTEGER,
                    anilist_id INTEGER,
                    anilist_status TEXT NOT NULL DEFAULT '',
                    anilist_progress_volumes INTEGER NOT NULL DEFAULT 0,
                    anilist_total_volumes INTEGER,
                    cover_url TEXT NOT NULL DEFAULT '',
                    current_chapter INTEGER NOT NULL DEFAULT 0,
                    current_offset REAL NOT NULL DEFAULT 0,
                    finished INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ln_chapters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    chapter_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    UNIQUE(book_id, chapter_index),
                    FOREIGN KEY(book_id) REFERENCES ln_books(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ln_chapters_hash ON ln_chapters(text_hash);
                CREATE TABLE IF NOT EXISTS ln_parse_cache (
                    text_hash TEXT PRIMARY KEY,
                    parsed_json TEXT NOT NULL,
                    parser_schema TEXT NOT NULL DEFAULT 'jiten-v1',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ln_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ln_translation_cache (
                    cache_key TEXT PRIMARY KEY,
                    target_language TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    context_text TEXT NOT NULL DEFAULT '',
                    translation TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                """
            )
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ln_books)")}
            if "content_schema" not in columns:
                conn.execute("ALTER TABLE ln_books ADD COLUMN content_schema INTEGER NOT NULL DEFAULT 1")

    def settings(self) -> LightNovelSettings:
        values: dict[str, str] = {}
        with self._connect() as conn:
            for row in conn.execute("SELECT key,value FROM ln_settings"):
                values[str(row["key"])] = str(row["value"])
        return LightNovelSettings(
            jiten_api_key=values.get("jiten_api_key", ""),
            jpdb_api_token=values.get("jpdb_api_token", ""),
            study_backend=values.get("study_backend", "jiten") if values.get("study_backend", "jiten") in {"jiten", "jpdb"} else "jiten",
            show_furigana=values.get("show_furigana", "1") != "0",
            custom_css=values.get("custom_css", ""),
            parse_ahead=values.get("parse_ahead", "next") if values.get("parse_ahead", "next") in {"current", "next", "book"} else "next",
            auto_download_nyaa=values.get("auto_download_nyaa", "0") == "1",
            nyaa_category=values.get("nyaa_category", "3_3") or "3_3",
            reader_font=values.get("reader_font", "mincho") or "mincho",
            reader_theme=values.get("reader_theme", "night") or "night",
            reader_font_size=max(12, min(72, int(float(values.get("reader_font_size", "22") or 22)))),
            reader_text_color=values.get("reader_text_color", "#dce7f6") or "#dce7f6",
            reader_background_color=values.get("reader_background_color", "#0b1420") or "#0b1420",
            reader_width=max(360, min(1600, int(float(values.get("reader_width", "900") or 900)))),
            reader_line_height=max(1.0, min(3.5, float(values.get("reader_line_height", "1.9") or 1.9))),
            reader_indent=max(0.0, min(5.0, float(values.get("reader_indent", "1.0") or 1.0))),
            reader_vertical=values.get("reader_vertical", "0") == "1",
            reader_mode=values.get("reader_mode", "scroll") if values.get("reader_mode", "scroll") in {"scroll", "pages"} else "scroll",
            translation_language=(values.get("translation_language") or ("ru" if str(getattr(getattr(self.config, "ui", None), "language", "en")).lower() == "ru" else "en")) if (values.get("translation_language") or ("ru" if str(getattr(getattr(self.config, "ui", None), "language", "en")).lower() == "ru" else "en")) in {"en", "ru"} else "en",
        )

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "jiten_api_key", "jpdb_api_token", "study_backend", "show_furigana",
            "custom_css", "parse_ahead", "auto_download_nyaa", "nyaa_category",
            "reader_font", "reader_theme", "reader_font_size", "reader_text_color", "reader_background_color",
            "reader_width", "reader_line_height", "reader_indent", "reader_vertical", "reader_mode",
            "translation_language",
        }
        current = self.settings()
        payload = {
            "jiten_api_key": str(values.get("jiten_api_key", current.jiten_api_key)).strip(),
            "jpdb_api_token": str(values.get("jpdb_api_token", current.jpdb_api_token)).strip(),
            "study_backend": str(values.get("study_backend", current.study_backend)).strip().lower(),
            "show_furigana": "1" if bool(values.get("show_furigana", current.show_furigana)) else "0",
            "custom_css": str(values.get("custom_css", current.custom_css)),
            "parse_ahead": str(values.get("parse_ahead", current.parse_ahead)).strip().lower(),
            "auto_download_nyaa": "1" if bool(values.get("auto_download_nyaa", current.auto_download_nyaa)) else "0",
            "nyaa_category": str(values.get("nyaa_category", current.nyaa_category)).strip() or "3_3",
            "reader_font": str(values.get("reader_font", current.reader_font)).strip() or "mincho",
            "reader_theme": str(values.get("reader_theme", current.reader_theme)).strip() or "night",
            "reader_font_size": str(max(12, min(72, int(float(values.get("reader_font_size", current.reader_font_size) or 22))))),
            "reader_text_color": str(values.get("reader_text_color", current.reader_text_color)).strip() or "#dce7f6",
            "reader_background_color": str(values.get("reader_background_color", current.reader_background_color)).strip() or "#0b1420",
            "reader_width": str(max(360, min(1600, int(float(values.get("reader_width", current.reader_width) or 900))))),
            "reader_line_height": str(max(1.0, min(3.5, float(values.get("reader_line_height", current.reader_line_height) or 1.9)))),
            "reader_indent": str(max(0.0, min(5.0, float(values.get("reader_indent", current.reader_indent) or 1.0)))),
            "reader_vertical": "1" if bool(values.get("reader_vertical", current.reader_vertical)) else "0",
            "reader_mode": str(values.get("reader_mode", current.reader_mode)).strip().lower(),
            "translation_language": str(values.get("translation_language", current.translation_language)).strip().lower(),
        }
        if payload["study_backend"] not in {"jiten", "jpdb"}:
            payload["study_backend"] = "jiten"
        if payload["parse_ahead"] not in {"current", "next", "book"}:
            payload["parse_ahead"] = "next"
        if payload["reader_mode"] not in {"scroll", "pages"}:
            payload["reader_mode"] = "scroll"
        if payload["translation_language"] not in {"en", "ru"}:
            payload["translation_language"] = "en"
        with self._connect() as conn:
            for key, value in payload.items():
                if key in allowed:
                    conn.execute("INSERT INTO ln_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        return self.settings_payload()

    def settings_payload(self) -> dict[str, Any]:
        s = self.settings()
        return {
            "jiten_api_key": s.jiten_api_key,
            "jpdb_api_token": s.jpdb_api_token,
            "study_backend": s.study_backend,
            "show_furigana": s.show_furigana,
            "custom_css": s.custom_css,
            "parse_ahead": s.parse_ahead,
            "auto_download_nyaa": s.auto_download_nyaa,
            "nyaa_category": s.nyaa_category,
            "reader_font": s.reader_font,
            "reader_theme": s.reader_theme,
            "reader_font_size": s.reader_font_size,
            "reader_text_color": s.reader_text_color,
            "reader_background_color": s.reader_background_color,
            "reader_width": s.reader_width,
            "reader_line_height": s.reader_line_height,
            "reader_indent": s.reader_indent,
            "reader_vertical": s.reader_vertical,
            "reader_mode": s.reader_mode,
            "translation_language": s.translation_language,
        }

    def _inherit_series_anilist(self, book_id: int) -> bool:
        """Reuse an AniList work already linked by another local volume."""
        book = self.book(int(book_id))
        key = _series_key(str(book.get("title") or Path(str(book.get("file_path") or "")).stem))
        if not key:
            return False
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ln_books WHERE id<>? AND anilist_id IS NOT NULL ORDER BY updated_at DESC",
                (int(book_id),),
            ).fetchall()
            match = next((row for row in rows if _series_key(str(row["title"] or "")) == key), None)
            if match is None:
                return False
            conn.execute(
                """UPDATE ln_books SET anilist_id=?,anilist_status=?,anilist_progress_volumes=?,
                   anilist_total_volumes=?,cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?""",
                (match["anilist_id"], match["anilist_status"], match["anilist_progress_volumes"],
                 match["anilist_total_volumes"], match["cover_url"], time.time(), int(book_id)),
            )
        self._log("LN inherited AniList series link book=%s media=%s", book_id, match["anilist_id"])
        return True

    def _propagate_series_anilist(self, book_id: int) -> int:
        book = self.book(int(book_id))
        media_id = book.get("anilist_id")
        key = _series_key(str(book.get("title") or ""))
        if not media_id or not key:
            return 0
        changed = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT id,title FROM ln_books WHERE id<>?", (int(book_id),)).fetchall()
            for row in rows:
                if _series_key(str(row["title"] or "")) != key:
                    continue
                conn.execute(
                    """UPDATE ln_books SET anilist_id=?,anilist_status=?,anilist_progress_volumes=?,
                       anilist_total_volumes=?,cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?""",
                    (media_id, book.get("anilist_status") or "", int(book.get("anilist_progress_volumes") or 0),
                     book.get("anilist_total_volumes"), book.get("cover_url") or "", time.time(), int(row["id"])),
                )
                changed += 1
        return changed

    def import_file(self, source: Path) -> dict[str, Any]:
        source = Path(source).expanduser().resolve()
        cover_blob: tuple[bytes, str] | None = None
        if source.suffix.casefold() == ".epub":
            title, chapters, cover_blob = _epub_metadata(source)
            file_type = "epub"
        elif source.suffix.casefold() == ".txt":
            title, chapters = _txt_metadata(source)
            file_type = "txt"
        else:
            raise LightNovelError("Only EPUB and TXT are supported")
        title = html.unescape(str(title or "")).strip() or source.stem
        volume = _volume_from_text(source.stem) or _volume_from_text(title)
        try:
            managed_source = source.is_relative_to(self.root.resolve())
        except (AttributeError, ValueError):
            managed_source = str(source).startswith(str(self.root.resolve()) + str(Path("/")))
        if managed_source:
            target = source
        else:
            target_dir = self.root / _safe_name(re.sub(r"(?i)\b(?:vol(?:ume)?|v)\s*[._ -]*\d{1,3}\b", "", title).strip() or title)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            if source != target:
                shutil.copy2(source, target)
        cover_url = ""
        if cover_blob is not None:
            raw_cover, cover_suffix = cover_blob
            media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(cover_suffix.casefold(), "image/jpeg")
            cover_url = f"data:{media_type};base64,{base64.b64encode(raw_cover).decode('ascii')}"
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO ln_books(title,file_path,file_type,volume,cover_url,content_schema,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(file_path) DO UPDATE SET title=excluded.title,file_type=excluded.file_type,volume=excluded.volume,cover_url=CASE WHEN excluded.cover_url<>'' THEN excluded.cover_url ELSE ln_books.cover_url END,content_schema=excluded.content_schema,updated_at=excluded.updated_at""",
                (title, str(target), file_type, volume, cover_url, self.CONTENT_SCHEMA, now, now),
            )
            row = conn.execute("SELECT id FROM ln_books WHERE file_path=?", (str(target),)).fetchone()
            book_id = int(row["id"])
            conn.execute("DELETE FROM ln_chapters WHERE book_id=?", (book_id,))
            for index, (chapter_title, text) in enumerate(chapters):
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
                    (book_id, index, chapter_title, text, text_hash),
                )
        self._inherit_series_anilist(book_id)
        return self.book(book_id)

    def reindex_outdated_sources(self) -> int:
        """Re-extract chapters after EPUB/TXT extraction rules change.

        Reader parsing bugs are persisted in ``ln_chapters``.  Merely fixing the
        extractor would leave already-imported books broken forever, so each
        source row records the extraction schema used to create its chapters.
        Re-import is local-only and preserves the book id, AniList link, reading
        position and finished state.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,file_path,file_type FROM ln_books WHERE COALESCE(content_schema,1)<? ORDER BY id",
                (self.CONTENT_SCHEMA,),
            ).fetchall()
        changed = 0
        for row in rows:
            path = Path(str(row["file_path"])).expanduser()
            if not path.is_file() or path.suffix.casefold() not in {".epub", ".txt"}:
                continue
            try:
                self.import_file(path)
                changed += 1
                self._log("LN reindexed source path=%s schema=%s", path, self.CONTENT_SCHEMA)
            except Exception as exc:
                self._log("LN source reindex skipped path=%s error=%s", path, exc)
        return changed

    def scan_downloaded(self) -> int:
        with self._connect() as conn:
            known = {str(row[0]) for row in conn.execute("SELECT file_path FROM ln_books")}
        added = 0
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in {".epub", ".txt"}:
                continue
            resolved = str(path.resolve())
            if resolved in known:
                continue
            try:
                self.import_file(path)
                known.add(resolved)
                added += 1
            except Exception as exc:
                self._log("LN import skipped path=%s error=%s", path, exc)
        return added

    def _repair_missing_volumes(self) -> int:
        repaired = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT id,title,file_path,volume FROM ln_books").fetchall()
            for row in rows:
                raw_title = str(row["title"] or "")
                clean_title = html.unescape(raw_title).strip()
                path = Path(str(row["file_path"] or ""))
                volume = int(row["volume"] or 0) or (_volume_from_text(path.stem) or _volume_from_text(clean_title) or 0)
                updates: list[str] = []
                params: list[Any] = []
                if clean_title and clean_title != raw_title:
                    updates.append("title=?"); params.append(clean_title)
                if volume and int(row["volume"] or 0) != volume:
                    updates.append("volume=?"); params.append(volume)
                if not updates:
                    continue
                updates.append("updated_at=?"); params.append(time.time()); params.append(int(row["id"]))
                conn.execute(f"UPDATE ln_books SET {','.join(updates)} WHERE id=?", params)
                repaired += 1
        if repaired:
            self._log("LN repaired local metadata rows=%s", repaired)
        return repaired

    def books(self) -> list[dict[str, Any]]:
        self._repair_missing_volumes()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT b.*,COUNT(c.id) AS chapter_count FROM ln_books b
                   LEFT JOIN ln_chapters c ON c.book_id=b.id GROUP BY b.id ORDER BY b.updated_at DESC,b.title COLLATE NOCASE"""
            ).fetchall()
        return [dict(row) for row in rows]

    def book(self, book_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ln_books WHERE id=?", (int(book_id),)).fetchone()
            if row is None:
                raise LightNovelError("Light novel not found")
            chapters = [dict(x) for x in conn.execute("SELECT id,chapter_index,title,text_hash FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (int(book_id),))]
        result = dict(row)
        result["chapters"] = chapters
        return result

    @staticmethod
    def _jiten_headers(token: str) -> dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"ApiKey {token}", "User-Agent": APP_SLUG}

    @staticmethod
    def _jpdb_headers(token: str) -> dict[str, str]:
        return {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}", "User-Agent": APP_SLUG}

    def _jiten_request(self, action: str, payload: dict[str, Any] | None = None) -> Any:
        token = self.settings().jiten_api_key
        if not token:
            raise LightNovelError("Jiten API token is not configured")
        url = f"{self.JITEN_BASE}/{action.lstrip('/')}"
        last: Exception | None = None
        for attempt in range(3):
            wait = max(0.0, 0.65 - (time.monotonic() - self._last_parse_at)) if action == "reader/parse" else 0.0
            if wait:
                time.sleep(wait)
            try:
                response = httpx.post(url, headers=self._jiten_headers(token), json=payload, timeout=30)
                self._last_parse_at = time.monotonic()
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(0.6 * (2 ** attempt))
                        continue
                # Authentication/path/client errors are deterministic: do not turn one
                # bad request into three slow requests per chapter batch.
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    detail = ""
                    try:
                        body = response.json()
                        detail = str(body.get("error_message") or body.get("detail") or "") if isinstance(body, dict) else ""
                    except Exception:
                        detail = ""
                    raise LightNovelError(f"Jiten HTTP {response.status_code}{': ' + detail if detail else ''}")
                response.raise_for_status()
                data = response.json() if response.content else {}
                if isinstance(data, dict) and data.get("error_message"):
                    raise LightNovelError(str(data["error_message"]))
                return data
            except LightNovelError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last = exc
                if attempt < 2:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                break
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                break
        raise LightNovelError(f"Jiten request failed: {last}")

    def _jpdb_request(self, action: str, payload: dict[str, Any] | None = None) -> Any:
        token = self.settings().jpdb_api_token
        if not token:
            raise LightNovelError("JPDB API token is not configured")
        try:
            response = httpx.post(f"{self.JPDB_BASE}/{action.lstrip('/')}", headers=self._jpdb_headers(token), json=payload, timeout=30)
            response.raise_for_status()
            data = response.json() if response.content else {}
            if isinstance(data, dict) and data.get("error_message"):
                raise LightNovelError(str(data["error_message"]))
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise LightNovelError(f"JPDB request failed: {exc}") from exc

    def test_study(self, backend: str) -> dict[str, Any]:
        backend = backend.casefold()
        if backend == "jpdb":
            self._jpdb_request("ping", {})
        else:
            self._jiten_request("reader/ping", {})
        return {"ok": True, "backend": backend}

    def _cached_parse(self, text_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT parsed_json FROM ln_parse_cache WHERE text_hash=?", (text_hash,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(str(row["parsed_json"]))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _parse_text(self, text: str, text_hash: str) -> dict[str, Any]:
        cached = self._cached_parse(text_hash)
        if cached is not None:
            return cached
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        # Keep payloads moderately sized while still making very few requests.
        batches: list[list[str]] = []
        current: list[str] = []
        size = 0
        for paragraph in paragraphs:
            if current and size + len(paragraph) > 14000:
                batches.append(current)
                current, size = [], 0
            current.append(paragraph)
            size += len(paragraph)
        if current:
            batches.append(current)
        all_tokens: list[Any] = []
        vocabulary: dict[tuple[int, int], Any] = {}
        with self._parse_lock:
            for batch in batches:
                result = self._jiten_request("reader/parse", {"text": batch})
                all_tokens.extend(result.get("tokens") or [])
                for item in result.get("vocabulary") or []:
                    if isinstance(item, dict):
                        try:
                            vocabulary[(int(item.get("wordId")), int(item.get("readingIndex")))] = item
                        except (TypeError, ValueError):
                            pass
        parsed = {"tokens": all_tokens, "vocabulary": list(vocabulary.values()), "paragraphs": paragraphs}
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)", (text_hash, json.dumps(parsed, ensure_ascii=False), "jiten-v1", time.time()))
        return parsed

    @staticmethod
    def _normalized_state(states: list[str]) -> str:
        lowered = {str(x).casefold() for x in states}
        if "due" in lowered or "failed" in lowered:
            return "due"
        if lowered & {"mastered", "known", "never-forget"}:
            return "known"
        if lowered & {"young", "mature", "learning"}:
            return "learning"
        if "blacklisted" in lowered:
            return "blacklisted"
        return "new"

    def _jpdb_states(self, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], list[str]]:
        if not pairs or not self.settings().jpdb_api_token:
            return {}
        result = self._jpdb_request("lookup-vocabulary", {"list": [[a, b] for a, b in pairs], "fields": ["card_state"]})
        rows = result.get("vocabulary_info") or []
        out: dict[tuple[int, int], list[str]] = {}
        for pair, row in zip(pairs, rows):
            states: list[str] = []
            if isinstance(row, list) and row:
                candidate = row[0]
                if isinstance(candidate, list):
                    states = [str(x) for x in candidate]
            out[pair] = states
        return out

    def _chapter_row(self, book_id: int, chapter_index: int, *, touch: bool = True) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ln_chapters WHERE book_id=? AND chapter_index=?", (int(book_id), int(chapter_index))).fetchone()
            if row is None:
                raise LightNovelError("Chapter not found")
            if touch:
                conn.execute("UPDATE ln_books SET current_chapter=?,updated_at=? WHERE id=?", (int(chapter_index), time.time(), int(book_id)))
        return row

    def _chapter_payload(self, row: sqlite3.Row, parsed: dict[str, Any]) -> dict[str, Any]:
        vocabulary = parsed.get("vocabulary") or []
        vocab_map: dict[tuple[int, int], dict[str, Any]] = {}
        pairs: list[tuple[int, int]] = []
        for item in vocabulary:
            if not isinstance(item, dict):
                continue
            try:
                pair = (int(item.get("wordId")), int(item.get("readingIndex")))
            except (TypeError, ValueError):
                continue
            vocab_map[pair] = item
            pairs.append(pair)
        settings = self.settings()
        if settings.study_backend == "jpdb":
            try:
                state_map = self._jpdb_states(list(dict.fromkeys(pairs)))
            except LightNovelError:
                state_map = {}
        else:
            state_map = {}
        result_vocab: list[dict[str, Any]] = []
        for pair, item in vocab_map.items():
            states = state_map.get(pair) or [str(x) for x in (item.get("knownState") or item.get("cardState") or [])]
            clone = dict(item)
            clone["states"] = states
            clone["normalizedState"] = self._normalized_state(states)
            result_vocab.append(clone)
        return {
            "book_id": int(row["book_id"]),
            "chapter_index": int(row["chapter_index"]),
            "title": str(row["title"]),
            "text": str(row["text"]),
            "paragraphs": parsed.get("paragraphs") or [],
            "tokens": parsed.get("tokens") or [],
            "vocabulary": result_vocab,
            "settings": self.settings_payload(),
            "parsing": False,
        }

    def chapter(self, book_id: int, chapter_index: int) -> dict[str, Any]:
        row = self._chapter_row(book_id, chapter_index)
        text_value = str(row["text"])
        parsed = self._parse_text(text_value, str(row["text_hash"]))
        payload = self._chapter_payload(row, parsed)
        self._schedule_parse_ahead(int(book_id), int(chapter_index))
        return payload

    def chapter_fast(self, book_id: int, chapter_index: int) -> dict[str, Any]:
        with self._prefetch_lock:
            self._reader_generation += 1
            reader_generation = self._reader_generation
            self._prefetch_generation += 1  # cancel prefetch from the previous chapter
        row = self._chapter_row(book_id, chapter_index)
        digest = str(row["text_hash"])
        cached = self._cached_parse(digest)
        if cached is not None:
            payload = self._chapter_payload(row, cached)
            self._schedule_parse_ahead(int(book_id), int(chapter_index), reader_generation=reader_generation)
            return payload
        paragraphs = [p.strip() for p in str(row["text"]).split("\n") if p.strip()]
        with self._parse_inflight_lock:
            should_start = digest not in self._parse_inflight
            if should_start:
                self._parse_inflight.add(digest)
        if should_start:
            def worker() -> None:
                try:
                    self._parse_text(str(row["text"]), digest)
                    with self._prefetch_lock:
                        still_current = reader_generation == self._reader_generation
                    if still_current:
                        self._schedule_parse_ahead(int(book_id), int(chapter_index), reader_generation=reader_generation)
                except Exception as exc:
                    self._log("LN foreground parse failed book=%s chapter=%s error=%s", book_id, chapter_index, exc)
                finally:
                    with self._parse_inflight_lock:
                        self._parse_inflight.discard(digest)
            threading.Thread(target=worker, name="ln-jiten-current", daemon=True).start()
        return {
            "book_id": int(book_id), "chapter_index": int(chapter_index), "title": str(row["title"]),
            "text": str(row["text"]), "paragraphs": paragraphs, "tokens": [], "vocabulary": [],
            "settings": self.settings_payload(), "parsing": True,
        }

    def chapter_parse_status(self, book_id: int, chapter_index: int) -> dict[str, Any]:
        row = self._chapter_row(book_id, chapter_index, touch=False)
        digest = str(row["text_hash"])
        cached = self._cached_parse(digest)
        if cached is None:
            with self._parse_inflight_lock:
                running = digest in self._parse_inflight
            return {"ready": False, "parsing": running}
        return {"ready": True, "payload": self._chapter_payload(row, cached)}

    def _schedule_parse_ahead(self, book_id: int, chapter_index: int, *, reader_generation: int | None = None) -> None:
        mode = self.settings().parse_ahead
        with self._prefetch_lock:
            if reader_generation is not None and reader_generation != self._reader_generation:
                return
            self._prefetch_generation += 1
            generation = self._prefetch_generation
        if mode == "current":
            return
        with self._connect() as conn:
            if mode == "book":
                rows = conn.execute("SELECT text,text_hash FROM ln_chapters WHERE book_id=? AND chapter_index>? ORDER BY chapter_index", (book_id, chapter_index)).fetchall()
            else:
                rows = conn.execute("SELECT text,text_hash FROM ln_chapters WHERE book_id=? AND chapter_index=?", (book_id, chapter_index + 1)).fetchall()
        pending = [(str(r["text"]), str(r["text_hash"])) for r in rows if self._cached_parse(str(r["text_hash"])) is None]
        if not pending:
            return
        def worker() -> None:
            for text, digest in pending:
                with self._prefetch_lock:
                    if generation != self._prefetch_generation:
                        return
                with self._parse_inflight_lock:
                    if digest in self._parse_inflight:
                        continue
                    self._parse_inflight.add(digest)
                try:
                    self._parse_text(text, digest)
                except Exception as exc:
                    self._log("LN parse-ahead failed: %s", exc)
                    return
                finally:
                    with self._parse_inflight_lock:
                        self._parse_inflight.discard(digest)
        threading.Thread(target=worker, name="ln-jiten-prefetch", daemon=True).start()

    @staticmethod
    def _google_translation(data: Any) -> str:
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            return ""
        chunks: list[str] = []
        for item in data[0]:
            if isinstance(item, list) and item and item[0]:
                chunks.append(str(item[0]))
        return "".join(chunks).strip()

    def _translate_online(self, text: str, target_language: str) -> str:
        response = httpx.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "ja", "tl": target_language, "dt": "t", "q": text},
            timeout=6.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        translated = self._google_translation(response.json())
        if not translated:
            raise LightNovelError("Online translation returned no text")
        return translated

    def _translate_local_llm(self, text: str, context: str, target_language: str) -> str:
        cfg = self.config.llm
        if not cfg.enabled or not str(cfg.model or "").strip():
            raise LightNovelError("Online translation is unavailable and local LLM is disabled")
        language_name = "Russian" if target_language == "ru" else "English"
        system = (
            "You translate Japanese prose from a light novel. Translate only the selected passage into "
            f"{language_name}. Use the preceding context only to resolve pronouns, omitted subjects, names, "
            "and ambiguity; never translate or repeat the context itself. Preserve tone and paragraph meaning. "
            "Return strict JSON with one string field named translation."
        )
        user = f"PRECEDING CONTEXT (up to 200 chars):\\n{context[-200:]}\\n\\nSELECTED TEXT:\\n{text}"
        payload = build_chat_payload(cfg, system, user)
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        response = httpx.post(
            f"{cfg.base_url.rstrip('/')}/api/chat",
            headers=headers,
            json=payload,
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        content = str((response.json().get("message") or {}).get("content") or "")
        try:
            decoded = json.loads(content)
            translated = str(decoded.get("translation") or "").strip() if isinstance(decoded, dict) else ""
        except json.JSONDecodeError:
            translated = content.strip()
        if not translated:
            raise LightNovelError("Local LLM returned no translation")
        return translated

    def translate_selection(self, text: str, context: str = "", target_language: str | None = None) -> dict[str, Any]:
        selected = re.sub(r"\\s+", " ", str(text or "")).strip()[:450]
        preceding = re.sub(r"\\s+", " ", str(context or "")).strip()[-200:]
        target = str(target_language or self.settings().translation_language or "en").lower()
        if target not in {"en", "ru"}:
            target = "en"
        if not selected:
            raise LightNovelError("Select Japanese text to translate")
        cache_key = hashlib.sha256(f"{target}\\0{preceding}\\0{selected}".encode("utf-8")).hexdigest()
        with self._connect() as conn:
            row = conn.execute("SELECT translation,provider FROM ln_translation_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if row is not None:
            return {"translation": str(row["translation"]), "target_language": target, "cached": True}
        provider = "google"
        try:
            translated = self._translate_online(selected, target)
        except Exception as online_exc:
            self._log("LN online translation failed; using local LLM: %s", online_exc)
            provider = "local_llm"
            try:
                translated = self._translate_local_llm(selected, preceding, target)
            except Exception as local_exc:
                raise LightNovelError(f"Translation failed: {local_exc}") from local_exc
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ln_translation_cache(cache_key,target_language,source_text,context_text,translation,provider,created_at) VALUES(?,?,?,?,?,?,?)",
                (cache_key, target, selected, preceding, translated, provider, time.time()),
            )
        return {"translation": translated, "target_language": target, "cached": False}

    def cancel_reader_background(self) -> None:
        # Running HTTP calls cannot be force-killed safely, but invalidating both
        # generations prevents their completion from spawning more prefetch work.
        with self._prefetch_lock:
            self._reader_generation += 1
            self._prefetch_generation += 1

    def update_position(self, book_id: int, chapter_index: int, offset: float) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE ln_books SET current_chapter=?,current_offset=?,updated_at=? WHERE id=?", (int(chapter_index), max(0.0, min(1.0, float(offset))), time.time(), int(book_id)))

    def study_action(self, backend: str, action: str, word_id: int, reading_index: int, *, grade: str = "good", sentence: str = "", deck_id: str | int | None = None) -> dict[str, Any]:
        backend = backend.casefold()
        if backend == "jpdb":
            if action == "review":
                self._jpdb_request("review", {"vid": int(word_id), "sid": int(reading_index), "grade": {"again":"fail","hard":"hard","good":"okay","easy":"easy"}.get(grade, "okay")})
            elif action == "add":
                target: str | int = int(deck_id) if str(deck_id or "").isdigit() else (str(deck_id or "forq") or "forq")
                self._jpdb_request("deck/add-vocabulary", {"id": target, "vocabulary": [[int(word_id), int(reading_index)]], "ignore_unknown": True})
                if sentence:
                    self._jpdb_request("set-card-sentence", {"vid": int(word_id), "sid": int(reading_index), "sentence": sentence})
            else:
                raise LightNovelError("Unsupported JPDB action")
        else:
            if action == "review":
                rating = {"again": 1, "hard": 2, "good": 3, "easy": 4}.get(grade, 3)
                self._jiten_request("srs/review", {"wordId": int(word_id), "readingIndex": int(reading_index), "rating": rating})
            elif action == "add":
                if deck_id is None or not str(deck_id).isdigit():
                    raise LightNovelError("Choose a Jiten study deck")
                self._jiten_request(f"srs/study-decks/{int(deck_id)}/words", {"wordId": int(word_id), "readingIndex": int(reading_index), "occurrences": 1, "sentence": sentence or None, "source": "pudge"})
            else:
                raise LightNovelError("Unsupported Jiten action")
        return {"ok": True}

    def decks(self, backend: str) -> list[dict[str, Any]]:
        if backend.casefold() == "jpdb":
            data = self._jpdb_request("list-user-decks", {"fields": ["id", "name"]})
            return [{"id": row[0], "name": row[1]} for row in (data.get("decks") or []) if isinstance(row, list) and len(row) >= 2]
        data = self._jiten_request("srs/reader-study-decks", {})
        return [{"id": item.get("userStudyDeckId"), "name": item.get("name")} for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def _anilist_post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.config.anilist.access_token:
            raise LightNovelError("AniList token is not configured")
        headers = {"Authorization": f"Bearer {self.config.anilist.access_token}", "Content-Type": "application/json", "Accept": "application/json"}
        response = httpx.post(self.config.anilist.endpoint, headers=headers, json={"query": query, "variables": variables}, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise LightNovelError(str(data["errors"][0].get("message") or "AniList error"))
        return data.get("data") or {}

    def anilist_literature(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            return []
        if not force and self._anilist_cache is not None and time.monotonic() - self._anilist_cache[0] < 300:
            return [dict(item) for item in self._anilist_cache[1]]
        viewer = self._anilist_post("query { Viewer { id } }", {}).get("Viewer") or {}
        uid = viewer.get("id")
        if not uid:
            return []
        collection_query = """
        query($userId:Int!){MediaListCollection(userId:$userId,type:MANGA){lists{entries{status progress progressVolumes media{id format chapters volumes status synonyms title{userPreferred romaji english native}coverImage{large}siteUrl}}}}}
        """
        collection = self._anilist_post(collection_query, {"userId": int(uid)}).get("MediaListCollection") or {}
        items: list[dict[str, Any]] = []
        for group in collection.get("lists") or []:
            for entry in group.get("entries") or []:
                media = entry.get("media") or {}
                media_format = str(media.get("format") or "").upper()
                if media_format not in {"NOVEL", "MANGA", "ONE_SHOT"}:
                    continue
                titles = media.get("title") or {}
                title = titles.get("userPreferred") or titles.get("romaji") or titles.get("native") or ""
                items.append({
                    "media_id": media.get("id"), "title": title, "titles": [x for x in [titles.get("romaji"), titles.get("english"), titles.get("native")] if x],
                    "synonyms": media.get("synonyms") or [], "format": media_format, "status": entry.get("status"),
                    "progress": entry.get("progress") or 0, "progress_volumes": entry.get("progressVolumes") or 0,
                    "chapters": media.get("chapters"), "volumes": media.get("volumes"), "media_status": media.get("status"),
                    "cover": (media.get("coverImage") or {}).get("large") or "", "site_url": media.get("siteUrl") or "",
                })
        self._anilist_cache = (time.monotonic(), [dict(item) for item in items])
        return items

    def anilist_novels(self, *, force: bool = False) -> list[dict[str, Any]]:
        return [item for item in self.anilist_literature(force=force) if str(item.get("format") or "").upper() == "NOVEL"]

    def anilist_planning_literature(self) -> list[dict[str, Any]]:
        items = self._anilist_cache[1] if self._anilist_cache is not None else []
        return [dict(item) for item in items if str(item.get("status") or "").upper() == "PLANNING"]

    @staticmethod
    def _anilist_search_text(value: str) -> str:
        text = html.unescape(str(value or "")).strip()
        text = re.sub(r"[（(][^()（）]{0,80}[)）]", " ", text)
        text = re.sub(r"(?i)\b(?:light[ ._-]*novel|novel|vol(?:ume)?|v)\s*[._ -]*[0-9０-９]{1,3}\b", " ", text)
        text = re.sub(r"第\s*[0-9０-９]{1,3}\s*巻", " ", text)
        text = re.sub(r"([0-9０-９]+年生編)\s*[0-9０-９]+$", r"\1", text.strip())
        text = re.sub(r"\s+[0-9０-９]{1,3}$", "", text.strip())
        # Japanese volume suffixes are often attached directly to the title.
        if re.search(r"[ぁ-ゟ゠-ヿ一-鿿]", text):
            text = re.sub(r"[0-9０-９]{1,3}$", "", text.strip())
        return re.sub(r"\s+", " ", text).strip()

    def search_anilist_novels(self, query: str, *, limit: int = 12) -> list[dict[str, Any]]:
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            return []
        cleaned = self._anilist_search_text(query)
        if not cleaned:
            return []
        gql = """
        query($search:String!,$perPage:Int!){Page(page:1,perPage:$perPage){media(search:$search,type:MANGA,format:NOVEL,sort:SEARCH_MATCH){id format chapters volumes status synonyms title{userPreferred romaji english native}coverImage{large}siteUrl mediaListEntry{status progress progressVolumes}}}}
        """
        data = self._anilist_post(gql, {"search": cleaned, "perPage": max(1, min(25, int(limit)))})
        out: list[dict[str, Any]] = []
        for media in (data.get("Page") or {}).get("media") or []:
            titles = media.get("title") or {}
            entry = media.get("mediaListEntry") or {}
            out.append({
                "media_id": media.get("id"), "title": titles.get("userPreferred") or titles.get("romaji") or titles.get("native") or "",
                "titles": [x for x in [titles.get("romaji"), titles.get("english"), titles.get("native")] if x], "synonyms": media.get("synonyms") or [],
                "format": "NOVEL", "status": entry.get("status") or "", "progress": entry.get("progress") or 0,
                "progress_volumes": entry.get("progressVolumes") or 0, "chapters": media.get("chapters"), "volumes": media.get("volumes"),
                "media_status": media.get("status"), "cover": (media.get("coverImage") or {}).get("large") or "", "site_url": media.get("siteUrl") or "",
            })
        return out

    def _anilist_novel_by_id(self, media_id: int) -> dict[str, Any]:
        gql = """
        query($id:Int!){Media(id:$id,type:MANGA){id format chapters volumes status synonyms title{userPreferred romaji english native}coverImage{large}siteUrl mediaListEntry{status progress progressVolumes}}}
        """
        media = self._anilist_post(gql, {"id": int(media_id)}).get("Media") or {}
        if str(media.get("format") or "").upper() != "NOVEL":
            raise LightNovelError("Selected AniList entry is not a light novel")
        titles = media.get("title") or {}; entry = media.get("mediaListEntry") or {}
        return {"media_id": media.get("id"), "title": titles.get("userPreferred") or titles.get("romaji") or "", "format": "NOVEL", "status": entry.get("status") or "", "progress": entry.get("progress") or 0, "progress_volumes": entry.get("progressVolumes") or 0, "volumes": media.get("volumes"), "chapters": media.get("chapters"), "media_status": media.get("status"), "cover": (media.get("coverImage") or {}).get("large") or "", "site_url": media.get("siteUrl") or ""}

    @staticmethod
    def _match_title(value: str) -> str:
        value = _series_title(value)
        value = re.sub(r"[\[\](){}._-]+", " ", value)
        return re.sub(r"\s+", " ", value).strip().casefold()

    def auto_bind_anilist(self) -> int:
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            return 0
        novels = self.anilist_novels()
        if not novels:
            return 0
        changed = 0
        for book in self.books():
            if book.get("anilist_id"):
                continue
            source = self._match_title(str(book.get("title") or Path(str(book.get("file_path") or "")).stem))
            if not source:
                continue
            scored = sorted(
                ((float(fuzz.WRatio(source, self._match_title(str(item.get("title") or "")))), item) for item in novels),
                key=lambda row: row[0],
                reverse=True,
            )
            if not scored:
                continue
            best_score, best = scored[0]
            margin = best_score - (scored[1][0] if len(scored) > 1 else 0.0)
            if best_score < 82.0 or (best_score < 95.0 and margin < 8.0):
                continue
            with self._connect() as conn:
                conn.execute(
                    "UPDATE ln_books SET anilist_id=?,anilist_status=?,anilist_progress_volumes=?,anilist_total_volumes=?,cover_url=?,updated_at=? WHERE id=?",
                    (int(best["media_id"]), str(best.get("status") or ""), int(best.get("progress_volumes") or 0), best.get("volumes"), str(best.get("cover") or ""), time.time(), int(book["id"])),
                )
            changed += 1
        return changed

    def bind_anilist(self, book_id: int, media_id: int, selection: dict[str, Any] | None = None) -> dict[str, Any]:
        item = dict(selection or {})
        if int(item.get("media_id") or 0) != int(media_id):
            item = self._anilist_novel_by_id(int(media_id))
        with self._connect() as conn:
            conn.execute("UPDATE ln_books SET anilist_id=?,anilist_status=?,anilist_progress_volumes=?,anilist_total_volumes=?,cover_url=CASE WHEN ?<>'' THEN ? ELSE cover_url END,updated_at=? WHERE id=?", (int(media_id), str(item.get("status") or ""), int(item.get("progress_volumes") or 0), item.get("volumes"), str(item.get("cover") or ""), str(item.get("cover") or ""), time.time(), int(book_id)))
        self._propagate_series_anilist(int(book_id))
        return self.book(book_id)

    def _save_anilist_volume(self, media_id: int, progress_volumes: int, status: str | None = None) -> dict[str, Any]:
        mutation = """
        mutation($mediaId:Int!,$progressVolumes:Int!,$status:MediaListStatus){SaveMediaListEntry(mediaId:$mediaId,progressVolumes:$progressVolumes,status:$status){status progress progressVolumes}}
        """
        variables: dict[str, Any] = {"mediaId": int(media_id), "progressVolumes": int(progress_volumes)}
        if status:
            variables["status"] = status
        return self._anilist_post(mutation, variables).get("SaveMediaListEntry") or {}

    def open_book(self, book_id: int) -> dict[str, Any]:
        book = self.book(book_id)
        media_id = book.get("anilist_id")
        old_status = str(book.get("anilist_status") or "")
        if media_id and self.config.anilist.enabled and self.config.anilist.access_token and old_status.upper() not in {"CURRENT", "REPEATING", "COMPLETED"}:
            # The reader must open immediately; AniList status sync is a side
            # effect, not a gate for local reading.  Optimistically expose
            # CURRENT and roll back only if the background mutation fails.
            book["anilist_status"] = "CURRENT"
            with self._connect() as conn:
                conn.execute("UPDATE ln_books SET anilist_status='CURRENT',updated_at=? WHERE id=?", (time.time(), int(book_id)))
            def worker() -> None:
                try:
                    entry = self._save_anilist_volume(int(media_id), int(book.get("anilist_progress_volumes") or 0), "CURRENT")
                    with self._connect() as conn:
                        conn.execute("UPDATE ln_books SET anilist_status=?,updated_at=? WHERE id=?", (str(entry.get("status") or "CURRENT"), time.time(), int(book_id)))
                except Exception as exc:
                    with self._connect() as conn:
                        conn.execute("UPDATE ln_books SET anilist_status=?,updated_at=? WHERE id=?", (old_status, time.time(), int(book_id)))
                    self._log("LN AniList CURRENT update failed: %s", exc)
            threading.Thread(target=worker, name="ln-anilist-current", daemon=True).start()
        return book

    def finish_volume(self, book_id: int) -> dict[str, Any]:
        book = self.book(book_id)
        volume = int(book.get("volume") or (int(book.get("anilist_progress_volumes") or 0) + 1))
        media_id = book.get("anilist_id")
        status = "COMPLETED" if book.get("anilist_total_volumes") and volume >= int(book["anilist_total_volumes"]) else "CURRENT"
        if media_id and self.config.anilist.enabled and self.config.anilist.access_token:
            entry = self._save_anilist_volume(int(media_id), volume, status)
            status = str(entry.get("status") or status)
        with self._connect() as conn:
            conn.execute("UPDATE ln_books SET finished=1,anilist_status=?,anilist_progress_volumes=MAX(anilist_progress_volumes,?),updated_at=? WHERE id=?", (status, volume, time.time(), int(book_id)))
        return self.book(book_id)

    def search_nyaa(self, query: str) -> list[dict[str, Any]]:
        settings = self.settings()
        client = NyaaClient(
            self.config.nyaa.base_url,
            category=settings.nyaa_category,
            proxy_mode=self.config.nyaa.proxy_mode,
            proxy_url=self.config.nyaa.proxy_url,
            pre_search_command=self.config.nyaa.pre_search_command,
        )
        releases = client.search(query, category=settings.nyaa_category)
        # Literature releases are ranked conservatively: title match first, then seeders, then size.
        words = {w for w in re.findall(r"[a-z0-9]+", query.casefold()) if len(w) > 2}
        def score(r: NyaaRelease) -> tuple[int, int, int]:
            title_words = set(re.findall(r"[a-z0-9]+", r.title.casefold()))
            overlap = len(words & title_words)
            return (overlap, int(r.seeders), int(r.size_bytes))
        releases.sort(key=score, reverse=True)
        return [
            {"title": r.title, "torrent_url": r.torrent_url, "link": r.link, "info_hash": r.info_hash, "seeders": r.seeders, "size": r.size_text, "trusted": r.trusted}
            for r in releases[:30]
        ]

    def download_nyaa_release(self, release: dict[str, Any]) -> dict[str, Any]:
        if not self.config.qbittorrent.enabled:
            raise LightNovelError("Light novel downloads currently require qBittorrent")
        item = NyaaRelease(
            title=str(release.get("title") or ""), link=str(release.get("link") or ""), torrent_url=str(release.get("torrent_url") or ""),
            info_hash=str(release.get("info_hash") or ""), size_text=str(release.get("size") or ""), size_bytes=0,
            seeders=int(release.get("seeders") or 0), leechers=0, downloads=0, trusted=bool(release.get("trusted")), remake=False,
            category_id=self.settings().nyaa_category, published="", is_batch=True, group="",
        )
        qbt = QBittorrentClient(
            self.config.qbittorrent.base_url, self.config.qbittorrent.username, self.config.qbittorrent.password,
            self.config.qbittorrent.api_key, verify_tls=self.config.qbittorrent.verify_tls,
            pre_download_command=self.config.qbittorrent.pre_download_command, auto_start_app=self.config.qbittorrent.auto_start_app,
        )
        try:
            torrent_hash = qbt.add_release(item, save_path=self.root, category=f"{APP_SLUG}-ln", tags=[APP_SLUG, "light-novel"])
        finally:
            qbt.close()
        return {"ok": True, "torrent_hash": torrent_hash}

    def auto_download_missing(self) -> list[dict[str, Any]]:
        if not self.settings().auto_download_nyaa or not self.config.qbittorrent.enabled:
            return []
        results: list[dict[str, Any]] = []
        books_by_media = {int(b["anilist_id"]): b for b in self.books() if b.get("anilist_id")}
        for novel in self.anilist_novels():
            media_id = int(novel.get("media_id") or 0)
            if not media_id or str(novel.get("status") or "").upper() not in {"PLANNING", "CURRENT"}:
                continue
            progress = int(novel.get("progress_volumes") or 0)
            next_volume = progress + 1
            local = books_by_media.get(media_id)
            if local and int(local.get("volume") or 0) >= next_volume and not int(local.get("finished") or 0):
                continue
            query = f"{novel.get('title','')} light novel volume {next_volume:02d}"
            releases = self.search_nyaa(query)
            if not releases:
                continue
            best = next((r for r in releases if int(r.get("seeders") or 0) > 0), releases[0])
            try:
                added = self.download_nyaa_release(best)
                results.append({"media_id": media_id, "volume": next_volume, "title": best["title"], **added})
            except Exception as exc:
                self._log("LN auto Nyaa failed for %s: %s", novel.get("title"), exc)
        return results

    def _state_payload_fast(self) -> dict[str, Any]:
        literature = [dict(item) for item in (self._anilist_cache[1] if self._anilist_cache is not None else [])]
        novels = [item for item in literature if str(item.get("format") or "").upper() == "NOVEL"]
        planning = [item for item in literature if str(item.get("status") or "").upper() == "PLANNING"]
        return {
            "books": self.books(), "anilist": novels, "planning": planning, "settings": self.settings_payload(),
            "refreshing": bool(self._state_refreshing), "version": int(self._state_version),
        }

    def request_state_refresh(self, *, force: bool = False) -> bool:
        with self._state_refresh_lock:
            if self._state_refreshing:
                return False
            self._state_refreshing = True

        def worker() -> None:
            try:
                self.scan_downloaded()
                self.reindex_outdated_sources()
                # Local series inheritance needs no network and should happen even
                # while AniList is unavailable.
                for book in self.books():
                    if not book.get("anilist_id"):
                        self._inherit_series_anilist(int(book["id"]))
                novels = self.anilist_novels(force=force) if self.config.anilist.enabled and self.config.anilist.access_token else []
                if novels:
                    self.auto_bind_anilist()
            except Exception as exc:
                self._log("LN background refresh failed: %s", exc)
            finally:
                with self._state_refresh_lock:
                    self._state_refreshing = False
                    self._state_version += 1

        threading.Thread(target=worker, name="ln-state-refresh", daemon=True).start()
        return True

    def state(self) -> dict[str, Any]:
        # Never block the WebView bridge on AniList/EPUB scanning. Trigger one
        # initial background fill; later refreshes are explicit and guarded.
        if self._state_version == 0 and self._anilist_cache is None and not self._state_refreshing:
            self.request_state_refresh(force=False)
        return self._state_payload_fast()

    def refresh_state(self) -> dict[str, Any]:
        started = self.request_state_refresh(force=True)
        payload = self._state_payload_fast()
        payload["refresh_started"] = started
        return payload
