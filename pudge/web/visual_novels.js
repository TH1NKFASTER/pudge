'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
  let windows = [], activeWindow = null, pollTimer = null, pollGeneration = 0, renderRequestId = 0;
  let lastLineId = 0, lastCurrentKey = '', transcriptRows = [];
  const regionPresets={lower62:[0,0.38,1,0.62],lower50:[0,0.5,1,0.5],lower40:[0,0.6,1,0.4],full:[0,0,1,1]};
  const parsed = new Map();

  function shell() {
    const root = $('visualNovelsContent');
    if (!root) return null;
    if (!root.firstChild) root.innerHTML = `<div class="vn-shell"><div class="vn-head"><label>${ru()?'Окно игры':'Game window'}<select id="vnWindow"></select></label><label>${ru()?'Название игры':'Game title'}<input id="vnGameTitle" type="text" placeholder="Visual Novel"></label><label>${ru()?'Область диалога':'Dialogue area'}<select id="vnRegion"><option value="lower62">${ru()?'Нижние 62%':'Lower 62%'}</option><option value="lower50">${ru()?'Нижние 50%':'Lower 50%'}</option><option value="lower40">${ru()?'Нижние 40%':'Lower 40%'}</option><option value="full">${ru()?'Всё окно':'Full window'}</option></select></label><button id="vnRefresh">${ru()?'Обновить окна':'Refresh windows'}</button><button id="vnStart" class="primary">${ru()?'Начать чтение':'Start reader'}</button><button id="vnStop" hidden>${ru()?'Остановить':'Stop'}</button></div><div id="vnStatus" class="vn-status"></div><div class="vn-grid"><section class="vn-live"><h3>${ru()?'Текущая реплика':'Current line'}</h3><div id="vnSpeaker" class="vn-speaker"></div><div id="vnCurrent" class="vn-current" data-pudge-study-hover data-pudge-translate-root></div></section><section class="vn-transcript"><h3>${ru()?'История':'Transcript'}</h3><div id="vnTranscript" class="vn-transcript-list"></div></section></div></div>`;
    return root;
  }

  async function loadWindows() {
    shell();
    const select = $('vnWindow'); select.innerHTML=`<option>${ru()?'Ищу окна…':'Finding windows…'}</option>`;
    try {
      windows = await pywebview.api.visual_novel_windows();
      select.innerHTML=windows.length?windows.map(row=>`<option value="${Number(row.id)}">${esc(row.label)}</option>`).join(''):`<option>${ru()?'Подходящих окон нет':'No suitable windows'}</option>`;
      const first=windows[0]||null;
      if(first&&$('vnGameTitle')&&!String($('vnGameTitle').value||'').trim())$('vnGameTitle').value=String(first.title||first.owner||'Visual Novel');
    } catch (error) {
      $('vnStatus').textContent=error.message||error;
      $('vnStatus').classList.add('error');
    }
  }

  function studyContextOptions(text,lineId){
    const index=transcriptRows.findIndex(row=>Number(row.id)===Number(lineId));
    if(index<0)return {backend:'jiten'};
    const previous=String(transcriptRows[index-1]?.text||''),next=String(transcriptRows[index+1]?.text||'');
    const contextText=[previous,String(text||''),next].filter(Boolean).join('\n');
    return {backend:'jiten',contextText,contextOffset:previous?previous.length+1:0};
  }

  async function renderCurrent(text,lineId,sessionGeneration=pollGeneration) {
    const root=$('vnCurrent'); if(!root)return;
    const requestId=++renderRequestId;
    if(sessionGeneration!==pollGeneration)return;
    if(!text){root.textContent='';return;}
    const options=studyContextOptions(text,lineId);
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
        if(status){status.textContent=String(error?.message||error);status.classList.add('error');}
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
      status.innerHTML=`${esc(state.detail||'Capture failed')}${permission?` <button id="vnPermission">${ru()?'Открыть доступ к записи экрана':'Open Screen Recording settings'}</button>`:''}${windowGone?` <button id="vnRefreshError">${ru()?'Обновить окна':'Refresh windows'}</button>`:''}`;
      return;
    }
    if(state.running){
      const captures=Number(state.capture_count||0),ocrCount=Number(state.ocr_count||0),emptyCount=Number(state.empty_ocr_count||0);
      const width=Number(state.last_frame_width||0),height=Number(state.last_frame_height||0),contrast=Number(state.last_frame_contrast||0);
      if(captures>0 && !state.current_text && !(state.transcript||[]).length){
        const frame=width&&height?`${width}×${height}`:'';
        const blankish=contrast>0&&contrast<1.5;
        status.textContent=ru()
          ?`Кадр получен${frame?` (${frame})`:''}; OCR пока не нашёл текст${emptyCount?` (${emptyCount} пустых попыток)`:''}. ${blankish?'Кадр выглядит почти пустым/чёрным. ':'Повторяю OCR на неподвижном кадре. '}`
          :`Frame captured${frame?` (${frame})`:''}; OCR has not found text yet${emptyCount?` (${emptyCount} empty attempts)`:''}. ${blankish?'The frame looks nearly blank/black. ':'Retrying OCR even while the frame is static. '}`;
      }else{
        status.textContent=ru()
          ?`Читаю через ScreenCaptureKit · кадров ${captures} · OCR ${ocrCount}`
          :`Reading through ScreenCaptureKit · frames ${captures} · OCR ${ocrCount}`;
      }
    }else{
      status.textContent=ru()?'Захват выключен. Выберите окно и нажмите «Начать».':'Capture is off. Select a window and press Start.';
    }
  }

  async function update(generation=pollGeneration) {
    if(generation!==pollGeneration)return;
    const state=await pywebview.api.visual_novel_state();
    if(generation!==pollGeneration)return;
    activeWindow=state.window_id?{id:Number(state.window_id),title:state.window_title||'Visual Novel'}:activeWindow;
    $('vnStart').hidden=!!state.running; $('vnStop').hidden=!state.running;
    renderStatus(state);
    if($('vnSpeaker'))$('vnSpeaker').textContent=String(state.speaker_text||'');

    const rows=state.transcript||[];
    transcriptRows=rows;
    $('vnTranscript').innerHTML=rows.slice().reverse().map(row=>`<button class="vn-line ${Number(row.id)===lastLineId?'active':''}" data-vn-line="${Number(row.id)}">${esc(row.text)}</button>`).join('');

    const newest=rows.at(-1);
    if(newest&&Number(newest.id)!==lastLineId){
      lastLineId=Number(newest.id);
      lastCurrentKey='';
      void renderCurrent(newest.text,lastLineId,generation);
    } else if(state.current_text) {
      const key=`${Number(state.generation||0)}:${Number(state.current_text_id||0)}:${String(state.current_text)}`;
      if(key!==lastCurrentKey){
        lastCurrentKey=key;
        void renderCurrent(state.current_text,`current:${key}`,generation);
      }
    } else if(!newest && lastCurrentKey) {
      lastCurrentKey='';
      void renderCurrent('', 'current:empty',generation);
    }
    if(state.running)schedulePoll(generation);
  }

  async function start(){
    const id=Number($('vnWindow')?.value||0),row=windows.find(item=>Number(item.id)===id);
    if(!id||!row)return;
    pollGeneration+=1;renderRequestId+=1;
    if(pollTimer)clearTimeout(pollTimer);pollTimer=null;
    await applyRegionPreset($('vnRegion')?.value||'lower62');
    const gameTitle=String($('vnGameTitle')?.value||row.title||row.label||'Visual Novel').trim()||'Visual Novel';
    activeWindow={id,title:gameTitle,owner:String(row.owner||'')};
    lastLineId=0;lastCurrentKey='';parsed.clear();
    await pywebview.api.visual_novel_start(id,activeWindow.title,activeWindow.owner);
    await update(pollGeneration);
  }

  async function stop(){
    pollGeneration+=1;renderRequestId+=1;
    if(pollTimer)clearTimeout(pollTimer);pollTimer=null;
    await pywebview.api.visual_novel_stop();
    await update(pollGeneration);
  }

  document.addEventListener('change',event=>{if(event.target.id==='vnRegion')void applyRegionPreset(event.target.value).catch(error=>window.toast?.(String(error?.message||error)));else if(event.target.id==='vnWindow'){const row=windows.find(item=>Number(item.id)===Number(event.target.value||0));if(row&&$('vnGameTitle'))$('vnGameTitle').value=String(row.title||row.owner||'Visual Novel');}});
  document.addEventListener('click',event=>{void (async()=>{if(event.target.id==='vnRefresh'||event.target.id==='vnRefreshError')await loadWindows();else if(event.target.id==='vnStart')await start();else if(event.target.id==='vnStop')await stop();else if(event.target.id==='vnPermission')await pywebview.api.open_screen_recording_settings();else{const line=event.target.closest?.('[data-vn-line]');if(line){const state=await pywebview.api.visual_novel_state();transcriptRows=state.transcript||[];const row=transcriptRows.find(item=>Number(item.id)===Number(line.dataset.vnLine));if(row){lastLineId=Number(row.id);lastCurrentKey='';void renderCurrent(row.text,lastLineId,pollGeneration);}}}})().catch(error=>window.toast?.(String(error?.message||error)));});
  window.PudgeVisualNovels={load:async()=>{shell();pollGeneration+=1;renderRequestId+=1;await loadWindows();await update(pollGeneration);},stop};
})();
