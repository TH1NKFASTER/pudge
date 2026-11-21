'use strict';

/* Light novel reader: the "read up to here" marker, one per volume and shared with the paired
   iPhone/iPad (pudge/ln_read_marker.py has the rules and the sync). "Read up to here" on a word
   puts it exactly before that word's first character. Reading on (the 20-second dwell save:
   the start of the first line wholly on screen) and the paired audiobook (the line of the word
   being read) move it forward only, by at least `auto_min_chars` characters; the Mac snaps
   those to the start of their sentence. It is drawn over the text (a red ribbon in the margin by
   the line and a thin caret before the character), so the text layout never moves. Hovering
   the ribbon for a second says who set it and when; a button brings it back into view. */
(() => {
  const HOVER_MS = 1000;
  const FLASH_MS = 1600;
  const AUDIO_GAP_MS = 2000;    // the paired audio asks the Mac at most this often
  const SCROLL_SYNC_MS = 150;
  const REFRESH_GAP_MS = 3000;  // a marker moved on the phone is fetched when the window is back
  const LINE_BACK = 160;        // characters looked back for the start of a line
  const DEFAULT_MIN_CHARS = 300;
  const state = {layer: null, tip: null, hoverTimer: null, flashTimer: null, frame: null, scrollTimer: null,
    audioBusy: false, audioAt: -Infinity, refreshAt: -Infinity, observer: null, observed: null};
  const startsCache = new WeakMap();

  const reader = () => document.getElementById('lnReader');
  const scroller = () => document.getElementById('lnReaderScroll');
  const shell = () => document.getElementById('lnReaderShell');
  const isOpen = () => shell()?.classList.contains('open') === true;
  const isVertical = () => reader()?.classList.contains('vertical') === true;
  const isPages = () => scroller()?.classList.contains('pages') === true;
  const tr = (key, params) => window.PudgeI18n ? PudgeI18n.t(key, params) : key;
  const current = () => (typeof ui === 'object' && ui?.lnBook?.read_marker) || null;
  const sameChapter = marker => !!marker && !!ui.lnChapter && Number(marker.chapter_index) === Number(ui.lnChapter.chapter_index);
  const samePlace = (a, b) => (!a && !b) || (!!a && !!b && Number(a.chapter_index) === Number(b.chapter_index) && Number(a.char_offset) === Number(b.char_offset));
  const isLow = code => code >= 0xDC00 && code <= 0xDFFF;

  // UTF-16 start of each paragraph in the chapter text, found as lnReaderCharacterOffset does.
  function paragraphStarts(payload) {
    const raw = String(payload?.text || ''), paragraphs = payload?.paragraphs || [];
    const entry = startsCache.get(payload);
    if (entry && entry.raw === raw && entry.count === paragraphs.length) return entry.starts;
    const starts = [];
    let cursor = 0;
    for (const value of paragraphs) {
      const text = String(value || ''), found = raw.indexOf(text, cursor), start = found >= 0 ? found : cursor;
      starts.push(start); cursor = Math.max(start + text.length, cursor);
    }
    startsCache.set(payload, {raw, count: paragraphs.length, starts});
    return starts;
  }
  // UTF-16 index of a character (code points from the chapter start): lnCodePointsBefore reversed.
  function utf16At(entry, char) {
    const pairs = entry.pairs;
    let lo = 0, hi = pairs.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (pairs[mid] - mid < char) lo = mid + 1; else hi = mid; }
    return Math.min(entry.raw.length, char + lo);
  }
  function charOf(index, local) {
    const payload = ui.lnChapter, raw = String(payload?.text || '');
    if (!raw || typeof lnCodePointIndex !== 'function') return null;
    const starts = paragraphStarts(payload);
    return lnCodePointsBefore(lnCodePointIndex(payload, raw), Number(starts[index] ?? 0) + Math.max(0, local));
  }

  // The paragraph's own text: furigana (<rt>, <rp>) and the marker are not part of it.
  function textNodes(paragraph) {
    const nodes = [];
    const walk = node => {
      for (const child of node.childNodes || []) {
        if (child.nodeType === 3) nodes.push(child);
        else if (child.nodeType === 1 && !/^(rt|rp)$/i.test(String(child.localName || child.tagName || '')) && !child.classList?.contains('ln-read-marker')) walk(child);
      }
    };
    walk(paragraph);
    return nodes;
  }
  function domAt(paragraph, local, nodes = textNodes(paragraph)) {
    let seen = 0;
    for (const node of nodes) {
      const size = node.data.length;
      if (local < seen + size) return {node, offset: local - seen};
      seen += size;
    }
    const last = nodes.at(-1);
    return last ? {node: last, offset: last.data.length} : null;
  }
  function firstRect(range) {
    for (const rect of range.getClientRects?.() || []) if (rect && (rect.width > 0 || rect.height > 0)) return rect;
    return null;
  }
  // The box of one character (the edge after the last one at the end of the text).
  function charRect(node, offset) {
    const size = node.data.length, range = document.createRange();
    if (offset < size) {
      const wide = !isLow(node.data.charCodeAt(offset)) && offset + 1 < size && isLow(node.data.charCodeAt(offset + 1));
      range.setStart(node, offset); range.setEnd(node, offset + (wide ? 2 : 1));
      return firstRect(range);
    }
    if (!size) return null;
    range.setStart(node, size - 1); range.setEnd(node, size);
    const rect = firstRect(range);
    if (!rect) return null;
    return isVertical()
      ? {left: rect.left, right: rect.right, width: rect.width, top: rect.bottom, bottom: rect.bottom, height: 0}
      : {left: rect.right, right: rect.right, width: 0, top: rect.top, bottom: rect.bottom, height: rect.height};
  }
  function rectAt(paragraph, local, nodes) {
    const at = domAt(paragraph, local, nodes);
    if (!at) return null;
    if (at.offset > 0 && at.offset < at.node.data.length && isLow(at.node.data.charCodeAt(at.offset))) at.offset -= 1;
    return charRect(at.node, at.offset);
  }

  // Where a marker is in the open chapter: {paragraph, index, local (UTF-16 in the paragraph), node, offset}.
  function point(marker = current()) {
    const payload = ui.lnChapter, root = reader();
    if (!marker || !payload || !root || !sameChapter(marker) || typeof lnCodePointIndex !== 'function') return null;
    const raw = String(payload.text || ''), paragraphs = payload.paragraphs || [];
    if (!raw || !paragraphs.length) return null;
    const utf16 = utf16At(lnCodePointIndex(payload, raw), Math.max(0, Math.round(Number(marker.char_offset || 0))));
    const starts = paragraphStarts(payload);
    let index = 0;
    for (let i = 0; i < starts.length; i++) { if (starts[i] <= utf16) index = i; else break; }
    let local = utf16 - starts[index];
    if (utf16 < starts[0]) local = 0;
    else if (local >= String(paragraphs[index] || '').length && index + 1 < paragraphs.length) { index += 1; local = 0; }  // between paragraphs: the next one
    // An illustration has no characters: the next paragraph with text.
    for (let i = index; i < paragraphs.length; i++) {
      const paragraph = root.querySelector(`p[data-ln-paragraph="${i}"]`), nodes = paragraph ? textNodes(paragraph) : [];
      if (!nodes.length) continue;
      if (i !== index) local = 0;
      local = Math.max(0, Math.min(local, String(paragraphs[i] || '').length));
      const at = domAt(paragraph, local, nodes);
      return at ? {paragraph, index: i, local, ...at} : null;
    }
    return null;
  }

  // The first character of the line holding `local` (paragraph-local UTF-16), by layout.
  function lineStart(paragraph, local) {
    const nodes = textNodes(paragraph), rect = rectAt(paragraph, local, nodes);
    if (!rect) return null;
    const vertical = isVertical();
    let start = local;
    for (let i = local - 1; i >= Math.max(0, local - LINE_BACK); i--) {
      const at = domAt(paragraph, i, nodes);
      if (at && isLow(at.node.data.charCodeAt(at.offset))) continue;
      const box = at ? charRect(at.node, at.offset) : null;
      if (!box) break;
      const same = vertical ? Math.abs(box.left - rect.left) < Math.max(2, rect.width / 2) : Math.abs(box.top - rect.top) < Math.max(2, rect.height / 2);
      if (!same) break;
      start = i;
    }
    return start;
  }

  // The start of the first line wholly on screen (vertical text: the rightmost column); null
  // without layout. The automatic marker goes there: what is on screen counts as reached.
  function visibleChar() {
    const root = reader(), view = scroller()?.getBoundingClientRect?.();
    if (!root || !view || !(view.width > 0 && view.height > 0) || !ui.lnChapter) return null;
    const vertical = isVertical(), pages = isPages();
    const before = rect => vertical ? rect.right > view.right + .5 : pages ? rect.left < view.left - .5 : rect.top < view.top - .5;
    const boxBefore = node => { const box = node.getBoundingClientRect(); return vertical ? box.left >= view.right - .5 : pages ? box.right <= view.left + .5 : box.bottom <= view.top + .5; };
    const list = [...root.querySelectorAll('p[data-ln-paragraph]')];
    let low = 0, high = list.length;  // the paragraphs wholly before the view, skipped by halves
    while (low < high) { const mid = (low + high) >> 1; if (boxBefore(list[mid])) low = mid + 1; else high = mid; }
    for (const paragraph of list.slice(Math.max(0, low - 1))) {
      const nodes = textNodes(paragraph), size = nodes.reduce((sum, node) => sum + node.data.length, 0);
      if (!size) continue;
      const last = rectAt(paragraph, size - 1, nodes);
      if (!last) return null;
      if (before(last)) continue;
      const index = Number(paragraph.dataset.lnParagraph), first = rectAt(paragraph, 0, nodes);
      if (first && !before(first)) return charOf(index, 0);
      let lo = 0, hi = size - 1;
      while (lo < hi) {
        const mid = (lo + hi) >> 1, rect = rectAt(paragraph, mid, nodes);
        if (!rect || before(rect)) lo = mid + 1; else hi = mid;
      }
      return charOf(index, lo);
    }
    return null;
  }

  // The reading place is a character: the one at the middle of the view, as a share of the
  // chapter's characters (lnReaderCharPoint). A volume used to open at that share of the scroll
  // range, which is another measure: a place near a chapter's start came back up to half a
  // screen further on (near its end, further back), and the next dwell save kept the shift.
  // Scrolls that character back to the middle of the view. The scroll share stays without
  // layout, in pages (a paragraph's box spans columns) and at a chapter's very start or end.
  function showPlace(fraction) {
    const payload = ui.lnChapter, root = reader(), el = scroller(), wanted = Number(fraction);
    if (!payload || !root || !el || !(wanted > 0 && wanted < 1) || isPages() || typeof lnCodePointIndex !== 'function') return false;
    const raw = String(payload.text || ''), paragraphs = payload.paragraphs || [];
    if (!raw || !paragraphs.length) return false;
    const entry = lnCodePointIndex(payload, raw);
    if (!entry.total) return false;
    const utf16 = utf16At(entry, Math.round(wanted * entry.total)), starts = paragraphStarts(payload);
    let index = 0;
    for (let i = 0; i < starts.length; i++) { if (starts[i] <= utf16) index = i; else break; }
    const size = String(paragraphs[index] || '').length;
    const ratio = size ? Math.max(0, Math.min(1, (utf16 - starts[index]) / size)) : 0;
    const rect = root.querySelector(`[data-ln-paragraph="${index}"]`)?.getBoundingClientRect?.(), view = el.getBoundingClientRect?.();
    if (!rect || !view || !(rect.width > 0 && rect.height > 0) || !(view.width > 0 && view.height > 0)) return false;
    if (isVertical()) {
      const range = typeof lnVerticalScrollRange === 'function' ? lnVerticalScrollRange(el) : null;
      if (!range) return false;
      const move = rect.right - rect.width * ratio - (view.left + view.width / 2);
      el.scrollLeft = Math.max(Math.min(range.start, range.end), Math.min(Math.max(range.start, range.end), el.scrollLeft + move));
    } else {
      const span = Math.max(0, el.scrollHeight - el.clientHeight);
      if (span <= 1) return false;
      el.scrollTop = Math.max(0, Math.min(span, el.scrollTop + rect.top + rect.height * ratio - (view.top + view.height / 2)));
    }
    return true;
  }

  function label(marker) {
    const kind = tr(marker?.source === 'auto' ? 'ln.readMarker.auto' : 'ln.readMarker.manual');
    const at = new Date(Number(marker?.updated_at || 0) * 1000), lang = window.PudgeI18n?.lang?.() || 'en';
    const today = at.toDateString() === new Date().toDateString();
    const time = today
      ? at.toLocaleTimeString(lang, {hour: '2-digit', minute: '2-digit'})
      : at.toLocaleString(lang, {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
    const device = String(marker?.device_id || 'mac') !== 'mac' ? String(marker?.device_name || '').trim() : '';
    return device ? tr('ln.readMarker.tooltipDevice', {kind, time, device}) : tr('ln.readMarker.tooltip', {kind, time});
  }

  function ensureLayer() {
    if (state.layer) return state.layer;
    const layer = document.createElement('div');
    layer.className = 'ln-read-marker';
    layer.hidden = true;
    layer.innerHTML = '<span class="ln-read-marker-glow"></span><span class="ln-read-marker-caret"></span><span class="ln-read-marker-ribbon" role="img"></span>';
    const ribbon = layer.querySelector('.ln-read-marker-ribbon');
    ribbon.addEventListener('pointerenter', () => { clearTimeout(state.hoverTimer); state.hoverTimer = setTimeout(() => showTip(ribbon), HOVER_MS); });
    ribbon.addEventListener('pointerleave', hideTip);
    state.layer = layer;
    return layer;
  }
  function setBox(node, left, top, width, height) {
    node.style.left = `${left.toFixed(1)}px`; node.style.top = `${top.toFixed(1)}px`;
    node.style.width = `${Math.max(0, width).toFixed(1)}px`; node.style.height = `${Math.max(0, height).toFixed(1)}px`;
  }
  function flash(layer) {
    if (!layer) return;
    layer.classList.remove('flash'); void layer.offsetWidth; layer.classList.add('flash');
    clearTimeout(state.flashTimer);
    state.flashTimer = setTimeout(() => layer.classList.remove('flash'), FLASH_MS);
  }

  // Draws the marker of the open chapter (none in another chapter) and syncs the button.
  function place(options = {}) {
    const root = reader(), marker = current(), layer = ensureLayer();
    observe();
    const at = isOpen() && root ? point(marker) : null;
    if (!at) {
      layer.hidden = true; if (layer.parentNode) layer.remove();
      hideTip(); syncJump();
      return false;
    }
    if (layer.parentNode !== root) root.append(layer);
    layer.hidden = false;
    const vertical = isVertical(), text = label(marker);
    layer.classList.toggle('vertical', vertical);
    layer.dataset.kind = marker.source === 'auto' ? 'auto' : 'manual';
    layer.dataset.chapter = String(Number(marker.chapter_index));
    layer.dataset.char = String(Math.round(Number(marker.char_offset || 0)));
    layer.dataset.paragraph = String(at.index);
    layer.dataset.local = String(at.local);
    const caret = layer.querySelector('.ln-read-marker-caret'), ribbon = layer.querySelector('.ln-read-marker-ribbon'), glow = layer.querySelector('.ln-read-marker-glow');
    ribbon.setAttribute('aria-label', text);
    if (state.tip && !state.tip.hidden) state.tip.textContent = text;
    const rect = charRect(at.node, at.offset);
    layer.dataset.placed = rect ? '1' : '0';
    if (rect) {
      const base = root.getBoundingClientRect(), box = at.paragraph.getBoundingClientRect(), x = rect.left - base.left, y = rect.top - base.top;
      // Pages: a paragraph's box spans its columns; the line's own start is the margin there.
      const line = isPages() ? (() => { const start = lineStart(at.paragraph, at.local); return start === null ? null : rectAt(at.paragraph, start); })() : null;
      if (vertical) {
        const top = (line?.top ?? box.top) - base.top;
        setBox(caret, x, y - 1, rect.width, 2);
        setBox(ribbon, x + rect.width * .2, top - 17, rect.width * .6, 12);
      } else {
        const left = (line?.left ?? box.left) - base.left;
        setBox(caret, x - 1, y, 2, rect.height);
        setBox(ribbon, left - 19, y + rect.height * .15, 11, rect.height * .7);
      }
      setBox(glow, x - 3, y - 3, Math.max(rect.width, 2) + 6, Math.max(rect.height, 2) + 6);
    }
    if (options.flash) flash(layer);
    syncJump();
    return true;
  }

  // A marker from the Mac (a save, the audio, the phone): kept on the book and drawn.
  function apply(next, options = {}) {
    if (!ui.lnBook || next === undefined) return;
    const previous = ui.lnBook.read_marker || null;
    ui.lnBook.read_marker = next || null;
    const book = (ui.lnState?.books || []).find(row => Number(row.id) === Number(ui.lnBook.id));
    if (book) book.read_marker = next || null;
    place({flash: !!next && options.flash === true && (options.always === true || !samePlace(previous, next))});
  }

  function showTip(anchor) {
    const marker = current(), host = shell();
    if (!marker || !host || !isOpen()) return;
    let tip = state.tip;
    if (!tip) { tip = document.createElement('div'); tip.id = 'lnReadMarkerTip'; tip.className = 'ln-read-marker-tip'; tip.setAttribute('role', 'tooltip'); state.tip = tip; }
    if (tip.parentNode !== host) host.append(tip);
    tip.textContent = label(marker);
    tip.hidden = false;
    const rect = anchor.getBoundingClientRect();
    tip.style.left = `${Math.max(8, Math.min(window.innerWidth - 320, rect.left)).toFixed(0)}px`;
    tip.style.top = `${Math.max(8, rect.bottom + 8).toFixed(0)}px`;
    anchor.setAttribute('aria-describedby', 'lnReadMarkerTip');
  }
  function hideTip() {
    clearTimeout(state.hoverTimer); state.hoverTimer = null;
    if (state.tip) state.tip.hidden = true;
  }

  // -1: the marker is before the view (already read), 1: after it, 0: on screen, null: unknown.
  function side(marker) {
    if (!ui.lnChapter) return null;
    if (!sameChapter(marker)) return Number(marker.chapter_index) < Number(ui.lnChapter.chapter_index) ? -1 : 1;
    const at = point(marker), rect = at ? charRect(at.node, at.offset) : null, view = scroller()?.getBoundingClientRect?.();
    if (!rect || !view || !(view.width > 0 && view.height > 0)) return null;
    if (isVertical()) return rect.left > view.right ? -1 : rect.right < view.left ? 1 : 0;
    if (isPages()) return rect.right < view.left ? -1 : rect.left > view.right ? 1 : 0;
    return rect.bottom < view.top ? -1 : rect.top > view.bottom ? 1 : 0;
  }
  function jumpButton() {
    let button = document.getElementById('lnReadMarkerJump');
    const host = shell();
    if (!button && host) {
      button = document.createElement('button');
      button.type = 'button'; button.id = 'lnReadMarkerJump'; button.className = 'ln-read-marker-jump'; button.hidden = true;
      button.innerHTML = '<span class="ln-read-marker-jump-arrow" aria-hidden="true"></span><span class="ln-read-marker-jump-label"></span>';
      button.addEventListener('click', () => { void jump(); });
      host.append(button);
    }
    return button;
  }
  function syncJump() {
    const button = jumpButton();
    if (!button) return;
    const marker = current(), where = isOpen() && marker ? side(marker) : null;
    button.hidden = !where;
    if (!where) return;
    const arrow = isVertical() ? (where < 0 ? '→' : '←') : isPages() ? (where < 0 ? '←' : '→') : (where < 0 ? '↑' : '↓');
    button.querySelector('.ln-read-marker-jump-arrow').textContent = arrow;
    button.querySelector('.ln-read-marker-jump-label').textContent = tr('ln.readMarker.back');
    button.dataset.side = where < 0 ? 'before' : 'after';
    button.title = label(marker);
    button.setAttribute('aria-label', `${tr('ln.readMarker.back')} · ${label(marker)}`);
  }
  async function jump() {
    const marker = current();
    if (!marker || !ui.lnBook) return;
    if (typeof markLnPairedManualNavigation === 'function') markLnPairedManualNavigation('read-marker', 3000);
    if (!sameChapter(marker)) {
      const loaded = await loadLightNovelChapter(Number(marker.chapter_index), null);
      if (loaded === false || !sameChapter(current())) return;
    }
    place();
    state.layer?.querySelector('.ln-read-marker-caret')?.scrollIntoView?.({block: 'center', inline: 'center', behavior: 'smooth'});
    flash(state.layer);
    syncJump();
  }

  // The paired audiobook reads `node` (a word or a paragraph): the marker follows its line when
  // that is far enough ahead (the Mac checks again and snaps it to the sentence).
  function followAudio(node) {
    const marker = current(), payload = ui.lnChapter;
    if (!node || !ui.lnBook || !payload || !isOpen() || typeof pywebview === 'undefined') return;
    if (ui.lnState?.settings?.auto_bookmarks === false) return;  // "Auto-bookmark after 20 s" off
    const paragraph = node.closest?.('p[data-ln-paragraph]');
    if (!paragraph) return;
    const word = node.closest?.('[data-ln-token]'), token = word ? ui.lnTokenMap?.get(String(word.dataset.lnToken || '')) : null;
    const local = Math.max(0, Number(token?.start || 0)), index = Number(paragraph.dataset.lnParagraph), spoken = charOf(index, local);
    if (spoken === null) return;
    const chapter = Number(payload.chapter_index);
    if (marker) {
      if (chapter < Number(marker.chapter_index)) return;  // never back
      if (chapter === Number(marker.chapter_index) && spoken - Number(marker.char_offset || 0) < Number(marker.auto_min_chars || DEFAULT_MIN_CHARS)) return;
    }
    const now = performance.now();
    if (state.audioBusy || now - state.audioAt < AUDIO_GAP_MS) return;
    const line = lineStart(paragraph, local), char = line === null ? spoken : charOf(index, line), bookId = Number(ui.lnBook.id);
    state.audioBusy = true; state.audioAt = now;
    Promise.resolve()
      .then(() => pywebview.api.light_novel_move_read_marker(bookId, chapter, char, 'auto'))
      .then(result => { if (Number(ui.lnBook?.id) === bookId) apply(result?.read_marker, {flash: result?.marker_moved === true}); })
      .catch(error => console.warn('LN read marker', error))
      .finally(() => { state.audioBusy = false; });
  }

  // A marker moved on the iPhone/iPad meanwhile: fetched when the window is back.
  async function refresh() {
    if (!isOpen() || !ui.lnBook || typeof pywebview === 'undefined' || typeof pywebview.api?.light_novel_read_marker !== 'function') return;
    const now = performance.now();
    if (now - state.refreshAt < REFRESH_GAP_MS) return;
    state.refreshAt = now;
    const bookId = Number(ui.lnBook.id);
    let result;
    try { result = await pywebview.api.light_novel_read_marker(bookId); } catch (_error) { return; }
    if (Number(ui.lnBook?.id) !== bookId || !result || result.read_marker === undefined) return;
    apply(result.read_marker, {flash: true});
  }

  function schedule() {
    if (state.frame) return;
    state.frame = requestAnimationFrame(() => { state.frame = null; place(); });
  }
  // Font size, width, furigana and window changes move the text: the marker is drawn again.
  function observe() {
    const root = reader();
    if (!root || state.observed === root || typeof ResizeObserver !== 'function') return;
    state.observer?.disconnect();
    state.observer = new ResizeObserver(() => schedule());
    state.observer.observe(root);
    state.observed = root;
  }

  // Pages: the columns are laid out as they come into view, so the marker is placed again there.
  scroller()?.addEventListener('scroll', () => {
    if (state.scrollTimer || !current()) return;
    state.scrollTimer = setTimeout(() => { state.scrollTimer = null; if (isPages()) place(); else syncJump(); }, SCROLL_SYNC_MS);
  }, {passive: true});
  // An illustration or a font that arrives later moves the text (in pages, into later columns).
  reader()?.addEventListener('load', event => { if (current() && event.target?.localName === 'img') schedule(); }, true);
  document.fonts?.addEventListener?.('loadingdone', () => { if (current()) schedule(); });
  window.addEventListener('focus', () => { void refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void refresh(); });

  window.PudgeLnReadMarker = {place, schedule, apply, label, point, lineStart, visibleChar, showPlace, followAudio, syncJump, jump, refresh, charOf};
})();
