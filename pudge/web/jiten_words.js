/* One lexical model for the word popup, sidebar and pre-episode review. */
(() => {
  const inflight = new Map(), hydration = new WeakMap(), warmed = new Map();
  const kana = value => String(value || '').normalize('NFKC')
    .replace(/([^\[\]]+)\[([^\]]+)\]/g, (_, base, reading) => {
      const prefix = base.match(/^[^\u3400-\u9fff々〆ヵヶ]*/u)?.[0] || '';
      return prefix + reading;
    }).replace(/\s+/g, '').replace(/[ァ-ヶ]/g, c => String.fromCharCode(c.charCodeAt(0) - 0x60));
  function normalize(card = {}) {
    const rows = [
      ...(card.mainReading && typeof card.mainReading === 'object' ? [card.mainReading] : []),
      ...(Array.isArray(card.readings) ? card.readings : []),
      ...(Array.isArray(card.alternativeReadings) ? card.alternativeReadings : []),
    ];
    const selected = rows.find(row => Number(row?.readingIndex) === Number(card.readingIndex));
    const reading = row => typeof row === 'string' ? row : row?.reading || row?.rubyText || row?.text || '';
    const primary = kana(card.primary_reading || card.reading || reading(selected) || reading(rows[0]));
    const all = [...new Set([primary, ...(Array.isArray(card.all_readings) ? card.all_readings : []), ...rows.map(reading)]
      .map(kana).filter(value => value && !/[\u3400-\u9fff々〆ヵヶ]/u.test(value)))];
    return {...card, readings:rows, primary_reading:primary || all[0] || '', all_readings:all};
  }
  function url(card = {}) {
    const wordId = Number(card.wordId), readingIndex = Number(card.readingIndex);
    return Number.isSafeInteger(wordId) && wordId > 0 && Number.isSafeInteger(readingIndex) && readingIndex >= 0
      ? `https://jiten.moe/vocabulary/${wordId}/${readingIndex}` : '';
  }
  async function openInJiten(card) {
    const target = url(card);
    if (!target) return false;
    await window.pywebview?.api?.open_url?.(target);
    return true;
  }
  function readingsLine(card = {}) {
    const model = normalize(card);
    const surface = String(card.wordTextPlain || card.spelling || card.wordText || '').replace(/\[[^\]]+\]/g, '').replace(/\s+/g, '');
    return model.all_readings.filter(value => value !== model.primary_reading && value !== kana(surface)).join(' ・ ');
  }
  async function enrich(card) {
    const api = window.pywebview?.api;
    if (!api?.study_word) return normalize(card);
    // Account identity comes from the backend; this map only joins simultaneous calls.
    const scope = String((await api.study_provider_capabilities?.("jiten"))?.account_key || "");
    const key = `${scope}:${card.wordId}:${card.readingIndex}`;
    if (inflight.has(key)) return inflight.get(key);
    const task = Promise.resolve(api.study_word(card)).then(async value => {
      const current = String((await api.study_provider_capabilities?.("jiten"))?.account_key || "");
      return normalize(current === scope ? value : card);
    }).finally(() => inflight.delete(key));
    inflight.set(key, task);
    return task;
  }
  async function prefetch(cards = []) {
    const api = window.pywebview?.api;
    if (!api?.study_word) return;
    let scope;
    try { scope = String((await api.study_provider_capabilities?.('jiten'))?.account_key || ''); }
    catch { return; }
    // Current card plus eight ahead; rendering and grading never await this work.
    for (const card of cards.slice(0, 9)) {
      if (!card?.wordId) continue;
      const key = `${scope}:${card.wordId}:${card.readingIndex}`;
      if (Date.now() - (warmed.get(key) || 0) < 30000) continue;
      warmed.delete(key);
      warmed.set(key, Date.now());
      while (warmed.size > 2048) warmed.delete(warmed.keys().next().value);
      void enrich(card).catch(() => warmed.delete(key));
    }
  }
  function hydrateReadings(card, root, selector, className, current) {
    const identity = `${card.wordId}:${card.readingIndex}`;
    let entry = hydration.get(root);
    const fresh = !entry || entry.identity !== identity;
    if (fresh) { entry = {identity, detail:null, pending:false}; hydration.set(root, entry); }
    const valid = () => { const value = current(); return hydration.get(root) === entry && value && `${value.wordId}:${value.readingIndex}` === identity && root.isConnected !== false; };
    const paint = detail => {
      if (!valid() || !detail) return;
      const parent = root.querySelector(selector);
      if (!parent) return;
      const text = readingsLine(detail);
      let line = parent.querySelector(`.${className}`);
      if (!text) { line?.remove(); return; }
      if (!line) { line = document.createElement('div'); line.className = className; parent.prepend(line); }
      line.textContent = text;
    };
    const refresh = async () => {
      if (!valid() || entry.pending || entry.detail?.dictionary_complete) return;
      entry.pending = true;
      try { entry.detail = await enrich(card); paint(entry.detail); }
      finally { entry.pending = false; }
    };
    paint(entry.detail);
    if (!fresh) return;
    void refresh().catch(() => {});
    for (const delay of [750, 2000, 5000, 10000, 20000, 40000, 60000]) setTimeout(() => { void refresh().catch(() => {}); }, delay);
  }
  window.PudgeJitenWords = {normalize, kana, url, openInJiten, readingsLine, enrich, prefetch, hydrateReadings};
})();
