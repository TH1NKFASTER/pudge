'use strict';
/* Run a Pudge window page (index.html, mpv_assistant.html, audiobook_float.html) in
node for behaviour tests (audit 71Б).

  const {openPage} = require('./page.cjs');
  const page = await openPage({lang: 'ru', state: {...}, api: {get_state: ...}});
  await page.run(() => renderLibrary());          // or a string of code
  page.text('#library');  page.click('[data-action=play-library]');  await page.tick(500);

The page's scripts run in their own vm context with dom_lite (no layout, no CSS);
pywebview.api calls are recorded in page.calls and answered from `api` (values or
functions) or `apiDefault`. Timers only run when the test calls tick(). Errors the
page did not handle are collected in page.errors (load() throws on them unless
allowErrors is set). See tests/page_harness.py for the Python side. */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = path.resolve(__dirname, '..', '..', 'pudge', 'web');
const DOM_SOURCE = fs.readFileSync(path.join(__dirname, 'dom_lite.cjs'), 'utf8');
const ENV_SOURCE = fs.readFileSync(path.join(__dirname, 'page_env.cjs'), 'utf8');
const scriptCache = new Map();
const fileCache = new Map();

function readWeb(web, name) {
  const file = path.join(web, name);
  const stat = fs.statSync(file);
  const key = `${file}:${stat.mtimeMs}:${stat.size}`;
  if (!fileCache.has(key)) fileCache.set(key, fs.readFileSync(file, 'utf8'));
  return fileCache.get(key);
}

function compiled(source, filename) {
  const key = filename + '\u0000' + source.length + '\u0000' + source.slice(0, 64) + source.slice(-64);
  let entry = scriptCache.get(key);
  if (!entry || entry.source !== source) {
    entry = {source, script: new vm.Script(source, {filename})};
    scriptCache.set(key, entry);
  }
  return entry.script;
}

function runModule(context, source, filename) {
  const wrapped = `(function (module, exports) {${source}\n;return module.exports;})`;
  const fn = compiled(wrapped, filename).runInContext(context);
  const module = {exports: {}};
  return fn(module, module.exports);
}

// A state that lets index.html start; tests pass their own (merged over this).
function defaultState(lang) {
  return {
    settings: {
      language: lang, onboarding_completed: true, permissions_requested: true, power_policy: {mode: 'normal'},
      light_novels: {}, version: '0.0.0-test', torrents_enabled: false, notifications_enabled: true,
    },
    current: [], planned: [], library: [], downloads: [], completed: [], dropped: [], paused: [],
    home: {new_ready: [], completed_ready: [], continue_watching: [], needs_action: [], waiting: [], download_available: [], caught_up: [], recently_watched: [], dropped: []},
    ready_state_version: '1', ui_state_version: '1',
  };
}

// What the backend answers at start-up when nothing is going on (shapes as in web_app.py),
// so a test only answers the calls it is about. Tests override any of them with `api`.
function startupAnswers(state, copy) {
  return {
    get_state: copy, get_state_fast: copy,
    ui_state_versions: {ready: String(state.ready_state_version ?? '1'), ui: String(state.ui_state_version ?? '1')},
    startup_maintenance: () => ({skipped: true, running: false, done: true, stats: {}, error: '', state: copy()}),
    startup_maintenance_status: () => ({running: false, done: true, stats: {}, error: '', state: copy()}),
    request_permissions: () => ({settings: copy().settings, folders: {}, notifications: {supported: true, granted: true}}),
    planning_episode_download_status: {running: false, episodes: []},
    get_relation_graph_cache: {graphs: {}, media_to_graph: {}},
    light_novel_state: {version: 1, settings: {}, books: []},
    manga_state: {books: [], ocr_available: false},
    audiobook_state: {books: []},
    bind_drop_import: {ok: true, bound: true},
    content_review_begin: {granted: true, blocking: false},
    review_gate_begin: {granted: true, blocking: false},
    poll_planned_release_discovery: {items: []},
    poll_anilist_related_media_notifications: {notifications: []},
  };
}

function mergeState(base, extra) {
  if (!extra) return base;
  const out = {...base, ...extra};
  if (extra.settings) out.settings = {...base.settings, ...extra.settings};
  if (extra.home) out.home = {...base.home, ...extra.home};
  return out;
}

