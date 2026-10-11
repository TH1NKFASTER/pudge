'use strict';
/* A small DOM for running Pudge's window code in node (audit 71Б).

Enough of the DOM for the window's scripts to run and for tests to look at what they
render: an HTML parser, elements with attributes, classList, dataset, style, form
values, selectors, events with capture and bubbling, focus. There is no layout and
no CSS: sizes are 0 unless a test sets them (element.__rect, scrollHeight, ...), and
an element counts as visible unless it or an ancestor is hidden (hidden attribute or
style display:none). */

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
const RAW = new Set(['script', 'style', 'textarea', 'title']);
// An opening tag closes an open element of these kinds first (simplified HTML rules).
const AUTO_CLOSE = {
  li: ['li'], option: ['option'], optgroup: ['optgroup', 'option'], p: ['p'], tr: ['tr', 'td', 'th'], td: ['td', 'th'], th: ['td', 'th'],
  dt: ['dt', 'dd'], dd: ['dt', 'dd'], thead: ['tbody', 'tfoot'], tbody: ['thead', 'tbody'], tfoot: ['tbody'],
};
const BLOCK_CLOSES_P = new Set(['div', 'p', 'ul', 'ol', 'section', 'header', 'footer', 'nav', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'form', 'pre', 'blockquote', 'dl', 'fieldset', 'aside', 'main', 'article', 'details', 'figure', 'hr']);
const ENTITIES = {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', times: '×', middot: '·', hellip: '…', mdash: '—', ndash: '–', laquo: '«', raquo: '»', copy: '©', larr: '←', rarr: '→', uarr: '↑', darr: '↓', check: '✓', bull: '•', minus: '−', thinsp: ' ', zwj: '‍', zwnj: '‌', shy: '­', deg: '°', plusmn: '±', hearts: '♥', star: '☆', starf: '★', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”', crarr: '↵', harr: '↔', loz: '◊'};

function decode(text) {
  if (!text || text.indexOf('&') < 0) return text;
  return text.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);/g, (whole, name) => {
    if (name[0] === '#') {
      const code = name[1] === 'x' || name[1] === 'X' ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10);
      try { return String.fromCodePoint(code); } catch (_) { return whole; }
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, name) ? ENTITIES[name] : whole;
  });
}
const escapeText = text => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/ /g, '&nbsp;');
const escapeAttr = text => String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/ /g, '&nbsp;');
const camel = name => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
const kebab = name => name.replace(/[A-Z]/g, c => '-' + c.toLowerCase());

/* ---------------------------------------------------------------- parser */

function parseHTML(html, doc, parent) {
  const stack = [parent];
  const top = () => stack[stack.length - 1];
  const s = String(html);
  let i = 0;
  const len = s.length;
  const closeTo = name => {
    for (let k = stack.length - 1; k > 0; k--) {
      if (stack[k].localName === name) { stack.length = k; return true; }
    }
    return false;
  };
  while (i < len) {
    const lt = s.indexOf('<', i);
    if (lt < 0) { top().appendChild(doc.createTextNode(decode(s.slice(i)))); break; }
    if (lt > i) top().appendChild(doc.createTextNode(decode(s.slice(i, lt))));
    i = lt;
    if (s.startsWith('<!--', i)) {
      const end = s.indexOf('-->', i + 4);
      const stop = end < 0 ? len : end;
      top().appendChild(doc.createComment(s.slice(i + 4, stop)));
      i = end < 0 ? len : end + 3;
      continue;
    }
    if (s[i + 1] === '!' || s[i + 1] === '?') {
      const end = s.indexOf('>', i);
      i = end < 0 ? len : end + 1;
      continue;
    }
    if (s[i + 1] === '/') {
      const end = s.indexOf('>', i);
      const name = s.slice(i + 2, end < 0 ? len : end).trim().toLowerCase();
      closeTo(name);
      i = end < 0 ? len : end + 1;
      continue;
    }
    const m = /^<([A-Za-z][A-Za-z0-9:-]*)/.exec(s.slice(i, i + 64));
    if (!m) { top().appendChild(doc.createTextNode('<')); i += 1; continue; }
    const name = m[1].toLowerCase();
    let j = i + m[0].length;
    const attrs = [];
    let selfClosing = false;
    for (;;) {
      while (j < len && /\s/.test(s[j])) j++;
      if (j >= len) break;
      if (s[j] === '>') { j++; break; }
      if (s[j] === '/' && s[j + 1] === '>') { selfClosing = true; j += 2; break; }
      if (s[j] === '/') { j++; continue; }
      let k = j;
      while (k < len && !/[\s=>]/.test(s[k]) && !(s[k] === '/' && s[k + 1] === '>')) k++;
      const attrName = s.slice(j, k).toLowerCase();
      j = k;
      while (j < len && /\s/.test(s[j])) j++;
      let value = '';
      if (s[j] === '=') {
        j++;
        while (j < len && /\s/.test(s[j])) j++;
        const q = s[j];
        if (q === '"' || q === "'") {
          const end = s.indexOf(q, j + 1);
          value = s.slice(j + 1, end < 0 ? len : end);
          j = end < 0 ? len : end + 1;
        } else {
          let e = j;
          while (e < len && !/[\s>]/.test(s[e])) e++;
          value = s.slice(j, e);
          j = e;
        }
        value = decode(value);
      }
      if (attrName && !attrs.some(([n]) => n === attrName)) attrs.push([attrName, value]);
    }
    i = j;
    const closes = AUTO_CLOSE[name] || (BLOCK_CLOSES_P.has(name) ? ['p'] : null);
    if (closes) {
      const current = top();
      if (current.localName && closes.includes(current.localName)) stack.pop();
    }
    const el = doc.createElement(name);
    for (const [n, v] of attrs) el.__attrs.set(n, v);
    top().appendChild(el);
    if (VOID.has(name) || selfClosing) continue;
    if (RAW.has(name)) {
      const re = new RegExp('</' + name + '\\s*>', 'ig');
      re.lastIndex = i;
      const found = re.exec(s);
      const end = found ? found.index : len;
      const text = s.slice(i, end);
      if (text) el.appendChild(doc.createTextNode(name === 'textarea' || name === 'title' ? decode(text) : text));
      i = found ? found.index + found[0].length : len;
      continue;
    }
    stack.push(el);
  }
}

/* ---------------------------------------------------------------- selectors */

function splitTop(text, sep) {
  const out = [];
  let depth = 0, quote = '', start = 0;
  for (let k = 0; k < text.length; k++) {
    const c = text[k];
    if (quote) { if (c === quote) quote = ''; continue; }
    if (c === '"' || c === "'") quote = c;
    else if (c === '(' || c === '[') depth++;
    else if (c === ')' || c === ']') depth--;
    else if (c === sep && depth === 0) { out.push(text.slice(start, k)); start = k + 1; }
  }
  out.push(text.slice(start));
  return out.map(x => x.trim()).filter(Boolean);
}

// Compiled selectors are plain functions over elements, so pages can share them
// (page.cjs passes one cache for all pages of a test run).
const selectorCache = globalThis.__sharedSelectorCache || new Map();
function compileSelector(selector) {
  const key = String(selector);
  if (selectorCache.has(key)) return selectorCache.get(key);
  const list = splitTop(key, ',').map(compileComplex);
  if (!list.length) throw new SyntaxError(`'${key}' is not a valid selector`);
  selectorCache.set(key, list);
  return list;
}

function compileComplex(text) {
  // Tokenise into compounds and combinators.
  const parts = [];
  let k = 0, buf = '', depth = 0, quote = '';
  const flush = () => { if (buf.trim()) parts.push({compound: compileCompound(buf.trim())}); buf = ''; };
  while (k < text.length) {
    const c = text[k];
    if (quote) { buf += c; if (c === quote) quote = ''; k++; continue; }
    if (c === '"' || c === "'") { quote = c; buf += c; k++; continue; }
    if (c === '(' || c === '[') depth++;
    if (c === ')' || c === ']') depth--;
    if (depth === 0 && (c === '>' || c === '+' || c === '~')) {
      flush(); parts.push({comb: c}); k++; continue;
    }
    if (depth === 0 && /\s/.test(c)) {
      flush();
      while (k < text.length && /\s/.test(text[k])) k++;
      if (k < text.length && !'>+~'.includes(text[k]) && parts.length && !parts[parts.length - 1].comb) parts.push({comb: ' '});
      continue;
    }
    buf += c; k++;
  }
  flush();
  // Normalise: compound (comb compound)*
  const seq = [];
  for (const p of parts) {
    if (p.comb) { if (seq.length && !seq[seq.length - 1].comb) seq.push(p); else if (seq.length) seq[seq.length - 1] = p; }
    else seq.push(p);
  }
  if (seq.length && seq[seq.length - 1].comb) seq.pop();
  return seq;
}

