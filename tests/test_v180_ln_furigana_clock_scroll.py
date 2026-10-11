"""Listen together v1.80: a lingering reading expires for good, the sync trace explains the
reader's clock (behaviour; audit 71Б rewrite).

The reader runs in node (tests/page_harness.py) with the set-ups of tests/t71g19_support.py
and tests/t71g21_support.py. Covered elsewhere: the word that starts being spoken loses its
early (preview) reading class, test_v0723_ln_audio_weighted_highlight.py::
test_spoken_word_is_highlighted_and_finished_before_the_next; a backward poll sample does not
move a settled clock back, test_v0729_ln_audio_sync_responsive.py::
test_reader_clock_ignores_late_poll_answers_and_follows_real_jumps and
test_v0106_ln_optimistic_transport_clock.py; no frames are painted while the user scrolls and
the animation restarts after it, test_v178_ln_audio_state_clock_torrent.py::
test_manual_scroll_freezes_the_highlight_and_catches_up_after; the in-view check runs at most
every 250 ms, test_v175_ln_audio_opening_scroll_torrent_feedback.py::
test_following_the_audio_reuses_its_word_index_and_reads_layout_rarely; scrolling leaves
pictures alone, test_v141_ln_blur_scroll_open_alignment_perf.py."""

from __future__ import annotations

import pytest

from t71g19_support import LN_PAIRED, OPEN
from t71g21_support import READER, computed_style, paired
from t71g22_support import USER_SCROLL


def test_lingering_reading_expires_for_good_and_stops_being_the_early_one(page) -> None:
    # 明日 shows its reading early (it is next); if it is also left lingering, after 100 ms
    # its reading is gone and it no longer counts as the early-shown word, so the next
    # paint shows the early reading class again; once 明日 is spoken its reading returns.
    result = page.run(
        computed_style("display")
        + READER
        + r"""
        await openReader();
        const draw = (offset, preview) => renderLnPairedPosition({...pairedState(), playing: true, paused: false, position: 100 + offset}, offset,
                                                                  {speechActive: true, previewOffset: preview});
        const look = () => ({classes: words()[1].classes, reading: __css(wordEls()[1].querySelector('.ln-furigana-ruby'), 'display', 'after')});
        draw(1.0, 2.05);
        const out = {early: look()};
        lingerLnPairedFurigana(wordEls()[1]);
        out.lingering = look();
        await __tick(110);
        out.expired = look();
        draw(1.1, 2.1);
        out.repainted = look();
        draw(2.1, 2.2);
        out.spoken = look();
        return out;
        """,
        **paired(playing=False),
    )
    out = result.value
    assert out["early"] == {"classes": ["ln-paired-furigana-preview"], "reading": "block"}
    assert out["lingering"] == {
        "classes": ["ln-paired-furigana-linger", "ln-paired-furigana-preview"],
        "reading": "block",
    }
    assert out["expired"] == {"classes": ["ln-paired-furigana-expired"], "reading": "none"}
    assert out["repainted"] == {
        "classes": ["ln-paired-furigana-expired", "ln-paired-furigana-preview"],
        "reading": "none",
    }
    assert out["spoken"] == {"classes": ["ln-paired-word-current"], "reading": "block"}


def test_sync_trace_export_carries_clock_and_render_diagnostics(page) -> None:
    # ⌘⇧L saves the listen-together trace. Each paint records what the backend said (its
    # audio position and lookup time), how far the reader's own clock is from it, how
    # long the paint took and whether the user was scrolling, so a stutter report can be
    # read without guessing.
    result = page.run(
        OPEN
        + USER_SCROLL
        + r"""
        await __tick(600);
        const lagFrom = performance.now();
        AUDIO.lag = 0.3;                                       // from now on mpv's answers come 0.3 s late
        await __tick(1000);
        await userScroll(300);
        await __tick(400);
        await __key('l', {code: 'KeyL', metaKey: true, shiftKey: true}, 'body'); await __tick(10);
        return lagFrom;
        """,
        before=LN_PAIRED
        + "const plainState = window.pairedState; window.pairedState = () => ({...plainState(), position_lookup_ms: 1.25});",
    )
    [[payload]] = result.calls_of("light_novel_export_audio_sync_trace")
    assert all(
        isinstance(payload[key], str) and payload[key]
        for key in ("perf_revision", "clock_revision", "scroll_perf_revision")
    )
    rows = payload["events"]
    polls = [row for row in rows if row["event"] == "poll"]
    for row in polls:
        # The position painted, the backend's own position and the reader's lead over it.
        assert row["backend_audio_position"] == pytest.approx(
            row["audio_position"] - row["transport_lead_ms"] / 1000, abs=0.002
        )
        assert row["transport_drift_ms"] == -row["transport_lead_ms"]
        assert row["backend_lookup_ms"] == 1.25
    on_time = [row["transport_lead_ms"] for row in polls if row["monotonic_ms"] < result.value]
    late = [row["transport_lead_ms"] for row in polls if row["monotonic_ms"] > result.value + 300]
    assert on_time and late
    # The late answers do not pull the reader back: it keeps its clock, 0.3 s further ahead.
    assert min(late) - max(on_time) == pytest.approx(300, abs=30)
    painted = [row for row in rows if row["event"] in ("poll", "frame")]
    assert painted and all(
        isinstance(row["render_ms"], (int, float)) and row["manual_scroll"] is False for row in painted
    )
    deferred = [row for row in rows if row["event"] == "scroll_defer"]
    assert deferred and all(row["manual_scroll"] is True for row in deferred)
