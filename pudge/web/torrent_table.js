/* Downloads window: one row per torrent, sortable columns, selection and actions on many.
 *
 * The window showed one large card per torrent with its own buttons: with a dozen
 * torrents nothing could be compared, sorted or removed together. A row opens the
 * old card with all details below it. Click a checkbox to select, Shift+click for a
 * range, Cmd/Ctrl+click on a row to add it to the selection.
 */
const TORRENT_STATE_ORDER = {downloading: 0, checking: 1, stalled: 2, waiting: 3, unconfirmed: 4, paused: 5, error: 6, unknown: 7, finished: 8};
const TORRENT_SORT_KEYS = {
  name: row => String(row.anime_title || row.name || '').toLocaleLowerCase(),
  size: row => Number(row.total_bytes || 0),
  progress: row => Number(row.progress || 0),
  down: row => Number(row.download_speed || 0),
  up: row => Number(row.upload_speed || 0),
  eta: row => { const value = Number(row.eta_seconds || 0); return value > 0 ? value : Infinity; },
  seeds: row => Number(row.seeders || 0),
  state: row => TORRENT_STATE_ORDER[torrentRowKind(row)] ?? 9,
  added: row => Number(row.added_on || 0),
};
const TORRENT_COLUMNS = [
  ['name', 'uiB.downloads.colName'], ['size', 'uiB.downloads.colSize'], ['progress', 'uiB.downloads.colProgress'],
  ['down', null, '↓'], ['up', null, '↑'], ['eta', 'uiB.downloads.eta'], ['seeds', 'uiB.downloads.colSeeds'],
  ['state', 'uiB.downloads.colState'], ['added', 'uiB.downloads.colAdded'],
];