function compileCompound(text) {
  const tests = [];
  let k = 0;
  const ident = () => {
    const m = /^(?:\\.|[A-Za-z0-9_\- -￿])+/.exec(text.slice(k));
    if (!m) throw new SyntaxError(`'${text}' is not a valid selector`);
    k += m[0].length;
    return m[0].replace(/\\(.)/g, '$1');
  };
  if (text[k] === '*') { k++; }
  else if (/[A-Za-z]/.test(text[k] || '')) { const tag = ident().toLowerCase(); tests.push(el => el.localName === tag); }
  while (k < text.length) {
    const c = text[k];
    if (c === '#') { k++; const id = ident(); tests.push(el => el.__attrs.get('id') === id); }
    else if (c === '.') { k++; const cls = ident(); tests.push(el => el.classList.contains(cls)); }
    else if (c === '[') {
      const end = (() => { let d = 0, q = ''; for (let p = k; p < text.length; p++) { const ch = text[p]; if (q) { if (ch === q) q = ''; continue; } if (ch === '"' || ch === "'") q = ch; else if (ch === '[') d++; else if (ch === ']') { d--; if (!d) return p; } } return text.length; })();
      const body = text.slice(k + 1, end).trim();
      k = end + 1;
      const m = /^([A-Za-z_:][-A-Za-z0-9_:.]*)\s*(?:([~|^$*]?=)\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^\s\]]+))\s*(i)?)?$/.exec(body);
      if (!m) throw new SyntaxError(`'${text}' is not a valid selector`);
      const name = m[1].toLowerCase(), op = m[2];
      let want = m[3] ?? m[4] ?? m[5];
      if (want !== undefined) want = want.replace(/\\(.)/g, '$1');
      const insensitive = Boolean(m[6]);
      tests.push(el => {
        if (!el.__attrs.has(name)) return false;
        if (!op) return true;
        let have = el.__attrs.get(name);
        let w = want;
        if (insensitive) { have = have.toLowerCase(); w = w.toLowerCase(); }
        switch (op) {
          case '=': return have === w;
          case '~=': return have.split(/\s+/).includes(w);
          case '|=': return have === w || have.startsWith(w + '-');
          case '^=': return w !== '' && have.startsWith(w);
          case '$=': return w !== '' && have.endsWith(w);
          case '*=': return w !== '' && have.includes(w);
        }
        return false;
      });
    } else if (c === ':') {
      k++;
      if (text[k] === ':') { k++; ident(); tests.push(() => false); continue; } // pseudo-elements never match
      const name = ident().toLowerCase();
      let arg = null;
      if (text[k] === '(') {
        let d = 0, p = k;
        for (; p < text.length; p++) { if (text[p] === '(') d++; else if (text[p] === ')') { d--; if (!d) break; } }
        arg = text.slice(k + 1, p).trim();
        k = p + 1;
      }
      tests.push(pseudo(name, arg, text));
    } else if (/\s/.test(c)) { k++; }
    else throw new SyntaxError(`'${text}' is not a valid selector`);
  }
  return el => tests.every(t => t(el));
}

function nth(arg) {
  const a = arg.replace(/\s+/g, '').toLowerCase();
  if (a === 'odd') return i => i % 2 === 1;
  if (a === 'even') return i => i % 2 === 0;
  const m = /^([+-]?\d*)n([+-]\d+)?$/.exec(a);
  if (m) {
    const step = m[1] === '' || m[1] === '+' ? 1 : m[1] === '-' ? -1 : parseInt(m[1], 10);
    const off = m[2] ? parseInt(m[2], 10) : 0;
    return i => step === 0 ? i === off : (i - off) / step >= 0 && (i - off) % step === 0;
  }
  const n = parseInt(a, 10);
  return i => i === n;
}

function pseudo(name, arg, text) {
  const siblings = el => el.parentNode ? el.parentNode.children : [el];
  switch (name) {
    case 'not': { const list = compileSelector(arg); return el => !list.some(seq => matchSeq(el, seq)); }
    case 'is': case 'where': case 'matches': { const list = compileSelector(arg); return el => list.some(seq => matchSeq(el, seq)); }
    case 'has': {
      const rel = arg.trim();
      if (rel[0] === '>') return el => el.querySelectorAll(':scope ' + rel).length > 0;
      if (rel[0] === '+' || rel[0] === '~') {
        const list = compileSelector(rel.slice(1).trim());
        return el => {
          for (let n = el.nextElementSibling; n; n = rel[0] === '+' ? null : n.nextElementSibling) if (list.some(seq => matchSeq(n, seq))) return true;
          return false;
        };
      }
      return el => el.querySelector(rel) !== null;
    }
    case 'scope': return el => el.__isScope === true;
    case 'root': return el => el.parentNode && el.parentNode.nodeType === 9;
    case 'checked': return el => el.checked === true || (el.localName === 'option' && el.selected === true);
    case 'disabled': return el => el.__attrs.has('disabled');
    case 'enabled': return el => ['input', 'button', 'select', 'textarea', 'option'].includes(el.localName) && !el.__attrs.has('disabled');
    case 'focus': case 'focus-visible': return el => el.ownerDocument && el.ownerDocument.activeElement === el;
    case 'focus-within': return el => { const a = el.ownerDocument && el.ownerDocument.activeElement; return Boolean(a && el.contains(a)); };
    case 'hover': case 'active': case 'visited': case 'target': case 'fullscreen': case 'modal': return () => false;
    case 'link': case 'any-link': return el => el.localName === 'a' && el.__attrs.has('href');
    case 'empty': return el => el.childNodes.every(n => n.nodeType === 8 || (n.nodeType === 3 && n.data === ''));
    case 'first-child': return el => siblings(el)[0] === el;
    case 'last-child': return el => { const s = siblings(el); return s[s.length - 1] === el; };
    case 'only-child': return el => siblings(el).length === 1;
    case 'nth-child': { const f = nth(arg); return el => f(siblings(el).indexOf(el) + 1); }
    case 'nth-last-child': { const f = nth(arg); return el => { const s = siblings(el); return f(s.length - s.indexOf(el)); }; }
    case 'first-of-type': return el => siblings(el).find(x => x.localName === el.localName) === el;
    case 'last-of-type': return el => { const s = siblings(el).filter(x => x.localName === el.localName); return s[s.length - 1] === el; };
    case 'nth-of-type': { const f = nth(arg); return el => f(siblings(el).filter(x => x.localName === el.localName).indexOf(el) + 1); }
    case 'placeholder-shown': return el => el.__attrs.has('placeholder') && !el.value;
    case 'required': return el => el.__attrs.has('required');
    case 'open': return el => el.__attrs.has('open');
    default: throw new SyntaxError(`'${text}': pseudo-class :${name} is not supported by dom_lite`);
  }
}

function matchSeq(el, seq, scope) {
  // seq: [compound, comb, compound, ...]; match from the right.
  const matchAt = (node, idx) => {
    if (!seq[idx].compound(node)) return false;
    if (idx === 0) return true;
    const comb = seq[idx - 1].comb;
    if (comb === '>') { const p = node.parentNode; return Boolean(p && p.nodeType === 1 && matchAt(p, idx - 2)); }
    if (comb === ' ') { for (let p = node.parentNode; p && p.nodeType === 1; p = p.parentNode) if (matchAt(p, idx - 2)) return true; return false; }
    if (comb === '+') { const p = node.previousElementSibling; return Boolean(p && matchAt(p, idx - 2)); }
    if (comb === '~') { for (let p = node.previousElementSibling; p; p = p.previousElementSibling) if (matchAt(p, idx - 2)) return true; return false; }
    return false;
  };
  return matchAt(el, seq.length - 1);
}

/* ---------------------------------------------------------------- events */

