from __future__ import annotations
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from pudge.config import AppConfig, write_config, load_config
from pudge.content_review import ContentReviewService
from pudge.database import Database
from pudge.audiobooks import AudiobookService
from pudge.power_policy import PowerPolicy
from pudge.review_gate import ContentReviewIdentity, ReviewGateStore
from pudge.web_app import WebAppApi
from pudge.mpv_assistant import llm_configured


def test_new_review_and_explain_settings_roundtrip(tmp_path):
    cfg=AppConfig();cfg.ui.review_gate_ln_enabled=True;cfg.ui.review_gate_manga_all_due=True
    cfg.ui.review_gate_manga_count=13;cfg.shortcuts.mpv_explain_subtitle='Ctrl+Shift+t'
    path=tmp_path/'config.toml';write_config(cfg,path);actual=load_config(path)
    assert actual.ui.review_gate_ln_enabled and actual.ui.review_gate_manga_all_due
    assert actual.ui.review_gate_manga_count==13
    assert actual.shortcuts.mpv_explain_subtitle=='Ctrl+Shift+t'
    assert not AppConfig().ui.review_gate_manga_enabled


def test_system_low_power_applies_on_ac_and_without_low_battery():
    run=lambda *a,**k:SimpleNamespace(stdout="Now drawing from 'AC Power'\n100%; charged")
    policy=PowerPolicy(platform='darwin',run_command=run,native_probe=lambda:(True,False))
    state=policy.snapshot();assert state.mode=='energy_saving' and state.on_battery is False
    assert 'system_low_power' in state.reasons


def test_poll_returns_while_single_expensive_worker_is_blocked():
    api=object.__new__(WebAppApi);api._download_poll_lock=threading.Lock()
    api.manager=SimpleNamespace(db=SimpleNamespace(subtitle_job_poll_hint=lambda:{}))
    api.logger=SimpleNamespace(exception=lambda *a:None)
    api._foreground_poll_state_payload=lambda v:{'state':None}
    entered=threading.Event();release=threading.Event();calls=[]
    def worker(*args):
        calls.append(1);entered.set();release.wait(3);return {'stats':{'subs':1}}
    api._poll_downloads_and_subtitles_sync=worker
    start=time.monotonic();result=api.poll_downloads_and_subtitles('v1');elapsed=time.monotonic()-start
    assert entered.wait(.3) and result['running'] and elapsed<.2
    api.poll_downloads_and_subtitles('v1');assert len(calls)==1
    release.set();api._download_poll_worker.join(1)


def _gate(tmp_path, monkeypatch):
    cfg=AppConfig();cfg.ui.review_gate_ln_enabled=True;cfg.ui.review_gate_ln_all_due=True
    db=Database(tmp_path/'db.sqlite3');source=tmp_path/'book.txt';source.write_text('架空の文章')
    book={'id':1,'created_at':1,'file_path':str(source)}
    revision=['a'];removed=[False]
    service=SimpleNamespace(settings=lambda:SimpleNamespace(study_backend='jiten',jiten_api_key='synthetic-key'),
        strict_review_submit=lambda *a,**k:{'ok':True,'outcome':'saved'},
        strict_review_undo=lambda *a,**k:{'ok':True,'outcome':'undone'})
    gate=ContentReviewService(SimpleNamespace(config=cfg,manager=SimpleNamespace(db=db),light_novels=service))
    def source_fn(kind,id,part,prepare):
        if removed[0]:raise ValueError('removed')
        return ContentReviewIdentity(kind,'content-a',revision[0],part),'架空の文章'
    gate.source=lambda kind,id,part,*,prepare:source_fn(kind,id,part,prepare)
    monkeypatch.setattr('pudge.content_review.build_text_due_review_cards',lambda *a:{'cards':[
        {'wordId':3,'readingIndex':0},{'wordId':3,'readingIndex':1}], 'unknown_words':[]})
    return gate, revision, removed


def test_content_gate_readings_are_distinct_and_retry_is_idempotent(tmp_path,monkeypatch):
    gate,_,_= _gate(tmp_path,monkeypatch);status=gate.begin('ln',1,0)
    assert status['required']==2 and status['blocking']
    first=gate.review(status['token'],3,0,'good','attempt-a');assert first['completed']==1 and not first['granted']
    again=gate.review(status['token'],3,0,'good','attempt-a');assert again['completed']==1
    done=gate.review(status['token'],3,1,'good','attempt-b');assert done['granted']
    assert gate.begin('ln',1,0)['granted']
    assert gate.begin('ln',1,1)['blocking']


