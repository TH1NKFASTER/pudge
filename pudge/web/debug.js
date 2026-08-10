'use strict';

(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
  let currentAnime = null;
  let currentData = null;
  let activeTab = 'summary';

  const labels = () => ru() ? {
    title:'Debug аниме', close:'Закрыть', refresh:'Обновить', copy:'Копировать JSON',
    export:'Экспорт JSON', summary:'Сводка', video:'Видеофайл', subtitles:'Субтитры',
    pipeline:'Pipeline', performance:'Производительность', raw:'Raw JSON',
    copied:'Debug JSON скопирован', exported:'Debug JSON сохранён',
    fresh:'Удалить текущие и подобрать заново', freshConfirm:'Удалить текущий выбор субтитров и запустить полностью свежий поиск/синхронизацию?',
  } : {
    title:'Anime Debug', close:'Close', refresh:'Refresh', copy:'Copy JSON',
    export:'Export JSON', summary:'Summary', video:'Video file', subtitles:'Subtitles',
    pipeline:'Pipeline', performance:'Performance', raw:'Raw JSON',
    copied:'Debug JSON copied', exported:'Debug JSON exported',
    fresh:'Delete current & reselect fresh', freshConfirm:'Delete the current subtitle selection and run a fully fresh search/sync?',
  };

  const ensure = () => {
    let root = document.getElementById('pudgeDebugOverlay');
    if (root) return root;
    root = document.createElement('section');
    root.id = 'pudgeDebugOverlay';
    root.className = 'pudge-debug-overlay';
    root.innerHTML = '<div class="pudge-debug-head"><button data-debug-action="close"></button><h2 id="pudgeDebugTitle"></h2><span id="pudgeDebugEpisode" class="pudge-debug-badge"></span><span class="spacer"></span><button data-debug-action="refresh"></button><button data-debug-action="copy"></button><button data-debug-action="export"></button></div><div id="pudgeDebugTabs" class="pudge-debug-tabs"></div><div id="pudgeDebugBody" class="pudge-debug-body"></div>';
    document.body.append(root);
    root.addEventListener('click', event => {
      const tab = event.target.closest('[data-debug-tab]')?.dataset.debugTab;
      if (tab) {
        activeTab = tab;
        render();
        return;
      }

      const action = event.target.closest('[data-debug-action]')?.dataset.debugAction;
      if (!action) return;
      if (action === 'close') close();
      if (action === 'refresh') void load();
      if (action === 'copy') void copy();
      if (action === 'export') void exportJson();
      if (action === 'fresh-subtitles') void freshSubtitles();
    });
    return root;
  };

  const kv = object => {
    const rows = Object.entries(object || {}).filter(([,value]) => value !== undefined && value !== null && value !== '');
    return `<dl class="pudge-debug-kv">${rows.map(([key,value]) => `<dt>${esc(key)}</dt><dd>${esc(typeof value === 'object' ? JSON.stringify(value) : value)}</dd>`).join('')}</dl>`;
  };

  const pre = value => `<pre class="pudge-debug-pre">${esc(typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2))}</pre>`;

  const renderSummary = data => {
    const anime = data.anime || {}, local = data.selected_local_episode || {}, summary = data.summary || {};
    return `<div class="pudge-debug-grid"><article class="pudge-debug-card"><h3>Anime</h3>${kv({media_id:anime.media_id,title:anime.title,status:anime.status,progress:anime.progress,episodes:anime.episodes,format:anime.format,media_status:anime.media_status})}</article><article class="pudge-debug-card"><h3>Local episode</h3>${kv({episode:data.selected_episode,state:local.state,video_path:local.video_path,subtitle_path:local.subtitle_path,subtitle_origin:local.subtitle_origin,embedded_sid:local.embedded_subtitle_id,torrent_hash:local.torrent_hash})}</article><article class="pudge-debug-card"><h3>Diagnosis</h3>${pre(summary.diagnosis || {})}</article><article class="pudge-debug-card"><h3>Captured data</h3>${kv({subtitle_jobs:summary.job_count,downloads:summary.download_count,prepare_trace:summary.has_prepare_trace,prepare_result:summary.has_prepare_result})}</article></div>`;
  };

  const renderVideo = data => {
    const section = data.video_selection || {}, decision = section.decision || {};
    const candidates = decision.candidates || [];
    const table = candidates.length ? `<table class="pudge-debug-table"><thead><tr><th>Selected</th><th>Title</th><th>Score</th><th>Seeders</th><th>Group</th><th>Reasons</th></tr></thead><tbody>${candidates.map(row => `<tr><td>${decision.selected?.info_hash && decision.selected.info_hash === row.info_hash ? '✓' : ''}</td><td>${esc(row.title)}</td><td>${esc(row.score)}</td><td>${esc(row.seeders)}</td><td>${esc(row.group)}</td><td>${esc((row.reasons||[]).join(', '))}</td></tr>`).join('')}</tbody></table>` : '<div class="pudge-debug-muted">No persisted candidate ranking yet. Runtime evidence is shown below.</div>';
    return `<article class="pudge-debug-card"><h3>Selection decision</h3>${kv({mode:decision.mode,threshold:decision.threshold,updated_at:decision.updated_at,selected:decision.selected?.title})}${table}</article><article class="pudge-debug-card"><h3>Download records</h3>${pre(section.downloads||[])}</article><article class="pudge-debug-card"><h3>Relevant runtime lines</h3>${pre((section.runtime_lines||[]).join('\n'))}</article>`;
  };

  const renderSubtitles = data => {
    const section = data.subtitle_selection || {}, result = section.prepare_result || {}, meta = result.subtitle_meta || section.latest_history?.details || {};
    const timeline=section.current_timeline_attempt||{},timelineResult=timeline.result||{};
    const historyTitle=section.history_is_current?'Current subtitle history':'Previous subtitle history';
    const timelineCard=Object.keys(timeline).length?`<article class="pudge-debug-card"><h3>Current timeline attempt</h3>${kv({stage:timeline.stage,reason:timelineResult.reason,accepted:timelineResult.accepted,engine:timelineResult.engine,algorithm:timelineResult.timeline_algorithm})}${pre({signals:timelineResult.timeline_signal_counts,segments:timelineResult.timeline_segments||timelineResult.segments,boundaries:timelineResult.timeline_boundaries||timelineResult.boundaries,cold_start:timelineResult.timeline_cold_start||timelineResult.cold_start,validation:timelineResult.timeline_validation||{before:timelineResult.before,after:timelineResult.after,activity_f1:timelineResult.activity_f1,holdout:timelineResult.holdout}})}</article>`:'';
    return `<div class="setting-inline-actions" style="margin-bottom:12px"><button class="primary" data-debug-action="fresh-subtitles">${esc(labels().fresh)}</button></div><div class="pudge-debug-grid"><article class="pudge-debug-card"><h3>Selected subtitle</h3>${kv(section.selected||{})}</article><article class="pudge-debug-card"><h3>Final prepare</h3>${kv({status:result.prepare_status,returncode:result.returncode,duration_ms:result.duration_ms,source:meta.source,name:meta.name,score:meta.score,engine:meta.engine,quality:meta.quality})}</article>${timelineCard}<article class="pudge-debug-card"><h3>${historyTitle}</h3>${pre(section.latest_history||{})}</article><article class="pudge-debug-card"><h3>Current/persisted jobs</h3>${pre(section.jobs||[])}</article></div><article class="pudge-debug-card"><h3>Timeline attempts in this prepare</h3>${pre(section.timeline_attempts||[])}</article><article class="pudge-debug-card"><h3>Relevant runtime lines</h3>${pre((section.runtime_lines||[]).join('\n'))}</article><article class="pudge-debug-card"><h3>Prepare stdout/stderr</h3>${pre([result.stdout||'',result.stderr||''].filter(Boolean).join('\n\n--- STDERR ---\n'))}</article>`;
  };

  const renderPipeline = data => {
    const stages = data.pipeline?.stages || [];
    const table = `<table class="pudge-debug-table"><thead><tr><th>Stage</th><th>Duration</th><th>CPU proxy</th><th>RSS</th><th>Details</th></tr></thead><tbody>${stages.map(row => `<tr><td>${esc(row.stage)}</td><td>${row.duration_ms == null ? '—' : `${esc(row.duration_ms)} ms`}</td><td>${row.cpu_activity_proxy_percent == null ? '—' : `${esc(row.cpu_activity_proxy_percent)}%`}</td><td>${row.rss_mb == null ? '—' : `${esc(row.rss_mb)} MB`}</td><td>${esc(JSON.stringify(row.details||{}))}</td></tr>`).join('')}</tbody></table>`;
    return `<article class="pudge-debug-card">${stages.length ? table : '<div class="pudge-debug-muted">No stage trace has been captured for this episode yet.</div>'}</article><article class="pudge-debug-card"><h3>Raw stage trace</h3>${pre(data.pipeline?.trace||[])}</article>`;
  };

  const renderPerformance = data => {
    const p = data.performance || {}, timings = p.runtime_timings || [];
    return `<article class="pudge-debug-card"><h3>Measurement</h3><p class="pudge-debug-muted">${esc(p.metric_note||'')}</p>${kv({prepare_duration_ms:p.prepare_duration_ms})}</article><article class="pudge-debug-card"><h3>Runtime timings</h3><table class="pudge-debug-table"><thead><tr><th>Step</th><th>Duration</th></tr></thead><tbody>${timings.map(row=>`<tr><td>${esc(row.step)}</td><td>${esc(row.duration_ms)} ms</td></tr>`).join('')}</tbody></table></article>${renderPipeline(data)}`;
  };

  const render = () => {
    const root = ensure(), l = labels(), body = root.querySelector('#pudgeDebugBody');
    root.querySelector('[data-debug-action="close"]').textContent = l.close;
    root.querySelector('[data-debug-action="refresh"]').textContent = l.refresh;
    root.querySelector('[data-debug-action="copy"]').textContent = l.copy;
    root.querySelector('[data-debug-action="export"]').textContent = l.export;
    root.querySelector('#pudgeDebugTitle').textContent = `${l.title}: ${currentAnime?.title || currentData?.anime?.title || ''}`;
    root.querySelector('#pudgeDebugEpisode').textContent = currentData?.selected_episode == null ? '' : `#${currentData.selected_episode}`;
    const tabs = [['summary',l.summary],['video',l.video],['subtitles',l.subtitles],['pipeline',l.pipeline],['performance',l.performance],['raw',l.raw]];
    root.querySelector('#pudgeDebugTabs').innerHTML = tabs.map(([key,label]) => `<button data-debug-tab="${key}" class="${activeTab===key?'active':''}">${label}</button>`).join('');
    if (!currentData) { body.innerHTML = '<div class="empty">Loading…</div>'; return; }
    if (activeTab === 'video') body.innerHTML = renderVideo(currentData);
    else if (activeTab === 'subtitles') body.innerHTML = renderSubtitles(currentData);
    else if (activeTab === 'pipeline') body.innerHTML = renderPipeline(currentData);
    else if (activeTab === 'performance') body.innerHTML = renderPerformance(currentData);
    else if (activeTab === 'raw') body.innerHTML = pre(currentData);
    else body.innerHTML = renderSummary(currentData);
  };

  let livePollTimer = null;
  const stopLivePoll = () => { if (livePollTimer) clearTimeout(livePollTimer); livePollTimer = null; };
  const load = async (showLoading=true) => {
    if (!currentAnime?.media_id) return;
    if (showLoading) { currentData = null; render(); }
    try { currentData = await pywebview.api.anime_debug_snapshot(Number(currentAnime.media_id), null); }
    catch (error) { currentData = {anime: currentAnime, summary:{diagnosis:{error:String(error?.message||error)}}}; }
    render();
  };
  const pollFreshRun = async () => {
    await load(false);
    const jobs=currentData?.subtitle_selection?.jobs||[],path=String(currentData?.selected_local_episode?.video_path||'');
    const running=jobs.some(job=>String(job.video_path||'')===path);
    if(running)livePollTimer=setTimeout(()=>void pollFreshRun(),800);else livePollTimer=null;
  };
  const freshSubtitles = async () => {
    const path=currentData?.selected_local_episode?.video_path;if(!path||!confirm(labels().freshConfirm))return;
    stopLivePoll();await pywebview.api.debug_reselect_subtitles(path);activeTab='subtitles';livePollTimer=setTimeout(()=>void pollFreshRun(),250);
  };

  const open = async anime => {
    currentAnime = anime; activeTab = 'summary';
    ensure().classList.add('open');
    await load();
  };
  const close = () => { stopLivePoll(); ensure().classList.remove('open'); };
  const copy = async () => {
    if (!currentData) return;
    await navigator.clipboard.writeText(JSON.stringify(currentData, null, 2));
    window.toast?.(labels().copied);
  };
  const exportJson = async () => {
    if (!currentAnime?.media_id) return;
    const result = await pywebview.api.export_anime_debug_snapshot(Number(currentAnime.media_id), null);
    window.toast?.(`${labels().exported}: ${result.path||''}`);
  };

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.getElementById('pudgeDebugOverlay')?.classList.contains('open')) {
      event.preventDefault(); close();
    }
  });
  window.PudgeDebug = {open, close};
})();
