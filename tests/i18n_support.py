"""Test helpers after audit 68Б: interface strings live in pudge/web/locales.<lang>.js."""

from __future__ import annotations

from pathlib import Path

from pudge import i18n

WEB = Path(__file__).resolve().parents[1] / "pudge" / "web"
I18N_SCRIPTS = ("locales.en.js", "locales.ru.js", "i18n.js")


def en(key: str, **params) -> str:
    return i18n.t(key, lang="en", **params)


def ru(key: str, **params) -> str:
    return i18n.t(key, lang="ru", **params)


def catalog(lang: str = "en") -> dict:
    return i18n.catalog(lang)


def keys_with_text(text: str, lang: str = "en", *, exact: bool = True) -> list[str]:
    """Catalog keys whose text (or any plural form) equals / contains ``text``."""
    found = []
    for key, value in catalog(lang).items():
        values = value.values() if isinstance(value, dict) else [value]
        if any((v == text) if exact else (text in v) for v in values):
            found.append(key)
    return found


def has_text(text: str, lang: str = "en", *, exact: bool = False) -> bool:
    return bool(keys_with_text(text, lang, exact=exact))


def i18n_source() -> str:
    """locales + i18n.js, to run in node before a module under test."""
    return "\n;\n".join((WEB / name).read_text(encoding="utf-8") for name in I18N_SCRIPTS)