class Event {
  constructor(type, init = {}) {
    this.type = String(type);
    this.bubbles = Boolean(init.bubbles);
    this.cancelable = Boolean(init.cancelable);
    this.composed = Boolean(init.composed);
    this.defaultPrevented = false;
    this.target = null;
    this.currentTarget = null;
    this.eventPhase = 0;
    this.timeStamp = 0;
    this.isTrusted = false;
    this.__stop = false;
    this.__stopNow = false;
    this.__path = [];
    for (const [k, v] of Object.entries(init)) if (!(k in this)) this[k] = v;
  }
  preventDefault() { if (this.cancelable) this.defaultPrevented = true; }
  stopPropagation() { this.__stop = true; }
  stopImmediatePropagation() { this.__stop = true; this.__stopNow = true; }
  composedPath() { return this.__path.slice(); }
  get returnValue() { return !this.defaultPrevented; }
}
class CustomEvent extends Event { constructor(type, init = {}) { super(type, init); this.detail = init.detail ?? null; } }
class UIEvent extends Event { constructor(type, init = {}) { super(type, init); this.detail = init.detail ?? 0; this.view = init.view ?? null; } }
class KeyboardEvent extends UIEvent {
  constructor(type, init = {}) {
    super(type, init);
    for (const k of ['key', 'code']) this[k] = init[k] ?? '';
    for (const k of ['altKey', 'ctrlKey', 'metaKey', 'shiftKey', 'repeat', 'isComposing']) this[k] = Boolean(init[k]);
    this.location = init.location ?? 0;
    this.keyCode = init.keyCode ?? 0;
  }
  getModifierState(k) { return {Alt: this.altKey, Control: this.ctrlKey, Meta: this.metaKey, Shift: this.shiftKey}[k] || false; }
}
class MouseEvent extends UIEvent {
  constructor(type, init = {}) {
    super(type, init);
    for (const k of ['clientX', 'clientY', 'screenX', 'screenY', 'pageX', 'pageY', 'offsetX', 'offsetY', 'movementX', 'movementY', 'button', 'buttons']) this[k] = init[k] ?? 0;
    for (const k of ['altKey', 'ctrlKey', 'metaKey', 'shiftKey']) this[k] = Boolean(init[k]);
    this.relatedTarget = init.relatedTarget ?? null;
    this.x = this.clientX; this.y = this.clientY;
  }
}
class PointerEvent extends MouseEvent { constructor(type, init = {}) { super(type, init); this.pointerId = init.pointerId ?? 1; this.pointerType = init.pointerType ?? 'mouse'; this.isPrimary = init.isPrimary ?? true; } }
class WheelEvent extends MouseEvent { constructor(type, init = {}) { super(type, init); this.deltaX = init.deltaX ?? 0; this.deltaY = init.deltaY ?? 0; this.deltaZ = init.deltaZ ?? 0; this.deltaMode = init.deltaMode ?? 0; } }
class FocusEvent extends UIEvent { constructor(type, init = {}) { super(type, init); this.relatedTarget = init.relatedTarget ?? null; } }
class InputEvent extends UIEvent { constructor(type, init = {}) { super(type, init); this.data = init.data ?? null; this.inputType = init.inputType ?? ''; this.isComposing = Boolean(init.isComposing); } }
class DragEvent extends MouseEvent { constructor(type, init = {}) { super(type, init); this.dataTransfer = init.dataTransfer ?? null; } }
class ClipboardEvent extends Event { constructor(type, init = {}) { super(type, init); this.clipboardData = init.clipboardData ?? null; } }

class EventTarget {
  constructor() { this.__listeners = new Map(); }
  addEventListener(type, fn, options) {
    if (!fn) return;
    const capture = typeof options === 'boolean' ? options : Boolean(options && options.capture);
    const once = Boolean(options && typeof options === 'object' && options.once);
    const signal = options && typeof options === 'object' ? options.signal : null;
    const list = this.__listeners.get(type) || [];
    if (list.some(l => l.fn === fn && l.capture === capture)) return;
    const entry = {fn, capture, once};
    list.push(entry);
    this.__listeners.set(type, list);
    if (signal && typeof signal.addEventListener === 'function') signal.addEventListener('abort', () => this.removeEventListener(type, fn, {capture}));
  }
  removeEventListener(type, fn, options) {
    const capture = typeof options === 'boolean' ? options : Boolean(options && options.capture);
    const list = this.__listeners.get(type);
    if (!list) return;
    const k = list.findIndex(l => l.fn === fn && l.capture === capture);
    if (k >= 0) list.splice(k, 1);
  }
  __fire(event, phase) {
    const list = (this.__listeners.get(event.type) || []).slice();
    event.currentTarget = this;
    for (const l of list) {
      if (phase === 1 && !l.capture) continue;
      if (phase === 3 && l.capture) continue;
      if (l.once) this.removeEventListener(event.type, l.fn, {capture: l.capture});
      try {
        if (typeof l.fn === 'function') l.fn.call(this, event);
        else if (l.fn && typeof l.fn.handleEvent === 'function') l.fn.handleEvent(event);
      } catch (error) { reportError(this, error); }
      if (event.__stopNow) return;
    }
    if (phase !== 1) {
      let handler = Object.prototype.hasOwnProperty.call(this, 'on' + event.type) ? this['on' + event.type] : null;
      if (typeof handler !== 'function' && this.__attrs && this.__attrs.has('on' + event.type)) {
        // An inline handler attribute (onload="..."): compiled once per attribute text.
        const code = this.__attrs.get('on' + event.type);
        this.__inline = this.__inline || {};
        const cached = this.__inline[event.type];
        if (!cached || cached.code !== code) this.__inline[event.type] = {code, fn: new Function('event', code)};
        handler = this.__inline[event.type].fn;
      }
      if (typeof handler === 'function') {
        try { const r = handler.call(this, event); if (r === false) event.preventDefault(); } catch (error) { reportError(this, error); }
      }
    }
  }
  dispatchEvent(event) {
    event.target = this;
    const path = [];
    for (let n = this; n; n = n.__parentTarget ? n.__parentTarget() : null) path.push(n);
    event.__path = path;
    event.eventPhase = 1;
    for (let k = path.length - 1; k > 0 && !event.__stop; k--) path[k].__fire(event, 1);
    if (!event.__stop) { event.eventPhase = 2; this.__fire(event, 2); }
    if (event.bubbles) {
      event.eventPhase = 3;
      for (let k = 1; k < path.length && !event.__stop; k++) path[k].__fire(event, 3);
    }
    event.eventPhase = 0;
    event.currentTarget = null;
    return !event.defaultPrevented;
  }
}

function reportError(target, error) {
  let doc = target;
  if (doc && doc.ownerDocument) doc = doc.ownerDocument;
  const win = doc && doc.defaultView ? doc.defaultView : target && target.document ? target : null;
  if (win && typeof win.__reportError === 'function') win.__reportError(error);
  else throw error;
}

/* ---------------------------------------------------------------- nodes */

class Node extends EventTarget {
  constructor(doc) { super(); this.ownerDocument = doc; this.parentNode = null; this.childNodes = []; }
  __parentTarget() { return this.parentNode || (this.nodeType === 9 ? this.defaultView : null); }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling() { const p = this.parentNode; if (!p) return null; return p.childNodes[p.childNodes.indexOf(this) + 1] || null; }
  get previousSibling() { const p = this.parentNode; if (!p) return null; return p.childNodes[p.childNodes.indexOf(this) - 1] || null; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n.nodeType === 9; }
  hasChildNodes() { return this.childNodes.length > 0; }
  getRootNode() { let n = this; while (n.parentNode) n = n.parentNode; return n; }
  contains(other) { for (let n = other; n; n = n.parentNode) if (n === this) return true; return false; }
  __adopt(node) {
    if (node.nodeType === 11) { const kids = node.childNodes.slice(); node.childNodes = []; for (const k of kids) k.parentNode = null; return kids; }
    if (node.parentNode) node.parentNode.removeChild(node);
    return [node];
  }
  // record: {type: 'childList', addedNodes, removedNodes} | {type: 'attributes', attributeName, oldValue} | {type: 'characterData'}
  __changed(record) {
    const doc = this.ownerDocument || this;
    if (!doc) return;
    doc.__version = (doc.__version || 0) + 1;
    if (doc.__mutated) doc.__mutated(this, record || {type: 'childList', addedNodes: [], removedNodes: []});
  }
  appendChild(node) {
    if (node.contains && node.contains(this)) throw new Error('HierarchyRequestError');
    const added = this.__adopt(node);
    for (const n of added) { n.parentNode = this; this.childNodes.push(n); }
    this.__changed({type: 'childList', addedNodes: added, removedNodes: []});
    return node;
  }
  insertBefore(node, ref) {
    if (!ref) return this.appendChild(node);
    const nodes = this.__adopt(node);
    const at = this.childNodes.indexOf(ref);
    if (at < 0) throw new Error('NotFoundError: reference node is not a child');
    for (const n of nodes) n.parentNode = this;
    this.childNodes.splice(at, 0, ...nodes);
    this.__changed({type: 'childList', addedNodes: nodes, removedNodes: []});
    return node;
  }
  removeChild(node) {
    const at = this.childNodes.indexOf(node);
    if (at < 0) throw new Error('NotFoundError: node is not a child');
    this.childNodes.splice(at, 1);
    node.parentNode = null;
    const doc = this.ownerDocument || this;
    if (doc.activeElement && node.contains(doc.activeElement)) doc.activeElement = doc.body;
    this.__changed({type: 'childList', addedNodes: [], removedNodes: [node]});
    return node;
  }
  replaceChild(node, old) { this.insertBefore(node, old); this.removeChild(old); return old; }
  get textContent() { return this.childNodes.map(n => n.nodeType === 8 ? '' : n.textContent).join(''); }
  set textContent(value) {
    const removed = this.childNodes;
    for (const n of removed) n.parentNode = null;
    this.childNodes = [];
    const text = value == null ? '' : String(value);
    const added = text ? [(this.ownerDocument || this).createTextNode(text)] : [];
    for (const n of added) { n.parentNode = this; this.childNodes.push(n); }
    this.__changed({type: 'childList', addedNodes: added, removedNodes: removed});
  }
  get nodeValue() { return null; }
  normalize() {
    const out = [];
    for (const n of this.childNodes) {
      if (n.nodeType === 3) {
        if (n.data === '') { n.parentNode = null; continue; }
        const prev = out[out.length - 1];
        if (prev && prev.nodeType === 3) { prev.data += n.data; n.parentNode = null; continue; }
      } else if (n.normalize) n.normalize();
      out.push(n);
    }
    this.childNodes = out;
  }
  compareDocumentPosition(other) {
    if (other === this) return 0;
    const chain = n => { const c = []; for (; n; n = n.parentNode) c.unshift(n); return c; };
    const a = chain(this), b = chain(other);
    if (a[0] !== b[0]) return 1;
    let k = 0; while (a[k] === b[k]) k++;
    if (k === a.length) return 16 | 4; // other is contained by this, follows
    if (k === b.length) return 8 | 2;  // other contains this, precedes
    const parent = a[k - 1];
    return parent.childNodes.indexOf(a[k]) < parent.childNodes.indexOf(b[k]) ? 4 : 2;
  }
}
Node.ELEMENT_NODE = 1; Node.TEXT_NODE = 3; Node.COMMENT_NODE = 8; Node.DOCUMENT_NODE = 9; Node.DOCUMENT_FRAGMENT_NODE = 11;
Node.DOCUMENT_POSITION_PRECEDING = 2; Node.DOCUMENT_POSITION_FOLLOWING = 4; Node.DOCUMENT_POSITION_CONTAINS = 8; Node.DOCUMENT_POSITION_CONTAINED_BY = 16;

