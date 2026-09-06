from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge import reading_audio_alignment as raa
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import (
    _restore_embedded_opening_clock_scaffold,
    _tri_modal_sparse_cold_open_clock,
)


def _hyakkano_sparse_plateau() -> dict[str, object]:
    return {
        "applied": False,
        "reason": "stt_plateau_no_refinement",
        "pre": {
            "accepted": False,
            "reason": "no_clear_improvement",
            "shift_seconds": 0.0,
            "best_shift_seconds": 10.4,
            "baseline": {"matched": 0, "coverage": 0.0, "mean_error_seconds": None},
            "best": {"matched": 3, "coverage": 0.375, "mean_error_seconds": 0.198},
            "matched_gain": 3,
            "mean_error_gain_seconds": 0.0,
        },
        "pre_semantic": {
            "accepted": False,
            "reason": "too_few_semantic_anchors",
            "shift_seconds": 0.0,
            "best_shift_seconds": 9.366,
            "eligible_cues": 8,
            "anchor_count": 2,
            "coverage": 0.25,
            "anchors": [
                {
                    "cue_index": 6,
                    "shift_seconds": 10.367,
                    "similarity": 0.75,
                    "margin": 0.0,
                    "reference_start_seconds": 35.6,
                },
                {
                    "cue_index": 7,
                    "shift_seconds": 8.365,
                    "similarity": 0.75,
                    "margin": 0.0,
                    "reference_start_seconds": 35.6,
                },
            ],
        },
    }


def _hyakkano_embedded() -> dict[str, object]:
    return {
        "timeline_segments": [
            {
                "source_start": 0.0,
                "source_end": 80.0,
                "offset_seconds": 3.5,
                "support": 1,
                "mean_score": 2.4294,
                "mean_coverage": 0.8571,
                "kind": "stable",
            },
            {
                "source_start": 80.0,
                "source_end": None,
                "offset_seconds": 0.0,
                "support": 23,
                "mean_score": 3.0926,
                "mean_coverage": 0.8244,
                "kind": "stable",
            },
        ],
        "timeline_edge_hints_seconds": [7.988, -1.413],
        "timeline_cold_start": {
            "applied": False,
            "reason": "cold_start_overlaps_main_boundary",
            "base_offset_seconds": 3.5,
            "hint_offset_seconds": 7.988,
            "delta_seconds": 4.488,
            "cue_count": 8,
            "gap_seconds": 105.172,
            "boundary_source_time": 83.417,
        },
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["opening_gap_clock_ambiguity"],
            "early_window_count": 6,
            "early_offset_span_seconds": 3.5,
            "early_max_jump_seconds": 3.5,
            "cold_start_delta_seconds": 4.488,
            "cold_start_gap_seconds": 105.172,
            "cold_start_boundary_seconds": 83.417,
        },
    }


def test_sparse_cold_open_uses_three_weak_modalities_without_episode_override() -> None:
    result = _tri_modal_sparse_cold_open_clock(
        _hyakkano_sparse_plateau(),
        _hyakkano_embedded(),
        first_support=1,
        post_support=23,
        post_offset=0.0,
        speech_offset=0.208,
        cold_overlap_ambiguity=True,
    )
    assert result["accepted"] is True
    assert result["reason"] == "tri_modal_sparse_cold_open_consensus"
    # Robust median of onset=10.4, Japanese-text median=9.366 and exact-video
    # edge=7.988. This is inferred from signals, not a title/episode constant.
    assert abs(float(result["shift_seconds"]) - 9.366) < 0.001


