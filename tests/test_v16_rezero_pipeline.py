from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pudge.config import SyncConfig
from pudge.manager import AnimeManager
from pudge.syncing import _optimize_subtitle_unguarded, raw_fallback_disqualified, subtitle_quality_accepted

ROOT=Path(__file__).resolve().parents[1]


def _diagnostic():
    return json.loads((ROOT/'tests/fixtures/rezero_ep19_unverified_timeline.json').read_text())


@pytest.mark.parametrize('verified',[False,True])
def test_direct_single_candidate_must_run_audio_verification(tmp_path,monkeypatch,verified):
    source=tmp_path/'AT-X episode19.srt'; reference=tmp_path/'embedded.srt'; output=tmp_path/'timeline.srt'
    for path in (source,reference,output):path.write_text('1\n00:00:40,000 --> 00:00:41,000\nhello\n')
    video=tmp_path/'SubsPlease-85.mkv';video.write_bytes(b'video')
    calls=[];speech=tmp_path/'speech.srt';speech.write_text(source.read_text())
    monkeypatch.setattr('pudge.syncing.extract_embedded_timing_reference',lambda *a,**k:(reference,{'language':'eng'}))
    monkeypatch.setattr('pudge.syncing._exact_release_zero_offset_result',lambda *a:None)
    monkeypatch.setattr('pudge.syncing.align_subtitle_timelines',lambda *a,**k:(output,_diagnostic()))
    monkeypatch.setattr('pudge.syncing._record_timeline_debug_attempt',lambda *a,**k:None)
    monkeypatch.setattr('pudge.syncing._strong_embedded_timeline_after_unsafe_stt',lambda *a:(False,{}))
    def verify(*a,**k):
        calls.append(a)
        return (speech if verified else None,{'reason':'verified' if verified else 'STT unavailable',
            'sync_was_successful':verified,'reference_alignment_reliable':verified,'engine':'japanese-stt'})
    monkeypatch.setattr('pudge.syncing._try_japanese_stt_fallback',verify)
    path,result=_optimize_subtitle_unguarded(video,source,tmp_path/'cache',SyncConfig(engine='alass',use_container_chapters=False))
    assert len(calls)==1 and calls[0][:2]==(video,source)
    if verified:
        assert path==speech and result['embedded_timeline_audio_verification']['accepted']
    else:
        assert path==source and result['reason']=='timeline_edit_requires_audio_verification'
        assert not subtitle_quality_accepted(result)[0]
        assert raw_fallback_disqualified(result)


def test_prepared_rezero_is_requeued_once_while_safe_episode_remains_ready(monkeypatch):
    unsafe=SimpleNamespace(video_path=Path('/tmp/rezero85.mkv'),subtitle_path=Path('/tmp/ready.srt'),media_id=189046,episode=19)
    safe=SimpleNamespace(video_path=Path('/tmp/safe.mkv'),subtitle_path=Path('/tmp/safe.srt'),media_id=1,episode=1)
    class DB:
        def __init__(self):self.state={};self.invalidated=[];self.history=10
        def episodes(self):return [unsafe,safe]
        def latest_selected_subtitle(self,video):
            return {'id':self.history,'details':{'alignment':_diagnostic() if video==unsafe.video_path else {'engine':'embedded-reference+timeline'}}}
        def get_state(self,key,default=''):return self.state.get(key,default)
        def set_state(self,key,value):self.state[key]=value
        def invalidate_subtitle(self,*args):self.invalidated.append(args)
    manager=AnimeManager.__new__(AnimeManager);manager.db=DB();manager.config=SimpleNamespace(sync=SyncConfig())
    monkeypatch.setattr('pudge.subtitles.stt.japanese_stt_available',lambda **_k:True)
    cache=[];monkeypatch.setattr('pudge.manager.invalidate_final_pipeline_result',lambda video,cfg:cache.append(video))
    assert manager._requeue_unverified_timeline_edits()==1
    assert cache==[unsafe.video_path] and manager.db.invalidated[0][0]==unsafe.video_path
    assert manager._requeue_unverified_timeline_edits()==0
    manager.db.history=11
    assert manager._requeue_unverified_timeline_edits()==1


@pytest.mark.parametrize('proof',[
    {'embedded_timeline_audio_verification':{'accepted':False}},
    {'speech_verification':{'reason':'STT unavailable','sync_was_successful':False}},
    {'embedded_opening_clock_scaffold':{'applied':False}},
])
def test_failed_audio_or_unapplied_scaffold_is_not_verification(proof):
    result={**_diagnostic(),**proof}
    assert not subtitle_quality_accepted(result)[0]
