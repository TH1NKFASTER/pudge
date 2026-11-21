// Statistics, Overview: the card "Met often, not in deck" (pudge/met_words.py).
// Words you do not know ranked by how many times you met them in the selected period,
// with where, and "Add checked words to the deck" (PudgeWordLists, episode_words.js).
// The first answer uses only what this Mac keeps (no Jiten request); then one batched
// check asks Jiten about the words whose state is not kept and the ones shown.
(() => {
  'use strict';
  const g = globalThis;
  const tr = (key, params) => g.PudgeI18n.t(key, params || {});
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const api = () => g.pywebview?.api;
  const PAGE = 50, HIDE_KEY = 'pudge.metWords.hideNames', MAX_CHECKS = 3;
  const stored = () => { try { return g.localStorage?.getItem(HIDE_KEY) === '1'; } catch (_) { return false; } };
  const state = { key: '', scope: null, limit: PAGE, hideNames: stored(), data: null, words: [], decks: null, loading: false, checking: false, checks: 0, epoch: 0, error: '', loadedAt: 0 };

  const host = () => g.document.getElementById('statsMetWords');
  const wordKey = word => `${Number(word.word_id)}:${Number(word.reading_index)}`;

  function listHtml() {
    return g.PudgeWordLists.rowsHtml(state.words, {checked: false});
  }
  function renderList() {
    const list = host()?.querySelector('[data-met-words-list]');
    if (!list) return;
    const checked = new Set([...list.querySelectorAll('[data-word-list-item]:checked')].map(input => wordKey(state.words[Number(input.dataset.wordListItem)] || {})));
    list.innerHTML = listHtml();
    for (const input of list.querySelectorAll('[data-word-list-item]')) input.checked = checked.has(wordKey(state.words[Number(input.dataset.wordListItem)] || {}));
  }
  function notesHtml(data) {
    const notes = [];
    if (state.checking) notes.push(tr('stats.met.checking'));
    else if (Number(data.unchecked || 0) > 0) notes.push(g.PudgeI18n.tn('stats.met.unchecked', Number(data.unchecked), {count: g.PudgeI18n.number(Number(data.unchecked))}) + (data.paused ? ` ${g.PudgeWordLists.pauseText(data.paused)}` : ''));
    if (Number(data.pending_units || 0) > 0) notes.push(g.PudgeI18n.tn('stats.fun.stillCounting', Number(data.pending_units), {value: Number(data.pending_units)}));
    if (state.hideNames && Number(data.hidden_names || 0) > 0) notes.push(tr('stats.met.hiddenNames', {count: Number(data.hidden_names)}));
    return notes.length ? `<div class="stats-muted stats-met-notes">${esc(notes.join(' · '))}</div>` : '';
  }
  function render() {
    const root = host();
    if (!root) return;
    const head = `<div class="stats-met-head"><strong>${esc(tr('stats.met.title'))}</strong><span class="stats-muted">${esc(tr('stats.met.note'))}</span></div>`;
    const data = state.data;
    if (!data) {
      root.innerHTML = `${head}<div class="stats-muted">${esc(state.error || (state.loading ? tr('stats.loading') : ''))}</div>`;
      return;
    }
    if (!data.ok) {
      const key = `stats.met.reason.${String(data.reason || '')}`;
      let text = tr(key);
      if (text === key) text = g.PudgeI18n.message(String(data.reason || ''));
      if (data.reason === 'no_words' && Number(data.pending_units || 0) > 0) text = g.PudgeI18n.tn('stats.fun.stillCounting', Number(data.pending_units), {value: Number(data.pending_units)});
      root.innerHTML = `${head}<div class="stats-muted" data-met-words-reason="${esc(data.reason || '')}">${esc(text)}</div>`;
      return;
    }
    const toggle = `<label class="stats-met-toggle"><input type="checkbox" id="statsMetHideNames"${state.hideNames ? ' checked' : ''}> ${esc(tr('stats.met.hideNames'))}</label>`;
    const refresh = `<button type="button" data-met-words-refresh${state.loading || state.checking ? ' disabled' : ''}>${esc(tr('stats.met.refresh'))}</button>`;
    const more = data.has_more ? `<button type="button" data-met-words-more${state.loading ? ' disabled' : ''}>${esc(tr('stats.met.showMore'))}</button>` : '';
    const summary = `<div class="stats-muted">${esc(tr('stats.met.summary', {shown: state.words.length, total: g.PudgeI18n.number(Number(data.candidates || 0))}))}</div>`;
    const body = state.words.length
      ? `<div class="stats-met-list" data-met-words-list>${listHtml()}</div>
         <div class="stats-met-actions">${more}</div>
         ${g.PudgeWordLists.addControlsHtml(state.decks?.decks || [], state.decks?.preferred || '')}`
      : `<div class="stats-muted" data-met-words-empty>${esc(tr('stats.met.none'))}</div>`;
    root.innerHTML = `${head}<div class="stats-met-controls">${toggle}${refresh}</div>${summary}${notesHtml(data)}${body}`;
    root.querySelector('#statsMetHideNames')?.addEventListener('change', event => {
      state.hideNames = !!event.target.checked;
      try { g.localStorage?.setItem(HIDE_KEY, state.hideNames ? '1' : '0'); } catch (_) { /* a preference only */ }
      load(false);
    });
    root.querySelector('[data-met-words-more]')?.addEventListener('click', () => { state.limit += PAGE; load(false); });
    root.querySelector('[data-met-words-refresh]')?.addEventListener('click', () => { state.checks = 0; load(true); });
    g.PudgeWordLists.bindAdd(root, () => state.words, String(data.backend || 'jiten'), done => {
      state.words = state.words.filter(word => !done.includes(word));
      state.data = {...state.data, candidates: Math.max(0, Number(state.data.candidates || 0) - done.length)};
      render();
    });
  }

  async function load(check) {
    if (!api()?.consumption_statistics_met_words || !state.scope) return;
    const epoch = ++state.epoch, key = state.key;
    if (check) state.checking = true; else state.loading = true;
    state.error = '';
    render();
    let data = null;
    try {
      data = await api().consumption_statistics_met_words({...state.scope}, state.limit, state.hideNames, !!check);
    } catch (error) {
      if (epoch !== state.epoch) return;
      state.loading = false; state.checking = false;
      state.error = String(error?.message || error);
      if (!state.data) render();
      return;
    }
    if (epoch !== state.epoch || key !== state.key) return;
    state.loading = false; state.checking = false;
    state.loadedAt = Date.now();
    // Boxes the user ticked stay ticked when the list is asked for again.
    const list = host()?.querySelector('[data-met-words-list]');
    const ticked = new Set(list ? [...list.querySelectorAll('[data-word-list-item]:checked')].map(input => wordKey(state.words[Number(input.dataset.wordListItem)] || {})) : []);
    state.data = data;
    state.words = Array.isArray(data?.words) ? data.words.map(word => ({...word})) : [];
    if (data?.ok && state.words.length && !state.decks) {
      state.decks = await g.PudgeWordLists.decksFor(String(data.backend || 'jiten'));
      if (epoch !== state.epoch) return;
    }
    render();
    if (ticked.size) for (const input of host()?.querySelectorAll('[data-word-list-item]') || []) input.checked = ticked.has(wordKey(state.words[Number(input.dataset.wordListItem)] || {}));
    if (!data?.ok) return;
    g.PudgeWordLists.fillNames(state.words, () => epoch === state.epoch && !!host(), renderList);
    // One batched check (not one request per word); again only while it keeps finding
    // words whose state this Mac did not keep.
    const progressed = !check || Number(data.checked || 0) > 0;
    if (!data.paused && state.checks < MAX_CHECKS && progressed && (state.checks === 0 || Number(data.unchecked || 0) > 0)) {
      state.checks += 1;
      load(true);
    }
  }

  // Called by statistics.js after it drew the Overview for `scope`.
  function mount(body, scope) {
    if (!body || !g.PudgeWordLists) return;
    const shell = body.querySelector('.stats-shell') || body;
    let root = shell.querySelector('#statsMetWords');
    if (!root) {
      root = g.document.createElement('section');
      root.id = 'statsMetWords';
      root.className = 'stats-met-words';
      shell.appendChild(root);
    }
    const key = JSON.stringify([scope?.period, scope?.kind, scope?.media_uuid, scope?.start_date, scope?.end_date]);
    const stale = Date.now() - state.loadedAt > 60000;
    if (key !== state.key) {
      state.key = key; state.scope = {...scope}; state.limit = PAGE; state.data = null; state.words = []; state.checks = 0;
      load(false);
    } else if (stale && !state.loading && !state.checking) {
      render();
      state.checks = MAX_CHECKS;  // a redraw of the same period: what is kept is enough
      load(false);
    } else render();
  }

  g.PudgeMetWords = { mount, load, render, state };
})();