def test_opening_scaffold_promotes_tri_modal_clock_over_coarse_single_window(tmp_path: Path) -> None:
    aligned = tmp_path / "speech-aligned.srt"
    early = [(10.0 + i * 3.0, 10.8 + i * 3.0, f"early-{i}") for i in range(8)]
    post = [(142.0 + i * 4.0, 142.8 + i * 4.0, f"post-{i}") for i in range(14)]
    write_srt([*early, *post], aligned)
    speech = {
        "offset_seconds": 0.208,
        "stt_opening_plateau_refinement": _hyakkano_sparse_plateau(),
    }
    output, diagnostics = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        speech,
        tmp_path / "cache",
    )
    assert output != aligned
    assert diagnostics["applied"] is True
    assert diagnostics["tri_modal_cold_open"]["accepted"] is True
    assert abs(float(diagnostics["target_relative_clock_seconds"]) - 9.366) < 0.001
    source_cues = parse_srt(aligned)
    output_cues = parse_srt(output)
    assert abs((output_cues[0][0] - source_cues[0][0]) - 9.366) < 0.01
    # Main/post-opening clock remains untouched.
    assert abs(output_cues[8][0] - source_cues[8][0]) < 0.001


def test_corrupt_orphan_clears_completed_intent_before_reselection(tmp_path: Path) -> None:
    video = tmp_path / "episode09.mkv"
    video.write_bytes(b"\0" * 4096)
    item = DownloadItem(
        torrent_hash="bad-hash",
        name=video.name,
        state="complete",
        progress=1.0,
        save_path=str(tmp_path),
        content_path=str(video),
        media_id=187260,
        episode=9,
        media_episode=9,
        release_episode=9,
        raw={"backend": "aria2", "orphaned_metadata": True},
    )

    class DB:
        def downloads(self):
            return [item]
        def delete_subtitle_job(self, _path): pass
        def delete_episode_record(self, _path): pass
        def delete_torrent_records(self, _hash): pass

    class Intent:
        def __init__(self): self.events = []
        def clear(self, media_id, episode, batch): self.events.append(("clear", media_id, episode, batch))
        def update(self, *args, **kwargs): self.events.append(("update", args, kwargs))

    class Client:
        def delete(self, *_args, **_kwargs): pass
        def close(self): pass

    manager = object.__new__(AnimeManager)
    manager.db = DB()
    manager.download_intents = Intent()
    manager.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    manager.qbt_client = lambda: Client()
    manager.downloads_enabled = lambda: True
    calls = []
    def search(media_id, *, episode, batch, automatic=False, **_kwargs):
        assert manager.download_intents.events[0] == ("clear", 187260, 9, False)
        calls.append((media_id, episode, batch, automatic))
        return SimpleNamespace(title="replacement")
    manager.search_and_add_best = search

    assert manager._repair_unreadable_orphaned_download(
        video,
        media_id=187260,
        episode=9,
        attempts=107,
        probe_error="EBML header parsing failed",
    )
    assert calls == [(187260, 9, False, True)]


def test_corrupt_orphan_becomes_waiting_when_torrent_traffic_is_off(tmp_path: Path) -> None:
    video = tmp_path / "episode09.mkv"
    video.write_bytes(b"\0" * 4096)
    item = DownloadItem(
        torrent_hash="bad-hash",
        name=video.name,
        state="complete",
        progress=1.0,
        save_path=str(tmp_path),
        content_path=str(video),
        media_id=187260,
        episode=9,
        media_episode=9,
        release_episode=9,
        raw={"backend": "aria2", "orphaned_metadata": True},
    )
    class DB:
        def downloads(self): return [item]
        def delete_subtitle_job(self, _path): pass
        def delete_episode_record(self, _path): pass
        def delete_torrent_records(self, _hash): pass
    class Intent:
        def __init__(self): self.events=[]
        def clear(self,*args): self.events.append(("clear",args))
        def update(self,*args,**kwargs): self.events.append(("update",args,kwargs))
    class Client:
        def delete(self,*_args,**_kwargs): pass
        def close(self): pass
    manager=object.__new__(AnimeManager)
    manager.db=DB(); manager.download_intents=Intent()
    manager.logger=SimpleNamespace(info=lambda *a,**k:None, warning=lambda *a,**k:None)
    manager.qbt_client=lambda: Client(); manager.downloads_enabled=lambda: False
    manager.search_and_add_best=lambda *_a,**_k: (_ for _ in ()).throw(AssertionError("must not add while off"))
    assert manager._repair_unreadable_orphaned_download(
        video, media_id=187260, episode=9, attempts=3,
        probe_error="Invalid data found when processing input",
    )
    assert manager.download_intents.events[0][0] == "clear"
    assert manager.download_intents.events[1][0] == "update"
    assert manager.download_intents.events[1][2]["state"] == "waiting"


