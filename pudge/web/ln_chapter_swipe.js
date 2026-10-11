'use strict';

/* Light novel reader: keep scrolling past the end of a chapter (down; leftwards in
   vertical text, rightwards in horizontal pages) to open the next one, like Firefox's
   two-finger back/forward swipe. Past the start opens the previous chapter at its end.
   As in Firefox the travel is short and the decision prompt: a quick flick turns the
   chapter at once, a slow pull turns it if the indicator is full when the gesture ends,
   otherwise the indicator springs straight back. */
(() => {
  const THRESHOLD = 72;      // accumulated overscroll (CSS px) that fills the indicator
  const EVENT_CAP = 36;      // one event adds at most this (a single mouse-wheel notch never fills it)
  const FLICK_WINDOW_MS = 60;
  const FLICK_SPEED = 1.6;   // px/ms over the last 60 ms, all forward: a flick turns the chapter at once
  const FLICK_MIN = 24;      // ... once at least this far
  const END_MS = 120;        // no wheel movement this long = gesture over (decide now)
  const FRESH_GAP_MS = 120;  // a gap this long starts a new gesture (momentum is continuous)
  const SHIFT = 88;          // how far the chip slides in from the top/bottom edge, px
  const SLIDE_MS = 200;
  const NOTICE_MS = 700;     // "Last chapter" stays this long after release
  const EDGE = 2;
  const ARROWS = {bottom: '↓', top: '↑', left: '←', right: '→'};
  const swipe = {dir: 0, acc: 0, target: null, side: '', armedDir: 0, lastWheelAt: -Infinity, idleTimer: null, hideTimer: null, busy: false, samples: [], swallow: false, swallowDir: 0, lastDir: 0, lastMag: 0};

  const scroller = () => document.getElementById('lnReaderScroll');
  const reader = () => document.getElementById('lnReader');
  const isVertical = () => reader()?.classList.contains('vertical') === true;
  const isPages = () => scroller()?.classList.contains('pages') === true;
  const reducedMotion = () => { try { return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true; } catch (_error) { return false; } };
  const tr = (key, params) => window.PudgeI18n ? PudgeI18n.t(key, params) : key;

  function edges(el) {
    if (isVertical()) {
      // The reader's own mapping of the sideways overflow (start at the right edge).
      const range = typeof lnVerticalScrollRange === 'function' ? lnVerticalScrollRange(el) : null;
      if (!range) return {end: true, start: true};
      return {end: el.scrollLeft - range.end <= EDGE, start: range.start - el.scrollLeft <= EDGE};
    }
    const maxX = Math.max(0, el.scrollWidth - el.clientWidth), maxY = Math.max(0, el.scrollHeight - el.clientHeight);
    if (isPages()) return {end: maxX <= EDGE || el.scrollLeft >= maxX - EDGE, start: maxX <= EDGE || el.scrollLeft <= EDGE};
    return {end: maxY <= EDGE || el.scrollTop >= maxY - EDGE, start: maxY <= EDGE || el.scrollTop <= EDGE};
  }

  // Distance moved in the reading direction (positive = forward), 0 for other axes.
  function forwardDelta(event, el) {
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? Math.max(1, el.clientHeight) : 1;
    const dx = Number(event.deltaX || 0) * unit, dy = Number(event.deltaY || 0) * unit;
    // Vertical text reads leftwards: only sideways movement (trackpad left = forward) swipes.
    if (isVertical()) return Math.abs(dx) >= Math.abs(dy) ? -dx : 0;
    if (isPages()) return Math.abs(dx) > Math.abs(dy) ? dx : dy;
    return Math.abs(dy) >= Math.abs(dx) ? dy : 0;
  }

  function side(dir) {
    if (isVertical()) return dir > 0 ? 'left' : 'right';
    if (isPages()) return dir > 0 ? 'right' : 'left';
    return dir > 0 ? 'bottom' : 'top';
  }

  function selecting() {
    if (ui?.lnPairedPointerScrolling) return true;
    const selection = window.getSelection?.();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
    const node = selection.getRangeAt(0)?.commonAncestorContainer;
    return !!node && !!reader()?.contains(node);
  }

  function chapterTitle(index) {
    const chapter = (ui?.lnBook?.chapters || []).find(row => Number(row.chapter_index) === index);
    return String(chapter?.title || tr('uiB.ln.chapterNumber', {number: index + 1}));
  }

  function targetChapter(dir) {
    const current = Number(ui?.lnChapter?.chapter_index), count = (ui?.lnBook?.chapters || []).length;
    if (!Number.isFinite(current)) return null;
    const next = current + dir;
    return next >= 0 && next < count ? next : null;
  }

  function layer() {
    let node = document.getElementById('lnChapterSwipe');
    if (!node) {
      node = document.createElement('div');
      node.id = 'lnChapterSwipe';
      node.className = 'ln-chapter-swipe';
      node.setAttribute('aria-live', 'polite');
      node.hidden = true;
      node.innerHTML = '<div class="ln-chapter-swipe-indicator"><span class="ln-chapter-swipe-chip"><span class="ln-chapter-swipe-arrow"></span></span><span class="ln-chapter-swipe-label"></span></div>';
      (document.getElementById('lnReaderShell') || document.body).appendChild(node);
    }
    return node;
  }

  // The indicator lives over the visible part of the reader's scroll box (never beyond the window).
  function placeLayer(node) {
    const rect = scroller()?.getBoundingClientRect?.();
    if (!rect) return;
    const width = window.innerWidth || rect.right, height = window.innerHeight || rect.bottom;
    const left = Math.max(0, rect.left), top = Math.max(0, rect.top);
    const right = Math.min(width, rect.right), bottom = Math.min(height, rect.bottom);
    Object.assign(node.style, {left: `${left}px`, top: `${top}px`, width: `${Math.max(0, right - left)}px`, height: `${Math.max(0, bottom - top)}px`});
  }

  function render() {
    const node = layer(), progress = Math.min(1, swipe.acc / THRESHOLD);
    // Follows the fingers one to one until full, then barely moves.
    const shift = SHIFT * progress + 10 * (1 - Math.exp(-Math.max(0, swipe.acc - THRESHOLD) / THRESHOLD));
    node.hidden = false;
    node.dataset.side = swipe.side;
    node.classList.remove('springing');
    node.classList.toggle('ready', swipe.target != null && progress >= 1);
    node.classList.toggle('edge', swipe.target == null);
    node.style.setProperty('--ln-swipe-shift', `${shift.toFixed(1)}px`);
    node.style.setProperty('--ln-swipe-progress', progress.toFixed(3));
    node.querySelector('.ln-chapter-swipe-arrow').textContent = swipe.target == null ? '' : ARROWS[swipe.side];
    node.querySelector('.ln-chapter-swipe-label').textContent = swipe.target == null
      ? tr(swipe.dir > 0 ? 'lnSwipe.lastChapter' : 'lnSwipe.firstChapter')
      : tr(swipe.dir > 0 ? 'lnSwipe.nextChapter' : 'lnSwipe.previousChapter', {title: chapterTitle(swipe.target)});
  }

  function hide(animated) {
    clearTimeout(swipe.hideTimer);
    const node = document.getElementById('lnChapterSwipe');
    if (!node) return;
    node.classList.remove('ready');
    if (!animated || reducedMotion()) { node.hidden = true; return; }
    node.classList.add('springing');
    node.style.setProperty('--ln-swipe-shift', '0px');
    node.style.setProperty('--ln-swipe-progress', '0');
    swipe.hideTimer = setTimeout(() => { node.hidden = true; node.classList.remove('springing'); }, 260);
  }

  function reset(animated = true) {
    clearTimeout(swipe.idleTimer);
    swipe.idleTimer = null;
    swipe.dir = 0; swipe.acc = 0; swipe.target = null; swipe.side = ''; swipe.samples = [];
    hide(animated);
  }

  function scheduleEnd() {
    clearTimeout(swipe.idleTimer);
    swipe.idleTimer = setTimeout(onEnd, END_MS);
  }

  function onEnd() {
    swipe.idleTimer = null;
    if (!swipe.dir) return;
    if (swipe.target != null && swipe.acc >= THRESHOLD) { void commit(); return; }
    // Short of it the indicator springs back at once; the last / first chapter notice stays a moment.
    if (swipe.target == null) swipe.idleTimer = setTimeout(() => reset(true), NOTICE_MS - END_MS);
    else reset(true);
  }

  // A quick flick in one direction turns the chapter without waiting for the gesture to end.
  function flicked(now) {
    const recent = swipe.samples.filter(sample => now - sample.at <= FLICK_WINDOW_MS);
    swipe.samples = recent;
    if (swipe.target == null || swipe.acc < FLICK_MIN || recent.length < 2 || recent.some(sample => sample.delta <= 0)) return false;
    const travelled = recent.reduce((sum, sample) => sum + sample.delta, 0);
    return travelled / (now - recent[0].at + 16) >= FLICK_SPEED;
  }

  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const frame = () => new Promise(resolve => requestAnimationFrame(() => resolve()));

  async function commit() {
    clearTimeout(swipe.idleTimer);
    swipe.idleTimer = null;
    const dir = swipe.dir, target = swipe.target, from = swipe.side, root = reader();
    if (swipe.busy || target == null || !root) return;
    swipe.busy = true;
    swipe.armedDir = 0; // the rest of this scroll stream must not swipe again
    swipe.swallow = true; // nor scroll the new chapter: the fling belongs to the swipe
    swipe.swallowDir = dir; // ... but a scroll the other way (back) is the user's at once
    const motion = !reducedMotion();
    let opened = false;
    try {
      if (typeof markLnPairedManualNavigation === 'function') markLnPairedManualNavigation('chapter', 5000);
      if (motion) {
        root.dataset.lnSwipeSide = from;
        root.classList.add('ln-chapter-swipe-out');
        await wait(SLIDE_MS);
      }
      hide(false);
      // The next chapter opens at its start, the previous one at its end.
      opened = await loadLightNovelChapter(target, dir > 0 ? 0 : 1) === true;
    } catch (error) {
      console.warn('LN chapter swipe failed', error);
    } finally {
      root.classList.remove('ln-chapter-swipe-out');
      if (motion && opened) {
        root.classList.add('ln-chapter-swipe-in');
        void root.offsetWidth;
        await frame();
        root.classList.remove('ln-chapter-swipe-in');
        root.classList.add('ln-chapter-swipe-settle');
        await wait(SLIDE_MS + 20);
        root.classList.remove('ln-chapter-swipe-settle');
      }
      delete root.dataset.lnSwipeSide;
      swipe.busy = false;
      reset(false);
    }
  }

  function onWheel(event) {
    const now = performance.now(), gap = now - swipe.lastWheelAt;
    swipe.lastWheelAt = now;
    const el = scroller();
    const delta = el ? forwardDelta(event, el) : 0, dir = delta > 0 ? 1 : delta < 0 ? -1 : 0;
    // Momentum never changes direction and only slows down: a scroll the other way, or one
    // that suddenly speeds up, is a new gesture even without a pause (no click needed).
    const reversed = dir !== 0 && swipe.lastDir !== 0 && dir !== swipe.lastDir;
    const rising = dir !== 0 && dir === swipe.lastDir && Math.abs(delta) > 2 * swipe.lastMag + 6;
    if (dir) { swipe.lastDir = dir; swipe.lastMag = Math.abs(delta); }
    let fresh = gap > FRESH_GAP_MS || reversed;
    if (swipe.swallow) {
      const theFling = dir === 0 || dir === swipe.swallowDir;
      if (swipe.busy || (theFling && gap <= FRESH_GAP_MS && !rising)) { event.preventDefault(); return; }
      swipe.swallow = false;
      fresh = true;
    }
    if (!el || swipe.busy || event.ctrlKey) return;
    if (!ui?.lnBook || !ui?.lnChapter || selecting()) { if (swipe.dir) reset(true); return; }
    if (!delta) return;
    if (fresh && !swipe.dir) {
      // Only a scroll that begins at the edge may swipe: inertia that carried the
      // reader to the end is not a request for the next chapter.
      const at = edges(el);
      swipe.armedDir = (dir > 0 ? at.end : at.start) ? dir : 0;
    }
    if (swipe.dir) {
      // The same gesture, its fling included, moves the indicator both ways.
      const along = delta * swipe.dir;
      swipe.acc += along > 0 ? Math.min(along, EVENT_CAP) : along;
      if (swipe.acc <= 0) { reset(false); return; } // pulled all the way back: an ordinary scroll again
      event.preventDefault();
      swipe.samples.push({at: now, delta: along});
      render();
      if (flicked(now)) { void commit(); return; }
      scheduleEnd();
      return;
    }
    if (swipe.armedDir !== dir) return;
    const at = edges(el);
    if (!(dir > 0 ? at.end : at.start)) { swipe.armedDir = 0; return; }
    event.preventDefault();
    clearTimeout(swipe.hideTimer);
    swipe.dir = dir;
    swipe.acc = Math.min(Math.abs(delta), EVENT_CAP);
    swipe.samples = [{at: now, delta: Math.abs(delta)}];
    swipe.target = targetChapter(dir);
    swipe.side = side(dir);
    placeLayer(layer());
    render();
    scheduleEnd();
  }

  function install() {
    const el = scroller();
    if (!el || el.dataset.lnChapterSwipe === '1') return;
    el.dataset.lnChapterSwipe = '1';
    el.addEventListener('wheel', onWheel, {passive: false});
  }

  window.PudgeLnChapterSwipe = {threshold: THRESHOLD, endMs: END_MS, reset: () => reset(false)};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
