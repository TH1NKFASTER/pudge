'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  let activeWindow = null, selecting = false, pollTimer = null, pollGeneration = 0, renderRequestId = 0;
  let lastLineId = 0, lastCurrentKey = '', transcriptRows = [];
  const regionPresets={lower62:[0,0.38,1,0.62],lower50:[0,0.5,1,0.5],lower40:[0,0.6,1,0.4],full:[0,0,1,1]};
  const parsed = new Map();

  function shell() {
    const root = $('visualNovelsContent');
    if (!root) return null;
    if (!root.firstChild) root.innerHTML = `<div class="vn-shell"><div class="vn-head"><label>${PudgeI18n.t('vn.gameTitle')}<input id="vnGameTitle" type="text" placeholder="${esc(PudgeI18n.t('vn.visualNovel'))}"></label><label>${PudgeI18n.t('vn.dialogueArea')}<select id="vnRegion"><option value="lower62">${PudgeI18n.t('vn.lower62')}</option><option value="lower50">${PudgeI18n.t('vn.lower50')}</option><option value="lower40">${PudgeI18n.t('vn.lower40')}</option><option value="full">${PudgeI18n.t('vn.fullWindow')}</option></select></label><button id="vnStart" class="primary">${PudgeI18n.t('vn.chooseGameWindow')}</button><button id="vnStop" hidden>${PudgeI18n.t('vn.stop')}</button></div><div id="vnStatus" class="vn-status"></div><div id="vnGrid" class="vn-grid" hidden><section class="vn-live"><h3>${PudgeI18n.t('vn.currentLine')}</h3><div id="vnSpeaker" class="vn-speaker"></div><div id="vnCurrent" class="vn-current" data-pudge-study-hover data-pudge-translate-root></div></section><section class="vn-transcript"><h3>${PudgeI18n.t('vn.transcript')}</h3><div id="vnTranscript" class="vn-transcript-list"></div></section></div></div>`;
    return root;
  }

  function studyContextOptions(text,lineId){
    const index=transcriptRows.findIndex(row=>Number(row.id)===Number(lineId));
    if(index<0)return {backend:'jiten'};
    const previous=String(transcriptRows[index-1]?.text||''),next=String(transcriptRows[index+1]?.text||'');
    const contextText=[previous,String(text||''),next].filter(Boolean).join('\n');
    return {backend:'jiten',contextText,contextOffset:previous?previous.length+1:0};
  }

  async function renderCurrent(text,lineId,sessionGeneration=pollGeneration,frameId=0) {
    const root=$('vnCurrent'); if(!root)return;
    const requestId=++renderRequestId;
    if(sessionGeneration!==pollGeneration)return;
    if(!text){root.textContent='';return;}
    // A word card made from this line gets the game frame it was read from as its picture.
    const options={...studyContextOptions(text,lineId),mediaContext:{kind:'vn',frame_id:Number(frameId||0)}};
    if(parsed.has(lineId)){
      if(requestId===renderRequestId&&sessionGeneration===pollGeneration)root.innerHTML=window.PudgeReadingTools?.study?.renderParsedText(parsed.get(lineId),options)||esc(text);
      return;
    }
    root.textContent=text;
    try{
      const payload=await pywebview.api.visual_novel_parse(text);
      if(sessionGeneration!==pollGeneration||requestId!==renderRequestId)return;
      parsed.set(lineId,payload);
      const currentKey=`current:${lastCurrentKey}`;
      const stillCurrent=Number(lastLineId)===Number(lineId)||String(lineId)===currentKey;
      if(stillCurrent)root.innerHTML=window.PudgeReadingTools?.study?.renderParsedText(payload,options)||esc(text);
    }catch(error){console.debug?.('VN parse',error);}
  }

  async function applyRegionPreset(key){
    const region=regionPresets[String(key||'lower62')]||regionPresets.lower62;
    await pywebview.api.visual_novel_set_dialogue_region(...region);
  }

  function schedulePoll(generation) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer=setTimeout(()=>{
      pollTimer=null;
      void update(generation).catch(error=>{
        const status=$('vnStatus');
        if(status){status.textContent=PudgeI18n.message(String(error?.message||error));status.classList.add('error');}
      });
    },500);
  }

  function renderStatus(state){
    const status=$('vnStatus');if(!status)return;
    const errored=state.status==='error';
    const code=String(state.error_code||'');
    status.classList.toggle('error',errored);
    if(errored){
      const permission=code==='permission_required';
      const windowGone=code==='window_unavailable';
      const picker=state.capture_selection==='system_picker';
      const detail=permission?(picker?(PudgeI18n.t('vn.theWindowWasSelectedBut')):(PudgeI18n.t('vn.macosDeclinedCaptureOfThe'))):(state.detail?PudgeI18n.message(String(state.detail)):PudgeI18n.t('vn.captureFailed'));
      status.innerHTML=`${esc(detail)}${permission?` <button id="vnPermission">${PudgeI18n.t('vn.openScreenRecordingSettings')}</button>`:''}${permission||windowGone||code==='capture_timeout'?` <button id="vnPickAgain">${PudgeI18n.t('vn.chooseWindowWithMacos')}</button>`:''}`;
      return;
    }
    if(state.running){
      const captures=Number(state.capture_count||0),ocrCount=Number(state.ocr_count||0),emptyCount=Number(state.empty_ocr_count||0);
      const width=Number(state.last_frame_width||0),height=Number(state.last_frame_height||0),contrast=Number(state.last_frame_contrast||0);
      if(captures>0 && !state.current_text && !(state.transcript||[]).length){
        const frame=width&&height?`${width}×${height}`:'';
        const blankish=contrast>0&&contrast<1.5;
        const attempts=emptyCount?` ${PudgeI18n.tn('vn.emptyAttempts',emptyCount)}`:'';
        status.textContent=`${PudgeI18n.t('vn.noTextYet',{frame:frame?` (${frame})`:'',attempts})} ${PudgeI18n.t(blankish?'vn.frameBlank':'vn.retryingStaticFrame')} `;
      }else{
        status.textContent=PudgeI18n.t('vn.readingThroughScreencapturekitFramesOcr',{captures:captures,ocrCount:ocrCount});
      }
    }else{
      // Idle: how to start, with the configured Screen text shortcut (the picker explains itself).
      const settings=(typeof ui!=='undefined'?ui:window.ui)?.state?.settings||{};
      const shortcut=String(settings.shortcut_screen_text??'Meta+Alt+Shift+v').trim();
      if(shortcut){
        const keys=typeof shortcutDisplay==='function'?shortcutDisplay(shortcut):esc(shortcut);
        status.innerHTML=esc(PudgeI18n.t('vn.idleShortcutHint',{shortcut:'\u0000'})).replace('\u0000',`<span class="vn-shortcut">${keys}</span>`);
      }else{
        status.textContent=PudgeI18n.t('vn.idleNoShortcut');
      }
    }
  }

  async function update(generation=pollGeneration) {
    if(generation!==pollGeneration)return;
    const state=await pywebview.api.visual_novel_state();
    if(generation!==pollGeneration)return;
    activeWindow=state.window_id?{id:Number(state.window_id),title:state.window_title||'Visual Novel'}:activeWindow;
    $('vnStart').hidden=!!state.running; $('vnStop').hidden=!state.running;
    // A capture started by the Screen text shortcut names the game after its window.
    const titleInput=$('vnGameTitle');
    if(state.running&&titleInput&&state.window_title&&document.activeElement!==titleInput)titleInput.value=String(state.window_title);
    renderStatus(state);
    if($('vnSpeaker'))$('vnSpeaker').textContent=String(state.speaker_text||'');

    const rows=state.transcript||[];
    transcriptRows=rows;
    // Current line / Transcript only once a capture runs (and while this session has text).
    $('vnGrid').hidden=!(state.running||rows.length||state.current_text);
    $('vnTranscript').innerHTML=rows.slice().reverse().map(row=>`<button class="vn-line ${Number(row.id)===lastLineId?'active':''}" data-vn-line="${Number(row.id)}">${esc(row.text)}</button>`).join('');

    const newest=rows.at(-1);
    if(newest&&Number(newest.id)!==lastLineId){
      lastLineId=Number(newest.id);
      lastCurrentKey='';
      void renderCurrent(newest.text,lastLineId,generation,newest.frame_id);
    } else if(state.current_text) {
      const key=`${Number(state.generation||0)}:${Number(state.current_text_id||0)}:${String(state.current_text)}`;
      if(key!==lastCurrentKey){
        lastCurrentKey=key;
        void renderCurrent(state.current_text,`current:${key}`,generation,state.current_frame_id);
      }
    } else if(!newest && lastCurrentKey) {
      lastCurrentKey='';
      void renderCurrent('', 'current:empty',generation);
    }
    if(state.running)schedulePoll(generation);
  }

  async function start(){
    if(selecting)return;
    selecting=true;
    const button=$('vnStart');if(button)button.disabled=true;
    pollGeneration+=1;renderRequestId+=1;
    const generation=pollGeneration;
    if(pollTimer)clearTimeout(pollTimer);pollTimer=null;
    try{
      await applyRegionPreset($('vnRegion')?.value||'lower62');
      const gameTitle=String($('vnGameTitle')?.value||'').trim();
      const state=await pywebview.api.visual_novel_start(0,gameTitle,'',true);
      if(generation!==pollGeneration)return;
      if(!state.selection_cancelled){
        activeWindow={id:Number(state.window_id||0),title:String(state.window_title||'Visual Novel')};
        if($('vnGameTitle'))$('vnGameTitle').value=activeWindow.title;
        lastLineId=0;lastCurrentKey='';parsed.clear();
      }
      await update(generation);
    }finally{selecting=false;if(button)button.disabled=false;}
  }

  async function stop(){
    pollGeneration+=1;renderRequestId+=1;
    if(pollTimer)clearTimeout(pollTimer);pollTimer=null;
    await pywebview.api.visual_novel_stop();
    await update(pollGeneration);
  }

  document.addEventListener('change',event=>{if(event.target.id==='vnRegion')void applyRegionPreset(event.target.value).catch(error=>window.toast?.(String(error?.message||error)));});
  document.addEventListener('click',event=>{void (async()=>{if(event.target.id==='vnStart'||event.target.id==='vnPickAgain')await start();else if(event.target.id==='vnStop')await stop();else if(event.target.id==='vnPermission')await pywebview.api.open_screen_recording_settings();else{const line=event.target.closest?.('[data-vn-line]');if(line){const state=await pywebview.api.visual_novel_state();transcriptRows=state.transcript||[];const row=transcriptRows.find(item=>Number(item.id)===Number(line.dataset.vnLine));if(row){lastLineId=Number(row.id);lastCurrentKey='';void renderCurrent(row.text,lastLineId,pollGeneration,row.frame_id);}}}})().catch(error=>window.toast?.(String(error?.message||error)));});
  async function load(){
    shell();
    // Opened before the bridge exists: wait for it; a bridge without the call is shown as an error.
    if(!window.pywebview?.api?.visual_novel_state&&!window.pywebview?.api?.get_state)await window.pudgeWhenBridgeReady?.();
    if(!window.pywebview?.api?.visual_novel_state)throw new Error('visual_novel_state is not available');
    pollGeneration+=1;renderRequestId+=1;await update(pollGeneration);
  }
  // The backend started or stopped a capture (the Screen text shortcut).
  async function refresh(){if(!$('visualNovelsContent')||!window.pywebview?.api?.visual_novel_state)return;await load();}
  window.PudgeVisualNovels={load,refresh,stop};
})();
