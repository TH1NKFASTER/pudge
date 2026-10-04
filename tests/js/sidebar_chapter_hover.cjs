'use strict';
// Chapter hover in the main audiobook card mirrors onto the sidebar timeline.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.argv[2];

const classList = () => {
  const set = new Set();
  return {add:c => set.add(c), remove:c => set.delete(c), contains:c => set.has(c), toggle:(c, on) => (on ? set.add(c) : set.delete(c))};
};
const transport = [];
const transportApi = new Proxy({}, {get:(_, name) => (...args) => { transport.push([name, ...args]); return Promise.resolve({}); }});

function sidebarHarness() {
  const marker = {classList:classList(), style:{}};
  const timeline = {max:'', value:''};
  const article = {isConnected:true, dataset:{scBook:'7', shellKey:'7:0'}, classList:classList(),
    querySelector:sel => sel === '[data-sc-chapter-hover]' ? marker : sel === '[data-sc-audio-timeline]' ? timeline : null};
  let shellWrites = 0;
  const host = {hidden:false, childElementCount:1, querySelector:sel => sel === '.sidebar-audio' ? article : null,
    set innerHTML(v) { shellWrites++; }, get innerHTML() { return ''; }};
  const root = {isConnected:true, querySelector:sel => sel === '[data-sidebar-audio]' ? host : sel === '.sidebar-audio' ? article : null};
  const context = {console, setTimeout:() => 1, clearTimeout() {}, requestAnimationFrame() {}, cancelAnimationFrame() {},
    Date, performance:{now:() => 0}, localStorage:{getItem:() => null, setItem() {}},
    document:{documentElement:{lang:'en', classList:{contains:() => false}}, body:{}, activeElement:null, hidden:false,
      addEventListener() {}, querySelector:() => null, querySelectorAll:() => [], getElementById:() => null},
    addEventListener() {}, ui:{lang:'en'}};
  context.window = context;
  vm.createContext(context);
  let source = fs.readFileSync(path.join(web, 'sidebar_companion.js'), 'utf8');
  const exportMarker = '  g.PudgeSidebarCompanion = {';
  assert(source.includes(exportMarker));
  source = source.replace(exportMarker, `g.sidebarTest = {updateAudio, renderAudio,
    set(v) { if ('root' in v) root = v.root; if ('audio' in v) audioState = v.audio; }, hover:() => chapterHover};\n${exportMarker}`);
  source = source.replace('function renderDue() {', 'function renderDue() { return;');
  vm.runInContext(source, context);
  context.pywebview = {api:transportApi};
  context.sidebarTest.set({root});
  const book = (id, extra = {}) => ({id, playing:false, player_running:true, duration:100, position:5, chapters:[], ...extra});
  return {context, marker, article, test:context.sidebarTest, sc:context.PudgeSidebarCompanion, book, shellWrites:() => shellWrites};
}

(() => {
  const h = sidebarHarness();
  assert.equal(typeof h.sc.showChapterHoverRange, 'function');
  assert.equal(typeof h.sc.clearChapterHoverRange, 'function');
  // Same (paused) book: shows, clipped to duration.
  h.test.set({audio:{books:[h.book(7)]}});
  h.test.renderAudio();
  const writes = h.shellWrites();
  assert.equal(h.sc.showChapterHoverRange({audiobookId:7, start:20, end:40, source:'a'}), true);
  assert(h.marker.classList.contains('show'));
  assert.equal(h.marker.style.left, '20%');
  assert.equal(h.marker.style.width, '20%');
  // Last chapter clipped to duration.
  h.sc.showChapterHoverRange({audiobookId:7, start:90, end:150, source:'b'});
  assert.equal(h.marker.style.left, '90%');
  assert.equal(h.marker.style.width, '10%');
  // Late leave of an older chapter does not clear the newer hover.
  assert.equal(h.sc.clearChapterHoverRange('a'), false);
  assert(h.marker.classList.contains('show'));
  assert.equal(h.sc.clearChapterHoverRange('b'), true);
  assert(!h.marker.classList.contains('show'));
  // Different audiobook: nothing highlighted, active book untouched.
  assert.equal(h.sc.showChapterHoverRange({audiobookId:8, start:0, end:10, source:'c'}), false);
  assert(!h.marker.classList.contains('show'));
  assert.equal(h.test.hover(), null);
  // end <= start and invalid values hide.
  assert.equal(h.sc.showChapterHoverRange({audiobookId:7, start:30, end:30, source:'d'}), false);
  assert.equal(h.sc.showChapterHoverRange({audiobookId:7, start:'x', end:30, source:'e'}), false);
  assert(!h.marker.classList.contains('show'));
  // Zero duration hides.
  h.test.set({audio:{books:[h.book(7, {duration:0})]}});
  h.test.renderAudio();
  assert.equal(h.sc.showChapterHoverRange({audiobookId:7, start:1, end:2, source:'f'}), false);
  assert(!h.marker.classList.contains('show'));
  // Re-render (poll) keeps the hover; a sidebar book switch clears it.
  h.test.set({audio:{books:[h.book(7, {playing:true})]}});
  h.sc.showChapterHoverRange({audiobookId:7, start:10, end:20, source:'g'});
  h.test.renderAudio();
  assert(h.marker.classList.contains('show'));
  h.article.dataset.scBook = '9'; h.article.dataset.shellKey = '9:0';
  h.test.set({audio:{books:[h.book(9, {playing:true})]}});
  h.test.renderAudio();
  assert(!h.marker.classList.contains('show'));
  assert.equal(h.test.hover(), null);
  // No active book clears state.
  h.article.dataset.scBook = '7'; h.article.dataset.shellKey = '7:0';
  h.test.set({audio:{books:[h.book(7)]}});
  h.sc.showChapterHoverRange({audiobookId:7, start:10, end:20, source:'h'});
  h.test.set({audio:{books:[]}});
  h.test.renderAudio();
  assert.equal(h.test.hover(), null);
  // Hover never rebuilt the shell (same shell key) and called zero transport APIs.
  assert.equal(h.shellWrites() - writes, 1); // only the "no book" branch emptied it
  assert.deepEqual(transport, []);
})();