class CharacterData extends Node {
  constructor(doc, data) { super(doc); this.data = String(data); }
  get textContent() { return this.data; }
  set textContent(v) { const old = this.data; this.data = String(v ?? ''); this.__changed({type: 'characterData', oldValue: old}); }
  get nodeValue() { return this.data; }
  set nodeValue(v) { this.textContent = v; }
  get length() { return this.data.length; }
  get nextElementSibling() { return siblingElement(this, 1); }
  get previousElementSibling() { return siblingElement(this, -1); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  appendData(text) { this.data += String(text); }
  splitText(offset) {
    const rest = this.ownerDocument.createTextNode(this.data.slice(offset));
    this.data = this.data.slice(0, offset);
    if (this.parentNode) this.parentNode.insertBefore(rest, this.nextSibling);
    return rest;
  }
}
class Text extends CharacterData {
  get nodeType() { return 3; }
  get nodeName() { return '#text'; }
  get wholeText() { return this.data; }
  cloneNode() { return this.ownerDocument.createTextNode(this.data); }
}
class Comment extends CharacterData {
  get nodeType() { return 8; }
  get nodeName() { return '#comment'; }
  cloneNode() { return this.ownerDocument.createComment(this.data); }
}

function siblingElement(node, dir) {
  const p = node.parentNode;
  if (!p) return null;
  const kids = p.childNodes;
  for (let k = kids.indexOf(node) + dir; k >= 0 && k < kids.length; k += dir) if (kids[k].nodeType === 1) return kids[k];
  return null;
}

class ParentNode extends Node {
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get childElementCount() { return this.children.length; }
  get firstElementChild() { return this.childNodes.find(n => n.nodeType === 1) || null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] || null; }
  append(...nodes) { for (const n of nodes) this.appendChild(typeof n === 'string' ? (this.ownerDocument || this).createTextNode(n) : n); }
  prepend(...nodes) { const first = this.firstChild; for (const n of nodes) this.insertBefore(typeof n === 'string' ? (this.ownerDocument || this).createTextNode(n) : n, first); }
  replaceChildren(...nodes) {
    const removed = this.childNodes;
    for (const n of removed) n.parentNode = null;
    this.childNodes = [];
    const doc = this.ownerDocument || this;
    const added = [];
    for (const n of nodes) added.push(...this.__adopt(typeof n === 'string' ? doc.createTextNode(n) : n));
    for (const n of added) { n.parentNode = this; this.childNodes.push(n); }
    this.__changed({type: 'childList', addedNodes: added, removedNodes: removed});
  }
  __descendants(out = []) {
    for (const n of this.childNodes) if (n.nodeType === 1) { out.push(n); n.__descendants(out); }
    return out;
  }
  querySelectorAll(selector) {
    const list = compileSelector(selector);
    this.__isScope = true;
    try { return makeNodeList(this.__descendants().filter(el => list.some(seq => matchSeq(el, seq)))); }
    finally { delete this.__isScope; }
  }
  querySelector(selector) {
    const list = compileSelector(selector);
    this.__isScope = true;
    try {
      const walk = node => {
        for (const n of node.childNodes) {
          if (n.nodeType !== 1) continue;
          if (list.some(seq => matchSeq(n, seq))) return n;
          const found = walk(n);
          if (found) return found;
        }
        return null;
      };
      return walk(this);
    } finally { delete this.__isScope; }
  }
  getElementsByClassName(names) { const want = String(names).split(/\s+/).filter(Boolean); return makeNodeList(this.__descendants().filter(el => want.every(c => el.classList.contains(c)))); }
  getElementsByTagName(tag) { const t = String(tag).toLowerCase(); return makeNodeList(this.__descendants().filter(el => t === '*' || el.localName === t)); }
}

function makeNodeList(items) {
  const list = items.slice();
  Object.defineProperty(list, 'item', {value: k => list[k] ?? null});
  return list;
}

class DocumentFragment extends ParentNode {
  get nodeType() { return 11; }
  get nodeName() { return '#document-fragment'; }
  getElementById(id) { return this.__descendants().find(el => el.__attrs.get('id') === id) || null; }
  cloneNode(deep) { const f = this.ownerDocument.createDocumentFragment(); if (deep) for (const n of this.childNodes) f.appendChild(n.cloneNode(true)); return f; }
  get innerHTML() { return this.childNodes.map(serialize).join(''); }
}

class DOMTokenList {
  constructor(el, attr) { this.__el = el; this.__attr = attr; }
  __get() {
    const text = this.__el.__attrs.get(this.__attr) || '';
    if (this.__text !== text) { this.__text = text; this.__list = text.split(/\s+/).filter(Boolean); }
    return this.__list.slice();
  }
  contains(token) { this.__get(); return this.__list.includes(String(token)); }
  __set(list) { this.__el.setAttribute(this.__attr, [...new Set(list)].join(' ')); }
  get length() { return this.__get().length; }
  get value() { return this.__el.__attrs.get(this.__attr) || ''; }
  item(k) { return this.__get()[k] ?? null; }
  add(...tokens) { const l = this.__get(); for (const t of tokens) if (!l.includes(String(t))) l.push(String(t)); this.__set(l); }
  remove(...tokens) { const drop = new Set(tokens.map(String)); const before = this.__get(); const l = before.filter(t => !drop.has(t)); if (l.length !== before.length || this.__el.__attrs.has(this.__attr)) this.__set(l); }
  toggle(token, force) {
    const t = String(token), has = this.contains(t);
    const on = force === undefined ? !has : Boolean(force);
    if (on && !has) this.add(t); else if (!on && has) this.remove(t);
    return on;
  }
  replace(a, b) { const l = this.__get(); const k = l.indexOf(a); if (k < 0) return false; l[k] = b; this.__set(l); return true; }
  forEach(fn) { this.__get().forEach(fn); }
  [Symbol.iterator]() { return this.__get()[Symbol.iterator](); }
  toString() { return this.value; }
}

function makeStyle(el) {
  const props = new Map();
  const write = () => {
    const text = [...props].map(([k, v]) => `${k}: ${v};`).join(' ');
    if (text) el.__attrs.set('style', text); else el.__attrs.delete('style');
  };
  const parse = text => {
    props.clear();
    for (const part of String(text || '').split(';')) {
      const k = part.indexOf(':');
      if (k < 0) continue;
      const name = part.slice(0, k).trim().toLowerCase();
      const value = part.slice(k + 1).trim();
      if (name) props.set(name, value);
    }
  };
  parse(el.__attrs.get('style'));
  const api = {
    setProperty(name, value) { const n = String(name); if (value === null || value === '' || value === undefined) props.delete(n); else props.set(n, String(value)); write(); },
    removeProperty(name) { const v = props.get(String(name)) || ''; props.delete(String(name)); write(); return v; },
    getPropertyValue(name) { return props.get(String(name)) || ''; },
    item(k) { return [...props.keys()][k] || ''; },
    get length() { return props.size; },
    get cssText() { return [...props].map(([k, v]) => `${k}: ${v};`).join(' '); },
    set cssText(text) { parse(text); write(); },
    __parse: parse,
  };
  return new Proxy(api, {
    get(target, key) {
      if (typeof key === 'symbol' || key in target) return target[key];
      if (/^\d+$/.test(key)) return target.item(Number(key));
      const name = key.startsWith('--') ? key : kebab(key);
      return props.get(name) || '';
    },
    set(target, key, value) {
      if (key === 'cssText') { target.cssText = value; return true; }
      const name = key.startsWith('--') ? key : kebab(key);
      target.setProperty(name, value);
      return true;
    },
  });
}

