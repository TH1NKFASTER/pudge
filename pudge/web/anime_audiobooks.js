// Anime episodes made into audiobooks (pudge/anime_audiobooks.py): the anime menu's
// "Make audiobook…" dialog (one row per downloaded episode, progress and Cancel) and
// "Make audiobook from all episodes" (shown when every released episode is downloaded).
(() => {
  'use strict';
  const g = globalThis;
  const tr = (key, params) => g.PudgeI18n.t(key, params || {});
  // The page's escapeHtml is a top-level const of the inline script: it is no property of
  // globalThis, so asking for g.escapeHtml found nothing and every value went in unescaped
  // (a failed job's error carries FFmpeg's message with the video's file name).
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const byId = id => g.document.getElementById(id);
  const state = { mediaId: null, token: 0, timer: null, lastJob: '', unfinished: null, observer: null, hooked: false };

  function releasedCount(a) {
    return typeof g.planningReleasedEpisodeCountForAnime === 'function' ? g.planningReleasedEpisodeCountForAnime(a) : 0;
  }

  function hasLocal(a) {
    return Number(a?.local_count || 0) > 0 || !!a?.local?.video_path;
  }

  // Menu buttons of an anime card that has downloaded episodes.
  function menuItems(a) {
    if (!hasLocal(a)) return '';
    const released = releasedCount(a);
    const all = released > 0 && Number(a?.local_count || 0) >= released
      ? `<button data-context-action="anime-audiobook-all">${esc(tr('action.makeAudiobookAll'))}</button>` : '';
    return `<button data-context-action="anime-audiobook">${esc(tr('action.makeAudiobook'))}</button>${all}`;
  }

  function jobLine(job) {
    if (!job) return '';
    const running = job.state === 'running';
    const percent = Math.round(Math.max(0, Math.min(1, Number(job.progress || 0))) * 100);
    const message = job.state === 'failed' && job.error ? `${job.message}: ${g.PudgeI18n.message(job.error)}` : job.message;
    const bar = running ? `<progress max="100" value="${percent}"></progress> <span>${percent}%</span>` : '';
    const cancel = running ? ` <button type="button" data-anime-audiobook-cancel>${esc(tr('action.cancel'))}</button>` : '';
    return `<div class="anime-audiobook-job ${esc(job.state)}"><div>${esc(message || '')}</div>${bar}${cancel}</div>`;
  }

  function render(status) {
    const body = byId('modalBody');
    if (!body || state.mediaId !== Number(status.media_id)) return;
    const busy = !!status.busy;
    const unfinished = Array.isArray(status.unfinished) ? status.unfinished.length : 0;
    state.unfinished = unfinished && !busy ? Number(status.media_id) : null;
    watchClose();
    const skip = byId('animeAudiobookSkipOpEd');
    const skipChecked = skip ? skip.checked : true;
    const rows = (status.episodes || []).map(row => {
      const label = row.episode_end ? tr('animeAudiobook.episodesChapter', { first: row.episode, last: row.episode_end }) : tr('animeAudiobook.episodeChapter', { episode: row.episode });
      const notes = [];
      if (row.audiobook_id) notes.push(tr('animeAudiobook.ready'));
      else if (row.made) notes.push(tr('animeAudiobook.madeNoBook'));
      if (!row.local) notes.push(tr('animeAudiobook.videoDeleted'));
      else notes.push(row.subtitles ? tr('animeAudiobook.withText') : tr('animeAudiobook.noText'));
      const button = row.local
        ? `<button type="button" data-anime-audiobook-episode="${Number(row.episode)}" ${busy ? 'disabled' : ''}>${esc(tr(row.audiobook_id ? 'animeAudiobook.remake' : 'animeAudiobook.make'))}</button>` : '';
      return `<div class="release-row"><div class="release-main"><strong>${esc(label)}</strong><span>${esc(notes.join(' · '))}</span></div>${button}</div>`;
    }).join('');
    const localCount = (status.episodes || []).filter(row => row.local).length;
    const whole = status.all_local
      ? `<button type="button" class="primary" data-anime-audiobook-all ${busy ? 'disabled' : ''}>${esc(tr('animeAudiobook.wholeButton', { count: status.released }))}</button>${status.whole_audiobook_id ? ` <span class="subtle">${esc(tr('animeAudiobook.wholeReady'))}</span>` : ''}`
      : (status.released > 0 ? `<p class="subtle">${esc(tr('animeAudiobook.notAllLocal', { local: localCount, total: status.released }))}</p>` : '');
    body.innerHTML = `<div data-anime-audiobook-root="${Number(status.media_id)}" data-modal-request="${state.token}">`
      + `<p class="subtle">${esc(tr('animeAudiobook.hint'))}</p>`
      + `<label class="anime-audiobook-skip"><input type="checkbox" id="animeAudiobookSkipOpEd" ${skipChecked ? 'checked' : ''}> ${esc(tr('animeAudiobook.skipOpEd'))}</label>`
      + jobLine(status.job)
      + (unfinished && !busy ? `<p class="subtle" data-anime-audiobook-unfinished>${esc(tr('animeAudiobook.unfinishedHint', { count: unfinished }))}</p>` : '')
      + `<div class="setting-inline-actions">${whole}</div>`
      + (rows ? `<div class="release-list">${rows}</div>` : `<div class="empty">${esc(tr('animeAudiobook.noLocal'))}</div>`)
      + '</div>';
    const job = status.job;
    const key = job ? `${job.state}:${job.updated_at}` : '';
    if (job && job.state !== 'running' && state.lastJob && state.lastJob.startsWith('running') && key !== state.lastJob) {
      g.toast?.(job.state === 'failed' ? `${job.message}${job.error ? ': ' + g.PudgeI18n.message(job.error) : ''}` : job.message);
    }
    state.lastJob = key;
    schedule(busy);
  }

  function open() {
    const backdrop = byId('modalBackdrop');
    return !!backdrop?.classList.contains('open') && !!byId('modalBody')?.querySelector(`[data-anime-audiobook-root="${state.mediaId}"]`);
  }

  // Episodes a failed "whole anime" run made belong to no book. The dialog lists them and
  // "Make" again continues from them; closed without that, they are removed from the disk.
  function discard() {
    const mediaId = state.unfinished;
    state.unfinished = null;
    if (mediaId == null) return;
    try { Promise.resolve(g.pywebview?.api?.anime_audiobook_discard?.(mediaId)).catch(() => {}); } catch (_) { /* the next open lists them again */ }
  }

  function watchClose() {
    if (!state.hooked && typeof g.closeModal === 'function') {
      // The page's closeModal (the close button, Escape, a click beside the dialog).
      const closeModal = g.closeModal;
      state.hooked = true;
      g.closeModal = function (...args) {
        const result = closeModal.apply(this, args);
        if (state.unfinished != null) discard();
        return result;
      };
    }
    // Another dialog drawn over this one closes it as well.
    if (state.observer || typeof g.MutationObserver !== 'function') return;
    const backdrop = byId('modalBackdrop'), body = byId('modalBody');
    if (!backdrop || !body) return;
    state.observer = new g.MutationObserver(() => { if (state.unfinished != null && !open()) discard(); });
    state.observer.observe(backdrop, { attributes: true, attributeFilter: ['class', 'hidden'] });
    state.observer.observe(body, { childList: true });
  }

  function schedule(busy) {
    if (state.timer) g.clearTimeout(state.timer);
    state.timer = null;
    if (busy) state.timer = g.setTimeout(refresh, 1000);
  }

  async function refresh() {
    state.timer = null;
    if (state.mediaId == null || !open()) return;
    try { render(await g.pywebview.api.anime_audiobook_status(state.mediaId)); } catch (error) { schedule(true); }
  }

  async function show(a) {
    if (state.unfinished != null && state.unfinished !== Number(a.media_id)) discard();
    state.mediaId = Number(a.media_id);
    state.lastJob = '';
    state.token = typeof g.beginModalRequest === 'function' ? g.beginModalRequest() : 0;
    const backdrop = byId('modalBackdrop');
    backdrop.hidden = false;
    backdrop.classList.remove('watch-order-open');
    byId('modalTitle').textContent = tr('animeAudiobook.modalTitle', { title: a.title || '' });
    backdrop.classList.add('open');
    byId('modalBody').innerHTML = `<div class="empty" data-anime-audiobook-root="${state.mediaId}"><span class="spinner"></span></div>`;
    render(await g.pywebview.api.anime_audiobook_status(state.mediaId));
  }

  async function start(options) {
    const skip = byId('animeAudiobookSkipOpEd');
    state.unfinished = null; // made again: the files are used
    const status = await g.pywebview.api.anime_audiobook_start(state.mediaId, options.episode ?? null, !!options.whole, skip ? skip.checked : true);
    state.lastJob = 'running';
    g.toast?.(tr('animeAudiobook.started'));
    render(status);
  }

  async function handle(action, a) {
    if (action === 'anime-audiobook') { await show(a); return; }
    if (action === 'anime-audiobook-all') { await show(a); if (open()) await start({ whole: true }); }
  }

  g.document.addEventListener('click', async event => {
    const target = event.target?.closest?.('[data-anime-audiobook-episode],[data-anime-audiobook-all],[data-anime-audiobook-cancel]');
    if (!target || state.mediaId == null) return;
    event.preventDefault();
    target.disabled = true;
    try {
      if (target.hasAttribute('data-anime-audiobook-cancel')) render(await g.pywebview.api.anime_audiobook_cancel(state.mediaId));
      else if (target.hasAttribute('data-anime-audiobook-all')) await start({ whole: true });
      else await start({ episode: Number(target.dataset.animeAudiobookEpisode) });
    } catch (error) {
      target.disabled = false;
      g.toast?.(tr('error.generic', { error: g.PudgeI18n.message(error?.message || error) }));
    }
  });

  g.PudgeAnimeAudiobooks = { menuItems, handle, render, show, discard };
})();
