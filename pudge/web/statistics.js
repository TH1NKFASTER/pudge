(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = () => window.pywebview?.api;
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
  const state = {
    tab: 'overview',
    scope: {period:'30d', kind:'all', media_uuid:'', start_date:'', end_date:''},
    payload: null,
    loading: false,
    journalOffset: 0,
    journalLimit: 200,
    journalRows: [],
    journalHasMore: true,
    displayedScope: null,
    error: '',
    requestEpoch: 0,
    mutationEpoch: 0,
    inflight: null,
    inflightKey: '',
    loadingMore: false,
    mediaOptionsSignature: '',
    legendHover: '',
    legendPinned: '',
  };

  const KIND_LABELS = {
    anime:'stats.kind.anime', light_novel:'stats.kind.lightNovel', manga:'stats.kind.manga',
    audiobook:'stats.kind.audiobook', visual_novel:'stats.kind.visualNovel',
    review:'stats.kind.review', simultaneous:'stats.kind.simultaneous',
    'light_novel+audiobook':'stats.kind.lightNovelAudio', unknown:'stats.kind.unknown',
  };
  const kindLabel = kind => KIND_LABELS[kind] ? PudgeI18n.t(KIND_LABELS[kind]) : kind;
  const duration = seconds => {
    let value=Math.max(0,Number(seconds||0));
    const h=Math.floor(value/3600),m=Math.floor((value%3600)/60),s=Math.floor(value%60);
    if(h)return PudgeI18n.t('library.durationHoursMinutes',{hours:h,minutes:m});
    if(m)return PudgeI18n.t('stats.durationMinutesSeconds',{minutes:m,seconds:s});
    return PudgeI18n.t('stats.durationSeconds',{seconds:s});
  };
  const nativeNumber = (value, unit) => {
    if(value==null)return '—';
    const n=Number(value||0);
    if(unit==='seconds')return duration(n);
    if(unit==='characters')return PudgeI18n.tn('stats.characters',Math.round(n),{value:PudgeI18n.number(Math.round(n))});
    if(unit==='pages')return PudgeI18n.tn('stats.pages',Math.round(n),{value:PudgeI18n.number(Math.round(n))});
    return '—';
  };
  const volumeText = row => {
    if(!row||!row.exact)return '—';
    const bits=[nativeNumber(row.consumed,row.unit)];
    if(Number(row.repeat_in_scope||0)>0)bits.push(PudgeI18n.t('stats.repeat',{value:nativeNumber(row.repeat_in_scope,row.unit)}));
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

  function staticShell(){
    const root=$('statisticsContent');if(!root)return;
    root.innerHTML=`<div class="stats-shell">
      <div class="stats-filterbar">
        <div class="stats-periods">
          ${[['7d',PudgeI18n.t('stats.period7d')],['30d',PudgeI18n.t('stats.period30d')],['year',PudgeI18n.t('stats.year')],['all',PudgeI18n.t('stats.allTime')],['custom',PudgeI18n.t('stats.custom')]].map(([v,l])=>`<button type="button" data-stats-period="${v}">${l}</button>`).join('')}
        </div>
        <span class="spacer"></span>
        <select id="statsKind"><option value="all">${PudgeI18n.t('stats.allFormats')}</option>${['anime','light_novel','manga','audiobook','visual_novel'].map(v=>`<option value="${v}">${kindLabel(v)}</option>`).join('')}</select>
        <select id="statsMedia"><option value="">${PudgeI18n.t('stats.allWorks')}</option></select>
        <span id="statsCustomDates" hidden><input id="statsStartDate" type="date"> <input id="statsEndDate" type="date"></span>
        <button id="statsManual" type="button">${PudgeI18n.t('stats.addManually')}</button>
        <button id="statsExport" type="button">CSV</button>
        <button id="statsDeleteHistory" class="stats-danger" type="button">${PudgeI18n.t('stats.deleteHistory')}</button>
      </div>
      <div class="stats-tabs"><button data-stats-tab="overview">${PudgeI18n.t('stats.overview')}</button><button data-stats-tab="works">${PudgeI18n.t('stats.works')}</button><button data-stats-tab="journal">${PudgeI18n.t('stats.journal')}</button></div>
      <div id="statsStatus" role="status" aria-live="polite"></div>
      <div id="statsBody"></div>
      <div id="statsModalHost"></div>
    </div>`;
    bindControls();syncControls();
  }

  // Scope identity is distinct from the payload already displayed. Relative
  // periods include the local calendar day and offset so crossing midnight or
  // changing timezone cannot reuse yesterday's request.
  function scopeKey(scope){
    const period=String(scope.period||'30d');
    const today=new Date();
    return JSON.stringify([
      period,String(scope.kind||'all'),String(scope.media_uuid||''),
      period==='custom'?String(scope.start_date||''):'',
      period==='custom'?String(scope.end_date||''):'',
      period==='custom'||period==='all'?'':`${today.toDateString()}:${today.getTimezoneOffset()}`,
    ]);
  }
  function changeScope(patch){
    const next={...state.scope,...patch};
    if(scopeKey(next)===scopeKey(state.scope))return false;
    state.scope=next;
    state.requestEpoch+=1;
    state.journalOffset=0;
    state.journalRows=[];
    state.journalHasMore=true;
    state.loadingMore=false;
    state.error='';
    syncControls();
    return true;
  }
  function invalidateAfterMutation(){
    state.mutationEpoch+=1;
    state.requestEpoch+=1;
    // Successful deletion/correction invalidates the displayed snapshot too.
    // Do not show deleted journal rows or old totals while reloading.
    state.payload=null;
    state.displayedScope=null;
    state.inflight=null;
    state.inflightKey='';
    state.journalOffset=0;
    state.journalRows=[];
    state.journalHasMore=true;
    state.loadingMore=false;
    state.error='';
  }
  function bindControls(){
    const root=$('statisticsContent');if(!root)return;
    root.addEventListener('click',async event=>{
      const period=event.target.closest?.('[data-stats-period]');if(period){if(changeScope({period:period.dataset.statsPeriod}))await load();return;}
      const legend=event.target.closest?.('[data-stats-legend]');if(legend){const kind=legend.dataset.statsLegend;state.legendPinned=state.legendPinned===kind?'':kind;applyLegendFocus();return;}
      const tab=event.target.closest?.('[data-stats-tab]');if(tab){if(state.tab!==tab.dataset.statsTab){state.tab=tab.dataset.statsTab;syncControls();render();}return;}
      const day=event.target.closest?.('[data-stats-day]');if(day){const changed=changeScope({period:'custom',start_date:day.dataset.statsDay,end_date:day.dataset.statsDay});const tabChanged=state.tab!=='journal';state.tab='journal';syncControls();if(changed)await load();else if(tabChanged)render();return;}
      const edit=event.target.closest?.('[data-stats-edit]');if(edit){openEdit(edit.dataset.statsEdit,edit.dataset.statsType);return;}
      if(event.target.closest?.('#statsManual')){openManual();return;}
      if(event.target.closest?.('#statsExport')){await exportCsv();return;}
      if(event.target.closest?.('#statsDeleteHistory')){await deleteHistory();return;}
      if(event.target.closest?.('[data-stats-modal-close]')){closeModal();return;}
      const saveManual=event.target.closest?.('[data-stats-save-manual]');if(saveManual){await saveManualEntry(saveManual);return;}
      const saveEdit=event.target.closest?.('[data-stats-save-edit]');if(saveEdit){await saveCorrection(saveEdit);return;}
      if(event.target.closest?.('[data-stats-retry]')){await load();return;}
      if(event.target.closest?.('[data-stats-more]')){await load({more:true});return;}
    });
    const legendOf=node=>node?.closest?.('[data-stats-legend]')||null;
    root.addEventListener('pointerover',event=>{const chip=legendOf(event.target);if(chip)legendHover(chip.dataset.statsLegend);});
    root.addEventListener('pointerout',event=>{const chip=legendOf(event.target);if(chip&&legendOf(event.relatedTarget)!==chip)legendHover('');});
    root.addEventListener('focusin',event=>{const chip=legendOf(event.target);if(chip)legendHover(chip.dataset.statsLegend);});
    root.addEventListener('focusout',event=>{const chip=legendOf(event.target);if(chip&&legendOf(event.relatedTarget)!==chip)legendHover('');});
    root.addEventListener('change',async event=>{
      if(event.target.id==='statsKind'){if(changeScope({kind:event.target.value,media_uuid:''}))await load();}
      else if(event.target.id==='statsMedia'){if(changeScope({media_uuid:event.target.value}))await load();}
      else if(event.target.id==='statsStartDate'||event.target.id==='statsEndDate'){
        const start=$('statsStartDate')?.value||'',end=$('statsEndDate')?.value||start;
        if(start&&end&&changeScope({start_date:start,end_date:end}))await load();
      }
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
  function updateMediaOptions(payload,scope){
    const media=$('statsMedia');if(!media)return;
    const options=(payload?.media_options||[]).filter(row=>scope.kind==='all'||row.kind===scope.kind);
    const signature=JSON.stringify([scope.kind,options.map(row=>[row.media_uuid,row.title,row.kind])]);
    if(signature!==state.mediaOptionsSignature){
      media.innerHTML=`<option value="">${PudgeI18n.t('stats.allWorks')}</option>`+options.map(row=>`<option value="${esc(row.media_uuid)}">${esc(row.title)} · ${esc(kindLabel(row.kind))}</option>`).join('');
      state.mediaOptionsSignature=signature;
    }
    if(media.value!==scope.media_uuid)media.value=scope.media_uuid||'';
  }
  function renderStatus(){
    const status=$('statsStatus');if(!status)return;
    const stale=!!state.payload&&state.displayedScope&&scopeKey(state.displayedScope)!==scopeKey(state.scope);
    const old=state.displayedScope?.period||'';
    const shown=stale?esc(PudgeI18n.t('stats.showingPeriod',{period:old})):'';
    const progress=state.loading||state.loadingMore?PudgeI18n.t('stats.updating'):'';
    const error=state.error?`<span class="stats-danger">${esc(PudgeI18n.message(String(state.error)))}</span> <button type="button" data-stats-retry>${PudgeI18n.t('media.retry')}</button>`:'';
    status.innerHTML=[shown,progress,error].filter(Boolean).join(' · ');
    status.hidden=!status.innerHTML;
    const body=$('statsBody');if(body)body.setAttribute('aria-busy',state.loading||state.loadingMore?'true':'false');
    const more=document.querySelector('[data-stats-more]');if(more)more.disabled=state.loadingMore;
  }
  async function load({more=false}={}){
    if(!$('statisticsContent')?.querySelector('.stats-shell'))staticShell();
    if(!API()?.consumption_statistics_query){
      // Opened before the bridge exists: wait for it. A bridge without the call is an error
      // the page shows (it used to stay blank).
      if(!API()?.get_state)await window.pudgeWhenBridgeReady?.();
      if(!API()?.consumption_statistics_query)throw new Error('consumption_statistics_query is not available');
    }
    const scope={...state.scope},key=scopeKey(scope),mutation=state.mutationEpoch;
    if(more&&(!state.payload||!state.displayedScope||scopeKey(state.displayedScope)!==key||state.loading||state.loadingMore||!state.journalHasMore))return;
    const offset=more?state.journalOffset:0;
    const flightKey=JSON.stringify([key,mutation,offset,more]);
    if(state.inflight&&state.inflightKey===flightKey)return state.inflight;
    const epoch=++state.requestEpoch;
    if(more)state.loadingMore=true;else{state.loading=true;state.loadingMore=false;}
    state.error='';
    if(!state.payload)render();
    renderStatus();
    const isCurrent=()=>state.requestEpoch===epoch&&state.mutationEpoch===mutation&&scopeKey(state.scope)===key;
    const request=(async()=>{
      try{
        const result=await API().consumption_statistics_query(scope,state.journalLimit,offset);
        if(!isCurrent())return;
        if(more){
          const seen=new Set(state.journalRows.map(row=>`${row.type}:${row.id}`));
          const added=[];
          for(const row of result.journal||[]){const id=`${row.type}:${row.id}`;if(!seen.has(id)){seen.add(id);added.push(row);}}
          state.journalRows.push(...added);
          state.journalOffset=offset+(result.journal||[]).length;
          state.journalHasMore=(result.journal||[]).length>0 && state.journalOffset<Number(result.journal_total||0);
          state.payload={...state.payload,journal:[...state.journalRows],journal_total:result.journal_total};
        }else{
          const pageRows=result.journal||[];
          const seen=new Set();
          state.journalRows=pageRows.filter(row=>{const id=`${row.type}:${row.id}`;if(seen.has(id))return false;seen.add(id);return true;});
          state.journalOffset=pageRows.length;
          state.journalHasMore=state.journalRows.length>0 && state.journalOffset<Number(result.journal_total||0);
          state.payload={...result,journal:[...state.journalRows]};
          state.displayedScope=scope;
          updateMediaOptions(result,scope);
        }
        render();
      }catch(error){if(isCurrent())state.error=String(error?.message||error);}
      finally{
        if(isCurrent()){
          if(more)state.loadingMore=false;else state.loading=false;
          syncControls();renderStatus();
        }
        if(state.inflight===request)state.inflight=null;
      }
    })();
    state.inflight=request;state.inflightKey=flightKey;
    return request;
  }

  function render(){
    const body=$('statsBody');if(!body)return;
    if(!state.payload){
      body.innerHTML=state.loading?`<div class="stats-empty">${PudgeI18n.t('stats.loading')}</div>`:'';
      renderStatus();return;
    }
    if(state.tab==='works')renderWorks(body);else if(state.tab==='journal')renderJournal(body);else renderOverview(body);
    renderStatus();
  }

  // Fixed stacking order (bottom to top); colours per type live in statistics.css.
  const KIND_ORDER=['anime','light_novel','manga','audiobook','visual_novel','light_novel+audiobook','simultaneous','review'];
  const kindRank = kind => {const k=KIND_ORDER.indexOf(kind);return k<0?KIND_ORDER.length:k;};
  const byKindOrder = (a,b) => kindRank(a[0])-kindRank(b[0])||String(a[0]).localeCompare(String(b[0]));
  const dayParts = d => {
    const parts=Object.entries(d.breakdown||{}).map(([kind,seconds])=>[kind,Number(seconds||0)]).filter(([,seconds])=>seconds>0).sort(byKindOrder);
    const rest=Number(d.seconds||0)-parts.reduce((sum,[,seconds])=>sum+seconds,0);
    if(!parts.length&&Number(d.seconds||0)>0)parts.push(['unknown',Number(d.seconds||0)]);
    else if(rest>1)parts.push(['unknown',rest]);
    return parts;
  };
  function barHtml(d,max){
    const total=Number(d.seconds||0),parts=dayParts(d);
    const label=[`${d.day} · ${duration(total)}`,...parts.map(([kind,seconds])=>`${kindLabel(kind)}: ${duration(seconds)}`)].join(', ');
    const segments=parts.map(([kind,seconds])=>`<span class="stats-seg" data-stats-kind="${esc(kind)}" style="height:${(seconds/Math.max(total,1)*100).toFixed(2)}%" title="${esc(`${kindLabel(kind)} · ${duration(seconds)} · ${d.day}`)}"></span>`).join('');
    return `<div class="stats-bar-wrap"><button class="stats-bar" type="button" data-stats-day="${esc(d.day)}" style="height:${Math.max(2,total/max*100)}%" title="${esc(`${d.day} · ${duration(total)}`)}" aria-label="${esc(label)}">${segments}</button><small>${esc(d.day.slice(5))}</small></div>`;
  }
  const number = value => PudgeI18n.number(Math.round(Number(value||0)));
  function highlightsHtml(h){
    if(!h)return '';
    const card=(title,value,note='',hint='')=>`<div class="stats-highlight"${hint?` title="${esc(hint)}"`:''}><span>${esc(title)}</span><strong>${esc(value)}</strong>${note?`<small>${esc(note)}</small>`:''}</div>`;
    const cards=[];
    if(Number(h.longest_streak_days||0)>0)cards.push(card(PudgeI18n.t('stats.fun.streak'),PudgeI18n.tn('stats.daysCount',Number(h.longest_streak_days),{value:number(h.longest_streak_days)}),PudgeI18n.t('stats.fun.streakNote')));
    if(h.busiest_day)cards.push(card(PudgeI18n.t('stats.fun.busiestDay'),duration(h.busiest_day.seconds),h.busiest_day.day));
    if(h.longest_session)cards.push(card(PudgeI18n.t('stats.fun.longestSession'),duration(h.longest_session.seconds),`${h.longest_session.title} · ${kindLabel(h.longest_session.kind)}`,h.longest_session.title));
    if(h.top_work)cards.push(card(PudgeI18n.t('stats.fun.topWork'),h.top_work.title,`${kindLabel(h.top_work.kind)} · ${duration(h.top_work.seconds)}`,h.top_work.title));
    if(Number(h.anime_episodes||0)>0)cards.push(card(PudgeI18n.t('stats.fun.episodes'),number(h.anime_episodes),PudgeI18n.t('stats.fun.episodesNote')));
    if(h.ln_characters_per_minute!=null)cards.push(card(PudgeI18n.t('stats.fun.lnSpeed'),PudgeI18n.t('stats.fun.charsPerMinute',{value:number(h.ln_characters_per_minute)}),PudgeI18n.t('stats.fun.estimate')));
    if(h.manga_pages_per_hour!=null)cards.push(card(PudgeI18n.t('stats.fun.mangaSpeed'),PudgeI18n.t('stats.fun.pagesPerHour',{value:number(h.manga_pages_per_hour)}),PudgeI18n.t('stats.fun.estimate')));
    const words=h.words;
    if(words){
      const notes=[PudgeI18n.t('stats.fun.wordsMetNote')];
      const pending=Number(words.pending||0),missing=Number(words.missing||0);
      if(pending>0)notes.push(PudgeI18n.tn('stats.fun.stillCounting',pending,{value:number(pending)}));
      if(pending>0&&words.job?.paused==='energy_saving')notes.push(PudgeI18n.t('stats.fun.countingPaused'));
      if(missing>0)notes.push(PudgeI18n.tn('stats.fun.withoutText',missing,{value:number(missing)}));
      const note=notes.join(' · ');
      cards.push(card(PudgeI18n.t('stats.fun.wordsMet'),words.total==null?'—':`≈ ${number(words.total)}`,note,note));
      if(words.total!=null){
        cards.push(card(PudgeI18n.t('stats.fun.uniqueWords'),`≈ ${number(words.unique)}`,PudgeI18n.t('stats.fun.estimate')));
        cards.push(card(PudgeI18n.t('stats.fun.newWords'),`≈ ${number(words.new)}`,PudgeI18n.t('stats.fun.newWordsNote')));
      }
      const kinds=Object.entries(words.by_kind||{}).sort(byKindOrder);
      if(kinds.length)cards.push(`<div class="stats-highlight stats-highlight-wide"><span>${esc(PudgeI18n.t('stats.fun.wordsByKind'))}</span>${kinds.map(([kind,row])=>`<small data-stats-kind="${esc(kind)}"><i class="stats-swatch" aria-hidden="true"></i>${esc(PudgeI18n.t('stats.fun.wordsKindRow',{kind:kindLabel(kind),total:number(row.total),unique:number(row.unique),new:number(row.new)}))}</small>`).join('')}</div>`);
    }
    return cards.length?`<section class="stats-highlights" aria-label="${esc(PudgeI18n.t('stats.fun.title'))}">${cards.join('')}</section>`:'';
  }

  function applyLegendFocus(){
    const kind=state.legendHover||state.legendPinned||'';
    const chart=document.querySelector('#statsBody .stats-chart');
    if(chart){
      if(kind)chart.setAttribute('data-stats-focus',kind);else chart.removeAttribute('data-stats-focus');
      chart.querySelectorAll('.stats-seg').forEach(seg=>seg.classList.toggle('stats-seg-hidden',!!kind&&seg.dataset.statsKind!==kind));
    }
    document.querySelectorAll('#statsBody [data-stats-legend]').forEach(chip=>{
      chip.classList.toggle('stats-chip-dim',!!kind&&chip.dataset.statsLegend!==kind);
      chip.setAttribute('aria-pressed',chip.dataset.statsLegend===state.legendPinned?'true':'false');
    });
  }
  function legendHover(kind){
    if(state.legendHover===kind)return;
    state.legendHover=kind;applyLegendFocus();
  }

  function renderOverview(body){
    const p=state.payload,s=p.summary||{},days=p.days||[],max=Math.max(1,...days.map(d=>Number(d.seconds||0)));
    const timezoneNote=PudgeI18n.t('stats.timezoneNote',{zone:p.timezone?.label||PudgeI18n.t('stats.localZone')});
    let chart='';
    if((state.displayedScope||state.scope).period==='year'){
      chart=`<div class="stats-calendar">${days.map(d=>`<button class="stats-day" data-stats-day="${esc(d.day)}" style="--heat:${Math.max(.08,Number(d.seconds||0)/max).toFixed(3)}" title="${esc([`${d.day} · ${duration(d.seconds)}`,...dayParts(d).map(([kind,seconds])=>`${kindLabel(kind)}: ${duration(seconds)}`)].join('\n'))}"></button>`).join('')}</div>`;
    }else{
      chart=`<div class="stats-chart" role="group" aria-label="${esc(PudgeI18n.t('stats.chartLabel'))}" style="--stats-columns:${Math.max(1,days.length)}">${days.length?days.map(d=>barHtml(d,max)).join(''):`<div class="stats-empty">${PudgeI18n.t('stats.noActivity')}</div>`}</div>`;
    }
    // The time chips are the chart's legend: same types, same colours.
    // The time chips are the legend: hovering or focusing one shows only that type in the
    // chart; a click (or tap) keeps it shown until clicked again.
    const breakdown=Object.entries(p.breakdown||{}).filter(([,seconds])=>Number(seconds||0)>0).sort(byKindOrder).map(([kind,seconds])=>`<button type="button" class="stats-chip" data-stats-kind="${esc(kind)}" data-stats-legend="${esc(kind)}" aria-pressed="false"><i class="stats-swatch" aria-hidden="true"></i>${esc(kindLabel(kind))}: ${esc(duration(seconds))}</button>`).join('');
    // Amounts only (no repeats), and only for types with something done in the period.
    const native=Object.values(p.volume?.by_kind||{}).filter(row=>row.exact&&Number(row.consumed||0)>0).sort((a,b)=>kindRank(a.kind)-kindRank(b.kind)).map(row=>`<span class="stats-chip">${esc(kindLabel(row.kind))}: ${esc(nativeNumber(row.consumed,row.unit))}</span>`).join('');
    const coverageNote=p.volume?.coverage_started_at_utc?PudgeI18n.t('stats.coverageNote') : '';
    body.innerHTML=`<div class="stats-shell"><div class="stats-cards"><div class="stats-card"><span class="stats-muted">${PudgeI18n.t('stats.timeWithContent')}</span><strong>${esc(duration(s.total_seconds))}</strong></div><div class="stats-card"><span class="stats-muted">${PudgeI18n.t('stats.activeDays')}</span><strong>${Number(s.active_days||0)}</strong></div><div class="stats-card"><span class="stats-muted">${PudgeI18n.t('stats.sessions')}</span><strong>${Number(s.sessions||0)}</strong></div></div><div class="stats-muted stats-note">${esc(timezoneNote)}${coverageNote?` · ${esc(coverageNote)}`:''}${Number(s.manual_unplaced_seconds||0)>0?` · ${esc(PudgeI18n.t('stats.manualWithoutTimeValue', {duration: duration(s.manual_unplaced_seconds)}))}`:''}</div>${chart}${breakdown?`<div class="stats-breakdown stats-legend" role="group" aria-label="${esc(PudgeI18n.t('stats.legend'))}">${breakdown}</div>`:''}${native?`<div class="stats-breakdown">${native}</div>`:''}${highlightsHtml(p.highlights)}</div>`;
    if(state.legendPinned&&!(p.breakdown||{})[state.legendPinned])state.legendPinned='';
    state.legendHover='';
    applyLegendFocus();
  }

  const cell = (label, value, cls='') => `<td${cls?` class="${cls}"`:''}${label?` data-label="${esc(label)}"`:''}>${value}</td>`;
  function renderWorks(body){
    const rows=state.payload.works||[];
    const h={work:PudgeI18n.t('stats.work'),format:PudgeI18n.t('stats.format'),time:PudgeI18n.t('ui.time'),volume:PudgeI18n.t('stats.volumeCoverage'),last:PudgeI18n.t('stats.lastActivity')};
    body.innerHTML=`<div class="stats-panel">${rows.length?`<table class="stats-table"><thead><tr><th>${h.work}</th><th>${h.format}</th><th>${h.time}</th><th>${h.volume}</th><th>${h.last}</th></tr></thead><tbody>${rows.map(row=>`<tr>${cell('',esc(row.title),'stats-title')}${cell(h.format,esc(kindLabel(row.kind)))}${cell(h.time,esc(duration(row.seconds)))}${cell(h.volume,esc(workVolumeText(row.volume)))}${cell(h.last,esc(new Date(Number(row.last_at_utc||0)*1000).toLocaleString()),'stats-when')}</tr>`).join('')}</tbody></table>`:`<div class="stats-empty">${PudgeI18n.t('stats.noWorks')}</div>`}</div>`;
  }

  function renderJournal(body){
    const rows=state.payload.journal||[];
    const h={start:PudgeI18n.t('stats.start'),end:PudgeI18n.t('stats.end'),time:PudgeI18n.t('ui.time'),work:PudgeI18n.t('stats.work'),format:PudgeI18n.t('stats.format'),source:PudgeI18n.t('table.source'),device:PudgeI18n.t('stats.device')};
    body.innerHTML=`<div class="stats-panel">${rows.length?`<table class="stats-table"><thead><tr><th>${h.start}</th><th>${h.end}</th><th>${h.time}</th><th>${h.work}</th><th>${h.format}</th><th>${h.source}</th><th>${h.device}</th><th></th></tr></thead><tbody>${rows.map(row=>`<tr class="${row.excluded?'excluded':''}">${cell(h.start,row.start_utc==null?PudgeI18n.t('stats.noExactTime'):esc(new Date(Number(row.start_utc)*1000).toLocaleString()),'stats-when')}${cell(h.end,row.end_utc==null?'—':esc(new Date(Number(row.end_utc)*1000).toLocaleString()),'stats-when')}${cell(h.time,esc(duration(row.seconds)))}${cell('',esc(row.title),'stats-title')}${cell(h.format,esc(kindLabel(row.kind)))}${cell(h.source,esc(row.origin==='manual'?PudgeI18n.t('stats.manual'):(row.estimated?PudgeI18n.t('stats.estimated'):PudgeI18n.t('stats.automatic'))))}${cell(h.device,esc(row.device||''))}${cell('',`<button data-stats-edit="${esc(row.id)}" data-stats-type="${esc(row.type)}">${PudgeI18n.t('stats.edit')}</button>`,'stats-row-actions')}</tr>`).join('')}</tbody></table>${state.journalHasMore&&Number(state.payload.journal_total||0)>state.journalOffset?`<div style="padding:12px"><button data-stats-more>${PudgeI18n.t('stats.more')}</button></div>`:''}`:`<div class="stats-empty">${PudgeI18n.t('stats.journalEmpty')}</div>`}</div>`;
  }

  function closeModal(){const host=$('statsModalHost');if(host)host.innerHTML='';}
  function modal(content){const host=$('statsModalHost');if(!host)return;host.innerHTML=`<div class="stats-modal-backdrop"><div class="stats-modal">${content}</div></div>`;}
  function openManual(){
    const options=(state.payload?.media_options||[]).map(row=>`<option value="${esc(row.media_uuid)}" data-kind="${esc(row.kind)}">${esc(row.title)} · ${esc(kindLabel(row.kind))}</option>`).join('');
    modal(`<h3>${PudgeI18n.t('stats.addActivity')}</h3><label>${PudgeI18n.t('stats.format')}<select id="statsManualKind">${['anime','light_novel','manga','audiobook','visual_novel'].map(v=>`<option value="${v}">${kindLabel(v)}</option>`).join('')}</select></label><label>${PudgeI18n.t('stats.existingWorkOptional')}<select id="statsManualMedia"><option value="">—</option>${options}</select></label><label>${PudgeI18n.t('stats.newWorkTitle')}<input id="statsManualTitle"></label><label>${PudgeI18n.t('stats.startTimeOptional')}<input id="statsManualStart" type="datetime-local"></label><label>${PudgeI18n.t('stats.durationMinutesLabel')}<input id="statsManualMinutes" type="number" min="0.1" step="0.1" value="30"></label><label>${PudgeI18n.t('stats.note')}<textarea id="statsManualNote"></textarea></label><footer><button data-stats-modal-close>${PudgeI18n.t('action.cancel')}</button><button class="primary" data-stats-save-manual>${PudgeI18n.t('action.save')}</button></footer>`);
  }
  async function saveManualEntry(button){
    button.disabled=true;try{const media=$('statsManualMedia')?.value||'',selected=$('statsManualMedia')?.selectedOptions?.[0],selectedKind=selected?.dataset?.kind;const kind=media?(selectedKind||$('statsManualKind')?.value):$('statsManualKind')?.value;await API().consumption_statistics_add_manual(kind,Number($('statsManualMinutes')?.value||0)*60,epochFromLocalInput($('statsManualStart')?.value),media,$('statsManualTitle')?.value||'',$('statsManualNote')?.value||'');closeModal();invalidateAfterMutation();await load();}catch(error){window.toast?.(String(error?.message||error));button.disabled=false;}
  }
  function openEdit(id,type){
    const row=(state.payload?.journal||[]).find(item=>String(item.id)===String(id)&&String(item.type)===String(type));if(!row)return;
    modal(`<h3>${PudgeI18n.t('stats.correctEntry')}</h3><div><strong>${esc(row.title)}</strong><div class="stats-muted">${esc(kindLabel(row.kind))} · ${esc(duration(row.seconds))}</div></div><label>${PudgeI18n.t('stats.start')}<input id="statsEditStart" type="datetime-local" value="${esc(localDateTimeValue(row.start_utc))}"></label><label>${PudgeI18n.t('stats.end')}<input id="statsEditEnd" type="datetime-local" value="${esc(localDateTimeValue(row.end_utc))}"></label><label><span><input id="statsEditExcluded" type="checkbox" ${row.excluded?'checked':''}> ${PudgeI18n.t('stats.exclude')}</span></label><label>${PudgeI18n.t('stats.reason')}<input id="statsEditReason" value="${esc(row.correction_reason||'')}"></label><footer><button data-stats-modal-close>${PudgeI18n.t('action.cancel')}</button><button class="primary" data-stats-save-edit data-id="${esc(row.id)}" data-type="${esc(row.type)}" data-revision="${Number(row.correction_revision||0)}">${PudgeI18n.t('stats.saveCorrection')}</button></footer>`);
  }
  async function saveCorrection(button){
    button.disabled=true;try{const start=epochFromLocalInput($('statsEditStart')?.value),end=epochFromLocalInput($('statsEditEnd')?.value);await API().consumption_statistics_correct(button.dataset.type,button.dataset.id,Number(button.dataset.revision||0),!!$('statsEditExcluded')?.checked,start,end,null,$('statsEditReason')?.value||'');closeModal();invalidateAfterMutation();await load();}catch(error){window.toast?.(String(error?.message||error));button.disabled=false;}
  }
  async function exportCsv(){
    try{const result=await API().consumption_statistics_export({...state.scope});window.toast?.(PudgeI18n.tn('stats.exportedRows',Number(result.rows||0)));}catch(error){window.toast?.(String(error?.message||error));}
  }

  async function deleteHistory(){
    if(!await window.pudgeConfirm?.(PudgeI18n.t('stats.deleteHistoryConfirm'),{danger:true}))return;
    try{await API().consumption_statistics_delete_history();invalidateAfterMutation();await load();window.toast?.(PudgeI18n.t('stats.historyDeleted'));}catch(error){window.toast?.(String(error?.message||error));}
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