function torrentTableState() {
  if (!ui.torrentTable) ui.torrentTable = {sort: 'added', dir: -1, selected: new Set(), expanded: new Set(), collapsed: new Set(), anchor: '', order: []};
  return ui.torrentTable;
}
function torrentRowHash(row) { return String(row?.hash || ''); }
function torrentRowKind(row) {
  const enabled = torrentToggleUiEnabled(), live = ui.torrentTraffic || {};
  const offConfirmed = !enabled && torrentOffConfirmed(live);
  return torrentUiState(row, enabled, offConfirmed ? 'off_confirmed' : 'unconfirmed');
}
// The only torrent in view (a title's own downloads from its card menu) is open
// unless it was closed.
function torrentRowExpanded(hash) {
  const state = torrentTableState();
  return state.expanded.has(hash) || (state.order.length === 1 && state.order[0] === hash && !state.collapsed.has(hash));
}
function torrentSortedRows(rows) {
  const state = torrentTableState(), key = TORRENT_SORT_KEYS[state.sort] || TORRENT_SORT_KEYS.added;
  return rows.map((row, index) => [row, index, key(row)])
    .sort((a, b) => (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0) * state.dir || a[1] - b[1])
    .map(([row]) => row);
}
function torrentAddedText(row) {
  const added = Number(row.added_on || 0);
  if (!added) return '—';
  const date = new Date(added * 1000), today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString(ui.lang || undefined, {hour: '2-digit', minute: '2-digit'})
    : date.toLocaleDateString(ui.lang || undefined, {day: 'numeric', month: 'short'});
}
function torrentRowHtml(row, observed, enabled) {
  const state = torrentTableState(), hash = torrentRowHash(row), kind = torrentRowKind(row);
  const selected = state.selected.has(hash), expanded = torrentRowExpanded(hash), finished = kind === 'finished';
  const percent = Math.max(0, Math.min(100, Math.round(Number(row.progress || 0) * 1000) / 10));
  const episode = row.batch ? PudgeI18n.t('ui.batch') : (row.episode ? `${PudgeI18n.t('table.episode')} ${Number(row.episode)}` : '');
  const title = row.anime_title || row.name || t('label.unknownAnime');
  const eta = finished || !observed || !enabled ? '—' : torrentEta(row.eta_seconds);
  return `<div class="torrent-row-group${expanded ? ' expanded' : ''}" data-torrent-group="${escapeHtml(hash)}" data-patch-key="torrent:${escapeHtml(hash)}">`
    + `<div class="torrent-row${selected ? ' selected' : ''} ${escapeHtml(kind)}" data-torrent-row="${escapeHtml(hash)}" aria-selected="${selected}">`
    + `<label class="torrent-check"><input type="checkbox" data-torrent-select="${escapeHtml(hash)}"${selected ? ' checked' : ''} aria-label="${escapeHtml(title)}"></label>`
    + `<div class="torrent-name" title="${escapeHtml(row.name || '')}"><strong>${escapeHtml(title)}</strong><small>${[episode, String(row.name || '')].filter(Boolean).map(escapeHtml).join(' · ')}</small></div>`
    + `<span class="torrent-num">${torrentBytes(row.total_bytes)}</span>`
    + `<div class="torrent-progress"><div class="download-progress" title="${percent}%"><span style="width:${percent}%"></span></div><small>${percent}%</small></div>`
    + `<span class="torrent-num">${torrentRowRate(row, false, observed)}</span><span class="torrent-num">${torrentRowRate(row, true, observed)}</span>`
    + `<span class="torrent-num">${eta}</span><span class="torrent-num">${Number(row.seeders || 0)}/${Number(row.peers || 0)}</span>`
    + `<span class="download-state">${escapeHtml(torrentUiLabel(kind, ui.lang))}</span><span class="torrent-num">${escapeHtml(torrentAddedText(row))}</span>`
    + `</div>${torrentCard(row).replace('<article class="download-card"', `<article class="download-card"${expanded ? '' : ' hidden'}`)}</div>`;
}
function torrentTableHtml(rows, observed, enabled) {
  const state = torrentTableState(), sorted = torrentSortedRows(rows);
  state.order = sorted.map(torrentRowHash);
  // Only rows on screen stay selected: a filter or a removal takes the others out.
  for (const hash of [...state.selected]) if (!state.order.includes(hash)) state.selected.delete(hash);
  const visibleSelected = state.order.filter(hash => state.selected.has(hash)).length;
  const allChecked = sorted.length > 0 && visibleSelected === sorted.length;
  const head = TORRENT_COLUMNS.map(([key, label, text]) => {
    const arrow = state.sort === key ? (state.dir > 0 ? ' ▲' : ' ▼') : '';
    return `<button type="button" class="torrent-sort${state.sort === key ? ' active' : ''}" data-torrent-sort="${key}">${text || PudgeI18n.t(label)}${arrow}</button>`;
  }).join('');
  const count = state.selected.size;
  const bulk = count ? `<div class="torrent-bulk"><strong>${PudgeI18n.t('uiB.downloads.selected', {count})}</strong>`
    + `<button type="button" data-torrent-bulk="pause">${PudgeI18n.t('ui.pause')}</button><button type="button" data-torrent-bulk="resume">${PudgeI18n.t('ui.resume')}</button>`
    + `<button type="button" class="danger-action" data-torrent-bulk="remove">${PudgeI18n.t('ui.remove')}</button><button type="button" class="danger-action" data-torrent-bulk="remove-files">${PudgeI18n.t('ui.removeFiles')}</button>`
    + `<span class="spacer"></span><button type="button" data-torrent-bulk="clear">${PudgeI18n.t('uiB.downloads.clearSelection')}</button></div>` : '';
  return `${bulk}<div class="torrent-table"><div class="torrent-head"><label class="torrent-check"><input type="checkbox" data-torrent-select-all${allChecked ? ' checked' : ''} aria-label="${escapeHtml(PudgeI18n.t('uiB.downloads.selectAll'))}"></label>${head}</div>`
    + `${sorted.map(row => torrentRowHtml(row, observed, enabled)).join('')}</div>`;
}
function torrentTableRender() { if (downloadCenterShown()) renderDownloadCenter(ui.downloadCenter); }
function torrentToggleSelection(hash, range) {
  const state = torrentTableState(), select = !state.selected.has(hash);
  const from = state.order.indexOf(state.anchor), to = state.order.indexOf(hash);
  const hashes = range && from >= 0 && to >= 0 ? state.order.slice(Math.min(from, to), Math.max(from, to) + 1) : [hash];
  for (const value of hashes) if (select) state.selected.add(value); else state.selected.delete(value);
  state.anchor = hash;
}
async function torrentBulkAction(kind, button) {
  const state = torrentTableState();
  if (kind === 'clear') { state.selected.clear(); torrentTableRender(); return; }
  // In the order on screen.
  const byHash = new Map((ui.downloadCenter?.downloads || []).map(row => [torrentRowHash(row), row]));
  const rows = state.order.filter(hash => state.selected.has(hash) && byHash.has(hash)).map(hash => byHash.get(hash));
  if (!rows.length) return;
  const destructive = kind === 'remove-files', removing = kind === 'remove' || destructive;
  if (removing) {
    const key = destructive ? 'uiB.downloads.removeSelectedFilesConfirm' : 'uiB.downloads.removeSelectedConfirm';
    if (!await pudgeConfirm(PudgeI18n.t(key, {count: rows.length}), {danger: true})) return;
  }
  button.disabled = true;
  let result = null, failed = 0;
  try {
    for (const row of rows) {
      try {
        const answer = await pywebview.api.torrent_download_action(row.hash, removing ? 'remove' : kind, destructive, row.backend_id || row.backend || '');
        if (answer?.blocked_by_global_off) { toast(PudgeI18n.t('ui.enableTorrentsBeforeResuming')); break; }
        result = answer;
      } catch (error) { failed += 1; }
    }
  } finally { button.disabled = false; }
  if (removing) for (const row of rows) state.selected.delete(torrentRowHash(row));
  // The window was closed (or another dialog took it) while the client worked through the
  // rows: the answer must not bring the Downloads window back over what is open now.
  if (result && downloadCenterShown()) {
    const mediaId = ui.downloadCenter?.mediaId ?? null;
    if (mediaId != null) result.downloads = (result.downloads || []).filter(item => Number(item.media_id) === Number(mediaId));
    ui.downloadCenter = {...result, mediaId};
    reapplyLiveDownloadProgress();
  }
  torrentTableRender();
  if (failed) toast(PudgeI18n.t('uiB.downloads.bulkFailed', {count: failed}));
}
document.addEventListener('click', event => {
  const target = event.target;
  if (!target?.closest?.('.torrent-table, .torrent-bulk')) return;
  const sort = target.closest('[data-torrent-sort]');
  if (sort) {
    const state = torrentTableState(), key = sort.dataset.torrentSort;
    state.dir = state.sort === key ? -state.dir : (key === 'name' ? 1 : -1);
    state.sort = key;
    torrentTableRender();
    return;
  }
  if (target.closest('[data-torrent-select-all]')) {
    const state = torrentTableState(), all = state.order.length > 0 && state.order.every(hash => state.selected.has(hash));
    for (const hash of state.order) if (all) state.selected.delete(hash); else state.selected.add(hash);
    torrentTableRender();
    return;
  }
  const check = target.closest('[data-torrent-select]');
  if (check) { torrentToggleSelection(check.dataset.torrentSelect, event.shiftKey); torrentTableRender(); return; }
  const bulk = target.closest('[data-torrent-bulk]');
  if (bulk) { void torrentBulkAction(bulk.dataset.torrentBulk, bulk); return; }
  const row = target.closest('[data-torrent-row]');
  if (row && !target.closest('button, a, input, label')) {
    const state = torrentTableState(), hash = row.dataset.torrentRow;
    if (event.metaKey || event.ctrlKey || event.shiftKey) torrentToggleSelection(hash, event.shiftKey);
    else if (torrentRowExpanded(hash)) { state.expanded.delete(hash); state.collapsed.add(hash); }
    else { state.expanded.add(hash); state.collapsed.delete(hash); }
    torrentTableRender();
  }
});