// Rules that set `display`, from CSS text (comments stripped; @media blocks kept when they
// match the viewport width; other at-rules skipped).
const cssCache = new Map();
function displayRules(text, width) {
  const key = text.length + ':' + width + ':' + text.slice(0, 80);
  if (cssCache.has(key) && cssCache.get(key).text === text) return cssCache.get(key).rules;
  const css = text.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [];
  const mediaMatches = query => {
    const q = query.toLowerCase();
    if (/print/.test(q) && !/screen/.test(q)) return false;
    if (/prefers-reduced-motion\s*:\s*reduce|prefers-color-scheme\s*:\s*light|hover\s*:\s*none|pointer\s*:\s*coarse/.test(q)) return false;
    for (const m of q.matchAll(/max-width\s*:\s*(\d+)px/g)) if (width > Number(m[1])) return false;
    for (const m of q.matchAll(/min-width\s*:\s*(\d+)px/g)) if (width < Number(m[1])) return false;
    return true;
  };
  const walk = (body, active) => {
    let k = 0;
    while (k < body.length) {
      const open = body.indexOf('{', k);
      if (open < 0) break;
      const head = body.slice(k, open).trim();
      let depth = 1, end = open + 1;
      for (; end < body.length && depth; end++) { if (body[end] === '{') depth++; else if (body[end] === '}') depth--; }
      const inner = body.slice(open + 1, end - 1);
      k = end;
      if (head.startsWith('@')) {
        if (/^@media/i.test(head)) walk(inner, active && mediaMatches(head.slice(6)));
        else if (/^@(supports|layer|container)/i.test(head)) walk(inner, active);
        continue;
      }
      if (!active) continue;
      const decl = /(?:^|;)\s*display\s*:\s*([^;!]+?)\s*(!important)?\s*(?:;|$)/i.exec(inner);
      if (!decl) continue;
      const selectors = head.split(',').map(x => x.trim()).filter(Boolean);
      rules.push({selectors, display: decl[1].trim().toLowerCase(), important: Boolean(decl[2])});
    }
  };
  walk(css, true);
  cssCache.set(key, {text, rules});
  return rules;
}

const flushOnce = () => new Promise(resolve => setImmediate(resolve));
const sharedSelectorCache = new Map();
let activePage = null;
process.on('unhandledRejection', reason => {
  if (!activePage) throw reason;
  const error = reason instanceof Error || (reason && reason.stack) ? reason : new Error(`unhandled rejection: ${String(reason)}`);
  activePage.errors.push(error);
  try {
    const event = new activePage.window.PromiseRejectionEvent('unhandledrejection', {cancelable: true, reason});
    activePage.window.dispatchEvent(event);
  } catch (_) { /* the page is gone */ }
});

class Page {
  constructor(options) {
    this.options = options;
    this.web = options.web || WEB;
    this.name = options.name || 'index.html';
    this.lang = options.lang === 'ru' ? 'ru' : 'en';
  }