def test_content_change_and_delete_invalidate_authorized_cards(tmp_path,monkeypatch):
    gate,revision,removed=_gate(tmp_path,monkeypatch);status=gate.begin('ln',1,0)
    revision[0]='b'
    with pytest.raises(ValueError,match='changed'):gate.review(status['token'],3,0,'good','a')
    newer=gate.begin('ln',1,0);removed[0]=True
    with pytest.raises(ValueError,match='removed'):gate.review(newer['token'],3,0,'good','b')


@pytest.mark.parametrize('origin,consumer,paused',[('ln','ln',True),('audio','ln',False),('ln','sidebar',False),('ln','float',False)])
def test_audio_review_boundary_origin_and_consumer(origin,consumer,paused):
    service=object.__new__(AudiobookService);service._lock=threading.Lock();service.review_gate_enabled=lambda:True
    service._review_contexts={1:{'origin':origin,'consumer':consumer,'chapter':0,'pending_chapter':None,
                               'ranges':[{'chapter_index':1,'start':100}]}}
    calls=[];service.set_paused=lambda id,state:calls.append(('pause',state));service.seek_to=lambda id,pos:calls.append(('seek',pos))
    service._check_reader_review_boundary(1,100.25)
    assert bool(calls)==paused
    if paused:assert calls==[('pause',True),('seek',100)]
    service.set_review_consumer(1,'sidebar')
    assert service._review_contexts[1]['pending_chapter'] is None


def test_assistant_configuration_handles_local_and_hosted():
    cfg=AppConfig();assert not llm_configured(cfg)
    cfg.llm.enabled=True;assert llm_configured(cfg)
    cfg.llm.provider='openai';cfg.llm.base_url='https://api.openai.com/v1'
    assert not llm_configured(cfg)
    cfg.llm.api_key='synthetic-key';assert llm_configured(cfg)


def test_playback_open_uses_current_schema_and_initializes_new_database(tmp_path):
    db=Database(tmp_path/'db.sqlite3');fast=Database(db.path,initialize=False)
    assert fast.get_state('missing','ok')=='ok'
    new=Database(tmp_path/'new.sqlite3',initialize=False)
    assert new.get_state('missing','ok')=='ok'


def test_resume_fixed_quota_does_not_expand_when_due_queue_changes(tmp_path,monkeypatch):
    gate,_,_=_gate(tmp_path,monkeypatch)
    gate.api.config.ui.review_gate_ln_all_due=False;gate.api.config.ui.review_gate_ln_count=2
    initial=gate.begin('ln',1,0);gate.review(initial['token'],3,0,'good','a')
    monkeypatch.setattr('pudge.content_review.build_text_due_review_cards',lambda *a:{'cards':[
        {'wordId':3,'readingIndex':1},{'wordId':9,'readingIndex':0}], 'unknown_words':[]})
    resumed=ContentReviewService(gate.api);resumed.source=gate.source
    status=resumed.begin('ln',1,0)
    assert status['required']==2 and status['completed']==1
    assert status['cards']==[{'wordId':3,'readingIndex':1}]
    assert resumed.review(status['token'],3,1,'good','b')['granted']


def test_playback_retry_and_late_heartbeat_preserve_active_time(tmp_path):
    from pudge.playback_save import main
    from pudge.manager_models import LibraryAnime,LibraryEpisode
    cfg=AppConfig();cfg.library.database_path=tmp_path/'db.sqlite3'
    cfg.library.root_dir=tmp_path/'library';cfg.paths.cache_dir=tmp_path/'cache'
    path=tmp_path/'config.toml';write_config(cfg,path)
    video=tmp_path/'synthetic.mkv';video.write_bytes(b'synthetic')
    db=Database(cfg.library.database_path);db.upsert_anime(LibraryAnime(7,'Synthetic'))
    db.upsert_episode(LibraryEpisode(7,'Synthetic',1,video))
    def save(sequence,total,position):
        return main(['--config',str(path),'--playback-video',str(video),'--playback-position',str(position),
                     '--playback-duration','300','--playback-session','session-a','--playback-sequence',str(sequence),
                     '--playback-active-total',str(total)])
    assert save(1,10,10)==0 and save(1,10,10)==0
    assert save(3,30,30)==0 and save(2,20,20)==0
    assert db.playback_evidence(video)=={'position':30.0,'duration':300.0,'active_seconds':30.0}
    with db.connect() as conn:conn.execute('DELETE FROM episodes WHERE video_path=?',(str(video),))
    assert save(4,40,40)==0 and db.episode_by_path(video) is None


