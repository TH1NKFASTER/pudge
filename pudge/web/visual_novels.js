'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
  let windows = [], activeWindow = null, pollTimer = null, lastLineId = 0;
  const parsed = new Map();

  function shell() {
    const root = $('visualNovelsContent');
    if (!root) return null;
    if (!root.firstChild) root.innerHTML = `<div class="vn-shell"><div class="vn-head"><label>${ru()?'Окно игры':'Game window'}<select id="vnWindow"></select></label><button id="vnRefresh">${ru()?'Обновить окна':'Refresh windows'}</button><button id="vnStart" class="primary">${ru()?'Начать чтение':'Start reader'}</button><button id="vnStop" hidden>${ru()?'Остановить':'Stop'}</button></div><div id="vnStatus" class="vn-status"></div><div class="vn-actions"><button id="vnIdentity">AniList</button><button id="vnNames">${ru()?'Имена персонажей':'Character names'}</button></div><div class="vn-grid"><section class="vn-live"><h3>${ru()?'Текущая реплика':'Current line'}</h3><div id="vnCurrent" class="vn-current" data-pudge-study-hover data-pudge-translate-root></div></section><section class="vn-transcript"><h3>${ru()?'История':'Transcript'}</h3><div id="vnTranscript" class="vn-transcript-list"></div></section></div></div>`;
    return root;
  }

  async function loadWindows() {
    shell();
    const select = $('vnWindow');select.innerHTML=`<option>${ru()?'Ищу окна…':'Finding windows…'}</option>`;
    try { windows = await pywebview.api.visual_novel_windows();select.innerHTML=windows.length?windows.map(row=>`<option value="${Number(row.id)}">${esc(row.label)}</option>`).join(''):`<option>${ru()?'Подходящих окон нет':'No suitable windows'}</option>`; }
    catch (error) { $('vnStatus').textContent=error.message||error; $('vnStatus').classList.add('error'); }
  }

  async function renderCurrent(text, lineId) {
    const root=$('vnCurrent');if(!root)return;
    if(!text){root.textContent='';return;}
    if(parsed.has(lineId)){root.innerHTML=window.PudgeReadingTools?.study?.renderParsedText(parsed.get(lineId),{backend:'jiten'})||esc(text);return;}
    root.textContent=text;
    try{const payload=await pywebview.api.visual_novel_parse(text);parsed.set(lineId,payload);if(lastLineId===lineId)root.innerHTML=window.PudgeReadingTools?.study?.renderParsedText(payload,{backend:'jiten'})||esc(text);}catch(error){console.debug?.('VN parse',error);}
  }

  async function update() {
    const state=await pywebview.api.visual_novel_state(),status=$('vnStatus');
    activeWindow=state.window_id?{id:Number(state.window_id),title:state.window_title||'Visual Novel'}:activeWindow;
    $('vnStart').hidden=!!state.running;$('vnStop').hidden=!state.running;
    status.classList.toggle('error',state.status==='error');status.innerHTML=state.status==='error'?`${esc(state.detail)} <button id="vnPermission">${ru()?'Открыть доступ к записи экрана':'Open Screen Recording settings'}</button>`:(state.running?(ru()?'Читаю выбранное окно; OCR запускается только при изменении кадра.':'Reading the selected window; OCR only runs after a frame change.'):(ru()?'Захват выключен. Выберите окно и нажмите «Начать».':'Capture is off. Select a window and press Start.'));
    const rows=state.transcript||[];$('vnTranscript').innerHTML=rows.slice().reverse().map(row=>`<button class="vn-line ${Number(row.id)===lastLineId?'active':''}" data-vn-line="${Number(row.id)}">${esc(row.text)}</button>`).join('');
    const newest=rows.at(-1);if(newest&&Number(newest.id)!==lastLineId){lastLineId=Number(newest.id);await renderCurrent(newest.text,lastLineId);}
    if(state.running)pollTimer=setTimeout(()=>void update(),500);
  }

  async function start(){const id=Number($('vnWindow')?.value||0),row=windows.find(item=>Number(item.id)===id);if(!id||!row)return;activeWindow={id,title:row.title||row.label||'Visual Novel'};lastLineId=0;parsed.clear();await pywebview.api.visual_novel_start(id,activeWindow.title);await update();}
  async function stop(){if(pollTimer)clearTimeout(pollTimer);pollTimer=null;await pywebview.api.visual_novel_stop();await update();}
  async function refreshIdentity(){if(!activeWindow)return;const identity=await pywebview.api.media_identity_current('visual_novel',activeWindow.id);if($('vnCurrent'))$('vnCurrent').dataset.pudgeMediaId=String(identity?.anilist_id||'');}

  document.addEventListener('click',event=>{void (async()=>{if(event.target.id==='vnRefresh')await loadWindows();else if(event.target.id==='vnStart')await start();else if(event.target.id==='vnStop')await stop();else if(event.target.id==='vnPermission')await pywebview.api.open_screen_recording_settings();else if(event.target.id==='vnIdentity'&&activeWindow)await window.showMediaIdentity?.('visual_novel',activeWindow.id,activeWindow.title);else if(event.target.id==='vnNames'&&activeWindow){const identity=await pywebview.api.media_identity_current('visual_novel',activeWindow.id);await window.showCharacterGlossaryEditor?.(identity.anilist_id,identity.title||activeWindow.title);}else{const line=event.target.closest?.('[data-vn-line]');if(line){const state=await pywebview.api.visual_novel_state(),row=(state.transcript||[]).find(item=>Number(item.id)===Number(line.dataset.vnLine));if(row){lastLineId=Number(row.id);await renderCurrent(row.text,lastLineId);}}}})().catch(error=>window.toast?.(String(error?.message||error)));});
  window.addEventListener('pudge-media-identity-changed',event=>{if(event.detail?.kind==='visual_novel')void refreshIdentity();});
  window.PudgeVisualNovels={load:async()=>{shell();await loadWindows();await update();await refreshIdentity();},stop};
})();