  async load() {
    activePage = this;
    const o = this.options;
    let html = readWeb(this.web, this.name);
    const bootstrap = o.bootstrap || {schema: 1, values: {}};
    html = html.split('__APP_NAME__').join(o.brand || 'Pudge')
      .split('__PUDGE_HTTP_TOKEN__').join('test-token')
      .split('__PUDGE_UI_BOOTSTRAP__').join(JSON.stringify(bootstrap).replace(/</g, '\\u003c'));
    // The page's stylesheets, in document order, for the `display` cascade.
    const width = (o.viewport && o.viewport.width) || 1300;
    const cssRules = [];
    if (o.css !== false) {
      const sheets = [...html.matchAll(/<link[^>]+rel=["']stylesheet["'][^>]*>|<style[^>]*>([\s\S]*?)<\/style>/gi)];
      for (const m of sheets) {
        if (m[1] !== undefined) { cssRules.push(...displayRules(m[1], width)); continue; }
        const href = /href=["']([^"']+)["']/i.exec(m[0]);
        if (!href || /^[a-z]+:/i.test(href[1])) continue;
        try { cssRules.push(...displayRules(readWeb(this.web, href[1]), width)); } catch (_) { /* missing stylesheet */ }
      }
    }
    const context = vm.createContext({__sharedSelectorCache: sharedSelectorCache});
    this.context = context;
    const dom = runModule(context, DOM_SOURCE, 'dom_lite.cjs');
    const install = runModule(context, ENV_SOURCE, 'page_env.cjs');
    const host = {console, URL, URLSearchParams, TextEncoder, TextDecoder, AbortController, AbortSignal, Blob, File: globalThis.File, FormData, Headers, Request, Response, atob, btoa, structuredClone, crypto: globalThis.crypto, Intl};
    const env = install(context, dom, {
      html, cssRules, name: this.name, now: o.now, viewport: o.viewport, media: o.media, localStorage: o.localStorage, sessionStorage: o.sessionStorage,
      confirm: o.confirm, prompt: o.prompt, apiDefault: o.apiDefault, missingApi: o.missingApi, pywebview: o.pywebview, echoConsole: o.echoConsole, platform: o.platform,
    }, host);
    this.env = env;
    this.window = context;
    this.document = env.doc;
    this.errors = env.errors;
    this.console = env.consoleLog;
    this.calls = env.calls;
    this.api = env.answers;
    context.__pageTick = ms => this.tick(ms);
    context.__page = {
      click: (selector, init) => this.click(selector, init), input: (selector, value, opts) => this.input(selector, value, opts),
      key: (key, init, selector) => this.key(key, init, selector), fire: (selector, type, init, kind) => this.fire(selector, type, init, kind),
      settle: (promise, opts) => this.settle(promise, opts), setHidden: hidden => this.setHidden(hidden), setFocused: focused => this.setFocused(focused),
    };
    // Answers: a default get_state, then the test's.
    if (this.name === 'index.html' && o.defaultState !== false) {
      const state = mergeState(defaultState(this.lang), o.state);
      const copy = () => JSON.parse(JSON.stringify(state));
      Object.assign(env.answers, startupAnswers(state, copy));
    }
    Object.assign(env.answers, o.api || {});
    if (typeof o.before === 'function') o.before(context, this);
    else if (typeof o.before === 'string') this.evalSync(`{\n${o.before}\n}`, 'before.js'); // a block: its consts stay out of the page's globals
    // Run the page's scripts in document order.
    const scripts = env.doc.querySelectorAll('script');
    for (const el of scripts) {
      const src = el.getAttribute('src');
      let source, filename;
      if (src) {
        if (/^[a-z]+:/i.test(src)) continue;
        filename = src;
        try { source = readWeb(this.web, src); } catch (error) { env.errors.push(new Error(`script ${src} not found`)); continue; }
      } else {
        filename = `${this.name}#script${[...scripts].indexOf(el)}`;
        source = el.textContent;
      }
      env.doc.currentScript = el;
      try { compiled(source, filename).runInContext(context); }
      catch (error) { env.errors.push(error); }
      env.doc.currentScript = null;
      await this.flush();
    }
    env.doc.readyState = 'interactive';
    env.doc.dispatchEvent(new dom.Event('DOMContentLoaded', {bubbles: true}));
    await this.flush();
    env.doc.readyState = 'complete';
    context.dispatchEvent(new dom.Event('load'));
    await this.flush();
    if (o.ready !== false) {
      context.dispatchEvent(new dom.Event('pywebviewready'));
      await this.flush();
      await this.tick(o.settle ?? 0);
    }
    if (!o.allowErrors && this.errors.length) throw this.failure('page errors while loading');
    return this;
  }

  failure(title) {
    const lines = this.errors.map(e => (e && e.stack) ? String(e.stack).split('\n').slice(0, 4).join('\n') : String(e));
    const err = new Error(`${title}:\n${lines.join('\n')}`);
    err.pageErrors = this.errors.slice();
    return err;
  }

  evalSync(code, filename = 'test.js') { return compiled(String(code), filename).runInContext(this.context); }

  // Runs a function (called with the page window) or code (an async function body) in the page.
  async run(code, ...args) {
    activePage = this;
    let result;
    if (typeof code === 'function') {
      const fn = this.evalSync(`(${code.toString()})`, 'test-fn.js');
      result = fn(this.window, ...args);
    } else {
      const fn = this.evalSync(`(async function(){${code}\n})`, 'test.js');
      result = fn.call(this.window);
    }
    const value = await result;
    await this.flush();
    return value;
  }

  // Lets pending promise callbacks run (no time passes).
  async flush(rounds = 8) {
    for (let k = 0; k < rounds; k++) {
      this.env.clock.resetMutationRounds();
      await flushOnce();
    }
  }

  // Advances the fake clock, running due timers (and the promise work they start) in order.
  async tick(ms = 0) {
    const clock = this.env.clock;
    const limit = clock.now + Math.max(0, Number(ms) || 0);
    let guard = 0;
    await this.flush();
    while (clock.runNext(limit)) {
      await this.flush(3);
      if (++guard > 20000) throw new Error('tick: too many timers (an interval faster than the window?)');
    }
    clock.now = limit;
    await this.flush();
  }

  // Awaits a page promise that needs time to pass (frames, timers): ticks until it settles.
  async settle(promise, {step = 50, max = 30000} = {}) {
    let done = false, value, error, failed = false;
    Promise.resolve(promise).then(v => { done = true; value = v; }, e => { done = true; failed = true; error = e; });
    await this.flush();
    for (let waited = 0; !done && waited <= max; waited += step) await this.tick(step);
    if (!done) throw new Error(`settle: still pending after ${max} ms of page time`);
    if (failed) throw error;
    return value;
  }

  // The window going to the background / coming back (document.hidden, focus).
  async setHidden(hidden) {
    const doc = this.document;
    doc.hidden = Boolean(hidden);
    doc.visibilityState = hidden ? 'hidden' : 'visible';
    if (hidden) doc.__hasFocus = false;
    doc.dispatchEvent(new this.window.Event('visibilitychange', {bubbles: true}));
    await this.flush();
  }
  async setFocused(focused) {
    this.document.__hasFocus = Boolean(focused);
    this.window.dispatchEvent(new this.window.FocusEvent(focused ? 'focus' : 'blur'));
    await this.flush();
  }

  pendingTimers() { return this.env.clock.pending(); }
  get now() { return this.env.clock.now; }

  $(selector) { return typeof selector === 'string' ? this.document.querySelector(selector) : selector; }
  $$(selector) { return [...this.document.querySelectorAll(selector)]; }
  need(selector) {
    const el = this.$(selector);
    if (!el) throw new Error(`no element matches ${selector}`);
    return el;
  }
  text(selector) { const el = this.$(selector); return el ? el.innerText : null; }
  html(selector) { const el = this.$(selector); return el ? el.innerHTML : null; }
  visible(selector) { const el = this.$(selector); return Boolean(el && el.__visible()); }

  async click(selector, init = {}) {
    const el = this.need(selector);
    if (Object.keys(init).length) {
      const event = new this.window.MouseEvent('click', {bubbles: true, cancelable: true, ...init});
      el.dispatchEvent(event);
    } else el.click();
    await this.flush();
    return el;
  }
  async fire(selector, type, init = {}, Kind) {
    const el = this.need(selector);
    const Ctor = Kind ? this.window[Kind] : this.window.Event;
    el.dispatchEvent(new Ctor(type, {bubbles: true, cancelable: true, ...init}));
    await this.flush();
    return el;
  }
  async input(selector, value, {change = true} = {}) {
    const el = this.need(selector);
    if (el.type === 'checkbox' || el.type === 'radio') el.checked = Boolean(value);
    else el.value = value;
    el.dispatchEvent(new this.window.InputEvent('input', {bubbles: true}));
    if (change) el.dispatchEvent(new this.window.Event('change', {bubbles: true}));
    await this.flush();
    return el;
  }
  async key(key, init = {}, selector = null) {
    const el = selector ? this.need(selector) : (this.document.activeElement || this.document.body);
    const event = new this.window.KeyboardEvent('keydown', {key, bubbles: true, cancelable: true, ...init});
    el.dispatchEvent(event);
    el.dispatchEvent(new this.window.KeyboardEvent('keyup', {key, bubbles: true, cancelable: true, ...init}));
    await this.flush();
    return event;
  }

  callsOf(name) { return this.calls.filter(c => c.name === name).map(c => c.args); }
  toasts() { return this.$$('#toast .toast-item:not(.leaving)').map(el => el.textContent); }

  close() { this.env.clock.timers.clear(); }
}

async function openPage(options = {}) {
  const page = new Page(options);
  return page.load();
}

module.exports = {openPage, Page, defaultState, WEB};