def test_ready_before_previous_playback_sets_cycle_baseline(tmp_path):
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryAnime,LibraryEpisode
    db=Database(tmp_path/'db.sqlite3');db.upsert_anime(LibraryAnime(7,'Synthetic'))
    videos=[]
    for n in (1,2):
        video=tmp_path/f'{n}.mkv';video.write_bytes(b'synthetic');videos.append(video)
        db.upsert_episode(LibraryEpisode(7,'Synthetic',n,video,embedded_subtitle_id=1,state='ready'))
    manager=object.__new__(AnimeManager);manager.db=db
    manager.remember_ready_before_playback(videos[0])
    assert db.get_state('ready_notification:episode:7:2')=='ready_before_previous_playback'
    db.delete_state('ready_notification:episode:7:2');videos[1].unlink()
    manager.remember_ready_before_playback(videos[0])
    assert not db.get_state('ready_notification:episode:7:2')


def test_jiten_url_and_title_search_do_not_need_anilist(tmp_path,monkeypatch):
    from pudge.light_novels import LightNovelService
    service=object.__new__(LightNovelService);calls=[]
    service._jiten_detail_with_subdecks=lambda id:({'title':'Synthetic','deckId':id},[])
    service._jiten_media_type=lambda *a:'novel'
    service._jiten_get=lambda endpoint,params:calls.append((endpoint,params)) or {'data':[{'deckId':95450,'title':'Synthetic'}]}
    assert service.search_jiten_books('https://jiten.moe/decks/media/95450/detail')[0]['deckId']==95450
    assert service.search_jiten_books('Mata, Onaji Yume wo Miteita')[0]['deckId']==95450
    assert calls[0][1]['titleFilter']=='Mata, Onaji Yume wo Miteita'


def test_select_hidden_disabled_and_reenabled_behavior():
    import subprocess
    subprocess.run(['node',str(Path(__file__).parent/'js/select_visibility.cjs')],check=True)


def test_scheduler_checks_database_before_constructing_manager(tmp_path):
    from pudge.agent import scheduled_work_due
    from pudge.manager_models import LibraryAnime
    cfg=AppConfig();cfg.library.database_path=tmp_path/'db.sqlite3';cfg.anilist.enabled=False
    db=Database(cfg.library.database_path);db.set_state('agent_last_run','1000')
    assert not scheduled_work_due(cfg,now=1001)
    assert scheduled_work_due(cfg,now=10000)
    db.upsert_anime(LibraryAnime(7,'Synthetic',status='PLANNING'))
    assert scheduled_work_due(cfg,now=1001)


def test_subtitle_assistant_owns_playback_and_freezes_context(tmp_path):
    from pudge.manager_models import LibraryAnime,LibraryEpisode
    cfg=AppConfig();cfg.llm.enabled=True
    db=Database(tmp_path/'db.sqlite3');db.upsert_anime(LibraryAnime(7,'Synthetic anime'))
    video=tmp_path/'video.mkv';video.write_bytes(b'synthetic');db.upsert_episode(LibraryEpisode(7,'Synthetic anime',2,video))
    alive=[True];api=object.__new__(WebAppApi);api.config=cfg;api.manager=SimpleNamespace(db=db)
    api._play_lock=threading.Lock();api._play_processes={str(video):SimpleNamespace(poll=lambda:None if alive[0] else 0)}
    api._mpv_assistant_window=SimpleNamespace(show=lambda:{'ok':True});api._play_key=lambda value:str(Path(value).resolve())
    assert api.mpv_explain_subtitle({'video':str(video),'text':'架空の字幕','context':'previous lines','position':12.5})['ok']
    snapshot=api.mpv_assistant_snapshot()['snapshot']
    assert snapshot['text']=='架空の字幕' and snapshot['metadata']['episode']==2
    assert 'previous lines' in snapshot['context'] and 'Synthetic anime' in snapshot['context']
    alive[0]=False
    assert not api.mpv_explain_subtitle({'video':str(video),'text':'次'})['ok']
    assert api.mpv_assistant_snapshot()['snapshot']==snapshot


