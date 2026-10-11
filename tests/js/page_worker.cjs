'use strict';
/* Long-running worker for tests/page_harness.py: one JSON request per line on stdin,
one JSON response per line on stdout. Each request opens a fresh page. */

const readline = require('node:readline');
const {openPage} = require('./page.cjs');

const out = process.stdout;
const lines = readline.createInterface({input: process.stdin});
let queue = Promise.resolve();

function plain(value, depth = 0) {
  // JSON-safe copy of what a test returns (DOM nodes become their outerHTML).
  if (value === undefined) return null;
  if (value === null || typeof value === 'number' || typeof value === 'string' || typeof value === 'boolean') return value;
  if (depth > 20) return '[deep]';
  if (typeof value === 'function') return `[function ${value.name || ''}]`;
  if (value.nodeType === 1 || value.nodeType === 11) return {outerHTML: value.outerHTML ?? value.innerHTML, text: value.innerText ?? value.textContent};
  if (value.nodeType === 3) return value.data;
  if (value instanceof Error || (value && typeof value.stack === 'string' && typeof value.message === 'string')) return {error: String(value.message), stack: String(value.stack)};
  if (Array.isArray(value) || (typeof value.length === 'number' && typeof value !== 'string' && typeof value[Symbol.iterator] === 'function')) return [...value].map(v => plain(v, depth + 1));
  if (value instanceof Map || Object.prototype.toString.call(value) === '[object Map]') return Object.fromEntries([...value].map(([k, v]) => [String(k), plain(v, depth + 1)]));
  if (value instanceof Set || Object.prototype.toString.call(value) === '[object Set]') return [...value].map(v => plain(v, depth + 1));
  const outObj = {};
  for (const [k, v] of Object.entries(value)) outObj[k] = plain(v, depth + 1);
  return outObj;
}

const describe = error => (error && error.stack) ? String(error.stack).split('\n').slice(0, 6).join('\n') : String(error);

async function handle(request) {
  const options = {...(request.options || {}), allowErrors: true};
  if (request.before) options.before = request.before;
  let page = null;
  const response = {id: request.id, ok: true};
  try {
    page = await openPage(options);
    response.loadErrors = page.errors.map(describe);
    const loadErrorCount = page.errors.length;
    if (request.run) response.value = plain(await page.run(request.run));
    response.errors = page.errors.slice(loadErrorCount).map(describe);
  } catch (error) {
    response.ok = false;
    response.failure = describe(error);
  }
  if (page) {
    response.console = page.console.filter(c => c.level === 'error' || c.level === 'warn').map(c => `${c.level}: ${c.text}`.slice(0, 2000));
    response.calls = page.calls.map(c => ({name: c.name, args: c.args, at: c.at}));
    response.dialogs = page.window.__dialogs;
    response.clipboard = page.window.__clipboard;
    response.opened = page.window.__opened;
    page.close();
  }
  return response;
}

lines.on('line', line => {
  if (!line.trim()) return;
  queue = queue.then(async () => {
    let request;
    try { request = JSON.parse(line); } catch (error) { out.write(JSON.stringify({ok: false, failure: `bad request: ${error.message}`}) + '\n'); return; }
    let response;
    try { response = await handle(request); } catch (error) { response = {id: request.id, ok: false, failure: describe(error)}; }
    let text;
    try { text = JSON.stringify(response); } catch (error) { text = JSON.stringify({id: request.id, ok: false, failure: `result is not JSON: ${error.message}`}); }
    out.write(text + '\n');
  });
});
lines.on('close', () => { queue.then(() => process.exit(0)); });
