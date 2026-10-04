'use strict';
(() => {
  const g = globalThis;
  const api = () => g.pywebview?.api || g.window?.pywebview?.api;
  const ru = () => String(g.ui?.lang || document.documentElement.lang || 'en').startsWith('ru');
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const AUDIO_KEY = 'pudge.sidebarAudio.last.v1';
  const REVIEW_AT = 'pudge.sidebarReview.lastAt.v1';
  const REVIEW_INTERVAL = 'pudge.sidebarReview.interval.v1';
  const PREFETCH_LOW_WATER = 7;
  const PREFETCH_TARGET = 12;
  const AUDIO_SPEEDS = [.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  const UNDO_WINDOW_MS = 10_000;
  const floating = Boolean(g.PudgeAudioFloat);
  const energySaving = () => document.documentElement.classList.contains('energy-saving');
  const liveTextAllowed = () => !energySaving() && !document.hidden && (floating || !audioState?.float_open);
  const chapterIndexes = new WeakMap();

  let root = null;
  let audioState = null;
  let audioTimer = null;
  let liveTimer = null;
  let liveShellKey = '';
  let liveTextSignature = '';
  let liveFollowFrozen = false;
  let liveBusy = false;
  let audioBusy = false;
  let audioGeneration = 0;
  let startingBook = null;
  let liveFrame = null;
  let livePaintAt = 0;
  let liveSnapshot = null;
  const liveClock = g.PudgePairedAudioClock?.createClock();
  let lastAudioId = Number(localStorage.getItem(AUDIO_KEY) || 0);
  let chapterHover = null;
  let chapterHoverGeneration = 0;
  let floatDisplayId = 0;
  const chapterCache = new Map();

  let dueStatus = 'loading';
  let dueError = '';
  let retryAt = 0;
  let dueCard = null;
  let dueRevealed = false;
  let dueQueue = [];
  let dueTimer = null;
  let duePrefetchTimer = null;
  let dueBusy = false;
  let prefetchBusy = false;
  let prefetchPromise = null;
  let prefetchGeneration = -1;
  let queueGeneration = 0;
  let checkingDue = false;
  const pendingReviews = new Set();
  const recentReviews = new Map();
  let previousReview = null;
  let previousExpiryTimer = null;

  let savedInterval = String(localStorage.getItem(REVIEW_INTERVAL) || '180000');
  let settingsSave = Promise.resolve();
  const intervalValue = () => savedInterval;
  const intervalMs = () => intervalValue() === 'continuous' ? 0 : Math.max(60_000, Number(intervalValue()) || 180_000);
  const lastReviewAt = () => Number(localStorage.getItem(REVIEW_AT) || 0);

  function ensure() {
    if (root?.isConnected) return root;
    const bottom = document.querySelector('.sidebar-bottom');
    const side = bottom?.closest('.sidebar');
    if (!side) return null;
    root = document.createElement('section');
    root.id = 'pudgeSidebarCompanion';
    root.className = 'sidebar-companion';
    root.innerHTML = '<div data-sidebar-audio></div><div data-sidebar-due></div>';
    let content = side.querySelector('.sidebar-content');
    if (!content) {
      content = document.createElement('div');
      content.className = 'sidebar-content';
      for (const child of [...side.children]) {
        if (child !== bottom) content.appendChild(child);
      }
      side.insertBefore(content, bottom);
    }
    content.appendChild(root);
    return root;
  }

  const clock = value => {
    const n = Math.max(0, Number(value || 0));
    return `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2, '0')}`;
  };

  function activeBook() {
    const books = audioState?.books || [];
    // Sidebar activity is exclusive: an audiobook owns the slot only while its
    // player exists (playing or paused).  A merely remembered last book must not
    // hide due reviews forever.
    return books.find(book => book.playing)
      || books.find(book => book.player_running)
      || null;
  }

  function coverMarkup(book) {
    return book.cover_url
      ? `<img src="${esc(book.cover_url)}" alt="">`
      : '<span class="sidebar-audio-cover-fallback">♫</span>';
  }

  function audioShell(book) {
    const linked = Number(book.linked_light_novel?.id || 0);
    const key = `${Number(book.id)}:${linked}`;
    const host = ensure()?.querySelector('[data-sidebar-audio]');
    if (!host) return null;
    host.hidden = false;
    let article = host.querySelector('.sidebar-audio');
    if (!article || article.dataset.shellKey !== key) {
      host.innerHTML = `<article class="sidebar-audio ${linked ? 'with-ln' : ''}" data-sc-book="${Number(book.id)}" data-shell-key="${esc(key)}">
        ${!floating && api()?.audiobook_float_open ? `<button type="button" class="sidebar-audio-detach" data-sc-audio="float" title="${ru() ? 'Поверх всех окон' : 'Always on top'}" aria-label="${ru() ? 'Поверх всех окон' : 'Always on top'}">↗</button>` : ''}
        <div class="sidebar-audio-chapter-row">
          <button type="button" data-sc-audio="previous-chapter" aria-label="${ru() ? 'Предыдущая глава' : 'Previous chapter'}">‹</button>
          <button class="sidebar-audio-cover" type="button" data-sc-audio="open" title="${ru() ? 'Показать аудиокнигу' : 'Show audiobook'}">${coverMarkup(book)}</button>
          <button type="button" data-sc-audio="next-chapter" aria-label="${ru() ? 'Следующая глава' : 'Next chapter'}">›</button>
        </div>
        <div class="sidebar-audio-chapter" data-sc-chapter></div>
        <div class="sidebar-audio-actions">
          <button type="button" data-sc-audio="toggle" class="sidebar-audio-play" aria-label="Play/Pause"></button>
          <button type="button" data-sc-audio="seek" data-delta="-15" aria-label="${ru() ? 'Назад на 15 секунд' : 'Back 15 seconds'}" title="−15s">↶</button>
          <button type="button" data-sc-audio="seek" data-delta="15" aria-label="${ru() ? 'Вперёд на 15 секунд' : 'Forward 15 seconds'}" title="+15s">↷</button>
          <button type="button" data-sc-audio="speed" class="sidebar-audio-speed" aria-label="${ru() ? 'Скорость' : 'Speed'}" title="${ru() ? 'Скорость воспроизведения' : 'Playback speed'}"></button>
          <button type="button" data-sc-audio="bookmark" aria-label="Bookmark" title="Bookmark"><svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M6 3h12v18l-6-4-6 4V3Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg></button>
        </div>
        <div class="sidebar-audio-scrubber"><input class="sidebar-audio-timeline" data-sc-audio-timeline type="range" min="0" step="0.1" value="0" aria-label="${ru() ? 'Позиция' : 'Position'}"><span class="audiobook-chapter-hover" data-sc-chapter-hover aria-hidden="true"></span></div>
        <div class="sidebar-audio-time"><span data-sc-position></span><span data-sc-duration></span></div>
        ${linked ? '<div data-sc-live-text class="sidebar-ln-live" data-pudge-translate-root data-pudge-study-hover></div>' : ''}
      </article>`;
      article = host.querySelector('.sidebar-audio');
      liveShellKey = '';
      liveTextSignature = '';
      liveFollowFrozen = false;
      liveSnapshot = null;
      if (liveFrame != null) cancelAnimationFrame(liveFrame);
      liveFrame = null;
    }
    return article;
  }

  function updateAudio(book) {
    const article = audioShell(book);
    if (!article) return;
    article.classList.toggle('text-in-float', !floating && Boolean(audioState?.float_open));
    const toggle = article.querySelector('[data-sc-audio="toggle"]');
    if (toggle) {
      toggle.textContent = book.playing ? 'Ⅱ' : '▶';
      toggle.setAttribute('aria-label', book.playing ? (ru() ? 'Пауза' : 'Pause') : (ru() ? 'Воспроизвести' : 'Play'));
    }
    const speed = article.querySelector('[data-sc-audio="speed"]');
    if (speed) speed.textContent = `${Number(book.speed || 1)}×`;
    const chapters = book.chapters || [], chapter = chapters.find(row => Number(row.start || 0) <= Number(book.position || 0)
      && Number(book.position || 0) < Number(row.end ?? book.duration ?? Infinity)) || book.current_chapter || chapters[0];
    const chapterLabel = article.querySelector('[data-sc-chapter]');
    if (chapterLabel) chapterLabel.textContent = String(chapter?.title || (ru() ? 'Аудиокнига' : 'Audiobook'));
    const chapterIndex = chapters.indexOf(chapter);
    const nextButton = article.querySelector('[data-sc-audio="next-chapter"]');
    const previousButton = article.querySelector('[data-sc-audio="previous-chapter"]');
    if (nextButton) nextButton.disabled = chapterIndex < 0 || chapterIndex >= chapters.length - 1;
    if (previousButton) previousButton.disabled = !chapters.length;
    const timeline = article.querySelector('[data-sc-audio-timeline]');
    if (timeline && document.activeElement !== timeline) {
      timeline.max = String(Math.max(0.1, Number(book.duration || 0.1)));
      timeline.value = String(Math.max(0, Math.min(Number(book.duration || 0), Number(book.position || 0))));
    }
    const position = article.querySelector('[data-sc-position]');
    const duration = article.querySelector('[data-sc-duration]');
    if (position) position.textContent = clock(book.position);
    if (duration) duration.textContent = clock(book.duration);
    if (chapterHover && Number(chapterHover.audiobookId) !== Number(book.id)) chapterHover = null; // Book switched.
    paintChapterHover(article, book);
    if (book.linked_light_novel && liveTextAllowed()) scheduleLiveText(book, book.playing ? 30 : 900);
  }

  // Chapter hover from the main audiobook card: a non-interactive overlay on the
  // sidebar timeline.  Never touches transport, never rebuilds the shell.
  function paintChapterHover(article = root?.querySelector?.('.sidebar-audio'), book = activeBook()) {
    const marker = article?.querySelector?.('[data-sc-chapter-hover]');
    if (!marker) return false;
    const hide = () => { marker.classList.remove('show'); return false; };
    const hover = chapterHover;
    if (!hover || !book || !article.isConnected) return hide();
    if (Number(hover.audiobookId) !== Number(book.id) || Number(article.dataset.scBook) !== Number(book.id)) return hide();
    const duration = Number(book.duration);
    if (!Number.isFinite(duration) || duration <= 0) return hide();
    const start = Math.max(0, Math.min(duration, Number(hover.start)));
    const end = Math.max(0, Math.min(duration, Number(hover.end)));
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return hide();
    marker.style.left = `${start / duration * 100}%`;
    marker.style.width = `${(end - start) / duration * 100}%`;
    marker.classList.add('show');
    return true;
  }

  function showChapterHoverRange({audiobookId, start, end, source} = {}) {
    const id = Number(audiobookId);
    const s = Number(start), e = Number(end);
    if (!(id > 0) || !Number.isFinite(s) || !Number.isFinite(e)) { clearChapterHoverRange(); return false; }
    chapterHover = {audiobookId:id, start:s, end:e, source:source ?? null, generation:++chapterHoverGeneration};
    const shown = paintChapterHover();
    if (!shown && Number(activeBook()?.id) !== id) chapterHover = null; // Other book: no highlight, no state.
    return shown;
  }

  function clearChapterHoverRange(source) {
    // A late leave of an older chapter must not clear a newer hover.
    if (source != null && chapterHover && chapterHover.source !== source) return false;
    chapterHover = null;
    chapterHoverGeneration++;
    paintChapterHover();
    return true;
  }

  function renderAudio() {
    const host = ensure()?.querySelector('[data-sidebar-audio]');
    if (!host) return;
    const book = activeBook();
    if (!book) {
      host.hidden = true;
      chapterHover = null;
      if (host.childElementCount) host.innerHTML = '';
      clearTimeout(liveTimer);
      liveTextSignature = '';
      liveSnapshot = null;
      if (liveFrame != null) cancelAnimationFrame(liveFrame);
      liveFrame = null;
      renderDue();
      return;
    }
    updateAudio(book);
    reportFloatDisplay(book);
    renderDue();
  }

  function reportFloatDisplay(book) {
    // The native float tells the backend which audiobook it shows (on change only),
    // so a light-novel reader for that book can hide the duplicate surface.
    if (!floating || !book || Number(book.id) === floatDisplayId) return;
    floatDisplayId = Number(book.id);
    try { void api()?.audiobook_float_display?.(floatDisplayId)?.catch?.(() => {}); } catch (error) { console.debug('float display', error); }
  }

  async function pollAudio() {
    if (audioBusy) return;
    const book = activeBook(), paired = g.PudgeLnAudio?.current?.(Number(book?.linked_light_novel?.id || 0));
    if (paired?.audiobook_id && Number(paired.audiobook_id) === Number(book?.id)) {
      clearTimeout(audioTimer); audioTimer = setTimeout(pollAudio, 2000);
      return; // The open reader already publishes its authoritative playback samples.
    }
    audioBusy = true;
    const generation = audioGeneration;
    try {
      const state = await (api()?.sidebar_audio_state?.() ?? api()?.audiobook_state?.());
      if (generation !== audioGeneration) return;
      audioState = reconcileAudioState(state);
      const live = (audioState?.books || []).find(book => book.playing || book.player_running);
      if (live) {
        lastAudioId = Number(live.id);
        localStorage.setItem(AUDIO_KEY, String(lastAudioId));
      }
      renderAudio();
    } catch (error) {
      console.debug('sidebar audiobook', error);
    } finally {
      audioBusy = false;
      clearTimeout(audioTimer);
      audioTimer = setTimeout(pollAudio, generation !== audioGeneration ? 40 : energySaving() || document.hidden ? 5000 : activeBook()?.playing ? 1000 : 2000);
    }
  }

  function reconcileAudioState(state) {
    const books = (state?.books || []).map(book => ({...book}));
    if (startingBook) {
      const confirmed = books.find(book => Number(book.id) === Number(startingBook.book.id)
        && (book.playing || book.player_running));
      if (confirmed || Date.now() >= startingBook.until) startingBook = null;
      else return {...state, books:[startingBook.book, ...books.filter(book => Number(book.id) !== Number(startingBook.book.id))]};
    }
    return {...state, books};
  }

  function acceptAudioState(state, options = {}) {
    if (!Array.isArray(state?.books)) return;
    audioGeneration += 1;
    if (options.rollback || options.stop) startingBook = null;
    if (options.optimistic) {
      const book = state.books.find(item => item.playing);
      if (book) startingBook = {book:{...book}, until:Date.now() + 15_000};
      audioState = {books:state.books.map(item => ({...item}))};
    } else audioState = reconcileAudioState(state);
    renderAudio();
  }

  function acceptPairedState(state, lnBook) {
    const id = Number(state?.audiobook_id || lnBook?.paired_audio?.book?.id || 0);
    if (!id) return;
    const old = (audioState?.books || []).find(book => Number(book.id) === id)
      || lnBook?.paired_audio?.book || {};
    const book = {...old, id, ...state, linked_light_novel:old.linked_light_novel || {id:Number(lnBook.id), title:lnBook.title}};
    acceptAudioState({books:[book]});
    if (liveSnapshot?.bookId === id) {
      liveSnapshot.state = state;
      liveClock?.reconcile(state);
    }
  }

  function audioTextLength(value) {
    return [...String(value || '').normalize('NFKC').toLocaleLowerCase().replace(/[\s\p{P}\p{Z}]/gu, '')].length;
  }

  function isImageParagraph(value) {
    return /^\[\[PUDGE_LN_IMAGE_URL:.+?\]\]$/u.test(String(value || '').trim());
  }

  function paragraphAudioLength(value) {
    return isImageParagraph(value) ? 0 : audioTextLength(value);
  }

  function chapterIndex(payload) {
    if (chapterIndexes.has(payload)) return chapterIndexes.get(payload);
    let total = 0;
    const rows = (payload?.paragraphs || []).map((paragraph, index) => {
      const text = String(paragraph || ''), prefix = new Float64Array(text.length + 1);
      let raw = 0, count = 0;
      const image = isImageParagraph(text);
      const parts = /[\u3099\u309a\uff9e\uff9f]/u.test(text) && typeof Intl.Segmenter === 'function'
        ? [...new Intl.Segmenter('ja', {granularity:'grapheme'}).segment(text)].map(row => row.segment) : text;
      for (const char of parts) {
        const width = image ? 0 : audioTextLength(char);
        for (let j = 1; j <= char.length; j++) prefix[raw + j] = count + (j === char.length ? width : 0);
        raw += char.length; count += width;
      }
      let tokens = [...((payload.tokens || [])[index] || [])].sort((a, b) => Number(a.start) - Number(b.start));
      if (!tokens.length) tokens = [...text.matchAll(/[\p{L}\p{N}\p{M}ー]+/gu)].flatMap(match => {
        const chars = [...match[0]], groups = []; let start = match.index;
        for (let i = 0; i < chars.length; i += 6) {
          const part = chars.slice(i, i + 6).join('');
          groups.push({start, end:start + part.length}); start += part.length;
        }
        return groups;
      });
      const row = {text, prefix, tokens, start:total, length:count}; total += count;
      return row;
    });
    const result = {rows, total}; chapterIndexes.set(payload, result); return result;
  }

  function rawIndexAtAudioOffset(text, audioOffset) {
    const target = Math.max(0, Number(audioOffset || 0));
    let normalized = 0, raw = 0;
    for (const character of String(text || '')) {
      const width = audioTextLength(character);
      if (width > 0 && normalized + width > target) return raw;
      normalized += width;
      raw += character.length;
    }
    return String(text || '').length;
  }

  function paragraphPosition(payload, state) {
    const paragraphs = payload?.paragraphs || [];
    if (!paragraphs.length) return {index:0, local:0, localAudio:0, target:0};
    // Paired-audio offsets use normalize_reading_text(), the same clock as the
    // main LN reader's lnAudioTextLength: whitespace/punctuation are zero-width.
    const indexed = chapterIndex(payload), lengths = indexed.rows.map(row => row.length), total = indexed.total;
    let target = state?.chapter_char_offset_exact == null ? NaN : Number(state.chapter_char_offset_exact);
    if (!Number.isFinite(target)) target = state?.chapter_char_offset == null ? NaN : Number(state.chapter_char_offset);
    if (!Number.isFinite(target)) target = Math.max(0, Math.min(total, Number(state?.chapter_progress || 0) * total));
    let cursor = 0;
    for (let index = 0; index < lengths.length; index += 1) {
      const length = lengths[index];
      if (length > 0 && target < cursor + length) {
        const localAudio = Math.max(0, target - cursor);
        const prefix = indexed.rows[index].prefix;
        let low = 0, high = prefix.length - 1;
        while (low < high) { const mid = (low + high) >> 1; if (prefix[mid + 1] > localAudio) high = mid; else low = mid + 1; }
        return {index, local:low, localAudio, target};
      }
      cursor += length;
    }
    for (let index = paragraphs.length - 1; index >= 0; index -= 1) {
      if (lengths[index] <= 0) continue;
      return {index, local:String(paragraphs[index] || '').length, localAudio:lengths[index], target};
    }
    return {index:0, local:0, localAudio:0, target};
  }

  function liveWindow(payload, position) {
    const index = Math.max(0, Math.min((payload?.paragraphs || []).length - 1, Number(position?.index || 0)));
    const text = String((payload?.paragraphs || [])[index] || '');
    // Keep roughly five sidebar lines around the spoken position.  Moving the
    // crop in small buckets makes old text disappear naturally without replacing
    // the whole audiobook card on every player-state tick.
    const local = Math.max(0, Math.min(text.length, Number(position?.local || 0)));
    let cropStart = Math.max(0, Math.floor(Math.max(0, local - 32) / 384) * 384);
    let cropEnd = Math.min(text.length, cropStart + 512);
    const indexed = chapterIndex(payload).rows[index];
    let sourceTokens = indexed.tokens;
    for (const token of sourceTokens) {
      const start = Number(token?.start), end = Number(token?.end ?? (start + Number(token?.length || 0)));
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || start < 0 || end > text.length) continue;
      if (start < cropStart && end > cropStart) cropStart = start;
      if (start < cropEnd && end > cropEnd) cropEnd = end;
    }
    let previousEnd = cropStart;
    const tokens = sourceTokens
      .map(token => {
        const start = Number(token?.start || 0), end = Number(token?.end ?? (start + Number(token?.length || 0)));
        if (!Number.isFinite(start) || !Number.isFinite(end) || start < previousEnd || end > cropEnd || end <= start) return null;
        previousEnd = end;
        const rubies = (Array.isArray(token?.rubies) ? token.rubies : []).map(ruby => {
          const rubyStart = Number(ruby?.start), rubyEnd = Number(ruby?.end ?? (rubyStart + Number(ruby?.length || 0)));
          return {...ruby, start:rubyStart - cropStart, end:rubyEnd - cropStart};
        });
        return {...token, start:start - cropStart, end:end - cropStart, rubies};
      })
      .filter(Boolean);
    let activeTokenIndex = -1;
    let activeTokenProgress = 0;
    for (let i = 0; i < tokens.length; i += 1) {
      const start = indexed.prefix[cropStart + Number(tokens[i].start)];
      const end = indexed.prefix[cropStart + Number(tokens[i].end)];
      if (position.localAudio >= start && position.localAudio < end && end > start) {
        activeTokenIndex = i;
        activeTokenProgress = Math.max(0, Math.min(100, (position.localAudio - start) / (end - start) * 100));
        break;
      }
    }
    return {
      payload:{...payload, paragraphs:[text.slice(cropStart, cropEnd)], tokens:[tokens]},
      cropStart, cropEnd, index, activeTokenIndex, activeTokenProgress,
    };
  }

  function updateLiveHighlight(host, windowed) {
    const words = host._scWords || (host._scWords = [...host.querySelectorAll('.pudge-study-word')]);
    const active = words[Number(windowed?.activeTokenIndex ?? -1)];
    if (host._scActive && host._scActive !== active) {
      host._scActive.classList.remove('ln-paired-word-current');
      host._scActive.style.removeProperty('--ln-paired-word-progress');
    }
    host._scActive = active;
    if (!active) return;
    active.classList.add('ln-paired-word-current');
    active.style.setProperty('--ln-paired-word-progress', `${Number(windowed.activeTokenProgress || 0).toFixed(1)}%`);
    const now = performance.now();
    if (now - Number(host._scScrollAt || 0) >= 250) {
      host._scScrollAt = now;
      const rect = active.getBoundingClientRect(), bounds = host.getBoundingClientRect();
      if (rect.top < bounds.top + 15 || rect.bottom > bounds.bottom - 20) {
        host.scrollTo({top:Math.max(0, host.scrollTop + rect.top - bounds.top - bounds.height * .35), behavior:'smooth'});
      }
    }
  }

  function paintLiveText(host, book, payload, state) {
    const position = paragraphPosition(payload, state), windowed = liveWindow(payload, position);
    const key = `${Number(book.linked_light_novel.id)}:${Number(state.ln_chapter_index || 0)}`;
    const signature = `${key}:${windowed.index}:${windowed.cropStart}:${windowed.cropEnd}`;
    if (signature !== liveTextSignature || liveShellKey !== key) {
      const renderer = g.PudgeReadingTools?.study?.renderParsedText;
      host.dataset.pudgeMediaId = String(book.linked_light_novel?.anilist_id || '');
      host.innerHTML = renderer ? renderer(windowed.payload, {
        backend:String(g.ui?.lnState?.settings?.study_backend || 'jiten'), highlightOptimalWords:false,
        mediaContext:{kind:'light_novel', book_id:Number(book.linked_light_novel.id)},
      }) : `<p class="current">${esc(windowed.payload.paragraphs[0] || '')}</p>`;
      host.querySelector('p')?.classList.add('current');
      host._scWords = null; host._scActive = null;
      host.scrollTop = 0;
      liveTextSignature = signature; liveShellKey = key;
    }
    updateLiveHighlight(host, windowed);
  }

  function startLiveFrames() {
    if (liveFrame != null || !liveSnapshot?.state.playing || !liveClock || !liveTextAllowed()) return;
    const frame = () => {
      liveFrame = null;
      const snapshot = liveSnapshot, book = activeBook();
      if (!snapshot || !book?.playing || !snapshot.host.isConnected || liveFollowFrozen || !liveTextAllowed()
        || g.PudgeLnAudio?.current?.(Number(book.linked_light_novel?.id))) return;
      const now = performance.now();
      if (now - livePaintAt >= 33) {
        livePaintAt = now;
        const offset = g.PudgePairedAudioClock.offsetAtTime(snapshot.state, liveClock.current());
        paintLiveText(snapshot.host, book, snapshot.payload, {...snapshot.state, chapter_char_offset_exact:offset});
      }
      liveFrame = requestAnimationFrame(frame);
    };
    liveFrame = requestAnimationFrame(frame);
  }

  function scheduleLiveText(book, delay = 650) {
    clearTimeout(liveTimer);
    if (!book?.linked_light_novel || !liveTextAllowed()) return;
    if (g.PudgeLnAudio?.current?.(Number(book.linked_light_novel.id))) return;
    liveTimer = setTimeout(() => void refreshLiveText(activeBook()), delay);
  }

  async function refreshLiveText(book) {
    if (!book?.linked_light_novel || liveBusy || !liveTextAllowed()) return;
    const lnId = Number(book.linked_light_novel?.id || 0);
    const host = root?.querySelector(`[data-sc-book="${Number(book.id)}"] [data-sc-live-text]`);
    if (!lnId || !host) return;
    if (liveFollowFrozen) {
      scheduleLiveText(book, 800);
      return;
    }
    liveBusy = true;
    try {
      const readerState = g.PudgeLnAudio?.current?.(lnId);
      const state = readerState || await api().light_novel_paired_state(lnId);
      const chapter = Number(state.ln_chapter_index || 0);
      const key = `${lnId}:${chapter}`;
      let payload = chapterCache.get(key);
      if (!payload) {
        payload = await api().light_novel_chapter(lnId, chapter);
        chapterCache.set(key, payload);
        if (chapterCache.size > 8) chapterCache.delete(chapterCache.keys().next().value);
      }
      if (!host.isConnected || liveFollowFrozen || !liveTextAllowed()) return;
      liveClock?.reconcile(state);
      liveSnapshot = {bookId:Number(book.id), host, state, payload};
      paintLiveText(host, book, payload, {...state, chapter_char_offset_exact:g.PudgePairedAudioClock?.offsetAtTime(state, liveClock?.current()) ?? state.chapter_char_offset_exact});
      startLiveFrames();
    } catch (error) {
      console.debug('sidebar live text', error);
    } finally {
      liveBusy = false;
      const live = activeBook();
      scheduleLiveText(live, live?.playing ? 500 : 1400);
    }
  }

  async function audioClick(action, target) {
    const book = activeBook();
    if (!book) return;
    if (action === 'float') {
      const result = await api()?.audiobook_float_open?.(Number(book.id));
      if (result?.ok === false) throw new Error(result.error || 'Could not open floating audiobook');
      void pollAudio();
      return;
    }
    if (floating && (action === 'open' || action === 'read')) {
      await api().audiobook_float_show_main(action, Number(book.id));
      return;
    }
    if (action === 'open') {
      g.setPage?.('audiobooks');
      await g.PudgeMedia?.loadAudio?.();
      requestAnimationFrame(() => document.querySelector(`[data-audiobook-id="${Number(book.id)}"]`)?.scrollIntoView({block:'center'}));
      return;
    }
    if (action === 'read') {
      const lnId = Number(book.linked_light_novel?.id || 0);
      if (!lnId) return;
      const snapshot = liveSnapshot?.bookId === Number(book.id) ? liveSnapshot : null;
      const paired = snapshot ? {...snapshot.state, position:liveClock?.current() ?? snapshot.state.position} : null;
      await g.openLightNovel?.(lnId, {readTogether:true, audiobookId:Number(book.id),
        bookPromise:api()?.sidebar_reader_book?.(lnId), audioBook:book, pairedState:paired, pairedAt:performance.now(), chapterPayload:snapshot?.payload});
      return;
    }
    if (action === 'stop') {
      const previous = audioState;
      acceptAudioState({books:[]}, {stop:true});
      g.PudgeLnAudio?.stopped?.(Number(book.id));
      try { await api().audiobook_stop(Number(book.id)); }
      catch (error) { acceptAudioState(previous); throw error; }
      void pollAudio();
      return;
    }
    if (action === 'previous-chapter' || action === 'next-chapter') {
      const chapters = [...(book.chapters || [])].sort((a, b) => Number(a.start) - Number(b.start));
      const position = Number(liveSnapshot?.bookId === Number(book.id) ? liveClock?.current() ?? book.position : book.position || 0);
      let index = chapters.findLastIndex(chapter => Number(chapter.start || 0) <= position);
      if (index < 0 || !chapters.length) return;
      if (action === 'next-chapter') index = Math.min(chapters.length - 1, index + 1);
      else if (position - Number(chapters[index].start || 0) <= 5) index = Math.max(0, index - 1);
      const result = await api().audiobook_seek_to(Number(book.id), Number(chapters[index].start || 0));
      if (result?.book) acceptAudioState({books:[result.book]});
      liveClock?.reset({...book, position:Number(chapters[index].start || 0)});
      liveTextSignature = ''; liveSnapshot = null;
      void refreshLiveText(activeBook());
      return;
    }
    if (action === 'toggle') {
      const resuming = book.player_running && !book.playing;
      if (book.player_running) await api().audiobook_set_paused(Number(book.id), !!book.playing);
      else await api().audiobook_play(Number(book.id), null, Number(book.speed || 1));
      if (resuming || !book.player_running) {
        liveFollowFrozen = false;
        liveTextSignature = '';
      }
    } else if (action === 'speed') {
      showSpeedMenu(target, book);
      return;
    } else if (action === 'seek') {
      await api().audiobook_seek(Number(book.id), Number(target.dataset.delta || 0));
      liveTextSignature = '';
    } else if (action === 'bookmark') {
      await api().audiobook_add_bookmark(Number(book.id), '');
    }
    audioState = await (api().sidebar_audio_state?.() ?? api().audiobook_state());
    renderAudio();
  }

  function reviewActions() {
    return g.PudgeReviewActions?.actionSet?.('jiten') || {actions:[
      {id:'again',tone:'again'}, {id:'hard',tone:'hard'}, {id:'good',tone:'good'}, {id:'easy',tone:'easy'},
    ]};
  }

  function selectedReading(card) {
    const rows = Array.isArray(card?.readings) ? card.readings : [];
    const wanted = Number(card?.readingIndex ?? -1);
    return rows.find(row => Number(row?.readingIndex ?? -2) === wanted) || rows[0] || null;
  }

  function readingText(card) {
    if (g.PudgeJitenWords) return g.PudgeJitenWords.readingsLine(card);
    const rows = Array.isArray(card?.readings) ? card.readings : [];
    const primary = kanaReading(card);
    const surface = String(card?.wordTextPlain || '').trim();
    return [...new Set(rows.map(row => readingFromRuby(row?.rubyText) || String(row?.text || '').trim())
      .filter(value => value && value !== primary && value !== surface))].join(' ・ ');
  }

  function rubyHtml(value) {
    const text = String(value || '');
    if (!text.includes('[')) return esc(text);
    let out = '';
    let pending = '';
    for (let i = 0; i < text.length; i += 1) {
      if (text[i] !== '[') { pending += text[i]; continue; }
      const close = text.indexOf(']', i + 1);
      if (close < 0) { pending += text.slice(i); break; }
      const reading = text.slice(i + 1, close);
      const match = pending.match(/([0-9０-９]+|[\u3400-\u9fff々]+)$/u);
      if (!match || !reading) { pending += text.slice(i, close + 1); i = close; continue; }
      const base = match[0];
      out += esc(pending.slice(0, pending.length - base.length)) + `<ruby>${esc(base)}<rt>${esc(reading)}</rt></ruby>`;
      pending = '';
      i = close;
    }
    return out + esc(pending);
  }

  function readingFromRuby(value) {
    const text = String(value || '');
    if (!text.includes('[')) return '';
    let out = '', pending = '';
    for (let i = 0; i < text.length; i += 1) {
      if (text[i] !== '[') { pending += text[i]; continue; }
      const close = text.indexOf(']', i + 1);
      if (close < 0) { pending += text.slice(i); break; }
      const reading = text.slice(i + 1, close);
      const match = pending.match(/([0-9０-９]+|[\u3400-\u9fff々]+)$/u);
      if (!match || !reading) { pending += text.slice(i, close + 1); i = close; continue; }
      out += pending.slice(0, pending.length - match[0].length) + reading;
      pending = '';
      i = close;
    }
    out += pending;
    return out;
  }

  function kanaReading(card) {
    const primary = selectedReading(card);
    const candidates = [];
    const ruby = String(primary?.rubyText || '').trim();
    if (ruby) candidates.push(readingFromRuby(ruby));
    candidates.push(String(primary?.text || '').trim());
    for (const row of (Array.isArray(card?.readings) ? card.readings : [])) {
      const rowRuby = String(row?.rubyText || '').trim();
      if (rowRuby) candidates.push(readingFromRuby(rowRuby));
      candidates.push(String(row?.text || '').trim());
    }
    candidates.push(String(card?.reading || '').trim());
    return candidates.find(value => value && /^[ぁ-ゟ゠-ヿー・]+$/u.test(value.replace(/\s+/g, ''))) || '';
  }

  function frontRubySource(card) {
    const primary = selectedReading(card);
    const ruby = String(primary?.rubyText || '').trim();
    return ruby && ruby.includes('[') ? ruby : '';
  }

  function secondsText(raw) {
    const seconds = Number(raw || 0);
    if (!Number.isFinite(seconds) || seconds <= 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
  }

  function confusableReadings(card) {
    return (Array.isArray(card?.confusableReadings) ? card.confusableReadings : [])
      .map(value => String(value || '').trim())
      .filter(Boolean)
      .filter((value, index, values) => values.indexOf(value) === index)
      .slice(0, 5);
  }

  function meanings(card) {
    const out = [];
    for (const definition of (card?.definitions || [])) {
      for (const meaning of (definition?.meanings || [])) {
        const text = String(meaning || '').trim();
        if (text && !out.includes(text)) out.push(text);
        if (out.length >= 8) return out;
      }
    }
    return out;
  }

  function wordLines(word) {
    const chars = [...String(word || '')];
    if (chars.length <= 8) return [chars.join('')];
    const lineCount = Math.ceil(chars.length / 8);
    const perLine = Math.ceil(chars.length / lineCount);
    const lines = [];
    for (let i = 0; i < chars.length; i += perLine) lines.push(chars.slice(i, i + perLine).join(''));
    return lines;
  }

  function frontWordHtml(card, revealReading = false) {
    const word = String(card?.wordTextPlain || card?.wordText || selectedReading(card)?.text || '').trim();
    const lines = wordLines(word);
    const widest = Math.max(1, ...lines.map(line => [...line].length));
    const reading = kanaReading(card);
    const pitch = g.PudgeReadingTools?.study?.inlinePitch?.({reading, pitchAccents:card?.pitchAccents || []}) || '';
    const showAnnotation = /[\u3400-\u9fff々]/u.test(word) || Boolean(pitch);
    const annotation = pitch || esc(reading);
    const base = lines.map(line => `<span class="sidebar-due-base-line">${esc(line)}</span>`).join('');
    // Reserve the reading above the same base from the first render, including
    // multiline words and cards that supply plain kana rather than rubyText.
    const markup = reading && showAnnotation
      ? `<span class="sidebar-due-front-ruby ${revealReading ? 'show-reading' : 'hide-reading'}"><span class="sidebar-due-front-reading" aria-hidden="${!revealReading}">${annotation}</span><span class="sidebar-due-front-base">${base}</span></span>`
      : base;
    const url = g.PudgeJitenWords?.url?.(card) || '';
    const attributes = `class="sidebar-due-word" data-char-width="${Math.min(8, widest)}" style="--sc-word-width:${Math.min(8, widest)};--sc-reading-width:${Math.max(1,[...reading].length)}"`;
    return url
      ? `<a ${attributes} href="${esc(url)}" data-sc-jiten-word="${Number(card.wordId)}" data-sc-jiten-reading="${Number(card.readingIndex)}" aria-label="${esc((ru() ? 'Открыть в Jiten: ' : 'Open in Jiten: ') + word)}">${markup}</a>`
      : `<div ${attributes}>${markup}</div>`;
  }

  function answerDetails(card, {compact = false} = {}) {
    const primary = selectedReading(card);
    const word = String(card?.wordTextPlain || card?.wordText || primary?.text || '').trim();
    const defs = meanings(card);
    const example = String(card?.exampleSentence?.text || card?.pudgeContextSentence || '').replaceAll('**', '').trim();
    const contextImage = String(card?.pudgeContextImage || '').trim();
    const confusable = confusableReadings(card);
    const source = String(card?.sourceDeckName || '').trim();
    const occurrences = (Array.isArray(card?.deckOccurrences) ? card.deckOccurrences : [])
      .slice(0, 3)
      .map(row => String(row?.originalTitle || row?.englishTitle || row?.romajiTitle || '').trim())
      .filter(Boolean);
    const alternate = readingText(card);
    return `<div class="sidebar-due-answer ${compact ? 'compact' : ''}">
      ${alternate ? `<div class="sidebar-due-readings">${esc(alternate)}</div>` : ''}
      ${!compact && confusable.length ? `<div class="sidebar-due-confusable"><small>${ru() ? 'Похожие чтения' : 'Confusable readings'}</small>${esc(confusable.join(' ・ '))}</div>` : ''}
      ${!compact && contextImage ? `<div class="sidebar-due-image"><img src="${esc(contextImage)}" alt="" loading="lazy"></div>` : ''}
      ${defs.length ? `<ol class="sidebar-due-meanings">${defs.slice(0, compact ? 3 : 8).map(item => `<li>${esc(item)}</li>`).join('')}</ol>` : '<div>—</div>'}
      ${!compact && example ? `<div class="sidebar-due-example"><small>${ru() ? 'Пример' : 'Example'}</small>${esc(example)}</div>` : ''}
      ${!compact && (source || occurrences.length) ? `<div class="sidebar-due-source"><small>${ru() ? 'Источник' : 'Source'}</small>${esc(source || occurrences.join(' · '))}</div>` : ''}
    </div>`;
  }

  function gradeButtonsHtml(card) {
    const actions = reviewActions().actions || [];
    const preview = card?.intervalPreview || {};
    return `<div class="sidebar-due-grades">${actions.map(action => {
      const interval = secondsText(preview?.[`${action.wire}Seconds`]);
      const label = esc(g.PudgeReviewActions?.label?.(action) || action.id);
      return `<button type="button" data-sc-grade="${esc(action.id)}" class="grade-${esc(action.tone || action.id)}"${dueBusy ? ' disabled' : ''}${action.shortcut ? ` title="${esc(action.shortcut)}"` : ''}><span>${label}</span>${interval ? `<small>${esc(interval)}</small>` : ''}</button>`;
    }).join('')}</div>`;
  }

  function previousReviewHtml() {
    if (!previousReview) return '';
    const remaining = Math.max(0, UNDO_WINDOW_MS - (Date.now() - Number(previousReview.reviewedAt || 0)));
    const action = previousReview.actions?.actions?.find(row => row.id === previousReview.grade);
    const label = g.PudgeReviewActions?.label?.(action || {}) || previousReview.grade || '';
    return `<article class="sidebar-due-previous">
      ${frontWordHtml(previousReview.card, true)}
      <div class="sidebar-due-previous-actions"><span class="sidebar-due-last-grade grade-${esc(action?.tone || previousReview.grade || '')}">${esc(label)}</span>
      <button type="button" data-sc-undo ${(previousReview.pending || remaining > 0) && !previousReview.undoRequested ? '' : 'disabled'}>${ru() ? 'Отменить' : 'Undo'}</button></div>
    </article>`;
  }

  function renderDue() {
    const host = ensure()?.querySelector('[data-sidebar-due]');
    if (!host) return;
    if (activeBook() && !audioState?.float_open) { host.hidden = true; return; }
    if (!dueCard && !previousReview) {
      host.hidden = false;
      const messages = {loading:ru()?'Загрузка карточек…':'Loading cards…', empty:ru()?'Нет карточек для повторения':'No cards due', error:ru()?'Не удалось загрузить карточки':'Could not load cards', interval:ru()?'До следующего повторения':'Waiting for next review', unconfigured:ru()?'Настройте Jiten для повторений':'Configure Jiten to review'};
      host.innerHTML = `<div class="sidebar-due-label">Jiten · ${esc(messages[dueStatus] || messages.loading)}</div>${dueStatus==='error'?`<button type="button" data-sc-retry>${ru()?'Повторить':'Retry'}</button>`:''}`;
      host.dataset.renderHtml = '';
      return;
    }
    host.hidden = false;
    const current = dueCard ? `<article class="sidebar-due-card">
      <div class="sidebar-due-label">Jiten · due</div>
      ${frontWordHtml(dueCard, dueRevealed)}
      ${dueRevealed ? `${answerDetails(dueCard)}${gradeButtonsHtml(dueCard)}` : `<button type="button" data-sc-reveal>${ru() ? 'Показать ответ' : 'Show answer'}</button>`}
    </article>` : '';
    const html = `${previousReviewHtml()}${current}`;
    const changed = host.dataset.renderHtml !== html;
    if (changed) {
      host.innerHTML = html;
      host.dataset.renderHtml = html;
    }
    if (dueCard) g.PudgeJitenWords?.hydrateReadings?.(dueCard, host, ".sidebar-due-answer:not(.compact)", "sidebar-due-readings", () => dueCard);
    if (changed && dueCard && !energySaving()) void g.PudgeJitenWords?.prefetch?.([dueCard, ...dueQueue]);
  }

  function schedulePreviousExpiry() {
    clearTimeout(previousExpiryTimer);
    if (!previousReview) return;
    const left = UNDO_WINDOW_MS - (Date.now() - Number(previousReview.reviewedAt || 0));
    if (left <= 0) { renderDue(); return; }
    previousExpiryTimer = setTimeout(() => renderDue(), left + 50);
  }

  function scheduleDue(delay = null) {
    clearTimeout(dueTimer);
    if (floating) return;
    const wait = delay == null
      ? Math.max(1000, intervalMs() - (Date.now() - lastReviewAt()))
      : delay;
    dueTimer = setTimeout(() => void checkDue(), wait);
  }

  const cardKey = card => `${Number(card?.wordId || 0)}:${Number(card?.readingIndex ?? -1)}`;

  function excludedReviewKeys(includeQueue = true) {
    for (const [key, reviewedAt] of recentReviews) {
      if (Date.now() - reviewedAt > 60_000) recentReviews.delete(key);
    }
    const keys = new Set([...pendingReviews, ...recentReviews.keys()]);
    if (dueCard) keys.add(cardKey(dueCard));
    if (includeQueue) for (const card of dueQueue) keys.add(cardKey(card));
    return keys;
  }

  function prefetchDueQueue(force = false) {
    if (floating || document.hidden || (!force && energySaving())) return Promise.resolve();
    if (!force && Date.now() < retryAt) return Promise.resolve();
    if (!force && dueQueue.length >= PREFETCH_LOW_WATER) return Promise.resolve();
    if (prefetchPromise) {
      return prefetchGeneration === queueGeneration ? prefetchPromise
        : prefetchPromise.then(() => prefetchDueQueue(force));
    }
    const generation = queueGeneration;
    prefetchGeneration = generation;
    prefetchBusy = true;
    if (!dueCard && !dueQueue.length) { dueStatus = 'loading'; renderDue(); }
    prefetchPromise = (async () => {
      try {
        const endpoint = api()?.sidebar_due_review_cards;
        let result;
        if (endpoint) result = await endpoint(energySaving() ? 1 : PREFETCH_TARGET, [...excludedReviewKeys()]);
        else {
          const one = await api()?.sidebar_due_review_card?.();
          if (!one || !Object.prototype.hasOwnProperty.call(one, 'card')) throw new Error('Jiten response unavailable');
          result = {...one, cards: one.card ? [one.card] : []};
        }
        if (generation !== queueGeneration) return;
        if (result?.ok === false) throw new Error(result.error || result.message || 'Jiten unavailable');
        if (!result || !Array.isArray(result.cards)) throw new Error('Jiten response unavailable');
        dueStatus = result.configured === false ? 'unconfigured' : (result.cards.length ? 'ready' : 'empty');
        dueError = ''; retryAt = 0;
        const incoming = (result?.cards || []).filter(card => card && typeof card === 'object');
        const existing = excludedReviewKeys();
        for (const card of incoming) {
          const key = cardKey(card);
          if (Number(card.wordId) > 0 && Number(card.readingIndex) >= 0 && !existing.has(key)) {
            existing.add(key); dueQueue.push(card);
          }
          if (dueQueue.length >= PREFETCH_TARGET) break;
        }
      } catch (error) {
        if (generation !== queueGeneration) return;
        dueStatus = 'error'; dueError = String(error?.message || error); retryAt = Date.now() + 60_000; renderDue();
        console.debug('sidebar due prefetch', error);
      } finally {
        prefetchBusy = false;
        prefetchPromise = null;
        clearTimeout(duePrefetchTimer);
        if (!energySaving()) duePrefetchTimer = setTimeout(() => void prefetchDueQueue(false), dueStatus === 'error' || !dueQueue.length ? 60_000 : 45_000);
      }
    })();
    return prefetchPromise;
  }

  function takePrefetchedCard() {
    const excluded = excludedReviewKeys(false);
    while (dueQueue.length) {
      const card = dueQueue.shift();
      if (card && Number(card.wordId) > 0 && !excluded.has(cardKey(card))) return card;
    }
    return null;
  }

  async function checkDue() {
    if (floating || document.hidden) return;
    if (dueCard || dueBusy || checkingDue) { scheduleDue(); return; }
    if (intervalMs() > 0 && Date.now() - lastReviewAt() < intervalMs()) { dueStatus='interval'; renderDue(); scheduleDue(); return; }
    checkingDue = true;
    try {
      let next = takePrefetchedCard();
      if (!next) {
        await prefetchDueQueue(true);
        if (dueCard || dueBusy) return;
        next = takePrefetchedCard();
      }
      dueCard = next;
      dueRevealed = false;
      renderDue();
      if (!dueCard) scheduleDue(intervalMs() === 0 ? 5_000 : 60_000);
      else if (dueQueue.length < PREFETCH_LOW_WATER) void prefetchDueQueue(false);
    } finally {
      checkingDue = false;
    }
  }

  async function submitDue(grade) {
    if (!dueCard || dueBusy || (activeBook() && !audioState?.float_open)) return;
    dueBusy = true;
    const card = {...dueCard};
    const submittedKey = cardKey(card);
    pendingReviews.add(submittedKey);
    const actions = reviewActions();
    const oldLastReviewAt = lastReviewAt();
    const oldPreviousReview = previousReview;
    const continuous = intervalMs() === 0;
    const optimisticNext = continuous ? takePrefetchedCard() : null;
    previousReview = {card, grade:String(grade), actions, reviewedAt:Date.now(), oldLastReviewAt, pending:true};
    dueCard = optimisticNext;
    dueRevealed = false;
    renderDue();
    if (continuous) {
      // Refill while the user is already looking at the next prefetched card.
      void prefetchDueQueue(false);
    }
    try {
      const attempt = crypto?.randomUUID?.() || `sidebar-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const result = await api().sidebar_due_review_submit(Number(card.wordId), Number(card.readingIndex), String(grade), attempt);
      if (result?.ok === false || result?.outcome === 'unknown') throw new Error(result?.message || 'Review outcome unknown');
      const reviewedAt = Date.now();
      recentReviews.set(submittedKey, reviewedAt);
      localStorage.setItem(REVIEW_AT, String(reviewedAt));
      previousReview = {...previousReview, card, grade:String(grade), actions, reviewedAt, oldLastReviewAt, pending:false};
      if (!continuous) {
        dueCard = null;
        dueRevealed = false;
      } else if (!dueCard) {
        pendingReviews.delete(submittedKey);
        await prefetchDueQueue(true);
        dueCard = takePrefetchedCard();
      }
      renderDue();
      schedulePreviousExpiry();
      if (continuous) {
        if (!dueCard) scheduleDue(5_000);
      } else {
        scheduleDue();
      }
      if (dueQueue.length < PREFETCH_LOW_WATER) void prefetchDueQueue(false);
    } catch (error) {
      if (dueCard) dueQueue.unshift(dueCard);
      dueCard = card;
      dueRevealed = true;
      previousReview = oldPreviousReview;
      renderDue();
      g.toast?.(error?.message || String(error));
    } finally {
      pendingReviews.delete(submittedKey);
      dueBusy = false;
      renderDue();
      if (previousReview?.undoRequested) void undoLastSidebarReview();
    }
  }

  async function undoLastSidebarReview() {
    if (previousReview?.pending) {
      previousReview.undoRequested = true;
      renderDue();
      return true;
    }
    if (!previousReview || dueBusy) return false;
    if (Date.now() - Number(previousReview.reviewedAt || 0) > UNDO_WINDOW_MS) return false;
    dueBusy = true;
    const entry = previousReview;
    previousReview = null;
    renderDue();
    try {
      const result = await api().sidebar_due_review_undo(Number(entry.card.wordId), Number(entry.card.readingIndex));
      if (!result?.ok || String(result?.outcome || '') !== 'undone') throw new Error(result?.message || 'Undo failed');
      localStorage.setItem(REVIEW_AT, String(Number(entry.oldLastReviewAt || 0)));
      recentReviews.delete(cardKey(entry.card));
      if (dueCard) dueQueue.unshift(dueCard);
      dueCard = {...entry.card};
      dueRevealed = false;
      renderDue();
      return true;
    } catch (error) {
      previousReview = entry;
      renderDue();
      schedulePreviousExpiry();
      g.toast?.((ru() ? 'Не удалось отменить: ' : 'Could not undo: ') + String(error?.message || error || ''));
      return false;
    } finally {
      dueBusy = false;
      renderDue();
    }
  }

  function noteExternalReview(event) {
    queueGeneration += 1;
    const detail = event?.detail;
    if (detail?.backend === 'jiten' && Number(detail.wordId) > 0) {
      recentReviews.set(cardKey(detail), Date.now());
    }
    localStorage.setItem(REVIEW_AT, String(Date.now()));
    dueCard = null;
    dueRevealed = false;
    dueQueue = [];
    dueStatus = 'loading';
    renderDue();
    scheduleDue();
    void prefetchDueQueue(true);
  }

  function bindSettings() {
    const select = document.getElementById('s_sidebar_review_interval');
    if (!select || select.dataset.sidebarBound === '1') return;
    select.dataset.sidebarBound = '1';
    select.value = intervalValue();
    select.addEventListener('change', () => {
      const previous = savedInterval, value = select.value;
      savedInterval = value;
      localStorage.setItem(REVIEW_INTERVAL, value);
      scheduleDue(20);
      settingsSave = settingsSave.catch(() => {}).then(async () => {
        try { await api()?.sidebar_review_settings?.(value); }
        catch (error) {
          if (savedInterval === value) {
            savedInterval = previous;
            localStorage.setItem(REVIEW_INTERVAL, previous);
            select.value = previous;
            scheduleDue();
          }
          g.toast?.(error?.message || String(error));
        }
      });
    });
  }

  async function restoreSettings() {
    try {
      const result = await api()?.sidebar_review_settings?.();
      if (result?.interval) savedInterval = String(result.interval);
      else if (api()?.sidebar_review_settings) await api().sidebar_review_settings(savedInterval);
      localStorage.setItem(REVIEW_INTERVAL, savedInterval);
      const select = document.getElementById('s_sidebar_review_interval');
      if (select) select.value = savedInterval;
    } catch (error) { console.debug('sidebar settings', error); }
  }

  function conflictingModalOpen() {
    return Boolean(document.querySelector('.modal-backdrop.open,.onboarding-backdrop.open,.pudge-review-gate.open,.ln-study-pop.open,.pudge-study-card.open'));
  }

  function handleKeydown(event) {
    if (event?.isComposing || event?.repeat) return;
    const host = root?.querySelector('[data-sidebar-due]');
    if (document.hidden || energySaving() || !host || host.hidden || host.isConnected === false) return;
    if (host.offsetParent === null && !host.getClientRects?.().length) return;
    if ((activeBook() && !audioState?.float_open) || document.getElementById('lnReaderShell')?.classList.contains('open') || document.getElementById('mangaReaderV2')?.classList.contains('open')) return;
    if (event?.target?.closest?.('input,textarea,select,[contenteditable="true"]')) return;
    const focusedControl = event?.target?.closest?.('button,a,[role="button"]') || document.activeElement?.closest?.('button,a,[role="button"]');
    if (focusedControl && !root.contains(focusedControl)) return;
    if (conflictingModalOpen()) return;
    const undoChord = (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && String(event.key || '').toLowerCase() === 'z';
    if (undoChord && previousReview && Date.now() - Number(previousReview.reviewedAt || 0) <= UNDO_WINDOW_MS) {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      void undoLastSidebarReview();
      return;
    }
    if (!dueCard) return;
    const key = String(event.key || '');
    const code = String(event.code || '');
    if (!dueRevealed) {
      if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      if (code !== 'Space' && key !== ' ') return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      dueRevealed = true;
      renderDue();
      return;
    }
    const action = g.PudgeReviewActions?.matchAction?.(event, reviewActions());
    if (action) {
      const button = root?.querySelector(`[data-sc-grade="${CSS.escape(String(action.id))}"]`);
      if (!button || button.disabled) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      button.focus?.({preventScroll:true});
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    if (code === 'Space' || key === ' ') {
      const focused = document.activeElement?.closest?.('[data-sc-grade]');
      if (!focused || !root?.contains(focused) || focused.disabled) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      focused.click();
    }
  }

  document.addEventListener('click', event => {
    const word = event.target.closest?.('[data-sc-jiten-word]');
    if (word) {
      event.preventDefault(); event.stopPropagation();
      void g.PudgeJitenWords.openInJiten({wordId:Number(word.dataset.scJitenWord), readingIndex:Number(word.dataset.scJitenReading)})
        .catch(error => g.toast?.(error?.message || String(error)));
      return;
    }
    if (event.target.closest?.('[data-sc-retry]')) { retryAt=0; dueStatus='loading'; renderDue(); void checkDue(); return; }
    // Audiobook cover: click opens the audiobook; zoom like every other cover
    // (trackpad pinch / drag, handled by CoverPreview). Jiten card image: click zooms.
    const image = event.target.closest?.('.sidebar-due-image img');
    if(image){event.preventDefault();g.PudgeCoverPreview?.open?.(image);return;}
    const action = event.target.closest?.('[data-sc-audio]');
    if (action) { void audioClick(String(action.dataset.scAudio || ''), action).catch(error => g.toast?.(error?.message || String(error))); return; }
    if (event.target.closest?.('[data-sc-reveal]') && (!activeBook() || audioState?.float_open)) { dueRevealed = true; renderDue(); return; }
    const grade = event.target.closest?.('[data-sc-grade]');
    if (grade) { void submitDue(grade.dataset.scGrade); return; }
    if (event.target.closest?.('[data-sc-undo]')) void undoLastSidebarReview();
  });

  document.addEventListener('change', event => {
    if (event.target.matches?.('[data-sc-audio-timeline]')) {
      const book = activeBook();
      if (!book) return;
      liveTextSignature = '';
      void api().audiobook_seek_to(Number(book.id), Number(event.target.value || 0)).then(() => pollAudio())
        .catch(error => g.toast?.(error?.message || String(error)));
    }
  });

  document.addEventListener('mouseup', event => {
    const live = event.target.closest?.('[data-sc-live-text]');
    if (!live) return;
    setTimeout(() => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || !selection.rangeCount || !live.contains(selection.getRangeAt(0).commonAncestorContainer)) return;
      liveFollowFrozen = true;
      const book = activeBook();
      if (book?.playing) void api().audiobook_set_paused(Number(book.id), true).then(() => pollAudio());
    }, 0);
  });

  document.addEventListener('keydown', handleKeydown, true);
  window.addEventListener('pudge-review-completed', noteExternalReview);

  function showAudioMenu(event) {
    const article = event.target.closest?.('.sidebar-audio');
    const book = activeBook();
    if (!article || !book) return;
    event.preventDefault(); event.stopPropagation();
    let menu = document.getElementById('sidebarAudioMenu');
    if (!menu) {
      menu = document.createElement('div'); menu.id = 'sidebarAudioMenu';
      menu.className = 'sidebar-audio-menu'; menu.setAttribute('role', 'menu');
      document.body.appendChild(menu);
    }
    menu.innerHTML = `<button type="button" role="menuitem" data-sc-menu="open">${ru() ? 'Показать аудиокнигу' : 'Show audiobook'}</button>
      ${!floating && api()?.audiobook_float_open ? `<button type="button" role="menuitem" data-sc-menu="float">${ru() ? 'Поверх всех окон' : 'Always on top'}</button>` : ''}
      ${book.linked_light_novel ? `<button type="button" role="menuitem" data-sc-menu="read">${ru() ? 'Перейти к чтению' : 'Go to reading'}</button>` : ''}
      <button type="button" role="menuitem" data-sc-menu="stop">${ru() ? 'Остановить прослушивание' : 'Stop listening'}</button>`;
    menu.hidden = false;
    menu.dataset.bookId = String(book.id);
    menu.style.left = `${Math.max(4, Math.min(event.clientX, window.innerWidth - 230))}px`;
    menu.style.top = `${Math.max(4, Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 4))}px`;
    menu.querySelector('button')?.focus({preventScroll:true});
  }
  function showSpeedMenu(target, book) {
    let menu = document.getElementById('sidebarSpeedMenu');
    if (!menu) {
      menu = document.createElement('div'); menu.id = 'sidebarSpeedMenu';
      menu.className = 'sidebar-audio-menu sidebar-speed-menu'; menu.setAttribute('role', 'menu');
      document.body.appendChild(menu);
    }
    if (!menu.hidden && menu.dataset.bookId === String(book.id)) { menu.hidden = true; return; }
    menu.dataset.bookId = String(book.id);
    menu.innerHTML = AUDIO_SPEEDS.map(speed => `<button type="button" role="menuitemradio" aria-checked="${Number(book.speed || 1) === speed}" data-sc-speed="${speed}">${speed}×</button>`).join('');
    menu.hidden = false;
    const bounds = target.getBoundingClientRect();
    menu.style.left = `${Math.max(4, Math.min(bounds.left, window.innerWidth - 120))}px`;
    menu.style.top = `${Math.max(4, Math.min(bounds.bottom + 4, window.innerHeight - menu.offsetHeight - 4))}px`;
    menu.querySelector('[aria-checked="true"]')?.focus({preventScroll:true});
  }
  document.addEventListener('click', event => {
    const menu = document.getElementById('sidebarSpeedMenu');
    if (!menu || menu.hidden || event.target.closest?.('[data-sc-audio="speed"]')) return;
    const choice = event.target.closest?.('[data-sc-speed]');
    menu.hidden = true;
    const book = activeBook();
    if (!choice || Number(menu.dataset.bookId) !== Number(book?.id)) return;
    void (async () => {
      const speed = Number(choice.dataset.scSpeed);
      const result = await api().audiobook_set_speed(Number(book.id), speed);
      if (Number(activeBook()?.id) !== Number(book.id)) return;
      acceptAudioState({books:[result?.book || {...book, speed}]});
      liveClock?.reset({...book, speed, position:liveClock.current()});
      void pollAudio();
    })().catch(error => g.toast?.(error?.message || String(error)));
  });
  document.addEventListener('contextmenu', showAudioMenu);
  document.addEventListener('click', event => {
    const menu = document.getElementById('sidebarAudioMenu');
    if (!menu || menu.hidden) return;
    const button = event.target.closest?.('[data-sc-menu]');
    menu.hidden = true;
    if (button && Number(menu.dataset.bookId) === Number(activeBook()?.id)) {
      void audioClick(button.dataset.scMenu, button).catch(error => g.toast?.(error?.message || String(error)));
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      const speeds = document.getElementById('sidebarSpeedMenu');
      if (speeds && !speeds.hidden) { speeds.hidden = true; event.preventDefault(); event.stopPropagation(); }
      const menu = document.getElementById('sidebarAudioMenu');
      if (menu && !menu.hidden) { menu.hidden = true; event.preventDefault(); event.stopPropagation(); }
    }
  });

  function powerChanged() {
    clearTimeout(liveTimer); clearTimeout(dueTimer); clearTimeout(duePrefetchTimer);
    if (liveFrame != null) cancelAnimationFrame(liveFrame);
    liveFrame = null;
    // Hidden (alt-tab / occluded window) only pauses frames: keep the painted
    // text so returning does not visibly rebuild it. Clear only when the text
    // is truly unwanted here (energy saving, or the float owns live text).
    if (!liveTextAllowed() && (energySaving() || (!floating && audioState?.float_open))) {
      const host = root?.querySelector('[data-sc-live-text]');
      if (host) { host.replaceChildren(); host._scWords = null; host._scActive = null; }
      liveSnapshot = null; liveTextSignature = ''; liveShellKey = '';
    }
    renderAudio();
    if (!energySaving()) void prefetchDueQueue(false);
    renderDue(); scheduleDue();
  }
  document.addEventListener('visibilitychange', () => {
    powerChanged();
    if (!document.hidden) { void pollAudio(); if (g.ui?.lnPairedState?.playing) g.startLnPairedInterpolation?.(g.ui.lnPairedState); }
  });

  const boot = async () => {
    ensure();
    pollAudio();
    const known = g.PudgeMedia?.audioSnapshot?.();
    if (known?.books) acceptAudioState(known);
    if (floating) return;
    await restoreSettings();
    void prefetchDueQueue(true);
    scheduleDue();
    const observer = new MutationObserver(() => bindSettings());
    observer.observe(document.body, {subtree:true, childList:true});
    bindSettings();
  };
  if (g.pywebview?.api) boot();
  else window.addEventListener('pywebviewready', boot, {once:true});

  async function navigateAudio(action, bookId) {
    await pollAudio();
    if (Number(activeBook()?.id) !== Number(bookId)) return false;
    await audioClick(action, {});
    return true;
  }

  g.PudgeSidebarCompanion = {navigateAudio, refresh:pollAudio, noteReview:noteExternalReview, prefetch:prefetchDueQueue,
    acceptAudioState, acceptPairedState, powerChanged, showChapterHoverRange, clearChapterHoverRange};
})();
