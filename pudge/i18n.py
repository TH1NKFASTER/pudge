"""Interface strings for Python (audit 68Б).

The catalogs are the window's ``pudge/web/locales.<lang>.js`` files: strict
JSON wrapped in one line of JavaScript, so the window, Python and the mpv
scripts share one source.

* ``t(key, **params)``: text in the interface language (console output, mpv
  messages, notifications).
* ``tn(key, count, **params)``: a plural entry.
* ``err(key, **params)``: an error message, always English. Logs stay English;
  the window shows the message in its language (``PudgeI18n.message``).

The interface language is set once per process from the config
(``set_language``); a missing key falls back to English, then to the key.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGUAGES = ("en", "ru")
_WEB = Path(__file__).resolve().parent / "web"
_FIELD = re.compile(r"\{([A-Za-z0-9_]+)\}")
_default_language = "en"


def normalize_language(value: object) -> str:
    return "ru" if str(value or "").strip().casefold().startswith("ru") else "en"


def set_language(value: object) -> str:
    """The process-wide interface language (from ``config.ui.language``)."""
    global _default_language
    _default_language = normalize_language(value)
    return _default_language


def language() -> str:
    return _default_language


def catalog_path(lang: str) -> Path:
    return _WEB / f"locales.{normalize_language(lang)}.js"


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, Any]:
    lang = normalize_language(lang)
    text = catalog_path(lang).read_text(encoding="utf-8")
    marker = f").{lang} = "
    start = text.index(marker) + len(marker)
    end = text.rindex("};") + 1
    data = json.loads(text[start:end])
    if not isinstance(data, dict):
        raise ValueError(f"locale catalog {lang} is not an object")
    return data


def plural_form(lang: str, count: object) -> str:
    try:
        number = abs(float(count))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = 0.0
    if normalize_language(lang) == "en":
        return "one" if number == 1 else "other"
    if number != int(number):
        return "other"
    n = int(number)
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _raw(key: str, lang: str) -> Any:
    own = catalog(lang)
    if key in own:
        return own[key]
    english = catalog("en")
    return english.get(key)


def has(key: str) -> bool:
    return key in catalog("en")


def format_text(template: str, params: dict[str, Any]) -> str:
    """Fill {name} fields; a passed None becomes empty text, an unknown field stays as is."""

    def field(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            return match.group(0)
        value = params[name]
        return "" if value is None else str(value)

    return _FIELD.sub(field, str(template))


def _pick(value: Any, lang: str, params: dict[str, Any]) -> str:
    if isinstance(value, dict):
        form = plural_form(lang, params.get("count", 0))
        return str(value.get(form) or value.get("other") or value.get("many") or value.get("one") or "")
    return str(value)


def t(key: str, /, lang: str | None = None, **params: Any) -> str:
    lang = normalize_language(lang or language())
    value = _raw(str(key), lang)
    if value is None:
        return str(key)
    return format_text(_pick(value, lang, params), params)


def tn(key: str, count: object, /, lang: str | None = None, **params: Any) -> str:
    return t(key, lang=lang, count=count, **params)


def err(key: str, /, **params: Any) -> str:
    """An error message in English (``error.<key>``); the window translates it."""
    full = key if key.startswith("error.") else f"error.{key}"
    return t(full, lang="en", **params)


@lru_cache(maxsize=None)
def _error_patterns() -> tuple[tuple[re.Pattern[str], tuple[str, ...], str], ...]:
    patterns: list[tuple[int, re.Pattern[str], tuple[str, ...], str]] = []
    for lang in LANGUAGES:
        for key, template in catalog(lang).items():
            if not key.startswith("error.") or not isinstance(template, str):
                continue
            names: list[str] = []
            parts: list[str] = []
            for part in re.split(r"(\{[A-Za-z0-9_]+\})", template):
                field = re.fullmatch(r"\{([A-Za-z0-9_]+)\}", part)
                if field:
                    names.append(field.group(1))
                    parts.append("(.*?)")
                else:
                    parts.append(re.escape(part))
            weight = len(_FIELD.sub("", template))
            if weight:
                patterns.append(
                    (weight, re.compile("^" + "".join(parts) + "$", re.DOTALL), tuple(names), key)
                )
    patterns.sort(key=lambda item: item[0], reverse=True)
    return tuple((pattern, names, key) for _weight, pattern, names, key in patterns)


def message(text: object, lang: str | None = None, *, _depth: int = 0) -> str:
    """A backend message (English, or Russian from older versions) in the interface language."""
    value = str(text or "")
    if not value or _depth > 3:
        return value
    lang = normalize_language(lang or language())
    prefix = "Error: " if value.startswith("Error: ") else ""
    body = value[len(prefix) :]
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "'\"":
        body = body[1:-1]  # str(KeyError(...)) quotes its message
    for pattern, names, key in _error_patterns():
        match = pattern.fullmatch(body)
        if match:
            params = {
                name: message(group, lang, _depth=_depth + 1) for name, group in zip(names, match.groups())
            }
            translated = format_text(_pick(_raw(key, lang), lang, params), params)
            return t("error.generic", lang=lang, error=translated) if prefix else translated
    return value


def placeholders(value: Any) -> set[str]:
    texts = value.values() if isinstance(value, dict) else [value]
    return {name for text in texts for name in _FIELD.findall(str(text))}