def test_watched_deleted_zero_byte_aria2_shell_is_removed_from_downloads(tmp_path: Path) -> None:
    item = DownloadItem(
        torrent_hash="ghost",
        name="watched-e08.mkv",
        state="paused",
        progress=0.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "watched-e08.mkv"),
        media_id=177699,
        episode=8,
        media_episode=8,
        release_episode=8,
        is_batch=False,
        raw={"backend": "aria2", "total_size": 0, "downloaded": 0},
    )
    anime = LibraryAnime(media_id=177699, title="Ghost", progress=9, episodes=10, format="TV")
    class DB:
        def __init__(self): self.deleted=[]
        def get_anime(self, media_id): return anime if media_id == 177699 else None
        def delete_torrent_records(self, torrent_hash): self.deleted.append(torrent_hash)
    class Intent:
        def __init__(self): self.cleared=[]
        def clear(self,*args): self.cleared.append(args)
    class Client:
        def __init__(self): self.deleted=[]
        def delete(self, torrent_hash, *, delete_files=True): self.deleted.append((torrent_hash, delete_files))
    manager=object.__new__(AnimeManager)
    manager.db=DB(); manager.download_intents=Intent()
    manager.logger=SimpleNamespace(info=lambda *a,**k:None, warning=lambda *a,**k:None)
    client=Client()
    assert manager._discard_watched_empty_aria2_shells(client, [item]) == []
    assert client.deleted == [("ghost", False)]
    assert manager.db.deleted == ["ghost"]
    assert manager.download_intents.cleared == [(177699, 8, False)]


def test_unwatched_zero_byte_shell_is_preserved(tmp_path: Path) -> None:
    item = DownloadItem(
        torrent_hash="still-needed",
        name="e09.mkv",
        state="paused",
        progress=0.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "e09.mkv"),
        media_id=1,
        episode=9,
        media_episode=9,
        release_episode=9,
        raw={"backend": "aria2", "total_size": 0, "downloaded": 0},
    )
    class DB:
        def get_anime(self, _mid): return LibraryAnime(media_id=1,title="A",progress=8,episodes=12,format="TV")
        def delete_torrent_records(self, _hash): raise AssertionError("must keep")
    manager=object.__new__(AnimeManager); manager.db=DB(); manager.download_intents=SimpleNamespace(clear=lambda *a:None)
    manager.logger=SimpleNamespace(info=lambda *a,**k:None, warning=lambda *a,**k:None)
    assert manager._discard_watched_empty_aria2_shells(SimpleNamespace(delete=lambda *a,**k:None), [item]) == [item]


def test_post_precision_bridge_uses_reading_mora_instead_of_raw_characters() -> None:
    chapter = {
        "_runtime_reading_hints": [
            {"offset_start": 142, "offset_end": 143, "reading": "を"},
            {"offset_start": 143, "offset_end": 145, "reading": "とおり"},
            {"offset_start": 145, "offset_end": 148, "reading": "つうこうしょう"},
            {"offset_start": 148, "offset_end": 149, "reading": "を"},
            {"offset_start": 149, "offset_end": 153, "reading": "もらって"},
        ]
    }
    anchors = [
        {"offset": 142, "time": 14089.015},
        {"offset": 154.999, "time": 14090.360},
        {"offset": 154.999, "time": 14090.899},
        {"offset": 155, "time": 14090.900},
    ]
    bridged, count, debug = raa._runtime_reading_weighted_bridge(
        chapter,
        anchors,
        {"verified_through_offset": 142},
    )
    assert count >= 3
    assert debug and debug["mode"] == "reading_weighted_post_precision_bridge"
    at_148 = next(float(row["time"]) for row in bridged if abs(float(row["offset"]) - 148.0) < 1e-6)
    raw_linear = 14089.015 + (14090.360 - 14089.015) * ((148 - 142) / (154.999 - 142))
    assert at_148 > raw_linear + 0.10
    assert all(float(b["time"]) >= float(a["time"]) for a,b in zip(bridged, bridged[1:]))


