from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
COVER_PREVIEW = ROOT / "pudge" / "web" / "cover_preview.js"
POLICY_SCRIPT = ROOT / "scripts" / "compare_subtitle_alignment_stt_policies.py"
AB_SCRIPT = ROOT / "scripts" / "compare_subtitle_alignment.py"


def _load_ab():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("pudge_alignment_ab_v146", AB_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reader_uses_native_scroll_and_cover_preview_waits_for_real_drag() -> None:
    html = HTML.read_text(encoding="utf-8")
    preview = COVER_PREVIEW.read_text(encoding="utf-8")
    assert "lnSmoothCoarseWheel" not in html
    assert "addEventListener('wheel',()=>" in html
    assert "{passive:true}" in html
    assert "peek:null" in preview
    assert "if(!drag.peek)drag.peek=makePeek" in preview


def test_anime_and_markup_ui_regressions_are_present() -> None:
    html = HTML.read_text(encoding="utf-8")
    anime_css = html.split(".anime-card {", 1)[1].split("}", 1)[0]
    assert "content-visibility" not in anime_css
    assert "contain:" not in anime_css
    assert "const names=pairedKind==='ordinary'?'':`<button" in html
    assert 'data-context-action="toggle-jp-subs"' in html
    assert "JP×" in html
    assert "japanese_subtitles_required===false" in html


def test_torrent_pack_plan_groups_exact_file_ids_by_volume() -> None:
    from pudge.audiobooks import audiobook_torrent_pack_plan

    result = audiobook_torrent_pack_plan([
        {"index": 4, "name": "Spice and Wolf Vol 01.m4b", "size": 100},
        {"index": 7, "name": "Spice and Wolf Vol 02.m4b", "size": 200},
        {"index": 8, "name": "cover.jpg", "size": 5},
    ])
    assert result["selective"] is True
    assert [row["volume"] for row in result["volumes"]] == [1, 2]
    assert [row["file_ids"] for row in result["volumes"]] == [[4], [7]]
    archive = audiobook_torrent_pack_plan([{"index": 3, "name": "all-volumes.7z", "size": 999}])
    assert archive["selective"] is False
    assert archive["reason"] == "single_archive"
    assert archive["archive_file_ids"] == [3]


def test_anilist_romaji_reading_is_canonical_for_ayanokouji() -> None:
    from pudge.light_novels import _romaji_to_hiragana

    assert _romaji_to_hiragana("Ayanokouji") == "あやのこうじ"
    assert _romaji_to_hiragana("Kiyotaka") == "きよたか"


def test_irodori_voice_seed_is_stable_per_character_and_caption() -> None:
    from pudge.web_app import WebAppApi

    a = WebAppApi._irodori_voice_seed("Ayanokouji Kiyotaka", "calm young male")
    b = WebAppApi._irodori_voice_seed("  AYANOKOUJI   KIYOTAKA ", "calm young male")
    c = WebAppApi._irodori_voice_seed("Ayanokouji Kiyotaka", "older male")
    assert a == b
    assert 1 <= a < 2_147_483_647
    assert c != a


def test_per_anime_subtitle_requirement_is_reversible_and_does_not_delete_subtitles(tmp_path: Path) -> None:
    from pudge.manager import AnimeManager

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "episode.srt"
    subtitle.write_text("kept", encoding="utf-8")
    episode = SimpleNamespace(
        video_path=video, subtitle_path=subtitle, media_id=42, episode=1, state="waiting_subtitles"
    )

    class DB:
        def __init__(self):
            self.state: dict[str, str] = {}
            self.deleted_jobs: list[Path] = []
            self.deleted_states: list[str] = []
            self.queued: list[Path] = []

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

        def delete_state(self, key):
            self.deleted_states.append(str(key))
            self.state.pop(key, None)

        def episodes(self, media_id=None):
            return [episode] if media_id in (None, 42) else []

        def delete_subtitle_job(self, path):
            self.deleted_jobs.append(Path(path))

        def ensure_subtitle_job(self, path, media_id, ep):
            self.queued.append(Path(path))

    manager = object.__new__(AnimeManager)
    manager.db = DB()
    manager.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    assert manager.japanese_subtitles_required(42) is True
    assert manager.set_japanese_subtitles_required(42, False) is False
    assert manager.japanese_subtitles_required(42) is False
    assert manager.db.deleted_jobs == [video]
    assert subtitle.read_text(encoding="utf-8") == "kept"
    assert manager.set_japanese_subtitles_required(42, True) is True
    assert manager.japanese_subtitles_required(42) is True
    assert manager.db.queued == [video]


def test_web_payload_contains_top_level_japanese_subtitle_requirement() -> None:
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert '"japanese_subtitles_required": japanese_subtitles_required' in source
    assert '"japanese_subtitles_required": self.manager.japanese_subtitles_required(anime.media_id)' in source


def test_stt_audio_selection_prefers_japanese_and_refuses_ambiguous_dual_audio(monkeypatch, tmp_path: Path) -> None:
    from pudge.subtitles import stt

    video = tmp_path / "dual.mkv"
    video.write_bytes(b"video")

    def run_japanese(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [
                {"index": 1, "codec_name": "aac", "tags": {"language": "eng"}, "disposition": {"default": 1}},
                {"index": 2, "codec_name": "flac", "tags": {"language": "jpn", "title": "Japanese"}, "disposition": {"default": 0}},
            ]}),
            stderr="",
        )

    monkeypatch.setattr(stt.subprocess, "run", run_japanese)
    index, info = stt._select_japanese_audio_stream(video, "ffprobe")
    assert index == 2
    assert info["language"] == "jpn"
    assert info["selection_reason"] == "japanese_language_tag"

    def run_ambiguous(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [
                {"index": 1, "codec_name": "aac", "tags": {}, "disposition": {"default": 1}},
                {"index": 2, "codec_name": "aac", "tags": {}, "disposition": {"default": 0}},
            ]}),
            stderr="",
        )

    monkeypatch.setattr(stt.subprocess, "run", run_ambiguous)
    index, info = stt._select_japanese_audio_stream(video, "ffprobe")
    assert index is None
    assert info["selection_reason"] == "ambiguous_multiple_audio_streams"
    assert info["stream_count"] == 2


