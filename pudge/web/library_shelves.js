(() => {
  'use strict';
  const STORAGE_KEY = 'pudge.library.shelves.v1';
  const COLLAPSED_SERIES = 3;

  function loadState() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      return raw && typeof raw === 'object' ? raw : {};
    } catch (_) { return {}; }
  }
  function saveState(state) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) {}
  }
  function expanded(key) { return loadState()[String(key || '')] === true; }
  function setExpanded(key, value) {
    key = String(key || ''); if (!key) return;
    const state = loadState();
    if (value) state[key] = true; else delete state[key];
    saveState(state);
  }
  function toggle(key) { const value = !expanded(key); setExpanded(key, value); return value; }

  function captureScrollAnchor(root, scrollHost) {
    if (!root || !scrollHost) return null;
    const hostRect = scrollHost.getBoundingClientRect?.();
    if (!hostRect) return null;
    const candidates = [...root.querySelectorAll?.('[data-ln-book],[data-manga-book],[data-ln-series-key],[data-manga-series-key],[data-library-shelf-key]') || []];
    let best = null;
    for (const node of candidates) {
      const rect = node.getBoundingClientRect?.();
      if (!rect || rect.bottom < hostRect.top) continue;
      const key = node.dataset.lnBook ? `ln-book:${node.dataset.lnBook}`
        : node.dataset.mangaBook ? `manga-book:${node.dataset.mangaBook}`
        : node.dataset.lnSeriesKey ? `ln-series:${node.dataset.lnSeriesKey}`
        : node.dataset.mangaSeriesKey ? `manga-series:${node.dataset.mangaSeriesKey}`
        : node.dataset.libraryShelfKey ? `shelf:${node.dataset.libraryShelfKey}` : '';
      if (!key) continue;
      best = {key, offset: rect.top - hostRect.top, scrollTop: Number(scrollHost.scrollTop || 0)};
      break;
    }
    return best || {key: '', offset: 0, scrollTop: Number(scrollHost.scrollTop || 0)};
  }
  function restoreScrollAnchor(root, scrollHost, anchor) {
    if (!root || !scrollHost || !anchor) return;
    const escaped = value => (globalThis.CSS?.escape ? CSS.escape(String(value)) : String(value).replace(/["\\]/g, '\\$&'));
    let selector = '';
    const [kind, ...rest] = String(anchor.key || '').split(':');
    const value = rest.join(':');
    if (kind === 'ln-book') selector = `[data-ln-book="${escaped(value)}"]`;
    else if (kind === 'manga-book') selector = `[data-manga-book="${escaped(value)}"]`;
    else if (kind === 'ln-series') selector = `[data-ln-series-key="${escaped(value)}"]`;
    else if (kind === 'manga-series') selector = `[data-manga-series-key="${escaped(value)}"]`;
    else if (kind === 'shelf') selector = `[data-library-shelf-key="${escaped(value)}"]`;
    const node = selector ? root.querySelector?.(selector) : null;
    if (!node) { scrollHost.scrollTop = Number(anchor.scrollTop || 0); return; }
    const hostRect = scrollHost.getBoundingClientRect?.(), rect = node.getBoundingClientRect?.();
    if (!hostRect || !rect) { scrollHost.scrollTop = Number(anchor.scrollTop || 0); return; }
    scrollHost.scrollTop += (rect.top - hostRect.top) - Number(anchor.offset || 0);
  }

  function build(books, options = {}) {
    const seriesKey = options.seriesKey || (book => String(book?.series_key || book?.id || ''));
    const seriesTitle = options.seriesTitle || (book => String(book?.series_title || book?.title || ''));
    const bookOrder = options.bookOrder || ((a, b) => Number(a?.volume || 0) - Number(b?.volume || 0) || Number(a?.id || 0) - Number(b?.id || 0));
    const series = new Map();
    for (const book of books || []) {
      const key = String(seriesKey(book) || `book:${Number(book?.id || 0)}`);
      if (!series.has(key)) series.set(key, {key, title: seriesTitle(book), books: [], first: book});
      const group = series.get(key); group.books.push(book);
      if (!group.title) group.title = seriesTitle(book);
    }
    for (const group of series.values()) group.books.sort(bookOrder);

    const shelves = new Map();
    for (const group of series.values()) {
      const first = group.first || group.books[0] || {};
      const franchise = String(first.franchise_key || `series:${group.key}`);
      if (!shelves.has(franchise)) shelves.set(franchise, {
        key: franchise,
        title: String(first.franchise_title || group.title || ''),
        series: [],
        ambiguous: false,
      });
      const shelf = shelves.get(franchise);
      shelf.series.push(group);
      shelf.ambiguous ||= Boolean(first.franchise_ambiguous);
    }
    const result = [...shelves.values()];
    for (const shelf of result) {
      shelf.series.sort((a, b) => {
        const aa = Number(a.first?.franchise_order ?? Number.MAX_SAFE_INTEGER);
        const bb = Number(b.first?.franchise_order ?? Number.MAX_SAFE_INTEGER);
        return aa - bb || String(a.title).localeCompare(String(b.title)) || String(a.key).localeCompare(String(b.key));
      });
      shelf.bookCount = shelf.series.reduce((sum, group) => sum + group.books.length, 0);
      shelf.expanded = expanded(shelf.key);
      shelf.visibleSeries = shelf.expanded ? shelf.series : shelf.series.slice(0, COLLAPSED_SERIES);
      shelf.hiddenSeriesCount = Math.max(0, shelf.series.length - shelf.visibleSeries.length);
    }
    result.sort((a, b) => String(a.title).localeCompare(String(b.title)) || String(a.key).localeCompare(String(b.key)));
    return result;
  }

  window.PudgeLibraryShelves = {build, expanded, setExpanded, toggle, captureScrollAnchor, restoreScrollAnchor, COLLAPSED_SERIES};
})();