function makeDataset(el) {
  return new Proxy({}, {
    get(_, key) { if (typeof key === 'symbol') return undefined; const v = el.__attrs.get('data-' + kebab(key)); return v === undefined ? undefined : v; },
    set(_, key, value) { el.setAttribute('data-' + kebab(key), String(value)); return true; },
    deleteProperty(_, key) { el.removeAttribute('data-' + kebab(key)); return true; },
    has(_, key) { return el.__attrs.has('data-' + kebab(String(key))); },
    ownKeys() { return [...el.__attrs.keys()].filter(k => k.startsWith('data-')).map(k => camel(k.slice(5))); },
    getOwnPropertyDescriptor(_, key) {
      const v = el.__attrs.get('data-' + kebab(String(key)));
      return v === undefined ? undefined : {value: v, writable: true, enumerable: true, configurable: true};
    },
  });
}

const BOOL_ATTRS = ['hidden', 'disabled', 'readonly', 'required', 'multiple', 'autofocus', 'open', 'draggable', 'inert', 'novalidate', 'controls', 'autoplay', 'loop', 'muted', 'playsinline', 'defer', 'async', 'contenteditable', 'spellcheck'];
const STRING_ATTRS = {id: 'id', className: 'class', title: 'title', name: 'name', type: null, href: 'href', src: 'src', alt: 'alt', placeholder: 'placeholder', htmlFor: 'for', role: 'role', lang: 'lang', dir: 'dir', accept: 'accept', rel: 'rel', target: 'target', min: 'min', max: 'max', step: 'step', pattern: 'pattern', autocomplete: 'autocomplete', download: 'download', inputMode: 'inputmode', enterKeyHint: 'enterkeyhint', content: 'content', httpEquiv: 'http-equiv', charset: 'charset', media: 'media', sizes: 'sizes', srcset: 'srcset', poster: 'poster', preload: 'preload', crossOrigin: 'crossorigin', referrerPolicy: 'referrerpolicy', loading: 'loading', decoding: 'decoding', width: 'width', height: 'height', cols: 'cols', rows: 'rows', maxLength: 'maxlength', minLength: 'minlength', wrap: 'wrap', form_: 'form', list: 'list', accessKey: 'accesskey', translate_: 'translate', slot: 'slot', part_: 'part', is_: 'is'};

