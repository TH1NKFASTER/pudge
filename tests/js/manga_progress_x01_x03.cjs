'use strict';
// COMPLIANCE X01: a failed manga progress save must not be remembered as saved
// (the next identical call retries) and the failure is surfaced.
// COMPLIANCE X03: vertical mode picks the most visible page among ALL tracked
// pages, not only among the entries of the latest IntersectionObserver batch.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.argv[2];
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
function classes() {
  const values = new Set();
  return {add: (...xs) => xs.forEach(x => values.add(x)), remove: (...xs) => xs.forEach(x => values.delete(x)), contains: x => values.has(x),
    toggle: (x, on) => { if (on ?? !values.has(x)) values.add(x); else values.delete(x); }};
}
function element() {
  return {dataset: {}, style: {setProperty() {}, removeProperty() {}}, classList: classes(), hidden: false, isConnected: true,
    innerHTML: '', textContent: '', querySelector: () => null, querySelectorAll: () => [], replaceChildren() {},
    setAttribute() {}, toggleAttribute() {}, addEventListener() {}, getBoundingClientRect: () => ({top: 0, bottom: 100, width: 100, height: 100}),
    scrollIntoView() {}};
}
function manga(api = {}) {
  const nodes = new Map(), observers = [], toasts = [];
  const doc = {documentElement: {lang: 'en', classList: classes(), style: {setProperty() {}}}, body: element(), hidden: false, activeElement: null,
    getElementById: id => nodes.get(id) || null, querySelector: () => null, querySelectorAll: () => [], createElement: element, addEventListener() {}};
  const context = {console: {...console, warn() {}}, document: doc, localStorage: {getItem: () => null, setItem() {}},
    setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    queueMicrotask() {}, performance: {now: () => 1000}, getComputedStyle: () => ({}), CustomEvent: function () {},
    MutationObserver: class { observe() {} },
    IntersectionObserver: class { constructor(callback, options) { this.callback = callback; this.options = options; observers.push(this); } observe() {} disconnect() {} },
    HTMLInputElement: class {}, HTMLSelectElement: class {}, addEventListener() {}, dispatchEvent() {}, innerWidth: 1000, innerHeight: 800,
    toast: message => toasts.push(String(message))};
  context.window = context;
  vm.createContext(context);
  vm.runInContext("const ui={lang:'en',state:{settings:{}},lnState:{settings:{study_backend:'jiten'}}};", context);
  for (const id of ['mangaReaderV2', 'mangaV2Pages', 'mangaV2PageLabel', 'mangaV2Viewport']) nodes.set(id, element());
  Object.assign(nodes.get('mangaV2Viewport'), {scrollTop: 0, clientHeight: 500, scrollHeight: 20000});
  context.pywebview = {api};
  context.PudgeReviewGate = {require: async () => true};
  const source = fs.readFileSync(path.join(web, 'manga_reader_v2.js'), 'utf8').replace('  window.PudgeMangaReaderV2 = {',
    `  window.mangaTest={setResumePage,markReadThrough,renderVertical,
    set(book,page=0,mode='single'){currentBook=book;currentPage=page;approvedPage=page;currentPageCount=Number(book?.page_count||0);settings.mode=mode;pageCache=new Map();},
    state:()=>({book:currentBook,page:currentPage})};\n  window.PudgeMangaReaderV2 = {`);
  vm.runInContext(source, context, {filename: 'manga_reader_v2.js'});
  return {test: context.mangaTest, nodes, observers, toasts};
}

