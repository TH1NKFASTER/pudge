from pathlib import Path

from pudge.config import load_config


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"


def test_hidden_policy_settings_are_fixed_even_for_old_configs(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[anilist]
watched_threshold = 0.2
watched_max_remaining_minutes = 99

[nyaa]
require_japanese_audio = false
avoid_upscaled = false
upgrade_min_score_gain = 1

[matching]
auto_upgrade_subtitles = false

[sync]
use_container_chapters = false
japanese_stt_fallback = false
""",
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.anilist.watched_threshold == 0.85
    assert cfg.anilist.watched_max_remaining_minutes == 10.0
    assert cfg.nyaa.require_japanese_audio is True
    assert cfg.nyaa.avoid_upscaled is True
    assert cfg.nyaa.upgrade_min_score_gain == 30.0
    assert cfg.matching.auto_upgrade_subtitles is True
    assert cfg.sync.use_container_chapters is True
    assert cfg.sync.japanese_stt_fallback is True


def test_simplified_settings_controls_are_not_rendered() -> None:
    source = HTML.read_text(encoding="utf-8")
    for element_id in (
        "s_anilist_threshold",
        "s_anilist_max_remaining",
        "s_use_container_chapters",
        "s_japanese_stt_fallback",
        "s_auto_upgrade_subtitles",
        "s_aria2_port",
        "s_upgrade_min_gain",
        "s_require_japanese",
        "s_avoid_upscaled",
    ):
        assert f'id="{element_id}"' not in source
        assert f"$('{element_id}')." not in source
    assert "shortcutDisplay(s.shortcut_mpv_mark_watched||'')" in source
    assert "Episode is marked watched once you've viewed at least 85% and 10 minutes or less remain" in source
    assert "просмотрено не меньше 85%" in source


def test_anilist_checkboxes_use_normal_vertical_stack() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert 'class="setting-stack"><label class="check"><input id="s_anilist_add_missing"' in source
    assert '<input id="s_relations_release" type="checkbox">' in source


def test_ln_progress_tooltip_uses_exact_character_counts() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "book.read_character_count" in source
    assert "progressTitle=characters?" in source
    assert 'data-tooltip="${escapeHtml(progressTitle)}"' in source


def test_listen_together_pauses_without_prepainting_next_word() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "speechActive=options.speechActive!==false" in source
    assert "if(!speechActive)" in source
    assert "word=previousWord" in source
    assert "word=null" in source
    assert "&&speechActive)" in source
    assert "until:performance.now()+34" not in source
    assert "ln-paired-word-finishing" not in source
    assert "renderLnPairedPosition(state,estimatedOffset,{speechActive:true,previewOffset})" in source
    assert "ln-paired-audio-listen" in source
    assert "ln-paired-audio-stop" in source


def test_manga_ocr_is_part_of_first_dependency_setup() -> None:
    html = HTML.read_text(encoding="utf-8")
    backend = WEB_APP.read_text(encoding="utf-8")
    assert "dependencyRow('MangaOCR',s.manga_ocr)" in html
    assert 'status["manga_ocr"] = manga_ocr' in backend
    assert "MangaOCR installation did not complete" in backend
