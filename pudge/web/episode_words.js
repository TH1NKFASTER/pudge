// Word lists built from what Pudge already parsed.
//  * PudgeWordLists: the rows, deck picker and "Add checked words to the deck" shared by
//    the anime "Episode words" dialog and the Statistics card "Met often, not in deck"
//    (the same flow as the manga "Volume words" dialog: study_action add, word by word).
//  * PudgeEpisodeWords: the anime menu's "Episode words…" (an episode picker that starts
//    at the next unwatched episode) and "Words of all episodes" (pudge/episode_words.py).
(() => {
  'use strict';
  const g = globalThis;
  const tr = (key, params) => g.PudgeI18n.t(key, params || {});
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const byId = id => g.document.getElementById(id);
  const api = () => g.pywebview.api;
  const pct = value => `${Math.round(Number(value || 0) * 100)}%`;

  // ---------------------------------------------------------------- shared word list
  const kindLabel = kind => {
    const key = {anime:'stats.kind.anime', anime_episode:'stats.kind.anime', light_novel:'stats.kind.lightNovel', manga:'stats.kind.manga', visual_novel:'stats.kind.visualNovel', audiobook:'stats.kind.audiobook'}[kind];
    return key ? tr(key) : '';
  };
  function sourcesText(word) {
    const rows = Array.isArray(word.sources) ? word.sources : [];
    if (!rows.length) return '';
    const parts = rows.map(row => `${row.title || kindLabel(row.kind) || '?'} ×${Number(row.count || 0)}`);
    if (Number(word.more_sources || 0) > 0) parts.push(tr('wordList.moreSources', {count: Number(word.more_sources)}));
    return parts.join(' · ');
  }
  // One row per word; `checked` false leaves the boxes empty (a long ranking: the user picks).
  function rowsHtml(words, options = {}) {
    return words.map((word, index) => {
      const named = !!word.spelling;
      const where = sourcesText(word);
      return `<label class="ln-nyaa-row manga-volume-word word-list-row${named ? '' : ' unnamed'}" data-word-list-row="${index}">
        <input type="checkbox" data-word-list-item="${index}"${options.checked === false ? '' : ' checked'}>
        <span class="title">${named ? esc(word.spelling) : `<span class="subtle">${esc(tr('wordList.loadingName'))}</span>`}</span>
        <span>${esc(word.reading || '')}</span>
        <span title="${esc(word.meaning || '')}">${esc(word.meaning || '')}${where ? `<small class="word-list-sources" title="${esc(where)}">${esc(where)}</small>` : ''}</span>
        <span>×${Number(word.count || 0)}</span>
      </label>`;
    }).join('');
  }
  async function decksFor(backend) {
    let decks = [], preferred = '';
    try { decks = (await api().study_decks(backend)) || []; } catch (_) { decks = []; }
    try { preferred = String((await api().study_deck_preference(backend))?.deck_id || ''); } catch (_) { preferred = ''; }
    return {decks, preferred};
  }
  function addControlsHtml(decks, preferred) {
    const options = decks.map(deck => `<option value="${esc(String(deck.id ?? ''))}"${String(deck.id ?? '') === preferred ? ' selected' : ''}>${esc(String(deck.name || deck.id || ''))}</option>`).join('');
    return `<div class="setting-inline-actions">
        <select data-word-list-deck>${options}</select>
        <button type="button" data-word-list-add>${esc(tr('manga.addCheckedWordsToThe'))}</button>
      </div>
      <div class="subtle" data-word-list-progress></div>`;
  }
  // "Add checked words to the deck" inside `root`; `words()` is the list the boxes index.
  function bindAdd(root, words, backend, onAdded) {
    const button = root.querySelector('[data-word-list-add]');
    if (!button) return;
    button.addEventListener('click', async () => {
      const deck = String(root.querySelector('[data-word-list-deck]')?.value || '');
      const progress = root.querySelector('[data-word-list-progress]');
      if (!deck) { g.toast?.(tr('manga.chooseADeck')); return; }
      const list = words();
      const chosen = [...root.querySelectorAll('[data-word-list-item]:checked')].map(input => list[Number(input.dataset.wordListItem)]).filter(Boolean);
      if (!chosen.length) { g.toast?.(tr('wordList.nothingChecked')); return; }
      button.disabled = true;
      let added = 0, failed = 0;
      const done = [];
      for (const word of chosen) {
        if (progress) progress.textContent = `${added + failed + 1}/${chosen.length}`;
        try {
          const result = await api().study_action({backend, action:'add', word_id:Number(word.word_id), reading_index:Number(word.reading_index), deck_id:deck, id_namespace:backend});
          if (result?.ok === false) failed += 1; else { added += 1; done.push(word); }
        } catch (_) { failed += 1; }
      }
      try { await api().set_study_deck_preference(backend, deck); } catch (_) { /* preference only */ }
      if (progress) progress.textContent = failed ? tr('manga.wordsAddedFailed', {added, failed}) : tr('manga.wordsAdded', {added});
      button.disabled = false;
      if (done.length) onAdded?.(done);
    });
  }
  // Words nothing has named yet: ask for their names (Pudge fetches them on its paced
  // background queue) a few times, and tell the caller when some arrived.
  async function fillNames(words, alive, onNamed, tries = 6) {
    for (let attempt = 0; attempt < tries; attempt += 1) {
      const missing = words.filter(word => !word.spelling);
      if (!missing.length || !alive()) return;
      let answer = null;
      try { answer = await api().study_word_names(missing.map(word => [Number(word.word_id), Number(word.reading_index)])); } catch (_) { return; }
      if (!alive()) return;
      let named = 0;
      for (const row of answer?.words || []) {
        const word = words.find(item => Number(item.word_id) === Number(row.word_id) && Number(item.reading_index) === Number(row.reading_index));
        if (!word || word.spelling || !row.spelling) continue;
        Object.assign(word, {spelling: row.spelling, reading: row.reading, meaning: row.meaning, is_name: !!row.is_name});
        named += 1;
      }
      if (named) onNamed?.();
      if (answer?.paused || !Number(answer?.missing || 0)) return;
      await new Promise(resolve => g.setTimeout(resolve, 3000));
    }
  }
  const pauseText = reason => {
    const key = `wordList.paused.${String(reason || '')}`;
    const text = tr(key);
    return text === key ? tr('wordList.paused.other') : text;
  };
  g.PudgeWordLists = { rowsHtml, decksFor, addControlsHtml, bindAdd, fillNames, pauseText, sourcesText };

  // ---------------------------------------------------------------- Episode words dialog
  const state = { mediaId: null, token: 0, timer: null, options: null, choice: '', result: null, words: [], decks: null };

  function hasLocal(a) {
    return Number(a?.local_count || 0) > 0 || !!a?.local?.video_path;
  }
  function menuItems(a) {
    if (!hasLocal(a)) return '';
    return `<button data-context-action="episode-words">${esc(tr('action.episodeWords'))}</button>`
      + `<button data-context-action="episode-words-all">${esc(tr('action.episodeWordsAll'))}</button>`;
  }
  function open() {
    const backdrop = byId('modalBackdrop');
    return !!backdrop?.classList.contains('open') && !!byId('modalBody')?.querySelector(`[data-episode-words-root="${state.mediaId}"]`);
  }
  function episodeLabel(row) {
    const label = row.episode_end ? tr('animeAudiobook.episodesChapter', {first: row.episode, last: row.episode_end}) : tr('animeAudiobook.episodeChapter', {episode: row.episode});
    return row.subtitles ? label : tr('episodeWords.episodeNoSubtitles', {episode: label});
  }
  function pickerHtml() {
    const rows = state.options?.episodes || [];
    const options = rows.map(row => `<option value="${Number(row.episode)}"${String(row.episode) === state.choice ? ' selected' : ''}>${esc(episodeLabel(row))}</option>`).join('');
    const all = `<option value="all"${state.choice === 'all' ? ' selected' : ''}>${esc(tr('episodeWords.allEpisodes', {count: Number(state.options?.with_subtitles || 0)}))}</option>`;
    return `<div class="setting-inline-actions episode-words-picker"><label>${esc(tr('episodeWords.pick'))} <select id="episodeWordsPick">${options}${all}</select></label></div>`;
  }
  function reasonText(result) {
    const reason = String(result?.reason || '');
    if (reason === 'paused') return tr('episodeWords.reason.paused', {reason: pauseText(result.paused)});
    if (reason === 'parse_failed') return `${tr('manga.couldNotParseTheText')}${result.detail ? `: ${g.PudgeI18n.message(String(result.detail))}` : ''}`;
    const key = `episodeWords.reason.${reason}`;
    const text = tr(key);
    return text === key ? g.PudgeI18n.message(reason) : text;
  }
  function notesHtml(result) {
    const notes = [];
    if (result.scope === 'title') notes.push(tr('episodeWords.counted', {counted: Number(result.episodes_counted || 0), total: Number(result.episodes_total || 0)}));
    const without = result.episodes_without_subtitles || [];
    if (result.scope === 'title' && without.length) notes.push(tr('episodeWords.withoutSubtitles', {episodes: without.join(', ')}));
    const waiting = result.episodes_waiting || [];
    let force = '';
    if (waiting.length) {
      notes.push(tr('episodeWords.waiting', {episodes: waiting.join(', '), reason: pauseText(result.paused)}));
      // Jiten's own limit cannot be skipped; the energy rules can, when the user says so.
      if (!String(result.paused || '').startsWith('jiten')) force = ` <button type="button" data-episode-words-force>${esc(tr('episodeWords.countNow'))}</button>`;
    }
    return notes.length ? `<p class="subtle episode-words-notes">${esc(notes.join(' '))}${force}</p>` : '';
  }
  function resultHtml() {
    const result = state.result;
    if (!result) return '';
    if (!result.ok) return `${notesHtml(result)}<div class="empty" data-episode-words-reason="${esc(result.reason || '')}">${esc(reasonText(result))}</div>`;
    const summary = tr('manga.youKnowOfTheRunning', {value: pct(result.known_share), value2: pct(result.known_or_learning_share), unique_words: Number(result.unique_words || 0), new_unique: Number(result.new_unique || 0)});
    const words = state.words;
    const list = words.length
      ? `<p class="subtle">${esc(tr(result.scope === 'title' ? 'episodeWords.mostFrequentTitle' : 'episodeWords.mostFrequentEpisode'))}</p>
         <div class="manga-volume-words" data-episode-words-list>${rowsHtml(words)}</div>
         ${addControlsHtml(state.decks?.decks || [], state.decks?.preferred || '')}`
      : `<div class="empty">${esc(tr('manga.noNewWords'))}</div>`;
    return `<p data-episode-words-summary>${esc(summary)}</p>${notesHtml(result)}${list}`;
  }
  function render(status) {
    const body = byId('modalBody');
    if (!body || state.mediaId == null) return;
    const busy = !!status?.busy;
    const progress = busy
      ? `<div class="anime-audiobook-job running episode-words-job"><div>${esc(Number(status.total || 0) > 1 ? tr('episodeWords.progress', {current: Number(status.current || 0), total: Number(status.total || 0)}) : tr('episodeWords.counting'))}</div>`
        + `<progress max="100" value="${Math.round(Number(status.progress || 0) * 100)}"></progress> <button type="button" data-episode-words-cancel>${esc(tr('action.cancel'))}</button></div>`
      : '';
    body.innerHTML = `<div data-episode-words-root="${state.mediaId}" data-modal-request="${state.token}">${pickerHtml()}${progress}${busy ? '' : resultHtml()}</div>`;
    const root = body.firstElementChild;
    byId('episodeWordsPick')?.addEventListener('change', event => { state.choice = String(event.target.value || ''); start(false); });
    root.querySelector('[data-episode-words-cancel]')?.addEventListener('click', async () => { try { await api().anime_episode_words_cancel(state.mediaId); } catch (_) { /* the poll shows it */ } });
    root.querySelector('[data-episode-words-force]')?.addEventListener('click', () => start(true));
    if (!busy && state.result?.ok) {
      bindAdd(root, () => state.words, String(state.result.backend || 'jiten'), done => {
        // Added words are no longer new: they leave the list.
        state.words = state.words.filter(word => !done.includes(word));
        const list = root.querySelector('[data-episode-words-list]');
        if (list) list.innerHTML = rowsHtml(state.words);
      });
    }
  }
  function schedule(delay) {
    if (state.timer) g.clearTimeout(state.timer);
    state.timer = g.setTimeout(poll, delay);
  }
  async function finished(status) {
    state.result = status.result || {ok: false, reason: status.state === 'cancelled' ? 'cancelled' : 'parse_failed', detail: status.error || ''};
    state.words = Array.isArray(state.result.top_new) ? state.result.top_new.map(word => ({...word})) : [];
    if (state.result.ok && state.words.length && !state.decks) state.decks = await decksFor(String(state.result.backend || 'jiten'));
    if (!open()) return;
    render(status);
    const token = state.token, job = status.job_id;
    fillNames(state.words, () => open() && state.token === token && state.lastJob === job, () => {
      const list = byId('modalBody')?.querySelector('[data-episode-words-list]');
      if (list) list.innerHTML = rowsHtml(state.words);
    });
  }
  async function poll() {
    state.timer = null;
    if (state.mediaId == null || !open()) return;
    let status = null;
    try { status = await api().anime_episode_words_status(state.mediaId); } catch (_) { schedule(1500); return; }
    if (!open()) return;
    if (status.busy) { render(status); schedule(700); return; }
    await finished(status);
  }
  async function start(force) {
    if (state.mediaId == null || !state.choice) return;
    state.result = null;
    state.words = [];
    const episode = state.choice === 'all' ? null : Number(state.choice);
    let status = null;
    try {
      status = await api().anime_episode_words_start(state.mediaId, episode, 40, !!force);
    } catch (error) {
      state.result = {ok: false, reason: 'parse_failed', detail: String(error?.message || error)};
      if (open()) render({busy: false});
      return;
    }
    state.lastJob = status.job_id;
    if (!open()) return;
    if (status.busy) { render(status); schedule(400); } else await finished(status);
  }
  async function show(a, all) {
    state.mediaId = Number(a.media_id);
    state.result = null; state.words = []; state.options = null; state.decks = null; state.choice = '';
    state.token = typeof g.beginModalRequest === 'function' ? g.beginModalRequest() : 0;
    const backdrop = byId('modalBackdrop');
    backdrop.hidden = false;
    backdrop.classList.remove('watch-order-open');
    byId('modalTitle').textContent = tr('episodeWords.modalTitle', {title: a.title || ''});
    backdrop.classList.add('open');
    byId('modalBody').innerHTML = `<div class="empty" data-episode-words-root="${state.mediaId}" data-modal-request="${state.token}"><span class="spinner"></span></div>`;
    const token = state.token;
    let options = null;
    try { options = await api().anime_episode_words_options(state.mediaId); } catch (error) {
      if (open() && state.token === token) byId('modalBody').innerHTML = `<div class="empty" data-episode-words-root="${state.mediaId}">${esc(g.PudgeI18n.message(String(error?.message || error)))}</div>`;
      return;
    }
    if (!open() || state.token !== token) return;
    state.options = options;
    if (!(options.episodes || []).length) {
      byId('modalBody').innerHTML = `<div class="empty" data-episode-words-root="${state.mediaId}">${esc(tr('episodeWords.reason.no_episodes'))}</div>`;
      return;
    }
    state.choice = all || options.default_episode == null ? 'all' : String(options.default_episode);
    await start(false);
  }
  async function handle(action, a) {
    if (action === 'episode-words') await show(a, false);
    else if (action === 'episode-words-all') await show(a, true);
  }

  g.PudgeEpisodeWords = { menuItems, handle, show, state };
})();
