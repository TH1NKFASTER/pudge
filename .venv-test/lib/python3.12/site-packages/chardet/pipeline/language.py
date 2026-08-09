"""Three-tier language detection for filling DetectionResult languages.

Tier 1: hardcoded mapping for single-language encodings (e.g. Big5 -> Chinese).
Tier 2: statistical bigram scoring against the encoding's language-model variants.
Tier 3: decode to UTF-8 and score against the UTF-8 byte-level language models.

Note: ``from __future__ import annotations`` is intentionally omitted because
this module is compiled with mypyc, which does not support PEP 563 string
annotations.
"""

from chardet.models import (
    BigramProfile,
    has_model_variants,
    infer_language,
    score_best_language,
)
from chardet.pipeline import DetectionResult

# Maximum bytes of data used for language scoring.
# Language bigrams converge quickly — 2 KB is sufficient for discrimination
# across all language models while keeping Tier 3 (language-model scoring) fast.
_LANG_SCORE_MAX_BYTES = 2048


def _to_utf8(data: bytes, encoding: str) -> bytes | None:
    """Decode data from encoding and re-encode as UTF-8 for language scoring.

    Returns None if the encoding is unknown. For UTF-8, returns data as-is.
    Uses ``errors="ignore"`` because the data already passed byte-validity
    filtering for the detected encoding; any residual invalid bytes are
    irrelevant for language scoring.
    """
    if encoding == "utf-8":
        return data
    try:
        return data.decode(encoding, errors="ignore").encode(
            "utf-8", errors="surrogatepass"
        )
    except (LookupError, TypeError, ValueError):
        return None


def fill_languages(
    data: bytes, results: list[DetectionResult]
) -> list[DetectionResult]:
    """Fill missing ``language`` fields on text results via the three-tier algorithm.

    Tier 1: single-language encodings via hardcoded map (instant).
    Tier 2: multi-language encodings via statistical bigram scoring (lazy).
    Tier 3: decode to UTF-8, score against UTF-8 language models (universal fallback).

    Binary results (``encoding is None``) are passed through unchanged, as are
    results that already have a non-``None`` language.

    :param data: The raw byte data the results were produced from.  Truncated
        to the first 2 KB internally — bigram language models converge quickly.
    :param results: A list of :class:`DetectionResult` from the pipeline.
    :returns: A list of results with ``language`` filled in where possible.
    """
    data = data[:_LANG_SCORE_MAX_BYTES]
    filled: list[DetectionResult] = []
    profile: BigramProfile | None = None
    utf8_profile: BigramProfile | None = None
    for result in results:
        if result.language is not None or result.encoding is None:
            filled.append(result)
            continue
        encoding = result.encoding
        # Tier 1: single-language encoding
        lang = infer_language(encoding)
        # Tier 2: statistical scoring for multi-language encodings
        if lang is None and data and has_model_variants(encoding):
            if profile is None:
                profile = BigramProfile(data)
            _, lang = score_best_language(data, encoding, profile=profile)
        # Tier 3: decode to UTF-8, score against UTF-8 language models
        if lang is None and data and has_model_variants("utf-8"):
            utf8_data = _to_utf8(data, encoding)
            if utf8_data:
                if utf8_profile is None or encoding != "utf-8":
                    utf8_profile = BigramProfile(utf8_data)
                _, lang = score_best_language(utf8_data, "utf-8", profile=utf8_profile)
        if lang is None:
            filled.append(result)
        else:
            filled.append(
                DetectionResult(encoding, result.confidence, lang, result.mime_type)
            )
    return filled