class Element extends ParentNode {
  constructor(doc, name) {
    super(doc);
    this.localName = String(name).toLowerCase();
    this.__attrs = new Map();
    this.__value = undefined;
    this.__checked = undefined;
    this.__selected = undefined;
    this.scrollTop = 0; this.scrollLeft = 0;
    this.__style = null; this.__classList = null; this.__dataset = null;
  }
  get nodeType() { return 1; }
  get nodeName() { return this.localName.toUpperCase(); }
  get tagName() { return this.localName.toUpperCase(); }
  get attributes() { return [...this.__attrs].map(([name, value]) => ({name, value, localName: name, nodeName: name})); }
  getAttributeNames() { return [...this.__attrs.keys()]; }
  getAttribute(name) { const v = this.__attrs.get(String(name).toLowerCase()); return v === undefined ? null : v; }
  hasAttribute(name) { return this.__attrs.has(String(name).toLowerCase()); }
  hasAttributes() { return this.__attrs.size > 0; }
  setAttribute(name, value) {
    const n = String(name).toLowerCase();
    const v = String(value);
    const old = this.__attrs.get(n);
    this.__attrs.set(n, v);
    if (n === 'style' && this.__style) this.__style.__parse(v);
    if (old !== v) this.__changed({type: 'attributes', attributeName: n, oldValue: old === undefined ? null : old});
  }
  removeAttribute(name) {
    const n = String(name).toLowerCase();
    const old = this.__attrs.get(n);
    if (this.__attrs.delete(n)) { if (n === 'style' && this.__style) this.__style.__parse(''); this.__changed({type: 'attributes', attributeName: n, oldValue: old}); }
  }
  toggleAttribute(name, force) {
    const n = String(name).toLowerCase(), has = this.__attrs.has(n);
    const on = force === undefined ? !has : Boolean(force);
    if (on && !has) this.setAttribute(n, ''); else if (!on && has) this.removeAttribute(n);
    return on;
  }
  get classList() { return this.__classList || (this.__classList = new DOMTokenList(this, 'class')); }
  get dataset() { return this.__dataset || (this.__dataset = makeDataset(this)); }
  get style() { return this.__style || (this.__style = makeStyle(this)); }
  set style(text) { this.style.cssText = text; }
  get nextElementSibling() { return siblingElement(this, 1); }
  get previousElementSibling() { return siblingElement(this, -1); }
  get innerHTML() { return (this.localName === 'template' ? this.content.childNodes : this.childNodes).map(serialize).join(''); }
  set innerHTML(html) {
    const doc = this.ownerDocument;
    const frag = doc.createDocumentFragment();
    if (RAW.has(this.localName)) { if (html) frag.appendChild(doc.createTextNode(String(html))); }
    else parseHTML(String(html ?? ''), doc, frag);
    this.replaceChildren(frag);
    if (doc.activeElement && !doc.activeElement.isConnected) doc.activeElement = doc.body;
  }
  get outerHTML() { return serialize(this); }
  set outerHTML(html) {
    const parent = this.parentNode;
    if (!parent) return;
    const frag = this.ownerDocument.createDocumentFragment();
    parseHTML(String(html), this.ownerDocument, frag);
    parent.replaceChild(frag, this);
  }
  get innerText() { return visibleText(this); }
  set innerText(v) { this.textContent = v; }
  get outerText() { return this.innerText; }
  insertAdjacentHTML(position, html) {
    const frag = this.ownerDocument.createDocumentFragment();
    parseHTML(String(html), this.ownerDocument, frag);
    this.__insertAdjacent(position, frag);
  }
  insertAdjacentElement(position, el) { this.__insertAdjacent(position, el); return el; }
  insertAdjacentText(position, text) { this.__insertAdjacent(position, this.ownerDocument.createTextNode(text)); }
  __insertAdjacent(position, node) {
    switch (String(position).toLowerCase()) {
      case 'beforebegin': this.parentNode && this.parentNode.insertBefore(node, this); break;
      case 'afterbegin': this.insertBefore(node, this.firstChild); break;
      case 'beforeend': this.appendChild(node); break;
      case 'afterend': this.parentNode && this.parentNode.insertBefore(node, this.nextSibling); break;
      default: throw new SyntaxError(`bad position ${position}`);
    }
  }
  before(...nodes) { const p = this.parentNode; if (!p) return; for (const n of nodes) p.insertBefore(typeof n === 'string' ? this.ownerDocument.createTextNode(n) : n, this); }
  after(...nodes) { const p = this.parentNode; if (!p) return; let ref = this.nextSibling; for (const n of nodes) { const node = typeof n === 'string' ? this.ownerDocument.createTextNode(n) : n; p.insertBefore(node, ref); } }
  replaceWith(...nodes) { const p = this.parentNode; if (!p) return; this.before(...nodes); p.removeChild(this); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  cloneNode(deep) {
    const el = this.ownerDocument.createElement(this.localName);
    for (const [k, v] of this.__attrs) el.__attrs.set(k, v);
    el.__value = this.__value; el.__checked = this.__checked; el.__selected = this.__selected;
    if (deep) for (const n of this.childNodes) el.appendChild(n.cloneNode(true));
    return el;
  }
  matches(selector) { const list = compileSelector(selector); return list.some(seq => matchSeq(this, seq)); }
  webkitMatchesSelector(selector) { return this.matches(selector); }
  closest(selector) { const list = compileSelector(selector); for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (list.some(seq => matchSeq(n, seq))) return n; return null; }
  // Visibility and geometry (no layout: tests set __rect / sizes when they matter).
  __visible() {
    if (!this.isConnected) return false;
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) {
      if (n.__attrs.has('hidden')) return false;
      const up = n.parentNode;
      if (up && up.nodeType === 1 && up.localName === 'details' && !up.__attrs.has('open') && !(n.localName === 'summary' && up.children.find(c => c.localName === 'summary') === n)) return false;
      const display = n.__style ? n.__style.getPropertyValue('display') : (/(?:^|;)\s*display\s*:\s*none/i.test(n.__attrs.get('style') || '') ? 'none' : '');
      if (display === 'none') return false;
      const view = this.ownerDocument.defaultView;
      if (view && typeof view.__cssHidden === 'function' && view.__cssHidden(n)) return false;
    }
    return true;
  }
  getBoundingClientRect() {
    const r = this.__rect || {};
    const x = r.x ?? r.left ?? 0, y = r.y ?? r.top ?? 0, width = r.width ?? 0, height = r.height ?? 0;
    return {x, y, left: x, top: y, width, height, right: x + width, bottom: y + height, toJSON() { return this; }};
  }
  getClientRects() { return this.__visible() ? [this.getBoundingClientRect()] : []; }
  checkVisibility() { return this.__visible(); }
  get offsetParent() { return this.__visible() ? (this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : this.ownerDocument.body) : null; }
  get offsetWidth() { return this.__visible() ? this.getBoundingClientRect().width : 0; }
  get offsetHeight() { return this.__visible() ? this.getBoundingClientRect().height : 0; }
  get offsetTop() { return this.getBoundingClientRect().top; }
  get offsetLeft() { return this.getBoundingClientRect().left; }
  get clientWidth() { return this.__clientWidth ?? this.offsetWidth; }
  set clientWidth(v) { this.__clientWidth = v; }
  get clientHeight() { return this.__clientHeight ?? this.offsetHeight; }
  set clientHeight(v) { this.__clientHeight = v; }
  get scrollWidth() { return this.__scrollWidth ?? this.clientWidth; }
  set scrollWidth(v) { this.__scrollWidth = v; }
  get scrollHeight() { return this.__scrollHeight ?? this.clientHeight; }
  set scrollHeight(v) { this.__scrollHeight = v; }
  scrollIntoView(options) { (this.__scrolls || (this.__scrolls = [])).push(options ?? true); }
  scrollTo(x, y) { if (x && typeof x === 'object') { if (x.top !== undefined) this.scrollTop = x.top; if (x.left !== undefined) this.scrollLeft = x.left; } else { this.scrollLeft = x ?? this.scrollLeft; this.scrollTop = y ?? this.scrollTop; } }
  scroll(x, y) { this.scrollTo(x, y); }
  scrollBy(x, y) { if (x && typeof x === 'object') { this.scrollTop += x.top || 0; this.scrollLeft += x.left || 0; } else { this.scrollLeft += x || 0; this.scrollTop += y || 0; } }
  focus(options) { this.ownerDocument.__focus(this, options); }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.__focus(this.ownerDocument.body); }
  click() {
    if (this.__attrs.has('disabled')) return;
    const view = this.ownerDocument.defaultView;
    const isCheck = this.localName === 'input' && (this.type === 'checkbox' || this.type === 'radio');
    const before = isCheck ? this.checked : null;
    if (isCheck) {
      if (this.type === 'radio') this.__checkRadio(); else this.checked = !this.checked;
    }
    const event = new (view ? view.MouseEvent : MouseEvent)('click', {bubbles: true, cancelable: true, button: 0});
    const ok = this.dispatchEvent(event);
    if (isCheck) {
      if (!ok) this.checked = before;
      else { this.dispatchEvent(new Event('input', {bubbles: true})); this.dispatchEvent(new Event('change', {bubbles: true})); }
    } else if (ok) {
      const summary = this.closest('summary');
      const details = summary && summary.parentNode;
      if (details && details.localName === 'details' && details.children.find(c => c.localName === 'summary') === summary) {
        details.toggleAttribute('open');
        details.dispatchEvent(new Event('toggle'));
      }
      const label = this.localName === 'label' ? this : null;
      if (label) { const target = label.__attrs.has('for') ? this.ownerDocument.getElementById(label.__attrs.get('for')) : label.querySelector('input,select,textarea,button'); if (target && target !== this) target.click(); }
      const submit = this.closest('button[type=submit],input[type=submit]') || (this.localName === 'button' && !this.__attrs.has('type') ? this : null);
      const form = submit && submit.closest('form');
      if (form) form.requestSubmit();
    }
  }
  // Dialog
  showModal() { this.setAttribute('open', ''); this.__modal = true; }
  show() { this.setAttribute('open', ''); }
  close(value) { if (value !== undefined) this.returnValue = value; this.removeAttribute('open'); this.dispatchEvent(new Event('close')); }
  // Form controls
  get type() {
    if (this.localName === 'input') return (this.__attrs.get('type') || 'text').toLowerCase();
    if (this.localName === 'button') return (this.__attrs.get('type') || 'submit').toLowerCase();
    if (this.localName === 'select') return this.__attrs.has('multiple') ? 'select-multiple' : 'select-one';
    if (this.localName === 'textarea') return 'textarea';
    return this.__attrs.get('type') || '';
  }
  set type(v) { this.setAttribute('type', v); }
  get value() {
    if (this.localName === 'select') { const o = this.selectedOptions[0]; return o ? o.value : ''; }
    if (this.localName === 'option') return this.__attrs.has('value') ? this.__attrs.get('value') : this.textContent.replace(/\s+/g, ' ').trim();
    if (this.localName === 'textarea') return this.__value !== undefined ? this.__value : this.textContent;
    if (this.localName === 'input') {
      if (this.__value !== undefined) return this.__value;
      const v = this.__attrs.get('value');
      if (v !== undefined) return v;
      return this.type === 'checkbox' || this.type === 'radio' ? 'on' : '';
    }
    return this.__attrs.get('value') ?? '';
  }
  set value(v) {
    const text = v == null ? '' : String(v);
    if (this.localName === 'select') {
      let found = false;
      for (const o of this.options) { const hit = !found && o.value === text; o.__selected = hit; if (hit) found = true; }
      if (!found) this.__noSelection = true; else delete this.__noSelection;
      return;
    }
    if (this.localName === 'input' || this.localName === 'textarea') { this.__value = text; return; }
    this.setAttribute('value', text);
  }
  get defaultValue() { return this.localName === 'textarea' ? this.textContent : this.__attrs.get('value') ?? ''; }
  set defaultValue(v) { if (this.localName === 'textarea') this.textContent = v; else this.setAttribute('value', v); }
  get valueAsNumber() { const n = parseFloat(this.value); return Number.isFinite(n) ? n : NaN; }
  set valueAsNumber(n) { this.value = String(n); }
  get checked() { return this.__checked !== undefined ? this.__checked : this.__attrs.has('checked'); }
  set checked(v) { this.__checked = Boolean(v); if (v && this.type === 'radio') this.__checkRadio(true); }
  __checkRadio(already) {
    if (!already) this.__checked = true;
    const name = this.__attrs.get('name');
    if (!name) return;
    const root = this.closest('form') || this.ownerDocument;
    for (const other of root.querySelectorAll(`input[type=radio][name="${name.replace(/"/g, '\\"')}"]`)) if (other !== this) other.__checked = false;
  }
  get defaultChecked() { return this.__attrs.has('checked'); }
  set defaultChecked(v) { this.toggleAttribute('checked', Boolean(v)); }
  get indeterminate() { return Boolean(this.__indeterminate); }
  set indeterminate(v) { this.__indeterminate = Boolean(v); }
  get options() { return makeNodeList(this.querySelectorAll('option')); }
  get selectedOptions() {
    const opts = this.options;
    if (this.localName !== 'select') return [];
    const chosen = opts.filter(o => o.__selected === true || (o.__selected === undefined && o.__attrs.has('selected')));
    if (this.__attrs.has('multiple')) return makeNodeList(chosen);
    if (chosen.length) return makeNodeList([chosen[chosen.length - 1]]);
    if (this.__noSelection) return makeNodeList([]);
    const first = opts.find(o => !o.__attrs.has('disabled')) || opts[0];
    return makeNodeList(first ? [first] : []);
  }
  get selectedIndex() { const o = this.selectedOptions[0]; return o ? this.options.indexOf(o) : -1; }
  set selectedIndex(k) { this.options.forEach((o, i) => { o.__selected = i === k; }); if (k < 0) this.__noSelection = true; else delete this.__noSelection; }
  get selected() {
    if (this.localName !== 'option') return false;
    const select = this.closest('select');
    if (!select) return this.__selected ?? this.__attrs.has('selected');
    return select.selectedOptions.includes(this);
  }
  set selected(v) {
    const select = this.closest('select');
    if (v && select && !select.__attrs.has('multiple')) for (const o of select.options) o.__selected = false;
    this.__selected = Boolean(v);
    if (select) delete select.__noSelection;
  }
  get text() { return this.textContent; }
  set text(v) { this.textContent = v; }
  get label() { return this.__attrs.get('label') ?? this.textContent; }
  get form() { return this.closest('form'); }
  get labels() { const id = this.id; return makeNodeList(id ? this.ownerDocument.querySelectorAll(`label[for="${id}"]`) : []); }
  get elements() { return makeNodeList(this.querySelectorAll('input,select,textarea,button')); }
  get files() { return this.__files || makeNodeList([]); }
  set files(v) { this.__files = v; }
  get selectionStart() { return this.__selStart ?? this.value.length; }
  set selectionStart(v) { this.__selStart = v; }
  get selectionEnd() { return this.__selEnd ?? this.value.length; }
  set selectionEnd(v) { this.__selEnd = v; }
  setSelectionRange(a, b) { this.__selStart = a; this.__selEnd = b; }
  select() { this.__selStart = 0; this.__selEnd = this.value.length; }
  checkValidity() { return true; }
  reportValidity() { return true; }
  setCustomValidity(message) { this.__validity = String(message || ''); }
  get validationMessage() { return this.__validity || ''; }
  get validity() { return {valid: !this.__validity, customError: Boolean(this.__validity)}; }
  requestSubmit() { const e = new Event('submit', {bubbles: true, cancelable: true}); this.dispatchEvent(e); }
  submit() {}
  reset() { for (const el of this.querySelectorAll('input,textarea,select')) { el.__value = undefined; el.__checked = undefined; for (const o of el.localName === 'select' ? el.options : []) o.__selected = undefined; } }
  get tabIndex() { const v = this.__attrs.get('tabindex'); return v === undefined ? (['a', 'button', 'input', 'select', 'textarea'].includes(this.localName) ? 0 : -1) : parseInt(v, 10); }
  set tabIndex(v) { this.setAttribute('tabindex', v); }
  get isContentEditable() { for (let n = this; n && n.nodeType === 1; n = n.parentNode) { const v = n.__attrs.get('contenteditable'); if (v !== undefined) return v !== 'false'; } return false; }
  // Media / canvas / images
  play() { this.paused = false; this.dispatchEvent(new Event('play')); return Promise.resolve(); }
  pause() { this.paused = true; this.dispatchEvent(new Event('pause')); }
  load() {}
  canPlayType() { return 'maybe'; }
  getContext() { return new Proxy({canvas: this}, {get: (t, k) => (k in t ? t[k] : () => ({data: [], width: 0}))}); }
  toDataURL() { return 'data:image/png;base64,'; }
  toBlob(cb) { cb && cb(null); }
  get naturalWidth() { return this.__naturalWidth ?? 0; }
  get naturalHeight() { return this.__naturalHeight ?? 0; }
  get complete() { return this.__complete ?? true; }
  decode() { return Promise.resolve(); }
  get content() {
    if (this.localName !== 'template') return this.__attrs.get('content') ?? '';  // <meta content>
    if (!this.__content) { this.__content = this.ownerDocument.createDocumentFragment(); for (const n of this.childNodes.slice()) this.__content.appendChild(n); }
    return this.__content;
  }
  set content(value) { if (this.localName !== 'template') this.setAttribute('content', value); }
  setPointerCapture(id) { this.__pointer = id; }
  releasePointerCapture() { this.__pointer = undefined; }
  hasPointerCapture(id) { return this.__pointer === id; }
  requestFullscreen() { this.ownerDocument.fullscreenElement = this; return Promise.resolve(); }
  animate() { return {finished: Promise.resolve(), cancel() {}, onfinish: null, play() {}, pause() {}}; }
  getAnimations() { return []; }
  attachShadow() { const f = this.ownerDocument.createDocumentFragment(); this.shadowRoot = f; return f; }
}
for (const name of BOOL_ATTRS) {
  const prop = name === 'readonly' ? 'readOnly' : name === 'novalidate' ? 'noValidate' : name === 'contenteditable' ? 'contentEditableAttr' : name === 'playsinline' ? 'playsInline' : name;
  if (Object.getOwnPropertyDescriptor(Element.prototype, prop)) continue;
  Object.defineProperty(Element.prototype, prop, {
    get() { return this.__attrs.has(name); },
    set(v) { this.toggleAttribute(name, Boolean(v)); },
    configurable: true,
  });
}
Object.defineProperty(Element.prototype, 'contentEditable', {
  get() { return this.__attrs.get('contenteditable') ?? 'inherit'; },
  set(v) { if (v === 'inherit') this.removeAttribute('contenteditable'); else this.setAttribute('contenteditable', v); },
  configurable: true,
});
for (const [prop, attr] of Object.entries(STRING_ATTRS)) {
  if (!attr || Object.getOwnPropertyDescriptor(Element.prototype, prop)) continue;
  Object.defineProperty(Element.prototype, prop, {
    get() { return this.__attrs.get(attr) ?? ''; },
    set(v) { this.setAttribute(attr, v); },
    configurable: true,
  });
}

