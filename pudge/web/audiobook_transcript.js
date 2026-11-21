'use strict';
// Read-along text of the audiobook player (sidebar and the always-on-top float).
//
// The "Text" toggle beside the player's time shows the book's timed lines
// (pywebview.api.audiobook_read_along: the text the iPhone's Read along shows — the
// subtitles of an audiobook made from anime episodes, or a linked light novel). The
// lines are loaded once per book and drawn once; playback only moves a highlight: the
// current line is found by binary search on the line starts, the previous one loses
// its class, nothing else is rebuilt. The current line follows the audio until the
// user scrolls the text; following resumes a few seconds later or with the button.
// Words are parsed a few lines at a time (study_parse_text, the parse every other
// Japanese text in Pudge uses) and get the same word popup on hover/click.
(() => {
  const g = globalThis;
  const api = () => g.pywebview?.api || g.window?.pywebview?.api;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const t = (key, params) => g.PudgeI18n?.t?.(key, params) ?? key;
  const OPEN_KEY = 'pudge.audiobookTranscript.open.v1';
  const BLOCK = 12;          // lines per word-parse request
  const RESUME_MS = 5000;    // following resumes this long after the last manual scroll
  const TICK_MS = 250;       // highlight step while playing (between player polls)
  const CACHE = 4;           // books kept in memory

  let open = false;
  try { open = localStorage.getItem(OPEN_KEY) === '1'; } catch (_) { open = false; }
  const books = new Map();   // bookId -> {lines, starts, chapters, source, revision}
  let view = null;           // {bookId, host, list, rows, current, parsed, queue}
  let clock = {bookId:0, position:0, at:0, playing:false, speed:1};
  let tickTimer = null;
  let resumeTimer = null;
  let scanTimer = null;
  let following = true;
  let parsing = false;
  let loadToken = 0;
  let jumpNext = true;       // opening the panel shows the current line at once

  const now = () => (g.performance?.now ? g.performance.now() : Date.now());
  const clockText = value => {
    const n = Math.max(0, Math.floor(Number(value || 0)));
    const h = Math.floor(n / 3600), m = Math.floor(n % 3600 / 60), s = String(n % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
  };

  // Index of the last line that started at or before `seconds` (-1 before the first).
  function lineAt(starts, seconds) {
    let low = 0, high = starts.length - 1, found = -1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (starts[mid] <= seconds) { found = mid; low = mid + 1; } else high = mid - 1;
    }
    return found;
  }

  function position() {
    if (!clock.playing) return clock.position;
    return clock.position + Math.max(0, now() - clock.at) / 1000 * Math.max(.1, Number(clock.speed || 1));
  }

  // What the lines were made from (audiobooks.book: read_along_revision, and the length):
  // an anime audiobook made again (another cut, subtitles prepared later) has other lines
  // at other times, and the lines kept for the book would follow the old file.
  function revisionOf(book) {
    return `${book?.read_along_revision ?? ''}:${Math.round(Number(book?.duration || 0) * 10)}`;
  }

  async function load(bookId, revision) {
    const kept = books.get(bookId);
    if (kept && kept.revision === revision) return kept;
    books.delete(bookId);
    const result = await api()?.audiobook_read_along?.(bookId);
    if (result?.ok === false) throw new Error(result.error || 'read-along');
    const lines = (Array.isArray(result?.lines) ? result.lines : [])
      .map(line => ({start:Number(line?.start), end:Number(line?.end), text:String(line?.text || ''), chapter:Number(line?.chapter || 0)}))
      .filter(line => Number.isFinite(line.start) && line.text)
      .sort((a, b) => a.start - b.start);
    const data = {
      lines, starts:Float64Array.from(lines.map(line => line.start)),
      chapters:Array.isArray(result?.chapters) ? result.chapters : [], source:String(result?.source || ''), revision,
    };
    if (lines.length) {
      books.set(bookId, data);
      if (books.size > CACHE) books.delete(books.keys().next().value);
    }
    return data;
  }

  function panelMarkup() {
    return `<div class="sidebar-audio-transcript" data-sc-transcript data-pudge-translate-root data-pudge-study-hover hidden>`
      + `<div class="abt-list" data-abt-list></div>`
      + `<button type="button" class="abt-follow" data-abt-follow data-i18n="sidebar.readAlongFollow" hidden>${esc(t('sidebar.readAlongFollow'))}</button></div>`;
  }

  function toggleMarkup() {
    return `<button type="button" class="sidebar-audio-text-toggle" data-sc-audio="text" aria-pressed="false" data-i18n="sidebar.readAlongToggle" data-i18n-title="sidebar.readAlongTitle" title="${esc(t('sidebar.readAlongTitle'))}" hidden>${esc(t('sidebar.readAlongToggle'))}</button>`;
  }

  function status(list, key, params) {
    list.innerHTML = `<div class="abt-status">${esc(t(key, params))}</div>`;
  }

  function drawLines(list, data) {
    let previousChapter = -1, html = '';
    data.lines.forEach((line, index) => {
      if (line.chapter !== previousChapter && data.chapters.length > 1) {
        previousChapter = line.chapter;
        const title = String(data.chapters[line.chapter]?.title || '');
        if (title) html += `<div class="abt-chapter">${esc(title)}</div>`;
      }
      html += `<div class="abt-line" data-abt-line="${index}"><button type="button" class="abt-time" data-abt-seek="${index}" tabindex="-1" aria-label="${esc(t('sidebar.readAlongSeek', {time:clockText(line.start)}))}">${clockText(line.start)}</button><span class="abt-text" data-abt-text>${esc(line.text)}</span></div>`;
    });
    list.innerHTML = html;
    return [...list.querySelectorAll('[data-abt-line]')];
  }

  async function show(article, book) {
    const host = article.querySelector('[data-sc-transcript]');
    const list = host?.querySelector('[data-abt-list]');
    if (!host || !list) return;
    const bookId = Number(book.id);
    const revision = revisionOf(book);
    if (view?.bookId === bookId && view.host === host && view.revision === revision) return;
    const token = ++loadToken;
    view = {bookId, revision, host, list, rows:[], current:-1, parsed:new Set(), queue:[], data:null,
      mediaContext:book.linked_light_novel?.id ? {kind:'light_novel', book_id:Number(book.linked_light_novel.id)} : null};
    following = true;
    host.querySelector('[data-abt-follow]').hidden = true;
    if (books.get(bookId)?.revision !== revision) status(list, 'sidebar.readAlongLoading');
    try {
      const data = await load(bookId, revision);
      if (token !== loadToken || !host.isConnected) return;
      if (!data.lines.length) { status(list, 'sidebar.readAlongEmpty'); view.data = null; return; }
      view.data = data;
      view.rows = drawLines(list, data);
      paint(true);
    } catch (error) {
      if (token !== loadToken) return;
      status(list, 'sidebar.readAlongError', {error:g.PudgeI18n?.message?.(String(error?.message || error)) ?? String(error?.message || error)});
    }
  }

  function scrollToRow(row, smooth) {
    const list = view?.list;
    if (!row || !list) return;
    const top = row.offsetTop - list.offsetTop - list.clientHeight * .35;
    if (typeof list.scrollTo === 'function') list.scrollTo({top:Math.max(0, top), behavior:smooth ? 'smooth' : 'auto'});
    else list.scrollTop = Math.max(0, top);
  }

  function paint(jump = false) {
    if (!view?.data || !view.host.isConnected || view.host.hidden) return;
    const index = lineAt(view.data.starts, position());
    if (index !== view.current) {
      view.rows[view.current]?.classList.remove('current');
      view.current = index;
      const row = view.rows[index];
      row?.classList.add('current');
      if (index >= 0) want(index);
      if (row && following && !g.PudgeReadingTools?.study?.isOpen?.()) scrollToRow(row, !jump);
    } else if (jump && following) scrollToRow(view.rows[index], false);
    if (jump) want(Math.max(0, index)); // before the first line: its words too
  }

  function schedule() {
    clearTimeout(tickTimer);
    tickTimer = null;
    if (!open || !view?.data || !clock.playing || document.hidden) return;
    tickTimer = setTimeout(() => { tickTimer = null; paint(); schedule(); }, TICK_MS);
  }

  // ---------------------------------------------------------------- words

  function want(index) {
    if (!view) return;
    const block = Math.floor(index / BLOCK);
    for (const wanted of [block, block + 1]) {
      if (wanted * BLOCK >= view.rows.length || view.parsed.has(wanted) || view.queue.includes(wanted)) continue;
      view.queue.unshift(wanted);
    }
    view.queue.length = Math.min(view.queue.length, 4); // the newest wishes only
    void parseNext();
  }

  async function parseNext() {
    const parse = api()?.study_parse_text;
    const renderer = g.PudgeReadingTools?.study?.renderParsedParagraph;
    if (parsing || !view?.queue.length || typeof parse !== 'function' || typeof renderer !== 'function') return;
    const current = view, block = current.queue.shift();
    current.parsed.add(block);
    const first = block * BLOCK, lines = current.data.lines.slice(first, first + BLOCK);
    const texts = lines.map(line => line.text.replace(/\s+/g, ' ').trim());
    parsing = true;
    try {
      const payload = await parse(texts.join('\n'));
      if (view !== current) return;
      const paragraphs = payload?.paragraphs || [];
      if (paragraphs.length !== texts.length) return; // the lines do not map one to one
      const backend = String(payload?.settings?.study_backend || 'jiten');
      const furigana = payload?.settings?.show_furigana !== false;
      texts.forEach((text, offset) => {
        const target = current.rows[first + offset]?.querySelector('[data-abt-text]');
        if (!target || paragraphs[offset] !== text) return;
        const previous = String(texts[offset - 1] || ''), next = String(texts[offset + 1] || '');
        target.innerHTML = String(renderer(payload, offset, {
          backend, furigana, highlightOptimalWords:false, mediaContext:current.mediaContext,
          contextText:[previous, text, next].filter(Boolean).join('\n'), contextOffset:previous ? previous.length + 1 : 0,
        })).replace(/^<p>|<\/p>$/g, '');
      });
      g.PudgeReadingTools?.study?.queueLiveStateHydration?.(payload, backend);
    } catch (error) {
      console.debug('read-along words', error);
    } finally {
      parsing = false;
      if (view === current && current.queue.length) void parseNext();
    }
  }

  // The first line shown after a manual scroll (rows are in document order).
  function firstVisible() {
    const rows = view?.rows || [], list = view?.list;
    if (!rows.length || !list) return -1;
    const top = list.scrollTop + list.offsetTop;
    let low = 0, high = rows.length - 1, found = 0;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (rows[mid].offsetTop + rows[mid].offsetHeight > top) { found = mid; high = mid - 1; } else low = mid + 1;
    }
    return found;
  }

  // ---------------------------------------------------------------- following

  function pauseFollowing() {
    if (!view?.data) return;
    following = false;
    view.host.querySelector('[data-abt-follow]').hidden = false;
    clearTimeout(resumeTimer);
    resumeTimer = setTimeout(resumeFollowing, RESUME_MS);
    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => { const index = firstVisible(); if (index >= 0) want(index); }, 200);
  }

  function resumeFollowing() {
    clearTimeout(resumeTimer);
    following = true;
    if (!view?.data) return;
    view.host.querySelector('[data-abt-follow]').hidden = true;
    scrollToRow(view.rows[view.current], true);
  }

  document.addEventListener('wheel', event => {
    if (event.target.closest?.('[data-abt-list]')) pauseFollowing();
  }, {capture:true, passive:true});
  // A drag on the scrollbar (the list itself, not a line) also scrolls by hand. Scroll
  // events are not used: a long smooth scroll of our own would read as the user's.
  document.addEventListener('pointerdown', event => {
    if (event.target?.matches?.('[data-abt-list]')) pauseFollowing();
  }, true);
  document.addEventListener('pointerover', event => {
    const row = event.target.closest?.('[data-abt-line]');
    if (row && view?.rows.length) want(Number(row.dataset.abtLine));
  }, true);
  document.addEventListener('click', event => {
    if (event.target.closest?.('[data-abt-follow]')) { resumeFollowing(); return; }
    const row = event.target.closest?.('[data-abt-line]');
    if (!row || !view?.data) return;
    // A word opens its card (reading_tools); a selection is for copying.
    if (event.target.closest?.('[data-pudge-study-token]')) return;
    const selection = g.getSelection?.();
    if (selection && !selection.isCollapsed) return;
    const line = view.data.lines[Number(row.dataset.abtLine)];
    if (line) void seek(line.start);
  });

  async function seek(seconds) {
    const bookId = view?.bookId;
    if (!bookId) return;
    clock = {...clock, bookId, position:seconds, at:now()};
    resumeFollowing();
    paint(true);
    try {
      await api().audiobook_seek_to(bookId, seconds);
      void g.PudgeSidebarCompanion?.refresh?.();
    } catch (error) {
      g.toast?.(error?.message || String(error));
    }
  }

  // ---------------------------------------------------------------- the player

  // Called by the sidebar player on every state it draws (cheap when nothing changed).
  function update(article, book, {hiddenText = false} = {}) {
    const toggle = article?.querySelector?.('[data-sc-audio="text"]');
    const host = article?.querySelector?.('[data-sc-transcript]');
    if (!toggle || !host) return;
    const available = Boolean(book?.read_along);
    toggle.hidden = !available;
    const label = t('sidebar.readAlongToggle'); // the window language can arrive after the shell
    if (toggle.textContent !== label) { toggle.textContent = label; toggle.title = t('sidebar.readAlongTitle'); }
    const shown = available && open && !hiddenText;
    toggle.setAttribute('aria-pressed', open && available ? 'true' : 'false');
    toggle.classList.toggle('active', open && available);
    article.classList.toggle('transcript-open', shown);
    host.hidden = !shown;
    const bookId = Number(book?.id || 0);
    const playing = Boolean(book?.playing), at = Number(book?.position || 0);
    // A fresh sample from the player: keep the local clock unless it is clearly off.
    if (clock.bookId !== bookId || playing !== clock.playing || !playing || Math.abs(position() - at) > .6) {
      clock = {bookId, position:at, at:now(), playing, speed:Number(book?.speed || 1)};
    } else clock.speed = Number(book?.speed || 1);
    if (!shown) { clearTimeout(tickTimer); tickTimer = null; return; }
    if (view?.bookId !== bookId || view.host !== host || view.revision !== revisionOf(book)) void show(article, book);
    else { paint(jumpNext); jumpNext = false; }
    schedule();
  }

  function toggle() {
    open = !open;
    try { localStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch (_) { /* private mode */ }
    if (!open) { clearTimeout(tickTimer); tickTimer = null; }
    else {
      if (view && !view.data) view = null; // an empty or failed load is asked again
      following = true; jumpNext = true;
      if (view) view.host.querySelector('[data-abt-follow]').hidden = true;
    }
    return open;
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) { paint(); schedule(); } });

  g.PudgeAudiobookTranscript = {
    panelMarkup, toggleMarkup, update, toggle, isOpen:() => open, lineAt,
    current:() => view?.current ?? -1, following:() => following,
  };
})();
