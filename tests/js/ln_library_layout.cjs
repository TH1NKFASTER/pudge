'use strict';
// L1: tiles have one fixed slot set regardless of volume count; the side panel
// lists all volumes, continues the first unfinished one, closes on Escape and
// returns focus to its tile, and follows library updates.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const byId = new Map();
let focused = null;
function node(tag) {
  return {
    tag, hidden: false, _html: '', attrs: {}, dataset: {}, children: [],
    set id(v) { this._id = v; byId.set(v, this); }, get id() { return this._id; },
    set className(v) { this._class = v; },
    get innerHTML() { return this._html; }, set innerHTML(v) { this._html = String(v); },
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k]; },
    appendChild(child) { this.children.push(child); child.parent = this; return child; },
    querySelector(sel) { return sel === '[data-ln-volume-close]' && this._html.includes('data-ln-volume-close') ? {focus() { focused = 'close'; }} : null; },
  };
}
const listeners = {click: [], keydown: [], pointerdown: [], contextmenu: []};
const content = node('div'); content.id = 'lightNovelsContent'; content.parentElement = node('main');
const tiles = [];
const document = {
  documentElement: {lang: 'ru'},
  body: node('body'),
  createElement: tag => node(tag),
  getElementById: id => byId.get(id) || null,
  querySelectorAll: sel => (String(sel).includes('[data-ln-group-open]') ? tiles : []),
  addEventListener: (type, fn) => (listeners[type] ||= []).push(fn),
};

const book = (id, volume, extra = {}) => ({id, volume, title: `Том ${volume}`, series_title: 'Академия магии', series_key: 'academy', reading_progress: 0, ...extra});
const state = {books: []};
const ctx = {
  document, console, setTimeout: fn => fn(),
  escapeHtml: v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  lnBookCard: (b, compact) => `<article class="ln-card${compact ? ' compact' : ''}" data-ln-book="${b.id}">${b.title}</article>`,
  lnSeriesKey: b => String(b.series_key || b.id),
  currentSeriesBook: group => group.find(b => !b.finished) || group[group.length - 1],
  lnSeriesForKey: key => { const g = state.books.filter(b => String(b.series_key || b.id) === key); return g.length ? g : null; },
  anilistScoreChip: () => '',
  applyLnSelection: () => {},
  hydrateLnCardJiten: () => {},
};
ctx.globalThis = ctx;
ctx.PudgeLibraryShelves = {build: books => {
  const groups = new Map();
  for (const b of books) { const k = b.franchise || b.series_key || String(b.id); (groups.get(k) || groups.set(k, new Map()).get(k)); }
  const shelves = [];
  const byShelf = new Map();
  for (const b of books) {
    const shelfKey = b.franchise || b.series_key || String(b.id);
    if (!byShelf.has(shelfKey)) byShelf.set(shelfKey, new Map());
    const s = byShelf.get(shelfKey); const sk = b.series_key || String(b.id);
    if (!s.has(sk)) s.set(sk, []); s.get(sk).push(b);
  }
  for (const [key, series] of byShelf) shelves.push({key, title: key === 'tower' ? 'Хроники башни' : '', series: [...series.values()].map(books => ({title: books[0].series_title, books}))});
  return shelves;
}};
vm.createContext(ctx);
// Like index.html: `const ui` lives in the global lexical scope, not on window.
ctx.__state = state;
vm.runInContext("const ui = {lang: 'ru', lnState: __state};", ctx);
assert.equal(ctx.ui, undefined);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx, {filename: 'ln_library.js'});
const L = ctx.PudgeLnLibrary;

// 1. Single book stays a normal card.
assert.equal(L.seriesTileHtml([book(1, 1, {series_key: 'solo'})]), '<article class="ln-card" data-ln-book="1">Том 1</article>');