(async () => {
  // ---- X01: failed saves are retried by the next identical call ----------
  let failing = true;
  const positionCalls = [], markCalls = [];
  const h = manga({
    manga_set_position: async (id, index) => { positionCalls.push(index); if (failing) throw new Error('bridge down'); return {book_id: id, page_index: index}; },
    manga_mark_read: async (id, index) => { markCalls.push(index); if (failing) throw new Error('bridge down'); return {id, read_pages: index + 1}; },
  });
  const book = {id: 1, page_count: 20, position: 4, read_pages: 10};
  h.test.set(book, 4);
  await h.test.setResumePage(7);
  await h.test.setResumePage(7);
  assert.deepEqual(positionCalls, [7, 7], 'identical position save is retried after a failure');
  assert.equal(book.position, 4, 'failed position save does not move the saved position');
  await h.test.markReadThrough(14);
  await h.test.markReadThrough(14);
  assert.deepEqual(markCalls, [14, 14], 'identical read-through save is retried after a failure');
  assert.equal(book.read_pages, 10, 'failed read-through does not advance read_pages');
  assert.equal(h.toasts.length, 1, 'failure is surfaced once per failure streak');
  assert.match(h.toasts[0], /Could not save manga progress: bridge down/);

  failing = false;
  await h.test.setResumePage(7);
  await h.test.markReadThrough(14);
  assert.equal(book.position, 7, 'successful save commits the position');
  assert.equal(book.read_pages, 15, 'successful save commits read_pages');
  await h.test.setResumePage(7);
  await h.test.markReadThrough(14);
  assert.deepEqual(positionCalls, [7, 7, 7], 'already saved position is not re-sent');
  assert.deepEqual(markCalls, [14, 14, 14], 'already saved read-through is not re-sent');

  // A failure after recovery is surfaced again.
  failing = true;
  await h.test.markReadThrough(16);
  assert.equal(h.toasts.length, 2);
  assert.equal(book.read_pages, 15);

  // Concurrent identical calls share the in-flight save (no duplicate POST).
  const replies = [];
  const c = manga({
    manga_set_position: (id, index) => { const r = deferred(); replies.push({kind: 'pos', index, ...r}); return r.promise; },
    manga_mark_read: (id, index) => { const r = deferred(); replies.push({kind: 'mark', index, ...r}); return r.promise; },
  });
  const cb = {id: 2, page_count: 20, position: 0, read_pages: 0};
  c.test.set(cb, 0);
  const p1 = c.test.setResumePage(3), p2 = c.test.setResumePage(3);
  const m1 = c.test.markReadThrough(5), m2 = c.test.markReadThrough(5);
  assert.deepEqual(replies.map(r => `${r.kind}:${r.index}`), ['pos:3', 'mark:5'], 'in-flight identical saves are not duplicated');
  assert.equal(cb.position, 0, 'nothing committed before the bridge confirms');
  assert.equal(cb.read_pages, 0);
  replies[0].reject(new Error('offline'));
  replies[1].reject(new Error('offline'));
  await Promise.all([p1, p2, m1, m2]);
  c.test.setResumePage(3); c.test.markReadThrough(5);
  assert.deepEqual(replies.map(r => `${r.kind}:${r.index}`), ['pos:3', 'mark:5', 'pos:3', 'mark:5'], 'rejected in-flight saves are retried');
  replies[2].resolve({}); replies[3].resolve({id: 2, read_pages: 6});
  await flush();
  assert.equal(cb.position, 3);
  assert.equal(cb.read_pages, 6);

  // ---- X03: most visible page among all tracked pages ---------------------
  const v = manga({manga_mark_read: async (id, index) => ({id, read_pages: index + 1}), manga_set_position: async () => ({})});
  const frames = [0, 1, 2].map(i => Object.assign(element(), {dataset: {pageIndex: String(i)}}));
  v.nodes.get('mangaV2Pages').querySelectorAll = () => frames;
  v.test.set({id: 3, page_count: 10, position: 0, read_pages: 0}, 0, 'vertical');
  v.test.renderVertical();
  const visible = v.observers.find(observer => observer.options?.threshold);
  visible.callback([{target: frames[0], isIntersecting: true, intersectionRatio: 0.9}, {target: frames[1], isIntersecting: true, intersectionRatio: 0.1}]);
  await flush();
  assert.equal(v.test.state().page, 0);
  // Only page 1 changed (to 25%); page 0 is still 90% visible.
  visible.callback([{target: frames[1], isIntersecting: true, intersectionRatio: 0.25}]);
  await flush();
  assert.equal(v.test.state().page, 0, 'a less visible changed page does not become current');
  // Page 0 scrolls mostly out, page 1 is now dominant.
  visible.callback([{target: frames[0], isIntersecting: true, intersectionRatio: 0.2}, {target: frames[1], isIntersecting: true, intersectionRatio: 0.75}]);
  await flush();
  assert.equal(v.test.state().page, 1);
  // Page 0 leaves; page 2 enters slightly — page 1 (unchanged, 75%) stays current.
  visible.callback([{target: frames[0], isIntersecting: false, intersectionRatio: 0}, {target: frames[2], isIntersecting: true, intersectionRatio: 0.05}]);
  await flush();
  assert.equal(v.test.state().page, 1, 'unchanged dominant page stays current');
  console.log('manga progress X01/X03: PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
