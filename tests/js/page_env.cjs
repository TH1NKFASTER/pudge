'use strict';
/* Window globals for a page running in a node vm context (audit 71Б).

Runs INSIDE the page's context (page.cjs evaluates it there), so every object it
creates belongs to the page: instanceof, Array and Promise behave as in a browser.
`host` carries the few things that must come from node (console, URL, crypto ...). */

module.exports = function install(sandbox, dom, opts, host) {
  // Inside the context the global object is a proxy of the sandbox: use the proxy, so
  // `event.target === window` and `doc.defaultView === window` hold for the page.
  const g = globalThis;
  void sandbox;
  const doc = dom.createDocument(opts.html);
  doc.defaultView = g;
  g.document = doc;
  g.window = g;
  g.self = g;
  g.top = g;
  g.parent = g;
  g.frames = g;
  g.globalThis = g;

  /* ---- errors seen by the page (uncaught in listeners, timers, scripts) */
  const errors = [];
  let reporting = false;
  g.__reportError = error => {
    errors.push(error);
    if (reporting) return;
    reporting = true;
    try {
      const event = new dom.Event('error', {cancelable: true});
      event.error = error;
      event.message = String(error && error.message || error);
      dispatchOnWindow(event);
    } finally { reporting = false; }
  };

  /* ---- CSS: only `display` is modelled, so tests can see what the stylesheets hide */
  const cssRules = opts.cssRules || [];
  let compiledCss = null;
  const specificity = sel => {
    const plainSel = sel.replace(/:where\([^)]*\)/g, '').replace(/:(?:not|is|has)\(/g, ' (');
    const ids = (plainSel.match(/#[\w-]+/g) || []).length;
    const classes = (plainSel.match(/\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+/g) || []).length;
    const tags = (plainSel.replace(/\[[^\]]*\]|\([^)]*\)/g, ' ').match(/(?:^|[\s>+~(])([a-z][a-z0-9]*)/gi) || []).length;
    return ids * 10000 + classes * 100 + tags;
  };
  const compileCss = () => {
    compiledCss = [];
    cssRules.forEach((rule, order) => {
      for (const selector of rule.selectors) {
        if (selector.includes('::') || /:(?:before|after|first-line|first-letter|placeholder|selection|-webkit-[\w-]+)\b/.test(selector)) continue;
        let list;
        try { list = dom.compileSelector(selector); } catch (_) { continue; }
        compiledCss.push({list, spec: specificity(selector), order, important: rule.important, display: rule.display});
      }
    });
  };
  let cssCache = new WeakMap(), cssVersion = -1;
  // The element's `display` from the stylesheets and its style attribute ('' when none applies).
  g.__cssDisplay = el => {
    if (!compiledCss) compileCss();
    if (cssVersion !== doc.__version) { cssCache = new WeakMap(); cssVersion = doc.__version; }
    if (cssCache.has(el)) return cssCache.get(el);
    let best = null;
    for (const rule of compiledCss) {
      if (best && (best.important && !rule.important)) continue;
      if (!rule.list.some(seq => dom.matchSeq(el, seq))) continue;
      if (!best || (rule.important && !best.important) || (rule.important === best.important && (rule.spec > best.spec || (rule.spec === best.spec && rule.order > best.order)))) best = rule;
    }
    let display = best ? best.display : '';
    const inline = el.__style ? el.__style.getPropertyValue('display') : (/(?:^|;)\s*display\s*:\s*([^;]+)/i.exec(el.__attrs.get('style') || '') || [])[1] || '';
    if (inline && !(best && best.important)) display = inline.trim().toLowerCase();
    cssCache.set(el, display);
    return display;
  };
  g.__cssHidden = el => g.__cssDisplay(el) === 'none';

  /* ---- window as an event target */
  const target = new dom.EventTarget();
  g.__listeners = target.__listeners;
  g.addEventListener = dom.EventTarget.prototype.addEventListener.bind(g);
  g.removeEventListener = dom.EventTarget.prototype.removeEventListener.bind(g);
  g.dispatchEvent = event => {
    event.target = g; event.__path = [g]; event.eventPhase = 2;
    g.__fire(event, 2);
    event.eventPhase = 0; event.currentTarget = null;
    return !event.defaultPrevented;
  };
  g.__fire = dom.EventTarget.prototype.__fire;
  g.__parentTarget = () => null;
  const dispatchOnWindow = event => g.dispatchEvent(event);

  /* ---- fake clock: timers run only when the test advances time */
  const clock = {now: 0, wall: opts.now ?? Date.UTC(2026, 9, 1, 12, 0, 0), timers: new Map(), nextId: 1};
  const schedule = (fn, ms, args, interval, kind) => {
    const id = clock.nextId++;
    const delay = Math.max(0, Number(ms) || 0);
    clock.timers.set(id, {id, due: clock.now + delay, fn, args, interval: interval ? Math.max(1, delay) : 0, kind});
    return id;
  };
  g.setTimeout = (fn, ms, ...args) => schedule(fn, ms, args, false, 'timeout');
  g.setInterval = (fn, ms, ...args) => schedule(fn, ms, args, true, 'interval');
  g.clearTimeout = id => { clock.timers.delete(id); };
  g.clearInterval = id => { clock.timers.delete(id); };
  g.requestAnimationFrame = fn => schedule(() => fn(g.performance.now()), 16, [], false, 'frame');
  g.cancelAnimationFrame = id => { clock.timers.delete(id); };
  g.requestIdleCallback = fn => schedule(() => fn({didTimeout: false, timeRemaining: () => 50}), 1, [], false, 'idle');
  g.cancelIdleCallback = id => { clock.timers.delete(id); };
  g.queueMicrotask = fn => { Promise.resolve().then(fn).catch(g.__reportError); };
  // Runs the earliest timer due at or before `limit`; false when none is due.
  clock.runNext = limit => {
    let next = null;
    for (const t of clock.timers.values()) if (t.due <= limit && (!next || t.due < next.due || (t.due === next.due && t.id < next.id))) next = t;
    if (!next) return false;
    clock.now = Math.max(clock.now, next.due);
    if (next.interval) next.due = clock.now + next.interval; else clock.timers.delete(next.id);
    try {
      if (typeof next.fn === 'function') next.fn(...next.args);
      else g.eval(String(next.fn));
    } catch (error) { g.__reportError(error); }
    return true;
  };
  clock.pending = () => [...clock.timers.values()].map(t => ({id: t.id, due: t.due - clock.now, kind: t.kind, interval: t.interval}));
  g.__clock = clock;
  g.performance = {now: () => 1000 + clock.now, mark() {}, measure() {}, getEntriesByName: () => [], getEntriesByType: () => [], timeOrigin: clock.wall, memory: {usedJSHeapSize: 0}};
  const RealDate = Date;
  class PageDate extends RealDate {
    constructor(...args) { if (args.length) super(...args); else super(clock.wall + clock.now); }
    static now() { return clock.wall + clock.now; }
  }
  g.Date = PageDate;

  /* ---- console */
  const consoleLog = [];
  const record = level => (...args) => {
    consoleLog.push({level, text: args.map(a => { try { return typeof a === 'string' ? a : a instanceof Error ? String(a.stack || a) : JSON.stringify(a); } catch (_) { return String(a); } }).join(' ')});
    if (opts.echoConsole) host.console[level === 'log' ? 'log' : 'error'](`[page ${level}]`, ...args);
  };
  g.console = {log: record('log'), info: record('info'), warn: record('warn'), error: record('error'), debug: record('debug'), trace: record('debug'), table: record('log'), group() {}, groupCollapsed() {}, groupEnd() {}, time() {}, timeEnd() {}, assert(ok, ...args) { if (!ok) record('error')('Assertion failed', ...args); }};

  /* ---- storage */
  class Storage {
    constructor(initial) { this.__data = new Map(Object.entries(initial || {}).map(([k, v]) => [k, String(v)])); }
    get length() { return this.__data.size; }
    key(k) { return [...this.__data.keys()][k] ?? null; }
    getItem(k) { return this.__data.has(String(k)) ? this.__data.get(String(k)) : null; }
    setItem(k, v) { this.__data.set(String(k), String(v)); }
    removeItem(k) { this.__data.delete(String(k)); }
    clear() { this.__data.clear(); }
  }
  g.Storage = Storage;
  g.localStorage = new Storage(opts.localStorage);
  g.sessionStorage = new Storage(opts.sessionStorage);

  /* ---- DOM classes and constructors */
  for (const name of ['Node', 'Text', 'Comment', 'Element', 'DocumentFragment', 'Document', 'Range', 'Selection', 'DOMTokenList', 'DOMParser',
    'Event', 'CustomEvent', 'UIEvent', 'KeyboardEvent', 'MouseEvent', 'PointerEvent', 'WheelEvent', 'FocusEvent', 'InputEvent', 'DragEvent', 'ClipboardEvent', 'EventTarget']) g[name] = dom[name];
  g.ErrorEvent = class ErrorEvent extends dom.Event { constructor(type, init = {}) { super(type, init); this.error = init.error ?? null; this.message = init.message ?? ''; } };
  g.PromiseRejectionEvent = class PromiseRejectionEvent extends dom.Event { constructor(type, init = {}) { super(type, init); this.reason = init.reason; this.promise = init.promise; } };
  const tagClass = (name, tags) => {
    const fn = function () { throw new TypeError('Illegal constructor'); };
    Object.defineProperty(fn, 'name', {value: name});
    Object.defineProperty(fn, Symbol.hasInstance, {value: x => x instanceof dom.Element && (!tags || tags.includes(x.localName))});
    g[name] = fn;
  };
  tagClass('HTMLElement'); tagClass('SVGElement', ['svg', 'path', 'circle', 'rect', 'g', 'line', 'polyline', 'polygon', 'text']);
  for (const [name, tags] of Object.entries({
    HTMLInputElement: ['input'], HTMLSelectElement: ['select'], HTMLOptionElement: ['option'], HTMLTextAreaElement: ['textarea'],
    HTMLButtonElement: ['button'], HTMLAnchorElement: ['a'], HTMLImageElement: ['img'], HTMLCanvasElement: ['canvas'],
    HTMLMediaElement: ['audio', 'video'], HTMLAudioElement: ['audio'], HTMLVideoElement: ['video'], HTMLDialogElement: ['dialog'],
    HTMLFormElement: ['form'], HTMLLabelElement: ['label'], HTMLDivElement: ['div'], HTMLSpanElement: ['span'], HTMLTemplateElement: ['template'],
    HTMLDetailsElement: ['details'], HTMLIFrameElement: ['iframe'], HTMLScriptElement: ['script'], HTMLStyleElement: ['style'],
  })) tagClass(name, tags);
  g.Image = function Image(width, height) { const el = doc.createElement('img'); if (width) el.setAttribute('width', width); if (height) el.setAttribute('height', height); return el; };
  g.Audio = function Audio(src) { const el = doc.createElement('audio'); if (src) el.setAttribute('src', src); el.paused = true; return el; };
  g.Option = function Option(text = '', value, defaultSelected, selected) { const el = doc.createElement('option'); el.textContent = text; if (value !== undefined) el.setAttribute('value', value); if (defaultSelected) el.setAttribute('selected', ''); if (selected) el.selected = true; return el; };
  g.NodeFilter = {SHOW_ALL: 0xFFFFFFFF, SHOW_ELEMENT: 0x1, SHOW_TEXT: 0x4, SHOW_COMMENT: 0x80, FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3};

  /* ---- observers: recorded so tests can drive them */
  const observers = {mutation: [], intersection: [], resize: []};
  g.__observers = observers;
  class MutationObserver {
    constructor(callback) { this.callback = callback; this.targets = []; this.queue = []; observers.mutation.push(this); }
    observe(node, options = {}) {
      const o = {...options};
      if (o.attributeFilter || o.attributeOldValue) o.attributes = o.attributes ?? true;
      if (o.characterDataOldValue) o.characterData = o.characterData ?? true;
      const at = this.targets.findIndex(t => t.node === node);
      if (at >= 0) this.targets[at].options = o; else this.targets.push({node, options: o});
    }
    disconnect() { this.targets = []; this.queue = []; }
    takeRecords() { const q = this.queue; this.queue = []; return q; }
  }
  g.MutationObserver = MutationObserver;
  let mutationScheduled = false, mutationRounds = 0;
  const wants = (options, record) => {
    if (record.type === 'childList') return Boolean(options.childList);
    if (record.type === 'attributes') return Boolean(options.attributes) && (!options.attributeFilter || options.attributeFilter.includes(record.attributeName));
    if (record.type === 'characterData') return Boolean(options.characterData);
    return false;
  };
  doc.__mutated = (node, record) => {
    let queued = false;
    for (const o of observers.mutation) {
      const match = o.targets.find(({node: t, options}) => (t === node || (options.subtree && t.contains(node))) && wants(options, record));
      if (!match) continue;
      if (o.queue.length < 500) {
        o.queue.push({
          type: record.type, target: node, addedNodes: record.addedNodes || [], removedNodes: record.removedNodes || [],
          attributeName: record.attributeName ?? null,
          oldValue: (record.type === 'attributes' && match.options.attributeOldValue) || (record.type === 'characterData' && match.options.characterDataOldValue) ? record.oldValue ?? null : null,
          previousSibling: null, nextSibling: null,
        });
        queued = true;
      }
    }
    if (!queued || mutationScheduled) return;
    mutationScheduled = true;
    Promise.resolve().then(() => {
      mutationScheduled = false;
      if (++mutationRounds > 200) return; // a callback that keeps changing what it observes
      for (const o of observers.mutation) {
        const records = o.takeRecords();
        if (records.length) { try { o.callback(records, o); } catch (error) { g.__reportError(error); } }
      }
    });
  };
  clock.resetMutationRounds = () => { mutationRounds = 0; };
  class IntersectionObserver {
    constructor(callback, options = {}) { this.callback = callback; this.options = options; this.targets = []; this.root = options.root ?? null; this.rootMargin = options.rootMargin ?? ''; this.thresholds = [].concat(options.threshold ?? 0); observers.intersection.push(this); }
    observe(el) { if (!this.targets.includes(el)) this.targets.push(el); }
    unobserve(el) { this.targets = this.targets.filter(x => x !== el); }
    disconnect() { this.targets = []; this.disconnected = true; }
    takeRecords() { return []; }
    // Test helper: report targets as (not) intersecting.
    __fire(entries) { this.callback(entries.map(e => ({isIntersecting: true, intersectionRatio: 1, boundingClientRect: e.target.getBoundingClientRect(), ...e})), this); }
  }
  g.IntersectionObserver = IntersectionObserver;
  class ResizeObserver {
    constructor(callback) { this.callback = callback; this.targets = []; observers.resize.push(this); }
    observe(el) { if (!this.targets.includes(el)) this.targets.push(el); }
    unobserve(el) { this.targets = this.targets.filter(x => x !== el); }
    disconnect() { this.targets = []; }
    __fire() { this.callback(this.targets.map(target => ({target, contentRect: target.getBoundingClientRect()})), this); }
  }
  g.ResizeObserver = ResizeObserver;

  /* ---- environment */
  const viewport = opts.viewport || {};
  g.innerWidth = viewport.width ?? 1300;
  g.innerHeight = viewport.height ?? 900;
  g.outerWidth = g.innerWidth; g.outerHeight = g.innerHeight;
  g.devicePixelRatio = viewport.devicePixelRatio ?? 2;
  g.screen = {width: 1728, height: 1117, availWidth: 1728, availHeight: 1080, colorDepth: 30};
  g.scrollX = 0; g.scrollY = 0; g.pageXOffset = 0; g.pageYOffset = 0;
  g.visualViewport = {width: g.innerWidth, height: g.innerHeight, scale: 1, offsetLeft: 0, offsetTop: 0, addEventListener() {}, removeEventListener() {}};
  const clipboard = [];
  g.__clipboard = clipboard;
  g.navigator = {
    platform: opts.platform ?? 'MacIntel', userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) dom_lite',
    language: 'en-US', languages: ['en-US', 'en'], onLine: true, hardwareConcurrency: 8, maxTouchPoints: 0, vendor: 'Apple Computer, Inc.',
    clipboard: {writeText: async text => { clipboard.push(String(text)); }, readText: async () => clipboard[clipboard.length - 1] ?? '', write: async () => {}},
    userAgentData: undefined, sendBeacon: () => true, vibrate: () => false, mediaSession: {setActionHandler() {}, metadata: null, playbackState: 'none', setPositionState() {}},
  };
  const pageUrl = opts.url || 'file:///pudge/web/' + (opts.name || 'index.html');
  g.location = {href: pageUrl, protocol: pageUrl.split(':')[0] + ':', host: '', hostname: '', port: '', pathname: pageUrl.replace(/^[a-z]+:\/\/[^/]*/, ''), search: '', hash: '', origin: 'null',
    reload() { g.__reloads = (g.__reloads || 0) + 1; }, assign(url) { g.__navigations.push(String(url)); }, replace(url) { g.__navigations.push(String(url)); }, toString() { return this.href; }};
  g.__navigations = [];
  g.history = {length: 1, state: null, pushState(state) { this.state = state; }, replaceState(state) { this.state = state; }, back() {}, forward() {}, go() {}};
  const media = opts.media || {};
  g.matchMedia = query => ({matches: Boolean(media[query]), media: String(query), onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent() { return true; }});
  g.getComputedStyle = el => new Proxy({}, {
    get(_, key) {
      if (key === 'getPropertyValue') return name => name === 'display' ? (el && el.__visible && !el.__visible() ? 'none' : (el && el.style && el.style.display) || 'block') : (el && el.style ? el.style.getPropertyValue(name) : '');
      if (typeof key !== 'string') return undefined;
      if (key === 'display') return el && el.__visible && !el.__visible() ? 'none' : (el && el.style && el.style.display) || 'block';
      if (key === 'visibility') return (el && el.style && el.style.visibility) || 'visible';
      return el && el.style ? el.style[key] || '' : '';
    },
  });
  g.getSelection = () => doc.getSelection();
  g.scrollTo = (x, y) => { if (x && typeof x === 'object') { g.scrollX = x.left ?? g.scrollX; g.scrollY = x.top ?? g.scrollY; } else { g.scrollX = x ?? 0; g.scrollY = y ?? 0; } };
  g.scroll = g.scrollTo;
  g.scrollBy = (x, y) => { if (x && typeof x === 'object') { g.scrollX += x.left || 0; g.scrollY += x.top || 0; } else { g.scrollX += x || 0; g.scrollY += y || 0; } };
  const dialogs = [];
  g.__dialogs = dialogs;
  g.alert = message => { dialogs.push({kind: 'alert', message: String(message)}); };
  g.confirm = message => { dialogs.push({kind: 'confirm', message: String(message)}); return opts.confirm ?? true; };
  g.prompt = (message, value) => { dialogs.push({kind: 'prompt', message: String(message)}); return opts.prompt ?? value ?? null; };
  g.__opened = [];
  g.open = (url, name) => { g.__opened.push({url: String(url ?? ''), name: name ?? ''}); return null; };
  g.close = () => { g.__closed = true; };
  g.focus = () => {}; g.blur = () => {}; g.print = () => {}; g.stop = () => {};
  g.postMessage = () => {};
  g.CSS = {escape: s => String(s).replace(/([\0-\x2c\x2e\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7f])/g, '\\$1'), supports: () => true};
  g.fetch = async url => { g.__fetches.push(String(url)); if (typeof opts.fetch === 'function') return opts.fetch(url); throw new TypeError('fetch is not available in tests'); };
  g.__fetches = [];
  g.XMLHttpRequest = function XMLHttpRequest() { throw new Error('XMLHttpRequest is not available in tests'); };
  g.Notification = Object.assign(function Notification() {}, {permission: 'denied', requestPermission: async () => 'denied'});
  g.speechSynthesis = {speak() {}, cancel() {}, getVoices: () => [], speaking: false, addEventListener() {}};
  g.SpeechSynthesisUtterance = function SpeechSynthesisUtterance(text) { this.text = text; };
  g.AudioContext = undefined; g.webkitAudioContext = undefined;
  g.FontFace = function FontFace() { this.load = async () => this; };
  g.BroadcastChannel = function BroadcastChannel() { this.postMessage = () => {}; this.close = () => {}; this.addEventListener = () => {}; };
  g.FileReader = function FileReader() { this.readAsDataURL = () => {}; this.readAsText = () => {}; this.addEventListener = () => {}; };
  g.DataTransfer = function DataTransfer() { this.items = []; this.files = []; this.types = []; this.getData = () => ''; this.setData = () => {}; };

  /* ---- things that must come from node */
  for (const name of ['URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'AbortController', 'AbortSignal', 'Blob', 'File', 'FormData', 'Headers', 'Request', 'Response', 'atob', 'btoa', 'structuredClone', 'crypto', 'Intl']) {
    if (host[name] !== undefined && g[name] === undefined) g[name] = host[name];
  }
  if (!g.crypto) g.crypto = host.crypto;

  /* ---- pywebview bridge: recorded calls, answers from the test */
  const calls = [];
  const answers = Object.create(null);
  g.__api = answers;
  g.__apiCalls = calls;
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const api = new Proxy({}, {
    get(_, name) {
      if (typeof name !== 'string' || name === 'then' || name === 'toJSON') return undefined;
      if (opts.missingApi && opts.missingApi.includes(name)) return undefined;
      return async (...args) => {
        calls.push({name, args: clone(args) ?? [], at: clock.now});
        const answer = answers[name];
        if (typeof answer === 'function') return await answer(...args);
        if (name in answers) return clone(answer);
        return clone(opts.apiDefault === undefined ? null : opts.apiDefault);
      };
    },
    has(_, name) { return typeof name === 'string' && !(opts.missingApi && opts.missingApi.includes(name)); },
  });
  if (opts.pywebview !== false) g.pywebview = {api, token: 'test-token', platform: 'cocoa'};

  return {doc, errors, consoleLog, clock, calls, answers};
};