def test_llm_protocol_profiles_roundtrip_and_backup_redaction(tmp_path):
    from pudge.backup import _redact_config
    cfg=AppConfig();cfg.llm.provider='openai';cfg.llm.base_url='https://gateway.invalid/v1';cfg.llm.model='synthetic'
    cfg.llm.api_key='active-test-secret';cfg.llm.profiles={'ollama':{'url':'http://127.0.0.1:11434','model':'local-model','api_key':'local-test-secret'}}
    path=tmp_path/'profiles.toml';write_config(cfg,path);loaded=load_config(path)
    assert loaded.llm.profiles['ollama']['model']=='local-model'
    assert loaded.llm.profiles['ollama']['api_key']=='local-test-secret'
    assert loaded.llm.profiles['openai']['api_key']==loaded.llm.api_key=='active-test-secret'
    redacted,_=_redact_config(path.read_text())
    assert 'active-test-secret' not in redacted and 'local-test-secret' not in redacted


def test_manga_review_scans_only_the_target_twenty_pages(tmp_path):
    source=tmp_path/'synthetic.cbz';source.write_bytes(b'synthetic')
    pages=[];manga=SimpleNamespace(_book=lambda id:{'id':id,'path':str(source),'created_at':1,'page_count':53,'source_fingerprint':'a'},
        text_regions=lambda id,page,cached_only:pages.append((page,cached_only)) or {'available':True,'regions':[{'text':f'架空{page}'}]})
    gate=ContentReviewService(SimpleNamespace(manager=SimpleNamespace(db=Database(tmp_path/'db.sqlite3')),manga=manga))
    identity,text=gate.source('manga',1,29,prepare=True)
    assert identity.part==20 and len(text.splitlines())==20
    assert pages==[(page,False) for page in range(20,40)]


def test_owner_save_and_fallback_share_one_retry_marker(tmp_path):
    from pudge.playback_save import save_playback
    from pudge.consumption import ConsumptionLedger
    from pudge.manager_models import LibraryAnime,LibraryEpisode
    db=Database(tmp_path/'db.sqlite3');db.upsert_anime(LibraryAnime(7,'Synthetic'))
    video=tmp_path/'video.mkv';video.write_bytes(b'synthetic');db.upsert_episode(LibraryEpisode(7,'Synthetic',1,video))
    api=object.__new__(WebAppApi);api.manager=SimpleNamespace(db=db);api.consumption=ConsumptionLedger(db)
    api._play_lock=threading.Lock();api._play_key=lambda p:str(Path(p).resolve())
    api._play_processes={str(video):SimpleNamespace(poll=lambda:None)}
    payload={'video':str(video),'position':20,'duration':300,'session':'synthetic','sequence':1,'active_total':20}
    assert api.mpv_playback_save(payload)['ok']
    # Simulate a committed owner save whose HTTP response was lost.
    assert save_playback(db,video,position=20,duration=300,session='synthetic',sequence=1,active_total=20)==0
    assert db.playback_evidence(video)['active_seconds']==20
    api._closing=True;assert not api.mpv_playback_save(payload)['ok']


def test_alignment_finishing_during_playback_gates_future_chapters_only():
    service=object.__new__(AudiobookService);service._lock=threading.Lock();service.review_gate_enabled=lambda:True
    service._review_contexts={4:{'ln_book_id':7,'origin':'ln','consumer':'ln','chapter':0,'pending_chapter':None,'ranges':[]}}
    service._last_positions={4:150}
    alignment={'chapters':[{'chapter_index':i,'start':i*100,'end':(i+1)*100} for i in range(3)]}
    with service._lock:service._refresh_reader_review_alignment_locked(7,4,300,alignment)
    assert service._review_contexts[4]['chapter']==1
    calls=[];service.set_paused=lambda *a:calls.append(a);service.seek_to=lambda *a:None
    service._check_reader_review_boundary(4,151);assert not calls
    service._check_reader_review_boundary(4,201);assert calls==[(4,True)]


def test_review_mutation_cannot_switch_accounts_after_authorization():
    from pudge.light_novels import LightNovelService,LightNovelError
    service=object.__new__(LightNovelService);service.settings=lambda:SimpleNamespace(jiten_api_key='changed-key',study_backend='jiten')
    with pytest.raises(LightNovelError,match='account changed'):
        service.strict_review_submit(3,0,'good',attempt_id='a',expected_account_key='old-account')
    with pytest.raises(LightNovelError,match='account changed'):
        service.strict_review_undo(3,0,expected_account_key='old-account')
