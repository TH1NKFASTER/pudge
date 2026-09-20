'use strict';

(() => {
  const API = () => window.pywebview && window.pywebview.api;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';

  const SETTINGS_KEY = 'pudge.manga.reader.v2';
  const COVER_CACHE_KEY = 'pudge.manga.anilist.covers.v1';
  const PAGE_CACHE_LIMIT = 8;
  const defaults = {
    mode: 'single',
    direction: 'rtl',
    fit: 'height',
    zoom: 100,
    gap: 16,
    toolbar: true,
    background: 'black'
  };

  let settings = loadJson(SETTINGS_KEY, defaults);
  let coverCache = loadJson(COVER_CACHE_KEY, {});
  const coverLoadInflight = new Map();
  const COVER_DECODE_TIMEOUT_MS = 8000;
  let state = {books: []};
  let currentBook = null;
  let currentBookOcrStatus = null;
  let currentPage = 0;
  let currentPageCount = 0;
  let pageCache = new Map();
  let textRegionCache = new Map();
  let textRegionResultCache = new Map();
  let textParseCache = new Map();
  let textParseInflight = new Map();
  const textForegroundOcrInflight = new Set();
  // pudge-v0.7.27-manga-cold-cache-foreground-runtime-v1
  let mangaDebugEvents = [];
  let mangaDebugSelectionKey = '';
  let mangaLastSelection = null;
  let mangaPointerSelection = null;
  let textGeneration = 0;
  let gestureBaseZoom = 100;
  let gestureActive = false;
  let toolbarPeekTimer = null;
  let libraryRendering = false;
  let libraryRenderSignature = '';
  let verticalObserver = null;
  let verticalPersistTimer = null;
  let pageRenderGeneration = 0;
  let preparationPollTimer = null;
  let preparationPollInFlight = false;
  let preparationPollGeneration = 0;
  let mangaContextBook = null;
  let mangaContextSeries = null;
  let pagedVisibleCount = 1;
  const selectedBookIds = new Set();
  const libraryOcrPollers = new Map();

  function emitSelection() {
    window.dispatchEvent(new CustomEvent('pudge-manga-selection-changed', {detail:{ids:[...selectedBookIds]}}));
  }

  function applyLibrarySelection() {
    const root = $('mangaContent');
    if (!root) return;
    for (const node of root.querySelectorAll('.ln-entry[data-manga-book]')) {
      node.classList.toggle('selected', selectedBookIds.has(Number(node.dataset.mangaBook)));
    }
    for (const node of root.querySelectorAll('[data-manga-series-ids]')) {
      const ids = mangaSeriesIds(node);
      const selectedCount = ids.filter(id => selectedBookIds.has(id)).length;
      node.classList.toggle('series-selected', Boolean(ids.length) && selectedCount === ids.length);
      node.classList.toggle('series-partial', selectedCount > 0 && selectedCount < ids.length);
    }
    for (const node of root.querySelectorAll('[data-manga-franchise-ids]')) {
      const ids = mangaFranchiseIds(node);
      const selectedCount = ids.filter(id => selectedBookIds.has(id)).length;
      node.classList.toggle('franchise-selected', Boolean(ids.length) && selectedCount === ids.length);
      node.classList.toggle('franchise-partial', selectedCount > 0 && selectedCount < ids.length);
    }
  }

  function toggleSelection(bookId) {
    const id = Number(bookId);
    if (!id) return;
    if (selectedBookIds.has(id)) selectedBookIds.delete(id); else selectedBookIds.add(id);
    applyLibrarySelection();
    emitSelection();
  }

  // pudge-v0.7.23-manga-series-context-spread-v1
  function mangaSeriesIds(node) {
    return String(node?.dataset?.mangaSeriesIds || '')
      .split(',')
      .map(Number)
      .filter(Boolean);
  }

  function mangaFranchiseIds(node) {
    return String(node?.dataset?.mangaFranchiseIds || '')
      .split(',')
      .map(Number)
      .filter(Boolean);
  }

  function toggleFranchiseSelection(node) {
    const ids = mangaFranchiseIds(node);
    if (!ids.length) return;
    const allSelected = ids.every(id => selectedBookIds.has(id));
    for (const id of ids) {
      if (allSelected) selectedBookIds.delete(id); else selectedBookIds.add(id);
    }
    applyLibrarySelection();
    emitSelection();
  }

  function mangaSeriesForKey(key) {
    return groupBooks(state.books || []).find(group => String(group.key) === String(key || '')) || null;
  }

  function toggleSeriesSelection(books) {
    const ids = (books || []).map(book => Number(book.id)).filter(Boolean);
    if (!ids.length) return;
    const allSelected = ids.every(id => selectedBookIds.has(id));
    for (const id of ids) {
      if (allSelected) selectedBookIds.delete(id); else selectedBookIds.add(id);
    }
    applyLibrarySelection();
    emitSelection();
  }

  function loadJson(key, fallback) {
    try {
      return {...fallback, ...JSON.parse(localStorage.getItem(key) || '{}')};
    } catch (_) {
      return {...fallback};
    }
  }

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  }

  function saveCoverCache() {
    try { localStorage.setItem(COVER_CACHE_KEY, JSON.stringify(coverCache)); } catch (_) {}
  }

  function naturalParts(value) {
    return String(value ?? '').split(/(\d+(?:\.\d+)?)/).map(part => {
      const n = Number(part);
      return Number.isFinite(n) && part.trim() !== '' ? n : part.toLocaleLowerCase();
    });
  }

  function naturalCompare(a, b) {
    const aa = naturalParts(a), bb = naturalParts(b);
    const n = Math.max(aa.length, bb.length);
    for (let i = 0; i < n; i++) {
      if (aa[i] === undefined) return -1;
      if (bb[i] === undefined) return 1;
      if (aa[i] === bb[i]) continue;
      if (typeof aa[i] === typeof bb[i]) return aa[i] < bb[i] ? -1 : 1;
      return String(aa[i]).localeCompare(String(bb[i]));
    }
    return 0;
  }

  function volumeNumber(book) {
    const direct = Number(book.volume ?? book.volume_number ?? book.volume_index);
    if (Number.isFinite(direct) && direct > 0) return direct;
    const source = `${book.title || ''} ${book.path || ''}`;
    const patterns = [
      /(?:^|[\s._\-\[(])(?:vol(?:ume)?|v|том)\s*[._\- ]*(\d+(?:\.\d+)?)/i,
      /(?:^|[\s._\-\[(])(\d+(?:\.\d+)?)\s*(?:巻|kan)(?:$|[\s._\-\])])/i,
    ];
    for (const pattern of patterns) {
      const match = source.match(pattern);
      if (match) return Number(match[1]);
    }
    return 1;
  }

  function stripVolume(value) {
    let text = String(value || '').trim();
    text = text.replace(/^\s*(?:\[[^\]\r\n]{1,80}\]\s*)+/g, '');
    text = text.replace(/(?:\s+\[[^\]\r\n]{1,80}\])+$/g, '');
    text = text
      .replace(/\s*[\[(]?\s*(?:vol(?:ume)?|v|том)\s*[._\- ]*\d+(?:\.\d+)?\s*[\])]?\s*/ig, ' ')
      .replace(/\s+\d+(?:\.\d+)?\s*巻\s*/ig, ' ');
    if (/[^\d\s._-]/.test(text)) text = text.replace(/[\s._-]+0*\d{1,3}(?:\.\d+)?\s*$/g, '');
    return text.replace(/\s{2,}/g, ' ').replace(/[\s._-]+$/g, '').trim();
  }

  function seriesTitle(book) {
    return String(
      book.series_title ||
      book.anilist_title ||
      book.media_title ||
      stripVolume(book.title) ||
      book.title ||
      'Manga'
    ).trim();
  }

  function normalizedSeriesKey(value) {
    return stripVolume(value)
      .replace(/[\s\[\](){}._・･:：!！?？'"“”‘’—–-]+/g, '')
      .toLocaleLowerCase();
  }

  function groupBooks(books) {
    const groups = new Map();
    for (const book of books) {
      const title = seriesTitle(book);
      // Local series identity wins over AniList id. That lets a linked and an
      // unlinked volume (or two previously mis-linked volumes) still merge.
      const key = String(book.series_key || normalizedSeriesKey(title) || `book:${book.id}`);
      if (!groups.has(key)) groups.set(key, {key, title, books: []});
      const group = groups.get(key);
      group.books.push(book);
      if ((!group.title || group.title === 'Manga') && title) group.title = title;
    }
    const result = [...groups.values()];
    for (const group of result) {
      group.books.sort((a, b) =>
        volumeNumber(a) - volumeNumber(b) ||
        naturalCompare(a.title, b.title)
      );
    }
    result.sort((a, b) => naturalCompare(a.title, b.title));
    return result;
  }

  function anilistId(book) {
    const direct = Number(book.anilist_id || book.media_id);
    if (Number.isFinite(direct) && direct > 0) return direct;
    const match = String(book.site_url || '').match(/anilist\.co\/manga\/(\d+)/i);
    return match ? Number(match[1]) : null;
  }

  function localCover(book) {
    const options = [
      book.local_cover_url,
      book.local_cover_data_uri,
      book.cover_data_uri,
      book.local_cover,
      book.cover
    ];
    if (book.cover_url && !/^https?:\/\//i.test(book.cover_url)) options.unshift(book.cover_url);
    return options.find(value => typeof value === 'string' && value.trim()) || '';
  }

  function existingRemoteCover(book) {
    const options = [
      book.anilist_cover_url,
      book.remote_cover_url,
      book.cover_url
    ];
    return options.find(value => /^https?:\/\//i.test(String(value || ''))) || '';
  }

  async function decodeCover(url) {
    const source = String(url || '').trim();
    if (!source) return false;
    if (coverLoadInflight.has(source)) return coverLoadInflight.get(source);
    const task = (async () => {
      const image = new Image();
      image.decoding = 'async';
      image.src = source;
      let timer = 0;
      const timeout = new Promise(resolve => { timer = setTimeout(() => resolve(false), COVER_DECODE_TIMEOUT_MS); });
      const load = (async () => {
        if (typeof image.decode === 'function') {
          try { await image.decode(); } catch (_) { return false; }
        } else if (!image.complete) {
          await new Promise(resolve => { image.onload = resolve; image.onerror = resolve; });
        }
        return Boolean(image.naturalWidth && image.naturalHeight);
      })();
      const ready = await Promise.race([load, timeout]);
      if (timer) clearTimeout(timer);
      return Boolean(ready);
    })().finally(() => coverLoadInflight.delete(source));
    coverLoadInflight.set(source, task);
    return task;
  }

  async function resolveCover(book) {
    const id = anilistId(book);
    // The backend already persists accepted AniList artwork / first-page covers
    // as local data. Prefer that synchronously instead of flashing a placeholder
    // while WebKit performs another remote request.
    const local = localCover(book);
    if (local) return local;
    if (id && coverCache[id]) return coverCache[id];
    const existing = existingRemoteCover(book);
    if (existing) {
      if (id) { coverCache[id] = existing; saveCoverCache(); }
      return existing;
    }
    return '';
  }

  function progressText(book) {
    const page = Math.min(Number(book.page_count || 0), Number(book.read_pages || 0));
    const total = Number(book.page_count || 0);
    return `${page} / ${total}`;
  }

  function volumeLabel(book, index) {
    const n = volumeNumber(book);
    if (Number.isFinite(n) && n > 0) return ru() ? `Том ${n}` : `Vol. ${n}`;
    return ru() ? `Том ${index + 1}` : `Vol. ${index + 1}`;
  }

  function mangaLibraryCard(book, compact = false) {
    const id = Number(book.id);
    const total = Math.max(0, Number(book.page_count || 0));
    const read = total ? Math.max(0, Math.min(total, Number(book.read_pages || 0))) : 0;
    const percent = total ? Math.max(0, Math.min(100, Math.round(read / total * 100))) : 0;
    const volume = volumeNumber(book);
    const title = compact ? (ru() ? `Том ${volume}` : `Volume ${volume}`) : esc(book.title || seriesTitle(book));
    const linkedId = anilistId(book);
    const linkedUrl = linkedId ? (book.site_url || `https://anilist.co/manga/${linkedId}`) : '';
    const score = Number(book.mean_score || 0);
    const scoreHtml = !compact && score && window.PudgeAniListScore?.chip
      ? window.PudgeAniListScore.chip(score, 'percent')
      : '';
    const progressTitle = total ? `${read} / ${total} ${ru() ? 'страниц' : 'pages'}` : `${percent}%`;
    // pudge-v0.7.23-manga-progress-tooltip-v1
    const facts = (compact
      ? [`${percent}%`, total ? `${total} ${ru() ? 'стр.' : 'pages'}` : '']
      : [ru() ? `Том ${volume}` : `Volume ${volume}`, total ? `${total} ${ru() ? 'страниц' : 'pages'}` : '', `${percent}%`])
      .filter(Boolean)
      .map(value => value === `${percent}%`
        ? `<span data-tooltip="${esc(progressTitle)}" aria-label="${esc(progressTitle)}">${esc(value)}</span>`
        : `<span>${esc(value)}</span>`)
      .join('') + scoreHtml;
    const cover = `<div class="ln-card-cover cover-placeholder" data-cover-placeholder="${id}" ${linkedUrl ? `data-manga-v2-action="anilist" data-url="${esc(linkedUrl)}" title="AniList"` : ''}>Cover</div><img class="ln-card-cover" data-cover-book="${id}" data-pudge-cover-kind="manga" data-pudge-cover-id="${id}" alt="" loading="lazy" decoding="async" ${linkedUrl ? `data-manga-v2-action="anilist" data-url="${esc(linkedUrl)}" title="AniList"` : ''} hidden>`;
    const jiten = linkedId ? `<div class="planned-jiten" data-library-card-jiten data-manga-card-jiten="${linkedId}" data-jiten-book="${id}" data-jiten-volume="${volume}"><span class="planned-jiten-loading">Jiten…</span></div>` : '';
    return `<article class="ln-card ln-entry ${compact ? 'compact' : ''} ${selectedBookIds.has(id) ? 'selected' : ''}" data-manga-book="${id}" data-manga-v2-action="read" data-id="${id}">${cover}<div class="ln-card-body"><h3>${title}</h3><div class="ln-card-meta">${facts}</div><div class="ln-card-progress" data-tooltip="${esc(progressTitle)}" aria-label="${esc(progressTitle)}"><span style="width:${percent}%"></span></div>${jiten}</div></article>`;
  }

  function mangaCurrentSeriesBook(books) {
    const unfinished = book => Number(book.read_pages || 0) < Number(book.page_count || 0);
    return books.find(unfinished) || books[books.length - 1];
  }

  function mangaSeriesGroupHtml(group) {
    if (group.books.length === 1) return mangaLibraryCard(group.books[0], false);
    const current = mangaCurrentSeriesBook(group.books);
    const scrollClass = group.books.length > 2 ? ' series-scroll' : '';
    const first = group.books[0];
    const linkedId = anilistId(first);
    const seriesJiten = linkedId ? `<div class="planned-jiten" data-planned-jiten data-manga-series-jiten="${linkedId}" data-jiten-book="${Number(first.id)}"><span class="planned-jiten-loading">Jiten…</span></div>` : '';
    const seriesIds = group.books.map(book => Number(book.id)).filter(Boolean).join(',');
    const seriesMean = Number(first.mean_score || 0);
    const seriesScore = window.PudgeAniListScore?.chip && seriesMean ? window.PudgeAniListScore.chip(seriesMean, 'percent') : '';
    const seriesStats = (seriesJiten || seriesScore) ? `<div class="ln-series-stats">${seriesJiten}${seriesScore}</div>` : '';
    return `<section class="ln-series-group" data-manga-series-key="${esc(group.key)}" data-manga-series-ids="${seriesIds}"><div class="ln-series-head" data-manga-series-select="${esc(group.key)}"><div class="ln-series-title-block"><strong>${esc(group.title)}</strong>${seriesStats}</div><span class="ln-series-count">${group.books.length} ${ru() ? 'тома/томов' : 'volumes'}</span></div><div class="ln-series-books${scrollClass}" ${group.books.length > 2 ? 'data-series-scroll="1"' : ''} data-series-current-book="${Number(current?.id || 0)}">${group.books.map(book => mangaLibraryCard(book, true)).join('')}</div></section>`;
  }

  function mangaShelfContinue(groups) {
    for (const group of groups) {
      const current = mangaCurrentSeriesBook(group.books || []);
      if (current && Number(current.read_pages || 0) < Number(current.page_count || 0)) return {group, book: current};
    }
    const last = groups[groups.length - 1];
    return last ? {group:last, book:mangaCurrentSeriesBook(last.books || [])} : null;
  }

  function mangaShelfHtml(shelf) {
    const groups = shelf.series || [];
    if (groups.length === 1) return mangaSeriesGroupHtml(groups[0]);
    const visible = shelf.visibleSeries || groups;
    const ids = groups.flatMap(group => group.books || []).map(book => Number(book.id)).filter(Boolean).join(',');
    const current = mangaShelfContinue(groups);
    const currentVolume = Number(current?.book ? volumeNumber(current.book) : 1);
    const continueText = current ? `${ru() ? 'Продолжить' : 'Continue'}: ${esc(current.group.title || '')} · ${ru() ? 'том' : 'volume'} ${currentVolume}` : '';
    const counts = `${groups.length} ${ru() ? 'серий' : 'series'} · ${Number(shelf.bookCount || 0)} ${ru() ? 'томов' : 'volumes'}${shelf.ambiguous ? (ru() ? ' · порядок связей неоднозначен' : ' · relation order ambiguous') : ''}`;
    const hidden = Number(shelf.hiddenSeriesCount || 0);
    const toggle = hidden > 0 || shelf.expanded ? `<button class="library-shelf-toggle" data-library-shelf-toggle="${esc(shelf.key)}">${shelf.expanded ? (ru() ? 'Свернуть' : 'Collapse') : (ru() ? `Ещё ${hidden}` : `${hidden} more`)}</button>` : '';
    return `<section class="ln-franchise-group library-shelf" data-manga-franchise-ids="${ids}" data-library-shelf-key="${esc(shelf.key)}" data-library-shelf-ambiguous="${shelf.ambiguous ? '1' : '0'}"><div class="library-shelf-head"><div class="library-shelf-title"><strong>${esc(shelf.title || groups[0]?.title || '')}</strong><span class="library-shelf-meta">${counts}</span>${continueText ? `<span class="library-shelf-continue">${continueText}</span>` : ''}</div>${toggle}</div><div class="ln-franchise-series">${visible.map(mangaSeriesGroupHtml).join('')}</div></section>`;
  }

  function mangaLibraryGroups(books) {
    const builder = window.PudgeLibraryShelves?.build;
    if (!builder) return groupBooks(books).map(mangaSeriesGroupHtml).join('');
    // Manga currently has no authoritative cross-series relation graph in its
    // domain state. The shared model therefore preserves each local series as
    // its own group instead of guessing franchises from titles. If confirmed
    // franchise metadata is added later, this renderer already handles it.
    const shelves = builder(books, {
      seriesKey: book => String(book.series_key || normalizedSeriesKey(seriesTitle(book)) || `book:${book.id}`),
      seriesTitle,
      bookOrder: (a, b) => volumeNumber(a) - volumeNumber(b) || naturalCompare(a.title, b.title),
    });
    return shelves.map(mangaShelfHtml).join('');
  }

  function hydrateLibraryJiten(root, books) {
    for (const region of root.querySelectorAll('[data-manga-card-jiten],[data-manga-series-jiten]')) {
      const mediaId = Number(region.dataset.mangaCardJiten || region.dataset.mangaSeriesJiten || 0);
      const bookId = Number(region.dataset.jitenBook || 0);
      const book = books.find(item => bookId ? Number(item.id) === bookId : Number(anilistId(item)) === mediaId);
      if (!mediaId || !book) continue;
      window.PudgeLiteratureJiten?.hydrate?.(region, {
        media_id: mediaId,
        media_kind: 'manga',
        format: 'MANGA',
        media_status: 'FINISHED',
        title: seriesTitle(book),
        titles: [book.title, seriesTitle(book)].filter(Boolean)
      });
    }
  }

  // pudge-v0.7.23-manga-library-stability-v1
  function mangaLibraryRenderSignature(books) {
    return JSON.stringify([
      ru() ? 'ru' : 'en',
      (books || []).map(book => [
        Number(book.id), String(book.title || ''), String(book.series_key || ''),
        Number(book.page_count || 0), Number(book.read_pages || 0),
        Number(book.anilist_id || 0), Number(book.mean_score || 0), Number(book.user_score || 0),
        Boolean(book.ocr_complete), Number(book.ocr_cached_pages || 0), String(book.site_url || ''), String(book.franchise_key || ''), Number(book.franchise_order || 0),
      ]),
    ]);
  }

  async function renderLibrary(fetchState = true) {
    const root = $('mangaContent');
    if (!root || !API()) return;
    libraryRendering = true;
    try {
      if (fetchState) state = await API().manga_state();
      const books = state.books || [];
      const validIds = new Set(books.map(book => Number(book.id)));
      let selectionChanged = false;
      for (const id of [...selectedBookIds]) {
        if (!validIds.has(id)) { selectedBookIds.delete(id); selectionChanged = true; }
      }
      const renderSignature = mangaLibraryRenderSignature(books);
      const hadLibrary = Boolean(root.querySelector('.manga-v2-library'));
      if (hadLibrary && renderSignature === libraryRenderSignature) {
        applyLibrarySelection();
        if (selectionChanged) emitSelection();
        return;
      }
      const scrollHost = hadLibrary ? root.closest?.('.page-scroll') : null;
      const scrollAnchor = scrollHost ? window.PudgeLibraryShelves?.captureScrollAnchor?.(root, scrollHost) : null;
      root.innerHTML = `<div class="manga-v2-library"><button id="mangaImportV2" hidden>Import</button>${books.length ? `<div class="ln-grid">${mangaLibraryGroups(books)}</div>` : `<div class="empty">${ru() ? 'Добавьте CBZ/ZIP с изображениями страниц.' : 'Add a CBZ/ZIP containing page images.'}</div>`}</div>`;
      libraryRenderSignature = renderSignature;
      for (const book of books) {
        const expectedSignature = renderSignature;
        void resolveCover(book).then(async url => {
          if (!url || expectedSignature !== libraryRenderSignature) return;
          const ready = await decodeCover(url);
          if (!ready || expectedSignature !== libraryRenderSignature) return;
          const img = root.querySelector(`img[data-cover-book="${Number(book.id)}"]`);
          const placeholder = root.querySelector(`[data-cover-placeholder="${Number(book.id)}"]`);
          if (!img?.isConnected) return;
          // Keep the placeholder visible until the candidate has decoded. A
          // slow localhost asset or remote AniList response must never expose a
          // broken-image frame in the library grid.
          img.src = url;
          img.hidden = false;
          if (placeholder) placeholder.hidden = true;
          console.debug('cover.thumbnail.ready', {kind:'manga', id:Number(book.id), source:url});
        }).catch(error => {
          console.debug('cover.thumbnail.fallback', {kind:'manga', id:Number(book.id), error:String(error?.message || error)});
        });
      }
      applyLibrarySelection();
      hydrateLibraryJiten(root, books);
      requestAnimationFrame(() => {
        window.PudgeSeriesScroll?.focus?.(root);
        if (scrollHost && scrollAnchor) window.PudgeLibraryShelves?.restoreScrollAnchor?.(root, scrollHost, scrollAnchor);
      });
      if (selectionChanged) emitSelection();
    } catch (error) {
      root.innerHTML = `<div class="empty">${esc(error?.message || error)}</div>`;
    } finally {
      setTimeout(() => { libraryRendering = false; }, 0);
    }
  }

  async function injectBook(book) {
    if (!book) return;
    const id = Number(book.id);
    state = state || {books: []};
    state.books = [...(state.books || []).filter(item => Number(item.id) !== id), book];
    await renderLibrary(false);
    setTimeout(() => void renderLibrary(true), 900);
  }

  function buildReader() {
    let reader = $('mangaReaderV2');
    if (reader) return reader;
    reader = document.createElement('div');
    reader.id = 'mangaReaderV2';
    reader.className = 'manga-v2-reader';
    reader.innerHTML = `
      <header class="manga-v2-toolbar">
        <button data-manga-v2-action="close">← ${ru() ? 'Библиотека' : 'Library'}</button>
        <strong id="mangaV2Title"></strong>
        <div class="manga-v2-page-picker-shell">
          <button id="mangaV2PageLabel" class="manga-v2-page-label" data-manga-v2-action="page-picker" aria-haspopup="listbox" aria-expanded="false"></button>
          <div id="mangaV2PagePicker" class="manga-v2-page-picker" role="listbox" hidden></div>
        </div>
        <div id="mangaV2OcrProgress" class="manga-v2-ocr-progress" aria-live="polite"></div>
        <span class="spacer"></span>
        <button data-manga-v2-action="ocr-book">${ru() ? 'OCR тома' : 'OCR volume'}</button>
        <button data-manga-v2-action="fullscreen">${ru() ? 'Полный экран' : 'Fullscreen'}</button>
        <button data-manga-v2-action="settings">⚙</button>
      </header>
      <button id="mangaV2ToolbarReveal" class="manga-v2-toolbar-reveal" data-manga-v2-action="toolbar-show"
        title="${ru() ? 'Показать панель' : 'Show toolbar'}" aria-label="${ru() ? 'Показать панель' : 'Show toolbar'}">⌄</button>
      <main id="mangaV2Viewport" class="manga-v2-viewport">
        <button class="manga-v2-turn manga-v2-turn-left" data-manga-v2-action="next" aria-label="Next page">‹</button>
        <div id="mangaV2Pages" class="manga-v2-pages"></div>
        <button class="manga-v2-turn manga-v2-turn-right" data-manga-v2-action="previous" aria-label="Previous page">›</button>
      </main>
      <aside id="mangaV2Settings" class="manga-v2-settings">
        <div class="manga-v2-settings-head">
          <strong>${ru() ? 'Настройки читалки' : 'Reader settings'}</strong>
          <button data-manga-v2-action="settings">×</button>
        </div>
        <label>${ru() ? 'Режим' : 'Layout'}
          <select data-manga-setting="mode">
            <option value="single">${ru() ? 'Одна страница' : 'Single page'}</option>
            <option value="double">${ru() ? 'Разворот' : 'Double page'}</option>
            <option value="vertical">${ru() ? 'Вертикальная лента' : 'Vertical scroll'}</option>
          </select>
        </label>
        <label>${ru() ? 'Направление' : 'Direction'}
          <select data-manga-setting="direction">
            <option value="rtl">${ru() ? 'Справа налево' : 'Right to left'}</option>
            <option value="ltr">${ru() ? 'Слева направо' : 'Left to right'}</option>
          </select>
        </label>
        <label>${ru() ? 'Подгонка' : 'Fit'}
          <select data-manga-setting="fit">
            <option value="height">${ru() ? 'По высоте' : 'Fit height'}</option>
            <option value="width">${ru() ? 'По ширине' : 'Fit width'}</option>
            <option value="original">${ru() ? 'Оригинальный размер' : 'Original size'}</option>
          </select>
        </label>
        <label>${ru() ? 'Масштаб' : 'Zoom'} <output id="mangaV2ZoomValue"></output>
          <input type="range" min="50" max="250" step="5" data-manga-setting="zoom">
        </label>
        <label>${ru() ? 'Отступ между страницами' : 'Page gap'} <output id="mangaV2GapValue"></output>
          <input type="range" min="0" max="48" step="2" data-manga-setting="gap">
        </label>
        <label>${ru() ? 'Фон' : 'Background'}
          <select data-manga-setting="background">
            <option value="black">${ru() ? 'Чёрный' : 'Black'}</option>
            <option value="charcoal">${ru() ? 'Тёмно-серый' : 'Charcoal'}</option>
            <option value="paper">${ru() ? 'Светлый' : 'Light'}</option>
          </select>
        </label>
        <label class="manga-v2-check">
          <input type="checkbox" data-manga-setting="toolbar">
          ${ru() ? 'Показывать панель' : 'Show toolbar'}
        </label>
        <div class="manga-v2-shortcuts">
          ${ru()
            ? '←/→ — страницы · +/- — масштаб · 0 — 100% · F — полный экран · T — панель · O — OCR · Esc — закрыть'
            : '←/→ pages · +/- zoom · 0 reset · F fullscreen · T toolbar · O OCR · Esc close'}
        </div>
      </aside>
      `;
    document.body.appendChild(reader);
    syncSettingsControls();
    applyReaderSettings();
    installZoomGestures(reader);
    return reader;
  }

  function syncSettingsControls() {
    const reader = $('mangaReaderV2');
    if (!reader) return;
    for (const control of reader.querySelectorAll('[data-manga-setting]')) {
      const key = control.dataset.mangaSetting;
      if (control.type === 'checkbox') control.checked = Boolean(settings[key]);
      else control.value = String(settings[key]);
    }
    const zoom = $('mangaV2ZoomValue');
    const gap = $('mangaV2GapValue');
    if (zoom) zoom.textContent = `${settings.zoom}%`;
    if (gap) gap.textContent = `${settings.gap}px`;
  }

  function applyReaderSettings() {
    const reader = $('mangaReaderV2');
    if (!reader) return;
    reader.dataset.mode = settings.mode;
    reader.dataset.direction = settings.direction;
    reader.dataset.fit = settings.fit;
    reader.dataset.background = settings.background;
    reader.classList.toggle('toolbar-hidden', !settings.toolbar);
    reader.style.setProperty('--manga-gap', `${Number(settings.gap || 0)}px`);
    requestAnimationFrame(applyPageSizing);
    syncSettingsControls();
  }

  async function getPage(index) {
    const clamped = Math.max(0, Math.min(Number(index), currentPageCount - 1));
    const key = `${Number(currentBook.id)}:${clamped}`;
    if (pageCache.has(key)) {
      const cached = pageCache.get(key);
      pageCache.delete(key);
      pageCache.set(key, cached);
      return cached;
    }
    const page = await API().manga_page(Number(currentBook.id), clamped);
    pageCache.set(key, page);
    while (pageCache.size > PAGE_CACHE_LIMIT) {
      pageCache.delete(pageCache.keys().next().value);
    }
    return page;
  }

  // pudge-v0.7.23-manga-explicit-read-progress-v1
  async function setResumePage(pageIndex) {
    if (!currentBook || currentPageCount <= 0 || !API()?.manga_set_position) return;
    const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
    if (Number(currentBook.position || 0) === index) return;
    currentBook.position = index;
    try { await API().manga_set_position(Number(currentBook.id), index); } catch (_) {}
  }

  async function markReadThrough(pageIndex) {
    if (!currentBook || currentPageCount <= 0 || !API()?.manga_mark_read) return;
    const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
    const targetPages = index + 1;
    if (targetPages <= Number(currentBook.read_pages || 0)) return;
    currentBook.read_pages = targetPages;
    try {
      const updated = await API().manga_mark_read(Number(currentBook.id), index);
      if (updated?.read_pages != null) currentBook.read_pages = Number(updated.read_pages);
    } catch (_) {}
  }

  async function persistVisiblePage() {
    if (!currentBook || currentPageCount <= 0) return;
    await setResumePage(currentPage);
    if (settings.mode === 'vertical') {
      const viewport = $('mangaV2Viewport');
      const atEnd = viewport && viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 4;
      if (atEnd) await markReadThrough(currentPageCount - 1);
      return;
    }
    const visibleEnd = settings.mode === 'double'
      ? Math.min(currentPageCount - 1, currentPage + Math.max(1, pagedVisibleCount) - 1)
      : currentPage;
    // Final page counts immediately because there is no following turn.
    if (visibleEnd >= currentPageCount - 1) await markReadThrough(currentPageCount - 1);
  }


  function currentStudyBackend(payload = null) {
    return String(
      payload?.settings?.study_backend ||
      window.ui?.lnState?.settings?.study_backend ||
      'jiten'
    );
  }

  function textKey(bookId, pageIndex) {
    return `${Number(bookId)}:${Number(pageIndex)}`;
  }

  function mangaDebugRecord(event, extra = {}) {
    if (!currentBook) return;
    mangaDebugEvents.push({
      event: String(event || 'event'),
      wall_time: new Date().toISOString(),
      monotonic_ms: Math.round(performance.now() * 10) / 10,
      book_id: Number(currentBook.id),
      page_index: Number(currentPage),
      mode: String(settings.mode || ''),
      zoom: Number(settings.zoom || 100),
      ...extra,
    });
    if (mangaDebugEvents.length > 600) mangaDebugEvents.splice(0, mangaDebugEvents.length - 600);
  }

  function mangaDebugPoint(event, phase) {
    if (!currentBook || !$('mangaReaderV2')?.classList.contains('open')) return;
    const frame = event.target?.closest?.('.manga-v2-page-frame') ||
      document.elementFromPoint(event.clientX, event.clientY)?.closest?.('.manga-v2-page-frame');
    if (!frame) return;
    const image = frame.querySelector('img');
    const imageRect = image?.getBoundingClientRect();
    const region = event.target?.closest?.('.manga-v2-text-region');
    const token = event.target?.closest?.('[data-pudge-study-token]');
    const elements = document.elementsFromPoint(event.clientX, event.clientY)
      .slice(0, 8)
      .map(node => ({
        tag: String(node.tagName || '').toLowerCase(),
        id: String(node.id || ''),
        class: String(node.className || ''),
        page_index: node.closest?.('.manga-v2-page-frame')?.dataset?.pageIndex ?? null,
        region_index: node.closest?.('.manga-v2-text-region')?.dataset?.regionIndex ?? null,
      }));
    mangaDebugRecord(`pointer_${phase}`, {
      client_x: Number(event.clientX),
      client_y: Number(event.clientY),
      normalized_x: imageRect?.width ? (event.clientX - imageRect.left) / imageRect.width : null,
      normalized_y: imageRect?.height ? (event.clientY - imageRect.top) / imageRect.height : null,
      frame_page_index: Number(frame.dataset.pageIndex),
      region_index: region ? Number(region.dataset.regionIndex) : null,
      token_text: token ? String(token.textContent || '').trim() : '',
      target_text: String(event.target?.textContent || '').trim().slice(0, 160),
      elements,
    });
  }

  function mangaDebugFrameSnapshot(frame) {
    const pageIndex = Number(frame?.dataset?.pageIndex);
    const image = frame?.querySelector('img');
    const imageRect = image?.getBoundingClientRect();
    const frameRect = frame?.getBoundingClientRect();
    const overlays = [...(frame?.querySelectorAll('.manga-v2-text-region') || [])].map(node => {
      const rect = node.getBoundingClientRect();
      const content = node.querySelector('.manga-v2-region-content');
      return {
        region_index: Number(node.dataset.regionIndex),
        text: String(node.getAttribute('aria-label') || ''),
        rendered_text: String(content?.textContent || '').trim(),
        parsed_token_count: content?.querySelectorAll?.('[data-pudge-study-token]')?.length || 0,
        effective_orientation: String(node.dataset.effectiveOrientation || ''),
        orientation_reason: String(node.dataset.orientationReason || ''),
        source: String(node.dataset.regionSource || ''),
        recognizer: String(node.dataset.regionOcrBackend || ''),
        detector: String(node.dataset.regionDetector || ''),
        geometry_source: String(node.dataset.geometrySource || ''),
        css: {
          left: node.style.left,
          top: node.style.top,
          width: node.style.width,
          height: node.style.height,
          writing_mode: getComputedStyle(content || node).writingMode,
        },
        rect: {left: rect.left, top: rect.top, width: rect.width, height: rect.height},
        normalized_to_image: imageRect?.width && imageRect?.height ? {
          left: (rect.left - imageRect.left) / imageRect.width,
          top: (rect.top - imageRect.top) / imageRect.height,
          width: rect.width / imageRect.width,
          height: rect.height / imageRect.height,
        } : null,
      };
    });
    return {
      page_index: pageIndex,
      frame_rect: frameRect ? {left: frameRect.left, top: frameRect.top, width: frameRect.width, height: frameRect.height} : null,
      image: image ? {
        natural_width: Number(image.naturalWidth || 0),
        natural_height: Number(image.naturalHeight || 0),
        rect: imageRect ? {left: imageRect.left, top: imageRect.top, width: imageRect.width, height: imageRect.height} : null,
        alt: String(image.alt || ''),
      } : null,
      raw_result: textRegionResultCache.get(textKey(currentBook.id, pageIndex)) || null,
      sorted_regions: textRegionCache.get(textKey(currentBook.id, pageIndex)) || [],
      overlays,
    };
  }

  async function exportMangaOcrDebug() {
    if (!currentBook || !API()?.manga_export_ocr_debug) return null;
    const frames = [...($('mangaV2Pages')?.querySelectorAll('.manga-v2-page-frame') || [])]
      .filter(frame => frame.querySelector('img'))
      .map(mangaDebugFrameSnapshot);
    const payload = {
      schema: 1,
      book: {
        id: Number(currentBook.id),
        title: String(currentBook.title || ''),
        series_title: seriesTitle(currentBook),
        page_count: Number(currentPageCount),
        anilist_id: Number(currentBook.anilist_id || 0),
      },
      page_index: Number(currentPage),
      reader: {
        settings: {...settings},
        device_pixel_ratio: Number(window.devicePixelRatio || 1),
        viewport: {width: window.innerWidth, height: window.innerHeight},
        scroll: {
          left: Number($('mangaV2Viewport')?.scrollLeft || 0),
          top: Number($('mangaV2Viewport')?.scrollTop || 0),
        },
      },
      frames,
      events: mangaDebugEvents.slice(-600),
      selection: String(window.getSelection?.()?.toString?.() || ''),
    };
    const result = await API().manga_export_ocr_debug(payload);
    window.toast?.(ru() ? `Лог OCR сохранён: ${result.path}` : `OCR log saved: ${result.path}`);
    return result;
  }

  function clearMangaTransientOverlays() {
    window.PudgeReadingTools?.closeAll?.();
  }

  function mangaRegionReadingOrder(regions) {
    return [...(Array.isArray(regions) ? regions : [])].sort((left, right) => {
      const leftTop = 1 - Number(left?.y || 0) - Number(left?.height || 0);
      const rightTop = 1 - Number(right?.y || 0) - Number(right?.height || 0);
      // Manga dialogue is primarily read from top to bottom.  Regions on the
      // same visual row follow Japanese right-to-left order.
      if (Math.abs(leftTop - rightTop) > .045) return leftTop - rightTop;
      const leftEdge = Number(left?.x || 0) + Number(left?.width || 0);
      const rightEdge = Number(right?.x || 0) + Number(right?.width || 0);
      return rightEdge - leftEdge;
    });
  }

  // v96p37: a narrow OCR lane can read printed furigana as a separate word.
  // Hide only conservative geometric duplicates from the study overlay; keep
  // the raw OCR regions and the original page image untouched.
  function mangaRubyDuplicateRegionIndices(regions) {
    const rows = Array.isArray(regions) ? regions : [];
    const duplicates = new Set();
    rows.forEach((ruby, index) => {
      const text = String(ruby?.text || '').trim();
      if (ruby?.orientation !== 'vertical' || !/^[\u3041-\u3096\u30a1-\u30fa\u30fc]{2,8}$/.test(text)) return;
      const rx = Number(ruby.x), ry = Number(ruby.y), rw = Number(ruby.width), rh = Number(ruby.height);
      if (![rx, ry, rw, rh].every(Number.isFinite) || rw <= 0 || rh <= 0) return;
      for (const base of rows) {
        if (base === ruby || base?.orientation !== 'vertical' || !/[\u3400-\u9fff]{2}/.test(String(base?.text || ''))) continue;
        const bx = Number(base.x), by = Number(base.y), bw = Number(base.width), bh = Number(base.height);
        if (![bx, by, bw, bh].every(Number.isFinite) || bw <= 0 || bh <= 0) continue;
        // Ruby is small, alongside the right-hand part of a kanji column and
        // contained in its vertical range. Ordinary neighbouring kana dialogue
        // has comparable dimensions, and must remain independently clickable.
        if (rw / bw > .58 || rh / bh > .75 || rx < bx + bw * .35) continue;
        const verticalOverlap = Math.max(0, Math.min(ry + rh, by + bh) - Math.max(ry, by)) / rh;
        const horizontalGap = Math.max(0, Math.max(rx, bx) - Math.min(rx + rw, bx + bw));
        if (verticalOverlap >= .83 && horizontalGap <= .012) {
          duplicates.add(index);
          break;
        }
      }
    });
    return duplicates;
  }

  function normalizedStudyRegionText(region) {
    return String(region?.text || '').replace(/[\r\n]+/g, ' ').trim();
  }

  function mangaPageStudyContext(pageIndex) {
    if (!currentBook) return {text: '', offsets: new Map()};
    const regions = textRegionCache.get(textKey(currentBook.id, pageIndex)) || [];
    const offsets = new Map();
    const parts = [];
    let cursor = 0;
    const rubyDuplicates = mangaRubyDuplicateRegionIndices(regions);
    regions.forEach((region, regionIndex) => {
      if (rubyDuplicates.has(regionIndex)) return;
      const text = normalizedStudyRegionText(region);
      offsets.set(Number(regionIndex), cursor);
      parts.push(text);
      cursor += text.length + 1; // newline separator between OCR regions/bubbles
    });
    return {text: parts.join('\n'), offsets};
  }

  function renderRegionContent(target, region, payload = null) {
    if (!target || !region) return;
    let content = target.querySelector('.manga-v2-region-content');
    if (!content) {
      content = document.createElement('div');
      content.className = 'manga-v2-region-content';
      target.appendChild(content);
    }
    const tools = window.PudgeReadingTools;
    const pageIndex = Number(target.dataset?.pageIndex ?? currentPage);
    const regionIndex = Number(target.dataset?.regionIndex ?? 0);
    const pageContext = mangaPageStudyContext(pageIndex);
    const html = payload && tools?.study?.renderParsedText
      ? tools.study.renderParsedText(payload, {
          backend: currentStudyBackend(payload),
          contextText: pageContext.text,
          contextOffset: Number(pageContext.offsets.get(regionIndex) || 0),
          mediaContext: currentBook ? {kind: 'manga', book_id: Number(currentBook.id), page_index: pageIndex} : null,
        })
      : '';
    if (html) content.innerHTML = html;
    else content.textContent = String(region.text || '');
  }

  function effectiveRegionOrientation(region, rawWidth, rawHeight) {
    if (region?.orientation === 'vertical') return {vertical: true, reason: region.orientation_reason || 'backend'};
    if (rawHeight > rawWidth * 1.05) return {vertical: true, reason: 'tall-geometry'};
    const compact = String(region?.text || region?.raw_text || '').replace(/\s+/g, '');
    const japanese = [...compact].filter(char => /[\u3040-\u30ff\u3400-\u9fff々〆ヶ]/.test(char)).length;
    const japaneseRatio = japanese / Math.max(1, [...compact].length);
    const detector = String(region?.detector || '');
    const confidence = Number(region?.confidence || 0);
    const lowConfidenceVertical = detector.includes('vision-rectangles') && confidence <= .35 &&
      japanese >= 2 && japaneseRatio >= .55;
    if (lowConfidenceVertical) return {vertical: true, reason: 'low-confidence-vision-japanese'};
    const classicMultiColumn = rawHeight >= .065 && rawWidth <= .34 && rawHeight >= rawWidth * .34;
    // A three/four-column bubble can be much wider than tall after Vision merges
    // neighbouring columns. This is exactly the common manga case where the old
    // aspect-ratio rule placed the invisible text left-to-right across the page.
    const wideMultiColumn = rawHeight >= .08 && rawWidth <= .48 && rawHeight >= rawWidth * .20 &&
      detector.includes('vision-rectangles');
    const multiColumn = japanese >= 6 && japaneseRatio >= .65 &&
      (classicMultiColumn || wideMultiColumn) &&
      (detector.includes('vision-rectangles') || rawHeight >= rawWidth * .45);
    return {
      vertical: multiColumn,
      reason: multiColumn ? (classicMultiColumn ? 'japanese-multicolumn-geometry' : 'japanese-wide-multicolumn-geometry') : 'horizontal',
    };
  }

  function renderTextLayer(frame, pageIndex) {
    if (!frame || !currentBook) return;
    const key = textKey(currentBook.id, pageIndex);
    const regions = textRegionCache.get(key) || [];
    const rubyDuplicates = mangaRubyDuplicateRegionIndices(regions);
    let layer = frame.querySelector('.manga-v2-text-layer');
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'manga-v2-text-layer';
      frame.appendChild(layer);
    }
    layer.innerHTML = regions.map((region, regionIndex) => {
      if (rubyDuplicates.has(regionIndex)) return '';
      const rawX = Math.max(0, Math.min(1, Number(region.x || 0)));
      const rawY = Math.max(0, Math.min(1, Number(region.y || 0)));
      const rawWidth = Math.max(0, Math.min(1 - rawX, Number(region.width || 0)));
      const rawHeight = Math.max(0, Math.min(1 - rawY, Number(region.height || 0)));
      // Vision boxes hug glyphs very tightly. A small visual-only expansion
      // makes the whole speech area easy to enter without changing the crop
      // MangaOCR received or merging neighbouring bubbles.
      const orientation = effectiveRegionOrientation(region, rawWidth, rawHeight);
      const isVertical = orientation.vertical;
      // Token layout must stay on the geometry used for OCR. Click forgiveness
      // is handled separately by mangaRegionAtPoint against rawRegionRect.
      const x = rawX;
      const y = rawY;
      const width = rawWidth;
      const height = rawHeight;
      const top = Math.max(0, 1 - y - height);
      const edge = x > .58 ? ' edge-right' : '';
      const vertical = isVertical ? ' vertical-text' : '';
      return `<div class="manga-v2-text-region${edge}${vertical}" tabindex="0"
        data-page-index="${Number(pageIndex)}" data-region-index="${regionIndex}"
        data-effective-orientation="${isVertical ? 'vertical' : 'horizontal'}"
        data-orientation-reason="${esc(orientation.reason)}"
        data-region-source="${esc(region.source || '')}"
        data-region-ocr-backend="${esc(region.recognizer || '')}"
        data-region-detector="${esc(region.detector || '')}"
        data-geometry-source="${esc(region.geometry_source || '')}"
        data-raw-x="${rawX}" data-raw-y="${rawY}" data-raw-width="${rawWidth}" data-raw-height="${rawHeight}"
        data-pudge-translate-root
        data-pudge-media-id="${Number(currentBook.anilist_id || 0)}"
        data-pudge-translate-language="${ru() ? 'ru' : 'en'}"
        aria-label="${esc(region.text || '')}"
        style="left:${x * 100}%;top:${top * 100}%;width:${width * 100}%;height:${height * 100}%">
          <div class="manga-v2-region-content">${esc(region.text || '')}</div>
          <div class="manga-v2-selection-content"
            data-manga-copy-surface="${esc(region.text || '')}"
            title="${esc(ru() ? 'Выделение OCR-текста; тройной клик — весь бабл' : 'Select OCR text; triple-click selects the whole bubble')}">${esc(region.text || '')}</div>
        </div>`;
    }).join('');
    // renderTextLayer is called repeatedly while background OCR preparation is
    // polling. Re-apply parsed payloads immediately; otherwise every refresh
    // silently replaces clickable study spans with plain text.
    regions.forEach((region, regionIndex) => {
      if (rubyDuplicates.has(regionIndex)) return;
      const parsed = textParseCache.get(`${key}:${Number(regionIndex)}`);
      if (!parsed) return;
      const target = layer.querySelector(`.manga-v2-text-region[data-region-index="${Number(regionIndex)}"]`);
      if (target) renderRegionContent(target, region, parsed);
    });
    requestAnimationFrame(() => mangaDebugRecord('overlay_rendered', {
      page_index: Number(pageIndex),
      region_count: regions.length,
      overlay_count: layer.querySelectorAll('.manga-v2-text-region').length,
    }));
  }

  async function parseRegionText(pageIndex, regionIndex, region, generation = textGeneration) {
    if (!currentBook || !region || !API()?.study_parse_text) return null;
    const bookId = Number(currentBook.id);
    const key = `${textKey(bookId, pageIndex)}:${Number(regionIndex)}`;
    if (textParseCache.has(key)) {
      const payload = textParseCache.get(key);
      const target = $('mangaV2Pages')?.querySelector(`.manga-v2-text-region[data-page-index="${Number(pageIndex)}"][data-region-index="${Number(regionIndex)}"]`);
      if (target) renderRegionContent(target, region, payload);
      return payload;
    }
    if (textParseInflight.has(key)) return textParseInflight.get(key);
    const text = String(region.text || '').replace(/[\r\n]+/g, ' ').trim();
    if (!text) return null;
    const request = (async () => {
      try {
        const payload = await API().study_parse_text(text);
        if (generation !== textGeneration || !currentBook || Number(currentBook.id) !== bookId) return null;
        textParseCache.set(key, payload);
        const target = $('mangaV2Pages')?.querySelector(`.manga-v2-text-region[data-page-index="${Number(pageIndex)}"][data-region-index="${Number(regionIndex)}"]`);
        if (target) renderRegionContent(target, region, payload);
        return payload;
      } catch (error) {
        console.debug?.('Manga study parse unavailable:', error);
        return null;
      } finally {
        textParseInflight.delete(key);
      }
    })();
    textParseInflight.set(key, request);
    return request;
  }

  async function parseRegionsSequentially(pageIndex, regions, generation = textGeneration) {
    const rubyDuplicates = mangaRubyDuplicateRegionIndices(regions);
    for (let regionIndex = 0; regionIndex < regions.length; regionIndex++) {
      if (generation !== textGeneration) return;
      if (rubyDuplicates.has(regionIndex)) continue;
      await parseRegionText(pageIndex, regionIndex, regions[regionIndex], generation);
    }
  }

  async function loadTextRegions(
    pageIndex,
    {refresh = false, showProgress = false, parse = false, cachedOnly = false} = {},
  ) {
    if (!currentBook || !API()?.manga_text_regions) return null;
    // Volume OCR is atomic from the reader's point of view.  Batch OCR may
    // persist individual page caches as it advances, but exposing those pages
    // immediately makes overlays appear/disappear while the user is reading.
    // Keep every page visually raw until the whole volume reaches complete.
    if (!currentBook.ocr_complete) {
      const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
      textRegionCache.delete(textKey(Number(currentBook.id), index));
      const frame = $('mangaV2Pages')?.querySelector(`[data-page-index="${index}"]`);
      frame?.querySelector('.manga-v2-text-layer')?.replaceChildren();
      return {regions: [], cached: false, suppressed_until_volume_complete: true};
    }
    const generation = textGeneration;
    const bookId = Number(currentBook.id);
    const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
    const key = textKey(bookId, index);
    if (!refresh && textRegionCache.has(key)) {
      mangaDebugRecord('ocr_memory_cache', {page_index:index, region_count:(textRegionCache.get(key) || []).length});
      const frame = $('mangaV2Pages')?.querySelector(`[data-page-index="${index}"]`);
      if (frame) renderTextLayer(frame, index);
      const regions = textRegionCache.get(key) || [];
      if (parse) await parseRegionsSequentially(index, regions, generation);
      return {regions, cached:true};
    }
    try {
      const result = await API().manga_text_regions(
        bookId,
        index,
        Boolean(refresh),
        Boolean(cachedOnly),
      );
      if (generation !== textGeneration || !currentBook || Number(currentBook.id) !== bookId) return null;
      const regions = mangaRegionReadingOrder(
        Array.isArray(result?.regions) ? result.regions : [],
      );
      // pudge-v0.7.27-manga-visible-page-foreground-ocr-v1
      // Cached-only polling is intentionally cheap for preloaded/background
      // pages. The page the user is actually reading must not wait for the
      // whole-volume batch to finish its all-pages Vision detection phase.
      const foregroundRetry = Boolean(
        cachedOnly &&
        index === Number(currentPage) &&
        result?.available &&
        !result?.cached &&
        regions.length === 0
      );
      if (foregroundRetry && !textForegroundOcrInflight.has(key)) {
        textForegroundOcrInflight.add(key);
        mangaDebugRecord('ocr_foreground_retry', {
          page_index:index,
          reason:'visible-cache-miss',
        });
        try {
          return await loadTextRegions(index, {
            refresh:false,
            showProgress:true,
            parse,
            cachedOnly:false,
          });
        } finally {
          textForegroundOcrInflight.delete(key);
        }
      }
      textRegionResultCache.set(key, {
        book_id: Number(result?.book_id || bookId),
        page_index: Number(result?.page_index ?? index),
        available: Boolean(result?.available),
        cached: Boolean(result?.cached),
        artifact: Boolean(result?.artifact),
        region_count: regions.length,
      });
      mangaDebugRecord('ocr_result', {
        page_index:index,
        refresh:Boolean(refresh),
        cached_only:Boolean(cachedOnly),
        available:Boolean(result?.available),
        cached:Boolean(result?.cached),
        artifact:Boolean(result?.artifact),
        region_count:regions.length,
        regions:regions.map((region, order) => ({order, ...region})),
      });
      if (result?.cached || regions.length || refresh) textRegionCache.set(key, regions);
      else textRegionCache.delete(key);
      if (refresh) {
        for (const cacheKey of [...textParseCache.keys()]) if (cacheKey.startsWith(`${key}:`)) textParseCache.delete(cacheKey);
      }
      const frame = $('mangaV2Pages')?.querySelector(`[data-page-index="${index}"]`);
      if (frame) renderTextLayer(frame, index);
      if (parse) await parseRegionsSequentially(index, regions, generation);
      return result;
    } catch (error) {
      console.debug?.('Manga OCR regions unavailable:', error);
      return null;
    } finally {
      // Volume-level progress owns the toolbar status.  Per-page fetches must
      // never clear it while a background OCR job is running.
    }
  }

  function visiblePageIndices() {
    return [...($('mangaV2Pages')?.querySelectorAll('[data-page-index]') || [])]
      .filter(frame => !frame.classList.contains('manga-v2-lazy') || frame.dataset.loaded === '1')
      .map(frame => Number(frame.dataset.pageIndex))
      .filter(Number.isFinite);
  }

  function stopPreparationPoll() {
    preparationPollGeneration += 1;
    if (preparationPollTimer) clearTimeout(preparationPollTimer);
    preparationPollTimer = null;
  }

  function suppressPartialVolumeOcr() {
    if (!currentBook || currentBook.ocr_complete) return;
    textGeneration += 1;
    textRegionCache.clear();
    textRegionResultCache.clear();
    textParseCache.clear();
    textParseInflight.clear();
    for (const layer of $('mangaV2Pages')?.querySelectorAll('.manga-v2-text-layer') || []) {
      layer.replaceChildren();
    }
  }

  async function refreshVisibleTextRegions() {
    if (!currentBook?.ocr_complete) {
      suppressPartialVolumeOcr();
      return;
    }
    const indices = visiblePageIndices();
    await Promise.all(indices.map(index => loadTextRegions(index, {
      cachedOnly: true,
      parse: true,
    })));
  }

  function mangaOcrProgressText(status) {
    const stateName = String(status?.state || '');
    const active = Boolean(status?.running);
    const total = Math.max(0, Number(status?.total_pages || 0));
    const cached_pages = Math.max(0, Number(status?.cached_pages || 0));
    const completed = Math.max(0, Number(
      status?.completed ?? ((status?.ready_pages || 0) + (status?.empty_pages || 0))
    ));
    const failed = Math.max(0, Number(status?.failed || status?.failed_pages || 0));
    const queued = Math.max(0, Number(status?.queued || 0));
    const processing = Math.max(0, Number(status?.processing || 0));
    const processed = Math.max(
      completed,
      Math.max(0, Number(status?.processed_pages ?? completed)),
    );
    const prepared = Math.max(
      processed,
      Math.max(0, Number(status?.prepared_pages ?? processed)),
    );
    const phase = String(status?.phase || '');
    const pages = total > 0 ? `${Math.min(processed, total)}/${total}` : String(processed || cached_pages);
    const preparedPages = total > 0 ? `${Math.min(prepared, total)}/${total}` : String(prepared);
    if (active && String(status?.wait_reason || '') === 'foreground') {
      return `${ru() ? 'OCR ждёт окончания просмотра аниме' : 'OCR waiting for playback to finish'} · ${pages}`;
    }
    if (active && phase === 'detecting') {
      return `${ru() ? 'Ищу области текста' : 'Detecting text'} · ${preparedPages} · OCR ${pages}`;
    }
    if (active && phase === 'yielded') {
      return `${ru() ? 'OCR уступил ручной задаче' : 'OCR yielded to manual task'} · ${pages}`;
    }
    if (active && stateName === 'parsing') {
      return `${ru() ? 'Готовлю Jiten' : 'Preparing Jiten'} ${Number(status?.parsed_regions || 0)}/${Number(status?.total_regions || 0)} · OCR ${pages}`;
    }
    if (active && (processing > 0 || stateName === 'running')) {
      const page = Number(status?.current_page || 0);
      return `OCR · ${pages}${page > 0 ? ` · ${ru() ? 'страница' : 'page'} ${page} ${ru() ? 'обрабатывается…' : 'processing…'}` : ''}`;
    }
    if (active && (queued > 0 || stateName === 'queued')) {
      return `${ru() ? 'OCR в очереди' : 'OCR queued'} · ${pages}${queued > 0 ? ` · ${queued}` : ''}`;
    }
    if (failed > 0 || stateName === 'failed') {
      return `${ru() ? 'OCR ошибок' : 'OCR failed'} · ${pages}${failed > 0 ? ` · ${failed}` : ''}`;
    }
    if (status?.complete || stateName === 'ready' || (total > 0 && completed >= total)) {
      return `${ru() ? 'OCR готов' : 'OCR ready'} · ${pages}`;
    }
    // An incomplete volume with no live worker is idle and not implicitly queued.
    // The explicit OCR Volume button remains available.
    return '';
  }

  function mangaOcrJobActive(status) {
    return Boolean(status?.running);
  }

  function syncMangaOcrUi(status) {
    currentBookOcrStatus = status ? {...status} : null;
    if (currentBook && status) {
      currentBook.ocr_cached_pages = Number(status.cached_pages || 0);
      currentBook.ocr_complete = Boolean(status.complete);
    }
    const progress = $('mangaV2OcrProgress');
    if (progress) {
      const text = mangaOcrProgressText(status);
      progress.textContent = text;
      progress.hidden = !text;
    }
    const button = document.querySelector('[data-manga-v2-action="ocr-book"]');
    if (button) {
      const active = mangaOcrJobActive(status);
      button.disabled = active;
      button.classList.toggle('busy', active);
      button.setAttribute('aria-disabled', active ? 'true' : 'false');
      button.textContent = active
        ? (ru() ? 'OCR идёт…' : 'OCR running…')
        : (currentBook?.ocr_complete
          ? (ru() ? 'Повторить OCR' : 'Rebuild OCR')
          : (ru() ? 'OCR тома' : 'OCR volume'));
    }
  }

  async function pollCurrentBookPreparation(bookId) {
    stopPreparationPoll();
    const generation = preparationPollGeneration;
    const poll = async () => {
      if (generation !== preparationPollGeneration || !currentBook || Number(currentBook.id) !== Number(bookId)) return;
      if (preparationPollInFlight) {
        preparationPollTimer = setTimeout(() => void poll(), 750);
        return;
      }
      preparationPollInFlight = true;
      let active = false;
      try {
        const status = await API().manga_ocr_book_status(Number(bookId));
        if (generation !== preparationPollGeneration || !currentBook || Number(currentBook.id) !== Number(bookId)) return;
        const wasComplete = Boolean(currentBook.ocr_complete);
        syncMangaOcrUi(status);
        active = mangaOcrJobActive(status);
        if (!currentBook.ocr_complete) {
          suppressPartialVolumeOcr();
        } else if (!wasComplete) {
          textRegionCache.clear();
          textRegionResultCache.clear();
          textParseCache.clear();
          textParseInflight.clear();
          await refreshVisibleTextRegions();
        }
      } catch (error) {
        console.debug?.('Manga preparation status unavailable:', error);
        active = true;
      } finally {
        preparationPollInFlight = false;
        if (active && generation === preparationPollGeneration && currentBook && Number(currentBook.id) === Number(bookId)) {
          preparationPollTimer = setTimeout(() => void poll(), 750);
        }
      }
    };
    void poll();
  }

  async function ensureCurrentBookPrepared() {
    if (!currentBook || !state.ocr_available) return;
    const bookId = Number(currentBook.id);
    try {
      // Opening a volume is read-only with respect to OCR scheduling. Only the
      // explicit OCR Volume action may create a new batch. If a batch that the
      // user already started is still alive, reconnect the UI to that job.
      const status = await API().manga_ocr_book_status(bookId);
      if (!currentBook || Number(currentBook.id) !== bookId) return;
      syncMangaOcrUi(status);
      if (!currentBook.ocr_complete) suppressPartialVolumeOcr();
      if (mangaOcrJobActive(status)) void pollCurrentBookPreparation(bookId);
    } catch (error) {
      console.debug?.('Manga background preparation unavailable:', error);
    }
  }

  async function recognizeWholeBook() {
    if (!currentBook) return;
    const bookId = Number(currentBook.id);
    stopPreparationPoll();
    const progress = $('mangaV2OcrProgress');
    const button = document.querySelector('[data-manga-v2-action="ocr-book"]');
    if (button) { button.disabled = true; button.classList.add('busy'); }
    try {
      // Explicit toolbar action means rebuild, not "fill only missing pages".
      // Passing refresh=true invalidates the current generation before the
      // background worker starts, so a complete/stale cache cannot make this
      // button finish instantly without traversing the volume.
      const started = await API().start_manga_ocr_book(bookId, true);
      syncMangaOcrUi(started);
      void pollCurrentBookPreparation(bookId);
      suppressPartialVolumeOcr();
      while (currentBook && Number(currentBook.id) === bookId) {
        const status = await API().manga_ocr_book_status(bookId);
        syncMangaOcrUi(status);
        if (!currentBook.ocr_complete) suppressPartialVolumeOcr();
        if (!mangaOcrJobActive(status)) break;
        await new Promise(resolve => setTimeout(resolve, 800));
      }
      textRegionCache.clear();
      textRegionResultCache.clear();
      textParseCache.clear();
      textParseInflight.clear();
      if (currentBook && Number(currentBook.id) === bookId && currentBook.ocr_complete) {
        await refreshVisibleTextRegions();
      }
    } finally {
      if (currentBook && Number(currentBook.id) === bookId) {
        try { syncMangaOcrUi(await API().manga_ocr_book_status(bookId)); } catch (_) {
          if (button) { button.disabled = false; button.classList.remove('busy'); }
        }
      }
    }
  }

  function closeMangaContextMenu() {
    const menu = $('contextMenu');
    if (menu && mangaContextBook) {
      menu.classList.remove('open');
      menu.innerHTML = '';
    }
    mangaContextBook = null;
  }

  function positionMangaContextMenu(menu, x, y) {
    menu.classList.add('open');
    const rect = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
  }

  function showMangaContextMenu(book, x, y) {
    if (!book) return;
    mangaContextBook = book;
    mangaContextSeries = null;
    const menu = $('contextMenu');
    if (!menu) return;
    const linked = Boolean(anilistId(book));
    const selected = selectedBookIds.has(Number(book.id));
    const select = `<button data-manga-context-action="select">${selected ? (ru() ? 'Снять выделение' : 'Deselect') : (ru() ? 'Выделить' : 'Select')}</button>`;
    const openAniList = linked
      ? `<button data-manga-context-action="anilist">${ru() ? 'Открыть AniList' : 'Open AniList'}</button>`
      : '';
    const link = `<button data-manga-context-action="anilist-search">${linked
      ? (ru() ? 'Изменить AniList' : 'Change AniList')
      : (ru() ? 'Найти в AniList' : 'Find on AniList')}</button>`;
    const names = '';
    const ocr = book.ocr_complete
      ? (ru() ? 'Повторить OCR тома' : 'Rebuild volume OCR')
      : (ru() ? 'OCR тома' : 'OCR volume');
    menu.innerHTML = `
      ${select}<button data-manga-context-action="read">${ru() ? 'Читать' : 'Read'}</button>
      ${openAniList}${link}${names}
      <button data-manga-context-action="ocr-book">${ocr}</button>
      <button data-manga-context-action="reset-progress">${ru() ? 'Сбросить прогресс чтения' : 'Reset reading progress'}</button>
      <button class="danger-action" data-manga-context-action="remove-series">${ru() ? 'Удалить из Pudge' : 'Remove from Pudge'}</button>`;
    positionMangaContextMenu(menu, x, y);
  }

  function showMangaSeriesContextMenu(group, x, y) {
    if (!group?.books?.length) return;
    mangaContextSeries = group;
    const book = group.books.find(item => anilistId(item)) || group.books[0];
    mangaContextBook = book;
    const menu = $('contextMenu');
    if (!menu) return;
    const linked = Boolean(anilistId(book));
    const allSelected = group.books.every(item => selectedBookIds.has(Number(item.id)));
    const allOcrReady = group.books.every(item => Boolean(item.ocr_complete));
    const select = `<button data-manga-context-action="select-series">${allSelected ? (ru() ? 'Снять выделение со всех томов' : 'Deselect all volumes') : (ru() ? 'Выделить все тома' : 'Select all volumes')}</button>`;
    const openAniList = linked
      ? `<button data-manga-context-action="anilist">${ru() ? 'Открыть AniList' : 'Open AniList'}</button>`
      : '';
    const score = linked
      ? `<button data-manga-context-action="score-series">${ru() ? 'Поставить оценку' : 'Set score'}</button>`
      : '';
    const link = `<button data-manga-context-action="anilist-search">${linked
      ? (ru() ? 'Изменить AniList' : 'Change AniList')
      : (ru() ? 'Найти в AniList' : 'Find on AniList')}</button>`;
    const ocr = allOcrReady
      ? (ru() ? 'Повторить OCR всех томов' : 'Rebuild OCR for all volumes')
      : (ru() ? 'OCR всех томов' : 'OCR all volumes');
    menu.innerHTML = `
      ${select}<button data-manga-context-action="read-series">${ru() ? 'Читать' : 'Read'}</button>
      ${openAniList}${score}${link}
      <button data-manga-context-action="ocr-series">${ocr}</button>
      <button data-manga-context-action="reset-progress-series">${ru() ? 'Сбросить прогресс серии' : 'Reset series progress'}</button>
      <button class="danger-action" data-manga-context-action="remove-series">${ru() ? 'Удалить из Pudge' : 'Remove from Pudge'}</button>`;
    positionMangaContextMenu(menu, x, y);
  }

  async function pollLibraryBookOcr(bookId) {
    try {
      const status = await API().manga_ocr_book_status(Number(bookId));
      if (status.running) {
        const timer = setTimeout(() => void pollLibraryBookOcr(Number(bookId)), 900);
        libraryOcrPollers.set(Number(bookId), timer);
        return;
      }
      libraryOcrPollers.delete(Number(bookId));
      await renderLibrary();
      if (status.complete) window.toast?.(ru() ? 'OCR тома готов' : 'Volume OCR ready');
      else if (status.errors?.length) window.toast?.(status.errors.join(' • '));
    } catch (error) {
      libraryOcrPollers.delete(Number(bookId));
      window.toast?.(error?.message || String(error));
    }
  }

  async function startLibraryBookOcr(book, refresh = false) {
    const bookId = Number(book?.id);
    if (!bookId || libraryOcrPollers.has(bookId)) return;
    try {
      await API().start_manga_ocr_book(bookId, Boolean(refresh));
      window.toast?.(ru() ? 'OCR тома запущен' : 'Volume OCR started');
      libraryOcrPollers.set(bookId, null);
      await pollLibraryBookOcr(bookId);
    } catch (error) {
      libraryOcrPollers.delete(bookId);
      window.toast?.(error?.message || String(error));
    }
  }

  async function startLibrarySeriesOcr(books, rebuild = false) {
    const queue = (books || []).filter(book => Number(book?.id) && !libraryOcrPollers.has(Number(book.id)));
    if (!queue.length) return;
    window.toast?.(ru() ? `OCR томов поставлен в очередь: ${queue.length}` : `Volume OCR queued: ${queue.length}`);
    for (const book of queue) {
      const bookId = Number(book.id);
      try {
        await API().start_manga_ocr_book(bookId, Boolean(rebuild));
        libraryOcrPollers.set(bookId, null);
        void pollLibraryBookOcr(bookId);
      } catch (error) {
        libraryOcrPollers.delete(bookId);
        window.toast?.(error?.message || String(error));
      }
    }
  }

  function isDoublePage(page) {
    if (page?.spread === true) return true;
    const match = String(page?.name || '').match(/(?:^|[^0-9])(\d{1,4})\s*[-–—]\s*(\d{1,4})(?:[^0-9]|$)/);
    return Boolean(match && Number(match[2]) === Number(match[1]) + 1);
  }

  function renderPagePicker() {
    const picker = $('mangaV2PagePicker');
    if (!picker) return;
    const current = Math.max(0, Math.min(currentPage, Math.max(0, currentPageCount - 1)));
    picker.innerHTML = Array.from({length: Math.max(0, currentPageCount)}, (_, index) =>
      `<button type="button" role="option" aria-selected="${index === current ? 'true' : 'false'}" class="${index === current ? 'active' : ''}" data-manga-v2-page-option="${index + 1}">${index + 1}</button>`
    ).join('');
  }

  function updatePageLabel() {
    const label = $('mangaV2PageLabel');
    if (!label) return;
    label.textContent = `${Math.min(currentPageCount, currentPage + 1)} / ${currentPageCount}`;
    if (!$('mangaV2PagePicker')?.hidden) renderPagePicker();
  }

  function closePagePicker() {
    const picker = $('mangaV2PagePicker');
    if (picker) picker.hidden = true;
    $('mangaV2PageLabel')?.setAttribute('aria-expanded', 'false');
  }

  function togglePagePicker() {
    const picker = $('mangaV2PagePicker');
    if (!picker) return;
    const opening = picker.hidden;
    if (!opening) { closePagePicker(); return; }
    renderPagePicker();
    picker.hidden = false;
    $('mangaV2PageLabel')?.setAttribute('aria-expanded', 'true');
    requestAnimationFrame(() => picker.querySelector('.active')?.scrollIntoView({block: 'center'}));
  }

  async function goToPage(pageNumber) {
    if (!currentBook || currentPageCount <= 0) return false;
    const requested = Math.trunc(Number(pageNumber));
    if (!Number.isFinite(requested) || requested < 1 || requested > currentPageCount) return false;
    clearMangaTransientOverlays();
    currentPage = requested - 1;
    await showCurrent();
    await setResumePage(currentPage);
    updatePageLabel();
    closePagePicker();
    return true;
  }

  async function renderPaged() {
    if (!currentBook) return;
    clearMangaTransientOverlays();
    const generation = ++pageRenderGeneration;
    const bookId = Number(currentBook.id);
    const pages = $('mangaV2Pages');
    currentPage = Math.max(0, Math.min(currentPage, Math.max(0, currentPageCount - 1)));
    const firstPage = await getPage(currentPage);
    const loaded = [firstPage];
    if (settings.mode === 'double' && !isDoublePage(firstPage) && currentPage + 1 < currentPageCount) {
      const secondPage = await getPage(currentPage + 1);
      if (!isDoublePage(secondPage)) loaded.push(secondPage);
    }
    pagedVisibleCount = Math.max(1, loaded.length);
    if (generation !== pageRenderGeneration || !currentBook || Number(currentBook.id) !== bookId) return;
    pages.innerHTML = loaded.map(page => `
      <figure class="manga-v2-page-frame${isDoublePage(page) ? ' spread' : ''}" data-spread="${isDoublePage(page) ? '1' : '0'}" data-page-index="${Number(page.page_index)}">
        <img src="${page.data_uri}" alt="${esc(page.name || '')}">
        <div class="manga-v2-text-layer"></div>
      </figure>`).join('');
    updatePageLabel();
    if (settings.mode === 'double' && loaded.length > 1) {
      $('mangaV2PageLabel').textContent = `${loaded[0].page_index + 1}–${loaded[loaded.length - 1].page_index + 1} / ${currentPageCount}`;
    }
    for (const page of loaded) void loadTextRegions(Number(page.page_index), {
      cachedOnly: true,
      parse: true,
    });
    await persistVisiblePage();
    const preload = [];
    for (const offset of [-step, step]) {
      const n = currentPage + offset;
      if (n >= 0 && n < currentPageCount) preload.push(getPage(n).catch(() => null));
    }
    Promise.all(preload).finally(() => void persistVisiblePage());
  }

  async function renderVertical() {
    if (!currentBook) return;
    const generation = ++pageRenderGeneration;
    const bookId = Number(currentBook.id);
    const pages = $('mangaV2Pages');
    if (verticalObserver) verticalObserver.disconnect();
    pages.innerHTML = Array.from({length: currentPageCount}, (_, i) => `
      <figure class="manga-v2-page-frame manga-v2-lazy" data-page-index="${i}">
        <div class="manga-v2-page-placeholder">${i + 1}</div>
      </figure>`).join('');

    verticalObserver = new IntersectionObserver(entries => {
      let best = null;
      for (const entry of entries) {
        const frame = entry.target;
        const index = Number(frame.dataset.pageIndex);
        if (entry.isIntersecting && !frame.dataset.loaded) {
          frame.dataset.loaded = 'loading';
          void getPage(index).then(page => {
            if (generation !== pageRenderGeneration || !currentBook || Number(currentBook.id) !== bookId) return;
            frame.innerHTML = `<img src="${page.data_uri}" alt="${esc(page.name || '')}"><div class="manga-v2-text-layer"></div>`;
            frame.dataset.loaded = '1';
            void loadTextRegions(index, {cachedOnly:true, parse:true});
          }).catch(() => { frame.dataset.loaded = ''; });
        }
        if (entry.isIntersecting && (!best || entry.intersectionRatio > best.ratio)) {
          best = {index, ratio: entry.intersectionRatio};
        }
      }
      if (best) {
        if (best.index !== currentPage) clearMangaTransientOverlays();
        if (best.index > currentPage) void markReadThrough(best.index - 1);
        currentPage = best.index;
        updatePageLabel();
        for (const frame of pages.querySelectorAll('.manga-v2-page-frame[data-loaded="1"]')) {
          const index = Number(frame.dataset.pageIndex);
          if (Math.abs(index - currentPage) <= 3) continue;
          frame.innerHTML = `<div class="manga-v2-page-placeholder">${index + 1}</div>`;
          delete frame.dataset.loaded;
          pageCache.delete(`${bookId}:${index}`);
        }
        clearTimeout(verticalPersistTimer);
        verticalPersistTimer = setTimeout(() => void persistVisiblePage(), 250);
      }
    }, {
      root: $('mangaV2Viewport'),
      rootMargin: '120% 0px 120% 0px',
      threshold: [0.05, 0.25, 0.5, 0.75]
    });
    for (const frame of pages.querySelectorAll('.manga-v2-page-frame')) verticalObserver.observe(frame);
    requestAnimationFrame(() => {
      const target = pages.querySelector(`[data-page-index="${currentPage}"]`);
      target?.scrollIntoView({block: 'center'});
    });
  }

  async function showCurrent() {
    applyReaderSettings();
    if (settings.mode === 'vertical') await renderVertical();
    else await renderPaged();
  }

  async function openBook(bookId) {
    clearMangaTransientOverlays();
    state = await API().manga_state();
    currentBook = (state.books || []).find(book => Number(book.id) === Number(bookId));
    if (!currentBook) return;
    currentPage = Number(currentBook.position || 0);
    currentPageCount = Number(currentBook.page_count || 0);
    pageCache = new Map();
    textRegionCache = new Map();
    textRegionResultCache = new Map();
    textParseCache = new Map();
    textParseInflight = new Map();
    mangaDebugEvents = [];
    mangaDebugSelectionKey = '';
    mangaLastSelection = null;
    mangaPointerSelection = null;
    textGeneration += 1;
    pageRenderGeneration += 1;
    const reader = buildReader();
    currentBookOcrStatus = null;
    $('mangaV2Title').textContent = `${seriesTitle(currentBook)} · ${volumeLabel(currentBook, 0)}`;
    const ocrButton = document.querySelector('[data-manga-v2-action="ocr-book"]');
    if (ocrButton) {
      // Opening a book is not an OCR job. Keep the action usable until the
      // backend proves that this exact volume has a live worker.
      ocrButton.disabled = false;
      ocrButton.classList.remove('busy');
      ocrButton.setAttribute('aria-disabled', 'false');
    }
    reader.classList.add('open');
    document.body.classList.add('manga-v2-reading');
    // Start status reconciliation immediately. Page rendering must never be a
    // prerequisite for re-enabling the OCR action.
    const ocrStatusPromise = (async () => {
      try {
        const status = await API().manga_ocr_book_status(Number(bookId));
        if (currentBook && Number(currentBook.id) === Number(bookId)) syncMangaOcrUi(status);
      } catch (_) {
        if (ocrButton) {
          ocrButton.disabled = false;
          ocrButton.classList.remove('busy');
          ocrButton.setAttribute('aria-disabled', 'false');
        }
      }
    })();
    await showCurrent();
    void ocrStatusPromise;
    void ensureCurrentBookPrepared();
  }

  function closeReader() {
    textGeneration += 1;
    pageRenderGeneration += 1;
    stopPreparationPoll();
    closePagePicker();
    if (toolbarPeekTimer) clearTimeout(toolbarPeekTimer);
    if (verticalObserver) verticalObserver.disconnect();
    verticalObserver = null;
    window.PudgeReadingTools?.closeAll?.();
    $('mangaV2Pages')?.replaceChildren();
    pageCache = new Map();
    textRegionCache = new Map();
    textRegionResultCache = new Map();
    textParseCache = new Map();
    textParseInflight = new Map();
    mangaDebugEvents = [];
    mangaDebugSelectionKey = '';
    mangaLastSelection = null;
    mangaPointerSelection = null;
    $('mangaReaderV2')?.classList.remove('open', 'toolbar-peek');
    document.body.classList.remove('manga-v2-reading');
    currentBook = null;
    currentBookOcrStatus = null;
    const ocrProgress = $('mangaV2OcrProgress');
    if (ocrProgress) { ocrProgress.textContent = ''; ocrProgress.hidden = true; }
    const ocrButton = document.querySelector('[data-manga-v2-action="ocr-book"]');
    if (ocrButton) { ocrButton.disabled = false; ocrButton.classList.remove('busy'); }
    currentPageCount = 0;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    void renderLibrary();
  }

  async function movePage(delta) {
    if (!currentBook || settings.mode === 'vertical') return;
    clearMangaTransientOverlays();
    const previousPage = currentPage;
    if (Number(delta) > 0) {
      const visibleEnd = settings.mode === 'double'
        ? Math.min(currentPageCount - 1, currentPage + Math.max(1, pagedVisibleCount) - 1)
        : currentPage;
      // Count currently visible physical pages only after a forward turn.
      await markReadThrough(visibleEnd);
    }
    if (settings.mode !== 'double') {
      currentPage = Math.max(0, Math.min(currentPageCount - 1, currentPage + Number(delta)));
    } else if (Number(delta) > 0) {
      currentPage = Math.max(0, Math.min(currentPageCount - 1, currentPage + Math.max(1, pagedVisibleCount)));
    } else if (currentPage > 0) {
      const activePage = await getPage(currentPage);
      if (isDoublePage(activePage)) {
        currentPage -= 1;
      } else {
        const previous = await getPage(currentPage - 1);
        if (isDoublePage(previous) || currentPage === 1) {
          currentPage -= 1;
        } else {
          currentPage = Math.max(0, currentPage - 2);
        }
      }
    }
    mangaDebugRecord('page_change', {previous_page:previousPage, next_page:currentPage, delta:Number(delta)});
    await showCurrent();
  }

  async function toggleFullscreen() {
    if (API()?.toggle_fullscreen) {
      await API().toggle_fullscreen();
      return;
    }
    const reader = $('mangaReaderV2');
    if (!document.fullscreenElement && typeof reader?.requestFullscreen === 'function') {
      try { await reader.requestFullscreen(); return; } catch (_) {}
    } else if (document.fullscreenElement && typeof document.exitFullscreen === 'function') {
      try { await document.exitFullscreen(); return; } catch (_) {}
    }
    reader?.classList.toggle('pseudo-fullscreen');
  }

  function sizePageImage(img) {
    if (!img || !img.naturalWidth || !img.naturalHeight) return;
    const viewport = $('mangaV2Viewport');
    if (!viewport) return;
    const zoom = Math.max(.5, Math.min(2.5, Number(settings.zoom || 100) / 100));
    const naturalWidth = Number(img.naturalWidth);
    const naturalHeight = Number(img.naturalHeight);
    const vertical = settings.mode === 'vertical';
    let availableWidth = Math.max(120, viewport.clientWidth - (vertical ? 36 : 116));
    const availableHeight = Math.max(120, viewport.clientHeight - 24);
    const spreadFrame = img.closest?.('.manga-v2-page-frame')?.dataset?.spread === '1';
    if (settings.mode === 'double' && !spreadFrame) {
      availableWidth = Math.max(120, (viewport.clientWidth - 116 - Number(settings.gap || 0)) / 2);
    } else if (vertical) {
      availableWidth = Math.min(1100, availableWidth);
    }
    let baseScale = 1;
    if (settings.fit === 'width') baseScale = availableWidth / naturalWidth;
    else if (settings.fit === 'height' && vertical) baseScale = Math.min(1, availableWidth / naturalWidth);
    else if (settings.fit === 'height') baseScale = Math.min(availableHeight / naturalHeight, availableWidth / naturalWidth);
    const scale = Math.max(.02, baseScale) * zoom;
    img.style.width = `${Math.max(1, naturalWidth * scale)}px`;
    img.style.height = `${Math.max(1, naturalHeight * scale)}px`;
    img.style.maxWidth = 'none';
    img.style.maxHeight = 'none';
  }

  function applyPageSizing() {
    const pages = $('mangaV2Pages');
    if (!pages) return;
    for (const img of pages.querySelectorAll('.manga-v2-page-frame img')) {
      if (img.complete && img.naturalWidth) sizePageImage(img);
    }
  }

  function captureMangaZoomAnchor(clientX = null, clientY = null, target = null) {
    const viewport = $('mangaV2Viewport');
    if (!viewport) return null;
    const rect = viewport.getBoundingClientRect();
    const x = Number.isFinite(Number(clientX)) ? Number(clientX) : rect.left + rect.width / 2;
    const y = Number.isFinite(Number(clientY)) ? Number(clientY) : rect.top + rect.height / 2;
    let image = target?.closest?.('.manga-v2-page-frame img') || null;
    if (!image && document.elementFromPoint) {
      image = document.elementFromPoint(x, y)?.closest?.('.manga-v2-page-frame img') || null;
    }
    if (image) {
      const imageRect = image.getBoundingClientRect();
      if (imageRect.width > 0 && imageRect.height > 0) {
        return {
          kind:'image', image, clientX:x, clientY:y,
          u:(x-imageRect.left)/imageRect.width,
          v:(y-imageRect.top)/imageRect.height,
        };
      }
    }
    return {
      kind:'viewport', clientX:x, clientY:y,
      localX:x-rect.left, localY:y-rect.top,
      contentX:viewport.scrollLeft+(x-rect.left),
      contentY:viewport.scrollTop+(y-rect.top),
      oldScrollWidth:Math.max(1,viewport.scrollWidth),
      oldScrollHeight:Math.max(1,viewport.scrollHeight),
    };
  }

  function restoreMangaZoomAnchor(anchor) {
    const viewport = $('mangaV2Viewport');
    if (!viewport || !anchor) return;
    if (anchor.kind === 'image' && anchor.image?.isConnected) {
      const rect = anchor.image.getBoundingClientRect();
      const nextX = rect.left + Number(anchor.u || 0) * rect.width;
      const nextY = rect.top + Number(anchor.v || 0) * rect.height;
      viewport.scrollLeft += nextX - Number(anchor.clientX || 0);
      viewport.scrollTop += nextY - Number(anchor.clientY || 0);
      return;
    }
    const ratioX = Math.max(1,viewport.scrollWidth) / Math.max(1,Number(anchor.oldScrollWidth || 1));
    const ratioY = Math.max(1,viewport.scrollHeight) / Math.max(1,Number(anchor.oldScrollHeight || 1));
    viewport.scrollLeft = Number(anchor.contentX || 0) * ratioX - Number(anchor.localX || 0);
    viewport.scrollTop = Number(anchor.contentY || 0) * ratioY - Number(anchor.localY || 0);
  }

  function setZoom(value, {persist = true, anchor = null} = {}) {
    const captured = captureMangaZoomAnchor(anchor?.clientX, anchor?.clientY, anchor?.target);
    settings.zoom = Math.max(50, Math.min(250, Math.round(Number(value || 100) / 5) * 5));
    if (persist) saveSettings();
    applyPageSizing();
    syncSettingsControls();
    requestAnimationFrame(() => restoreMangaZoomAnchor(captured));
  }

  function toggleToolbar(force = null) {
    settings.toolbar = force == null ? !settings.toolbar : Boolean(force);
    saveSettings();
    const reader = $('mangaReaderV2');
    reader?.classList.remove('toolbar-peek');
    applyReaderSettings();
  }

  function peekToolbar() {
    if (settings.toolbar) return;
    const reader = $('mangaReaderV2');
    if (!reader?.classList.contains('open')) return;
    reader.classList.add('toolbar-peek');
    if (toolbarPeekTimer) clearTimeout(toolbarPeekTimer);
    toolbarPeekTimer = setTimeout(() => reader.classList.remove('toolbar-peek'), 1800);
  }

  function installZoomGestures(reader) {
    if (!reader || reader.dataset.zoomGestures === '1') return;
    reader.dataset.zoomGestures = '1';
    const viewport = $('mangaV2Viewport');

    viewport?.addEventListener('wheel', event => {
      if (gestureActive || !(event.ctrlKey || event.metaKey || event.altKey)) return;
      event.preventDefault();
      setZoom(Number(settings.zoom || 100) + (event.deltaY < 0 ? 10 : -10), {anchor:event});
    }, {passive:false});

    viewport?.addEventListener('dblclick', event => {
      if (!event.target?.closest?.('.manga-v2-page-frame img')) return;
      event.preventDefault();
      const next = Number(settings.zoom || 100) >= 200 ? 100 : Number(settings.zoom || 100) + 50;
      setZoom(next, {anchor:event});
    }, {passive:false});

    reader.addEventListener('gesturestart', event => {
      gestureActive = true;
      gestureBaseZoom = Number(settings.zoom || 100);
      event.preventDefault();
    }, {passive:false});
    reader.addEventListener('gesturechange', event => {
      event.preventDefault();
      setZoom(gestureBaseZoom * Number(event.scale || 1), {persist:false, anchor:event});
    }, {passive:false});
    reader.addEventListener('gestureend', event => {
      event.preventDefault();
      gestureActive = false;
      saveSettings();
      syncSettingsControls();
    }, {passive:false});

    reader.addEventListener('pointermove', event => {
      if (!settings.toolbar && event.clientY <= 20) peekToolbar();
      scheduleMangaStudyCursor(event);
    });
    reader.addEventListener('pointerleave', () => {
      mangaCursorRegion?.classList?.remove('study-hit');
      mangaCursorRegion = null;
    });
  }

  document.addEventListener('change', event => {
    const control = event.target.closest?.('[data-manga-setting]');
    if (!control) return;
    const key = control.dataset.mangaSetting;
    settings[key] = control.type === 'checkbox'
      ? control.checked
      : (control.type === 'range' ? Number(control.value) : control.value);
    saveSettings();
    applyReaderSettings();
    if (key === 'mode' || key === 'direction') void showCurrent();
    else if (key === 'fit' || key === 'zoom' || key === 'gap') requestAnimationFrame(applyPageSizing);
  });

  document.addEventListener('input', event => {
    const control = event.target.closest?.('[data-manga-setting="zoom"],[data-manga-setting="gap"]');
    if (!control) return;
    settings[control.dataset.mangaSetting] = Number(control.value);
    saveSettings();
    applyReaderSettings();
  });

  document.addEventListener('contextmenu', event => {
    const entry = event.target.closest?.('[data-manga-book]');
    if (entry && !entry.closest?.('#mangaReaderV2')) {
      const book = (state.books || []).find(
        item => Number(item.id) === Number(entry.dataset.mangaBook)
      );
      if (!book) return;
      event.preventDefault();
      event.stopPropagation();
      showMangaContextMenu(book, event.clientX, event.clientY);
      return;
    }
    const seriesNode = event.target.closest?.('[data-manga-series-key]');
    if (!seriesNode || seriesNode.closest?.('#mangaReaderV2')) return;
    const group = mangaSeriesForKey(seriesNode.dataset.mangaSeriesKey);
    if (!group) return;
    event.preventDefault();
    event.stopPropagation();
    showMangaSeriesContextMenu(group, event.clientX, event.clientY);
  }, true);

  function rectSnapshot(rect) {
    if (!rect) return null;
    return {
      left:Number(rect.left || 0), top:Number(rect.top || 0),
      right:Number(rect.right || 0), bottom:Number(rect.bottom || 0),
      width:Number(rect.width || 0), height:Number(rect.height || 0),
    };
  }

  function pointNearRect(x, y, rect, pad = 18) {
    return Boolean(rect) && x >= rect.left - pad && x <= rect.right + pad &&
      y >= rect.top - pad && y <= rect.bottom + pad;
  }

  function currentJapaneseSelection(image = null) {
    const selection = window.getSelection?.();
    const raw = String(selection?.toString?.() || '').trim();
    const text = raw.replace(/\s+/g, '');
    if (!selection || selection.isCollapsed || !selection.rangeCount || !text || text.length > 48 ||
        !/[\u3040-\u30ff\u3400-\u9fff]/.test(text)) return null;
    // Native WebKit Live Text can expose an image selection without useful DOM
    // ancestry. Geometry is therefore the reliable boundary: reject stale/global
    // UI selections and accept only a compact selection actually over the page.
    const rect = selection.getRangeAt(0).getBoundingClientRect?.();
    const snap = rectSnapshot(rect);
    const page = image || document.elementFromPoint?.(snap ? snap.left + snap.width / 2 : 0, snap ? snap.top + snap.height / 2 : 0)?.closest?.('.manga-v2-page-frame')?.querySelector?.('img');
    const imageRect = page?.getBoundingClientRect?.();
    if (!snap || !imageRect || snap.width <= 0 || snap.height <= 0) return null;
    const cx = snap.left + snap.width / 2, cy = snap.top + snap.height / 2;
    if (cx < imageRect.left - 8 || cx > imageRect.right + 8 || cy < imageRect.top - 8 || cy > imageRect.bottom + 8) return null;
    if (snap.width > imageRect.width * .75 || snap.height > imageRect.height * .75) return null;
    return {text:text.slice(0, 48), rect:snap, at:performance.now()};
  }

  document.addEventListener('pointerdown', event => {
    mangaDebugPoint(event, 'down');
    const image = event.target?.closest?.('.manga-v2-page-frame img');
    mangaPointerSelection = image ? currentJapaneseSelection(image) : null;
  }, true);
  document.addEventListener('selectionchange', () => {
    if (!currentBook || !$('mangaReaderV2')?.classList.contains('open')) return;
    const selection = window.getSelection?.();
    document.querySelectorAll('.manga-v2-text-region.bubble-selected').forEach(node => {
      if (!mangaOverlaySelectionInRegion(node)) node.classList.remove('bubble-selected');
    });
    const text = String(selection?.toString?.() || '').trim();
    if (!text) return;
    const anchor = selection?.anchorNode?.parentElement?.closest?.('.manga-v2-text-region');
    const rangeRect = selection?.rangeCount ? rectSnapshot(selection.getRangeAt(0).getBoundingClientRect?.()) : null;
    const selectedCandidate = currentJapaneseSelection();
    if (selectedCandidate) mangaLastSelection = selectedCandidate;
    const key = `${Number(currentPage)}:${anchor?.dataset?.regionIndex || ''}:${text}`;
    if (key === mangaDebugSelectionKey) return;
    mangaDebugSelectionKey = key;
    mangaDebugRecord('selection', {
      text: text.slice(0, 500),
      region_index: anchor ? Number(anchor.dataset.regionIndex) : null,
      anchor_text: String(selection?.anchorNode?.textContent || '').trim().slice(0, 160),
    });
  });
  function tokenForCharacterOffset(tokens, target) {
    if (!tokens?.length) return null;
    const weights = tokens.map(token => Math.max(1, [...String(token.textContent || '').trim()].length));
    const total = weights.reduce((sum, value) => sum + value, 0);
    let cursor = Math.max(0, Math.min(Math.max(0, total - .001), Number(target || 0)));
    for (let index = 0; index < tokens.length; index++) {
      if (cursor < weights[index]) return tokens[index];
      cursor -= weights[index];
    }
    return tokens[tokens.length - 1] || null;
  }

  function rawRegionRect(frame, regionNode) {
    const image = frame?.querySelector?.('img');
    const imageRect = image?.getBoundingClientRect?.();
    if (!imageRect || !regionNode) return null;
    const x = Number(regionNode.dataset.rawX || 0), y = Number(regionNode.dataset.rawY || 0);
    const width = Number(regionNode.dataset.rawWidth || 0), height = Number(regionNode.dataset.rawHeight || 0);
    return {
      left:imageRect.left + x * imageRect.width,
      right:imageRect.left + (x + width) * imageRect.width,
      top:imageRect.top + (1 - y - height) * imageRect.height,
      bottom:imageRect.top + (1 - y) * imageRect.height,
      width:width * imageRect.width,
      height:height * imageRect.height,
      imageRect,
    };
  }

  function pointDistanceToRect(x, y, rect) {
    if (!rect) return Infinity;
    const dx = x < rect.left ? rect.left - x : (x > rect.right ? x - rect.right : 0);
    const dy = y < rect.top ? rect.top - y : (y > rect.bottom ? y - rect.bottom : 0);
    return Math.hypot(dx, dy);
  }

  function mangaRegionAtPoint(frame, clientX, clientY) {
    if (!frame) return null;
    const candidates = [...frame.querySelectorAll('.manga-v2-text-region')]
      .map(node => {
        const rawRect = rawRegionRect(frame, node);
        const distance = pointDistanceToRect(clientX, clientY, rawRect);
        const maxSnap = Math.max(18, Math.min(42, Math.max(rawRect?.width || 0, rawRect?.height || 0) * .18));
        if (!Number.isFinite(distance) || distance > maxSnap) return null;
        return {node, distance, area:Math.max(1, (rawRect?.width || 0) * (rawRect?.height || 0))};
      })
      .filter(Boolean)
      .sort((a, b) => a.distance - b.distance || a.area - b.area);
    return candidates[0]?.node || null;
  }

  // pudge-v0.7.27-manga-exact-hitboxes-v2
  const MANGA_TOKEN_HIT_SLOP_PX = 8;

  function mangaTokenWeight(token) {
    return Math.max(1, [...String(token?.textContent || '').trim()].length);
  }

  function mangaTokenIntervals(tokens) {
    let cursor = 0;
    return tokens.map((token, tokenIndex) => {
      const weight = mangaTokenWeight(token);
      const row = {token, tokenIndex, start:cursor, end:cursor + weight, weight};
      cursor += weight;
      return row;
    });
  }

  function pushMangaTokenBox(boxes, interval, geometry, source) {
    const x = Math.max(0, Math.min(1, Number(geometry.x || 0)));
    const y = Math.max(0, Math.min(1, Number(geometry.y || 0)));
    const width = Math.max(0, Math.min(1 - x, Number(geometry.width || 0)));
    const height = Math.max(0, Math.min(1 - y, Number(geometry.height || 0)));
    if (width <= 0.0001 || height <= 0.0001) return;
    boxes.push({
      token:interval.token,
      tokenIndex:Number(interval.tokenIndex),
      text:String(interval.token?.textContent || '').trim(),
      x, y, width, height,
      source:String(source || 'fallback'),
    });
  }

  // pudge-v0.7.27-manga-exact-surface-map-v1
  function mangaHitSurface(value) {
    return String(value || '').normalize('NFKC').replace(/\s+/g, '');
  }

  function mangaFindCharacterSequence(haystack, needle, start = 0) {
    if (!needle.length || needle.length > haystack.length) return -1;
    for (let index = Math.max(0, Number(start || 0)); index <= haystack.length - needle.length; index++) {
      let matches = true;
      for (let offset = 0; offset < needle.length; offset++) {
        if (haystack[index + offset] !== needle[offset]) { matches = false; break; }
      }
      if (matches) return index;
    }
    return -1;
  }

  // pudge-manga-recovery-hitbox-contract-v2
  const MANGA_STUDY_TRAILING_DECORATION = new Set([
    '！','!','？','?','。','…','‥','〜','～','ー','―','—','−','・','、',',','．','.'
  ]);

  function mangaStudySurfaceCandidates(value) {
    const exact = [...mangaHitSurface(value)];
    if (!exact.length) return [];
    const candidates = [exact];
    let end = exact.length;
    while (end > 1 && MANGA_STUDY_TRAILING_DECORATION.has(exact[end - 1])) end -= 1;
    if (end < exact.length) candidates.push(exact.slice(0, end));
    return candidates;
  }

  function mangaMapTokenSurfaces(stream, surfaces) {
    let cursor = 0;
    return (surfaces || []).map(surfaceValue => {
      const candidates = mangaStudySurfaceCandidates(surfaceValue);
      if (!candidates.length) return null;
      for (const surface of candidates) {
        const found = mangaFindCharacterSequence(stream, surface, cursor);
        if (found < 0) continue;
        cursor = found + surface.length;
        return {
          start:found,
          end:cursor,
          surface:surface.join(''),
          strippedTrailingDecoration:surface.length < candidates[0].length,
        };
      }
      return null;
    });
  }

  function mangaExtendEmphaticSmallTsu(stream, mappings) {
    const punctuation = new Set(['！','!','？','?','。','…','‥','〜','～']);
    return (mappings || []).map((mapping, index) => {
      if (!mapping) return mapping;
      const next = (mappings || []).slice(index + 1).find(Boolean);
      const limit = next ? Number(next.start) : stream.length;
      const end = Number(mapping.end || 0);
      if (end >= limit || !['っ','ッ'].includes(stream[end])) return mapping;
      const tail = stream.slice(end + 1, limit);
      if (tail.every(character => punctuation.has(character))) {
        return {...mapping, end:end + 1, visualSuffix:stream[end]};
      }
      return mapping;
    });
  }

  function mangaGeometryStatus(region, segments) {
    const explicit = String(region?.geometry_status || '').toLowerCase();
    if (['observed','approximate','synthetic','unknown','unavailable'].includes(explicit)) return explicit;
    if (!segments?.length) return 'unavailable';
    if (segments.some(segment => !mangaHitSurface(segment?.text))) return 'unknown';
    const synthetic = new Set(['ink-grid-v1','ink-columns-v2','dark-columns-v1','vertical-grid-fallback','horizontal-region-fallback']);
    if (segments.some(segment => synthetic.has(String(segment?.source || '')))) return 'synthetic';
    if (segments.some(segment => String(segment?.source || '').startsWith('vision-'))) return 'observed';
    return 'approximate';
  }

  function mangaSegmentCharacters(segments, vertical) {
    const characters = [];
    for (const segment of segments) {
      const surface = [...mangaHitSurface(segment?.text)];
      if (!surface.length) continue;
      const sx = Number(segment.x || 0), sy = Number(segment.y || 0);
      const sw = Number(segment.width || 0), sh = Number(segment.height || 0);
      if (!(sw > 0 && sh > 0)) continue;
      surface.forEach((character, characterIndex) => {
        let geometry;
        if (surface.length === 1) {
          geometry = {x:sx, y:sy, width:sw, height:sh};
        } else if (vertical) {
          const unit = sh / surface.length;
          geometry = {
            x:sx,
            y:sy + sh - unit * (characterIndex + 1),
            width:sw,
            height:unit,
          };
        } else {
          const unit = sw / surface.length;
          geometry = {x:sx + unit * characterIndex, y:sy, width:unit, height:sh};
        }
        characters.push({
          character,
          geometry,
          source:String(segment.source || ''),
        });
      });
    }
    return characters;
  }

  function mangaGeometryUnion(items) {
    if (!items.length) return null;
    const x1 = Math.min(...items.map(item => Number(item.geometry.x || 0)));
    const y1 = Math.min(...items.map(item => Number(item.geometry.y || 0)));
    const x2 = Math.max(...items.map(item => Number(item.geometry.x || 0) + Number(item.geometry.width || 0)));
    const y2 = Math.max(...items.map(item => Number(item.geometry.y || 0) + Number(item.geometry.height || 0)));
    return {x:x1, y:y1, width:Math.max(0, x2-x1), height:Math.max(0, y2-y1)};
  }

  function mangaTokenCharacterRuns(items, vertical) {
    const runs = [];
    for (const item of items) {
      const previous = runs[runs.length - 1];
      if (!previous) { runs.push([item]); continue; }
      const last = previous[previous.length - 1];
      const a = last.geometry, b = item.geometry;
      const sameTrack = vertical
        ? Math.abs((a.x + a.width/2) - (b.x + b.width/2)) <= Math.max(a.width, b.width) * .55
        : Math.abs((a.y + a.height/2) - (b.y + b.height/2)) <= Math.max(a.height, b.height) * .55;
      if (sameTrack) previous.push(item);
      else runs.push([item]);
    }
    return runs;
  }

  // pudge-v0.7.27-manga-dark-shape-hitboxes-v2
  function mangaPageImageForHitboxes(regionNode) {
    return regionNode?.closest?.('.manga-v2-page-frame')?.querySelector?.('img') || null;
  }

  function mangaHitboxPixelContext(image) {
    if (!image?.naturalWidth || !image?.naturalHeight) return null;
    const cached = image.__pudgeMangaHitboxPixelContext;
    if (cached && cached.width === image.naturalWidth && cached.height === image.naturalHeight) {
      return cached.ctx;
    }
    const canvas = document.createElement('canvas');
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const ctx = canvas.getContext('2d', {willReadFrequently:true});
    if (!ctx) return null;
    try {
      ctx.drawImage(image, 0, 0, image.naturalWidth, image.naturalHeight);
    } catch (_) {
      return null;
    }
    image.__pudgeMangaHitboxPixelContext = {
      ctx,
      width:image.naturalWidth,
      height:image.naturalHeight,
    };
    return ctx;
  }

  function mangaDarkSupportForHitbox(image, box) {
    const ctx = mangaHitboxPixelContext(image);
    if (!ctx || !box) return null;

    const naturalWidth = Number(image.naturalWidth || 0);
    const naturalHeight = Number(image.naturalHeight || 0);
    if (!(naturalWidth > 0 && naturalHeight > 0)) return null;

    const x = Number(box.x || 0);
    const y = Number(box.y || 0);
    const width = Number(box.width || 0);
    const height = Number(box.height || 0);

    // White glyphs in narration boxes must be surrounded by the black panel.
    // False geometry in an L-shaped white cut-out fails this support check.
    const padX = Math.max(2 / naturalWidth, width * .35);
    const padY = Math.max(2 / naturalHeight, height * .25);
    const left = Math.max(0, Math.floor((x - padX) * naturalWidth));
    const right = Math.min(naturalWidth, Math.ceil((x + width + padX) * naturalWidth));
    const topNorm = Math.min(1, y + height + padY);
    const bottomNorm = Math.max(0, y - padY);
    const top = Math.max(0, Math.floor((1 - topNorm) * naturalHeight));
    const bottom = Math.min(naturalHeight, Math.ceil((1 - bottomNorm) * naturalHeight));
    const sampleWidth = Math.max(1, right - left);
    const sampleHeight = Math.max(1, bottom - top);

    let pixels;
    try {
      pixels = ctx.getImageData(left, top, sampleWidth, sampleHeight).data;
    } catch (_) {
      return null;
    }
    let dark = 0;
    let light = 0;
    let total = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      const luma = (pixels[index] * 299 + pixels[index + 1] * 587 + pixels[index + 2] * 114) / 1000;
      if (luma <= 72) dark += 1;
      if (luma >= 180) light += 1;
      total += 1;
    }
    if (!total) return null;
    return {dark:dark / total, light:light / total};
  }

  function mangaFinalizeTokenHitboxes(regionNode, region, boxes) {
    if (!Array.isArray(boxes) || !boxes.length) return boxes || [];
    if (String(region?.source || '') !== 'dark-block-proposal') return boxes;
    const image = mangaPageImageForHitboxes(regionNode);
    if (!image) return boxes;

    return boxes.filter(box => {
      const support = mangaDarkSupportForHitbox(image, box);
      // Canvas failure preserves behaviour. A real sampled target must sit on
      // the black panel and include at least a trace of the white glyph.
      return support == null || (support.dark >= .45 && support.light >= .01);
    });
  }

  function mangaAddChapterPrefixFallbackHitbox(boxes, region, segmentCharacters) {
    const surface = mangaHitSurface(region?.text || region?.raw_text || '');
    if (!/^第[0-9０-９一二三四五六七八九十百千]+話/.test(surface)) return;
    if (boxes.some(box => mangaHitSurface(box?.text || '') === '第')) return;
    const character = segmentCharacters.find(item => item?.character === '第');
    if (!character?.geometry) return;
    const geometry = character.geometry;
    const x = Math.max(0, Math.min(1, Number(geometry.x || 0)));
    const y = Math.max(0, Math.min(1, Number(geometry.y || 0)));
    const width = Math.max(0, Math.min(1-x, Number(geometry.width || 0)));
    const height = Math.max(0, Math.min(1-y, Number(geometry.height || 0)));
    if (!(width > .0001 && height > .0001)) return;
    boxes.unshift({
      token:null,
      tokenIndex:-1,
      text:'第',
      virtualText:'第',
      x, y, width, height,
      source:'chapter-prefix-fallback-v1',
    });
  }

  function mangaTokenHitboxes(regionNode, region) {
    if (!regionNode || !region) return [];
    const tokens = [...regionNode.querySelectorAll('[data-pudge-study-token]')];
    if (!tokens.length) return [];

    const rawX = Number(regionNode.dataset.rawX || 0);
    const rawY = Number(regionNode.dataset.rawY || 0);
    const rawWidth = Number(regionNode.dataset.rawWidth || 0);
    const rawHeight = Number(regionNode.dataset.rawHeight || 0);
    const vertical = String(regionNode.dataset.effectiveOrientation || '') === 'vertical';
    const segments = Array.isArray(region.segments)
      ? region.segments.filter(item => item && Number(item.width) > 0 && Number(item.height) > 0)
      : [];
    const boxes = [];
    const regionSource = String(region.source || '');
    const generatedVerticalSource = ['expanded-vision-rectangle', 'expanded-vertical-seed', 'dark-block-proposal']
      .includes(regionSource);

    // return null; // do not invent token geometry
    if (vertical && generatedVerticalSource && segments.length <= 1) return [];

    if (segments.length > 1) {
      const segmentCharacters = mangaSegmentCharacters(segments, vertical);
      if (!segmentCharacters.length) return [];
      const stream = segmentCharacters.map(item => item.character);
      const mappings = mangaExtendEmphaticSmallTsu(
        stream,
        mangaMapTokenSurfaces(stream, tokens.map(token => token?.textContent)),
      );
      tokens.forEach((token, tokenIndex) => {
        const mapping = mappings[tokenIndex];
        if (!mapping) return;
        const matched = segmentCharacters.slice(mapping.start, mapping.end);
        const interval = {token, tokenIndex};
        for (const run of mangaTokenCharacterRuns(matched, vertical)) {
          const geometry = mangaGeometryUnion(run);
          if (!geometry) continue;
          pushMangaTokenBox(
            boxes,
            interval,
            geometry,
            run.map(item => item.source).find(Boolean) || region.geometry_source || 'segment-surface-map',
          );
        }
      });
      mangaAddChapterPrefixFallbackHitbox(boxes, region, segmentCharacters);
      if (boxes.length || generatedVerticalSource) {
        const filteredBoxes = mangaFinalizeTokenHitboxes(regionNode, region, boxes);
        boxes.splice(0, boxes.length, ...filteredBoxes);
      }
      if (boxes.length || generatedVerticalSource) return boxes;
    }

    if (segments.length) return [];

    const regionSurface = mangaHitSurface(region?.text || region?.raw_text || '');
    const onlyTokenSurface = tokens.length === 1 ? mangaHitSurface(tokens[0]?.textContent) : '';
    if (tokens.length === 1 && regionSurface && regionSurface === onlyTokenSurface && rawWidth > 0 && rawHeight > 0) {
      pushMangaTokenBox(boxes, {token:tokens[0], tokenIndex:0}, {
        x:rawX, y:rawY, width:rawWidth, height:rawHeight,
      }, 'region-single-token');
      return boxes;
    }
    return [];
  }

  function mangaTokenBoxClientRect(box, imageRect, slop = 0) {
    if (!box || !imageRect) return null;
    const left = imageRect.left + Number(box.x || 0) * imageRect.width;
    const right = imageRect.left + (Number(box.x || 0) + Number(box.width || 0)) * imageRect.width;
    const top = imageRect.top + (1 - Number(box.y || 0) - Number(box.height || 0)) * imageRect.height;
    const bottom = imageRect.top + (1 - Number(box.y || 0)) * imageRect.height;
    return {left:left-slop, right:right+slop, top:top-slop, bottom:bottom+slop};
  }

  function mangaTokenHitAtPoint(frame, regionNode, region, clientX, clientY) {
    if (!frame || !regionNode || !region) return null;
    const imageRect = frame.querySelector('img')?.getBoundingClientRect?.();
    if (!imageRect) return null;
    const boxes = mangaTokenHitboxes(regionNode, region);
    if (!boxes.length) return null;
    const ranked = boxes.map(box => {
      const core = mangaTokenBoxClientRect(box, imageRect, 0);
      const insideCore = Boolean(core) && clientX >= core.left && clientX <= core.right && clientY >= core.top && clientY <= core.bottom;
      const expanded = mangaTokenBoxClientRect(box, imageRect, MANGA_TOKEN_HIT_SLOP_PX);
      const insideSlop = Boolean(expanded) && clientX >= expanded.left && clientX <= expanded.right && clientY >= expanded.top && clientY <= expanded.bottom;
      const dx = core ? (clientX < core.left ? core.left-clientX : (clientX > core.right ? clientX-core.right : 0)) : Infinity;
      const dy = core ? (clientY < core.top ? core.top-clientY : (clientY > core.bottom ? clientY-core.bottom : 0)) : Infinity;
      const area = core ? Math.max(1, (core.right-core.left) * (core.bottom-core.top)) : Infinity;
      return {box, insideCore, insideSlop, distance:Math.hypot(dx,dy), area};
    }).filter(item => item.insideCore || item.insideSlop)
      .sort((a,b) => Number(b.insideCore)-Number(a.insideCore) || a.distance-b.distance || a.area-b.area);
    return ranked[0]?.box || null;
  }

  function mangaTokenAtPoint(frame, regionNode, region, clientX, clientY) {
    return mangaTokenHitAtPoint(frame, regionNode, region, clientX, clientY)?.token || null;
  }

  async function dispatchMangaVirtualStudyHit(hit, frame) {
    const text = String(hit?.virtualText || '').trim();
    if (!text) return false;
    const imageRect = frame?.querySelector?.('img')?.getBoundingClientRect?.();
    const core = mangaTokenBoxClientRect(hit, imageRect, 0);
    if (!core) return false;
    const openText = window.PudgeReadingTools?.study?.openText;
    if (typeof openText !== 'function') return false;
    const rect = typeof DOMRect === 'function'
      ? new DOMRect(core.left, core.top, core.right-core.left, core.bottom-core.top)
      : {left:core.left, top:core.top, right:core.right, bottom:core.bottom, width:core.right-core.left, height:core.bottom-core.top};
    try {
      return Boolean(await openText(text, rect, {backend:currentStudyBackend()}));
    } catch (_) {
      return false;
    }
  }

  async function dispatchMangaStudyClick(token, sourceEvent) {
    if (!token) return false;
    const tokenText = String(token.textContent || '').trim();
    const regionIndex = Number(token.closest('.manga-v2-text-region')?.dataset?.regionIndex ?? -1);
    mangaDebugRecord('jiten_hit', {
      token_text: tokenText,
      region_index: regionIndex,
      client_x: Number(sourceEvent.clientX || 0),
      client_y: Number(sourceEvent.clientY || 0),
    });
    const openElement = window.PudgeReadingTools?.study?.openElement;
    if (typeof openElement !== 'function') {
      mangaDebugRecord('jiten_card_failed', {
        reason: 'study-open-element-unavailable',
        token_text: tokenText,
        region_index: regionIndex,
      });
      return false;
    }
    try {
      const opened = await openElement(token);
      mangaDebugRecord(opened ? 'jiten_card_open' : 'jiten_card_failed', {
        reason: opened ? 'direct-open' : 'direct-open-returned-false',
        token_text: tokenText,
        region_index: regionIndex,
        card_open: Boolean(document.getElementById('pudgeStudyCard')?.classList.contains('open')),
      });
      return Boolean(opened);
    } catch (error) {
      mangaDebugRecord('jiten_card_failed', {
        reason: 'direct-open-error',
        error: String(error?.message || error),
        token_text: tokenText,
        region_index: regionIndex,
      });
      return false;
    }
  }

  async function openMangaSelectedText(candidate, event, reason) {
    if (!candidate?.text || !pointNearRect(event.clientX, event.clientY, candidate.rect, 22)) return false;
    const openText = window.PudgeReadingTools?.study?.openText;
    if (typeof openText !== 'function') return false;
    try {
      const opened = await openText(candidate.text, candidate.rect, {backend:currentStudyBackend()});
      mangaDebugRecord(opened ? 'jiten_selection_open' : 'jiten_selection_failed', {
        reason:String(reason || 'selection'),
        selected_text:String(candidate.text || '').slice(0, 160),
        client_x:Number(event.clientX || 0), client_y:Number(event.clientY || 0),
      });
      return Boolean(opened);
    } catch (error) {
      mangaDebugRecord('jiten_selection_failed', {
        reason:'selection-open-error', error:String(error?.message || error),
        selected_text:String(candidate.text || '').slice(0, 160),
      });
      return false;
    }
  }


  function mangaOverlaySelectionInRegion(regionNode) {
    const selection = window.getSelection?.();
    if (!selection || selection.isCollapsed || !selection.rangeCount || !regionNode) return false;
    const anchor = selection.anchorNode?.nodeType === Node.TEXT_NODE ? selection.anchorNode.parentElement : selection.anchorNode;
    const focus = selection.focusNode?.nodeType === Node.TEXT_NODE ? selection.focusNode.parentElement : selection.focusNode;
    return Boolean(anchor?.closest?.('.manga-v2-text-region') === regionNode || focus?.closest?.('.manga-v2-text-region') === regionNode);
  }

  function selectWholeMangaBubble(regionNode) {
    const surface = regionNode?.querySelector?.('.manga-v2-selection-content');
    const selection = window.getSelection?.();
    if (!surface || !selection || typeof document.createRange !== 'function') return false;
    const range = document.createRange();
    range.selectNodeContents(surface);
    selection.removeAllRanges();
    selection.addRange(range);
    regionNode.classList.add('bubble-selected');
    mangaDebugRecord('bubble_selection', {
      region_index:Number(regionNode.dataset.regionIndex || -1),
      text:String(surface.dataset.mangaCopySurface || surface.textContent || '').slice(0, 500),
    });
    return true;
  }

  async function handleMangaRegionStudyClick(event, regionNode) {
    if (!regionNode || !currentBook || mangaOverlaySelectionInRegion(regionNode)) return false;
    const frame = regionNode.closest?.('.manga-v2-page-frame');
    if (!frame) return false;
    const pageIndex = Number(frame.dataset.pageIndex);
    const regionIndex = Number(regionNode.dataset.regionIndex);
    const regions = textRegionCache.get(textKey(currentBook.id, pageIndex)) || [];
    const region = regions[regionIndex];
    if (!region) return false;
    await parseRegionText(pageIndex, regionIndex, region, textGeneration);
    const tokenHit = mangaTokenHitAtPoint(frame, regionNode, region, event.clientX, event.clientY);
    if (tokenHit?.token) return await dispatchMangaStudyClick(tokenHit.token, event);
    if (tokenHit?.virtualText) return await dispatchMangaVirtualStudyHit(tokenHit, frame);
    return false;
  }

  let mangaCursorRegion = null;
  let mangaCursorFrame = 0;
  function scheduleMangaStudyCursor(event) {
    if (mangaCursorFrame) cancelAnimationFrame(mangaCursorFrame);
    const clientX = Number(event.clientX || 0), clientY = Number(event.clientY || 0);
    const frame = event.target?.closest?.('.manga-v2-page-frame');
    mangaCursorFrame = requestAnimationFrame(() => {
      mangaCursorFrame = 0;
      mangaCursorRegion?.classList?.remove('study-hit');
      mangaCursorRegion = null;
      if (!frame || !currentBook) return;
      const regionNode = mangaRegionAtPoint(frame, clientX, clientY);
      if (!regionNode) return;
      const pageIndex = Number(frame.dataset.pageIndex);
      const regionIndex = Number(regionNode.dataset.regionIndex);
      const region = (textRegionCache.get(textKey(currentBook.id, pageIndex)) || [])[regionIndex];
      if (!region) return;
      const hit = mangaTokenHitAtPoint(frame, regionNode, region, clientX, clientY);
      if (hit?.token || hit?.virtualText) {
        regionNode.classList.add('study-hit');
        mangaCursorRegion = regionNode;
      }
    });
  }

  async function handleMangaImageStudyClick(event) {
    const image = event.target?.closest?.('.manga-v2-page-frame img');
    if (!image || !$('mangaReaderV2')?.classList.contains('open')) return false;
    const pointerSelection = mangaPointerSelection;
    mangaPointerSelection = null;
    if (await openMangaSelectedText(pointerSelection, event, 'selection-at-pointerdown')) return true;
    const liveSelection = currentJapaneseSelection(image);
    if (await openMangaSelectedText(liveSelection, event, 'live-selection')) return true;
    if (mangaLastSelection && performance.now() - Number(mangaLastSelection.at || 0) < 2500 &&
        await openMangaSelectedText(mangaLastSelection, event, 'recent-selection')) return true;
    const frame = image.closest('.manga-v2-page-frame');
    const regionNode = mangaRegionAtPoint(frame, event.clientX, event.clientY);
    if (!regionNode || !currentBook) {
      mangaDebugRecord('jiten_miss', {
        reason: 'no-region-at-point',
        client_x: Number(event.clientX || 0),
        client_y: Number(event.clientY || 0),
      });
      return false;
    }
    const pageIndex = Number(frame.dataset.pageIndex);
    const regionIndex = Number(regionNode.dataset.regionIndex);
    const regions = textRegionCache.get(textKey(currentBook.id, pageIndex)) || [];
    const region = regions[regionIndex];
    if (!region) return false;
    await parseRegionText(pageIndex, regionIndex, region, textGeneration);
    const tokenHit = mangaTokenHitAtPoint(frame, regionNode, region, event.clientX, event.clientY);
    if (tokenHit?.token) return await dispatchMangaStudyClick(tokenHit.token, event);
    if (tokenHit?.virtualText) return await dispatchMangaVirtualStudyHit(tokenHit, frame);
    mangaDebugRecord('jiten_miss', {
      reason: 'region-without-token-hit',
      region_index: regionIndex,
      region_text: String(region.text || '').slice(0, 160),
      parsed_token_count: regionNode.querySelectorAll('[data-pudge-study-token]').length,
      effective_orientation: String(regionNode.dataset.effectiveOrientation || ''),
      client_x: Number(event.clientX || 0),
      client_y: Number(event.clientY || 0),
    });
    return false;
  }

  document.addEventListener('click', async event => {
    mangaDebugPoint(event, 'click');
    const franchiseCard = event.target.closest?.('[data-manga-franchise-ids]');
    const franchiseSeries = event.target.closest?.('[data-manga-series-key]');
    const franchiseBook = event.target.closest?.('[data-manga-book]');
    const franchiseInteractive = event.target.closest?.('button,a,input,select,textarea,[data-action]');
    if (franchiseCard && !franchiseSeries && !franchiseBook && !franchiseCard.closest?.('#mangaReaderV2') && !franchiseInteractive) {
      event.preventDefault();
      event.stopImmediatePropagation();
      toggleFranchiseSelection(franchiseCard);
      return;
    }
    // pudge-v0.7.23-series-primary-click-select-all-v1
    const seriesCard = event.target.closest?.('[data-manga-series-key]');
    const seriesVolume = event.target.closest?.('[data-manga-book]');
    const interactiveSeriesChild = event.target.closest?.('button,a,input,select,textarea,[data-action]');
    if (seriesCard && !seriesVolume && !seriesCard.closest?.('#mangaReaderV2') && !interactiveSeriesChild) {
      const group = mangaSeriesForKey(seriesCard.dataset.mangaSeriesKey);
      if (group) {
        event.preventDefault();
        event.stopImmediatePropagation();
        toggleSeriesSelection(group.books);
        return;
      }
    }
    const libraryEntry = event.target.closest?.('[data-manga-book]');
    if (event.metaKey && libraryEntry && !libraryEntry.closest?.('#mangaReaderV2')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      toggleSelection(Number(libraryEntry.dataset.mangaBook));
      return;
    }
    const contextAction = event.target.closest?.('[data-manga-context-action]');
    if (contextAction) {
      event.preventDefault();
      event.stopPropagation();
      const book = mangaContextBook;
      const series = mangaContextSeries;
      const type = contextAction.dataset.mangaContextAction;
      closeMangaContextMenu();
      if (!book) return;
      if (type === 'select-series' && series) toggleSeriesSelection(series.books);
      else if (type === 'read-series' && series) await openBook(Number(mangaCurrentSeriesBook(series.books)?.id || book.id));
      else if (type === 'select') toggleSelection(Number(book.id));
      else if (type === 'read') await openBook(Number(book.id));
      else if (type === 'anilist') {
        const id = anilistId(book);
        if (id) await API().open_url(book.site_url || `https://anilist.co/manga/${id}`);
      } else if (type === 'score-series' && series) {
        const linkedBook = series.books.find(item => anilistId(item)) || book;
        const id = anilistId(linkedBook);
        if (id) window.showLiteratureScoreModal?.(
          'manga', Number(linkedBook.id), id, series.title || seriesTitle(linkedBook), linkedBook.user_score,
        );
      } else if (type === 'anilist-search') {
        await window.PudgeMedia?.showMangaAniListSearch?.(
          Number(book.id), seriesTitle(book),
        );
      } else if (type === 'names') {
        await window.showCharacterGlossaryEditor?.(anilistId(book), seriesTitle(book));
      } else if (type === 'ocr-series' && series) {
        void startLibrarySeriesOcr(series.books, series.books.every(item => Boolean(item.ocr_complete)));
      } else if (type === 'ocr-book') {
        void startLibraryBookOcr(book, Boolean(book.ocr_complete));
      } else if (type === 'reset-progress' || (type === 'reset-progress-series' && series)) {
        const confirmed = await window.pudgeConfirm?.(
          ru() ? 'Сбросить прогресс чтения? Настройки и привязка AniList сохранятся.' : 'Reset reading progress? Settings and AniList metadata will be preserved.',
        );
        if (!confirmed) return;
        if (type === 'reset-progress-series' && series) {
          const result = await API().manga_reset_progress_many(series.books.map(item => Number(item.id)));
          const updated = new Map((result?.books || []).map(item => [Number(item.id), item]));
          state.books = (state.books || []).map(item => updated.get(Number(item.id)) || item);
        } else {
          const updated = await API().manga_reset_progress(Number(book.id));
          state.books = (state.books || []).map(item => Number(item.id) === Number(book.id) ? updated : item);
        }
        await renderLibrary(false);
      } else if (type === 'remove-series') {
        const confirmed = await window.pudgeConfirm?.(
          ru() ? 'Удалить эту мангу из Pudge? Исходные CBZ/ZIP останутся на диске.' : 'Remove this manga from Pudge? Source CBZ/ZIP files will stay on disk.',
          {danger:true},
        );
        if (!confirmed) return;
        const key = String(book.series_key || normalizedSeriesKey(seriesTitle(book)) || `book:${book.id}`);
        const removedIds = new Set((state.books || []).filter(item => String(item.series_key || normalizedSeriesKey(seriesTitle(item)) || `book:${item.id}`) === key).map(item => Number(item.id)));
        state.books = (state.books || []).filter(item => !removedIds.has(Number(item.id)));
        for (const id of removedIds) selectedBookIds.delete(id);
        await renderLibrary(false); emitSelection();
        setTimeout(() => { API().manga_remove_series(Number(book.id)).then(() => void renderLibrary(true)).catch(error => { window.toast?.(error?.message || String(error)); void renderLibrary(true); }); }, 0);
      }
      return;
    }
    if (event.target.closest?.('[data-pudge-study-token]')) return;
    const textRegion = event.target.closest?.('.manga-v2-text-region');
    const selectionSurface = event.target.closest?.('.manga-v2-selection-content');
    if (selectionSurface && textRegion) {
      if (Number(event.detail || 0) >= 3) {
        if (selectWholeMangaBubble(textRegion)) {
          event.preventDefault();
          event.stopPropagation();
        }
        return;
      }
      if (mangaOverlaySelectionInRegion(textRegion)) return;
      if (await handleMangaRegionStudyClick(event, textRegion)) {
        event.preventDefault();
        return;
      }
      return;
    }
    if (event.target.closest?.('.manga-v2-page-frame img')) {
      if (await handleMangaImageStudyClick(event)) {
        event.preventDefault();
        return;
      }
    }
    if (textRegion) return;
    if (event.target.id === 'mangaImportV2') {
      // pudge-v0.7.23-manga-folder-picker-v1
      // Bulk manga torrents are usually nested folder trees; selecting the
      // containing folder is the primary import flow.
      const result = await API().choose_manga_folder();
      if (result?.errors?.length) window.toast?.(result.errors.join(' • '));
      if (!result?.cancelled) {
        await renderLibrary();
        const first = (result.books || []).find(book => !anilistId(book));
        if (first) await window.PudgeMedia?.showMangaAniListSearch?.(first.id, first.series_title || first.title || '');
      }
      return;
    }

    const action = event.target.closest?.('[data-manga-v2-action]');
    if (!action) return;
    const type = action.dataset.mangaV2Action;
    if (type === 'read') await openBook(Number(action.dataset.id));
    else if (type === 'anilist' && action.dataset.url) await API().open_url(action.dataset.url);
    else if (type === 'anilist-search') await window.PudgeMedia?.showMangaAniListSearch?.(Number(action.dataset.id), action.dataset.title || '');
    else if (type === 'close') closeReader();
    else if (type === 'next') await movePage(+1);
    else if (type === 'previous') await movePage(-1);
    else if (type === 'ocr-book') await recognizeWholeBook();
    else if (type === 'page-picker') togglePagePicker();
    else if (type === 'fullscreen') await toggleFullscreen();
    else if (type === 'toolbar-show') toggleToolbar(true);
    else if (type === 'settings') $('mangaV2Settings')?.classList.toggle('open');
  }, true);

  document.addEventListener('click', event => {
    const option = event.target?.closest?.('[data-manga-v2-page-option]');
    if (option) {
      event.preventDefault();
      void goToPage(option.dataset.mangaV2PageOption);
      return;
    }
    if (!$('mangaV2PagePicker')?.hidden && !event.target?.closest?.('.manga-v2-page-picker-shell')) closePagePicker();
  }, true);

  document.addEventListener('keydown', event => {
    if (!$('mangaReaderV2')?.classList.contains('open')) return;
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;

    const nextKey = settings.direction === 'rtl' ? 'ArrowLeft' : 'ArrowRight';
    const previousKey = settings.direction === 'rtl' ? 'ArrowRight' : 'ArrowLeft';

    if (event.key === nextKey) {
      event.preventDefault();
      void movePage(+1);
    } else if (event.key === previousKey) {
      event.preventDefault();
      void movePage(-1);
    } else if (event.key.toLowerCase() === 'f' && !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey) {
      event.preventDefault();
      void toggleFullscreen();
    } else if (event.key.toLowerCase() === 't') {
      event.preventDefault();
      toggleToolbar();
    } else if (event.key.toLowerCase() === 'o' && !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey) {
      event.preventDefault();
      if (currentBook?.ocr_complete) void loadTextRegions(currentPage, {refresh:true, showProgress:false, parse:true});
      else window.toast?.(ru() ? 'Дождитесь OCR всего тома' : 'Wait for the whole-volume OCR to finish');
    } else if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      setZoom(Number(settings.zoom || 100) + 10);
    } else if (event.key === '-' || event.key === '_') {
      event.preventDefault();
      setZoom(Number(settings.zoom || 100) - 10);
    } else if (event.key === '0') {
      event.preventDefault();
      setZoom(100);
    }
  });


  document.addEventListener('load', event => {
    if (event.target?.matches?.('.manga-v2-page-frame img')) sizePageImage(event.target);
  }, true);
  window.addEventListener('resize', () => requestAnimationFrame(applyPageSizing));

  const root = $('mangaContent');
  if (root) {
    const observer = new MutationObserver(() => {
      if (libraryRendering) return;
      if (!root.querySelector('.manga-v2-library')) setTimeout(() => void renderLibrary(), 30);
    });
    observer.observe(root, {childList: true});
  }

  function closeEscapeSurface() {
    if (!$('mangaReaderV2')?.classList.contains('open')) return false;
    if (!$('mangaV2PagePicker')?.hidden) { closePagePicker(); return true; }
    if ($('mangaV2Settings')?.classList.contains('open')) {
      $('mangaV2Settings').classList.remove('open');
      return true;
    }
    closeReader();
    return true;
  }

  window.PudgeMangaReaderV2 = {
    renderLibrary,
    injectBook,
    openBook,
    closeEscapeSurface,
    exportDebug: exportMangaOcrDebug,
    selectedBookIds: () => [...selectedBookIds],
    selectAll: () => { (state.books || []).forEach(book => selectedBookIds.add(Number(book.id))); applyLibrarySelection(); emitSelection(); },
    clearSelection: () => { selectedBookIds.clear(); applyLibrarySelection(); emitSelection(); },
    deleteSelected: async () => {
      const ids = [...selectedBookIds];
      if (!ids.length) return;
      const removed = new Set(ids.map(Number));
      state.books = (state.books || []).filter(book => !removed.has(Number(book.id)));
      selectedBookIds.clear();
      await renderLibrary(false);
      emitSelection();
      setTimeout(() => { API().manga_remove_books(ids).then(() => void renderLibrary(true)).catch(error => { window.toast?.(error?.message || String(error)); void renderLibrary(true); }); }, 0);
    },
    ocrSelected: async () => {
      const books = (state.books || []).filter(book => selectedBookIds.has(Number(book.id)));
      for (const book of books) void startLibraryBookOcr(book, Boolean(book.ocr_complete));
    },
    settings: () => ({...settings}),
    consumptionContext: () => ({
      open: Boolean(currentBook && $('mangaReaderV2')?.classList.contains('open')),
      book_id: Number(currentBook?.id || 0),
      page_index: Number(currentPage || 0),
      page_id: String(pageCache.get(`${Number(currentBook?.id || 0)}:${Number(currentPage || 0)}`)?.name || `page:${Number(currentPage || 0)}`),
      page_count: Number(currentPageCount || 0)
    })
  };

  // pudge-v0.7.27-manga-debug-overlay-v1
  const MANGA_DEBUG_OVERLAY_KEY = 'pudge.manga.debugOverlay';
  const MANGA_DEBUG_OVERLAY_STATES = ['off', 'regions', 'words'];

  function mangaReaderNode() {
    return document.getElementById('mangaReaderV2');
  }

  function normalizeMangaDebugOverlay(value) {
    const mode = String(value || 'off').toLowerCase();
    return MANGA_DEBUG_OVERLAY_STATES.includes(mode) ? mode : 'off';
  }

  let mangaDebugOverlayMode = (() => {
    try {
      return normalizeMangaDebugOverlay(localStorage.getItem(MANGA_DEBUG_OVERLAY_KEY));
    } catch (_) {
      return 'off';
    }
  })();

  function mangaDebugOverlayLabel(mode = mangaDebugOverlayMode) {
    if (mode === 'regions') return 'OCR Debug: Regions';
    if (mode === 'words') return 'OCR Debug: Words';
    return 'OCR Debug: Off';
  }

  function mangaDebugRegionText(region) {
    const tokens = [...region.querySelectorAll('[data-pudge-study-token]')]
      .map(node => String(node.textContent || '').trim())
      .filter(Boolean);
    if (tokens.length) return tokens.join(' | ');
    return String(region.querySelector('.manga-v2-region-content')?.textContent || '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function annotateMangaDebugRegion(region) {
    if (!region) return;
    const text = mangaDebugRegionText(region);
    const regionIndex = String(region.dataset.regionIndex || '?');
    const provenance = [
      String(region.dataset.regionOcrBackend || ''),
      String(region.dataset.regionSource || ''),
      String(region.dataset.geometrySource || ''),
    ].filter(Boolean).filter((value, index, rows) => rows.indexOf(value) === index).join('/');
    region.dataset.debugTokens = text.slice(0, 220);
    region.dataset.debugLabel = (`R${regionIndex}${provenance ? ` · ${provenance}` : ''}${text ? ` · ${text}` : ''}`).slice(0, 260);
  }

  function ensureMangaDebugOverlayButton(reader) {
    const toolbar = reader?.querySelector?.('.manga-v2-toolbar');
    if (!toolbar) return null;
    let button = toolbar.querySelector('[data-manga-v2-action="toggle-debug-overlay"]');
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.dataset.mangaV2Action = 'toggle-debug-overlay';
      button.title = 'Cycle visible OCR overlay (Shift+D)';
      const spacer = toolbar.querySelector('.spacer');
      if (spacer?.parentNode === toolbar) toolbar.insertBefore(button, spacer);
      else toolbar.appendChild(button);
    }
    button.textContent = mangaDebugOverlayLabel();
    button.classList.toggle('active', mangaDebugOverlayMode !== 'off');
    return button;
  }


  function clearMangaDebugHitboxes(reader = mangaReaderNode()) {
    reader?.querySelectorAll?.('.manga-v2-debug-hitbox-layer')?.forEach?.(node => node.remove());
  }

  function renderMangaDebugHitboxes(frame) {
    if (!frame || !currentBook || mangaDebugOverlayMode !== 'words') return;
    const image = frame.querySelector('img');
    if (!image?.naturalWidth) return;
    const pageIndex = Number(frame.dataset.pageIndex);
    const regions = textRegionCache.get(textKey(currentBook.id, pageIndex)) || [];
    let layer = frame.querySelector('.manga-v2-debug-hitbox-layer');
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'manga-v2-debug-hitbox-layer';
      frame.appendChild(layer);
    }
    layer.replaceChildren();
    const padX = MANGA_TOKEN_HIT_SLOP_PX / Math.max(1, image.clientWidth || image.naturalWidth);
    const padY = MANGA_TOKEN_HIT_SLOP_PX / Math.max(1, image.clientHeight || image.naturalHeight);
    regions.forEach((region, regionIndex) => {
      const regionNode = frame.querySelector(`.manga-v2-text-region[data-region-index="${Number(regionIndex)}"]`);
      if (!regionNode) return;
      const boxes = mangaTokenHitboxes(regionNode, region);
      boxes.forEach((box, pieceIndex) => {
        const core = document.createElement('div');
        core.className = 'manga-v2-debug-token-hitbox manga-v2-debug-token-core';
        core.dataset.word = String(box.text || '?');
        core.dataset.source = String(box.source || '');
        core.dataset.geometryStatus = mangaGeometryStatus(region, Array.isArray(region.segments) ? region.segments : []);
        core.dataset.regionIndex = String(regionIndex);
        core.dataset.tokenIndex = String(box.tokenIndex);
        core.dataset.pieceIndex = String(pieceIndex);
        core.style.left = `${box.x * 100}%`;
        core.style.top = `${(1 - box.y - box.height) * 100}%`;
        core.style.width = `${box.width * 100}%`;
        core.style.height = `${box.height * 100}%`;
        layer.appendChild(core);

        const slop = document.createElement('div');
        slop.className = 'manga-v2-debug-token-hitbox manga-v2-debug-token-slop';
        slop.dataset.word = String(box.text || '?');
        slop.style.left = `${Math.max(0, box.x-padX) * 100}%`;
        slop.style.top = `${Math.max(0, 1-box.y-box.height-padY) * 100}%`;
        slop.style.width = `${Math.min(1-Math.max(0, box.x-padX), box.width + padX*2) * 100}%`;
        slop.style.height = `${Math.min(1-Math.max(0, 1-box.y-box.height-padY), box.height + padY*2) * 100}%`;
        layer.appendChild(slop);
      });
    });
  }

  function syncMangaDebugHitboxes(reader = mangaReaderNode()) {
    if (!reader) return;
    if (mangaDebugOverlayMode !== 'words') {
      clearMangaDebugHitboxes(reader);
      return;
    }
    reader.querySelectorAll('.manga-v2-page-frame').forEach(renderMangaDebugHitboxes);
  }

  function syncMangaDebugOverlayUi(reader = mangaReaderNode()) {
    if (!reader) return;
    reader.dataset.debugOverlay = mangaDebugOverlayMode;
    ensureMangaDebugOverlayButton(reader);
    reader.querySelectorAll('.manga-v2-text-region').forEach(annotateMangaDebugRegion);
    syncMangaDebugHitboxes(reader);
  }

  function setMangaDebugOverlay(mode) {
    mangaDebugOverlayMode = normalizeMangaDebugOverlay(mode);
    try { localStorage.setItem(MANGA_DEBUG_OVERLAY_KEY, mangaDebugOverlayMode); } catch (_) {}
    syncMangaDebugOverlayUi();
  }

  function cycleMangaDebugOverlay() {
    const index = MANGA_DEBUG_OVERLAY_STATES.indexOf(mangaDebugOverlayMode);
    setMangaDebugOverlay(MANGA_DEBUG_OVERLAY_STATES[(index + 1) % MANGA_DEBUG_OVERLAY_STATES.length]);
  }

  document.addEventListener('click', event => {
    const button = event.target.closest?.('[data-manga-v2-action="toggle-debug-overlay"]');
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    cycleMangaDebugOverlay();
  }, true);

  document.addEventListener('keydown', event => {
    const reader = mangaReaderNode();
    if (!reader?.classList.contains('open')) return;
    if (!event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key !== 'D' && event.key !== 'd') return;
    event.preventDefault();
    cycleMangaDebugOverlay();
  }, true);

  const mangaDebugOverlayObserver = new MutationObserver(() => {
    if (mangaDebugOverlayMode === 'off') return;
    syncMangaDebugOverlayUi();
  });

  queueMicrotask(() => {
    const reader = mangaReaderNode();
    if (!reader) return;
    mangaDebugOverlayObserver.observe(reader, {subtree:true, childList:true, attributes:true, attributeFilter:['class']});
    syncMangaDebugOverlayUi(reader);
  });

})();
