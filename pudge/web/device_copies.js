'use strict';
// Downloaded on a paired iPhone/iPad: a small icon on the library's cards and rows, the
// devices in the usual tooltip ("On iPad · iPhone", no counts). The phones report what they have
// downloaded (pudge/iphone_inventory.py); iphone_device_copies() answers per library item.
//
// Renderers put a slot in their markup: PudgeDeviceCopies.slot(kind, ids, place)
//   kind   'anime' (media ids), 'episode' ("<media id>:<episode number>"), 'ln', 'manga',
//          'audiobook' (the window's item ids)
//   place  'corner' (anime cards: top-left corner), 'info' (a card's title line),
//          'row' (an episode or volume row inside a title: device names only)
// The slot is filled from the last answer and updated in place when a new one arrives
// (a phone reported, a device was revoked), so cards are not redrawn for it.
(() => {
  const g = globalThis;
  // A missed push only delays an update; the "as of" dates also age without one.
  const POLL_MS = 10 * 60 * 1000;
  const KINDS = new Set(['anime', 'episode', 'ln', 'manga', 'audiobook']);
  const ICONS = {
    phone: '<svg viewBox="0 0 10 16" aria-hidden="true" focusable="false"><rect x="1.2" y="1" width="7.6" height="14" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M4 12.6h2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
    pad: '<svg viewBox="0 0 14 16" aria-hidden="true" focusable="false"><rect x="1" y="1" width="12" height="14" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="7" cy="12.4" r=".9" fill="currentColor"/></svg>',
  };
  let data = null;
  let loading = null;
  let again = false;
  let timer = null;

  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const t = (key, params) => g.PudgeI18n.t(key, params);

  function dateText(ms) {
    const date = new Date(Number(ms) || 0);
    try { return date.toLocaleDateString(t('ui.enUs'), {day: 'numeric', month: 'short'}); }
    catch (_) { return date.toISOString().slice(0, 10); }
  }

  function idList(ids) {
    const values = Array.isArray(ids) ? ids : [ids];
    return [...new Set(values.map(value => String(value ?? '').trim()).filter(value => value && value !== '0' && value !== 'undefined' && value !== 'null'))];
  }

  // device id -> how many of the items it has (downloaded episodes for an anime).
  function holdings(kind, ids) {
    const counts = new Map();
    const add = (device, count) => { if (count > 0) counts.set(String(device), (counts.get(String(device)) || 0) + count); };
    if (!data) return counts;
    if (kind === 'anime') {
      for (const id of ids) for (const [device, numbers] of Object.entries(data.anime?.[id] || {})) add(device, (numbers || []).length);
    } else if (kind === 'episode') {
      for (const id of ids) {
        const [media, number] = id.split(':');
        for (const [device, numbers] of Object.entries(data.anime?.[media] || {})) if ((numbers || []).map(Number).includes(Number(number))) add(device, 1);
      }
    } else {
      const table = (kind === 'audiobook' ? data.audiobooks : data[kind]) || {};
      for (const id of ids) for (const device of new Set(table[id] || [])) add(device, 1);
    }
    return counts;
  }

  // What a slot shows: the tooltip text and which glyphs (phone, pad); null when nothing.
  function describe(kind, ids, place = 'info') {
    const list = idList(ids);
    const counts = holdings(kind, list);
    const devices = (data?.devices || []).filter(device => counts.get(String(device.id)) > 0);
    if (!devices.length) return null;
    const name = device => device.stale ? t('deviceCopies.asOf', {device: device.name, date: dateText(device.reportedAt)}) : String(device.name || '');
    // Which devices have it, the same for one item and for several (an anime's episodes, a
    // series' volumes): "On iPad · iPhone", without counts.
    const text = t('deviceCopies.on', {devices: devices.map(name).join(' · ')});
    const idioms = ['phone', 'pad'].filter(idiom => devices.some(device => (device.idiom === 'pad' ? 'pad' : 'phone') === idiom));
    return {text, idioms, stale: devices.every(device => device.stale)};
  }

  function glyphs(view) { return view.idioms.map(idiom => ICONS[idiom]).join(''); }

  function slot(kind, ids, place = 'info') {
    const list = idList(ids);
    if (!list.length || !KINDS.has(kind)) return '';
    const view = describe(kind, list, place);
    const attrs = `data-device-copies="${esc(kind)}" data-device-ids="${esc(list.join(','))}" data-device-place="${esc(place)}"`;
    const classes = `device-copies device-copies-${esc(place)}${view?.stale ? ' stale' : ''}`;
    if (!view) return `<span class="${classes}" ${attrs} hidden></span>`;
    return `<span class="${classes}" ${attrs} role="img" data-tooltip="${esc(view.text)}" aria-label="${esc(view.text)}">${glyphs(view)}</span>`;
  }

  function paint(node) {
    const kind = node.dataset.deviceCopies, place = node.dataset.devicePlace || 'info';
    const view = describe(kind, String(node.dataset.deviceIds || '').split(','), place);
    node.classList.toggle('stale', Boolean(view?.stale));
    if (!view) {
      if (!node.hidden) node.hidden = true;
      delete node.dataset.tooltip;
      node.removeAttribute('aria-label');
      node.removeAttribute('role');
      if (node.innerHTML) node.innerHTML = '';
      return;
    }
    node.hidden = false;
    node.setAttribute('role', 'img');
    if (node.dataset.tooltip !== view.text) node.dataset.tooltip = view.text;
    if (node.getAttribute('aria-label') !== view.text) node.setAttribute('aria-label', view.text);
    const html = glyphs(view);
    if (node.innerHTML !== html) node.innerHTML = html;
  }

  function apply(root = g.document) {
    for (const node of root?.querySelectorAll?.('[data-device-copies]') || []) paint(node);
  }

  function accept(value) {
    data = value && typeof value === 'object' && Array.isArray(value.devices) ? value : null;
    apply();
  }

  async function reload() {
    if (loading) { again = true; return loading; }
    const api = g.pywebview?.api;
    if (!api?.iphone_device_copies) return null;
    loading = (async () => {
      try { accept(await api.iphone_device_copies()); }
      catch (error) { console.error(error); }
    })();
    try { await loading; } finally { loading = null; }
    if (again) { again = false; return reload(); }
    return data;
  }

  function boot() {
    void reload();
    if (!timer) timer = setInterval(() => { if (!g.document?.hidden) void reload(); }, POLL_MS);
  }

  // The corner icon sits on covers that are links or play buttons (Planning: AniList): a click on
  // it only shows its tooltip, it does not open or start anything.
  g.document?.addEventListener?.('click', event => {
    if (event.target?.closest?.('.device-copies-corner')) { event.stopPropagation(); event.preventDefault(); }
  }, true);

  g.PudgeDeviceCopies = {slot, apply, accept, reload, changed: () => void reload(), describe, data: () => data};
  if (g.pywebview?.api) boot();
  else g.addEventListener?.('pywebviewready', boot, {once: true});
})();
