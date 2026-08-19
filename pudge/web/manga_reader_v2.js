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
  let state = {books: []};
  let currentBook = null;
  let currentPage = 0;
  let currentPageCount = 0;
  let pageCache = new Map();
  let textRegionCache = new Map();
  let textRegionResultCache = new Map();
  let textParseCache = new Map();
  let textParseInflight = new Map();
  let mangaDebugEvents = [];
  let mangaDebugSelectionKey = '';
  let mangaLastSelection = null;
  let mangaPointerSelection = null;
  let textGeneration = 0;
  let gestureBaseZoom = 100;
  let gestureActive = false;
  let toolbarPeekTimer = null;
  let libraryRendering = false;
  let verticalObserver = null;
  let verticalPersistTimer = null;
  let pageRenderGeneration = 0;
  let preparationPollTimer = null;
  let preparationPollInFlight = false;
  let mangaContextBook = null;
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
  }

  function toggleSelection(bookId) {
    const id = Number(bookId);
    if (!id) return;
    if (selectedBookIds.has(id)) selectedBookIds.delete(id); else selectedBookIds.add(id);
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
    const page = Math.min(Number(book.page_count || 0), Number(book.position || 0) + 1);
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
    const read = total ? Math.max(0, Math.min(total, Number(book.position || 0) + 1)) : 0;
    const percent = total ? Math.max(0, Math.min(100, Math.round(read / total * 100))) : 0;
    const volume = volumeNumber(book);
    const title = compact ? (ru() ? `Том ${volume}` : `Volume ${volume}`) : esc(book.title || seriesTitle(book));
    const linkedId = anilistId(book);
    const linkedUrl = linkedId ? (book.site_url || `https://anilist.co/manga/${linkedId}`) : '';
    const score = Number(book.user_score || 0);
    const progressTitle = total ? `${read} / ${total} ${ru() ? 'страниц' : 'pages'}` : `${percent}%`;
    const facts = (compact
      ? [`${percent}%`, total ? `${total} ${ru() ? 'стр.' : 'pages'}` : '']
      : [ru() ? `Том ${volume}` : `Volume ${volume}`, total ? `${total} ${ru() ? 'страниц' : 'pages'}` : '', `${percent}%`, score ? `AniList ${score.toFixed(score % 1 ? 1 : 0)}/10` : ''])
      .filter(Boolean)
      .map(value => value === `${percent}%`
        ? `<span title="${esc(progressTitle)}">${esc(value)}</span>`
        : `<span>${esc(value)}</span>`)
      .join('');
    const cover = `<div class="ln-card-cover cover-placeholder" data-cover-placeholder="${id}" ${linkedUrl ? `data-manga-v2-action="anilist" data-url="${esc(linkedUrl)}" title="AniList"` : ''}>Cover</div><img class="ln-card-cover" data-cover-book="${id}" alt="" loading="lazy" decoding="async" ${linkedUrl ? `data-manga-v2-action="anilist" data-url="${esc(linkedUrl)}" title="AniList"` : ''} hidden>`;
    const jiten = linkedId ? `<div class="planned-jiten" data-library-card-jiten data-manga-card-jiten="${linkedId}" data-jiten-book="${id}" data-jiten-volume="${volume}"><span class="planned-jiten-loading">Jiten…</span></div>` : '';
    return `<article class="ln-card ln-entry ${compact ? 'compact' : ''} ${selectedBookIds.has(id) ? 'selected' : ''}" data-manga-book="${id}" data-manga-v2-action="read" data-id="${id}">${cover}<div class="ln-card-body"><h3>${title}</h3><div class="ln-card-meta">${facts}</div><div class="ln-card-progress" title="${esc(progressTitle)}"><span style="width:${percent}%"></span></div>${jiten}</div></article>`;
  }

  function mangaCurrentSeriesBook(books) {
    const unfinished = book => Number(book.position || 0) + 1 < Number(book.page_count || 0);
    return books.find(unfinished) || books[books.length - 1];
  }

  function mangaLibraryGroups(books) {
    return groupBooks(books).map(group => {
      if (group.books.length === 1) return mangaLibraryCard(group.books[0], false);
      const current = mangaCurrentSeriesBook(group.books);
      const scrollClass = group.books.length > 2 ? ' series-scroll' : '';
      const first = group.books[0];
      const linkedId = anilistId(first);
      const seriesJiten = linkedId ? `<div class="planned-jiten" data-planned-jiten data-manga-series-jiten="${linkedId}" data-jiten-book="${Number(first.id)}"><span class="planned-jiten-loading">Jiten…</span></div>` : '';
      return `<section class="ln-series-group"><div class="ln-series-head"><div class="ln-series-title-block"><strong>${esc(group.title)}</strong>${seriesJiten}</div><span class="ln-series-count">${group.books.length} ${ru() ? 'тома/томов' : 'volumes'}</span></div><div class="ln-series-books${scrollClass}" ${group.books.length > 2 ? 'data-series-scroll="1"' : ''} data-series-current-book="${Number(current?.id || 0)}">${group.books.map(book => mangaLibraryCard(book, true)).join('')}</div></section>`;
    }).join('');
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
      root.innerHTML = `<div class="manga-v2-library"><button id="mangaImportV2" hidden>Import</button>${books.length ? `<div class="ln-grid">${mangaLibraryGroups(books)}</div>` : `<div class="empty">${ru() ? 'Добавьте CBZ/ZIP с изображениями страниц.' : 'Add a CBZ/ZIP containing page images.'}</div>`}</div>`;
      for (const book of books) {
        void resolveCover(book).then(url => {
          if (!url) return;
          const img = root.querySelector(`img[data-cover-book="${Number(book.id)}"]`);
          const placeholder = root.querySelector(`[data-cover-placeholder="${Number(book.id)}"]`);
          if (img) { img.src = url; img.hidden = false; }
          if (placeholder) placeholder.hidden = true;
        });
      }
      applyLibrarySelection();
      hydrateLibraryJiten(root, books);
      requestAnimationFrame(() => window.PudgeSeriesScroll?.focus?.(root));
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
        <span id="mangaV2PageLabel"></span>
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

  async function persistVisiblePage() {
    if (!currentBook || currentPageCount <= 0) return;
    try { await API().manga_page(Number(currentBook.id), Number(currentPage)); } catch (_) {}
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

  function renderRegionContent(target, region, payload = null) {
    if (!target || !region) return;
    let content = target.querySelector('.manga-v2-region-content');
    if (!content) {
      content = document.createElement('div');
      content.className = 'manga-v2-region-content';
      target.appendChild(content);
    }
    const tools = window.PudgeReadingTools;
    const html = payload && tools?.study?.renderParsedText
      ? tools.study.renderParsedText(payload, {backend: currentStudyBackend(payload)})
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
    let layer = frame.querySelector('.manga-v2-text-layer');
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'manga-v2-text-layer';
      frame.appendChild(layer);
    }
    layer.innerHTML = regions.map((region, regionIndex) => {
      const rawX = Math.max(0, Math.min(1, Number(region.x || 0)));
      const rawY = Math.max(0, Math.min(1, Number(region.y || 0)));
      const rawWidth = Math.max(0, Math.min(1 - rawX, Number(region.width || 0)));
      const rawHeight = Math.max(0, Math.min(1 - rawY, Number(region.height || 0)));
      // Vision boxes hug glyphs very tightly. A small visual-only expansion
      // makes the whole speech area easy to enter without changing the crop
      // MangaOCR received or merging neighbouring bubbles.
      const orientation = effectiveRegionOrientation(region, rawWidth, rawHeight);
      const isVertical = orientation.vertical;
      const padX = isVertical
        ? Math.max(.034, Math.min(.075, rawWidth * 1.1))
        : .018;
      const padY = isVertical ? .024 : .016;
      const x = Math.max(0, rawX - padX);
      const y = Math.max(0, rawY - padY);
      const width = Math.min(1 - x, rawWidth + padX * 2);
      const height = Math.min(1 - y, rawHeight + padY * 2);
      const top = Math.max(0, 1 - y - height);
      const edge = x > .58 ? ' edge-right' : '';
      const vertical = isVertical ? ' vertical-text' : '';
      return `<div class="manga-v2-text-region${edge}${vertical}" tabindex="0"
        data-page-index="${Number(pageIndex)}" data-region-index="${regionIndex}"
        data-effective-orientation="${isVertical ? 'vertical' : 'horizontal'}"
        data-orientation-reason="${esc(orientation.reason)}"
        data-raw-x="${rawX}" data-raw-y="${rawY}" data-raw-width="${rawWidth}" data-raw-height="${rawHeight}"
        data-pudge-translate-root
        data-pudge-media-id="${Number(currentBook.anilist_id || 0)}"
        data-pudge-translate-language="${ru() ? 'ru' : 'en'}"
        aria-label="${esc(region.text || '')}"
        style="left:${x * 100}%;top:${top * 100}%;width:${width * 100}%;height:${height * 100}%">
          <div class="manga-v2-region-content">${esc(region.text || '')}</div>
        </div>`;
    }).join('');
    // renderTextLayer is called repeatedly while background OCR preparation is
    // polling. Re-apply parsed payloads immediately; otherwise every refresh
    // silently replaces clickable study spans with plain text.
    regions.forEach((region, regionIndex) => {
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
    for (let regionIndex = 0; regionIndex < regions.length; regionIndex++) {
      if (generation !== textGeneration) return;
      await parseRegionText(pageIndex, regionIndex, regions[regionIndex], generation);
    }
  }

  async function loadTextRegions(
    pageIndex,
    {refresh = false, showProgress = false, parse = false, cachedOnly = false} = {},
  ) {
    if (!currentBook || !API()?.manga_text_regions) return null;
    const generation = textGeneration;
    const bookId = Number(currentBook.id);
    const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
    const key = textKey(bookId, index);
    const progress = $('mangaV2OcrProgress');
    if (!refresh && textRegionCache.has(key)) {
      mangaDebugRecord('ocr_memory_cache', {page_index:index, region_count:(textRegionCache.get(key) || []).length});
      const frame = $('mangaV2Pages')?.querySelector(`[data-page-index="${index}"]`);
      if (frame) renderTextLayer(frame, index);
      const regions = textRegionCache.get(key) || [];
      if (parse) await parseRegionsSequentially(index, regions, generation);
      return {regions, cached:true};
    }
    if (showProgress && progress) progress.textContent = `OCR · ${index + 1}/${currentPageCount}…`;
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
      if (showProgress && progress) setTimeout(() => { progress.textContent = ''; }, 650);
    }
  }

  function visiblePageIndices() {
    return [...($('mangaV2Pages')?.querySelectorAll('[data-page-index]') || [])]
      .filter(frame => !frame.classList.contains('manga-v2-lazy') || frame.dataset.loaded === '1')
      .map(frame => Number(frame.dataset.pageIndex))
      .filter(Number.isFinite);
  }

  function stopPreparationPoll() {
    if (preparationPollTimer) clearTimeout(preparationPollTimer);
    preparationPollTimer = null;
  }

  async function refreshVisibleTextRegions() {
    const indices = visiblePageIndices();
    await Promise.all(indices.map(index => loadTextRegions(index, {
      cachedOnly: true,
      parse: true,
    })));
  }

  async function pollCurrentBookPreparation(bookId) {
    if (preparationPollInFlight || !currentBook || Number(currentBook.id) !== Number(bookId)) return;
    preparationPollInFlight = true;
    stopPreparationPoll();
    try {
      const status = await API().manga_ocr_book_status(Number(bookId));
      if (!currentBook || Number(currentBook.id) !== Number(bookId)) return;
      currentBook.ocr_cached_pages = Number(status.cached_pages || 0);
      currentBook.ocr_complete = Boolean(status.complete);
      await refreshVisibleTextRegions();
      const progress = $('mangaV2OcrProgress');
      if (progress && status.running) {
        progress.textContent = status.state === 'parsing'
          ? `${ru() ? 'Готовлю Jiten' : 'Preparing Jiten'} ${Number(status.parsed_regions || 0)}/${Number(status.total_regions || 0)}`
          : ru() ? 'Обработалось' : 'Processed';
      }
      if (status.running) {
        preparationPollTimer = setTimeout(
          () => void pollCurrentBookPreparation(Number(bookId)),
          750,
        );
      } else if (progress) {
        progress.textContent = '';
      }
    } catch (error) {
      console.debug?.('Manga preparation status unavailable:', error);
    } finally {
      preparationPollInFlight = false;
    }
  }

  async function ensureCurrentBookPrepared() {
    if (!currentBook || !state.ocr_available) return;
    const bookId = Number(currentBook.id);
    try {
      if (!currentBook.ocr_complete) await API().start_manga_ocr_book(bookId);
      await pollCurrentBookPreparation(bookId);
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
    button?.classList.add('busy');
    try {
      await API().start_manga_ocr_book(bookId);
      while (currentBook && Number(currentBook.id) === bookId) {
        const status = await API().manga_ocr_book_status(bookId);
        if (progress) {
          progress.textContent = status.state === 'parsing'
            ? `${ru() ? 'Готовлю Jiten' : 'Preparing Jiten'} ${Number(status.parsed_regions || 0)}/${Number(status.total_regions || 0)}`
            : ru() ? 'Обработалось' : 'Processed';
        }
        if (!status.running) break;
        await new Promise(resolve => setTimeout(resolve, 800));
      }
      textRegionCache.clear();
      textParseCache.clear();
      if (currentBook && Number(currentBook.id) === bookId) await refreshVisibleTextRegions();
    } finally {
      button?.classList.remove('busy');
      if (progress) setTimeout(() => { progress.textContent = ''; }, 650);
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
    const menu = $('contextMenu');
    if (!menu) return;
    const linked = Boolean(anilistId(book));
    const selected = selectedBookIds.has(Number(book.id));
    const select = `<button data-manga-context-action="select">${selected ? (ru() ? 'Снять выделение' : 'Deselect') : (ru() ? 'Выделить' : 'Select')}</button>`;
    const openAniList = linked
      ? `<button data-manga-context-action="anilist">${ru() ? 'Открыть AniList' : 'Open AniList'}</button>`
      : '';
    const score = linked
      ? `<button data-manga-context-action="score">${ru() ? 'Поставить оценку' : 'Set score'}</button>`
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
      ${openAniList}${score}${link}${names}
      <button data-manga-context-action="ocr-book">${ocr}</button>
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

  async function renderPaged() {
    if (!currentBook) return;
    clearMangaTransientOverlays();
    const generation = ++pageRenderGeneration;
    const bookId = Number(currentBook.id);
    const pages = $('mangaV2Pages');
    const step = settings.mode === 'double' ? 2 : 1;
    currentPage = Math.max(0, Math.min(currentPage, Math.max(0, currentPageCount - 1)));
    if (settings.mode === 'double') currentPage = Math.floor(currentPage / 2) * 2;
    const indices = [currentPage];
    if (settings.mode === 'double' && currentPage + 1 < currentPageCount) indices.push(currentPage + 1);
    const loaded = await Promise.all(indices.map(getPage));
    if (generation !== pageRenderGeneration || !currentBook || Number(currentBook.id) !== bookId) return;
    pages.innerHTML = loaded.map(page => `
      <figure class="manga-v2-page-frame" data-page-index="${Number(page.page_index)}">
        <img src="${page.data_uri}" alt="${esc(page.name || '')}">
        <div class="manga-v2-text-layer"></div>
      </figure>`).join('');
    $('mangaV2PageLabel').textContent =
      settings.mode === 'double' && loaded.length > 1
        ? `${loaded[0].page_index + 1}–${loaded[loaded.length - 1].page_index + 1} / ${currentPageCount}`
        : `${currentPage + 1} / ${currentPageCount}`;
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
        currentPage = best.index;
        $('mangaV2PageLabel').textContent = `${currentPage + 1} / ${currentPageCount}`;
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
    $('mangaV2Title').textContent = `${seriesTitle(currentBook)} · ${volumeLabel(currentBook, 0)}`;
    reader.classList.add('open');
    document.body.classList.add('manga-v2-reading');
    await showCurrent();
    void ensureCurrentBookPrepared();
  }

  function closeReader() {
    textGeneration += 1;
    pageRenderGeneration += 1;
    stopPreparationPoll();
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
    currentPageCount = 0;
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    void renderLibrary();
  }

  async function movePage(delta) {
    if (!currentBook || settings.mode === 'vertical') return;
    clearMangaTransientOverlays();
    const step = settings.mode === 'double' ? 2 : 1;
    const previousPage = currentPage;
    currentPage = Math.max(0, Math.min(currentPageCount - 1, currentPage + delta * step));
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
    if (settings.mode === 'double') {
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

  function setZoom(value, {persist = true} = {}) {
    settings.zoom = Math.max(50, Math.min(250, Math.round(Number(value || 100) / 5) * 5));
    if (persist) saveSettings();
    applyReaderSettings();
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
      setZoom(Number(settings.zoom || 100) + (event.deltaY < 0 ? 10 : -10));
    }, {passive:false});

    reader.addEventListener('gesturestart', event => {
      gestureActive = true;
      gestureBaseZoom = Number(settings.zoom || 100);
      event.preventDefault();
    }, {passive:false});
    reader.addEventListener('gesturechange', event => {
      event.preventDefault();
      setZoom(gestureBaseZoom * Number(event.scale || 1), {persist:false});
    }, {passive:false});
    reader.addEventListener('gestureend', event => {
      event.preventDefault();
      gestureActive = false;
      saveSettings();
      syncSettingsControls();
    }, {passive:false});

    reader.addEventListener('pointermove', event => {
      if (!settings.toolbar && event.clientY <= 20) peekToolbar();
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
    if (!entry || entry.closest?.('#mangaReaderV2')) return;
    const book = (state.books || []).find(
      item => Number(item.id) === Number(entry.dataset.mangaBook)
    );
    if (!book) return;
    event.preventDefault();
    event.stopPropagation();
    showMangaContextMenu(book, event.clientX, event.clientY);
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

  function mangaTokenAtPoint(frame, regionNode, region, clientX, clientY) {
    if (!frame || !regionNode || !region) return null;
    const tokens = [...regionNode.querySelectorAll('[data-pudge-study-token]')];
    if (!tokens.length) return null;
    const rawRect = rawRegionRect(frame, regionNode);
    if (!rawRect) return null;
    const totalChars = tokens.reduce((sum, token) => sum + Math.max(1, [...String(token.textContent || '').trim()].length), 0);
    const vertical = String(regionNode.dataset.effectiveOrientation || '') === 'vertical';
    const segments = Array.isArray(region.segments) ? region.segments.filter(item => item && Number(item.width) > 0 && Number(item.height) > 0) : [];
    if (segments.length > 1) {
      const imageRect = rawRect.imageRect;
      const px = (clientX - imageRect.left) / Math.max(1, imageRect.width);
      const pyBottom = 1 - (clientY - imageRect.top) / Math.max(1, imageRect.height);
      const weighted = segments.map(segment => {
        const textWeight = [...String(segment.text || '').replace(/\s+/g, '')].length;
        const geometryWeight = vertical ? Number(segment.height || 0) : Number(segment.width || 0);
        return {segment, weight:Math.max(1, textWeight || Math.round(geometryWeight * 100))};
      });
      const nearest = weighted.map((item, index) => {
        const s = item.segment;
        const left = Number(s.x || 0), right = left + Number(s.width || 0);
        const bottom = Number(s.y || 0), top = bottom + Number(s.height || 0);
        const dx = px < left ? left - px : (px > right ? px - right : 0);
        const dy = pyBottom < bottom ? bottom - pyBottom : (pyBottom > top ? pyBottom - top : 0);
        return {...item, index, distance:Math.hypot(dx, dy)};
      }).sort((a,b)=>a.distance-b.distance)[0];
      if (nearest) {
        const totalWeight = weighted.reduce((sum,item)=>sum+item.weight,0);
        const before = weighted.slice(0, nearest.index).reduce((sum,item)=>sum+item.weight,0);
        const s = nearest.segment;
        const local = vertical
          ? Math.max(0, Math.min(.999, (clientY - (imageRect.top + (1 - Number(s.y || 0) - Number(s.height || 0)) * imageRect.height)) / Math.max(1, Number(s.height || 0) * imageRect.height)))
          : Math.max(0, Math.min(.999, (clientX - (imageRect.left + Number(s.x || 0) * imageRect.width)) / Math.max(1, Number(s.width || 0) * imageRect.width)));
        return tokenForCharacterOffset(tokens, (before + local * nearest.weight) / Math.max(1, totalWeight) * totalChars);
      }
    }
    if (!vertical) {
      const ratio = Math.max(0, Math.min(.999, (clientX - rawRect.left) / Math.max(1, rawRect.width)));
      return tokenForCharacterOffset(tokens, ratio * totalChars);
    }
    // Cached OCR artifacts from older versions do not have per-column segments.
    // Reconstruct a compact vertical reading grid from the raw OCR rectangle so
    // x chooses the right-to-left column and y chooses the character within it.
    const aspect = Math.max(.15, rawRect.width / Math.max(1, rawRect.height));
    const columns = Math.max(1, Math.min(totalChars, Math.round(Math.sqrt(totalChars * aspect * .72))));
    const rows = Math.max(1, Math.ceil(totalChars / columns));
    const column = Math.max(0, Math.min(columns - 1, Math.floor((rawRect.right - clientX) / Math.max(1, rawRect.width) * columns)));
    const row = Math.max(0, Math.min(rows - 1, Math.floor((clientY - rawRect.top) / Math.max(1, rawRect.height) * rows)));
    return tokenForCharacterOffset(tokens, Math.min(totalChars - .001, column * rows + row));
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
    const token = mangaTokenAtPoint(frame, regionNode, region, event.clientX, event.clientY);
    if (token) return await dispatchMangaStudyClick(token, event);
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
      const type = contextAction.dataset.mangaContextAction;
      closeMangaContextMenu();
      if (!book) return;
      if (type === 'select') toggleSelection(Number(book.id));
      else if (type === 'read') await openBook(Number(book.id));
      else if (type === 'anilist') {
        const id = anilistId(book);
        if (id) await API().open_url(book.site_url || `https://anilist.co/manga/${id}`);
      } else if (type === 'score') {
        const id = anilistId(book);
        if (id) window.showLiteratureScoreModal?.(
          'manga', Number(book.id), id, seriesTitle(book), book.user_score,
        );
      } else if (type === 'anilist-search') {
        await window.PudgeMedia?.showMangaAniListSearch?.(
          Number(book.id), seriesTitle(book),
        );
      } else if (type === 'names') {
        await window.showCharacterGlossaryEditor?.(anilistId(book), seriesTitle(book));
      } else if (type === 'ocr-book') {
        void startLibraryBookOcr(book, Boolean(book.ocr_complete));
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
    if (event.target.closest?.('.manga-v2-page-frame img')) {
      if (await handleMangaImageStudyClick(event)) {
        event.preventDefault();
        return;
      }
    }
    const textRegion = event.target.closest?.('.manga-v2-text-region');
    if (textRegion) return;
    if (event.target.id === 'mangaImportV2') {
      const result = await API().choose_manga_file();
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
    else if (type === 'fullscreen') await toggleFullscreen();
    else if (type === 'toolbar-show') toggleToolbar(true);
    else if (type === 'settings') $('mangaV2Settings')?.classList.toggle('open');
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
    } else if (event.key.toLowerCase() === 'f') {
      event.preventDefault();
      void toggleFullscreen();
    } else if (event.key.toLowerCase() === 't') {
      event.preventDefault();
      toggleToolbar();
    } else if (event.key.toLowerCase() === 'o' && !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey) {
      event.preventDefault();
      void loadTextRegions(currentPage, {refresh:true, showProgress:true, parse:true});
    } else if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      setZoom(Number(settings.zoom || 100) + 10);
    } else if (event.key === '-' || event.key === '_') {
      event.preventDefault();
      setZoom(Number(settings.zoom || 100) - 10);
    } else if (event.key === '0') {
      event.preventDefault();
      setZoom(100);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      if ($('mangaV2Settings')?.classList.contains('open')) $('mangaV2Settings').classList.remove('open');
      else {
        window.PudgeReadingTools?.closeAll?.();
        closeReader();
      }
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

  window.PudgeMangaReaderV2 = {
    renderLibrary,
    injectBook,
    openBook,
    exportDebug: exportMangaOcrDebug,
    selectedBookIds: () => [...selectedBookIds],
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
    settings: () => ({...settings})
  };
})();
