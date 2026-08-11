from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig


ROOT = Path(__file__).parents[1]


def test_audio_analysis_copy_is_compact_and_has_no_background_label() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    card_status = media[media.index("const audioPreparation"):media.index("const nextPaint")]
    reader_status = html[
        html.index("function lnPairedPreparationText"):html.index("function syncLnPairedTray")
    ]

    assert "Audio analysis" in card_status
    assert "Background audio analysis" not in card_status
    assert "remaining_audio_seconds" not in card_status
    assert "elapsed_seconds" not in card_status
    assert "Audio analysis'} · ${percent}%" in reader_status
    assert "remaining_audio_seconds" not in reader_status
    assert "background" not in reader_status.casefold()


def test_jimaku_account_link_and_automatic_anilist_step_are_current() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "https://jimaku.cc/account" in html
    assert "https://jimaku.cc/account" in readme
    assert "https://jimaku.cc/profile" not in html
    assert "https://jimaku.cc/api/docs" not in html
    assert "AniList data updates automatically" in html
    assert "updates AniList data automatically" in readme


def test_transcription_runs_use_isolated_work_directories() -> None:
    source = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")
    worker = source[source.index("def _transcribe_worker"):source.index("def prepare_transcription")]

    assert "tempfile.mkdtemp" in worker
    assert 'prefix=f".{output.stem}-work-"' in worker
    assert 'temporary = work_dir / f"{output.name}.tmp"' in worker
    assert 'output.parent / f".{output.stem}-work"' not in worker


def test_long_countdowns_hide_hours_and_playback_defaults_live_in_advanced() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    settings = (ROOT / "pudge/web/settings.js").read_text(encoding="utf-8")
    config = AppConfig()

    assert "return d?`${d}${t('remaining.day')}`" in html
    assert "return d?`${d}${t('remaining.day')} ${h}${t('remaining.hour')}`" not in html
    assert "if (has('s_playback_enabled')) return 'advanced';" in settings
    assert "playback: 'Playback'" not in settings
    assert config.playback.enabled is True
    assert config.playback.rewind_seconds == 15.0