function serialize(node) {
  if (node.nodeType === 3) {
    const p = node.parentNode;
    return p && p.nodeType === 1 && (p.localName === 'script' || p.localName === 'style') ? node.data : escapeText(node.data);
  }
  if (node.nodeType === 8) return `<!--${node.data}-->`;
  if (node.nodeType === 11) return node.childNodes.map(serialize).join('');
  const attrs = [...node.__attrs].map(([k, v]) => v === '' && BOOL_ATTRS.includes(k) ? ` ${k}` : ` ${k}="${escapeAttr(v)}"`).join('');
  if (VOID.has(node.localName)) return `<${node.localName}${attrs}>`;
  const kids = node.localName === 'template' && node.__content ? node.__content.childNodes : node.childNodes;
  return `<${node.localName}${attrs}>${kids.map(serialize).join('')}</${node.localName}>`;
}

const BLOCK_TEXT = new Set(['div', 'p', 'li', 'ul', 'ol', 'section', 'header', 'footer', 'nav', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'table', 'form', 'pre', 'blockquote', 'dl', 'dt', 'dd', 'fieldset', 'aside', 'main', 'article', 'details', 'summary', 'figure', 'figcaption', 'label', 'option', 'button']);
function visibleText(el) {
  // Rough innerText: hidden subtrees dropped, block-level elements (by tag, by the
  // stylesheets' display, or as flex/grid items) on their own lines.
  const parts = [];
  const view = el.ownerDocument && el.ownerDocument.defaultView;
  const cssDisplay = n => (view && typeof view.__cssDisplay === 'function' ? view.__cssDisplay(n) : '');
  const walk = (node, parentDisplay) => {
    const closedDetails = node.nodeType === 1 && node.localName === 'details' && !node.__attrs.has('open');
    const summary = closedDetails ? node.children.find(c => c.localName === 'summary') : null;
    for (const n of node.childNodes) {
      if (closedDetails && n !== summary) continue;
      if (n.nodeType === 3) parts.push(n.data);
      else if (n.nodeType === 1) {
        if (n.localName === 'script' || n.localName === 'style' || n.localName === 'template') continue;
        if (n.__attrs.has('hidden')) continue;
        const display = cssDisplay(n) || (n.__style ? n.__style.getPropertyValue('display') : '');
        if (display === 'none') continue;
        if (n.localName === 'br') { parts.push('\n'); continue; }
        if (n.localName === 'input' || n.localName === 'textarea') continue;
        const block = /^(block|flex|grid|table|list-item|flow-root)/.test(display) || /(^|-)(flex|grid)$/.test(parentDisplay) || (!display && BLOCK_TEXT.has(n.localName));
        if (block) parts.push('\n');
        walk(n, display);
        if (block) parts.push('\n');
      }
    }
  };
  walk(el, cssDisplay(el));
  return parts.join('').replace(/[ \t ]+/g, m => m.includes(' ') ? m.replace(/[ \t]+/g, ' ') : ' ').replace(/ *\n[\s]*/g, '\n').trim();
}

/* ---------------------------------------------------------------- document */

class Range {
  constructor(doc) { this.__doc = doc; this.startContainer = doc; this.startOffset = 0; this.endContainer = doc; this.endOffset = 0; this.collapsed = true; }
  setStart(node, offset) { this.startContainer = node; this.startOffset = offset; this.collapsed = this.startContainer === this.endContainer && this.startOffset === this.endOffset; }
  setEnd(node, offset) { this.endContainer = node; this.endOffset = offset; this.collapsed = this.startContainer === this.endContainer && this.startOffset === this.endOffset; }
  setStartBefore(n) { this.setStart(n.parentNode, n.parentNode.childNodes.indexOf(n)); }
  setStartAfter(n) { this.setStart(n.parentNode, n.parentNode.childNodes.indexOf(n) + 1); }
  setEndBefore(n) { this.setEnd(n.parentNode, n.parentNode.childNodes.indexOf(n)); }
  setEndAfter(n) { this.setEnd(n.parentNode, n.parentNode.childNodes.indexOf(n) + 1); }
  selectNode(n) { this.setStartBefore(n); this.setEndAfter(n); }
  selectNodeContents(n) { this.setStart(n, 0); this.setEnd(n, n.nodeType === 3 ? n.data.length : n.childNodes.length); }
  collapse(toStart) { if (toStart) this.setEnd(this.startContainer, this.startOffset); else this.setStart(this.endContainer, this.endOffset); }
  cloneRange() { const r = new Range(this.__doc); r.setStart(this.startContainer, this.startOffset); r.setEnd(this.endContainer, this.endOffset); return r; }
  get commonAncestorContainer() { for (let n = this.startContainer; n; n = n.parentNode) if (n.contains(this.endContainer)) return n; return this.__doc; }
  getBoundingClientRect() { return Element.prototype.getBoundingClientRect.call({__rect: this.__rect}); }
  getClientRects() { return this.__rect ? [this.getBoundingClientRect()] : []; }
  toString() {
    if (this.startContainer === this.endContainer && this.startContainer.nodeType === 3) return this.startContainer.data.slice(this.startOffset, this.endOffset);
    return this.commonAncestorContainer.textContent || '';
  }
  detach() {}
  intersectsNode(n) { return this.commonAncestorContainer.contains(n); }
  // The nodes between the boundaries, cloned (text cut at the boundary offsets).
  cloneContents() {
    const doc = this.__doc, frag = doc.createDocumentFragment();
    const sc = this.startContainer, ec = this.endContainer;
    if (sc === ec && sc.nodeType === 3) { frag.appendChild(doc.createTextNode(sc.data.slice(this.startOffset, this.endOffset))); return frag; }
    const boundary = (container, offset) => container.nodeType === 3 ? {node: container, offset} : {node: container.childNodes[offset] || null, parent: container, offset: 0};
    const start = boundary(sc, this.startOffset), end = boundary(ec, this.endOffset);
    const order = [];
    const walk = n => { order.push(n); for (const c of n.childNodes) walk(c); };
    walk(this.commonAncestorContainer);
    const indexOf = b => b.node ? order.indexOf(b.node) : (() => { let last = b.parent; while (last.lastChild) last = last.lastChild; return order.indexOf(last) + 1; })();
    const from = indexOf(start), to = indexOf(end);
    const inside = new Set(order.slice(from, end.node && end.node.nodeType !== 3 ? to : to + 1));
    const copy = node => {
      if (node.nodeType === 3) {
        let text = node.data;
        if (node === ec && ec.nodeType === 3) text = text.slice(0, this.endOffset);
        if (node === sc && sc.nodeType === 3) text = text.slice(node === ec ? this.startOffset : this.startOffset);
        return doc.createTextNode(text);
      }
      const el = node.cloneNode(false);
      for (const c of node.childNodes) if (inside.has(c) || [...inside].some(x => c.contains(x))) el.appendChild(copy(c));
      return el;
    };
    for (const c of this.commonAncestorContainer.childNodes) if (inside.has(c) || [...inside].some(x => c.contains(x))) frag.appendChild(copy(c));
    return frag;
  }
}