// 2. Series tile: identical structure for 2 and 20 volumes.
const series = n => Array.from({length: n}, (_, i) => book(100 + i, i + 1, {finished: i < 3}));
const small = L.seriesTileHtml(series(4));
const big = L.seriesTileHtml(series(20));
const shape = html => html.replace(/data-ln-series-ids="[^"]*"/, 'IDS').replace(/\d+/g, '#');
assert.equal(shape(small), shape(big), 'tile slots do not depend on volume count');
assert.equal((big.match(/<article/g) || []).length, 1);
assert.equal((big.match(/ln-card-cover/g) || []).length, 1);
assert.ok(!big.includes('data-ln-book='), 'volumes are not rendered inside the tile');
assert.match(big, /data-ln-group-open="series" data-ln-group-key="academy"/);
assert.match(big, /data-ln-series-ids="100,101,/);
assert.match(big, /<div class="ln-group-cover-col"><img class="ln-card-cover"|<div class="ln-group-cover-col"><div class="ln-card-cover/, 'Continue lives in the cover column');
assert.match(big, /<button type="button" class="ln-group-continue" data-ln-action="read" data-id="103" title="Продолжить · том 4"[^>]*>Продолжить<\/button><\/div>/);
assert.match(big, /Серия · 20 т\./);
assert.match(big, /tabindex="0" role="button" aria-haspopup="dialog"/);

// 3. Finished series: explicit state, no invented next volume.
const done = L.seriesTileHtml(series(3).map(b => ({...b, finished: true})));
assert.ok(!done.includes('data-ln-action="read"'));
assert.match(done, /<span class="ln-group-continue is-empty" aria-hidden="true"><\/span><\/div>/, 'finished tile keeps the slot');
assert.equal(shape(done).split('ln-group-cover-col').length, shape(small).split('ln-group-cover-col').length);
assert.match(done, /Прочитано/);

// 4. Franchise tile.
const fr = L.franchiseTileHtml({key: 'tower', title: 'Хроники башни', series: [
  {title: 'Часть 1', books: [book(201, 1, {series_key: 'a', finished: true}), book(202, 2, {series_key: 'a', finished: true})]},
  {title: 'Часть 2', books: [book(203, 1, {series_key: 'b'})]},
]});
assert.match(fr, /data-ln-group-open="franchise" data-ln-group-key="tower"/);
assert.match(fr, /data-ln-franchise-ids="201,202,203"/);
assert.match(fr, /Франшиза · 2 сер\./);
assert.match(fr, /data-id="203"/);

// 5. Panel: all volumes, continue, Escape closes and restores focus.
state.books = series(20);
tiles.push({dataset: {lnGroupOpen: 'series', lnGroupKey: 'academy'}, focus() { focused = 'tile'; }});
L.open('series', 'academy');
const panel = byId.get('lnVolumePanel');
assert.equal(panel.hidden, false);
assert.equal((panel.innerHTML.match(/class="ln-card compact"/g) || []).length, 20);
assert.match(panel.innerHTML, /data-ln-action="read" data-id="103"/);
assert.equal(focused, 'close');
// Escape goes through index.html's dispatcher -> closeIfOpen().
assert.equal(L.closeIfOpen(), true);
assert.equal(L.closeIfOpen(), false, 'nothing left to close');
assert.equal(panel.hidden, true);
assert.equal(focused, 'tile');
assert.equal(L.isOpen(), false);

// 6. Enter on a focused tile opens it; library updates refresh or close the panel.
const tileEl = {dataset: {lnGroupOpen: 'series', lnGroupKey: 'academy'}, closest: () => tileEl};
tileEl.target = tileEl;
for (const fn of listeners.keydown) fn({key: 'Enter', target: tileEl, preventDefault() {}, stopPropagation() {}});
assert.equal(L.isOpen(), true);
state.books = series(19);
L.refresh();
assert.equal((panel.innerHTML.match(/class="ln-card compact"/g) || []).length, 19);
state.books = [];
L.refresh();
assert.equal(L.isOpen(), false);
assert.equal(panel.hidden, true);

// 7. Franchise panel finds its shelf through the lexical `ui` (regression: it closed at once).
state.books = [book(301, 1, {series_key: 'a', franchise: 'tower'}), book(302, 1, {series_key: 'b', franchise: 'tower'})];
L.open('franchise', 'tower');
assert.equal(L.isOpen(), true, 'franchise panel opens');
assert.equal(panel.hidden, false);
assert.equal((panel.innerHTML.match(/class="ln-volume-section"/g) || []).length, 2);
assert.equal((panel.innerHTML.match(/class="ln-card compact"/g) || []).length, 2);

// 8. Pointer press outside the panel closes it; inside, on a tile or on an overlay it does not.
const press = sel => { const target = {closest: q => (q.split(',').includes(sel) ? {} : null)}; for (const fn of listeners.pointerdown) fn({button: 0, target}); };
press('#lnVolumePanel'); assert.equal(L.isOpen(), true, 'press inside keeps panel');
press('#contextMenu'); assert.equal(L.isOpen(), true, 'context menu keeps panel');
press('[data-ln-group-open]'); assert.equal(L.isOpen(), true, 'tile press keeps panel (click switches it)');
for (const fn of listeners.pointerdown) fn({button: 2, target: {closest: () => null}});
assert.equal(L.isOpen(), true, 'right button ignored');
press('#somewhereElse'); assert.equal(L.isOpen(), false, 'outside press closes panel');
assert.equal(panel.hidden, true);

// 9. No flicker: reopening the shown group or refreshing unchanged data keeps the same DOM.
state.books = series(5);
L.open('series', 'academy');
let writes = 0; let html = panel._html;
Object.defineProperty(panel, 'innerHTML', {get() { return html; }, set(v) { writes += 1; html = String(v); }, configurable: true});
L.open('series', 'academy');
L.refresh();
L.refresh();
assert.equal(writes, 0, 'unchanged panel is not rebuilt');
state.books = series(5).map(b => (b.id === 103 ? {...b, finished: true} : b));
L.refresh();
assert.equal(writes, 1, 'changed data rebuilds once');
L.close();

// 10. Leaving the page dismisses the panel without moving focus to a hidden tile.
state.books = series(5);
L.open('series', 'academy');
focused = null;
L.dismiss();
assert.equal(L.isOpen(), false);
assert.equal(panel.hidden, true);
assert.equal(focused, null, 'no focus restore on page change');
L.dismiss();

// 11. Continue on a tile (outside the panel) also leaves the volumes view.
L.open('series', 'academy');
const readTarget = {closest: q => (String(q).split(',').includes('[data-ln-action="read"]') ? {} : null)};
for (const fn of listeners.click) fn({target: readTarget, preventDefault() {}});
assert.equal(L.isOpen(), false);

// 12. Closing keeps the DOM; reopening the same group does not rebuild (covers stay cached).
state.books = series(4).map(b => ({...b, cover_url: `covers/ln-${b.id}.jpg`}));
ctx.lnBookCard = (b, compact) => `<article class="ln-card${compact ? ' compact' : ''}" data-ln-book="${b.id}"><img src="${b.cover_url}" loading="lazy"></article>`;
L.open('series', 'academy');
assert.ok(!panel.innerHTML.includes('loading="lazy"') && panel.innerHTML.includes('loading="eager"'), 'panel covers load eagerly');
let rewrites = 0; let kept = panel.innerHTML;
Object.defineProperty(panel, 'innerHTML', {get() { return kept; }, set(v) { rewrites += 1; kept = String(v); }, configurable: true});
L.close();
assert.equal(panel.hidden, true);
assert.ok(kept.includes('data-ln-book'), 'DOM kept while hidden');
L.open('series', 'academy');
assert.equal(panel.hidden, false);
assert.equal(rewrites, 0, 'reopen reuses DOM');
L.close();

// 13. A standalone series keeps its scrollable volume list and gets the panel hook.
ctx.lnSeriesGroupHtml = books => `<section class="ln-series-group" data-ln-series-key="academy" data-ln-series-ids="${books.map(b => b.id).join(',')}"><div class="ln-series-head"><div class="ln-series-title-block"><strong>Academy</strong></div><span class="ln-series-count">3</span></div><div class="ln-series-books">${books.map(b => ctx.lnBookCard(b, true)).join('')}</div></section>`;
const classic = L.seriesGroupHtml(series(3));
assert.match(classic, /^<section class="ln-series-group" data-ln-group-open="series" data-ln-group-key="academy" data-ln-series-key="academy"/);
assert.equal((classic.match(/data-ln-book=/g) || []).length, 3, 'volumes stay visible in the card');
assert.ok(!classic.includes('ln-series-continue'), 'finished series has no Continue');
assert.match(L.seriesGroupHtml(series(5)), /data-ln-action="read" data-id="103">Продолжить · том 4<\/button><\/div><span class="ln-series-count">/);
const shelfHtml = L.entryHtml({key: 'series:academy', series: [{title: 'x', books: series(3)}]});
assert.ok(shelfHtml.startsWith('<section class="ln-series-group"'), 'single-series shelf uses the classic card');
assert.ok(L.entryHtml({key: 'tower', title: 'T', series: [{title: 'a', books: series(2)}, {title: 'b', books: series(2)}]}).includes('ln-group-tile'), 'franchise stays a tile');
assert.equal(L.seriesGroupHtml([book(9, 1, {series_key: 'solo'})]).includes('data-ln-book="9"'), true, 'single book unchanged');

// 14. Panel and context menu dismiss each other.
let menuOpen = true;
byId.set('contextMenu', {classList: {contains: () => menuOpen}});
ctx.hideContextMenu = () => { menuOpen = false; };
state.books = series(4);
L.open('series', 'academy');
assert.equal(menuOpen, false, 'opening the panel closes the context menu');
const rc = sel => ({target: {closest: q => (q.split(',').includes(sel) ? {} : null)}});
for (const fn of listeners.contextmenu) fn(rc('#lnVolumePanel'));
assert.equal(L.isOpen(), true, 'context menu inside the panel keeps it');
for (const fn of listeners.contextmenu) fn(rc('[data-anime-card]'));
assert.equal(L.isOpen(), false, 'context menu elsewhere closes the panel');

// 15. Custom (manga) provider: rendered in the same panel, own Continue, afterRender hook.
let painted = 0;
L.openCustom('manga:berserk', () => ({title: 'Berserk', body: '<div class="ln-volume-grid"><article data-manga-book="5"></article></div>', continueHtml: '<button data-manga-v2-action="read" data-id="5">Continue</button>', afterRender: () => { painted += 1; }}));
assert.equal(L.isOpen(), true);
assert.match(panel.innerHTML, /data-manga-v2-action="read" data-id="5"/);
assert.ok(!panel.innerHTML.includes('data-ln-action="read"'));
assert.equal(painted, 1);
const mangaRead = {closest: q => (String(q).split(',').includes('[data-manga-v2-action="read"]') ? {} : null)};
for (const fn of listeners.click) fn({target: mangaRead, preventDefault() {}});
assert.equal(L.isOpen(), false, 'reading a manga volume leaves the panel');
console.log('LN library tiles: PASS');
