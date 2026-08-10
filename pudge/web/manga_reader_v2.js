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
  let textParseCache = new Map();
  let textParseInflight = new Map();
  let textGeneration = 0;
  let ocrCache = new Map();
  let ocrParsedCache = new Map();
  let ocrGeneration = 0;
  let ocrParseGeneration = 0;
  let gestureBaseZoom = 100;
  let gestureActive = false;
  let toolbarPeekTimer = null;
  let libraryRendering = false;
  let verticalObserver = null;
  let verticalPersistTimer = null;

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
    // Covers are sticky. Never ask AniList just because the library was opened:
    // use the already accepted cached artwork first, then persisted metadata,
    // then the cached first-page thumbnail supplied by the backend.
    if (id && coverCache[id]) return coverCache[id];
    const existing = existingRemoteCover(book);
    if (existing) {
      if (id) { coverCache[id] = existing; saveCoverCache(); }
      return existing;
    }
    return localCover(book);
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

  async function renderLibrary() {
    document.addEventListener('selectionchange', () => {
    const panel = $('mangaV2OcrText');
    const selection = window.getSelection();
    if (!panel?.classList.contains('open') || (selection && !selection.isCollapsed)) return;
    updateOcrPanel(currentPage);
  });

  const root = $('mangaContent');
    if (!root || !API()) return;
    libraryRendering = true;
    try {
      state = await API().manga_state();
      const books = state.books || [];
      const groups = groupBooks(books);
      root.innerHTML = `
        <div class="manga-v2-library">
          <div class="media-head manga-v2-head">
            <div><h2>${ru() ? 'Манга' : 'Manga'}</h2></div>
            <button id="mangaImportV2" class="primary">${ru() ? 'Добавить CBZ / ZIP' : 'Add CBZ / ZIP'}</button>
          </div>
          <div class="manga-v2-series-list">
            ${groups.map(group => {
              const continueBook = [...group.books].sort((a,b) => Number(b.updated_at||0)-Number(a.updated_at||0))[0];
              const linkedBook = group.books.find(book => anilistId(book)) || group.books[0];
              const linkedId = anilistId(linkedBook);
              const linkedUrl = linkedId ? (linkedBook.site_url || `https://anilist.co/manga/${linkedId}`) : '';
              const aniActions = linkedId
                ? `<button data-manga-v2-action="anilist" data-url="${esc(linkedUrl)}">AniList</button><button data-manga-v2-action="anilist-search" data-id="${Number(linkedBook.id)}" data-title="${esc(group.title)}">${ru() ? 'Изменить' : 'Change'}</button>`
                : `<button data-manga-v2-action="anilist-search" data-id="${Number(linkedBook.id)}" data-title="${esc(group.title)}">${ru() ? 'Найти в AniList' : 'Find on AniList'}</button>`;
              return `
                <article class="manga-v2-series" data-series="${esc(group.key)}" data-manga-v2-action="read" data-id="${Number(continueBook.id)}">
                  <div class="manga-v2-cover-shell"${linkedUrl ? ` data-manga-v2-action="anilist" data-url="${esc(linkedUrl)}"` : ''}>
                    <div class="manga-v2-cover-placeholder">漫</div>
                    <img class="manga-v2-cover" data-cover-book="${Number(group.books[0].id)}" alt="" width="92" height="132" loading="lazy" decoding="async">
                  </div>
                  <div class="manga-v2-series-body">
                    <div class="manga-v2-series-top">
                      <div>
                        <strong class="manga-v2-series-title">${esc(group.title)}</strong>
                        <span>${group.books.length} ${ru() ? 'том(а)' : (group.books.length === 1 ? 'volume' : 'volumes')}</span>
                      </div>
                      <div class="manga-v2-series-actions">${aniActions}<button class="primary" data-manga-v2-action="read" data-id="${Number(continueBook.id)}">${ru() ? 'Продолжить' : 'Continue'}</button></div>
                    </div>
                    <div class="manga-v2-volumes">
                      ${group.books.map((book, index) => `
                        <button class="manga-v2-volume" data-manga-v2-action="read" data-id="${Number(book.id)}"
                          title="${esc(book.title)}">
                          <b>${esc(volumeLabel(book, index))}</b>
                          <span>${progressText(book)}</span>
                        </button>`).join('')}
                    </div>
                  </div>
                </article>`;
            }).join('')}
          </div>
          ${books.length ? '' : `<div class="empty">${ru() ? 'Добавьте CBZ/ZIP с изображениями страниц.' : 'Add a CBZ/ZIP containing page images.'}</div>`}
        </div>`;
      for (const group of groups) {
        const book = group.books[0];
        void resolveCover(book).then(url => {
          if (!url) return;
          const img = root.querySelector(`img[data-cover-book="${Number(book.id)}"]`);
          if (img) {
            img.src = url;
            img.classList.add('ready');
          }
        });
      }
    } catch (error) {
      root.innerHTML = `<div class="empty">${esc(error?.message || error)}</div>`;
    } finally {
      setTimeout(() => { libraryRendering = false; }, 0);
    }
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
        <button data-manga-v2-action="ocr-page">OCR</button>
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

  function ocrKey(bookId, pageIndex) {
    return `${Number(bookId)}:${Number(pageIndex)}`;
  }

  async function getPage(index) {
    const clamped = Math.max(0, Math.min(Number(index), currentPageCount - 1));
    const key = `${Number(currentBook.id)}:${clamped}`;
    if (pageCache.has(key)) return pageCache.get(key);
    const page = await API().manga_page(Number(currentBook.id), clamped);
    pageCache.set(key, page);
    return page;
  }

  async function persistVisiblePage() {
    if (!currentBook || currentPageCount <= 0) return;
    try { await API().manga_page(Number(currentBook.id), Number(currentPage)); } catch (_) {}
  }

  function hasActiveSelectionInside(root) {
    const selection = window.getSelection();
    return Boolean(selection && !selection.isCollapsed && selection.rangeCount &&
      root.contains(selection.getRangeAt(0).commonAncestorContainer));
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

  function deactivateTextRegion(target) {
    if (!target) return;
    target.classList.remove('active');
    const content = target.querySelector('.manga-v2-region-content');
    if (content) content.replaceChildren();
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
    target.classList.add('active');
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
      const x = Math.max(0, Math.min(1, Number(region.x || 0)));
      const y = Math.max(0, Math.min(1, Number(region.y || 0)));
      const width = Math.max(0, Math.min(1 - x, Number(region.width || 0)));
      const height = Math.max(0, Math.min(1 - y, Number(region.height || 0)));
      const top = Math.max(0, 1 - y - height);
      const edge = x > .58 ? ' edge-right' : '';
      return `<div class="manga-v2-text-region${edge}" tabindex="0"
        data-page-index="${Number(pageIndex)}" data-region-index="${regionIndex}"
        data-pudge-study-hover="1" data-pudge-translate-root
        data-pudge-translate-language="${ru() ? 'ru' : 'en'}"
        aria-label="${esc(region.text || '')}"
        style="left:${x * 100}%;top:${top * 100}%;width:${width * 100}%;height:${height * 100}%"></div>`;
    }).join('');
  }

  async function parseRegionText(pageIndex, regionIndex, region, generation = textGeneration) {
    if (!currentBook || !region || !API()?.study_parse_text) return null;
    const bookId = Number(currentBook.id);
    const key = `${textKey(bookId, pageIndex)}:${Number(regionIndex)}`;
    if (textParseCache.has(key)) return textParseCache.get(key);
    if (textParseInflight.has(key)) return textParseInflight.get(key);
    const text = String(region.text || '').replace(/[\r\n]+/g, ' ').trim();
    if (!text) return null;
    const request = (async () => {
      try {
        const payload = await API().study_parse_text(text);
        if (generation !== textGeneration || !currentBook || Number(currentBook.id) !== bookId) return null;
        textParseCache.set(key, payload);
        const target = $('mangaV2Pages')?.querySelector(`.manga-v2-text-region[data-page-index="${Number(pageIndex)}"][data-region-index="${Number(regionIndex)}"]`);
        if (target?.classList.contains('active')) renderRegionContent(target, region, payload);
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

  async function loadTextRegions(pageIndex, {refresh = false, showProgress = false, parse = false} = {}) {
    if (!currentBook || !API()?.manga_text_regions) return null;
    const generation = textGeneration;
    const bookId = Number(currentBook.id);
    const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
    const key = textKey(bookId, index);
    const progress = $('mangaV2OcrProgress');
    if (!refresh && textRegionCache.has(key)) {
      const frame = $('mangaV2Pages')?.querySelector(`[data-page-index="${index}"]`);
      if (frame) renderTextLayer(frame, index);
      return {regions:textRegionCache.get(key) || [], cached:true};
    }
    if (showProgress && progress) progress.textContent = `OCR · ${index + 1}/${currentPageCount}…`;
    try {
      const result = await API().manga_text_regions(bookId, index, Boolean(refresh));
      if (generation !== textGeneration || !currentBook || Number(currentBook.id) !== bookId) return null;
      const regions = Array.isArray(result?.regions) ? result.regions : [];
      textRegionCache.set(key, regions);
      if (refresh) {
        for (const cacheKey of [...textParseCache.keys()]) if (cacheKey.startsWith(`${key}:`)) textParseCache.delete(cacheKey);
      }
      const frame = $('mangaV2Pages')?.querySelector(`[data-page-index="${index}"]`);
      if (frame) renderTextLayer(frame, index);
      return result;
    } catch (error) {
      console.debug?.('Manga OCR regions unavailable:', error);
      return null;
    } finally {
      if (showProgress && progress) setTimeout(() => { progress.textContent = ''; }, 650);
    }
  }

  function activateTextRegion(target) {
    if (!target || !currentBook) return;
    const pageIndex = Number(target.dataset.pageIndex);
    const regionIndex = Number(target.dataset.regionIndex);
    const key = textKey(currentBook.id, pageIndex);
    const regions = textRegionCache.get(key) || [];
    const region = regions[regionIndex];
    if (!region) return;
    const parsed = textParseCache.get(`${key}:${regionIndex}`) || null;
    renderRegionContent(target, region, parsed);
    if (!parsed) void parseRegionText(pageIndex, regionIndex, region);
  }

  async function recognizeWholeBook() {
    if (!currentBook) return;
    const generation = textGeneration;
    const progress = $('mangaV2OcrProgress');
    const button = document.querySelector('[data-manga-v2-action="ocr-book"]');
    button?.classList.add('busy');
    try {
      for (let index = 0; index < currentPageCount; index++) {
        if (generation !== textGeneration) break;
        if (progress) progress.textContent = `${ru() ? 'Распознаю' : 'Recognizing'} ${index + 1}/${currentPageCount}`;
        await loadTextRegions(index, {refresh:false, showProgress:false});
      }
    } finally {
      button?.classList.remove('busy');
      if (progress) setTimeout(() => { progress.textContent = ''; }, 650);
    }
  }

  function updateOcrPanel(pageIndex = currentPage) {
    const panel = $('mangaV2OcrText');
    if (!panel || !currentBook) return;
    const key = ocrKey(currentBook.id, pageIndex);
    const cached = ocrCache.get(key);
    if (!cached) {
      panel.classList.remove('open');
      panel.textContent = '';
      panel.removeAttribute('data-ocr-key');
      return;
    }
    const parsed = ocrParsedCache.get(key);
    const preserveSelection = hasActiveSelectionInside(panel);
    if (parsed && parsed.source === cached && !preserveSelection) {
      if (panel.dataset.ocrRender !== `parsed:${key}`) panel.innerHTML = parsed.html;
      panel.dataset.ocrRender = `parsed:${key}`;
    } else if (!preserveSelection && panel.dataset.ocrRender !== `plain:${key}:${cached.length}`) {
      panel.textContent = cached;
      panel.dataset.ocrRender = `plain:${key}:${cached.length}`;
    }
    panel.dataset.ocrKey = key;
    panel.dataset.ocrLabel = ru() ? 'OCR-текст' : 'OCR text';
    panel.dataset.pudgeTranslateLanguage = ru() ? 'ru' : 'en';
    panel.classList.add('open');
  }

  async function parseOcrStudyText(pageIndex, text, generation = ocrParseGeneration) {
    if (!currentBook || !text || !/[\u3040-\u30ff\u3400-\u9fff]/.test(text)) return;
    const api = API();
    const tools = window.PudgeReadingTools;
    if (!api?.study_parse_text || !tools?.study?.renderParsedText) return;
    const bookId = Number(currentBook.id);
    const key = ocrKey(bookId, pageIndex);
    const existing = ocrParsedCache.get(key);
    if (existing?.source === text) return;
    try {
      const payload = await api.study_parse_text(text);
      if (generation !== ocrParseGeneration || !currentBook || Number(currentBook.id) !== bookId) return;
      if (ocrCache.get(key) !== text) return;
      const html = tools.study.renderParsedText(payload, {
        backend: currentStudyBackend(payload),
      });
      ocrParsedCache.set(key, {source: text, html});
      if (currentPage === Number(pageIndex)) updateOcrPanel(pageIndex);
    } catch (error) {
      console.debug?.('Manga Jiten parse unavailable:', error);
    }
  }

  function storeOcrText(pageIndex, text, {parse = true} = {}) {
    if (!currentBook) return;
    const index = Number(pageIndex);
    const key = ocrKey(currentBook.id, index);
    const value = String(text || '');
    const previous = ocrCache.get(key);
    ocrCache.set(key, value);
    if (previous !== value) ocrParsedCache.delete(key);
    if (currentPage === index) updateOcrPanel(index);
    if (parse && value) void parseOcrStudyText(index, value);
  }

  async function loadCachedOcr(pageIndex = currentPage) {
    if (!currentBook || !API()?.manga_ocr_cached_page) return null;
    const generation = ocrGeneration;
    const bookId = Number(currentBook.id);
    const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
    try {
      const result = await API().manga_ocr_cached_page(bookId, index);
      if (generation !== ocrGeneration || !currentBook || Number(currentBook.id) !== bookId) return null;
      if (!result?.cached) return result;
      storeOcrText(index, String(result.text || ''), {parse: currentPage === index});
      const progress = $('mangaV2OcrProgress');
      if (currentPage === index && progress) {
        progress.textContent = '';
      }
      return result;
    } catch (error) {
      console.debug?.('Cached manga OCR lookup failed:', error);
      return null;
    }
  }

  async function renderPaged() {
    if (!currentBook) return;
    const pages = $('mangaV2Pages');
    const step = settings.mode === 'double' ? 2 : 1;
    currentPage = Math.max(0, Math.min(currentPage, Math.max(0, currentPageCount - 1)));
    if (settings.mode === 'double') currentPage = Math.floor(currentPage / 2) * 2;
    const indices = [currentPage];
    if (settings.mode === 'double' && currentPage + 1 < currentPageCount) indices.push(currentPage + 1);
    const loaded = await Promise.all(indices.map(getPage));
    pages.innerHTML = loaded.map(page => `
      <figure class="manga-v2-page-frame" data-page-index="${Number(page.page_index)}">
        <img src="${page.data_uri}" alt="${esc(page.name || '')}">
        <div class="manga-v2-text-layer"></div>
      </figure>`).join('');
    $('mangaV2PageLabel').textContent =
      settings.mode === 'double' && loaded.length > 1
        ? `${loaded[0].page_index + 1}–${loaded[loaded.length - 1].page_index + 1} / ${currentPageCount}`
        : `${currentPage + 1} / ${currentPageCount}`;
    for (const page of loaded) void loadTextRegions(Number(page.page_index));
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
            frame.innerHTML = `<img src="${page.data_uri}" alt="${esc(page.name || '')}"><div class="manga-v2-text-layer"></div>`;
            frame.dataset.loaded = '1';
            void loadTextRegions(index);
          }).catch(() => { frame.dataset.loaded = ''; });
        }
        if (entry.isIntersecting && (!best || entry.intersectionRatio > best.ratio)) {
          best = {index, ratio: entry.intersectionRatio};
        }
      }
      if (best) {
        currentPage = best.index;
        $('mangaV2PageLabel').textContent = `${currentPage + 1} / ${currentPageCount}`;
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
    state = await API().manga_state();
    currentBook = (state.books || []).find(book => Number(book.id) === Number(bookId));
    if (!currentBook) return;
    currentPage = Number(currentBook.position || 0);
    currentPageCount = Number(currentBook.page_count || 0);
    pageCache = new Map();
    textRegionCache = new Map();
    textParseCache = new Map();
    textParseInflight = new Map();
    textGeneration += 1;
    ocrCache = new Map();
    ocrParsedCache = new Map();
    ocrGeneration += 1;
    ocrParseGeneration += 1;
    const reader = buildReader();
    $('mangaV2Title').textContent = `${seriesTitle(currentBook)} · ${volumeLabel(currentBook, 0)}`;
    reader.classList.add('open');
    document.body.classList.add('manga-v2-reading');
    await showCurrent();
  }

  function closeReader() {
    textGeneration += 1;
    ocrGeneration += 1;
    ocrParseGeneration += 1;
    if (toolbarPeekTimer) clearTimeout(toolbarPeekTimer);
    if (verticalObserver) verticalObserver.disconnect();
    window.PudgeReadingTools?.closeAll?.();
    $('mangaReaderV2')?.classList.remove('open', 'toolbar-peek');
    document.body.classList.remove('manga-v2-reading');
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    void renderLibrary();
  }

  async function movePage(delta) {
    if (!currentBook || settings.mode === 'vertical') return;
    const step = settings.mode === 'double' ? 2 : 1;
    currentPage = Math.max(0, Math.min(currentPageCount - 1, currentPage + delta * step));
    await showCurrent();
  }

  async function ocrPage(pageIndex, {show = true, generation = ocrGeneration} = {}) {
    if (!currentBook) return null;
    const index = Math.max(0, Math.min(currentPageCount - 1, Number(pageIndex)));
    const progress = $('mangaV2OcrProgress');
    if (show && progress) progress.textContent = `${ru() ? 'OCR страницы' : 'OCR page'} ${index + 1}/${currentPageCount}…`;

    // Force the exact page identity through the same backend page lookup before OCR.
    // This avoids stale "first opened page" state in older reader flows.
    await getPage(index);
    const result = await API().manga_ocr_page(Number(currentBook.id), index);
    if (generation !== ocrGeneration) return null;

    const text = String(result?.text || result?.error || (ru() ? 'Текст не найден' : 'No text found'));
    // Plain OCR becomes selectable immediately. Jiten tokenization upgrades it in
    // the background and never blocks selection/translation.
    storeOcrText(index, text, {parse: show && currentPage === index});
    if (show && progress) progress.textContent = `${ru() ? 'OCR готов' : 'OCR ready'} · ${index + 1}/${currentPageCount}`;
    return result;
  }

  async function ocrWholeBook() {
    if (!currentBook) return;
    const generation = ++ocrGeneration;
    const progress = $('mangaV2OcrProgress');
    const button = document.querySelector('[data-manga-v2-action="ocr-book"]');
    button?.classList.add('busy');
    try {
      for (let index = 0; index < currentPageCount; index++) {
        if (generation !== ocrGeneration) break;
        if (progress) {
          const pct = Math.round(index / Math.max(1, currentPageCount) * 100);
          progress.innerHTML = `<span>${ru() ? 'OCR тома' : 'OCR volume'} ${index + 1}/${currentPageCount}</span><i style="--ocr-progress:${pct}%"></i>`;
        }
        try {
          await ocrPage(index, {show: false, generation});
        } catch (error) {
          ocrCache.set(ocrKey(currentBook.id, index), String(error?.message || error));
        }
      }
      if (generation === ocrGeneration && progress) {
        progress.innerHTML = `<span>${ru() ? 'OCR тома готов' : 'Volume OCR ready'} · ${currentPageCount}/${currentPageCount}</span><i style="--ocr-progress:100%"></i>`;
        updateOcrPanel(currentPage);
        const text = ocrCache.get(ocrKey(currentBook.id, currentPage));
        if (text) void parseOcrStudyText(currentPage, text);
      }
    } finally {
      button?.classList.remove('busy');
    }
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

  document.addEventListener('pointerover', event => {
    const region = event.target.closest?.('.manga-v2-text-region');
    if (region && !region.contains(event.relatedTarget)) activateTextRegion(region);
  }, true);

  document.addEventListener('pointerout', event => {
    const region = event.target.closest?.('.manga-v2-text-region');
    if (region && !region.contains(event.relatedTarget)) deactivateTextRegion(region);
  }, true);

  document.addEventListener('focusin', event => {
    const region = event.target.closest?.('.manga-v2-text-region');
    if (region) activateTextRegion(region);
  }, true);

  document.addEventListener('click', async event => {
    const textRegion = event.target.closest?.('.manga-v2-text-region');
    if (textRegion) {
      activateTextRegion(textRegion);
      return;
    }
    const nav = event.target.closest?.('.nav button[data-page]');
    if (nav?.dataset.page === 'manga') setTimeout(() => void renderLibrary(), 80);

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
    else if (type === 'ocr-page') await loadTextRegions(currentPage, {refresh:true, showProgress:true});
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
    } else if (event.key.toLowerCase() === 'o') {
      event.preventDefault();
      void loadTextRegions(currentPage, {refresh:true, showProgress:true});
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
    openBook,
    settings: () => ({...settings})
  };
})();