def test_fallback_ln_word_uses_shared_study_card_singleton_contract() -> None:
    index = Path("pudge/web/index.html").read_text(encoding="utf-8")
    tools = Path("pudge/web/reading_tools.js").read_text(encoding="utf-8")
    shared = "if(globalThis.PudgeReadingTools?.study?.open)"
    fallback = "if(token.fallback){const pop=$('lnStudyPop')"
    assert index.index(shared) < index.index(fallback)
    assert "const fallbackOnly = Boolean(token.fallback);" in tools
    assert "if (fallbackOnly) return;" in tools
    # Same header gives Play/Read actions, green listen tone and the normal close.
    assert 'data-pudge-study-action-tone="${String(action.tone || \'\') === \'listen\'' in tools
    assert "data-pudge-study-close" in tools
    # No deck/Jiten API request is made for fallback-only cards.
    assert tools.index("if (fallbackOnly) return;") < tools.index("const decks = await apiDecks")


def test_existing_stale_complete_intent_without_file_is_restarted() -> None:
    anime = LibraryAnime(
        media_id=187260,
        title="Current show",
        progress=8,
        episodes=13,
        format="TV",
        status="CURRENT",
        media_status="RELEASING",
        next_airing_episode=10,
        next_airing_at=9999999999,
    )
    class DB:
        def anime_list(self, statuses=None): return [anime]
        def has_episode(self, media_id, episode): return False
    class Intent:
        def __init__(self): self.payload={"state":"complete","backend":"aria2"}; self.events=[]
        def get(self,*_args): return dict(self.payload) if self.payload else None
        def clear(self,*args): self.events.append(("clear",args)); self.payload=None
        def update(self,*args,**kwargs): self.events.append(("update",args,kwargs))
    manager=object.__new__(AnimeManager)
    manager.db=DB(); manager.download_intents=Intent()
    manager.downloads_enabled=lambda: True
    manager.logger=SimpleNamespace(info=lambda *a,**k:None, warning=lambda *a,**k:None)
    calls=[]
    manager.search_and_add_best=lambda media_id, **kwargs: (calls.append((media_id,kwargs)) or SimpleNamespace(title="fresh"))
    assert manager._repair_stale_completed_current_download_intents([]) == 1
    assert manager.download_intents.events[0][0] == "clear"
    assert calls == [(187260, {"episode":9,"batch":False,"automatic":True})]


def test_stale_complete_intent_with_active_torrent_is_not_restarted() -> None:
    anime = LibraryAnime(
        media_id=1,title="Current",progress=8,episodes=12,format="TV",status="CURRENT",
        media_status="RELEASING",next_airing_episode=10,next_airing_at=9999999999,
    )
    active=DownloadItem(
        torrent_hash="active",name="e09.mkv",state="downloading",progress=.2,
        save_path="/tmp",content_path="/tmp/e09.mkv",media_id=1,episode=9,media_episode=9,
        release_episode=9,raw={"backend":"aria2","total_size":100,"downloaded":20},
    )
    class DB:
        def anime_list(self,statuses=None): return [anime]
        def has_episode(self,*_args): return False
    class Intent:
        def get(self,*_args): return {"state":"complete","backend":"aria2"}
        def clear(self,*_args): raise AssertionError("active download must win")
    manager=object.__new__(AnimeManager); manager.db=DB(); manager.download_intents=Intent()
    manager.downloads_enabled=lambda: True
    manager.logger=SimpleNamespace(info=lambda *a,**k:None, warning=lambda *a,**k:None)
    manager.search_and_add_best=lambda *_a,**_k: (_ for _ in ()).throw(AssertionError("no restart"))
    assert manager._repair_stale_completed_current_download_intents([active]) == 0