class Selection {
  constructor(doc) { this.__doc = doc; this.__ranges = []; }
  get rangeCount() { return this.__ranges.length; }
  get isCollapsed() { return !this.__ranges.length || this.__ranges[0].collapsed; }
  get anchorNode() { return this.__ranges[0] ? this.__ranges[0].startContainer : null; }
  get anchorOffset() { return this.__ranges[0] ? this.__ranges[0].startOffset : 0; }
  get focusNode() { return this.__ranges[0] ? this.__ranges[0].endContainer : null; }
  get focusOffset() { return this.__ranges[0] ? this.__ranges[0].endOffset : 0; }
  get type() { return !this.__ranges.length ? 'None' : this.isCollapsed ? 'Caret' : 'Range'; }
  getRangeAt(k) { if (!this.__ranges[k]) throw new Error('IndexSizeError'); return this.__ranges[k]; }
  addRange(r) { this.__ranges = [r]; }
  removeAllRanges() { this.__ranges = []; }
  empty() { this.__ranges = []; }
  collapse(node, offset = 0) { const r = new Range(this.__doc); r.setStart(node, offset); r.setEnd(node, offset); this.__ranges = [r]; }
  selectAllChildren(node) { const r = new Range(this.__doc); r.selectNodeContents(node); this.__ranges = [r]; }
  containsNode(n) { return this.__ranges.some(r => r.intersectsNode(n)); }
  toString() { return this.__ranges.map(r => r.toString()).join(''); }
}

class Document extends ParentNode {
  constructor() {
    super(null);
    this.defaultView = null;
    this.activeElement = null;
    this.fullscreenElement = null;
    this.visibilityState = 'visible';
    this.hidden = false;
    this.readyState = 'loading';
    this.cookie = '';
    this.referrer = '';
    this.__selection = new Selection(this);
    this.fonts = {ready: Promise.resolve(), check: () => true, load: () => Promise.resolve([])};
    this.__hasFocus = true;
  }
  get nodeType() { return 9; }
  get nodeName() { return '#document'; }
  get documentElement() { return this.childNodes.find(n => n.nodeType === 1) || null; }
  get head() { const h = this.documentElement; return h ? h.children.find(n => n.localName === 'head') || null : null; }
  get body() { const h = this.documentElement; return h ? h.children.find(n => n.localName === 'body') || null : null; }
  get title() { const t = this.querySelector('title'); return t ? t.textContent : ''; }
  set title(v) { let t = this.querySelector('title'); if (!t && this.head) { t = this.createElement('title'); this.head.appendChild(t); } if (t) t.textContent = v; }
  get scrollingElement() { return this.documentElement; }
  createElement(name) { return new Element(this, name); }
  createElementNS(_ns, name) { return new Element(this, name); }
  createTextNode(text) { return new Text(this, text); }
  createComment(text) { return new Comment(this, text); }
  createDocumentFragment() { return new DocumentFragment(this); }
  createRange() { return new Range(this); }
  createEvent() { return new Event(''); }
  createTreeWalker(root, whatToShow = 0xFFFFFFFF, filter = null) {
    const accept = n => {
      const bit = n.nodeType === 1 ? 0x1 : n.nodeType === 3 ? 0x4 : n.nodeType === 8 ? 0x80 : 0;
      if (!(whatToShow & bit)) return false;
      if (!filter) return true;
      const f = typeof filter === 'function' ? filter : filter.acceptNode;
      return f.call(filter, n) === 1;
    };
    const order = [];
    const walk = n => { for (const c of n.childNodes) { order.push(c); walk(c); } };
    walk(root);
    let at = -1;
    const walker = {
      root, currentNode: root,
      nextNode() { for (let k = at + 1; k < order.length; k++) if (accept(order[k])) { at = k; walker.currentNode = order[k]; return order[k]; } return null; },
      previousNode() { for (let k = at - 1; k >= 0; k--) if (accept(order[k])) { at = k; walker.currentNode = order[k]; return order[k]; } return null; },
    };
    return walker;
  }
  importNode(node, deep) { return node.cloneNode(deep); }
  adoptNode(node) { if (node.parentNode) node.parentNode.removeChild(node); return node; }
  getElementById(id) {
    // Cached by id; a stale or missing entry rebuilds the index with one walk.
    const want = String(id);
    if (this.__ids && this.__idsVersion === this.__version) return this.__ids.get(want) || null;
    const hit = this.__ids && this.__ids.get(want);
    if (hit && hit.__attrs.get('id') === want && hit.getRootNode() === this) return hit;
    const ids = new Map();
    const walk = node => { for (const n of node.childNodes) { if (n.nodeType !== 1) continue; const v = n.__attrs.get('id'); if (v !== undefined && !ids.has(v)) ids.set(v, n); walk(n); } };
    walk(this);
    this.__ids = ids;
    this.__idsVersion = this.__version;
    return ids.get(want) || null;
  }
  getElementsByName(name) { return this.querySelectorAll(`[name="${String(name).replace(/"/g, '\\"')}"]`); }
  getSelection() { return this.__selection; }
  hasFocus() { return this.__hasFocus; }
  elementFromPoint(x, y) { const v = this.defaultView; return v && v.__elementFromPoint ? v.__elementFromPoint(x, y) : null; }
  elementsFromPoint(x, y) { const e = this.elementFromPoint(x, y); return e ? [e] : []; }
  caretRangeFromPoint() { return null; }
  caretPositionFromPoint() { return null; }
  exitFullscreen() { this.fullscreenElement = null; return Promise.resolve(); }
  execCommand() { return false; }
  queryCommandSupported() { return false; }
  open() { return this; }
  write() {}
  close() {}
  __focus(el, options) {
    const prev = this.activeElement;
    if (prev === el) return;
    const FE = this.defaultView ? this.defaultView.FocusEvent : FocusEvent;
    this.activeElement = el;
    if (prev && prev !== this.body) { prev.dispatchEvent(new FE('blur', {relatedTarget: el})); prev.dispatchEvent(new FE('focusout', {bubbles: true, relatedTarget: el})); }
    if (el && el !== this.body) { el.dispatchEvent(new FE('focus', {relatedTarget: prev})); el.dispatchEvent(new FE('focusin', {bubbles: true, relatedTarget: prev})); }
    if (options && options.preventScroll === false) el.scrollIntoView();
  }
}

function createDocument(html) {
  const doc = new Document();
  parseHTML(String(html || ''), doc, doc);
  // Make sure html/head/body exist.
  let root = doc.documentElement;
  if (!root || root.localName !== 'html') {
    const wrap = doc.createElement('html');
    for (const n of doc.childNodes.slice()) wrap.appendChild(n);
    doc.appendChild(wrap);
    root = wrap;
  }
  if (!doc.head) root.insertBefore(doc.createElement('head'), root.firstChild);
  if (!doc.body) {
    const body = doc.createElement('body');
    for (const n of root.childNodes.slice()) if (n !== doc.head) body.appendChild(n);
    root.appendChild(body);
  }
  doc.activeElement = doc.body;
  return doc;
}

class DOMParser {
  parseFromString(text) { return createDocument(text); }
}

module.exports = {
  createDocument, parseHTML, serialize, compileSelector, matchSeq, decode,
  Node, Text, Comment, Element, DocumentFragment, Document, Range, Selection, DOMTokenList, DOMParser,
  Event, CustomEvent, UIEvent, KeyboardEvent, MouseEvent, PointerEvent, WheelEvent, FocusEvent, InputEvent, DragEvent, ClipboardEvent, EventTarget,
};
