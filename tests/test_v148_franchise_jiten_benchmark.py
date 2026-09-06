from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'pudge' / 'web' / 'index.html').read_text(encoding='utf-8')
SCRIPT = ROOT / 'scripts' / 'compare_subtitle_alignment_stt_policies.py'


def _load_policy_module():
    spec = importlib.util.spec_from_file_location('stt_policy_v148', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_franchise_hover_masonry_and_three_series_layout():
    assert '.ln-franchise-group:hover{' in HTML
    assert 'grid-auto-rows:8px' in HTML
    assert 'function layoutLnMasonry(' in HTML
    assert 'new ResizeObserver(schedule)' in HTML
    assert 'data-ln-franchise-count="${groups.length}"' in HTML
    assert '[data-ln-franchise-count="3"]{grid-column:span 3}' in HTML
    assert '[data-ln-franchise-count="3"] .ln-franchise-series{grid-template-columns:repeat(3,minmax(0,1fr))}' in HTML
    assert '[data-ln-franchise-count="4"] .ln-franchise-series{grid-template-columns:repeat(2,minmax(0,1fr))}' in HTML


def test_ordinary_audiobook_context_menu_unpairs_and_hides_markup():
    assert "const names=pairedKind==='ordinary'?'':`<button data-ln-context-action=\"export-speaker-markup\"" in HTML
    assert "const pairAction=pairedKind==='ordinary'?`<button data-ln-context-action=\"unpair-audiobook\"" in HTML
    assert "if(action==='unpair-audiobook'){await pywebview.api.light_novel_unlink_audiobook" in HTML


def test_jiten_apply_waits_for_scroll_idle_and_audio_offsets_are_incremental():
    assert 'function applyParsedLnChapterWhenIdle(' in HTML
    assert 'sinceScroll<180' in HTML
    assert 'ui.lnLastReaderScrollAt=performance.now()' in HTML
    assert 'audioLocal+=lnAudioTextLength(text.slice(pos,start))' in HTML
    assert 'audioStart=audioBase+audioLocal' in HTML
    assert 'lnAudioTextLength(text.slice(0,start))' not in HTML
    assert '[LN perf] jiten apply chapter=' in HTML


def test_policy_benchmark_has_case_timeout_checkpoint_and_raw_candidate_matrix():
    text = SCRIPT.read_text(encoding='utf-8')
    assert '--case-timeout-seconds' in text
    assert 'class _CaseTimeout' in text
    assert 'def _checkpoint(' in text
    assert 'case-frontier.partial.csv' in text
    assert 'raw_stt_eligible' in text
    assert 'production_gate_accepted' in text
    assert 'P2-P9 are experimental offline arbitration' in text


def test_raw_stt_eligibility_is_independent_of_production_transition_gate():
    m = _load_policy_module()
    probe = {
        'available': True,
        'accepted': False,
        'output': '/tmp/rejected-but-preserved.srt',
        'text_alignment': {'score': 0.73, 'coverage': 0.99},
        'activity': {'weighted': 0.8},
        'gate_failures': ['large_transition_without_real_gap'],
    }
    assert m._raw_stt_eligible(probe) is True
    current = {'accepted': False, 'result': {}}
    use, reason, facts = m._decision(2, current, probe)
    assert use is True
    assert reason == 'rejected_result_rescue'
    assert facts['production_gate_accepted'] is False
    assert facts['raw_stt_eligible'] is True


def test_raw_stt_eligibility_still_requires_semantic_minima():
    m = _load_policy_module()
    probe = {
        'available': True,
        'accepted': False,
        'output': '/tmp/weak.srt',
        'text_alignment': {'score': 0.40, 'coverage': 0.99},
        'activity': {'weighted': 0.8},
    }
    assert m._raw_stt_eligible(probe) is False
    use, reason, _ = m._decision(9, {'accepted': False, 'result': {}}, probe)
    assert use is False
    assert reason == 'stt_raw_candidate_not_eligible'
