"""Declarations of the window's stylesheets, parsed (audit 71Б).

For tests about styling that the page harness cannot observe (it models only
`display`): look rules up by selector instead of searching the CSS text, so
formatting and rule order do not matter.

    rules = css_rules("reading_tools.css")
    assert rules.get(".ln-reader .word.known", "color") == "var(--known)"
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "pudge" / "web"


class CssRules:
    def __init__(self, rules: list[tuple[str, str, dict[str, str]]]) -> None:
        self.rules = rules  # (media, selector, declarations)

    def declarations(self, selector: str, *, media: str | None = "") -> dict[str, str]:
        """Merged declarations of every rule whose selector list contains `selector`
        (whitespace-insensitive); media="" means rules outside @media, None any."""
        want = _norm_selector(selector)
        merged: dict[str, str] = {}
        for rule_media, rule_selector, decls in self.rules:
            if media is not None and _norm_media(rule_media) != _norm_media(media):
                continue
            if want in [_norm_selector(s) for s in _split(rule_selector)]:
                merged.update(decls)
        return merged

    def get(self, selector: str, prop: str, *, media: str | None = "") -> str | None:
        return self.declarations(selector, media=media).get(prop.lower())

    def selectors(self) -> list[str]:
        return [_norm_selector(s) for _m, sel, _d in self.rules for s in _split(sel)]


def _split(selector: str) -> list[str]:
    out, depth, start = [], 0, 0
    for k, c in enumerate(selector):
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(selector[start:k])
            start = k + 1
    out.append(selector[start:])
    return [s.strip() for s in out if s.strip()]


def _norm_selector(selector: str) -> str:
    s = re.sub(r"\s+", " ", selector.strip())
    return re.sub(r"\s*([>+~,])\s*", r"\1", s)


def _norm_media(media: str) -> str:
    return re.sub(r"\s+", "", media or "").lower()


def _parse(text: str) -> list[tuple[str, str, dict[str, str]]]:
    css = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules: list[tuple[str, str, dict[str, str]]] = []

    def walk(body: str, media: str) -> None:
        k = 0
        while k < len(body):
            open_at = body.find("{", k)
            if open_at < 0:
                return
            head = body[k:open_at].strip()
            depth, end = 1, open_at + 1
            while end < len(body) and depth:
                if body[end] == "{":
                    depth += 1
                elif body[end] == "}":
                    depth -= 1
                end += 1
            inner = body[open_at + 1 : end - 1]
            k = end
            if head.startswith("@media"):
                walk(inner, head[len("@media") :].strip())
            elif head.startswith("@"):
                continue
            else:
                decls: dict[str, str] = {}
                for part in inner.split(";"):
                    if ":" in part:
                        name, value = part.split(":", 1)
                        decls[name.strip().lower()] = re.sub(r"\s+", " ", value.strip())
                rules.append((media, head, decls))

    walk(css, "")
    return rules


def parse_css(text: str) -> CssRules:
    """Rules of CSS text (e.g. a stylesheet a page adds at run time)."""
    return CssRules(_parse(text))


@lru_cache(maxsize=None)
def css_rules(name: str) -> CssRules:
    """Rules of pudge/web/<name>; for index.html, its <style> blocks."""
    text = (WEB / name).read_text(encoding="utf-8")
    if name.endswith(".html"):
        text = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S))
    return CssRules(_parse(text))
