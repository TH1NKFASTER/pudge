'use strict';
// L1: light-novel library tiles. One equal-height tile per book, series or
// franchise; all volumes open in a side panel instead of stretching the grid
// row (the old variable-height series groups left large gaps under short
// neighbours). Relies on the index.html helpers at call time: lnBookCard,
// lnSeriesGroupHtml, currentSeriesBook, lnSeriesKey, lnSeriesForKey,
// escapeHtml, anilistScoreChip, applyLnSelection, hydrateLnCardJiten, ui.
(() => {
  const g = globalThis;
  // index.html declares `const ui` (global lexical scope, not window.ui).
  // eslint-disable-next-line no-undef
  const uiRef = () => (typeof ui !== 'undefined' ? ui : g.ui);
  const ru = () => (uiRef()?.lang || document.documentElement.lang) === 'ru';
  const esc = value => (typeof g.escapeHtml === 'function' ? g.escapeHtml(value) : String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])));
  const byVolume = (a, b) => Number(a.volume || 1) - Number(b.volume || 1) || Number(a.id) - Number(b.id);
  let panelState = null; // {kind:'series'|'franchise', key, returnFocusKey}
  let panelHtml = ''; // last markup written; unchanged content is never rebuilt (no cover/Jiten flicker)

  function percentOf(book) {
    if (!book) return 0;
    if (book.finished) return 100;
    return Math.min(99, Math.round(Math.max(0, Math.min(1, Number(book.reading_progress || 0))) * 100));
  }

  function coverHtml(book) {
    const preview = ` data-pudge-cover-kind="light_novel" data-pudge-cover-id="${Number(book?.id || 0)}"`;
    return book?.cover_url
      ? `<img class="ln-card-cover" src="${esc(book.cover_url)}" alt="" loading="lazy" decoding="async"${preview}>`
      : '<div class="ln-card-cover cover-placeholder">Cover</div>';
  }

  // Shared tile shell: identical slot structure for series and franchise.
  function tileHtml({kind, key, attrs, title, current, total, finishedAll, badge, stats}) {
    const percent = percentOf(current);
    const volume = Number(current?.volume || 1);
    const position = finishedAll
      ? (ru() ? 'Прочитано' : 'Finished')
      : `${ru() ? 'Том' : 'Volume'} ${volume} · ${percent}%`;
    // Continue sits in the cover column: same width as the cover, fixed gap
    // below it; finished tiles keep an empty slot so every tile lines up.
    const contLabel = `${ru() ? 'Продолжить' : 'Continue'} · ${ru() ? 'том' : 'vol.'} ${volume}`;
    const cont = finishedAll || !current
      ? '<span class="ln-group-continue is-empty" aria-hidden="true"></span>'
      : `<button type="button" class="ln-group-continue" data-ln-action="read" data-id="${Number(current.id)}" title="${esc(contLabel)}" aria-label="${esc(contLabel)}">${ru() ? 'Продолжить' : 'Continue'}</button>`;
    const label = `${title}. ${badge}. ${position}`;
    return `<article class="ln-card ln-entry ln-group-tile" ${attrs} data-ln-group-open="${kind}" data-ln-group-key="${esc(key)}" tabindex="0" role="button" aria-haspopup="dialog" aria-label="${esc(label)}">`
      + `<div class="ln-group-cover-col">${coverHtml(current)}${cont}</div>`
      + `<div class="ln-card-body"><h3><span class="ln-card-title">${esc(title)}</span></h3>`
      + `<div class="ln-card-meta"><span>${esc(position)}</span><span class="ln-group-badge">${esc(badge)}</span></div>`
      + `<div class="ln-card-progress"><span style="width:${finishedAll ? 100 : percent}%"></span></div>`
      + (stats ? `<div class="ln-group-stats">${stats}</div>` : '')
      + `<div class="ln-group-actions"><span class="ln-group-open-hint">${ru() ? 'Все тома' : 'All volumes'} · ${total}</span></div>`
      + '</div></article>';
  }

  function seriesStats(first) {
    const jiten = first?.anilist_id || first?.jiten_deck_id
      ? `<div class="planned-jiten" data-planned-jiten data-ln-series-jiten="${Number(first.anilist_id)}" data-jiten-book="${Number(first.id)}"><span class="planned-jiten-loading">Jiten…</span></div>`
      : '';
    const score = first?.anilist_mean_score && typeof g.anilistScoreChip === 'function'
      ? g.anilistScoreChip(first.anilist_mean_score, 'percent')
      : '';
    return jiten + score;
  }

  // A standalone multi-volume series keeps its scrollable list of volumes (the
  // classic card); a click on the card outside the volumes opens the same
  // volumes panel as a franchise tile.
  function seriesGroupHtml(group) {
    const books = [...(group || [])].sort(byVolume);
    if (books.length === 0) return '';
    if (books.length === 1 || typeof g.lnSeriesGroupHtml !== 'function') return seriesTileHtml(books);
    const key = g.lnSeriesKey(books[0]);
    const current = g.currentSeriesBook(books);
    const cont = current && !current.finished
      ? `<button type="button" class="ln-group-continue ln-series-continue" data-ln-action="read" data-id="${Number(current.id)}">${ru() ? 'Продолжить' : 'Continue'} · ${ru() ? 'том' : 'vol.'} ${Number(current.volume || 1)}</button>`
      : '';
    const html = String(g.lnSeriesGroupHtml(books))
      .replace(/^(\s*<section\b[^>]*class="ln-series-group")/, `$1 data-ln-group-open="series" data-ln-group-key="${esc(key)}"`);
    // Inside the title block (a grid): the button sits right under the title.
    return cont ? html.replace('</div><span class="ln-series-count">', `${cont}</div><span class="ln-series-count">`) : html;
  }

  function seriesTileHtml(group) {
    const books = [...(group || [])].sort(byVolume);
    if (books.length === 0) return '';
    if (books.length === 1) return g.lnBookCard(books[0], false);
    const first = books[0];
    const key = g.lnSeriesKey(first);
    const current = g.currentSeriesBook(books);
    const finishedAll = books.every(book => Boolean(book.finished));
    const ids = books.map(book => Number(book.id)).filter(Boolean).join(',');
    return tileHtml({
      kind: 'series',
      key,
      attrs: `data-ln-series-key="${esc(key)}" data-ln-series-ids="${ids}"`,
      title: first.series_title || first.title || 'Light Novel',
      current,
      total: books.length,
      finishedAll,
      badge: `${ru() ? 'Серия' : 'Series'} · ${books.length} ${ru() ? 'т.' : 'vol.'}`,
      stats: seriesStats(first),
    });
  }

  function franchiseCurrent(shelf) {
    for (const series of shelf.series || []) {
      const current = g.currentSeriesBook([...(series.books || [])].sort(byVolume));
      if (current && !current.finished) return current;
    }
    const last = (shelf.series || []).at(-1);
    return last ? g.currentSeriesBook([...(last.books || [])].sort(byVolume)) : null;
  }

  function franchiseTileHtml(shelf) {
    const series = shelf.series || [];
    const books = series.flatMap(row => row.books || []);
    const ids = books.map(book => Number(book.id)).filter(Boolean).join(',');
    const current = franchiseCurrent(shelf);
    return tileHtml({
      kind: 'franchise',
      key: String(shelf.key || ''),
      attrs: `data-ln-franchise-ids="${ids}" data-library-shelf-key="${esc(shelf.key || '')}"`,
      title: shelf.title || series[0]?.title || 'Light Novel',
      current,
      total: books.length,
      finishedAll: books.length > 0 && books.every(book => Boolean(book.finished)),
      badge: `${ru() ? 'Франшиза' : 'Franchise'} · ${series.length} ${ru() ? 'сер.' : 'series'}`,
      stats: seriesStats(books.find(book => book.anilist_id || book.jiten_deck_id) || books[0]),
    });
  }

  function entryHtml(shelf) {
    const series = shelf?.series || [];
    if (series.length === 1) return seriesGroupHtml(series[0].books || []);
    return franchiseTileHtml(shelf);
  }

  function shelvesFor(books) {
    const builder = g.PudgeLibraryShelves?.build;
    if (!builder) return null;
    return builder(books, {
      seriesKey: g.lnSeriesKey,
      seriesTitle: book => String(book?.series_title || book?.title || 'Light Novel'),
      bookOrder: byVolume,
    });
  }

  function panelElement() {
    let panel = document.getElementById('lnVolumePanel');
    if (!panel) {
      panel = document.createElement('aside');
      panel.id = 'lnVolumePanel';
      panel.className = 'ln-volume-panel';
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'false');
      panel.hidden = true;
      // In <body>: the panel serves the light-novel and manga pages alike.
      document.body.appendChild(panel);
    }
    return panel;
  }

  // Panel covers load eagerly (a lazy image in a just-shown fixed panel
  // flashes empty first) and stay referenced, so WebKit keeps them decoded.
  const warmCovers = new Map();
  const WARM_LIMIT = 400;
  function warm(url) {
    if (!url || warmCovers.has(url) || typeof Image !== 'function') return;
    const img = new Image();
    img.decoding = 'async';
    img.src = url;
    warmCovers.set(url, img);
    if (warmCovers.size > WARM_LIMIT) warmCovers.delete(warmCovers.keys().next().value);
  }
  function eager(html) { return String(html).replace(/ loading="lazy"/g, ' loading="eager"'); }

  function volumesHtml(books) {
    const sorted = [...books].sort(byVolume);
    for (const book of sorted) warm(book.cover_url);
    return `<div class="ln-volume-grid">${sorted.map(book => eager(g.lnBookCard(book, true))).join('')}</div>`;
  }

  function warmGroup(kind, key) {
    const books = uiRef()?.lnState?.books || [];
    if (kind === 'series') {
      for (const book of g.lnSeriesForKey?.(key) || []) warm(book.cover_url);
      return;
    }
    const shelf = (shelvesFor(books) || []).find(row => String(row.key || '') === key);
    for (const series of shelf?.series || []) for (const book of series.books || []) warm(book.cover_url);
  }

  function panelContent(state) {
    // Other libraries (manga) reuse the panel through openCustom(): their
    // provider returns {title, body, continueHtml, afterRender}.
    if (state.kind === 'custom') return typeof state.provider === 'function' ? state.provider() : null;
    const books = uiRef()?.lnState?.books || [];
    if (state.kind === 'series') {
      const group = g.lnSeriesForKey(state.key);
      if (!group) return null;
      const first = [...group].sort(byVolume)[0];
      return {title: first.series_title || first.title || 'Light Novel', body: volumesHtml(group), current: g.currentSeriesBook([...group].sort(byVolume))};
    }
    const shelf = (shelvesFor(books) || []).find(row => String(row.key || '') === state.key);
    if (!shelf) return null;
    const body = (shelf.series || []).map(series => `<section class="ln-volume-section"><h4>${esc(series.title || '')}</h4>${volumesHtml(series.books || [])}</section>`).join('');
    return {title: shelf.title || '', body, current: franchiseCurrent(shelf)};
  }

  function renderPanel() {
    const panel = panelElement();
    // Closing only hides the panel: reopening the same group reuses the same
    // DOM, so covers are not re-requested or re-decoded.
    if (!panelState) { panel.hidden = true; return; }
    const content = panelContent(panelState);
    if (!content) { close(); return; }
    const current = content.current;
    const cont = typeof content.continueHtml === 'string' ? content.continueHtml : current && !current.finished
      ? `<button type="button" class="primary" data-ln-action="read" data-id="${Number(current.id)}">${ru() ? 'Продолжить' : 'Continue'} · ${ru() ? 'том' : 'vol.'} ${Number(current.volume || 1)}</button>`
      : '';
    const html = `<div class="ln-volume-head"><strong>${esc(content.title)}</strong><div class="ln-volume-head-actions">${cont}<button type="button" data-ln-volume-close aria-label="${ru() ? 'Закрыть' : 'Close'}">×</button></div></div><div class="ln-volume-body">${content.body}</div>`;
    if (html === panelHtml) { panel.hidden = false; if (panelState.kind !== 'custom') g.applyLnSelection?.(); return; }
    panel.setAttribute('aria-label', content.title);
    panel.innerHTML = html;
    panelHtml = html;
    panel.hidden = false;
    if (panelState.kind === 'custom') { try { content.afterRender?.(panel); } catch (_) {} return; }
    g.applyLnSelection?.();
    g.hydrateLnCardJiten?.(panel);
  }

  function openCustom(key, provider) {
    if (document.getElementById('contextMenu')?.classList?.contains('open')) g.hideContextMenu?.();
    const next = {kind: 'custom', key: String(key || ''), provider};
    if (panelState && panelState.kind === 'custom' && panelState.key === next.key) return;
    panelState = next;
    renderPanel();
    panelElement().querySelector?.('[data-ln-volume-close]')?.focus?.({preventScroll: true});
  }

  function open(kind, key) {
    const next = {kind: kind === 'franchise' ? 'franchise' : 'series', key: String(key || '')};
    // Opening the volumes panel dismisses an open context menu.
    if (document.getElementById('contextMenu')?.classList?.contains('open')) g.hideContextMenu?.();
    if (panelState && panelState.kind === next.kind && panelState.key === next.key) return; // already shown
    panelState = next;
    renderPanel();
    panelElement().querySelector?.('[data-ln-volume-close]')?.focus?.({preventScroll: true});
  }

  function close({restoreFocus = true} = {}) {
    const state = panelState;
    panelState = null;
    renderPanel();
    if (!state || !restoreFocus) return;
    const tile = [...document.querySelectorAll('[data-ln-group-open],[data-manga-group-open]')]
      .find(node => state.kind === 'custom'
        ? `manga:${node.dataset.mangaSeriesKey}` === state.key
        : node.dataset.lnGroupOpen === state.kind && node.dataset.lnGroupKey === state.key);
    tile?.focus?.({preventScroll: true});
  }

  // Keep an open panel in sync after library re-renders (progress, deletes).
  function refresh() { if (panelState) renderPanel(); }

  document.addEventListener('click', event => {
    if (event.target.closest?.('[data-ln-volume-close]')) { event.preventDefault(); close(); }
  });
  // Escape is routed by index.html's single Escape dispatcher (window capture),
  // which calls closeIfOpen(); a local Escape listener would never see the key.
  function closeIfOpen() {
    const panel = document.getElementById('lnVolumePanel');
    if (!panelState || !panel || panel.hidden) return false;
    close();
    return true;
  }

  // Surfaces that sit above the panel or belong to it: a press there is not "outside".
  const INSIDE = '#lnVolumePanel,[data-ln-group-open],[data-manga-group-open],#contextMenu,#modalBackdrop,#onboardingBackdrop,.pudge-confirm-backdrop,.pudge-select-menu,.pudge-cover-preview,.pudge-translation-pop,#lnStudyPop,#lnTranslatePop';
  document.addEventListener('pointerdown', event => {
    if (!panelState || event.button > 0) return;
    const target = event.target;
    if (!target?.closest || target.closest(INSIDE)) return;
    close();
  }, true);
  // Starting to read from the panel leaves the volumes view.
  document.addEventListener('click', event => {
    // Starting to read from the panel *or* from a tile's Continue leaves the volumes view.
    if (panelState && event.target?.closest?.('[data-ln-action="read"],[data-manga-v2-action="read"]')) setTimeout(() => { if (panelState) close({restoreFocus: false}); }, 0);
  });

  // A context menu opened for anything outside the panel (another card, anime,
  // a series) closes the panel; right-click on a franchise tile reopens it.
  document.addEventListener('contextmenu', event => {
    if (!panelState) return;
    const target = event.target;
    if (target?.closest?.('#lnVolumePanel,[data-ln-group-open="franchise"],#mangaReaderV2')) return;
    close({restoreFocus: false});
  }, true);

  // Hovering a tile preloads its volumes' covers before the click.
  document.addEventListener('pointerover', event => {
    const tile = event.target?.closest?.('[data-ln-group-open]');
    if (tile) warmGroup(tile.dataset.lnGroupOpen, tile.dataset.lnGroupKey);
  }, {passive: true});

  document.addEventListener('keydown', event => {
    const tile = event.target?.closest?.('[data-ln-group-open]');
    if (tile && event.target === tile && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      open(tile.dataset.lnGroupOpen, tile.dataset.lnGroupKey);
    }
  }, true);

  // Leaving the light-novel page always leaves the volumes view (no focus move:
  // the tile is on a hidden page).
  function dismiss() { if (panelState) close({restoreFocus: false}); }

  g.PudgeLnLibrary = {seriesTileHtml, seriesGroupHtml, franchiseTileHtml, entryHtml, open, openCustom, close, closeIfOpen, dismiss, refresh, isOpen: () => Boolean(panelState), state: () => panelState && {...panelState}};
})();
