"""Text matching for the window's global search (Cmd+F): every title, alias and
romanisation of an anime, light novel, manga or audiobook against what was typed."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

from rapidfuzz import fuzz


# pudge-ui-v0.7.23-global-search-recent-v1
def _global_search_normalize(value: Any) -> str:
    text = unicodedata.normalize(
        "NFKC",
        html.unescape(str(value or "")),
    ).casefold()
    return "".join(char for char in text if char.isalnum())


_KANA_ROMAJI = {
    "きゃ": "kya",
    "きゅ": "kyu",
    "きょ": "kyo",
    "しゃ": "sha",
    "しゅ": "shu",
    "しょ": "sho",
    "ちゃ": "cha",
    "ちゅ": "chu",
    "ちょ": "cho",
    "にゃ": "nya",
    "にゅ": "nyu",
    "にょ": "nyo",
    "ひゃ": "hya",
    "ひゅ": "hyu",
    "ひょ": "hyo",
    "みゃ": "mya",
    "みゅ": "myu",
    "みょ": "myo",
    "りゃ": "rya",
    "りゅ": "ryu",
    "りょ": "ryo",
    "ぎゃ": "gya",
    "ぎゅ": "gyu",
    "ぎょ": "gyo",
    "じゃ": "ja",
    "じゅ": "ju",
    "じょ": "jo",
    "びゃ": "bya",
    "びゅ": "byu",
    "びょ": "byo",
    "ぴゃ": "pya",
    "ぴゅ": "pyu",
    "ぴょ": "pyo",
    "てぃ": "ti",
    "でぃ": "di",
    "ふぁ": "fa",
    "ふぃ": "fi",
    "ふぇ": "fe",
    "ふぉ": "fo",
    "うぃ": "wi",
    "うぇ": "we",
    "うぉ": "wo",
    "あ": "a",
    "い": "i",
    "う": "u",
    "え": "e",
    "お": "o",
    "か": "ka",
    "き": "ki",
    "く": "ku",
    "け": "ke",
    "こ": "ko",
    "さ": "sa",
    "し": "shi",
    "す": "su",
    "せ": "se",
    "そ": "so",
    "た": "ta",
    "ち": "chi",
    "つ": "tsu",
    "て": "te",
    "と": "to",
    "な": "na",
    "に": "ni",
    "ぬ": "nu",
    "ね": "ne",
    "の": "no",
    "は": "ha",
    "ひ": "hi",
    "ふ": "fu",
    "へ": "he",
    "ほ": "ho",
    "ま": "ma",
    "み": "mi",
    "む": "mu",
    "め": "me",
    "も": "mo",
    "や": "ya",
    "ゆ": "yu",
    "よ": "yo",
    "ら": "ra",
    "り": "ri",
    "る": "ru",
    "れ": "re",
    "ろ": "ro",
    "わ": "wa",
    "を": "o",
    "ん": "n",
    "が": "ga",
    "ぎ": "gi",
    "ぐ": "gu",
    "げ": "ge",
    "ご": "go",
    "ざ": "za",
    "じ": "ji",
    "ず": "zu",
    "ぜ": "ze",
    "ぞ": "zo",
    "だ": "da",
    "ぢ": "ji",
    "づ": "zu",
    "で": "de",
    "ど": "do",
    "ば": "ba",
    "び": "bi",
    "ぶ": "bu",
    "べ": "be",
    "ぼ": "bo",
    "ぱ": "pa",
    "ぴ": "pi",
    "ぷ": "pu",
    "ぺ": "pe",
    "ぽ": "po",
    "ゔ": "vu",
}


def _kana_to_romaji(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    # Katakana -> hiragana while leaving kanji/latin untouched.
    text = "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)
    out: list[str] = []
    geminate = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "っ":
            geminate = True
            i += 1
            continue
        pair = text[i : i + 2]
        roma = _KANA_ROMAJI.get(pair)
        if roma is not None:
            i += 2
        else:
            roma = _KANA_ROMAJI.get(ch)
            i += 1
        if roma is None:
            if ch == "ー" and out:
                last = out[-1]
                vowel = next((v for v in reversed(last) if v in "aeiou"), "")
                if vowel:
                    out.append(vowel)
            elif ch.isalnum():
                out.append(ch.casefold())
            continue
        if geminate and roma:
            consonant = "t" if roma.startswith("ch") else "s" if roma.startswith("sh") else roma[0]
            if consonant not in "aeioun":
                out.append(consonant)
        geminate = False
        out.append(roma)
    return "".join(out)


def _global_search_keys(value: Any) -> list[str]:
    native = _global_search_normalize(value)
    romaji = _global_search_normalize(_kana_to_romaji(value))
    return list(dict.fromkeys(key for key in (native, romaji) if key))


def _global_search_score(
    query: str,
    names: list[str],
) -> tuple[float, str]:
    cleaned = re.sub(r"\s+", " ", str(query or "")).strip()
    query_keys = _global_search_keys(cleaned)
    query_key = query_keys[0] if query_keys else ""
    if not query_key:
        return 0.0, ""

    best_score = 0.0
    best_name = ""
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        name_keys = _global_search_keys(name)
        if not name_keys:
            continue
        score = 0.0
        for qkey in query_keys:
            for name_key in name_keys:
                if name_key == qkey:
                    candidate = 100.0
                elif name_key.startswith(qkey):
                    candidate = 97.0
                elif qkey in name_key:
                    candidate = 93.0
                else:
                    candidate = float(fuzz.ratio(qkey, name_key))
                score = max(score, candidate)
        score = max(score, float(fuzz.WRatio(cleaned.casefold(), name.casefold())))
        if score > best_score:
            best_score = score
            best_name = name
    return best_score, best_name
