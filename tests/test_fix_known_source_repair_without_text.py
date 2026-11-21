"""The known-source repair (Bleach S01E45, NanakoRaws TVA) ships no subtitle text.

It recognises the TVA file's opening sentences by the SHA-256 of their normalized text and
builds the repaired opening from the user's own file: each recognised sentence is moved
onto the WEB reference cue it is spoken in. The two lines the TVA file never had are not
restored (the repository cannot hold them), so their reference cue stays without Japanese.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pudge.subtitle_formats import parse_srt
from pudge.subtitles import source_repairs
from pudge.subtitles.source_repairs import apply_known_source_repair

NAME = "[NanakoRaws] Bleach Sennen Kessen-hen S01E45 (TVA TV 1080p HEVC AAC).srt"
# Invented stand-ins for the real file's sentences.
FIRST, SECOND, THIRD = "明かりはもう十分だ。", "けれど　まだ足りない➨", "鐘が鳴るまで待て！"
REFERENCE = (
    "1\n00:00:17,590 --> 00:00:21,770\nGetsuga and Gran Rey, side by side...\n\n"
    "2\n00:00:21,770 --> 00:00:27,360\nA fitting test for every lamp in the hall.\n\n"
    "3\n00:00:28,010 --> 00:00:31,360\nBut still...\n\n"
    "4\n00:00:31,360 --> 00:00:33,450\nwait for the bell!\n\n"
    "5\n00:02:27,000 --> 00:02:29,000\nLater dialogue\n"
)


def _point_rule_at_invented_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = source_repairs._sentence_hash
    monkeypatch.setattr(
        source_repairs,
        "_BLEACH_E45_SOURCE_SENTENCES",
        {digest(FIRST): 1, digest(SECOND): 2, digest(THIRD): 3},
    )
    monkeypatch.setattr(source_repairs, "_BLEACH_E45_REQUIRED_SENTENCES", (digest(FIRST), digest(THIRD)))


def _repair(tmp_path: Path, source_text: str):
    source = tmp_path / "aligned.srt"
    source.write_text(source_text, encoding="utf-8")
    reference = tmp_path / "english.srt"
    reference.write_text(REFERENCE, encoding="utf-8")
    return apply_known_source_repair(
        source,
        tmp_path / "cache",
        media_id=185874,
        episode=5,
        candidate_name=NAME,
        timing_reference=reference,
    )


def test_the_shipped_rule_holds_only_sentence_hashes() -> None:
    known = source_repairs._BLEACH_E45_SOURCE_SENTENCES
    assert known and all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in known)
    assert set(known.values()) <= {0, 1, 2, 3}
    assert set(source_repairs._BLEACH_E45_REQUIRED_SENTENCES) <= set(known)
    for name in dir(source_repairs):
        value = getattr(source_repairs, name)
        texts = [value] if isinstance(value, str) else list(value) if isinstance(value, (tuple, dict)) else []
        assert not any(isinstance(text, str) and re.search(r"[぀-ヿ一-鿿]", text) for text in texts), name


def test_sentences_are_recognised_whatever_the_line_layout_and_punctuation_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_rule_at_invented_sentences(monkeypatch)
    repaired, result = _repair(
        tmp_path,
        "1\n00:00:05,000 --> 00:00:07,000\n（看板）\n\n"
        "2\n00:00:33,082 --> 00:00:38,053\n明かりはもう十分だ!  鐘が鳴るまで待て!\n\n"
        "3\n00:02:27,162 --> 00:02:29,665\n窓を閉めて！\n",
    )
    assert result["applied"] is True and result["reason"] == "known_incomplete_source_repaired"
    assert parse_srt(repaired) == [
        (21.77, 27.36, "明かりはもう十分だ!"),
        (31.36, 33.45, "鐘が鳴るまで待て!"),
        (147.162, 149.665, "窓を閉めて！"),
    ]
    assert result["inserted_opening_cues"] == 2 and result["replaced_source_cues"] == 2


def test_every_repaired_cue_comes_from_the_users_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_rule_at_invented_sentences(monkeypatch)
    source_text = (
        f"1\n00:00:33,082 --> 00:00:35,985\n{FIRST}\n{SECOND}\n\n"
        f"2\n00:00:36,085 --> 00:00:38,053\n{THIRD}\n\n"
        "3\n00:02:27,162 --> 00:02:29,665\n窓を閉めて！\n"
    )
    repaired, result = _repair(tmp_path, source_text)
    assert result["applied"] is True
    texts = [text for _start, _end, text in parse_srt(repaired)]
    assert texts == [FIRST, SECOND, THIRD, "窓を閉めて！"]
    assert all(text in source_text for text in texts)


def test_a_source_without_both_known_sentences_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_rule_at_invented_sentences(monkeypatch)
    source_text = (
        f"1\n00:00:33,082 --> 00:00:35,985\n{FIRST}\n{SECOND}\n\n"
        "2\n00:00:36,085 --> 00:00:38,053\n鐘はもう鳴った！\n\n"
        "3\n00:02:27,162 --> 00:02:29,665\n窓を閉めて！\n"
    )
    repaired, result = _repair(tmp_path, source_text)
    assert repaired == tmp_path / "aligned.srt"
    assert result == {"applied": False, "reason": "source_signature_mismatch"}


def test_the_shipped_hashes_never_match_invented_text(tmp_path: Path) -> None:
    repaired, result = _repair(
        tmp_path,
        f"1\n00:00:33,082 --> 00:00:38,053\n{FIRST}\n{THIRD}\n\n2\n00:02:27,162 --> 00:02:29,665\n窓を閉めて！\n",
    )
    assert result == {"applied": False, "reason": "source_signature_mismatch"}