(() => {
  // media.js: hover forwards card id and chapter range; leave/toggle/re-render clear by source.
  const media = fs.readFileSync(path.join(web, 'media.js'), 'utf8');
  const helpersStart = media.indexOf('  // Mirror of the hovered chapter');
  const helpersEnd = media.indexOf('  const renderAudio = () => {');
  const hoverStart = media.indexOf('  const showSidebarChapterHover = chapter => {');
  const hoverEnd = media.indexOf('  const persistAudioBookmarkOrder');
  assert(helpersStart > 0 && helpersEnd > helpersStart && hoverStart > helpersEnd && hoverEnd > hoverStart);
  const listeners = new Map();
  const calls = [];
  const context = {console, document:{addEventListener:(name, cb) => listeners.set(name, cb)},
    PudgeSidebarCompanion:{showChapterHoverRange:arg => calls.push(['show', arg]), clearChapterHoverRange:src => calls.push(['clear', src])}};
  context.window = context;
  vm.createContext(context);
  vm.runInContext(`${media.slice(helpersStart, helpersEnd)}${media.slice(hoverStart, hoverEnd)}
    globalThis.mediaHover = {dropDetachedSidebarChapterHover};`, context);
  const marker = {classList:classList(), style:{}};
  const timeline = {dataset:{duration:'100'}, querySelector:() => marker};
  const card = {dataset:{audiobookId:'7'}, querySelector:sel => sel === '[data-audio-timeline]' ? timeline : marker};
  const chapter = (start, end) => {
    const node = {isConnected:true, dataset:{audioChapterStart:String(start), audioChapterEnd:String(end)},
      closest:sel => sel === '.audiobook-card' ? card : sel === '[data-audio-chapter-start]' ? node : null, contains:other => other === node};
    return node;
  };
  const a = chapter(10, 20), b = chapter(20, 30);
  listeners.get('pointerover')({target:a, relatedTarget:null});
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), ['show', {audiobookId:7, start:10, end:20, source:'media-chapter:1'}]);
  assert(marker.classList.contains('show'));
  listeners.get('pointerover')({target:b, relatedTarget:a});
  listeners.get('pointerout')({target:a, relatedTarget:b}); // late leave of A after B
  assert.equal(calls.filter(c => c[0] === 'clear').length, 0);
  listeners.get('pointerout')({target:b, relatedTarget:null});
  assert.deepEqual(calls.at(-1), ['clear', 'media-chapter:2']);
  // Closing the chapter details with the pointer inside clears.
  listeners.get('pointerover')({target:a, relatedTarget:null});
  const details = {open:false, matches:sel => sel === '.audiobook-chapters', contains:node => node === a};
  listeners.get('toggle')({target:details});
  assert.deepEqual(calls.at(-1), ['clear', 'media-chapter:3']);
  // Re-render removing the hovered node clears.
  listeners.get('pointerover')({target:b, relatedTarget:null});
  b.isConnected = false;
  context.mediaHover.dropDetachedSidebarChapterHover();
  assert.deepEqual(calls.at(-1), ['clear', 'media-chapter:4']);
  assert.equal(media.slice(hoverStart, hoverEnd).includes('pywebview'), false);
  assert.equal(media.slice(helpersStart, helpersEnd).includes('pywebview'), false);
})();
console.log('ok');
