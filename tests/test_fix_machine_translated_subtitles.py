"""Machine-translated Japanese subtitles are a last resort, not a timing winner."""

import logging
from pathlib import Path

import pytest

from pudge import cli
from pudge.models import SubtitleCandidate
from pudge.subtitle_formats import subtitle_name_is_machine_translated


@pytest.mark.parametrize(
    "name",
    [
        "[Group] Show - S01E11 [JPN] (AI Translated From English).ass",
        "Show 03 [MTL].srt",
        "Show 03 machine-translated.ja.srt",
        "Show 03 (translated from English).srt",
        "Show 03 [DeepL TL].ass",
        "Show 03 [ChatGPT translation].srt",
        "Show 03 AI翻訳.srt",
        "Show 03 機械翻訳.ass",
    ],
)
def test_machine_translated_names(name):
    assert subtitle_name_is_machine_translated(name)


@pytest.mark.parametrize(
    "name",
    [
        "Show.S01E11.WEBRip.Netflix.ja[cc].srt",
        "[Group] Show - 11 [720p][JPN].ass",
        "Show 03 [BDRip_1080P][X265_Main10p_Flac_Chap].srt",
        "Aishiteru Show - Ai no Uta 03.srt",
        "Show 03 [TV] (AT-X).srt",
    ],
)
def test_ordinary_names_are_not(name):
    assert not subtitle_name_is_machine_translated(name)


def _candidate(name: str) -> SubtitleCandidate:
    return SubtitleCandidate(path=Path("/x") / name, source="jimaku", score=1.0, name=name)


def test_real_files_win_over_a_machine_translation():
    machine = _candidate("[Group] Show - S01E11 [JPN] (AI Translated From English).ass")
    real = _candidate("Show.S01E11.WEBRip.Netflix.ja[cc].srt")
    kept = cli._drop_machine_translated([machine, real], logging.getLogger("t"), "v.mkv")
    assert kept == [real]


def test_a_machine_translation_alone_is_kept():
    machine = _candidate("[Group] Show - S01E11 [JPN] (AI Translated From English).ass")
    assert cli._drop_machine_translated([machine], logging.getLogger("t"), "v.mkv") == [machine]
