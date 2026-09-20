(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = () => window.pywebview?.api;
  const ru = () => document.documentElement.lang === 'ru';
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
  const state = {
    tab: 'overview',
    scope: {period:'30d', kind:'all', media_uuid:'', start_date:'', end_date:''},
    payload: null,
    loading: false,
    journalOffset: 0,
    journalLimit: 200,
  };

  const text = (en, r) => ru() ? r : en;
  const KIND_LABELS = {
    anime:['Anime','Аниме'], light_novel:['Light novel','Ранобэ'], manga:['Manga','Манга'],
    audiobook:['Audiobook','Аудиокнига'], visual_novel:['Visual novel','Визуальная новелла'],
    review:['Review','Ревью'], simultaneous:['Simultaneous','Одновременно'],
    'light_novel+audiobook':['LN + audio','Ранобэ + аудио'], unknown:['Unknown','Неизвестно'],
  };
  const kindLabel = kind => (KIND_LABELS[kind] || [kind,kind])[ru()?1:0];
  const duration = seconds => {
    let value=Math.max(0,Number(seconds||0));
    const h=Math.floor(value/3600),m=Math.floor((value%3600)/60),s=Math.floor(value%60);
    if(h)return `${h}h ${m}m`;
    if(m)return `${m}m ${s}s`;
    return `${s}s`;
  };
  const nativeNumber = (value, unit) => {
    if(value==null)return '—';
    const n=Number(value||0);
    if(unit==='seconds')return duration(n);
    if(unit==='characters')return `${Math.round(n).toLocaleString()} ${text('chars','зн.')}`;
    if(unit==='pages')return `${Math.round(n).toLocaleString()} ${text('pages','стр.')}`;
    return '—';
  };
  const volumeText = row => {
    if(!row||!row.exact)return '—';
    const bits=[nativeNumber(row.consumed,row.unit)];
    if(Number(row.repeat_in_scope||0)>0)bits.push(`${text('repeat','повтор')}: ${nativeNumber(row.repeat_in_scope,row.unit)}`);
    if(row.coverage!=null)bits.push(`${Math.round(Number(row.coverage)*100)}%`);
    return bits.join(' · ');
  };
  const workVolumeText = rows => (rows||[]).map(volumeText).filter(v=>v&&v!=='—').join(' + ')||'—';

  const localDateTimeValue = epoch => {
    if(epoch==null||!Number.isFinite(Number(epoch)))return '';
    const d=new Date(Number(epoch)*1000),pad=n=>String(n).padStart(2,'0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const epochFromLocalInput = value => {
    if(!String(value||'').trim())return null;
    const n=new Date(value).getTime();
    return Number.isFinite(n)?n/1000:null;
  };

  function ensureStyles(){
    if($('pudgeStatisticsStyle'))return;
    const style=document.createElement('style');style.id='pudgeStatisticsStyle';style.textContent=`
      .stats-shell{display:grid;gap:14px}.stats-filterbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
      .stats-filterbar select,.stats-filterbar input{min-height:34px}.stats-periods,.stats-tabs{display:flex;gap:6px;flex-wrap:wrap}.stats-periods button.active,.stats-tabs button.active{background:var(--accent);color:white;border-color:transparent}
      .stats-tabs{padding:0 2px}.stats-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.stats-card{padding:14px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.stats-card strong{display:block;font-size:24px;margin-top:5px}.stats-muted{color:var(--muted);font-size:12px}
      .stats-chart{display:grid;grid-template-columns:repeat(var(--stats-columns,1),minmax(20px,1fr));align-items:end;gap:5px;min-height:220px;padding:16px 10px 26px;border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow-x:auto}.stats-bar-wrap{min-width:20px;width:100%;height:180px;display:flex;align-items:end;position:relative}.stats-bar{width:100%;min-height:2px;border-radius:5px 5px 2px 2px;background:linear-gradient(to top,var(--accent),color-mix(in srgb,var(--accent) 45%,#fff));cursor:pointer}.stats-bar-wrap small{position:absolute;top:184px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--muted);white-space:nowrap}
      .stats-calendar{display:grid;grid-template-columns:repeat(18,1fr);gap:4px;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.stats-day{aspect-ratio:1;border:1px solid color-mix(in srgb,var(--line) 70%,transparent);border-radius:3px;background:color-mix(in srgb,var(--accent) calc(var(--heat)*80%),var(--panel));cursor:pointer;min-width:13px}
      .stats-breakdown{display:flex;gap:8px;flex-wrap:wrap}.stats-chip{padding:5px 8px;border:1px solid var(--line);border-radius:999px;font-size:12px}.stats-table{width:100%;border-collapse:collapse}.stats-table th,.stats-table td{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}.stats-table th{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.stats-table tr.excluded{opacity:.55;text-decoration:line-through}.stats-actions{display:flex;gap:6px;flex-wrap:wrap}
      .stats-panel{border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:auto}.stats-empty{padding:30px;text-align:center;color:var(--muted)}
      .stats-modal-backdrop{position:fixed;inset:0;z-index:10040;background:rgba(0,0,0,.58);display:flex;align-items:center;justify-content:center;padding:24px}.stats-modal{width:min(520px,94vw);max-height:90vh;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;display:grid;gap:12px}.stats-modal label{display:grid;gap:5px}.stats-modal footer{display:flex;gap:8px;justify-content:flex-end}.stats-modal input,.stats-modal select,.stats-modal textarea{width:100%}.stats-danger{color:#ff7878}
      @media(max-width:800px){.stats-cards{grid-template-columns:1fr}.stats-calendar{grid-template-columns:repeat(12,1fr)}.stats-table{min-width:760px}}
    `;document.head.appendChild(style);
  }

  function staticShell(){
    const root=$('statisticsContent');if(!root)return;
    ensureStyles();
    root.innerHTML=`<div class="stats-shell">
      <div class="stats-filterbar">
        <div class="stats-periods">
          ${[['7d','7d'],['30d','30d'],['year',text('Year','Год')],['all',text('All time','Всё время')],['custom',text('Custom','Период')]].map(([v,l])=>`<button type="button" data-stats-period="${v}">${l}</button>`).join('')}
        </div>
        <span class="spacer"></span>
        <select id="statsKind"><option value="all">${text('All formats','Все форматы')}</option>${['anime','light_novel','manga','audiobook','visual_novel'].map(v=>`<option value="${v}">${kindLabel(v)}</option>`).join('')}</select>
        <select id="statsMedia"><option value="">${text('All works','Все произведения')}</option></select>
        <span id="statsCustomDates" hidden><input id="statsStartDate" type="date"> <input id="statsEndDate" type="date"></span>
        <button id="statsManual" type="button">${text('Add manually','Добавить вручную')}</button>
        <button id="statsExport" type="button">CSV</button>
        <button id="statsDeleteHistory" class="stats-danger" type="button">${text('Delete history','Удалить историю')}</button>
      </div>
      <div class="stats-tabs"><button data-stats-tab="overview">${text('Overview','Обзор')}</button><button data-stats-tab="works">${text('Works','Произведения')}</button><button data-stats-tab="journal">${text('Journal','Журнал')}</button></div>
      <div id="statsBody"></div>
      <div id="statsModalHost"></div>
    </div>`;
    bindControls();syncControls();
  }

  function bindControls(){
    const root=$('statisticsContent');if(!root)return;
    root.addEventListener('click',async event=>{
      const period=event.target.closest?.('[data-stats-period]');if(period){state.scope.period=period.dataset.statsPeriod;state.journalOffset=0;syncControls();await load();return;}
      const tab=event.target.closest?.('[data-stats-tab]');if(tab){state.tab=tab.dataset.statsTab;syncControls();render();return;}
      const day=event.target.closest?.('[data-stats-day]');if(day){state.scope={...state.scope,period:'custom',start_date:day.dataset.statsDay,end_date:day.dataset.statsDay};state.tab='journal';syncControls();await load();return;}
      const edit=event.target.closest?.('[data-stats-edit]');if(edit){openEdit(edit.dataset.statsEdit,edit.dataset.statsType);return;}
      if(event.target.closest?.('#statsManual')){openManual();return;}
      if(event.target.closest?.('#statsExport')){await exportCsv();return;}
      if(event.target.closest?.('#statsDeleteHistory')){await deleteHistory();return;}
      if(event.target.closest?.('[data-stats-modal-close]')){closeModal();return;}
      const saveManual=event.target.closest?.('[data-stats-save-manual]');if(saveManual){await saveManualEntry(saveManual);return;}
      const saveEdit=event.target.closest?.('[data-stats-save-edit]');if(saveEdit){await saveCorrection(saveEdit);return;}
      const more=event.target.closest?.('[data-stats-more]');if(more){state.journalOffset+=state.journalLimit;await load();return;}
    });
    root.addEventListener('change',async event=>{
      if(event.target.id==='statsKind'){state.scope.kind=event.target.value;state.scope.media_uuid='';state.journalOffset=0;await load();}
      else if(event.target.id==='statsMedia'){state.scope.media_uuid=event.target.value;state.journalOffset=0;await load();}
      else if(event.target.id==='statsStartDate'||event.target.id==='statsEndDate'){state.scope.start_date=$('statsStartDate')?.value||'';state.scope.end_date=$('statsEndDate')?.value||state.scope.start_date;if(state.scope.start_date&&state.scope.end_date)await load();}
    });
  }

  function syncControls(){
    document.querySelectorAll('[data-stats-period]').forEach(b=>b.classList.toggle('active',b.dataset.statsPeriod===state.scope.period));
    document.querySelectorAll('[data-stats-tab]').forEach(b=>b.classList.toggle('active',b.dataset.statsTab===state.tab));
    if($('statsKind'))$('statsKind').value=state.scope.kind||'all';
    if($('statsCustomDates'))$('statsCustomDates').hidden=state.scope.period!=='custom';
    if($('statsStartDate'))$('statsStartDate').value=state.scope.start_date||'';
    if($('statsEndDate'))$('statsEndDate').value=state.scope.end_date||'';
  }

  async function load(){
    if(!API()?.consumption_statistics_query)return;
    if(!$('statisticsContent')?.querySelector('.stats-shell'))staticShell();
    state.loading=true;render();
    try{
      state.payload=await API().consumption_statistics_query({...state.scope},state.journalLimit,state.journalOffset);
      const media=$('statsMedia'),options=state.payload?.media_options||[];
      if(media){const selected=state.scope.media_uuid||'';media.innerHTML=`<option value="">${text('All works','Все произведения')}</option>`+options.filter(row=>state.scope.kind==='all'||row.kind===state.scope.kind).map(row=>`<option value="${esc(row.media_uuid)}">${esc(row.title)} · ${esc(kindLabel(row.kind))}</option>`).join('');media.value=selected;}
    }catch(error){state.payload={error:String(error?.message||error)};}
    finally{state.loading=false;syncControls();render();}
  }

  function render(){
    const body=$('statsBody');if(!body)return;
    if(state.loading){body.innerHTML=`<div class="stats-empty">${text('Loading statistics…','Загружаю статистику…')}</div>`;return;}
    if(state.payload?.error){body.innerHTML=`<div class="stats-empty stats-danger">${esc(state.payload.error)}</div>`;return;}
    if(!state.payload){body.innerHTML='';return;}
    if(state.tab==='works')renderWorks(body);else if(state.tab==='journal')renderJournal(body);else renderOverview(body);
  }

  function renderOverview(body){
    const p=state.payload,s=p.summary||{},days=p.days||[],max=Math.max(1,...days.map(d=>Number(d.seconds||0)));
    const timezoneNote=text(`Local time follows this computer automatically (${p.timezone?.label||'local'})`,`Локальное время определяется компьютером автоматически (${p.timezone?.label||'local'})`);
    let chart='';
    if(state.scope.period==='year'){
      chart=`<div class="stats-calendar">${days.map(d=>`<button class="stats-day" data-stats-day="${esc(d.day)}" style="--heat:${Math.max(.08,Number(d.seconds||0)/max).toFixed(3)}" title="${esc(d.day)} · ${esc(duration(d.seconds))}"></button>`).join('')}</div>`;
    }else{
      chart=`<div class="stats-chart" style="--stats-columns:${Math.max(1,days.length)}">${days.length?days.map(d=>`<div class="stats-bar-wrap"><button class="stats-bar" data-stats-day="${esc(d.day)}" style="height:${Math.max(2,Number(d.seconds||0)/max*100)}%" title="${esc(duration(d.seconds))}"></button><small>${esc(d.day.slice(5))}</small></div>`).join(''):`<div class="stats-empty">${text('No recorded activity in this period','Нет записанной активности за период')}</div>`}</div>`;
    }
    const breakdown=Object.entries(p.breakdown||{}).sort((a,b)=>b[1]-a[1]).map(([kind,seconds])=>`<span class="stats-chip">${esc(kindLabel(kind))}: ${esc(duration(seconds))}</span>`).join('');
    const native=Object.values(p.volume?.by_kind||{}).map(row=>`<span class="stats-chip">${esc(kindLabel(row.kind))}: ${esc(row.exact?nativeNumber(row.consumed,row.unit):'—')}${Number(row.repeat_in_scope||0)>0?` · ${text('repeat','повтор')} ${esc(nativeNumber(row.repeat_in_scope,row.unit))}`:''}</span>`).join('');
    const coverageNote=p.volume?.coverage_started_at_utc?text('Native coverage is recorded only since Statistics tracking started.','Объём учитывается только с момента запуска Statistics.') : '';
    body.innerHTML=`<div class="stats-shell"><div class="stats-cards"><div class="stats-card"><span class="stats-muted">${text('Time with content','Время с контентом')}</span><strong>${esc(duration(s.total_seconds))}</strong></div><div class="stats-card"><span class="stats-muted">${text('Active days','Активные дни')}</span><strong>${Number(s.active_days||0)}</strong></div><div class="stats-card"><span class="stats-muted">${text('Sessions','Сессии')}</span><strong>${Number(s.sessions||0)}</strong></div></div><div class="stats-muted">${esc(timezoneNote)}${coverageNote?` · ${esc(coverageNote)}`:''}${Number(s.manual_unplaced_seconds||0)>0?` · ${text('Manual without exact time','Вручную без точного времени')}: ${duration(s.manual_unplaced_seconds)}`:''}</div>${chart}<div class="stats-breakdown">${breakdown}</div>${native?`<div class="stats-breakdown">${native}</div>`:''}</div>`;
  }

  function renderWorks(body){
    const rows=state.payload.works||[];
    body.innerHTML=`<div class="stats-panel">${rows.length?`<table class="stats-table"><thead><tr><th>${text('Work','Произведение')}</th><th>${text('Format','Формат')}</th><th>${text('Time','Время')}</th><th>${text('Volume / coverage','Объём / покрытие')}</th><th>${text('Last activity','Последняя активность')}</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row.title)}</td><td>${esc(kindLabel(row.kind))}</td><td>${esc(duration(row.seconds))}</td><td>${esc(workVolumeText(row.volume))}</td><td>${esc(new Date(Number(row.last_at_utc||0)*1000).toLocaleString())}</td></tr>`).join('')}</tbody></table>`:`<div class="stats-empty">${text('No works yet','Пока нет произведений')}</div>`}</div>`;
  }

  function renderJournal(body){
    const rows=state.payload.journal||[];
    body.innerHTML=`<div class="stats-panel">${rows.length?`<table class="stats-table"><thead><tr><th>${text('Start','Начало')}</th><th>${text('End','Конец')}</th><th>${text('Time','Время')}</th><th>${text('Work','Произведение')}</th><th>${text('Format','Формат')}</th><th>${text('Source','Источник')}</th><th>${text('Device','Устройство')}</th><th></th></tr></thead><tbody>${rows.map(row=>`<tr class="${row.excluded?'excluded':''}"><td>${row.start_utc==null?text('No exact time','Без точного времени'):esc(new Date(Number(row.start_utc)*1000).toLocaleString())}</td><td>${row.end_utc==null?'—':esc(new Date(Number(row.end_utc)*1000).toLocaleString())}</td><td>${esc(duration(row.seconds))}</td><td>${esc(row.title)}</td><td>${esc(kindLabel(row.kind))}</td><td>${esc(row.origin==='manual'?text('Manual','Вручную'):(row.estimated?text('Estimated','Оценка'):text('Automatic','Автоматически')))}</td><td>${esc(row.device||'')}</td><td><button data-stats-edit="${esc(row.id)}" data-stats-type="${esc(row.type)}">${text('Edit','Исправить')}</button></td></tr>`).join('')}</tbody></table>${Number(state.payload.journal_total||0)>state.journalOffset+rows.length?`<div style="padding:12px"><button data-stats-more>${text('More','Ещё')}</button></div>`:''}`:`<div class="stats-empty">${text('Journal is empty','Журнал пуст')}</div>`}</div>`;
  }

  function closeModal(){const host=$('statsModalHost');if(host)host.innerHTML='';}
  function modal(content){const host=$('statsModalHost');if(!host)return;host.innerHTML=`<div class="stats-modal-backdrop"><div class="stats-modal">${content}</div></div>`;}
  function openManual(){
    const options=(state.payload?.media_options||[]).map(row=>`<option value="${esc(row.media_uuid)}" data-kind="${esc(row.kind)}">${esc(row.title)} · ${esc(kindLabel(row.kind))}</option>`).join('');
    modal(`<h3>${text('Add activity manually','Добавить активность вручную')}</h3><label>${text('Format','Формат')}<select id="statsManualKind">${['anime','light_novel','manga','audiobook','visual_novel'].map(v=>`<option value="${v}">${kindLabel(v)}</option>`).join('')}</select></label><label>${text('Existing work (optional)','Существующее произведение (необязательно)')}<select id="statsManualMedia"><option value="">—</option>${options}</select></label><label>${text('Title for a new work','Название нового произведения')}<input id="statsManualTitle"></label><label>${text('Start time (optional)','Время начала (необязательно)')}<input id="statsManualStart" type="datetime-local"></label><label>${text('Duration, minutes','Длительность, минут')}<input id="statsManualMinutes" type="number" min="0.1" step="0.1" value="30"></label><label>${text('Note','Заметка')}<textarea id="statsManualNote"></textarea></label><footer><button data-stats-modal-close>${text('Cancel','Отмена')}</button><button class="primary" data-stats-save-manual>${text('Save','Сохранить')}</button></footer>`);
  }
  async function saveManualEntry(button){
    button.disabled=true;try{const media=$('statsManualMedia')?.value||'',selected=$('statsManualMedia')?.selectedOptions?.[0],selectedKind=selected?.dataset?.kind;const kind=media?(selectedKind||$('statsManualKind')?.value):$('statsManualKind')?.value;await API().consumption_statistics_add_manual(kind,Number($('statsManualMinutes')?.value||0)*60,epochFromLocalInput($('statsManualStart')?.value),media,$('statsManualTitle')?.value||'',$('statsManualNote')?.value||'');closeModal();await load();}catch(error){window.toast?.(String(error?.message||error));button.disabled=false;}
  }
  function openEdit(id,type){
    const row=(state.payload?.journal||[]).find(item=>String(item.id)===String(id)&&String(item.type)===String(type));if(!row)return;
    modal(`<h3>${text('Correct journal entry','Исправить запись журнала')}</h3><div><strong>${esc(row.title)}</strong><div class="stats-muted">${esc(kindLabel(row.kind))} · ${esc(duration(row.seconds))}</div></div><label>${text('Start','Начало')}<input id="statsEditStart" type="datetime-local" value="${esc(localDateTimeValue(row.start_utc))}"></label><label>${text('End','Конец')}<input id="statsEditEnd" type="datetime-local" value="${esc(localDateTimeValue(row.end_utc))}"></label><label><span><input id="statsEditExcluded" type="checkbox" ${row.excluded?'checked':''}> ${text('Exclude from statistics','Исключить из статистики')}</span></label><label>${text('Reason','Причина')}<input id="statsEditReason" value="${esc(row.correction_reason||'')}"></label><footer><button data-stats-modal-close>${text('Cancel','Отмена')}</button><button class="primary" data-stats-save-edit data-id="${esc(row.id)}" data-type="${esc(row.type)}" data-revision="${Number(row.correction_revision||0)}">${text('Save correction','Сохранить исправление')}</button></footer>`);
  }
  async function saveCorrection(button){
    button.disabled=true;try{const start=epochFromLocalInput($('statsEditStart')?.value),end=epochFromLocalInput($('statsEditEnd')?.value);await API().consumption_statistics_correct(button.dataset.type,button.dataset.id,Number(button.dataset.revision||0),!!$('statsEditExcluded')?.checked,start,end,null,$('statsEditReason')?.value||'');closeModal();await load();}catch(error){window.toast?.(String(error?.message||error));button.disabled=false;}
  }
  async function exportCsv(){
    try{const result=await API().consumption_statistics_export({...state.scope});window.toast?.(text(`Exported ${result.rows} rows`,`Экспортировано строк: ${result.rows}`));}catch(error){window.toast?.(String(error?.message||error));}
  }

  async function deleteHistory(){
    if(!await window.pudgeConfirm?.(text('Delete all Statistics history on every synced device? Library files and current progress stay untouched.','Удалить всю историю Statistics на всех синхронизируемых устройствах? Файлы библиотеки и текущий прогресс останутся.'),{danger:true}))return;
    try{await API().consumption_statistics_delete_history();state.journalOffset=0;await load();window.toast?.(text('Statistics history deleted','История Statistics удалена'));}catch(error){window.toast?.(String(error?.message||error));}
  }

  // Reader recorder -------------------------------------------------------
  const activity={lastInteraction:performance.now(), samples:new Map(), inflight:new Set()};
  function readerOpen(){return !!window.PudgeLnConsumptionContext?.()?.open||!!window.PudgeMangaReaderV2?.consumptionContext?.()?.open;}
  function markInteraction(event){
    if(event?.target?.closest?.('#lnReaderShell,#mangaReaderV2')||readerOpen())activity.lastInteraction=performance.now();
  }
  ['pointerdown','keydown','wheel','touchstart'].forEach(name=>document.addEventListener(name,markInteraction,{capture:true,passive:name==='wheel'||name==='touchstart'}));
  function contexts(){
    const rows=[];
    try{const ln=window.PudgeLnConsumptionContext?.();if(ln?.open&&ln.book_id)rows.push({key:`light_novel:${ln.book_id}`,kind:'light_novel',id:Number(ln.book_id),locator:{chapter_index:Number(ln.chapter_index||0),chapter_key:String(ln.chapter_key||`chapter:${Number(ln.chapter_index||0)}`),character_offset:Number(ln.character_offset||0)},payload:{paired_audio_id:Number(ln.paired_audio_id||0),character_count:Number(ln.character_count||0),locator_policy:'ln-text-hash-v1'}});}catch(_e){}
    try{const manga=window.PudgeMangaReaderV2?.consumptionContext?.();if(manga?.open&&manga.book_id)rows.push({key:`manga:${manga.book_id}`,kind:'manga',id:Number(manga.book_id),locator:{page_index:Number(manga.page_index||0),page_id:String(manga.page_id||`page:${Number(manga.page_index||0)}`)},payload:{page_count:Number(manga.page_count||0),dwell_policy:'page-dwell-v1'}});}catch(_e){}
    return rows;
  }
  async function sampleReaders(){
    const now=performance.now(),wall=Date.now(),visible=!document.hidden&&document.hasFocus(),openContexts=visible?contexts():[];
    if(openContexts.some(context=>!activity.samples.has(context.key)))activity.lastInteraction=now;
    const activeRecently=now-activity.lastInteraction<=5*60*1000,current=activeRecently?openContexts:[],seen=new Set();
    for(const context of current){
      seen.add(context.key);const previous=activity.samples.get(context.key);activity.samples.set(context.key,{mono:now,wall,locator:context.locator});
      if(!previous)continue;const delta=(now-previous.mono)/1000,wallDelta=(wall-previous.wall)/1000;
      if(delta<0.15||delta>15||Math.abs(delta-wallDelta)>2.5||activity.inflight.has(context.key))continue;
      activity.inflight.add(context.key);
      const request=API()?.consumption_reader_observation?.(context.kind,context.id,delta,previous.locator,context.locator,context.payload);
      if(!request||typeof request.then!=='function'){activity.inflight.delete(context.key);continue;}
      request.catch(()=>{}).finally(()=>activity.inflight.delete(context.key));
    }
    for(const key of [...activity.samples.keys()])if(!seen.has(key))activity.samples.delete(key);
  }
  setInterval(sampleReaders,5000);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)activity.samples.clear();});
  window.addEventListener('blur',()=>activity.samples.clear());

  window.PudgeStatistics={load,render,state};
})();