def test_stt_rejected_text_clock_is_preserved_with_explicit_gate_failures(monkeypatch, tmp_path: Path) -> None:
    import pudge.syncing as syncing

    source = tmp_path / "source.srt"
    reference = tmp_path / "reference.srt"
    lines = []
    for i in range(1, 7):
        start = i * 2
        lines.append(f"{i}\n00:00:{start:02d},000 --> 00:00:{start+1:02d},000\nこれは十分に長い日本語テキストです{i}\n")
    source.write_text("\n".join(lines), encoding="utf-8")
    reference.write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setattr(syncing, "align_light_novel_to_transcript", lambda *a, **k: {
        "alignment_method": "test", "matched_anchor_count": 6, "anchor_count": 6,
        "confidence": 0.9, "frontier": None, "frontier_reason": "not_applicable",
    })
    monkeypatch.setattr(syncing, "audio_position_for_light_novel_offset", lambda alignment, chapter, offset: float(offset) / 5.0)
    monkeypatch.setattr(syncing, "_stt_alass_transition_safety", lambda *a, **k: {
        "accepted": False, "reason": "large_transition_without_real_gap", "unsupported_transition_count": 2,
    })
    monkeypatch.setattr(syncing, "_subtitle_stt_text_score", lambda *a, **k: {
        "available": True, "score": 0.8, "coverage": 1.0,
    })
    monkeypatch.setattr(syncing, "compare_timing_activity", lambda *a, **k: {
        "available": True, "weighted": 0.8,
    })

    output, diagnostics = syncing._stt_text_clock_candidate(source, reference, tmp_path / "cache", model="tiny")
    assert output is None
    assert diagnostics["accepted"] is False
    assert diagnostics["reject_reason"] == "transition_safety:large_transition_without_real_gap"
    assert diagnostics["gate_failures"] == ["transition_safety:large_transition_without_real_gap"]
    rejected = Path(str(diagnostics["rejected_output"]))
    assert rejected.is_file()
    assert diagnostics["segment_count"] == 6
    assert diagnostics["anchor_count"] == 6
    assert "shift_spread_seconds" in diagnostics


def test_benchmark_hard_identity_gate_rejects_known_pollution_and_keeps_generic_filename() -> None:
    ab = _load_ab()

    ok, why = ab._benchmark_identity_check(
        {"title": "Boku no Hero Academia", "episode": 5, "media_id": 21459},
        Path("[Judas] Vigilantes - S01E05.mkv"),
        ["Boku no Hero Academia - 05.srt", "My Hero Academia - 05.srt"],
    )
    assert ok is False and why["reason"] == "title_identity_mismatch"

    ok, why = ab._benchmark_identity_check(
        {"title": "Another", "episode": 4, "media_id": 11111},
        Path("[SubsPlease] 16bit Sensation - Another Layer - 04 (1080p).mkv"),
    )
    assert ok is False and why["reason"] == "title_identity_mismatch"

    ok, why = ab._benchmark_identity_check(
        {"title": "BOFURI", "episode": 2, "media_id": 116867},
        Path("BOFURI S2E05 English Dub.mkv"),
        ["BOFURI S02E02.ja.srt"],
    )
    assert ok is False and why["reason"] == "episode_identity_mismatch"

    ok, why = ab._benchmark_identity_check(
        {"title": "Munou na Nana", "episode": 7, "media_id": 117343},
        Path("S01E07-Necromancer Part 2.mkv"),
    )
    assert ok is True and why["reason"] == "episode_identity_only"


