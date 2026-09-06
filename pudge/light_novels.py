from __future__ import annotations

import base64
import hashlib
import html
import json
import posixpath
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

import httpx
from rapidfuzz import fuzz

from .branding import APP_SLUG
from .llm import OllamaClient, build_chat_payload
from .metadata_cache import MetadataCache
from .providers.nyaa import NyaaClient, NyaaRelease
from .providers.qbittorrent import QBittorrentClient


class LightNovelError(RuntimeError):
    pass


def _japanese_text_profile(chapters: list[tuple[str, str]]) -> dict[str, Any]:
    sample = "".join(text for _title, text in chapters)[:250_000]
    kana = len(re.findall(r"[\u3040-\u30ffー]", sample))
    han = len(re.findall(r"[\u3400-\u9fff々〆ヶ]", sample))
    japanese = kana + han
    letters = sum(1 for char in sample if char.isalpha())
    japanese_ratio = japanese / max(1, letters)
    kana_share = kana / max(1, japanese)
    accepted = bool(
        letters >= 160
        and japanese >= 100
        and kana >= 30
        and japanese_ratio >= 0.35
        and kana_share >= 0.04
    )
    return {
        "accepted": accepted,
        "sample_chars": len(sample),
        "letters": letters,
        "japanese_chars": japanese,
        "kana_chars": kana,
        "japanese_ratio": round(japanese_ratio, 4),
        "kana_share": round(kana_share, 4),
    }


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "article"}
    SKIP_TAGS = {"style", "script", "head", "title", "noscript", "template", "rt", "rp"}

    def __init__(self, image_resolver: Any | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._image_resolver = image_resolver
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
        if tag in {"img", "image"} and not self._ruby_depth and self._image_resolver is not None:
            attrs_map = {str(key or "").casefold(): str(value or "") for key, value in attrs}
            if tag == "img":
                src = attrs_map.get("src", "").strip()
            else:
                # EPUB illustrations are frequently XHTML pages containing an
                # SVG <image href=...> (or legacy xlink:href) instead of <img>.
                # ッツ Reader handles both forms; keep the same preservation
                # principle here so image-only spine pages are not lost.
                src = next((value.strip() for key, value in attrs_map.items() if key.endswith("href") and value.strip()), "")
            marker = self._image_resolver(src) if src else ""
            if marker:
                self.parts.extend(["\n", marker, "\n"])
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


def _plain_html(raw: bytes, *, image_resolver: Any | None = None) -> str:
    text = raw.decode("utf-8", errors="replace")
    parser = _TextExtractor(image_resolver=image_resolver)
    parser.feed(text)
    return parser.text()


def _ln_epub_image_marker(archive_path: str) -> str:
    token = base64.urlsafe_b64encode(str(archive_path or "").encode("utf-8")).decode("ascii").rstrip("=")
    return f"[[PUDGE_EPUB_IMAGE:{token}]]" if token else ""


def _ln_epub_image_path(marker: str) -> str:
    match = re.fullmatch(r"\[\[PUDGE_EPUB_IMAGE:([A-Za-z0-9_-]+)\]\]", str(marker or "").strip())
    if not match:
        return ""
    token = match.group(1)
    token += "=" * ((4 - len(token) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeError):
        return ""


def _is_ln_image_paragraph(value: str) -> bool:
    return bool(re.fullmatch(r"\[\[PUDGE_LN_IMAGE_URL:.+?\]\]", str(value or "").strip()))


def _is_epub_image_only_text(value: str) -> bool:
    """True when an extracted XHTML spine item contains only illustrations.

    Japanese EPUBs commonly put a full-page illustration in its own XHTML
    spine item.  That is presentation structure, not a logical chapter.
    """
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    return bool(lines) and all(
        re.fullmatch(r"\[\[PUDGE_EPUB_IMAGE:[A-Za-z0-9_-]+\]\]", line)
        for line in lines
    )


def _split_html_nav_sections(
    raw: bytes,
    targets: list[tuple[str, str]],
    *,
    image_resolver: Any | None = None,
) -> list[tuple[str, str]]:
    """Split one XHTML spine document at TOC fragment anchors.

    Some commercial EPUBs keep the whole novel in one XHTML file and expose
    logical chapters only as ``file.xhtml#chapter-id`` entries in the TOC.
    Pudge used to collapse those hrefs to the file path and therefore imported
    the whole book as one chapter.
    """
    if len(targets) < 2:
        return []
    source = raw.decode("utf-8", errors="replace")
    located: list[tuple[int, str, str]] = []
    seen_offsets: set[int] = set()
    for fragment, label in targets:
        fragment = html.unescape(unquote(str(fragment or "").strip()))
        label = re.sub(r"\s+", " ", str(label or "")).strip()
        if not fragment or not label:
            continue
        quoted = re.escape(fragment)
        pattern = re.compile(
            rf"<[^>]+\b(?:id|name)\s*=\s*(['\"]){quoted}\1[^>]*>",
            re.IGNORECASE,
        )
        match = pattern.search(source)
        if match is None:
            continue
        offset = match.start()
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        located.append((offset, label, fragment))
    if len(located) < 2:
        return []
    located.sort(key=lambda row: row[0])
    sections: list[tuple[str, str]] = []
    for index, (offset, label, _fragment) in enumerate(located):
        end = located[index + 1][0] if index + 1 < len(located) else len(source)
        text = _plain_html(source[offset:end].encode("utf-8"), image_resolver=image_resolver)
        if text.strip():
            sections.append((label, text))
    return sections if len(sections) >= 2 else []


def _romaji_to_hiragana(value: str) -> str:
    """Best-effort Hepburn -> hiragana for AniList romanized character names."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    for source, target in {"ā":"aa", "ī":"ii", "ū":"uu", "ē":"ee", "ō":"ou"}.items():
        text = text.replace(source, target)
    text = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    text = re.sub(r"[^a-z']+", "", text)
    if not text:
        return ""
    table = {
        "kya":"きゃ","kyu":"きゅ","kyo":"きょ","gya":"ぎゃ","gyu":"ぎゅ","gyo":"ぎょ",
        "sha":"しゃ","shu":"しゅ","sho":"しょ","sya":"しゃ","syu":"しゅ","syo":"しょ",
        "jya":"じゃ","jyu":"じゅ","jyo":"じょ","cha":"ちゃ","chu":"ちゅ","cho":"ちょ","cya":"ちゃ","cyu":"ちゅ","cyo":"ちょ",
        "nya":"にゃ","nyu":"にゅ","nyo":"にょ","hya":"ひゃ","hyu":"ひゅ","hyo":"ひょ",
        "bya":"びゃ","byu":"びゅ","byo":"びょ","pya":"ぴゃ","pyu":"ぴゅ","pyo":"ぴょ",
        "mya":"みゃ","myu":"みゅ","myo":"みょ","rya":"りゃ","ryu":"りゅ","ryo":"りょ",
        "shi":"し","chi":"ち","tsu":"つ","dzu":"づ","dji":"ぢ",
        "ja":"じゃ","ju":"じゅ","jo":"じょ",
        "ka":"か","ki":"き","ku":"く","ke":"け","ko":"こ","ga":"が","gi":"ぎ","gu":"ぐ","ge":"げ","go":"ご",
        "sa":"さ","si":"し","su":"す","se":"せ","so":"そ","za":"ざ","ji":"じ","zi":"じ","zu":"ず","ze":"ぜ","zo":"ぞ",
        "ta":"た","ti":"ち","tu":"つ","te":"て","to":"と","da":"だ","di":"ぢ","du":"づ","de":"で","do":"ど",
        "na":"な","ni":"に","nu":"ぬ","ne":"ね","no":"の",
        "ha":"は","hi":"ひ","fu":"ふ","hu":"ふ","he":"へ","ho":"ほ",
        "ba":"ば","bi":"び","bu":"ぶ","be":"べ","bo":"ぼ","pa":"ぱ","pi":"ぴ","pu":"ぷ","pe":"ぺ","po":"ぽ",
        "ma":"ま","mi":"み","mu":"む","me":"め","mo":"も",
        "ya":"や","yu":"ゆ","yo":"よ","ra":"ら","ri":"り","ru":"る","re":"れ","ro":"ろ",
        "wa":"わ","wo":"を","va":"ゔぁ","vi":"ゔぃ","vu":"ゔ","ve":"ゔぇ","vo":"ゔぉ",
        "a":"あ","i":"い","u":"う","e":"え","o":"お",
    }
    out=[]; index=0; vowels=set("aeiou")
    while index < len(text):
        char=text[index]
        if char=="'":
            index+=1; continue
        if index+1<len(text) and char==text[index+1] and char not in vowels and char!="n":
            out.append("っ"); index+=1; continue
        matched=False
        for width in (3,2,1):
            kana=table.get(text[index:index+width])
            if kana:
                out.append(kana); index+=width; matched=True; break
        if matched:
            continue
        if char=="n":
            next_char=text[index+1] if index+1<len(text) else ""
            if not next_char or next_char=="'" or next_char not in vowels|{"y"}:
                out.append("ん"); index+=1; continue
        return ""
    return "".join(out)


def _volume_from_text(value: str) -> int | None:
    # NFKC makes full-width Japanese digits (３) behave like ordinary digits.
    # Keep the matcher conservative: years and random release numbers must not
    # silently become AniList volume progress.
    original = str(value or "")
    # Ignore trailing publisher / alternate-title annotations when looking for
    # the local volume marker.  Store filenames such as
    # ``...へ２<...へ> (MF文庫J)`` still mean volume 2.
    volume_source = re.sub(r"[（(][^()（）]{0,80}[)）]\s*$", "", original).strip()
    volume_source = re.sub(r"\s*[<＜][^<>＜＞]{1,160}[>＞]\s*$", "", volume_source).strip()
    value = unicodedata.normalize("NFKC", volume_source)
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
    original_match = re.search(r"([０-９]{1,3})\s*$", volume_source)
    if original_match:
        number = int(unicodedata.normalize("NFKC", original_match.group(1)))
        if 0 < number <= 300:
            return number
    # Japanese filenames also commonly attach an ASCII volume directly to the title,
    # e.g. 狼と香辛料2. Require Japanese characters before the suffix so years/ASINs
    # and numeric-only titles are not treated as volumes.
    ascii_match = re.search(r"[ぁ-ゟ゠-ヿ一-鿿].*?(\d{1,3})\s*$", value)
    if ascii_match:
        number = int(ascii_match.group(1))
        if 0 < number <= 300:
            return number
    return None


def _series_title(value: str) -> str:
    """Return a stable local series title with publisher/volume suffixes removed."""
    original = html.unescape(str(value or "")).strip()
    text = unicodedata.normalize("NFKC", original)
    text = re.sub(r"[（(][^()（）]{0,80}[)）]\s*$", " ", text)
    # Some Japanese stores encode the canonical/alternate series title after the
    # volume as a trailing angle-bracket annotation, e.g.
    # ``ようこそ実力至上主義の教室へ２<ようこそ実力至上主義の教室へ>``.  Treat that
    # suffix as metadata before stripping the local volume number; otherwise the
    # ``２`` is no longer terminal and can leak into AniList matching as a
    # second-year/sub-series hint.
    text = re.sub(r"\s*[<＜][^<>＜＞]{1,160}[>＞]\s*$", " ", text)
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



def _epub_visible_contents_labels(value: str) -> list[str]:
    """Recover chapter labels from a visible Contents/目次 page without links.

    Some Japanese EPUBs render the TOC as ordinary paragraphs rather than
    anchors.  The package navigation can simultaneously contain only the book
    title, so link-based recovery has nothing to work with.  Keep this fallback
    deliberately narrow: a Contents marker must appear near the top and at least
    three short, distinct labels must follow it.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(value or "").splitlines()]
    lines = [line for line in lines if line and not _is_epub_image_only_text(line)]
    if not lines:
        return []

    marker_index = -1
    for index, line in enumerate(lines[:5]):
        key = unicodedata.normalize("NFKC", line).casefold().strip(" :：-—")
        if key in {"contents", "content", "table of contents", "toc", "目次"}:
            marker_index = index
            break
    if marker_index < 0:
        return []

    labels: list[str] = []
    seen: set[str] = set()
    prose_like = 0
    for raw in lines[marker_index + 1 : marker_index + 81]:
        label = re.sub(r"^[\s•·・●○◆◇▪▫■□▶▷►▸\-–—]+", "", raw).strip()
        label = re.sub(r"^(?:\d{1,3}|[０-９]{1,3})[.)、．:]\s*", "", label).strip()
        if not label or len(label) > 80:
            prose_like += 1
            continue
        key = unicodedata.normalize("NFKC", label).casefold().strip()
        if key in {"contents", "content", "table of contents", "toc", "目次"}:
            continue
        if re.fullmatch(r"(?:page\s*)?\d{1,4}", key):
            continue
        # A TOC is a run of short labels, not prose.  A couple of noisy lines are
        # tolerated for publisher ornaments/page numbers.
        if len(label) > 48 or len(label.split()) > 8:
            prose_like += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        labels.append(label[:160])

    if len(labels) < 3 or prose_like > max(3, len(labels)):
        return []
    return labels


def _html_chapter_title(raw: bytes) -> str:
    """Best-effort semantic title when EPUB navigation omits a spine label."""
    source = raw.decode("utf-8", errors="replace")
    candidates: list[str] = []
    for pattern in (r"<h[1-4][^>]*>(.*?)</h[1-4]>", r"<title[^>]*>(.*?)</title>"):
        for match in re.finditer(pattern, source, flags=re.I | re.S):
            text = re.sub(r"<[^>]+>", " ", match.group(1))
            text = html.unescape(re.sub(r"\s+", " ", text)).strip()
            if text:
                candidates.append(text)
        if candidates:
            break
    for title in candidates:
        key = unicodedata.normalize("NFKC", title).casefold().strip()
        if key in {"contents", "content", "toc", "table of contents", "cover", "navigation", "nav"}:
            continue
        if re.fullmatch(r"chapter\s*\d+", key):
            continue
        return title[:160]
    return ""

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

        def href_parts(href: str) -> tuple[str, str]:
            raw = str(href or "")
            path_part, separator, fragment = raw.partition("#")
            return unquote(path_part), unquote(fragment) if separator else ""

        def archive_path_for(href: str, *, relative_to: PurePosixPath | None = None) -> str:
            path_part, _fragment = href_parts(href)
            href_path = PurePosixPath(path_part)
            parent = relative_to if relative_to is not None else base
            return str((parent / href_path).as_posix())

        # EPUB3 nav / EPUB2 NCX titles and fragment targets. Some books keep
        # multiple TOC chapters in a single spine XHTML document, so fragments
        # are structural metadata rather than something we can discard.
        nav_titles: dict[str, str] = {}
        nav_targets: dict[str, list[tuple[str, str]]] = {}

        def add_nav_target(href: str, label: str, *, relative_to: PurePosixPath) -> None:
            label = re.sub(r"\s+", " ", str(label or "")).strip()
            if not href or not label:
                return
            archive_path = archive_path_for(href, relative_to=relative_to)
            _path_part, fragment = href_parts(href)
            nav_titles.setdefault(archive_path, label)
            if fragment:
                row = (fragment, label)
                rows = nav_targets.setdefault(archive_path, [])
                if row not in rows:
                    rows.append(row)

        nav_item = next((entry for entry in manifest.values() if "nav" in entry[2].split()), None)
        if nav_item is not None:
            nav_href = nav_item[0]
            nav_path = archive_path_for(nav_href)
            try:
                nav_root = ET.fromstring(zf.read(nav_path))
                nav_parent = PurePosixPath(nav_path).parent
                nav_nodes = [node for node in nav_root.iter() if node.tag.rsplit("}", 1)[-1] == "nav"]
                toc_nodes = [
                    node for node in nav_nodes
                    if any(
                        key.rsplit("}", 1)[-1] == "type" and "toc" in str(value or "").casefold().split()
                        for key, value in node.attrib.items()
                    )
                ]
                search_roots = toc_nodes or nav_nodes[:1] or [nav_root]
                for root in search_roots:
                    for node in root.iter():
                        if node.tag.rsplit("}", 1)[-1] != "a":
                            continue
                        href = str(node.attrib.get("href") or "")
                        label = re.sub(r"\s+", " ", "".join(node.itertext())).strip()
                        add_nav_target(href, label, relative_to=nav_parent)
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
                    label = re.sub(r"\s+", " ", "".join(label_node.itertext())).strip() if label_node is not None else ""
                    add_nav_target(src, label, relative_to=ncx_parent)
            except (KeyError, ET.ParseError):
                pass

        # Some Japanese commercial EPUBs ship an unhelpful package TOC where
        # every entry is just the series title, while a visible ``Contents``
        # page in the spine links to the real chapter names. Treat a spine
        # document with several internal links to other spine targets as a
        # secondary TOC. This is intentionally conservative so ordinary
        # cross-links/footnotes do not become chapter metadata.
        spine_archive_paths: set[str] = set()
        for spine_idref, spine_linear in spine:
            if not spine_linear:
                continue
            spine_entry = manifest.get(spine_idref)
            if not spine_entry:
                continue
            spine_archive_paths.add(archive_path_for(spine_entry[0]))
        content_nav_titles: dict[str, str] = {}
        content_nav_targets: dict[str, list[tuple[str, str]]] = {}
        visible_contents_labels: list[str] = []
        for spine_idref, spine_linear in spine:
            if not spine_linear:
                continue
            spine_entry = manifest.get(spine_idref)
            if not spine_entry:
                continue
            source_archive = archive_path_for(spine_entry[0])
            try:
                source_root = ET.fromstring(zf.read(source_archive))
            except (KeyError, ET.ParseError):
                continue
            source_parent = PurePosixPath(source_archive).parent
            source_plain = _plain_html(zf.read(source_archive))
            page_labels = _epub_visible_contents_labels(source_plain)
            if page_labels and not visible_contents_labels:
                visible_contents_labels = page_labels
            rows: list[tuple[str, str, str]] = []
            seen_rows: set[tuple[str, str]] = set()
            for node in source_root.iter():
                if node.tag.rsplit("}", 1)[-1] != "a":
                    continue
                href = str(node.attrib.get("href") or "").strip()
                label = re.sub(r"\s+", " ", "".join(node.itertext())).strip()
                if not href or not label or href.startswith(("http:", "https:", "mailto:", "javascript:")):
                    continue
                path_part, separator, fragment = href.partition("#")
                if not path_part:
                    target_archive = source_archive
                else:
                    target_archive = posixpath.normpath(
                        str((source_parent / PurePosixPath(unquote(path_part))).as_posix())
                    )
                fragment = unquote(fragment) if separator else ""
                if target_archive not in spine_archive_paths:
                    continue
                if target_archive == source_archive and not fragment:
                    continue
                key = (target_archive, fragment)
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                rows.append((target_archive, fragment, label[:160]))
            source_text = re.sub(r"\s+", " ", "".join(source_root.itertext())).strip()
            looks_like_contents = bool(page_labels) or "contents" in source_text.casefold() or "目次" in source_text
            if (len(rows) < 3 or len({label for _path, _fragment, label in rows}) < 2
                    or (not looks_like_contents and len(rows) < 5)):
                continue
            for target_archive, fragment, label in rows:
                content_nav_titles.setdefault(target_archive, label)
                if fragment:
                    target_rows = content_nav_targets.setdefault(target_archive, [])
                    row = (fragment, label)
                    if row not in target_rows:
                        target_rows.append(row)

        def best_fragment_targets(archive_path: str) -> list[tuple[str, str]]:
            official = list(nav_targets.get(archive_path, []))
            secondary = list(content_nav_targets.get(archive_path, []))
            if not secondary:
                return official
            official_labels = [re.sub(r"\s+", " ", label).strip() for _fragment, label in official if label]
            official_unique = {label.casefold() for label in official_labels}
            official_is_book_title = bool(official_labels) and all(
                _series_key(label) == _series_key(title) for label in official_labels
            )
            if not official or len(official_unique) <= 1 or official_is_book_title:
                return secondary
            return official

        chapters: list[tuple[str, str]] = []
        pending_images: list[str] = []

        def append_content_chapter(chapter_title: str, chapter_text: str, hint: str = "") -> None:
            nonlocal pending_images
            image_only = _is_epub_image_only_text(chapter_text)
            if image_only:
                # Match the useful part of ッツ Reader's EPUB model: image-only
                # spine items remain in reading order instead of being discarded
                # just because their href says cover/titlepage/front-matter. Pudge
                # has no separate no-text section model, so pre-chapter images are
                # prepended to the first logical chapter and later illustrations
                # stay attached to the preceding chapter.
                if chapters:
                    old_title, old_text = chapters[-1]
                    chapters[-1] = (old_title, f"{old_text.rstrip()}\n{chapter_text.strip()}".strip())
                else:
                    pending_images.append(chapter_text.strip())
                return
            if pending_images:
                chapter_text = "\n".join([*pending_images, chapter_text]).strip()
                pending_images = []
            chapters.append((chapter_title, chapter_text))

        for idref, linear in spine:
            entry = manifest.get(idref)
            if not entry:
                continue
            href, media_type, props = entry
            if media_type and "html" not in media_type and "xml" not in media_type:
                continue
            archive_path = archive_path_for(href)
            try:
                raw_chapter = zf.read(archive_path)
            except KeyError:
                continue
            lower_hint = f"{idref} {href} {props}".casefold()
            chapter_parent = PurePosixPath(archive_path).parent
            def image_resolver(src: str) -> str:
                raw_src = str(src or "").split("#", 1)[0].strip()
                if not raw_src or raw_src.startswith(("data:", "http:", "https:")):
                    return ""
                resolved = posixpath.normpath(str((chapter_parent / PurePosixPath(unquote(raw_src))).as_posix()))
                return _ln_epub_image_marker(resolved)
            split_sections = _split_html_nav_sections(raw_chapter, best_fragment_targets(archive_path), image_resolver=image_resolver)
            if split_sections:
                for chapter_title, chapter_text in split_sections:
                    if not _is_epub_image_only_text(chapter_text) and _is_technical_epub_section(chapter_title, chapter_text, lower_hint):
                        continue
                    append_content_chapter(chapter_title, chapter_text, lower_hint)
                continue
            chapter_text = _plain_html(raw_chapter, image_resolver=image_resolver)
            if not chapter_text.strip():
                continue
            if not linear and not _is_epub_image_only_text(chapter_text):
                continue
            # Cover/TOC/title pages are commonly marked inconsistently.  After
            # CSS/head/rt removal they contain only a handful of characters;
            # omit those structural pages without dropping a genuinely short
            # prologue/epilogue that has a TOC title.
            structural = any(x in lower_hint for x in ("cover", "titlepage", "title-page", "toc", "nav"))
            if structural and len(chapter_text) < 120 and archive_path not in nav_titles and not _is_epub_image_only_text(chapter_text):
                continue
            content_title = content_nav_titles.get(archive_path) or ''
            nav_title = nav_titles.get(archive_path) or ''
            semantic_title = _html_chapter_title(raw_chapter)
            nav_is_book_title = bool(nav_title) and _series_key(nav_title) == _series_key(title)
            if content_title and (not nav_title or nav_is_book_title or re.fullmatch(r'(?i)chapter\s*\d+', nav_title.strip())):
                chapter_title = content_title
            elif semantic_title and (not nav_title or nav_is_book_title or re.fullmatch(r'(?i)chapter\s*\d+', nav_title.strip())):
                chapter_title = semantic_title
            else:
                chapter_title = nav_title or semantic_title or f"Chapter {len(chapters) + 1}"
            if not _is_epub_image_only_text(chapter_text) and _is_technical_epub_section(chapter_title, chapter_text, lower_hint):
                continue
            append_content_chapter(chapter_title, chapter_text, lower_hint)

        # A visible Contents/目次 page may contain only plain paragraphs (no
        # hrefs).  Once that page has been removed as structural metadata, use
        # its ordered labels to repair only generic/repeated chapter titles.
        if visible_contents_labels:
            chapter_title_counts: dict[str, int] = {}
            for candidate_title, _candidate_text in chapters:
                candidate_key = unicodedata.normalize("NFKC", str(candidate_title or "")).casefold().strip()
                if candidate_key:
                    chapter_title_counts[candidate_key] = chapter_title_counts.get(candidate_key, 0) + 1
            repaired: list[tuple[str, str]] = []
            label_index = 0
            for chapter_title, chapter_text in chapters:
                if label_index < len(visible_contents_labels):
                    label = visible_contents_labels[label_index]
                    title_key = _series_key(chapter_title)
                    exact_title_key = unicodedata.normalize("NFKC", str(chapter_title or "")).casefold().strip()
                    label_key = _series_key(label)
                    generic = bool(
                        (title_key and title_key == _series_key(title))
                        or chapter_title_counts.get(exact_title_key, 0) >= 2
                        or re.fullmatch(r"(?i)chapter\s*\d+", str(chapter_title or "").strip())
                    )
                    if label_key and title_key == label_key:
                        label_index += 1
                    elif generic:
                        chapter_title = label
                        label_index += 1
                repaired.append((chapter_title, chapter_text))
            if label_index >= 3:
                chapters = repaired

        # Repeating the package/book title for every story chapter is not useful
        # metadata. If neither TOC nor semantic headings could improve it, keep
        # navigation deterministic with Chapter N labels instead.
        exact_title_groups: dict[str, list[int]] = {}
        for index, (chapter_title, _chapter_text) in enumerate(chapters):
            key = unicodedata.normalize("NFKC", str(chapter_title or "")).casefold().strip()
            if key:
                exact_title_groups.setdefault(key, []).append(index)
        repeated_set: set[int] = set()
        package_key = _series_key(title)
        for key, indices in exact_title_groups.items():
            sample_title = chapters[indices[0]][0]
            package_like = bool(_series_key(sample_title) and _series_key(sample_title) == package_key)
            dominant = len(indices) >= 3 and len(indices) * 2 >= max(1, len(chapters))
            if len(indices) >= 2 and (package_like or dominant):
                repeated_set.update(indices)
        if repeated_set:
            fallback_number = 0
            numbered_chapters: list[tuple[str, str]] = []
            for index, (chapter_title, chapter_text) in enumerate(chapters):
                if index in repeated_set:
                    fallback_number += 1
                    chapter_title = f"Chapter {fallback_number}"
                numbered_chapters.append((chapter_title, chapter_text))
            chapters = numbered_chapters

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


def _is_technical_epub_section(title: str, text: str, hint: str = "") -> bool:
    normalized_title = unicodedata.normalize("NFKC", str(title or "")).casefold().strip()
    normalized_hint = unicodedata.normalize("NFKC", str(hint or "")).casefold()
    normalized_text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    if _epub_visible_contents_labels(str(text or "")):
        return True
    if any(
        marker in normalized_hint
        for marker in ("colophon", "copyright", "imprint", "titlepage", "title-page")
    ):
        return True
    if normalized_title in {
        "奥付",
        "著者紹介",
        "作者紹介",
        "copyright",
        "colophon",
        "imprint",
        "about the author",
    }:
        return True
    rights_markers = sum(
        marker in normalized_text
        for marker in (
            "著作権",
            "無断転載",
            "複製・転載",
            "正当な権利を有する",
            "all rights reserved",
            "isbn",
        )
    )
    if rights_markers >= 2:
        return True
    if (
        len(normalized_text) < 1500
        and normalized_title
        and normalized_title in normalized_text
        and any(marker in normalized_text for marker in ("著者", "作者", "プロフィール", "略歴"))
    ):
        return True
    if "author" in normalized_hint and len(normalized_text) < 2000:
        return True
    return False

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
    furigana_on_hover: bool = True
    furigana_on_reading: bool = True
    show_pitch_accent: bool = True
    furigana_unknown_only: bool = True
    word_mark_style: str = "underline"
    study_card_mode: str = "button"
    study_card_triggers: str = "MouseLeft"
    furigana_states: str = "new,learning,young,mature,due,failed"
    underline_states: str = "new,learning,young,mature,due,failed,known,mastered,never-forget,blacklisted"
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
    blur_images: bool = False
    auto_bookmarks: bool = True
    word_color_theme: str = "balanced"
    word_color_new: str = "#f3f6fb"
    word_color_learning: str = "#f4bd63"
    word_color_due: str = "#ff7d8c"
    word_color_known: str = "#57d38c"
    word_color_blacklisted: str = "#7d8795"
    pitch_accent_color: str = "#9ec5ff"
    translation_language: str = "en"
    audiobook_generation_provider: str = "off"
    audiobook_tts_model: str = "tts-1"
    audiobook_character_voices: bool = False
    irodori_tts_enabled: bool = False
    irodori_tts_auto_generate: bool = False
    irodori_tts_url: str = "http://127.0.0.1:8088"
    irodori_tts_api_key: str = ""
    irodori_tts_voice: str = "none"
    irodori_tts_caption: str = ""
    irodori_tts_speed: float = 1.0


class LightNovelService:
    CONTENT_SCHEMA = 10
    JITEN_BASE = "https://api.jiten.moe/api"
    JPDB_BASE = "https://jpdb.io"
    WORD_COLOR_THEMES = {
        "balanced", "jiten", "jpdb", "focus", "underline", "none", "custom"
    }

    JITEN_STUDY_STATES = (
        "new", "learning", "young", "mature", "due", "failed",
        "known", "mastered", "never-forget", "blacklisted",
    )
    DEFAULT_FURIGANA_STATES = "new,learning,young,mature,due,failed"
    DEFAULT_UNDERLINE_STATES = "new,learning,young,mature,due,failed,known,mastered,never-forget,blacklisted"

    @classmethod
    def _jiten_state_csv(cls, value: Any, default: str) -> str:
        if isinstance(value, (list, tuple, set)):
            raw = [str(item or "").strip().lower() for item in value]
        else:
            raw = [
                part.strip().lower()
                for part in re.split(r"[,;\n]+", str(value or ""))
            ]
        allowed = set(cls.JITEN_STUDY_STATES)
        result = []
        for state in raw:
            if state in allowed and state not in result:
                result.append(state)
        if not result:
            result = [part for part in default.split(",") if part]
        return ",".join(result)

    @staticmethod
    def _word_color(value: Any, fallback: str) -> str:
        candidate = str(value or "").strip().lower()
        return candidate if re.fullmatch(r"#[0-9a-f]{6}", candidate) else fallback

    @staticmethod
    def _study_trigger_codes(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            raw = [str(item or "").strip() for item in value]
        else:
            raw = [part.strip() for part in re.split(r"[,;\n]+", str(value or ""))]
        aliases = {
            "left": "MouseLeft", "lmb": "MouseLeft", "mouse1": "MouseLeft",
            "middle": "MouseMiddle", "mmb": "MouseMiddle", "mouse2": "MouseMiddle",
            "right": "MouseRight", "rmb": "MouseRight", "mouse3": "MouseRight",
        }
        result: list[str] = []
        for item in raw:
            if not item:
                continue
            code = aliases.get(item.casefold(), item)
            if code not in {"MouseLeft", "MouseMiddle", "MouseRight"} and not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9]{1,31}", code
            ):
                continue
            if code not in result:
                result.append(code)
            if len(result) >= 8:
                break
        return result or ["MouseLeft"]

    def __init__(self, config: Any, *, logger: Any = None) -> None:
        self.config = config
        self.db_path = Path(config.library.database_path)
        self.root = Path(config.library.root_dir) / "Light Novels"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cover_cache_dir = Path(config.library.cover_cache_dir)
        self.cover_cache_dir.mkdir(parents=True, exist_ok=True)
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
        self._anilist_bind_lock = threading.Lock()
        self._anilist_bind_inflight: set[int] = set()
        self._character_cache = MetadataCache(
            Path(config.paths.cache_dir), "anilist-characters", schema="v2"
        )
        self._jiten_media_cache = MetadataCache(
            Path(config.paths.cache_dir), "jiten-media-stats", schema="v2"
        )
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
                    anilist_user_score REAL,
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
                CREATE TABLE IF NOT EXISTS ln_deleted_sources (
                    file_path TEXT PRIMARY KEY,
                    deleted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ln_scan_rejections (
                    file_path TEXT PRIMARY KEY,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    checked_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ln_bookmarks (
                    book_id INTEGER PRIMARY KEY,
                    chapter_index INTEGER NOT NULL DEFAULT 0,
                    offset REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'manual',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(book_id) REFERENCES ln_books(id) ON DELETE CASCADE
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
                CREATE TABLE IF NOT EXISTS character_name_overrides (
                    media_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    preferred TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(media_id,source)
                );
                CREATE TABLE IF NOT EXISTS media_identities (
                    kind TEXT NOT NULL,
                    local_id INTEGER NOT NULL,
                    anilist_id INTEGER NOT NULL,
                    anilist_type TEXT NOT NULL DEFAULT 'MANGA',
                    title TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    site_url TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(kind,local_id)
                );
                """
            )
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ln_books)")}
            if "content_schema" not in columns:
                conn.execute("ALTER TABLE ln_books ADD COLUMN content_schema INTEGER NOT NULL DEFAULT 1")
            if "anilist_user_score" not in columns:
                conn.execute("ALTER TABLE ln_books ADD COLUMN anilist_user_score REAL")

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
            furigana_on_hover=values.get("furigana_on_hover", "1") != "0",
            furigana_on_reading=values.get("furigana_on_reading", "1") != "0",
            furigana_states=self._jiten_state_csv(
                values.get("furigana_states"), self.DEFAULT_FURIGANA_STATES
            ),
            underline_states=self._jiten_state_csv(
                values.get("underline_states"), self.DEFAULT_UNDERLINE_STATES
            ),
            show_pitch_accent=values.get("show_pitch_accent", "1") != "0",
            furigana_unknown_only=values.get("furigana_unknown_only", "1") != "0",
            word_mark_style=(
                "color"
                if values.get("word_mark_style", "underline") == "none"
                else (
                    values.get("word_mark_style", "underline")
                    if values.get("word_mark_style", "underline") in {"underline", "color"}
                    else "underline"
                )
            ),
            study_card_mode=(
                values.get("study_card_mode", "button")
                if values.get("study_card_mode", "button") in {"button", "hover"}
                else "button"
            ),
            study_card_triggers=",".join(
                self._study_trigger_codes(values.get("study_card_triggers", "MouseLeft"))
            ),
            custom_css=values.get("custom_css", ""),
            parse_ahead="next",  # automatic: current + next chapter
            auto_download_nyaa=values.get("auto_download_nyaa", "0") == "1",
            nyaa_category=values.get("nyaa_category", "3_3") or "3_3",
            reader_font=values.get("reader_font", "mincho") or "mincho",
            reader_theme=values.get("reader_theme", "sumi") or "night",
            reader_font_size=max(12, min(72, int(float(values.get("reader_font_size", "22") or 22)))),
            reader_text_color=values.get("reader_text_color", "#c9c7c2") or "#dce7f6",
            reader_background_color=values.get("reader_background_color", "#000000") or "#0b1420",
            reader_width=max(360, min(2400, int(float(values.get("reader_width", "900") or 900)))),
            reader_line_height=max(1.0, min(3.5, float(values.get("reader_line_height", "1.9") or 1.9))),
            reader_indent=max(0.0, min(5.0, float(values.get("reader_indent", "1.0") or 1.0))),
            reader_vertical=values.get("reader_vertical", "0") == "1",
            reader_mode=values.get("reader_mode", "scroll") if values.get("reader_mode", "scroll") in {"scroll", "pages"} else "scroll",
            blur_images=values.get("blur_images", "0") == "1",
            auto_bookmarks=values.get("auto_bookmarks", "1") != "0",
            word_color_theme=(
                values.get("word_color_theme", "balanced")
                if values.get("word_color_theme", "balanced") in self.WORD_COLOR_THEMES
                else "balanced"
            ),
            word_color_new=self._word_color(values.get("word_color_new"), "#f3f6fb"),
            word_color_learning=self._word_color(values.get("word_color_learning"), "#f4bd63"),
            word_color_due=self._word_color(values.get("word_color_due"), "#ff7d8c"),
            word_color_known=self._word_color(values.get("word_color_known"), "#57d38c"),
            word_color_blacklisted=self._word_color(
                values.get("word_color_blacklisted"), "#7d8795"
            ),
            pitch_accent_color=self._word_color(
                values.get("pitch_accent_color"), "#9ec5ff"
            ),
            # Selection translation is intentionally not an independent setting.
            # It always follows General -> Language; legacy ln_settings rows are ignored.
            translation_language=(
                "ru"
                if str(getattr(getattr(self.config, "ui", None), "language", "en")).lower() == "ru"
                else "en"
            ),
            audiobook_generation_provider=(
                (values.get("audiobook_generation_provider") or "").strip().lower()
                or ("irodori" if values.get("irodori_tts_enabled", "0") == "1" else "off")
            ),
            audiobook_tts_model=(values.get("audiobook_tts_model", "tts-1").strip() or "tts-1"),
            audiobook_character_voices=values.get("audiobook_character_voices", "0") == "1",
            irodori_tts_enabled=(
                ((values.get("audiobook_generation_provider") or "").strip().lower() or ("irodori" if values.get("irodori_tts_enabled", "0") == "1" else "off")) != "off"
            ),
            irodori_tts_auto_generate=values.get("irodori_tts_auto_generate", "0") == "1",
            irodori_tts_url=(values.get("irodori_tts_url", "http://127.0.0.1:8088").strip().rstrip("/") or "http://127.0.0.1:8088"),
            irodori_tts_api_key=values.get("irodori_tts_api_key", ""),
            irodori_tts_voice=values.get("irodori_tts_voice", "none").strip() or "none",
            irodori_tts_caption=values.get("irodori_tts_caption", "").strip(),
            irodori_tts_speed=max(0.25, min(4.0, float(values.get("irodori_tts_speed", "1") or 1))),
        )

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "jiten_api_key", "jpdb_api_token", "study_backend", "show_furigana",
            "furigana_on_hover", "furigana_on_reading",
            "show_pitch_accent", "furigana_unknown_only", "word_mark_style",
            "furigana_states", "underline_states",
            "study_card_mode", "study_card_triggers",
            "custom_css", "parse_ahead", "auto_download_nyaa", "nyaa_category",
            "reader_font", "reader_theme", "reader_font_size", "reader_text_color", "reader_background_color",
            "reader_width", "reader_line_height", "reader_indent", "reader_vertical", "reader_mode",
            "blur_images",
            "auto_bookmarks",
            "word_color_theme", "word_color_new", "word_color_learning", "word_color_due",
            "word_color_known", "word_color_blacklisted",
            "pitch_accent_color",
            "audiobook_generation_provider", "audiobook_tts_model",
            "audiobook_character_voices",
            "irodori_tts_enabled", "irodori_tts_auto_generate", "irodori_tts_url", "irodori_tts_api_key",
            "irodori_tts_voice", "irodori_tts_caption", "irodori_tts_speed",
        }
        current = self.settings()
        requested_provider = values.get("audiobook_generation_provider")
        if requested_provider is None and "irodori_tts_enabled" in values:
            requested_provider = "irodori" if bool(values.get("irodori_tts_enabled")) else "off"
        if requested_provider is None:
            requested_provider = current.audiobook_generation_provider
        payload = {
            "jiten_api_key": str(values.get("jiten_api_key", current.jiten_api_key)).strip(),
            "jpdb_api_token": str(values.get("jpdb_api_token", current.jpdb_api_token)).strip(),
            "study_backend": str(values.get("study_backend", current.study_backend)).strip().lower(),
            "show_furigana": "1" if bool(values.get("show_furigana", current.show_furigana)) else "0",
            "furigana_on_hover": (
                "1" if bool(values.get("furigana_on_hover", current.furigana_on_hover)) else "0"
            ),
            "furigana_on_reading": (
                "1" if bool(values.get("furigana_on_reading", current.furigana_on_reading)) else "0"
            ),
            "show_pitch_accent": (
                "1" if bool(values.get("show_pitch_accent", current.show_pitch_accent)) else "0"
            ),
            "furigana_states": self._jiten_state_csv(
                values.get("furigana_states", current.furigana_states),
                self.DEFAULT_FURIGANA_STATES,
            ),
            "underline_states": self._jiten_state_csv(
                values.get("underline_states", current.underline_states),
                self.DEFAULT_UNDERLINE_STATES,
            ),
            "furigana_unknown_only": (
                "1" if bool(values.get("furigana_unknown_only", current.furigana_unknown_only)) else "0"
            ),
            "word_mark_style": str(values.get("word_mark_style", current.word_mark_style)).strip().lower(),
            "study_card_mode": str(values.get("study_card_mode", current.study_card_mode)).strip().lower(),
            "study_card_triggers": ",".join(
                self._study_trigger_codes(values.get("study_card_triggers", current.study_card_triggers))
            ),
            "custom_css": str(values.get("custom_css", current.custom_css)),
            "parse_ahead": "next",
            "auto_download_nyaa": "1" if bool(values.get("auto_download_nyaa", current.auto_download_nyaa)) else "0",
            "nyaa_category": str(values.get("nyaa_category", current.nyaa_category)).strip() or "3_3",
            "reader_font": str(values.get("reader_font", current.reader_font)).strip() or "mincho",
            "reader_theme": str(values.get("reader_theme", current.reader_theme)).strip() or "night",
            "reader_font_size": str(max(12, min(72, int(float(values.get("reader_font_size", current.reader_font_size) or 22))))),
            "reader_text_color": str(values.get("reader_text_color", current.reader_text_color)).strip() or "#dce7f6",
            "reader_background_color": str(values.get("reader_background_color", current.reader_background_color)).strip() or "#0b1420",
            "reader_width": str(max(360, min(2400, int(float(values.get("reader_width", current.reader_width) or 900))))),
            "reader_line_height": str(max(1.0, min(3.5, float(values.get("reader_line_height", current.reader_line_height) or 1.9)))),
            "reader_indent": str(max(0.0, min(5.0, float(values.get("reader_indent", current.reader_indent) or 1.0)))),
            "reader_vertical": "1" if bool(values.get("reader_vertical", current.reader_vertical)) else "0",
            "reader_mode": str(values.get("reader_mode", current.reader_mode)).strip().lower(),
            "blur_images": "1" if bool(values.get("blur_images", current.blur_images)) else "0",
            "auto_bookmarks": "1" if bool(values.get("auto_bookmarks", current.auto_bookmarks)) else "0",
            "word_color_theme": str(
                values.get("word_color_theme", current.word_color_theme)
            ).strip().lower(),
            "word_color_new": self._word_color(
                values.get("word_color_new", current.word_color_new), current.word_color_new
            ),
            "word_color_learning": self._word_color(
                values.get("word_color_learning", current.word_color_learning),
                current.word_color_learning,
            ),
            "word_color_due": self._word_color(
                values.get("word_color_due", current.word_color_due), current.word_color_due
            ),
            "word_color_known": self._word_color(
                values.get("word_color_known", current.word_color_known),
                current.word_color_known,
            ),
            "word_color_blacklisted": self._word_color(
                values.get("word_color_blacklisted", current.word_color_blacklisted),
                current.word_color_blacklisted,
            ),
            "pitch_accent_color": self._word_color(
                values.get("pitch_accent_color", current.pitch_accent_color),
                current.pitch_accent_color,
            ),
            "audiobook_generation_provider": str(requested_provider).strip().lower() or "off",
            "audiobook_tts_model": str(values.get("audiobook_tts_model", current.audiobook_tts_model)).strip() or "tts-1",
            "audiobook_character_voices": "1" if bool(values.get("audiobook_character_voices", current.audiobook_character_voices)) else "0",
            "irodori_tts_enabled": "1" if str(requested_provider).strip().lower() not in {"", "off"} else "0",
            "irodori_tts_auto_generate": "1" if bool(values.get("irodori_tts_auto_generate", current.irodori_tts_auto_generate)) else "0",
            "irodori_tts_url": str(values.get("irodori_tts_url", current.irodori_tts_url)).strip().rstrip("/") or "http://127.0.0.1:8088",
            "irodori_tts_api_key": str(values.get("irodori_tts_api_key", current.irodori_tts_api_key)).strip(),
            "irodori_tts_voice": str(values.get("irodori_tts_voice", current.irodori_tts_voice)).strip() or "none",
            "irodori_tts_caption": str(values.get("irodori_tts_caption", current.irodori_tts_caption)).strip(),
            "irodori_tts_speed": str(max(0.25, min(4.0, float(values.get("irodori_tts_speed", current.irodori_tts_speed) or 1)))),
        }
        if payload["audiobook_generation_provider"] not in {"off", "irodori", "external"}:
            payload["audiobook_generation_provider"] = "off"
        payload["irodori_tts_enabled"] = "1" if payload["audiobook_generation_provider"] != "off" else "0"
        if payload["study_backend"] not in {"jiten", "jpdb"}:
            payload["study_backend"] = "jiten"
        if payload["parse_ahead"] not in {"current", "next", "book"}:
            payload["parse_ahead"] = "next"
        if payload["reader_mode"] not in {"scroll", "pages"}:
            payload["reader_mode"] = "scroll"
        if payload["word_mark_style"] == "none":
            payload["word_mark_style"] = "color"
        if payload["word_mark_style"] not in {"underline", "color"}:
            payload["word_mark_style"] = "underline"
        if payload["study_card_mode"] not in {"button", "hover"}:
            payload["study_card_mode"] = "button"
        if payload["word_color_theme"] not in self.WORD_COLOR_THEMES:
            payload["word_color_theme"] = "balanced"
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
            "furigana_on_hover": s.furigana_on_hover,
            "furigana_on_reading": s.furigana_on_reading,
            "furigana_states": s.furigana_states.split(","),
            "underline_states": s.underline_states.split(","),
            "show_pitch_accent": s.show_pitch_accent,
            "furigana_unknown_only": s.furigana_unknown_only,
            "word_mark_style": s.word_mark_style,
            "study_card_mode": s.study_card_mode,
            "study_card_triggers": self._study_trigger_codes(s.study_card_triggers),
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
            "blur_images": s.blur_images,
            "auto_bookmarks": s.auto_bookmarks,
            "word_color_theme": s.word_color_theme,
            "word_color_new": s.word_color_new,
            "word_color_learning": s.word_color_learning,
            "word_color_due": s.word_color_due,
            "word_color_known": s.word_color_known,
            "word_color_blacklisted": s.word_color_blacklisted,
            "pitch_accent_color": s.pitch_accent_color,
            "translation_language": s.translation_language,
            "audiobook_generation_provider": s.audiobook_generation_provider,
            "audiobook_tts_model": s.audiobook_tts_model,
            "irodori_tts_enabled": s.irodori_tts_enabled,
            "irodori_tts_auto_generate": s.irodori_tts_auto_generate,
            "irodori_tts_url": s.irodori_tts_url,
            "irodori_tts_api_key": s.irodori_tts_api_key,
            "irodori_tts_voice": s.irodori_tts_voice,
            "irodori_tts_caption": s.irodori_tts_caption,
            "irodori_tts_speed": s.irodori_tts_speed,
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
                   anilist_total_volumes=?,anilist_user_score=?,
                   cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?""",
                (match["anilist_id"], match["anilist_status"], match["anilist_progress_volumes"],
                 match["anilist_total_volumes"], match["anilist_user_score"], match["cover_url"],
                 time.time(), int(book_id)),
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
                       anilist_total_volumes=?,anilist_user_score=?,
                       cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?""",
                    (media_id, book.get("anilist_status") or "", int(book.get("anilist_progress_volumes") or 0),
                     book.get("anilist_total_volumes"), book.get("anilist_user_score"),
                     book.get("cover_url") or "", time.time(), int(row["id"])),
                )
                changed += 1
        return changed

    def _deleted_source_paths(self) -> set[str]:
        with self._connect() as conn:
            return {str(row[0]) for row in conn.execute("SELECT file_path FROM ln_deleted_sources")}

    def is_deleted_source(self, source: Path) -> bool:
        try:
            resolved = str(Path(source).expanduser().resolve())
        except (OSError, RuntimeError):
            return False
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM ln_deleted_sources WHERE file_path=?",
                (resolved,),
            ).fetchone() is not None

    def _mark_deleted_source(self, source: Path) -> None:
        try:
            resolved = str(Path(source).expanduser().resolve())
        except (OSError, RuntimeError):
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ln_deleted_sources(file_path,deleted_at) VALUES(?,?) "
                "ON CONFLICT(file_path) DO UPDATE SET deleted_at=excluded.deleted_at",
                (resolved, time.time()),
            )

    def _clear_deleted_source(self, source: Path) -> None:
        try:
            resolved = str(Path(source).expanduser().resolve())
        except (OSError, RuntimeError):
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM ln_deleted_sources WHERE file_path=?", (resolved,))

    @staticmethod
    def _cover_suffix(value: str) -> str:
        mapping = {
            "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
            "image/webp": ".webp", "image/gif": ".gif",
        }
        return mapping.get(str(value or "").casefold(), ".jpg")

    def _store_cover_bytes(self, raw: bytes, suffix: str) -> str:
        suffix = suffix.casefold() if str(suffix).startswith(".") else f".{suffix.casefold()}"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            suffix = ".jpg"
        if suffix == ".jpeg":
            suffix = ".jpg"
        digest = hashlib.sha256(raw).hexdigest()[:24]
        target = self.cover_cache_dir / f"ln-{digest}{suffix}"
        if not target.is_file() or target.stat().st_size != len(raw):
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_bytes(raw)
            temp.replace(target)
        return f"covers/{target.name}"

    def _migrate_inline_covers(self) -> int:
        """Move legacy EPUB data URLs out of SQLite/WebKit into the cover cache."""
        migrated = 0
        while True:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id,cover_url FROM ln_books WHERE cover_url LIKE 'data:%;base64,%' LIMIT 1"
                ).fetchone()
            if row is None:
                break
            book_id = int(row["id"])
            value = str(row["cover_url"] or "")
            try:
                header, payload = value.split(",", 1)
                media_type = header[5:].split(";", 1)[0]
                raw = base64.b64decode(payload, validate=False)
                if not raw:
                    raise ValueError("empty cover")
                cover_url = self._store_cover_bytes(raw, self._cover_suffix(media_type))
            except Exception as exc:
                self._log("LN inline cover migration skipped book=%s error=%s", book_id, exc)
                # Corrupt historical inline data must not keep being shipped to
                # WebKit forever. The AniList/local refresh can repopulate it.
                cover_url = ""
            with self._connect() as conn:
                conn.execute("UPDATE ln_books SET cover_url=?,updated_at=? WHERE id=?", (cover_url, time.time(), book_id))
            migrated += 1
        if migrated:
            self._log("LN migrated inline covers count=%s", migrated)
        return migrated

    @staticmethod
    def _book_select_columns(conn: sqlite3.Connection) -> str:
        columns = [str(row["name"]) for row in conn.execute("PRAGMA table_info(ln_books)")]
        parts: list[str] = []
        for name in columns:
            quoted = '"' + name.replace('"', '""') + '"'
            if name == "cover_url":
                parts.append(
                    "CASE WHEN b.cover_url LIKE 'data:%' THEN '' ELSE b.cover_url END AS cover_url"
                )
            else:
                parts.append(f"b.{quoted}")
        return ",".join(parts)

    def source_language_profile(self, source: Path) -> dict[str, Any]:
        source = Path(source).expanduser().resolve()
        if source.suffix.casefold() == ".epub":
            _title, chapters, _cover = _epub_metadata(source)
        elif source.suffix.casefold() == ".txt":
            _title, chapters = _txt_metadata(source)
        else:
            return {"accepted": False, "reason": "unsupported"}
        profile = _japanese_text_profile(chapters)
        profile["path"] = str(source)
        return profile

    def is_probably_japanese_source(self, source: Path) -> bool:
        try:
            return bool(self.source_language_profile(source).get("accepted"))
        except (OSError, LightNovelError, UnicodeError, zipfile.BadZipFile):
            return False

    def import_file(self, source: Path, *, explicit: bool = True) -> dict[str, Any]:
        source = Path(source).expanduser().resolve()
        cover_blob: tuple[bytes, str] | None = None
        if source.suffix.casefold() == ".epub":
            title, chapters, cover_blob = _epub_metadata(source)
            file_type = "epub"
            # Inline illustrations are stored in the existing cover cache and
            # represented by tiny marker paragraphs, preserving their reading
            # order without sending binary/image markup to Jiten.
            image_urls: dict[str, str] = {}
            try:
                with zipfile.ZipFile(source) as zf:
                    for _chapter_title, chapter_text in chapters:
                        for marker in re.findall(r"\[\[PUDGE_EPUB_IMAGE:[A-Za-z0-9_-]+\]\]", chapter_text):
                            if marker in image_urls:
                                continue
                            archive_path = _ln_epub_image_path(marker)
                            if not archive_path:
                                continue
                            try:
                                raw_image = zf.read(archive_path)
                            except KeyError:
                                continue
                            suffix = PurePosixPath(archive_path).suffix.casefold() or ".jpg"
                            image_urls[marker] = self._store_cover_bytes(raw_image, suffix)
            except (OSError, zipfile.BadZipFile):
                image_urls = {}
            if image_urls:
                chapters = [
                    (chapter_title, "\n".join(
                        f"[[PUDGE_LN_IMAGE_URL:{image_urls.get(line, '')}]]" if line in image_urls else line
                        for line in chapter_text.splitlines()
                    ))
                    for chapter_title, chapter_text in chapters
                ]
        elif source.suffix.casefold() == ".txt":
            title, chapters = _txt_metadata(source)
            file_type = "txt"
        else:
            raise LightNovelError("Only EPUB and TXT are supported")
        title = html.unescape(str(title or "")).strip() or source.stem
        explicit_volume = _volume_from_text(source.stem) or _volume_from_text(source.parent.name) or _volume_from_text(title)
        volume = explicit_volume or 1
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
        target = target.expanduser().resolve()
        if not explicit and self.is_deleted_source(target):
            raise LightNovelError("Source was explicitly removed from Pudge")
        if source != target:
            shutil.copy2(source, target)
        if explicit:
            self._clear_deleted_source(target)
            with self._connect() as conn:
                conn.execute("DELETE FROM ln_scan_rejections WHERE file_path=?", (str(target),))
        cover_url = ""
        if cover_blob is not None:
            raw_cover, cover_suffix = cover_blob
            cover_url = self._store_cover_bytes(raw_cover, cover_suffix)
        now = time.time()
        if explicit_volume is None:
            series_key = _series_key(title or source.stem)
            if series_key:
                with self._connect() as conn:
                    siblings = conn.execute("SELECT id,title,file_path,volume FROM ln_books").fetchall()
                used = {int(row['volume'] or 0) for row in siblings if str(row['file_path'] or '') != str(target) and _series_key(str(row['title'] or Path(str(row['file_path'] or '')).stem)) == series_key and int(row['volume'] or 0) > 0}
                volume = next((candidate for candidate in range(1, 301) if candidate not in used), 1)
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
        book = self.book(book_id)
        self.queue_auto_bind_anilist(book_id)
        return book

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
                self.import_file(path, explicit=False)
                changed += 1
                self._log("LN reindexed source path=%s schema=%s", path, self.CONTENT_SCHEMA)
            except Exception as exc:
                self._log("LN source reindex skipped path=%s error=%s", path, exc)
        return changed

    def scan_downloaded(self) -> int:
        with self._connect() as conn:
            known = {str(row[0]) for row in conn.execute("SELECT file_path FROM ln_books")}
            deleted = {str(row[0]) for row in conn.execute("SELECT file_path FROM ln_deleted_sources")}
            rejected = {
                str(row["file_path"]): (int(row["mtime_ns"]), int(row["size"]))
                for row in conn.execute("SELECT file_path,mtime_ns,size FROM ln_scan_rejections")
            }
        added = 0
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in {".epub", ".txt"}:
                continue
            resolved = str(path.resolve())
            if resolved in known or resolved in deleted:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (int(stat.st_mtime_ns), int(stat.st_size))
            if rejected.get(resolved) == signature:
                continue
            try:
                if not self.is_probably_japanese_source(path):
                    with self._connect() as conn:
                        conn.execute(
                            "INSERT INTO ln_scan_rejections(file_path,mtime_ns,size,checked_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(file_path) DO UPDATE SET mtime_ns=excluded.mtime_ns,size=excluded.size,checked_at=excluded.checked_at",
                            (resolved, signature[0], signature[1], time.time()),
                        )
                    rejected[resolved] = signature
                    self._log("LN auto-import rejected non-Japanese source path=%s", path)
                    continue
                self.import_file(path, explicit=False)
                with self._connect() as conn:
                    conn.execute("DELETE FROM ln_scan_rejections WHERE file_path=?", (resolved,))
                known.add(resolved)
                added += 1
            except Exception as exc:
                self._log("LN import skipped path=%s error=%s", path, exc)
        return added

    def _repair_missing_volumes(self) -> int:
        repaired = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT id,title,file_path,volume,created_at FROM ln_books ORDER BY created_at,id").fetchall()
            groups: dict[str, list[Any]] = {}
            for row in rows:
                raw_title = str(row["title"] or "")
                clean_title = html.unescape(raw_title).strip()
                path = Path(str(row["file_path"] or ""))
                key = _series_key(clean_title or path.stem) or f"book:{int(row['id'])}"
                groups.setdefault(key, []).append((row, clean_title, path))
            for group in groups.values():
                explicit: dict[int, int] = {}
                for row, clean_title, path in group:
                    detected = _volume_from_text(path.stem) or _volume_from_text(path.parent.name) or _volume_from_text(clean_title)
                    if detected:
                        explicit[int(row['id'])] = detected
                used = set(explicit.values())
                # Existing all-volume-1 groups are historical bad metadata. Preserve a
                # single true volume 1 and assign later imports in creation order.
                for row, clean_title, path in group:
                    target = explicit.get(int(row['id']))
                    if target is None:
                        current = int(row['volume'] or 0)
                        if len(group) == 1 and current > 0:
                            target = current
                        elif current > 1 and current not in used:
                            target = current; used.add(current)
                        else:
                            target = next((n for n in range(1, 301) if n not in used), 1); used.add(target)
                    updates: list[str] = []; params: list[Any] = []
                    if clean_title and clean_title != str(row['title'] or ''):
                        updates.append('title=?'); params.append(clean_title)
                    if int(row['volume'] or 0) != int(target):
                        updates.append('volume=?'); params.append(int(target))
                    if updates:
                        updates.append('updated_at=?'); params.append(time.time()); params.append(int(row['id']))
                        conn.execute(f"UPDATE ln_books SET {','.join(updates)} WHERE id=?", params); repaired += 1
        if repaired:
            self._log("LN repaired local metadata rows=%s", repaired)
        return repaired

    def books(self) -> list[dict[str, Any]]:
        self._repair_missing_volumes()
        with self._connect() as conn:
            book_columns = self._book_select_columns(conn)
            rows = conn.execute(
                f"""SELECT {book_columns},bm.source AS bookmark_source,bm.updated_at AS bookmark_updated_at,
                          COUNT(c.id) AS chapter_count,
                          COALESCE(SUM(LENGTH(c.text)),0) AS character_count,
                          COALESCE(SUM(
                              CASE
                                  WHEN c.chapter_index < b.current_chapter
                                      THEN LENGTH(c.text)
                                  WHEN c.chapter_index = b.current_chapter
                                      THEN LENGTH(c.text) * b.current_offset
                                  ELSE 0
                              END
                          ),0) AS read_character_count FROM ln_books b
                   LEFT JOIN ln_bookmarks bm ON bm.book_id=b.id
                   LEFT JOIN ln_chapters c ON c.book_id=b.id GROUP BY b.id
                   ORDER BY b.updated_at DESC,b.title COLLATE NOCASE"""
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            title = str(item.get("title") or "")
            media_id = item.get("anilist_id")
            item["series_key"] = (
                f"anilist:{int(media_id)}"
                if media_id
                else (_series_key(title) or f"book:{int(item['id'])}")
            )
            item["series_title"] = _series_title(title) or title
            total_characters = max(0, int(item.get("character_count") or 0))
            read_characters = max(0.0, float(item.get("read_character_count") or 0.0))
            if bool(item.get("finished")) and total_characters:
                read_characters = float(total_characters)
            if total_characters:
                read_characters = min(float(total_characters), read_characters)
                reading_progress = read_characters / float(total_characters)
            else:
                reading_progress = 1.0 if bool(item.get("finished")) else 0.0
            item["read_character_count"] = round(read_characters, 3)
            item["reading_progress"] = max(0.0, min(1.0, reading_progress))
            item["reading_progress_percent"] = round(
                item["reading_progress"] * 100.0,
                2,
            )
            result.append(item)
        return result

    def drop_card(self, book_id: int) -> dict[str, Any]:
        """Small one-book payload for the Finder drop bridge.

        EPUB covers are stored as data URLs for offline rendering and can be
        several megabytes. Sending one through run_js, then rebuilding every
        sibling volume, temporarily duplicates that data in Python, JavaScript,
        DOM attributes, and decoded WebKit images. The drop acknowledgement only
        needs enough data to paint/focus the new card; the normal library state
        refresh can fill the cover and AniList decoration later.
        """
        with self._connect() as conn:
            book_columns = self._book_select_columns(conn)
            row = conn.execute(
                f"""SELECT {book_columns},bm.source AS bookmark_source,bm.updated_at AS bookmark_updated_at,
                          COUNT(c.id) AS chapter_count,
                          COALESCE(SUM(LENGTH(c.text)),0) AS character_count,
                          COALESCE(SUM(
                              CASE
                                  WHEN c.chapter_index < b.current_chapter THEN LENGTH(c.text)
                                  WHEN c.chapter_index = b.current_chapter THEN LENGTH(c.text) * b.current_offset
                                  ELSE 0
                              END
                          ),0) AS read_character_count
                   FROM ln_books b
                   LEFT JOIN ln_bookmarks bm ON bm.book_id=b.id
                   LEFT JOIN ln_chapters c ON c.book_id=b.id
                   WHERE b.id=? GROUP BY b.id""",
                (int(book_id),),
            ).fetchone()
        if row is None:
            raise LightNovelError("Light novel not found")
        item = dict(row)
        title = str(item.get("title") or "")
        media_id = item.get("anilist_id")
        item["series_key"] = (
            f"anilist:{int(media_id)}"
            if media_id
            else (_series_key(title) or f"book:{int(item['id'])}")
        )
        item["series_title"] = _series_title(title) or title
        total = max(0, int(item.get("character_count") or 0))
        read = max(0.0, float(item.get("read_character_count") or 0.0))
        if bool(item.get("finished")) and total:
            read = float(total)
        if total:
            read = min(float(total), read)
            progress = read / float(total)
        else:
            progress = 1.0 if bool(item.get("finished")) else 0.0
        item["read_character_count"] = round(read, 3)
        item["reading_progress"] = max(0.0, min(1.0, progress))
        item["reading_progress_percent"] = round(item["reading_progress"] * 100.0, 2)
        if str(item.get("cover_url") or "").startswith("data:"):
            item["cover_url"] = ""
        return item

    def book(self, book_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            book_columns = self._book_select_columns(conn)
            row = conn.execute(
                f"""SELECT {book_columns},bm.source AS bookmark_source,
                          bm.updated_at AS bookmark_updated_at
                   FROM ln_books b LEFT JOIN ln_bookmarks bm ON bm.book_id=b.id
                   WHERE b.id=?""",
                (int(book_id),),
            ).fetchone()
            if row is None:
                raise LightNovelError("Light novel not found")
            chapters = [dict(x) for x in conn.execute("SELECT id,chapter_index,title,text_hash FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (int(book_id),))]
        result = dict(row)
        result["chapters"] = chapters
        title = str(result.get("title") or "")
        result["series_key"] = (
            f"anilist:{int(result['anilist_id'])}"
            if result.get("anilist_id")
            else (_series_key(title) or f"book:{int(result['id'])}")
        )
        result["series_title"] = _series_title(title) or title
        return result

    def delete_book(self, book_id: int, *, delete_file: bool = False) -> dict[str, Any]:
        book = self.book(int(book_id))
        path = Path(str(book.get("file_path") or "")).expanduser()
        self._mark_deleted_source(path)
        with self._connect() as conn:
            conn.execute("DELETE FROM ln_chapters WHERE book_id=?", (int(book_id),))
            conn.execute("DELETE FROM ln_books WHERE id=?", (int(book_id),))
        if delete_file:
            try:
                root = self.root.expanduser().resolve()
                resolved = path.resolve()
                if resolved.is_file() and resolved.is_relative_to(root):
                    resolved.unlink(missing_ok=True)
            except (OSError, RuntimeError, ValueError, AttributeError):
                pass
        return {"ok": True, "book_id": int(book_id), "file_kept": not bool(delete_file)}

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

    def _jiten_get(self, action: str, params: dict[str, Any] | None = None) -> Any:
        token = self.settings().jiten_api_key
        headers = {
            "Accept": "application/json",
            "User-Agent": APP_SLUG,
        }
        if token:
            headers["Authorization"] = f"ApiKey {token}"
        url = f"{self.JITEN_BASE}/{action.lstrip('/')}"
        last: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.get(
                    url,
                    headers=headers,
                    params=params or {},
                    timeout=20,
                    follow_redirects=True,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(0.5 * (2**attempt))
                        continue
                response.raise_for_status()
                return response.json() if response.content else {}
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) and attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
                break
        raise LightNovelError(f"Jiten request failed: {last}")

    @staticmethod
    def _jiten_title_key(value: str) -> str:
        value = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
        return re.sub(r"[^0-9a-zぁ-ゟ゠-ヿ一-鿿]+", "", value)

    @staticmethod
    def _jiten_media_type(media_kind: str, media_format: str) -> int:
        kind = str(media_kind or "").casefold()
        media_format = str(media_format or "").upper()
        if kind == "novel":
            return 4
        if kind == "manga":
            return 9
        if media_format == "MOVIE":
            return 3
        return 1

    @staticmethod
    def _jiten_anilist_match(candidate: dict[str, Any], media_id: int) -> bool:
        for link in candidate.get("links") or []:
            if not isinstance(link, dict):
                continue
            try:
                link_type = int(link.get("linkType") or 0)
            except (TypeError, ValueError):
                continue
            if link_type != 4:
                continue
            url = str(link.get("url") or "")
            match = re.search(r"anilist\.co/(?:anime|manga)/(\d+)", url, re.IGNORECASE)
            if match and int(match.group(1)) == int(media_id):
                return True
        return False

    @staticmethod
    def _jiten_deck_stats(deck: dict[str, Any], *, url: str) -> dict[str, Any]:
        coverage = float(deck.get("coverage") or 0.0)
        learning = float(deck.get("youngCoverage") or 0.0)
        return {
            "available": True,
            "deck_id": int(deck.get("deckId") or 0),
            "url": url,
            "character_count": int(deck.get("characterCount") or 0),
            "word_count": int(deck.get("wordCount") or 0),
            "unique_word_count": int(deck.get("uniqueWordCount") or 0),
            "difficulty": int(
                deck.get("difficulty") if deck.get("difficulty") is not None else -1
            ),
            "difficulty_raw": float(deck.get("difficultyRaw") or 0.0),
            "speech_duration_ms": int(deck.get("speechDuration") or 0),
            "coverage_available": deck.get("coverage") is not None,
            "known_coverage": max(0.0, min(100.0, coverage)),
            "expected_comprehension": max(0.0, min(100.0, coverage)),
            "learning_coverage": max(0.0, min(100.0, learning)),
        }

    @staticmethod
    def _jiten_subdeck_volume(deck: dict[str, Any]) -> int | None:
        values = [
            deck.get("originalTitle"),
            deck.get("romajiTitle"),
            deck.get("englishTitle"),
            deck.get("originalFileName"),
            *(deck.get("aliases") or []),
        ]
        patterns = (
            r"(?i)(?:\bvol(?:ume)?\.?\s*|(?:^|[\s._-])v\s*)0*(\d{1,3})(?:\.\d+)?(?:\b|$)",
            r"第\s*0*(\d{1,3})(?:\.\d+)?\s*巻",
            r"(?:^|[\s._-])0*(\d{1,3})(?:\.\d+)?\s*巻(?:$|[\s._-])",
        )
        for raw in values:
            text = unicodedata.normalize("NFKC", str(raw or "")).strip()
            if not text:
                continue
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    number = int(match.group(1))
                    if 0 < number <= 500:
                        return number
        return None

    def _jiten_detail_with_subdecks(
        self, deck_id: int
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        payload = self._jiten_get(
            f"media-deck/{deck_id}/detail",
            {"offset": 0},
        )
        envelope = payload if isinstance(payload, dict) else {}
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope

        # Older/mocked API responses are flat DeckDto payloads.
        if not isinstance(data, dict):
            return None, []
        if not any(key in data for key in ("mainDeck", "subDecks", "parentDeck")):
            return data, []

        main = data.get("mainDeck") if isinstance(data.get("mainDeck"), dict) else None
        subdecks = [
            item for item in (data.get("subDecks") or []) if isinstance(item, dict)
        ]
        try:
            total = max(len(subdecks), int(envelope.get("totalItems") or len(subdecks)))
            page_size = max(1, int(envelope.get("pageSize") or 25))
        except (TypeError, ValueError):
            total, page_size = len(subdecks), 25

        # Jiten's detail endpoint pages subdecks 25 at a time. Fetch the rest once
        # and cache the combined result at the parent-series level.
        offset = page_size
        while offset < min(total, 500):
            page = self._jiten_get(
                f"media-deck/{deck_id}/detail",
                {"offset": offset},
            )
            page_envelope = page if isinstance(page, dict) else {}
            page_data = (
                page_envelope.get("data")
                if isinstance(page_envelope.get("data"), dict)
                else page_envelope
            )
            values = page_data.get("subDecks") if isinstance(page_data, dict) else []
            rows = [item for item in (values or []) if isinstance(item, dict)]
            if not rows:
                break
            subdecks.extend(rows)
            offset += page_size
        return main, subdecks

    def jiten_media_stats(
        self,
        media_id: int,
        media_kind: str,
        media_format: str,
        titles: list[str],
    ) -> dict[str, Any]:
        clean_titles = list(
            dict.fromkeys(str(value or "").strip() for value in titles if str(value or "").strip())
        )[:6]
        authenticated = bool(self.settings().jiten_api_key)
        cache_key = {
            "anilist_id": int(media_id),
            "kind": str(media_kind or "anime").casefold(),
            "format": str(media_format or "").upper(),
            "authenticated": authenticated,
        }
        cached = self._jiten_media_cache.get(cache_key, ttl_seconds=7 * 24 * 3600)
        if isinstance(cached, dict):
            return dict(cached)
        if not clean_titles:
            return {"available": False}

        media_type = self._jiten_media_type(media_kind, media_format)
        candidates: dict[int, dict[str, Any]] = {}
        for title in clean_titles[:3]:
            payload = self._jiten_get(
                "media-deck/get-media-decks",
                {"titleFilter": title, "mediaType": media_type, "offset": 0},
            )
            values = payload.get("data") if isinstance(payload, dict) else []
            for candidate in values or []:
                if not isinstance(candidate, dict):
                    continue
                try:
                    candidates[int(candidate.get("deckId"))] = candidate
                except (TypeError, ValueError):
                    continue
            if any(self._jiten_anilist_match(item, int(media_id)) for item in candidates.values()):
                break

        selected = next(
            (item for item in candidates.values() if self._jiten_anilist_match(item, int(media_id))),
            None,
        )
        if selected is None and candidates:
            source_keys = [self._jiten_title_key(value) for value in clean_titles]

            def score(item: dict[str, Any]) -> float:
                names = [
                    item.get("originalTitle"),
                    item.get("romajiTitle"),
                    item.get("englishTitle"),
                    *(item.get("aliases") or []),
                ]
                target_keys = [self._jiten_title_key(str(value or "")) for value in names]
                return max(
                    (
                        float(fuzz.ratio(source, target))
                        for source in source_keys
                        for target in target_keys
                        if source and target
                    ),
                    default=0.0,
                )

            ranked = sorted(((score(item), item) for item in candidates.values()), reverse=True, key=lambda x: x[0])
            best_score, best = ranked[0]
            margin = best_score - (ranked[1][0] if len(ranked) > 1 else 0.0)
            if best_score >= 94.0 and (len(ranked) == 1 or margin >= 3.0):
                selected = best

        if selected is None:
            result = {"available": False}
        else:
            deck_id = int(selected.get("deckId"))
            main_deck: dict[str, Any] | None = None
            subdecks: list[dict[str, Any]] = []
            try:
                main_deck, subdecks = self._jiten_detail_with_subdecks(deck_id)
            except Exception as exc:
                self._log("Jiten deck detail unavailable for %s: %s", deck_id, exc)

            aggregate = {**selected, **(main_deck or {})}
            result = self._jiten_deck_stats(
                aggregate,
                url=f"https://jiten.moe/decks/media/{deck_id}/detail",
            )
            # Public responses do not carry personal coverage. Do not present a
            # numeric zero as a real known-word percentage when no key is set.
            result["coverage_available"] = bool(
                authenticated and aggregate.get("coverage") is not None
            )

            volumes: dict[str, dict[str, Any]] = {}
            used: set[int] = set()
            for index, child in enumerate(subdecks, start=1):
                volume = self._jiten_subdeck_volume(child)
                if volume is None or volume in used:
                    # Jiten's default subdeck order is the media order shown on
                    # its detail page, so it is the safest fallback when titles
                    # omit an explicit "Vol. N" marker.
                    volume = index
                    while volume in used:
                        volume += 1
                used.add(volume)
                child_id = int(child.get("deckId") or 0)
                child_stats = self._jiten_deck_stats(
                    child,
                    url=f"https://jiten.moe/decks/media/{child_id}/detail"
                    if child_id
                    else result["url"],
                )
                child_stats["coverage_available"] = bool(
                    authenticated and child.get("coverage") is not None
                )
                volumes[str(volume)] = child_stats
            if volumes:
                result["volumes"] = volumes
        self._jiten_media_cache.put(cache_key, result)
        self._jiten_media_cache.prune(older_than_seconds=60 * 24 * 3600, max_entries=1000)
        return result

    def invalidate_jiten_media_stats(self) -> int:
        """Drop replaceable Jiten coverage/difficulty metadata."""
        return self._jiten_media_cache.clear()

    def jiten_unparsed_chapters(self) -> list[tuple[str, str]]:
        """Return local LN chapter texts that do not have a Jiten parse yet."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT text,text_hash FROM ln_chapters ORDER BY book_id,chapter_index"
            ).fetchall()
        pending: list[tuple[str, str]] = []
        for row in rows:
            digest = str(row["text_hash"] or "")
            text = str(row["text"] or "")
            if text.strip() and digest and self._cached_parse(digest) is None:
                pending.append((text, digest))
        return pending

    def jiten_preparse(self, text: str, digest: str | None = None) -> dict[str, Any]:
        selected = str(text or "").strip()
        if not selected:
            return {"tokens": [], "vocabulary": [], "paragraphs": []}
        if digest is None:
            digest = hashlib.sha256(("study-v1\0" + selected[:20000]).encode("utf-8")).hexdigest()
            selected = selected[:20000]
        return self._parse_text(selected, str(digest))

    def tts_source(self, book_id: int) -> dict[str, Any]:
        """Return stable local text/chapter data for optional TTS backends."""
        with self._connect() as conn:
            book = conn.execute("SELECT * FROM ln_books WHERE id=?", (int(book_id),)).fetchone()
            if book is None:
                raise LightNovelError("Light novel was not found")
            chapters = conn.execute(
                "SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
                (int(book_id),),
            ).fetchall()
        return {
            "id": int(book["id"]),
            "title": str(book["title"] or f"Light Novel {book_id}"),
            "volume": int(book["volume"] or 0),
            "anilist_id": int(book["anilist_id"]) if book["anilist_id"] is not None else None,
            "chapters": [
                {
                    "index": int(row["chapter_index"]),
                    "title": str(row["title"] or f"Chapter {int(row['chapter_index']) + 1}"),
                    "text": str(row["text"] or ""),
                }
                for row in chapters
                if str(row["text"] or "").strip()
            ],
        }

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
        text_paragraphs = [p for p in paragraphs if not _is_ln_image_paragraph(p)]
        # Keep payloads moderately sized while still making very few requests.
        batches: list[list[str]] = []
        current: list[str] = []
        size = 0
        for paragraph in text_paragraphs:
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
        token_rows = iter(all_tokens)
        display_tokens = [([] if _is_ln_image_paragraph(paragraph) else next(token_rows, [])) for paragraph in paragraphs]
        parsed = {"tokens": display_tokens, "vocabulary": list(vocabulary.values()), "paragraphs": paragraphs}
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

    def _chapter_row(self, book_id: int, chapter_index: int, *, touch: bool = False) -> sqlite3.Row:
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

    def parse_study_text(self, text: str) -> dict[str, Any]:
        selected = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:20000]
        if not selected:
            raise LightNovelError("No Japanese text to parse")
        digest = hashlib.sha256(("study-v1\0" + selected).encode("utf-8")).hexdigest()
        parsed = self._parse_text(selected, digest)
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
            states = state_map.get(pair) or [
                str(value) for value in (item.get("knownState") or item.get("cardState") or [])
            ]
            clone = dict(item)
            clone["states"] = states
            clone["normalizedState"] = self._normalized_state(states)
            result_vocab.append(clone)
        return {
            "text": selected,
            "paragraphs": parsed.get("paragraphs") or [],
            "tokens": parsed.get("tokens") or [],
            "vocabulary": result_vocab,
            "settings": self.settings_payload(),
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

    @staticmethod
    def _validate_reader_css(value: str) -> str:
        css = str(value or "").strip()
        if not css:
            raise LightNovelError("Local LLM returned empty CSS")
        if len(css) > 12000:
            raise LightNovelError("Generated CSS is too large")
        forbidden = (
            r"@(?:import|font-face|namespace|supports|media|keyframes)\b",
            r"url\s*\(",
            r"expression\s*\(",
            r"javascript\s*:",
            r"-moz-binding\s*:",
            r"\bbehavior\s*:",
            r"</?style\b",
            r"\bcontent\s*:",
            r"\bposition\s*:\s*(?:fixed|sticky)\b",
            r"\bz-index\s*:",
            r"\bpointer-events\s*:\s*none\b",
            r"\buser-select\s*:\s*none\b",
            r"\bdisplay\s*:\s*none\b",
            r"\bvisibility\s*:\s*hidden\b",
        )
        for pattern in forbidden:
            if re.search(pattern, css, re.IGNORECASE):
                raise LightNovelError("Generated CSS contains a forbidden construct")
        clean = re.sub(r"/\*[\s\S]*?\*/", "", css)
        if clean.count("{") != clean.count("}") or not re.search(r"\{[^{}]*\}", clean):
            raise LightNovelError("Generated CSS is malformed")
        residual = re.sub(r"[^{}]+\{[^{}]*\}", "", clean).strip()
        if residual:
            raise LightNovelError("Generated CSS must contain only flat CSS rules")
        selector_root = re.compile(
            r"^(?:#lnReader(?:\b|[.#:[>+~ ])|#lnReaderScroll(?:\b|[.#:[>+~ ])|"
            r"\.ln-reader(?:\b|[.#:[>+~ ])|\.ln-reader-scroll(?:\b|[.#:[>+~ ]))"
        )
        blocked_selector = re.compile(
            r"(?:\bbutton\b|\binput\b|\bselect\b|\btextarea\b|"
            r"\.ln-reader-toolbar\b|\.ln-reader-appearance\b|#lnReaderAppearance\b)",
            re.IGNORECASE,
        )
        blocks = list(re.finditer(r"([^{}]+)\{([^{}]*)\}", clean))
        if len(blocks) > 80:
            raise LightNovelError("Generated CSS has too many rules")
        for block in blocks:
            selectors = [part.strip() for part in block.group(1).split(",") if part.strip()]
            declarations = block.group(2)
            if not selectors:
                raise LightNovelError("Generated CSS has an empty selector")
            for selector in selectors:
                if selector == ":root":
                    for declaration in declarations.split(";"):
                        declaration = declaration.strip()
                        if not declaration:
                            continue
                        if not re.match(r"--(?:ln|pudge)-[a-z0-9_-]+\s*:", declaration, re.I):
                            raise LightNovelError(":root may only define --ln-* or --pudge-* variables")
                    continue
                if blocked_selector.search(selector) or not selector_root.match(selector):
                    raise LightNovelError("Generated CSS may style reader content only")
        return css

    def generate_reader_css(self, request: str, current_css: str = "") -> dict[str, str]:
        instruction = re.sub(r"\s+", " ", str(request or "")).strip()[:3000]
        if not instruction:
            raise LightNovelError("Describe how the reader should look")
        cfg = self.config.llm
        if not cfg.enabled or not str(cfg.model or "").strip():
            raise LightNovelError("Enable the local LLM in Settings first")
        system = (
            "You generate CSS only for Pudge's Light Novel reader. "
            "Return strict JSON with exactly one string field named css. "
            "Rules: selectors may target only #lnReader, .ln-reader, #lnReaderScroll, "
            ".ln-reader-scroll and their descendants; :root is allowed only for --ln-* "
            "and --pudge-* custom properties. Never target toolbar, buttons, inputs, "
            "selects, textareas, app pages, html, or body. No @-rules, imports, external "
            "fonts/assets, url(), JavaScript, content, fixed/sticky positioning, z-index, "
            "pointer-events:none, user-select:none, display:none, or visibility:hidden. "
            "Never break word clicking, text selection, ruby/furigana, vertical text, "
            "page/scroll modes, or hide reading content. Prefer existing CSS variables. "
            "Implement only the requested visual change; if unsafe, choose the closest safe CSS."
        )
        user = (
            "CURRENT SAVED CSS (context only):\n"
            f"{str(current_css or '')[:6000] or '(none)'}\n\nUSER REQUEST:\n{instruction}"
        )
        client = OllamaClient(cfg)
        try:
            decoded = client.json_chat(system, user)
        finally:
            client.close()
        if not isinstance(decoded, dict):
            raise LightNovelError("LLM CSS generation failed")
        css = str(decoded.get("css") or "").strip()
        css = re.sub(r"^```(?:css|json)?\s*", "", css, flags=re.IGNORECASE)
        css = re.sub(r"\s*```$", "", css).strip()
        return {"css": self._validate_reader_css(css)}

    def _translate_local_llm(
        self,
        text: str,
        context: str,
        target_language: str,
        glossary: list[dict[str, str]],
    ) -> str:
        cfg = self.config.llm
        if not cfg.enabled or not str(cfg.model or "").strip():
            raise LightNovelError("Online translation is unavailable and local LLM is disabled")
        language_name = "Russian" if target_language == "ru" else "English"
        system = (
            "You translate Japanese text from reading material such as light novels and manga. Translate only the selected passage into "
            f"{language_name}. Use the preceding context only to resolve pronouns, omitted subjects, names, "
            "and ambiguity; never translate or repeat the context itself. Preserve tone and paragraph meaning. "
            "Use the supplied character glossary exactly and do not translate character names. "
            "Return strict JSON with one string field named translation."
        )
        glossary_text = "\n".join(
            f"- {item['source']} = {item['preferred']}" for item in glossary[:60]
        )
        user = (
            f"CHARACTER GLOSSARY:\n{glossary_text or '(none)'}\n\n"
            f"PRECEDING CONTEXT (up to 4000 chars):\n{context[-4000:]}\n\nSELECTED TEXT:\n{text}"
        )
        provider = str(getattr(cfg, "provider", "ollama") or "ollama").strip().lower()
        if provider == "ollama":
            # Preserve the original Ollama transport contract used by the reader.
            # OpenAI-compatible providers use the shared client below.
            headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
            response = httpx.post(
                f"{cfg.base_url.rstrip('/')}/api/chat",
                headers=headers,
                json=build_chat_payload(cfg, system, user),
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
        else:
            client = OllamaClient(cfg)
            try:
                decoded = client.json_chat(system, user)
            finally:
                client.close()
            translated = str(decoded.get("translation") or "").strip() if isinstance(decoded, dict) else ""
        if not translated:
            raise LightNovelError("LLM returned no translation")
        return translated

    def character_glossary_overrides(self, media_id: int | None) -> list[dict[str, str]]:
        if not media_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source,preferred FROM character_name_overrides WHERE media_id=? "
                "ORDER BY length(source) DESC,source", (int(media_id),)
            ).fetchall()
        return [{"source": str(row["source"]), "preferred": str(row["preferred"]), "user_override": True} for row in rows]

    def save_character_glossary_override(self, media_id: int, source: str, preferred: str) -> list[dict[str, str]]:
        source_text, preferred_text = str(source or "").strip(), str(preferred or "").strip()
        if not source_text or not preferred_text:
            raise LightNovelError("Both the source name and preferred name are required")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO character_name_overrides(media_id,source,preferred,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(media_id,source) DO UPDATE SET preferred=excluded.preferred,updated_at=excluded.updated_at",
                (int(media_id), source_text, preferred_text, time.time()),
            )
        return self.character_glossary(int(media_id))

    def delete_character_glossary_override(self, media_id: int, source: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            conn.execute("DELETE FROM character_name_overrides WHERE media_id=? AND source=?", (int(media_id), str(source or "").strip()))
        return self.character_glossary(int(media_id))

    def _merge_character_glossary(self, media_id: int, generated: list[dict[str, str]]) -> list[dict[str, str]]:
        merged = {str(item.get("source") or ""): dict(item) for item in generated}
        for item in self.character_glossary_overrides(int(media_id)):
            source = str(item["source"])
            previous = merged.get(source, {})
            replacement = dict(item)
            if previous.get("character_id") and not replacement.get("character_id"):
                replacement["character_id"] = previous["character_id"]
            if previous.get("reading") and not replacement.get("reading"):
                replacement["reading"] = previous["reading"]
            merged[source] = replacement
        rows = [item for source, item in merged.items() if source and item.get("preferred")]
        rows.sort(key=lambda item: len(str(item["source"])), reverse=True)
        return rows

    def character_glossary(self, media_id: int | None) -> list[dict[str, str]]:
        if not media_id:
            return []
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            return self.character_glossary_overrides(int(media_id))
        # v2 adds stable AniList Character IDs; do not reuse pre-v96 name-only cache rows.
        key = {"media_id": int(media_id), "schema": 3}
        cached = self._character_cache.get(key, ttl_seconds=30 * 24 * 3600)
        if isinstance(cached, list):
            return self._merge_character_glossary(int(media_id), [dict(item) for item in cached if isinstance(item, dict)])
        query = """
        query($id:Int!){Media(id:$id){characters(page:1,perPage:50,sort:[ROLE,RELEVANCE,ID]){edges{role node{id name{first middle last full native alternative}}}}}}
        """
        try:
            data = self._anilist_post(query, {"id": int(media_id)})
        except Exception as exc:
            self._log("AniList character glossary unavailable for %s: %s", media_id, exc)
            return self.character_glossary_overrides(int(media_id))
        candidates: dict[str, set[tuple[str, int | None, str]]] = {}

        def add(source: Any, preferred: Any, character_id: int | None = None, reading: str = "") -> None:
            source_text = str(source or "").strip()
            preferred_text = str(preferred or "").strip()
            if (
                len(source_text) < 2
                or not preferred_text
                or not re.search(r"[ぁ-ゟ゠-ヿ一-鿿]", source_text)
            ):
                return
            candidates.setdefault(source_text, set()).add((preferred_text, character_id, str(reading or "").strip()))

        for edge in ((data.get("Media") or {}).get("characters") or {}).get("edges") or []:
            node = (edge or {}).get("node") or {}
            names = node.get("name") or {}
            try:
                character_id = int(node.get("id")) if node.get("id") is not None else None
            except (TypeError, ValueError):
                character_id = None
            preferred = str(names.get("full") or "").strip()
            native = str(names.get("native") or "").strip()
            native_parts = [part for part in re.split(r"[\s・･=＝]+", native) if part]
            latin_parts = [
                str(names.get(key) or "").strip()
                for key in ("first", "middle", "last")
                if str(names.get(key) or "").strip()
            ]
            mapped_parts = list(latin_parts)
            if len(native_parts) == len(mapped_parts) and len(native_parts) > 1:
                if len(native_parts) == 2 and re.search(r"[一-鿿]", native):
                    mapped_parts.reverse()
                part_readings = [_romaji_to_hiragana(target) for target in mapped_parts]
                whole_reading = "".join(part_readings) if all(part_readings) else ""
                add(native, preferred, character_id, whole_reading)
                for source, target, reading in zip(native_parts, mapped_parts, part_readings):
                    add(source, target, character_id, reading)
            else:
                add(native, preferred, character_id, _romaji_to_hiragana(preferred))
            for alias in names.get("alternative") or []:
                add(alias, preferred, character_id)

        rows = []
        for source, identities in candidates.items():
            if len(identities) != 1:
                continue
            preferred, character_id, reading = next(iter(identities))
            row: dict[str, Any] = {"source": source, "preferred": preferred}
            if character_id is not None:
                row["character_id"] = character_id
            if reading:
                row["reading"] = reading
            rows.append(row)
        rows.sort(key=lambda item: len(item["source"]), reverse=True)
        self._character_cache.put(key, rows)
        self._character_cache.prune(
            older_than_seconds=180 * 24 * 3600,
            max_entries=500,
        )
        return self._merge_character_glossary(int(media_id), rows)

    @staticmethod
    def _protect_character_names(
        text: str, glossary: list[dict[str, str]]
    ) -> tuple[str, dict[str, str]]:
        protected = text
        replacements: dict[str, str] = {}
        for index, item in enumerate(glossary):
            source = str(item.get("source") or "")
            preferred = str(item.get("preferred") or "")
            if not source or source not in protected:
                continue
            marker = f"PUDGEZXQ{index}ZXQ"
            protected = protected.replace(source, marker)
            replacements[marker] = preferred
        return protected, replacements

    @staticmethod
    def _restore_character_names(text: str, replacements: dict[str, str]) -> str:
        restored = text
        for marker, preferred in replacements.items():
            restored = restored.replace(marker, preferred)
            flexible = r"[\s_-]*".join(re.escape(char) for char in marker)
            restored = re.sub(flexible, preferred, restored, flags=re.IGNORECASE)
        return restored

    def translate_selection(
        self,
        text: str,
        context: str = "",
        target_language: str | None = None,
        media_id: int | None = None,
    ) -> dict[str, Any]:
        selected = re.sub(r"\s+", " ", str(text or "")).strip()[:450]
        preceding = "\n".join(
            re.sub(r"\s+", " ", line).strip()
            for line in str(context or "").splitlines()
            if line.strip()
        )[-4000:]
        # target_language is retained only for API compatibility with older
        # clients. The app language is authoritative.
        target = (
            "ru"
            if str(getattr(getattr(self.config, "ui", None), "language", "en")).lower() == "ru"
            else "en"
        )
        if not selected:
            raise LightNovelError("Select Japanese text to translate")
        glossary = self.character_glossary(media_id)
        glossary_key = json.dumps(glossary, ensure_ascii=False, sort_keys=True)
        cache_key = hashlib.sha256(
            f"{target}\0{preceding}\0{selected}\0{glossary_key}".encode("utf-8")
        ).hexdigest()
        with self._connect() as conn:
            row = conn.execute("SELECT translation,provider FROM ln_translation_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if row is not None:
            return {
                "translation": str(row["translation"]),
                "target_language": target,
                "provider": str(row["provider"] or ""),
                "cached": True,
            }
        provider = "local_llm" if preceding and self.config.llm.enabled else "google"
        try:
            if provider == "local_llm":
                translated = self._translate_local_llm(selected, preceding, target, glossary)
            else:
                protected, replacements = self._protect_character_names(selected, glossary)
                translated = self._restore_character_names(
                    self._translate_online(protected, target), replacements
                )
        except Exception as primary_exc:
            self._log("LN primary translation failed; using fallback: %s", primary_exc)
            try:
                if provider == "local_llm":
                    provider = "google"
                    protected, replacements = self._protect_character_names(selected, glossary)
                    translated = self._restore_character_names(
                        self._translate_online(protected, target), replacements
                    )
                else:
                    provider = "local_llm"
                    translated = self._translate_local_llm(selected, preceding, target, glossary)
            except Exception as fallback_exc:
                raise LightNovelError(f"Translation failed: {fallback_exc}") from fallback_exc
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ln_translation_cache(cache_key,target_language,source_text,context_text,translation,provider,created_at) VALUES(?,?,?,?,?,?,?)",
                (cache_key, target, selected, preceding, translated, provider, time.time()),
            )
        return {
            "translation": translated,
            "target_language": target,
            "provider": provider,
            "cached": False,
        }

    def cancel_reader_background(self) -> None:
        # Running HTTP calls cannot be force-killed safely, but invalidating both
        # generations prevents their completion from spawning more prefetch work.
        with self._prefetch_lock:
            self._reader_generation += 1
            self._prefetch_generation += 1

    def save_bookmark(
        self,
        book_id: int,
        chapter_index: int,
        offset: float,
        *,
        source: str = "manual",
    ) -> dict[str, Any]:
        chapter = max(0, int(chapter_index))
        position = max(0.0, min(1.0, float(offset)))
        kind = "auto" if str(source).casefold() == "auto" else "manual"
        now = time.time()
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM ln_books WHERE id=?", (int(book_id),)
            ).fetchone()
            if exists is None:
                raise LightNovelError("Light novel not found")
            conn.execute(
                "UPDATE ln_books SET current_chapter=?,current_offset=?,updated_at=? WHERE id=?",
                (chapter, position, now, int(book_id)),
            )
            conn.execute(
                """INSERT INTO ln_bookmarks(book_id,chapter_index,offset,source,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                   chapter_index=excluded.chapter_index,offset=excluded.offset,
                   source=excluded.source,updated_at=excluded.updated_at""",
                (int(book_id), chapter, position, kind, now),
            )
        return {
            "book_id": int(book_id),
            "chapter_index": chapter,
            "offset": position,
            "source": kind,
            "updated_at": now,
        }

    def progress_summary(self, book_id: int) -> dict[str, Any]:
        """Return exact character-weighted local reading progress for one book."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT b.current_chapter,b.current_offset,b.finished,
                          COALESCE(SUM(LENGTH(c.text)),0) AS character_count,
                          COALESCE(SUM(
                              CASE
                                  WHEN c.chapter_index < b.current_chapter THEN LENGTH(c.text)
                                  WHEN c.chapter_index = b.current_chapter THEN LENGTH(c.text) * b.current_offset
                                  ELSE 0
                              END
                          ),0) AS read_character_count
                   FROM ln_books b
                   LEFT JOIN ln_chapters c ON c.book_id=b.id
                   WHERE b.id=?
                   GROUP BY b.id""",
                (int(book_id),),
            ).fetchone()
        if row is None:
            raise LightNovelError("Light novel not found")
        total = max(0, int(row["character_count"] or 0))
        read = max(0.0, float(row["read_character_count"] or 0.0))
        if bool(row["finished"]) and total:
            read = float(total)
        if total:
            read = min(float(total), read)
            progress = read / float(total)
        else:
            progress = 1.0 if bool(row["finished"]) else 0.0
        return {
            "current_chapter": int(row["current_chapter"] or 0),
            "current_offset": max(0.0, min(1.0, float(row["current_offset"] or 0.0))),
            "character_count": total,
            "read_character_count": round(read, 3),
            "reading_progress": max(0.0, min(1.0, progress)),
            "reading_progress_percent": round(max(0.0, min(1.0, progress)) * 100.0, 2),
            "finished": bool(row["finished"]),
        }

    def reset_position(self, book_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM ln_books WHERE id=?", (int(book_id),)
            ).fetchone()
            if exists is None:
                raise LightNovelError("Light novel not found")
            conn.execute("DELETE FROM ln_bookmarks WHERE book_id=?", (int(book_id),))
            conn.execute(
                "UPDATE ln_books SET current_chapter=0,current_offset=0,updated_at=? WHERE id=?",
                (time.time(), int(book_id)),
            )
        return self.book(int(book_id))

    def update_position(self, book_id: int, chapter_index: int, offset: float) -> None:
        # Compatibility with older WebViews. New readers call save_bookmark and
        # distinguish deliberate/manual bookmarks from dwell-based ones.
        self.save_bookmark(
            int(book_id), int(chapter_index), float(offset), source="auto"
        )

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
        query($userId:Int!){MediaListCollection(userId:$userId,type:MANGA){lists{entries{status progress progressVolumes score(format:POINT_10) media{id format chapters volumes status synonyms meanScore genres startDate{year} title{userPreferred romaji english native}coverImage{large}siteUrl relations{edges{relationType node{id format}}}}}}}}
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
                    "mean_score": media.get("meanScore"), "genres": media.get("genres") or [],
                    "year": (media.get("startDate") or {}).get("year"),
                    "relations": [
                        {
                            "relation_type": str((edge or {}).get("relationType") or ""),
                            "media_id": ((edge or {}).get("node") or {}).get("id"),
                            "format": str(((edge or {}).get("node") or {}).get("format") or ""),
                        }
                        for edge in ((media.get("relations") or {}).get("edges") or [])
                        if str((edge or {}).get("relationType") or "").upper() in {"PREQUEL", "SEQUEL"}
                    ],
                    "user_score": float(entry.get("score")) if entry.get("score") is not None else None,
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
        text = re.sub(r"\s*[<＜][^<>＜＞]{1,160}[>＞]\s*$", " ", text)
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
        query($search:String!,$perPage:Int!){Page(page:1,perPage:$perPage){media(search:$search,type:MANGA,format:NOVEL,sort:SEARCH_MATCH){id format chapters volumes status synonyms meanScore genres startDate{year} title{userPreferred romaji english native}coverImage{large}siteUrl relations{edges{relationType node{id format}}} mediaListEntry{status progress progressVolumes score(format:POINT_10)}}}}
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
                "mean_score": media.get("meanScore"), "genres": media.get("genres") or [],
                "year": (media.get("startDate") or {}).get("year"),
                "relations": [
                    {
                        "relation_type": str((edge or {}).get("relationType") or ""),
                        "media_id": ((edge or {}).get("node") or {}).get("id"),
                        "format": str(((edge or {}).get("node") or {}).get("format") or ""),
                    }
                    for edge in ((media.get("relations") or {}).get("edges") or [])
                    if str((edge or {}).get("relationType") or "").upper() in {"PREQUEL", "SEQUEL"}
                ],
                "user_score": float(entry.get("score")) if entry.get("score") is not None else None,
            })
        return out

    def _anilist_novel_by_id(self, media_id: int) -> dict[str, Any]:
        gql = """
        query($id:Int!){Media(id:$id,type:MANGA){id format chapters volumes status synonyms meanScore genres startDate{year} title{userPreferred romaji english native}coverImage{large}siteUrl relations{edges{relationType node{id format}}} mediaListEntry{status progress progressVolumes score(format:POINT_10)}}}
        """
        media = self._anilist_post(gql, {"id": int(media_id)}).get("Media") or {}
        if str(media.get("format") or "").upper() != "NOVEL":
            raise LightNovelError("Selected AniList entry is not a light novel")
        titles = media.get("title") or {}; entry = media.get("mediaListEntry") or {}
        return {"media_id": media.get("id"), "title": titles.get("userPreferred") or titles.get("romaji") or "", "format": "NOVEL", "status": entry.get("status") or "", "progress": entry.get("progress") or 0, "progress_volumes": entry.get("progressVolumes") or 0, "volumes": media.get("volumes"), "chapters": media.get("chapters"), "media_status": media.get("status"), "cover": (media.get("coverImage") or {}).get("large") or "", "site_url": media.get("siteUrl") or "", "mean_score": media.get("meanScore"), "genres": media.get("genres") or [], "year": (media.get("startDate") or {}).get("year"), "relations": [{"relation_type": str((edge or {}).get("relationType") or ""), "media_id": ((edge or {}).get("node") or {}).get("id"), "format": str(((edge or {}).get("node") or {}).get("format") or "")} for edge in ((media.get("relations") or {}).get("edges") or []) if str((edge or {}).get("relationType") or "").upper() in {"PREQUEL", "SEQUEL"}], "user_score": float(entry.get("score")) if entry.get("score") is not None else None}

    @staticmethod
    def _match_title(value: str) -> str:
        value = _series_title(value)
        value = re.sub(r"[\[\](){}._-]+", " ", value)
        return re.sub(r"\s+", " ", value).strip().casefold()

    def auto_bind_book_anilist(self, book_id: int) -> dict[str, Any] | None:
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            return None
        try:
            book = self.book(int(book_id))
        except LightNovelError:
            return None
        if book.get("anilist_id"):
            return book
        if self._inherit_series_anilist(int(book_id)):
            return self.book(int(book_id))
        query = str(book.get("series_title") or book.get("title") or Path(str(book.get("file_path") or "")).stem)
        rows = self.search_anilist_novels(query, limit=10)
        if not rows:
            return None
        source = self._match_title(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            candidates = [row.get("title"), *(row.get("titles") or []), *(row.get("synonyms") or [])]
            best = max((float(fuzz.WRatio(source, self._match_title(str(value or "")))) for value in candidates if value), default=0.0)
            scored.append((best, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return None
        best_score, best = scored[0]
        margin = best_score - (scored[1][0] if len(scored) > 1 else 0.0)
        exact = any(self._match_title(str(value or "")) == source for value in [best.get("title"), *(best.get("titles") or []), *(best.get("synonyms") or [])] if value)
        if not exact and (best_score < 90.0 or (best_score < 97.0 and margin < 7.0)):
            self._log("LN AniList auto-match skipped book_id=%s title=%r score=%.1f margin=%.1f", book_id, query, best_score, margin)
            return None
        bound = self.bind_anilist(int(book_id), int(best["media_id"]), best)
        self._log("LN AniList auto-matched book_id=%s media_id=%s title=%r score=%.1f", book_id, best["media_id"], query, best_score)
        return bound

    def queue_auto_bind_anilist(self, book_id: int, *, delay: float = 0.8) -> bool:
        book_id = int(book_id)
        if book_id <= 0 or not self.config.anilist.enabled or not self.config.anilist.access_token:
            return False
        with self._anilist_bind_lock:
            if book_id in self._anilist_bind_inflight:
                return False
            self._anilist_bind_inflight.add(book_id)

        def worker() -> None:
            try:
                if delay > 0:
                    time.sleep(delay)
                self.auto_bind_book_anilist(book_id)
            except Exception as exc:
                self._log("LN AniList auto-match failed book_id=%s error=%s", book_id, exc)
            finally:
                with self._anilist_bind_lock:
                    self._anilist_bind_inflight.discard(book_id)
                with self._state_refresh_lock:
                    self._state_version += 1

        threading.Thread(target=worker, name=f"ln-anilist-{book_id}", daemon=True).start()
        return True

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
                    "UPDATE ln_books SET anilist_id=?,anilist_status=?,anilist_progress_volumes=?,anilist_total_volumes=?,cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?",
                    (int(best["media_id"]), str(best.get("status") or ""), int(best.get("progress_volumes") or 0), best.get("volumes"), str(best.get("cover") or ""), time.time(), int(book["id"])),
                )
            changed += 1
        return changed

    def bind_anilist(self, book_id: int, media_id: int, selection: dict[str, Any] | None = None) -> dict[str, Any]:
        item = dict(selection or {})
        if int(item.get("media_id") or 0) != int(media_id):
            item = self._anilist_novel_by_id(int(media_id))
        with self._connect() as conn:
            conn.execute("UPDATE ln_books SET anilist_id=?,anilist_status=?,anilist_progress_volumes=?,anilist_total_volumes=?,anilist_user_score=?,cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?", (int(media_id), str(item.get("status") or ""), int(item.get("progress_volumes") or 0), item.get("volumes"), item.get("user_score"), str(item.get("cover") or ""), time.time(), int(book_id)))
        self._propagate_series_anilist(int(book_id))
        return self.book(book_id)

    def unbind_anilist(self, book_id: int) -> dict[str, Any]:
        book = self.book(int(book_id))
        series_key = str(book.get("series_key") or _series_key(str(book.get("title") or "")))
        ids = [int(item["id"]) for item in self.books() if str(item.get("series_key") or "") == series_key] or [int(book_id)]
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE ln_books SET anilist_id=NULL,anilist_status='',anilist_progress_volumes=0,anilist_total_volumes=NULL,anilist_user_score=NULL,updated_at=? WHERE id IN ({placeholders})",
                (time.time(), *ids),
            )
        return self.book(int(book_id))

    def set_score(self, book_id: int, score: float) -> dict[str, Any]:
        # pudge-v0.7.23-ln-series-score-v1
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT anilist_id FROM ln_books WHERE id=?",
                (int(book_id),),
            ).fetchone()
            if row is None:
                raise LightNovelError(f"Unknown light novel id={book_id}")
            if row["anilist_id"] is not None:
                conn.execute(
                    "UPDATE ln_books SET anilist_user_score=?,updated_at=? WHERE anilist_id=?",
                    (float(score), now, int(row["anilist_id"])),
                )
            else:
                conn.execute(
                    "UPDATE ln_books SET anilist_user_score=?,updated_at=? WHERE id=?",
                    (float(score), now, int(book_id)),
                )
        return self.book(int(book_id))

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

    def set_finished(self, book_id: int, finished: bool) -> dict[str, Any]:
        """Set the local Finished badge without reducing AniList progress."""

        if bool(finished):
            return self.finish_volume(int(book_id))
        with self._connect() as conn:
            conn.execute(
                "UPDATE ln_books SET finished=0,updated_at=? WHERE id=?",
                (time.time(), int(book_id)),
            )
        return self.book(int(book_id))

    @staticmethod
    def _nyaa_title(value: str) -> str:
        text = _series_title(html.unescape(str(value or "")))
        text = re.sub(r"(?i)\b(?:light[ ._-]*novel|novel)\b", " ", text)
        text = re.sub(r"(?i)\b(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}\b", " ", text)
        text = re.sub(r"第\s*0*\d{1,3}\s*巻", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _literature_volume_range(value: str) -> tuple[int, int] | None:
        text = unicodedata.normalize("NFKC", str(value or ""))
        patterns = (
            r"(?i)\b(?:vol(?:ume)?s?|v)\s*0*(\d{1,3})\s*[-~–—]\s*(?:vol(?:ume)?s?|v)?\s*0*(\d{1,3})\b",
            r"第?\s*0*(\d{1,3})\s*[-~–—]\s*0*(\d{1,3})\s*巻",
            r"(?i)(?:^|[\s\[(])0*(\d{1,3})\s*[-~–—]\s*0*(\d{1,3})(?=$|[\s._\])])",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            start, end = int(match.group(1)), int(match.group(2))
            if 0 < start <= end <= 999:
                return start, end
        return None

    @classmethod
    def _release_volume_match(cls, title: str, target_volume: int) -> tuple[bool, tuple[int, int] | None, bool]:
        target = int(target_volume or 0)
        volume_range = cls._literature_volume_range(title)
        if target <= 0:
            return True, volume_range, False
        if volume_range is not None:
            return volume_range[0] <= target <= volume_range[1], volume_range, False
        explicit = _volume_from_text(title)
        if explicit is not None:
            return int(explicit) == target, (int(explicit), int(explicit)), True
        return True, None, False

    def search_nyaa(
        self,
        query: str,
        *,
        target_volume: int | None = None,
    ) -> list[dict[str, Any]]:
        settings = self.settings()
        clean_query = self._nyaa_title(query)
        if not clean_query:
            return []
        client = NyaaClient(
            self.config.nyaa.base_url,
            category=settings.nyaa_category,
            proxy_mode=self.config.nyaa.proxy_mode,
            proxy_url=self.config.nyaa.proxy_url,
            pre_search_command=self.config.nyaa.pre_search_command,
        )
        releases = client.search(clean_query, category="3_3")
        target = max(0, int(target_volume or 0))
        eligible: list[tuple[NyaaRelease, tuple[int, int] | None, bool]] = []
        for release in releases:
            matches, volume_range, exact = self._release_volume_match(release.title, target)
            if matches:
                eligible.append((release, volume_range, exact))
        # Prefer a release that explicitly contains the requested volume, then title
        # match and availability. Unlabelled releases remain as a fallback.
        words = {w for w in re.findall(r"[a-z0-9]+", clean_query.casefold()) if len(w) > 2}
        def score(row: tuple[NyaaRelease, tuple[int, int] | None, bool]) -> tuple[int, int, int, int]:
            r, volume_range, exact = row
            title_words = set(re.findall(r"[a-z0-9]+", r.title.casefold()))
            overlap = len(words & title_words)
            volume_score = 3 if exact else (2 if volume_range is not None else 1)
            return (volume_score, overlap, int(r.seeders), int(r.size_bytes))
        eligible.sort(key=score, reverse=True)
        return [
            {
                "title": r.title,
                "torrent_url": r.torrent_url,
                "link": r.link,
                "info_hash": r.info_hash,
                "seeders": r.seeders,
                "size": r.size_text,
                "trusted": r.trusted,
                "target_volume": target or None,
                "volume_start": volume_range[0] if volume_range else None,
                "volume_end": volume_range[1] if volume_range else None,
                "volume_range": list(volume_range) if volume_range else None,
                "contains_target_volume": bool(target and volume_range),
            }
            for r, volume_range, _exact in eligible[:30]
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
        target_volume = max(0, int(release.get("target_volume") or 0))
        try:
            torrent_hash = qbt.add_release(
                item,
                save_path=self.root,
                category=f"{APP_SLUG}-ln",
                tags=[APP_SLUG, "light-novel"] + ([f"volume: {target_volume}"] if target_volume else []),
                paused=bool(target_volume),
                stop_at_metadata=bool(target_volume),
            )
            selected_files: list[str] = []
            if target_volume:
                selected_files = self._select_torrent_volume_files(
                    qbt,
                    torrent_hash,
                    target_volume,
                    allow_unlabelled=(
                        int(release.get("volume_start") or 0) == target_volume
                        and int(release.get("volume_end") or 0) == target_volume
                    ),
                )
                qbt.start(torrent_hash)
        except Exception:
            if target_volume and 'torrent_hash' in locals() and torrent_hash:
                try:
                    qbt.delete(torrent_hash, delete_files=True)
                except Exception:
                    pass
            raise
        finally:
            qbt.close()
        if target_volume and torrent_hash:
            threading.Thread(
                target=self._finish_volume_torrent,
                args=(str(torrent_hash), target_volume),
                name=f"ln-volume-{target_volume}-download",
                daemon=True,
            ).start()
        return {
            "ok": True,
            "torrent_hash": torrent_hash,
            "target_volume": target_volume or None,
            "selected_files": selected_files if target_volume else [],
        }

    def _qbt_client(self) -> QBittorrentClient:
        return QBittorrentClient(
            self.config.qbittorrent.base_url,
            self.config.qbittorrent.username,
            self.config.qbittorrent.password,
            self.config.qbittorrent.api_key,
            verify_tls=self.config.qbittorrent.verify_tls,
            pre_download_command=self.config.qbittorrent.pre_download_command,
            auto_start_app=self.config.qbittorrent.auto_start_app,
        )

    @staticmethod
    def _downloadable_literature_file(name: str) -> bool:
        return Path(str(name or "")).suffix.casefold() in {
            ".epub", ".txt", ".pdf", ".mobi", ".azw3", ".zip", ".rar", ".7z"
        }

    @classmethod
    def _torrent_file_volume(cls, name: str) -> int | None:
        path = Path(str(name or ""))
        parts = [path.stem, *reversed(path.parts[:-1])]
        for part in parts:
            if cls._literature_volume_range(part) is not None:
                continue
            explicit = _volume_from_text(part)
            if explicit is not None:
                return explicit
            normalized = unicodedata.normalize("NFKC", part)
            matches = re.findall(
                r"(?:^|[\s._\-\[\]()])0*(\d{1,3})(?=$|[\s._\-\[\]()])",
                normalized,
            )
            if matches:
                number = int(matches[-1])
                if 0 < number <= 300:
                    return number
        return None

    def _select_torrent_volume_files(
        self,
        qbt: QBittorrentClient,
        torrent_hash: str,
        target_volume: int,
        *,
        allow_unlabelled: bool = False,
    ) -> list[str]:
        deadline = time.monotonic() + 35.0
        files: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            files = qbt.files(torrent_hash)
            if files:
                break
            time.sleep(0.5)
        if not files:
            raise LightNovelError("Could not read the torrent file list")
        candidates = [row for row in files if self._downloadable_literature_file(str(row.get("name") or ""))]
        selected = [
            row for row in candidates
            if self._torrent_file_volume(str(row.get("name") or "")) == int(target_volume)
        ]
        if not selected and allow_unlabelled:
            selected = candidates
        if not selected:
            archives = []
            for row in candidates:
                name = str(row.get("name") or "")
                if Path(name).suffix.casefold() not in {".zip", ".rar", ".7z"}:
                    continue
                volume_range = self._literature_volume_range(name)
                if volume_range and volume_range[0] <= int(target_volume) <= volume_range[1]:
                    archives.append(row)
            if archives:
                selected = archives[:1]
        if not selected:
            raise LightNovelError(f"Volume {target_volume} is not a separate file in this torrent")
        all_ids = [int(row.get("index")) for row in files if row.get("index") is not None]
        selected_ids = [int(row.get("index")) for row in selected if row.get("index") is not None]
        qbt.set_file_priority(torrent_hash, all_ids, 0)
        qbt.set_file_priority(torrent_hash, selected_ids, 6)
        return [str(row.get("name") or "") for row in selected]

    def _materialize_selected_volume(
        self,
        selected: list[dict[str, Any]],
        target_volume: int,
    ) -> list[Path]:
        archives: list[Path] = []
        extracted = 0
        for row in selected:
            relative = Path(str(row.get("name") or ""))
            source = (self.root / relative).resolve()
            try:
                source.relative_to(self.root.resolve())
            except ValueError as exc:
                raise LightNovelError("Torrent file is outside the Light Novels folder") from exc
            suffix = source.suffix.casefold()
            if suffix not in {".zip", ".rar", ".7z"}:
                continue
            if not source.is_file():
                raise LightNovelError(f"Downloaded archive is missing: {relative.name}")
            archives.append(source)
            if suffix == ".zip":
                with zipfile.ZipFile(source) as archive:
                    members = [
                        name for name in archive.namelist()
                        if Path(name).suffix.casefold() in {".epub", ".txt"}
                        and self._torrent_file_volume(name) == int(target_volume)
                    ]
                    for member in members:
                        destination = self._selected_volume_destination(Path(member).name, target_volume)
                        with archive.open(member) as src, destination.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        extracted += 1
                continue
            from .providers.jimaku import find_7zip

            tool = find_7zip()
            if not tool:
                raise LightNovelError("7-Zip is required to extract this Light Novel batch")
            with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-ln-volume-") as temp_dir:
                completed = subprocess.run(
                    [tool, "x", "-y", f"-o{temp_dir}", str(source)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if completed.returncode != 0:
                    raise LightNovelError(f"Could not extract {relative.name}")
                for candidate in Path(temp_dir).rglob("*"):
                    if (
                        candidate.is_file()
                        and candidate.suffix.casefold() in {".epub", ".txt"}
                        and self._torrent_file_volume(str(candidate.relative_to(temp_dir))) == int(target_volume)
                    ):
                        shutil.copy2(
                            candidate,
                            self._selected_volume_destination(candidate.name, target_volume),
                        )
                        extracted += 1
        if archives and not extracted:
            raise LightNovelError(f"Volume {target_volume} was not found inside the downloaded archive")
        return archives

    def _selected_volume_destination(self, name: str, target_volume: int) -> Path:
        safe_name = Path(str(name or "")).name or f"volume-{int(target_volume):02d}.epub"
        destination = self.root / safe_name
        if not destination.exists():
            return destination
        return self.root / f"{destination.stem}-volume-{int(target_volume):02d}{destination.suffix}"

    def _finish_volume_torrent(self, torrent_hash: str, target_volume: int) -> None:
        qbt = self._qbt_client()
        try:
            deadline = time.monotonic() + 14 * 24 * 3600
            while time.monotonic() < deadline:
                files = qbt.files(torrent_hash)
                selected = [row for row in files if int(row.get("priority") or 0) > 0]
                if selected and all(float(row.get("progress") or 0.0) >= 0.999 for row in selected):
                    archives = self._materialize_selected_volume(selected, target_volume)
                    qbt.delete(torrent_hash, delete_files=False)
                    for archive in archives:
                        archive.unlink(missing_ok=True)
                    self.scan_downloaded()
                    self._log("LN volume download complete volume=%s", target_volume)
                    return
                time.sleep(20.0)
        except Exception as exc:
            self._log("LN volume download monitor failed volume=%s error=%s", target_volume, exc)
        finally:
            qbt.close()

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
            query = self._nyaa_title(str(novel.get("title") or ""))
            releases = self.search_nyaa(query, target_volume=next_volume)
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
        by_media = {
            int(item["media_id"]): item
            for item in novels
            if item.get("media_id") is not None
        }
        franchise_by_media: dict[int, str] = {}
        adjacency: dict[int, set[int]] = {media_id: set() for media_id in by_media}
        for media_id, item in by_media.items():
            for relation in item.get("relations") or []:
                if not isinstance(relation, dict):
                    continue
                try:
                    other = int(relation.get("media_id"))
                except (TypeError, ValueError):
                    continue
                if (
                    other in adjacency
                    and str(relation.get("relation_type") or "").upper() in {"PREQUEL", "SEQUEL"}
                    and str(relation.get("format") or "NOVEL").upper() == "NOVEL"
                ):
                    adjacency[media_id].add(other)
                    adjacency[other].add(media_id)
        seen: set[int] = set()
        for root in sorted(adjacency):
            if root in seen:
                continue
            component: set[int] = set()
            stack = [root]
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                stack.extend(adjacency.get(current, ()))
            seen.update(component)
            if len(component) > 1:
                key = f"anilist-franchise:{min(component)}"
                for related_media_id in component:
                    franchise_by_media[related_media_id] = key

        books = self.books()
        for book in books:
            media_id = book.get("anilist_id")
            item = by_media.get(int(media_id)) if media_id is not None else None
            if not item:
                continue
            book["anilist_mean_score"] = item.get("mean_score")
            book["anilist_genres"] = list(item.get("genres") or [])
            book["anilist_year"] = item.get("year")
            book["anilist_media_status"] = item.get("media_status")
            book["franchise_key"] = franchise_by_media.get(int(media_id), "")
            if not book.get("cover_url") and item.get("cover"):
                book["cover_url"] = item["cover"]
        return {
            "books": books, "anilist": novels, "planning": planning, "settings": self.settings_payload(),
            "refreshing": bool(self._state_refreshing), "version": int(self._state_version),
        }

    def request_state_refresh(self, *, force: bool = False) -> bool:
        with self._state_refresh_lock:
            if self._state_refreshing:
                return False
            self._state_refreshing = True

        def worker() -> None:
            try:
                self._migrate_inline_covers()
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