def test_stt_policy_matrix_exports_forensic_fields_and_passes_ffprobe() -> None:
    source = POLICY_SCRIPT.read_text(encoding="utf-8")
    assert "ffprobe_path=config.tools.ffprobe" in source
    for field in (
        "stt_reject_reason", "stt_gate_failures", "stt_audio_stream_index",
        "video_content_fingerprint", "audio_sha256", "reference_sha256", "source_sha256",
        "benchmark-identity-rejections.json",
    ):
        assert field in source


def test_stt_cache_key_is_path_independent_for_same_content_identity() -> None:
    from pudge.subtitles.stt import _cache_key

    assert _cache_key("same-fingerprint", "tiny", 2) == _cache_key("same-fingerprint", "tiny", 2)
    assert _cache_key("same-fingerprint", "tiny", 2) != _cache_key("same-fingerprint", "tiny", 1)


def test_ln_store_title_keeps_local_volume_but_matches_base_anilist_work() -> None:
    from pudge.light_novels import LightNovelService, _series_key, _series_title, _volume_from_text

    title = "ようこそ実力至上主義の教室へ２<ようこそ実力至上主義の教室へ> (MF文庫J)"
    assert _volume_from_text(title) == 2
    assert _series_title(title) == "ようこそ実力至上主義の教室へ"
    assert LightNovelService._anilist_search_text(title) == "ようこそ実力至上主義の教室へ"
    assert LightNovelService._match_title(title) == "ようこそ実力至上主義の教室へ"
    assert _series_key(title) == _series_key("ようこそ実力至上主義の教室へ 第1巻")


def test_ln_franchise_layout_is_dense_label_free_and_selectable() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "grid-auto-flow:dense" in html
    assert 'data-ln-franchise-ids=' in html
    assert "toggleLnFranchiseSelection" in html
    assert '<div class="ln-franchise-head">' not in html


def test_anime_cards_avoid_webkit_paint_containment_and_subtitle_toggle_is_optimistic() -> None:
    html = HTML.read_text(encoding="utf-8")
    anime_css = html.split(".anime-card {", 1)[1].split("}", 1)[0]
    assert "contain:" not in anime_css
    assert "patchAnimeJapaneseSubtitlePolicyLocal" in html
    assert "ui-anime-scrolling" in html
    assert "noteAnimeListScroll" in html
    toggle_line = next(line for line in html.splitlines() if "if(action==='toggle-jp-subs')" in line)
    assert "ui.state=r.state" not in toggle_line
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    endpoint = source.split("def set_anime_japanese_subtitles_required", 1)[1].split("# pudge-v0.7.23-sidebar", 1)[0]
    assert "get_state_fast()" not in endpoint
    assert '"ui_state_version"' in endpoint


def test_manual_speaker_markup_accepts_character_owned_narration(tmp_path: Path) -> None:
    from pudge.web_app import WebAppApi

    units = [
        {"id": 0, "text": "地の文。", "dialogue": False},
        {"id": 1, "text": "「台詞」", "dialogue": True},
    ]
    path = tmp_path / "manual.json"
    path.write_text(
        json.dumps(
            {
                "assignments": [
                    {"id": 0, "speaker": "綾小路清隆", "caption": "低く落ち着いた声。"},
                    {"id": 1, "speaker": "堀北鈴音", "caption": "澄んだ声。"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = WebAppApi._load_audiobook_speaker_assignments(path, units)
    assert loaded[0]["speaker"] == "綾小路清隆"
    assert loaded[1]["speaker"] == "堀北鈴音"
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert "Also annotate a `narration` span when it is clearly voiced by a specific character" in source
    assert '"narration_ids": narration_ids' in source


def test_stt_policy_benchmark_supports_clean_ten_minute_partial_run() -> None:
    source = POLICY_SCRIPT.read_text(encoding="utf-8")
    assert '"--time-limit-minutes"' in source
    assert '"stopped_by_time_limit"' in source
    assert '"cases_discovered"' in source
