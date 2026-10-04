'use strict';

(() => {
  const API = () => window.pywebview && window.pywebview.api;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[ch]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
  function llmAvailable(settings = (typeof ui !== 'undefined' ? ui : window.ui)?.state?.settings || {}) {
    if (!settings.llm_enabled || !String(settings.llm_model || '').trim()) return false;
    const url = String(settings.llm_url || '').trim();
    if (!/^https?:\/\/[^\s/]+/i.test(url)) return false;
    // Hosted OpenAI requires a key; compatible/local gateways can accept anonymous requests.
    if (settings.llm_provider === 'openai' && /^https?:\/\/api\.openai\.com(?::\d+)?(?:\/|$)/i.test(url)
      && !String(settings.llm_api_key || '').trim()) return false;
    return true;
  }
  const STUDY_THEMES = new Set(['balanced','jiten','jpdb','focus','underline','none','custom']);
  const STUDY_COLORS = {
    new: '#f3f6fb', learning: '#f4bd63', due: '#ff7d8c',
    known: '#57d38c', blacklisted: '#7d8795', unknown: '#9aa8ba',
  };

  const registry = new Map();
  let tokenSequence = 0;
  let activeToken = null;
  let studyCardGeneration = 0;
  let translationRequest = 0;
  let grammarRequest = 0;
  let lastTranslationSelection = null;
  const optimisticStudyPairs = new Set();
  // Native Jiten states are mutable and account-scoped. Parsed vocabulary is not.
  let studyStateEpoch = 0;
  let studyStateScope = '';
  // S3: jpdb states come from lookup-vocabulary and have their own account scope.
  let jpdbStateScope = '';
  const pendingJitenMutations = new Set();

  // Which provider can refresh this token's state: Jiten, or jpdb for native
  // vid/sid tokens only (a Jiten-ID fallback under jpdb is never sent to jpdb).
  function liveStateBackend(token = {}) {
    const backend = String(token?.backend || 'jiten').toLowerCase();
    if (backend === 'jiten') return 'jiten';
    const namespace = String(token?.idNamespace || token?.card?.idNamespace || '').toLowerCase();
    return backend === 'jpdb' && namespace === 'jpdb' ? 'jpdb' : '';
  }

  function mutationKey(token = {}) {
    const pair = jitenTokenPair(token);
    const backend = liveStateBackend(token);
    if (!pair || !backend) return '';
    return backend === 'jiten' ? pair : `${backend}|${pair}`;
  }

  function notifyStudyStatesChanged() {
    if (typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
      window.dispatchEvent(new CustomEvent('pudge-study-states-changed'));
    }
  }

  function jitenTokenPair(token = {}) {
    const id = Number(token.wordId ?? token.word_id ?? token.card?.wordId ?? token.card?.word_id ?? 0);
    const reading = Number(token.readingIndex ?? token.reading_index ?? token.card?.readingIndex ?? token.card?.reading_index ?? -1);
    return Number.isInteger(id) && id > 0 && Number.isInteger(reading) && reading >= 0
      ? `${id}:${reading}` : '';
  }

  function invalidateJitenStatePair(token) {
    const key = mutationKey(token);
    if (!key) return;
    pendingJitenMutations.add(key);
    studyStateEpoch += 1;
    for (const node of document.querySelectorAll('[data-pudge-study-token]')) {
      const item = registry.get(String(node.dataset.pudgeStudyToken || ''));
      if (mutationKey(item) !== key) continue;
      delete node.dataset.pudgeStudyVerified;
      delete node.dataset.pudgeStudyScope;
    }
    notifyStudyStatesChanged();
  }

  function applyJitenStatesToVisible(rows, backend = 'jiten') {
    const byPair = new Map((rows || []).filter(row => row?.ok).map(row => [
      `${Number(row.wordId)}:${Number(row.readingIndex)}`, row,
    ]));
    for (const node of document.querySelectorAll('[data-pudge-study-token]')) {
      const token = registry.get(String(node.dataset.pudgeStudyToken || ''));
      if (!token || liveStateBackend(token) !== backend) continue;
      const pair = jitenTokenPair(token);
      if (pendingJitenMutations.has(mutationKey(token))) continue;
      const row = byPair.get(pair);
      if (row) applyStudyStateToCard(token.card || (token.card = {}), node, row);
    }
    notifyStudyStatesChanged();
  }

  const JITEN_POST_MUTATION_RETRY_MS = [1200, 3000, 7000];
  const studyStateFingerprint = card => JSON.stringify([
    (card?.states || card?.knownState || card?.cardState || []).map(value => String(value || '').toLowerCase()).sort(),
    String(card?.normalizedState || ''),
    (Array.isArray(card?.studyDeckIds) ? card.studyDeckIds : []).map(Number).sort((a, b) => a - b),
  ]);

  // After a review/add, fetch the authoritative state right away and, while
  // Jiten still reports the pre-mutation state (its SRS update lags), retry a
  // few times so the word visibly changes colour without reopening anything.
  async function refreshJitenPairAfterMutation(token, attempt = 0, before = null) {
    const pair = jitenTokenPair(token);
    const key = mutationKey(token);
    const backend = liveStateBackend(token);
    if (!pair || !key) return;
    const prior = before ?? studyStateFingerprint(token.card || {});
    const [id, reading] = pair.split(':').map(Number);
    const retry = () => {
      if (attempt >= JITEN_POST_MUTATION_RETRY_MS.length) return;
      setTimeout(() => void refreshJitenPairAfterMutation(token, attempt + 1, prior), JITEN_POST_MUTATION_RETRY_MS[attempt]);
    };
    try {
      const rows = await lookupLiveStates([[id, reading]], {force:true, backend});
      if (!rows.length) { retry(); return; }
      if (attempt === 0 && !pendingJitenMutations.has(key)) return;
      // Any previously started hydration must not overwrite this confirmed POST.
      studyStateEpoch += 1;
      pendingJitenMutations.delete(key);
      applyJitenStatesToVisible(rows, backend);
      try { window.dispatchEvent?.(new CustomEvent('pudge-study-pairs-updated', {detail:{backend, rows}})); } catch (_) {}
      const row = rows.find(item => item?.ok && `${Number(item.wordId)}:${Number(item.readingIndex)}` === pair);
      if (!row || studyStateFingerprint(row) === prior) retry();
    } catch (_) {
      // Keep unresolved operations uncolored until an authoritative refresh.
      retry();
    }
  }


  const selectedDeckByBackend = new Map();
  const studyDeckCache = new Map();
  const STUDY_DECK_CACHE_TTL_MS = 5 * 60_000;

  function rememberedStudyDeck(backend) {
    const key = String(backend || 'jiten').toLowerCase();
    if (selectedDeckByBackend.has(key)) return selectedDeckByBackend.get(key) || '';
    try {
      const value = localStorage.getItem(`pudge.studyDeck.${key}`) || '';
      selectedDeckByBackend.set(key, value);
      return value;
    } catch (_) {
      return '';
    }
  }

  function rememberStudyDeck(backend, deckId) {
    const key = String(backend || 'jiten').toLowerCase();
    const value = String(deckId || '');
    selectedDeckByBackend.set(key, value);
    try { localStorage.setItem(`pudge.studyDeck.${key}`, value); } catch (_) {}
    // localStorage does not survive an app relaunch (private webview): persist in the app.
    try { void Promise.resolve(API()?.set_study_deck_preference?.(key, value)).catch(() => {}); } catch (_) {}
  }

  // Deck chosen in an earlier app session (app database), loaded once per backend.
  async function persistedStudyDeck(backend) {
    const key = String(backend || 'jiten').toLowerCase();
    const local = rememberedStudyDeck(key);
    if (local || selectedDeckByBackend.get(`${key}:persisted`) === '1') return local;
    selectedDeckByBackend.set(`${key}:persisted`, '1');
    try {
      const result = await API()?.study_deck_preference?.(key);
      const value = String(result?.deck_id || '');
      if (value && !selectedDeckByBackend.get(key)) selectedDeckByBackend.set(key, value);
      return selectedDeckByBackend.get(key) || '';
    } catch (_) {
      return local;
    }
  }

  function ensureUi() {
    let card = document.getElementById('pudgeStudyCard');
    if (!card) {
      card = document.createElement('aside');
      card.id = 'pudgeStudyCard';
      card.className = 'pudge-study-card';
      document.body.appendChild(card);
    }
    let translation = document.getElementById('pudgeTranslationPop');
    if (!translation) {
      translation = document.createElement('aside');
      translation.id = 'pudgeTranslationPop';
      translation.className = 'pudge-translation-pop';
      document.body.appendChild(translation);
    }
    return {card, translation};
  }

  function normalizeState(card = {}) {
    const explicit = String(card.normalizedState || '').toLowerCase();
    if (explicit) return explicit;
    const states = (card.cardState || card.states || []).map(x => String(x).toLowerCase());
    if (states.includes('due') || states.includes('failed')) return 'due';
    if (states.some(x => ['known','mastered','never-forget'].includes(x))) return 'known';
    if (states.some(x => ['learning','young','mature'].includes(x))) return 'learning';
    if (states.includes('blacklisted')) return 'blacklisted';
    return 'new';
  }

  function applyStudyAppearance(settings = {}, target = document.documentElement) {
    globalThis.PudgeReviewActions?.update?.(settings);
    if (!target?.style) return;
    const theme = String(settings.word_color_theme || 'balanced').toLowerCase();
    target.dataset.pudgeStudyTheme = STUDY_THEMES.has(theme) ? theme : 'balanced';
    for (const [state, fallback] of Object.entries(STUDY_COLORS)) {
      const value = String(settings[`word_color_${state}`] || fallback);
      target.style.setProperty(`--pudge-study-${state}`, value);
    }
    target.style.setProperty(
      '--pudge-pitch-color',
      String(settings.pitch_accent_color || '#9ec5ff')
    );
  }

  function stateLabel(card = {}) {
    const liveLabel = String(card.rawStateLabel || '').trim();
    if (liveLabel) return liveLabel;
    const raw = (card.knownState || card.cardState || card.states || [])
      .map(value => String(value || '').trim().toLowerCase());
    for (const [state, label] of [
      ['blacklisted','Blacklisted'], ['mastered','Mastered'], ['mature','Mature'],
      ['young','Young'], ['due','Due'], ['suspended','Suspended'],
      ['redundant','Redundant'], ['new','New'],
    ]) {
      if (raw.includes(state)) return label;
    }
    const state = normalizeState(card);
    const labels = ru()
      ? {new:'Новое',learning:'Изучается',due:'К повторению',known:'Известно',blacklisted:'Игнорируется',unknown:'Неизвестно'}
      : {new:'New',learning:'Learning',due:'Due',known:'Known',blacklisted:'Blacklisted',unknown:'Unknown'};
    return labels[state] || labels.new;
  }

  function rubyReading(value) {
    const text = String(value || '');
    let out = '', last = 0;
    const pattern = /([\u3400-\u9fff々〆ヵヶ]+)\[([^\]\r\n]+)\]/g;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      out += esc(text.slice(last, match.index));
      out += `<ruby class="pudge-study-term-ruby"><span>${esc(match[1])}</span><rt>${esc(match[2])}</rt></ruby>`;
      last = pattern.lastIndex;
    }
    out += esc(text.slice(last));
    return out || esc(text);
  }

  function annotatedStudySpelling(value) {
    return String(value || '').replace(/([\u3400-\u9fff々〆ヵヶ]+)\[[^\]\r\n]+\]/g, '$1').replace(/\s+/g, '');
  }

  function kanjiOnlyStudyTerm(spelling, reading) {
    const text = String(spelling || '').trim();
    const kanaReading = plainStudyReading(reading || '');
    if (!text || !kanaReading || !/[\u3400-\u9fff々〆ヵヶ]/u.test(text)) return esc(text);
    const segments = text.match(/[\u3400-\u9fff々〆ヵヶ]+|[ぁ-ゟ゠-ヿー]+|[^\u3400-\u9fff々〆ヵヶぁ-ゟ゠-ヿー]+/gu) || [];
    const normalizedReading = normalizedKana(kanaReading);
    let readingPos = 0;
    let out = '';
    for (let index = 0; index < segments.length; index += 1) {
      const segment = segments[index];
      if (/^[\u3400-\u9fff々〆ヵヶ]+$/u.test(segment)) {
        const nextKana = segments.slice(index + 1).find(part => /^[ぁ-ゟ゠-ヿー]+$/u.test(part));
        const end = nextKana ? normalizedReading.indexOf(normalizedKana(nextKana), readingPos + 1) : kanaReading.length;
        if (end < readingPos) return esc(text);
        const ruby = kanaReading.slice(readingPos, end);
        if (!ruby) return esc(text);
        out += `<ruby class="pudge-study-term-ruby"><span>${esc(segment)}</span><rt>${esc(ruby)}</rt></ruby>`;
        readingPos = end;
        continue;
      }
      out += esc(segment);
      if (/^[ぁ-ゟ゠-ヿー]+$/u.test(segment)) {
        const kana = normalizedKana(segment);
        if (!normalizedReading.startsWith(kana, readingPos)) return esc(text);
        readingPos += segment.length;
      }
    }
    return readingPos === kanaReading.length ? out : esc(text);
  }

  function plainStudyReading(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    const replaced = text.replace(/[\u3400-\u9fff々〆ヵヶ]+\[([^\]\r\n]+)\]/g, (_match, reading) => reading);
    return replaced.replace(/\s+/g, '');
  }

  function studyTerm(card = {}, token = {}) {
    const spelling = String(card.spelling || token.surface || '').trim();
    const rawReading = String(card.rawReading || card.reading || token.reading || '');
    const reading = plainStudyReading(rawReading);
    if (!spelling) return '';
    if (!reading || normalizedKana(spelling) === normalizedKana(reading) || !/[\u3400-\u9fff々〆ヵヶ]/u.test(spelling)) {
      return `<span class="pudge-study-term-text">${esc(spelling)}</span>`;
    }
    if (/([\u3400-\u9fff々〆ヵヶ]+)\[[^\]\r\n]+\]/u.test(rawReading) && annotatedStudySpelling(rawReading) === spelling) {
      return `<span class="pudge-study-term-mixed">${rubyReading(rawReading)}</span>`;
    }
    return `<span class="pudge-study-term-mixed">${kanjiOnlyStudyTerm(spelling, reading)}</span>`;
  }

  function sentenceSpans(text) {
    const raw = String(text || '');
    const spans = [];
    let start = 0;
    for (let index = 0; index < raw.length; index += 1) {
      const ch = raw[index];
      if (!'。！？!?\n'.includes(ch)) continue;
      const end = index + 1;
      if (raw.slice(start, end).trim()) spans.push({start, end});
      start = end;
    }
    if (raw.slice(start).trim()) spans.push({start, end: raw.length});
    return spans;
  }

  function studyContextFromEntries(text, token = {}, tokens = []) {
    const raw = String(text || '');
    if (!raw.trim()) return '';
    const explicitlyMined = String(token.minedSentence || '').replace(/\s+/g, ' ').trim();
    if (explicitlyMined) return explicitlyMined.slice(0, 1900);
    const spans = sentenceSpans(raw);
    if (!spans.length) return raw.replace(/\s+/g, ' ').trim().slice(0, 1900);
    const surface = String(token.surface || cardSpelling(token.card) || '').trim();
    let point = Number(token.contextStart ?? token.start);
    if (!Number.isFinite(point) || point < 0 || point >= raw.length) {
      const found = surface ? raw.indexOf(surface) : -1;
      point = found >= 0 ? found : 0;
    }
    let sentenceIndex = spans.findIndex(span => point >= span.start && point < span.end);
    if (sentenceIndex < 0) sentenceIndex = 0;
    let first = sentenceIndex, last = sentenceIndex;
    const current = raw.slice(spans[sentenceIndex].start, spans[sentenceIndex].end).replace(/\s+/g, '').length;
    if (current < 28) {
      if (sentenceIndex > 0) first = sentenceIndex - 1;
      if (sentenceIndex + 1 < spans.length) last = sentenceIndex + 1;
    }
    let desiredStart = spans[first].start;
    let desiredEnd = spans[last].end;

    const rows = (Array.isArray(tokens) ? tokens : [])
      .filter(candidate => candidate && String(candidate.sentence || '') === raw)
      .map(candidate => ({candidate, ...tokenContextBounds(candidate)}))
      .filter(row => Number.isFinite(row.start) && Number.isFinite(row.end))
      .sort((left, right) => left.start - right.start || left.end - right.end);
    let tokenIndex = rows.findIndex(row => row.candidate === token);
    if (tokenIndex < 0) {
      const wordId = Number(token.wordId ?? token.word_id ?? token.card?.wordId ?? 0);
      const readingIndex = Number(token.readingIndex ?? token.reading_index ?? token.card?.readingIndex ?? -1);
      const start = Number(token.contextStart ?? token.start);
      tokenIndex = rows.findIndex(row =>
        row.start === start &&
        Number(row.candidate.wordId ?? row.candidate.word_id ?? row.candidate.card?.wordId ?? 0) === wordId &&
        Number(row.candidate.readingIndex ?? row.candidate.reading_index ?? row.candidate.card?.readingIndex ?? -1) === readingIndex
      );
    }
    if (tokenIndex >= 0) {
      // Guarantee at least five parsed words of context on each side whenever
      // they exist in the local text, even if that crosses sentence boundaries.
      const before = rows[Math.max(0, tokenIndex - 5)];
      const after = rows[Math.min(rows.length - 1, tokenIndex + 5)];
      if (before) desiredStart = Math.min(desiredStart, before.start);
      if (after) desiredEnd = Math.max(desiredEnd, after.end);
    }
    first = spans.findIndex(span => desiredStart >= span.start && desiredStart < span.end);
    if (first < 0) first = sentenceIndex;
    last = spans.findIndex(span => Math.max(desiredStart, desiredEnd - 1) >= span.start && Math.max(desiredStart, desiredEnd - 1) < span.end);
    if (last < 0) last = sentenceIndex;
    if (last < first) [first, last] = [last, first];
    return raw.slice(spans[first].start, spans[last].end).replace(/\s+/g, ' ').trim().slice(0, 1900);
  }

  function studyContext(text, token = {}) {
    const raw = String(text || '');
    const candidates = [...registry.values()].filter(candidate => String(candidate?.sentence || '') === raw);
    return studyContextFromEntries(raw, token, candidates);
  }

  function cardSpelling(card = {}) {
    return String(card.spelling || '');
  }

  function pitchMorae(reading) {
    const small = new Set(['ゃ','ゅ','ょ','ャ','ュ','ョ','ァ','ィ','ゥ','ェ','ォ']);
    const cleaned = String(reading || '').replace(/[^\u3040-\u30ffー]/g, '');
    const morae = [];
    for (const character of cleaned) {
      if (morae.length && small.has(character)) morae[morae.length - 1] += character;
      else morae.push(character);
    }
    return morae;
  }

  function pitchClass(accent, moraCount) {
    if (accent === 0) return 'heiban';
    if (accent === 1 && moraCount > 1) return 'atamadaka';
    if (accent === moraCount) return 'odaka';
    return 'nakadaka';
  }

  function pitchPattern(accent, moraCount) {
    const values = [];
    for (let index = 0; index <= moraCount; index += 1) {
      if (accent === 0) values.push(index > 0);
      else if (index === 0) values.push(accent === 1);
      else if (index < moraCount) values.push(index < accent);
      else values.push(false);
    }
    return values;
  }

  function tokenReading(surface, token = {}, card = {}) {
    const text = String(surface || '');
    const tokenStart = Number(token.start || 0);
    const ranges = [];
    for (const ruby of Array.isArray(token.rubies) ? token.rubies : []) {
      const rawStart = Number(ruby?.start);
      const rawEnd = Number(ruby?.end ?? (rawStart + Number(ruby?.length || 0)));
      const reading = String(ruby?.text || '').replace(/[^\u3040-\u30ffー]/g, '');
      const start = rawStart - tokenStart;
      const end = rawEnd - tokenStart;
      if (!Number.isFinite(start) || !Number.isFinite(end) || !reading ||
          start < 0 || end <= start || start >= text.length) continue;
      ranges.push({start, end: Math.min(text.length, end), reading});
    }
    ranges.sort((a, b) => a.start - b.start || a.end - b.end);
    if (!ranges.length) return String(token.reading || card.reading || '');
    let position = 0, reading = '';
    for (const range of ranges) {
      if (range.start < position) continue;
      reading += text.slice(position, range.start);
      reading += range.reading;
      position = range.end;
    }
    reading += text.slice(position);
    return /[\u3400-\u9fff々〆ヵヶ]/.test(reading)
      ? String(token.reading || card.reading || '')
      : reading;
  }

  function inflectedPitchCard(surface, token = {}, card = {}) {
    const reading = tokenReading(surface, token, card);
    const dictionaryReading = String(card.reading || '');
    const moraCount = pitchMorae(reading).length;
    const direct = token.pitchAccents || token.pitch_accents;
    const source = Array.isArray(direct) && direct.length
      ? direct
      : (card.pitchAccents || card.pitch_accents || []);
    const pitchAccents = [...new Set(source.map(value => Number(value))
      .filter(value => Number.isInteger(value) && value >= 0)
      .map(value => value === 0 ? 0 : Math.min(value, moraCount)))]
      .filter(value => value <= moraCount);
    return {
      ...card,
      reading: reading || dictionaryReading,
      pitchAccents,
      pitchDerived: Boolean(
        reading && dictionaryReading &&
        pitchMorae(reading).join('') !== pitchMorae(dictionaryReading).join('') &&
        !(Array.isArray(direct) && direct.length)
      ),
    };
  }

  function renderPitchAccent(card = {}) {
    const morae = pitchMorae(card.reading || '');
    if (!morae.length) return '';
    const accents = [...new Set(
      (card.pitchAccents || card.pitch_accents || [])
        .map(value => Number(value))
        .filter(value => Number.isInteger(value) && value >= 0 && value <= morae.length)
    )];
    if (!accents.length) return '';
    const rows = accents.map(accent => {
      const pattern = pitchPattern(accent, morae.length);
      const nodes = [...morae, '・'].map((mora, index) => {
        const high = pattern[index];
        const next = pattern[index + 1];
        const transition = index < pattern.length - 1 && high !== next
          ? (high ? ' drop' : ' rise') : '';
        const particle = index === morae.length ? ' particle' : '';
        return `<span class="pudge-pitch-mora ${high ? 'high' : 'low'}${transition}${particle}">${esc(mora)}</span>`;
      }).join('');
      const type = pitchClass(accent, morae.length);
      return `<div class="pudge-pitch-row pitch-${type}" title="${esc(type)}"><span class="pudge-pitch-number">${accent}</span><span class="pudge-pitch-track">${nodes}</span></div>`;
    }).join('');
    return `<div class="pudge-study-pitch">${rows}</div>`;
  }

  function renderInlinePitch(card = {}) {
    const morae = pitchMorae(card.reading || '');
    const accent = (card.pitchAccents || card.pitch_accents || [])
      .map(value => Number(value))
      .find(value => Number.isInteger(value) && value >= 0 && value <= morae.length);
    if (!morae.length || accent === undefined) return '';
    const pattern = pitchPattern(accent, morae.length);
    const type = pitchClass(accent, morae.length);
    const nodes = morae.map((mora, index) => {
      const high = pattern[index];
      const next = pattern[index + 1];
      const transition = high !== next ? (high ? ' drop' : ' rise') : '';
      return `<span class="pudge-pitch-mora ${high ? 'high' : 'low'}${transition}">${esc(mora)}</span>`;
    }).join('');
    const detail = card.pitchDerived
      ? (ru() ? 'форма: акцент перенесён со словарной формы' : 'inflection: contour derived from dictionary form')
      : `${type} ${accent}`;
    return `<span class="pudge-inline-pitch pitch-${type}${card.pitchDerived ? ' pitch-derived' : ''}" title="${esc(detail)}">${nodes}</span>`;
  }

  function normalizedKana(value) {
    return String(value || '').normalize('NFKC').replace(/[ァ-ヶ]/g, character =>
      String.fromCharCode(character.charCodeAt(0) - 0x60)
    );
  }

  function renderInlinePitchOnSurface(surface, card = {}) {
    const text = String(surface || '');
    const reading = String(card.reading || '');
    if (!text || !reading || /[^ぁ-ゟ゠-ヿー]/u.test(text)) return '';
    if (normalizedKana(text) !== normalizedKana(reading)) return '';
    return renderInlinePitch({...card, reading:text});
  }

  // Width for the card header: headword column + its ↗/★ controls, optional
  // header actions (Play from here / Read up to here) and close, all measured
  // at their natural single-line width. When that cannot fit inside the card
  // cap, the header actions move to their own row and the headword wraps.
  const STUDY_CARD_MIN_WIDTH = 440;
  const STUDY_CARD_MAX_WIDTH = 620;
  function sizeStudyCard(el) {
    if (!el) return;
    el.style.removeProperty('--pudge-study-width');
    const head = el.querySelector('.pudge-study-head');
    const termWrap = el.querySelector('.pudge-study-term-wrap');
    if (!head || !termWrap) return;
    const actions = el.querySelector('.pudge-study-head-actions');
    const close = el.querySelector('.pudge-study-close');
    const layoutWhenFits = actions ? 'inline' : 'compact';
    if (window.matchMedia?.('(max-width:520px)').matches) {
      head.dataset.layout = actions ? 'stacked' : 'compact';
      return;
    }
    head.dataset.layout = layoutWhenFits;
    const number = value => Number.parseFloat(value || '0') || 0;
    const width = node => (node ? Math.max(Number(node.scrollWidth || 0), Number(node.getBoundingClientRect?.().width || 0)) : 0);
    el.classList.add('pudge-study-measuring');
    let termWidth = 0, actionsWidth = 0, closeWidth = 0;
    try {
      termWidth = width(termWrap);
      actionsWidth = width(actions);
      closeWidth = width(close);
    } finally {
      el.classList.remove('pudge-study-measuring');
    }
    const headStyle = getComputedStyle(head);
    const cardStyle = getComputedStyle(el);
    const gap = number(headStyle.columnGap || headStyle.gap);
    const chrome = number(cardStyle.paddingLeft) + number(cardStyle.paddingRight)
      + number(cardStyle.borderLeftWidth) + number(cardStyle.borderRightWidth);
    const cap = Math.max(0, Math.min(STUDY_CARD_MAX_WIDTH, Number(window.innerWidth || STUDY_CARD_MAX_WIDTH) - 24));
    const inline = Math.ceil(termWidth + (actions ? actionsWidth + gap : 0) + (close ? closeWidth + gap : 0) + chrome);
    let required = inline;
    if (actions && inline > cap) {
      head.dataset.layout = 'stacked';
      required = Math.ceil(Math.max(termWidth + (close ? closeWidth + gap : 0), actionsWidth) + chrome);
    }
    const next = Math.max(STUDY_CARD_MIN_WIDTH, Math.min(STUDY_CARD_MAX_WIDTH, required));
    el.style.setProperty('--pudge-study-width', `${next}px`);
  }

  function position(el, rect) {
    if (!el || !rect) return;
    // Force layout and place the card in the same JS task that makes it visible.
    // The old requestAnimationFrame path allowed one painted frame at the
    // element's static position near the top-left, which looked like a giant
    // empty outlined card before the real Jiten popup jumped into place.
    const r = el.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - r.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
    let top = rect.bottom + 10;
    if (top + r.height > window.innerHeight - 8) top = Math.max(8, rect.top - r.height - 10);
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  }

  async function apiDecks(backend) {
    const key = String(backend || 'jiten').toLowerCase();
    const now = Date.now();
    const cached = studyDeckCache.get(key);
    if (cached?.value && now - Number(cached.fetchedAt || 0) < STUDY_DECK_CACHE_TTL_MS) {
      return cached.value;
    }
    if (cached?.promise) return cached.promise;
    const promise = (async () => {
      const value = API()?.study_decks
        ? await API().study_decks(key)
        : await API().light_novel_decks(key);
      const rows = Array.isArray(value) ? value : [];
      studyDeckCache.set(key, {value:rows, fetchedAt:Date.now(), promise:null});
      return rows;
    })();
    studyDeckCache.set(key, {value:cached?.value || null, fetchedAt:Number(cached?.fetchedAt || 0), promise});
    try {
      return await promise;
    } catch (error) {
      const previous = studyDeckCache.get(key);
      if (previous?.promise === promise) {
        if (cached?.value) studyDeckCache.set(key, cached);
        else studyDeckCache.delete(key);
      }
      throw error;
    }
  }

  async function apiAction(payload) {
    if (API()?.study_action) return API().study_action(payload);
    return API().light_novel_study_action(payload);
  }

  async function apiStudyState(payload) {
    if (API()?.study_state) return API().study_state(payload);
    if (API()?.light_novel_study_state) return API().light_novel_study_state(payload);
    return null;
  }

  async function apiStudyStates(payload) {
    if (API()?.study_states) return API().study_states(payload);
    if (API()?.light_novel_study_states) return API().light_novel_study_states(payload);
    return null;
  }

  function applyStudyStateToCard(card, target, payload) {
    if (!card || !payload?.ok) return false;
    const states = Array.isArray(payload.states) ? payload.states.map(value => String(value || '').toLowerCase()) : [];
    card.states = states;
    card.knownState = states;
    card.normalizedState = String(payload.normalizedState || normalizeState({...card, normalizedState:''}));
    card.rawStateLabel = String(payload.rawStateLabel || '');
    card.knowledgeStatus = String(payload.knowledgeStatus || 'live_provider_batch');
    if (target?.dataset) {
      // A parse snapshot or a stale/error result can never assert "New".
      const scoped = String(payload.account_scope || '');
      const expectedScope = String(payload.provider || '').toLowerCase() === 'jpdb' ? jpdbStateScope : studyStateScope;
      const confirmed = scoped && scoped === expectedScope && !payload.stale &&
        ['new','learning','known','due','blacklisted'].includes(String(card.normalizedState || '').toLowerCase());
      if (confirmed) {
        target.dataset.pudgeStudyVerified = '1';
        target.dataset.pudgeStudyScope = scoped;
      } else {
        delete target.dataset.pudgeStudyVerified;
        delete target.dataset.pudgeStudyScope;
      }
    }
    if (Array.isArray(payload.studyDeckIds)) {
      card.studyDeckIds = payload.studyDeckIds.map(value => Number(value)).filter(Number.isInteger);
    }
    const state = normalizeState(card);
    if (target?.classList) {
      for (const name of [...target.classList]) if (name.startsWith('state-')) target.classList.remove(name);
      target.classList.add(`state-${state}`);
      if (nPlusOneCompanionKnown(card)) target.classList.remove('pudge-optimal-word');
    }
    return true;
  }

  function applyLiveStudyState(current, target, payload) {
    if (!current || !payload?.ok || activeToken !== current || activeToken?.generation !== current.generation) return false;
    const card = current.token.card || (current.token.card = {});
    applyStudyStateToCard(card, target, payload);
    const state = normalizeState(card);
    const badge = document.querySelector('#pudgeStudyCard .pudge-study-state');
    if (badge) {
      for (const name of [...badge.classList]) if (name.startsWith('state-')) badge.classList.remove(name);
      badge.classList.add(`state-${state}`);
      badge.textContent = stateLabel(card);
      const providerName = String(payload.provider || current.backend || '').toLowerCase() === 'jpdb' ? 'jpdb' : 'Jiten';
      badge.title = payload.stale ? (ru() ? `Последнее известное состояние ${providerName}` : `Last known ${providerName} state`) : '';
      refreshActiveOptimalBadge();
    }
    return true;
  }

  function activeStudyTokenPair(current) {
    const token = current?.token || {};
    const wordId = Number(token.wordId ?? token.word_id ?? token.card?.wordId ?? token.card?.word_id ?? 0);
    const readingIndex = Number(token.readingIndex ?? token.reading_index ?? token.card?.readingIndex ?? token.card?.reading_index ?? -1);
    return Number.isInteger(wordId) && wordId > 0 && Number.isInteger(readingIndex) && readingIndex >= 0
      ? {wordId, readingIndex, key:`${wordId}:${readingIndex}`} : null;
  }

  function activeStudyMutationKey(current) {
    return mutationKey(current?.token ? {...current.token, backend:current.backend, idNamespace:current.idNamespace} : {});
  }

  // One guarded path for every validated live row that may reach the open card
  // (per-card refresh and batch hydration alike). The open card can be an
  // unregistered token (LN uses its own [data-ln-token] map), so it is never
  // reached through the registry-only visible pass.
  function applyLiveRowToActiveCard(current, row, backend) {
    if (!current || !row?.ok || activeToken !== current || activeToken.generation !== current.generation) return false;
    if (liveStateBackend(current) !== backend) return false;
    const pair = activeStudyTokenPair(current);
    if (!pair || `${Number(row.wordId)}:${Number(row.readingIndex)}` !== pair.key) return false;
    if (pendingJitenMutations.has(activeStudyMutationKey(current))) return false;
    const applied = applyLiveStudyState(current, current.target, row);
    if (applied) updateStudyReviewAvailability(current);
    return applied;
  }

  function refreshLiveStudyState(current, target, {force = false} = {}) {
    if (!current) return Promise.resolve(null);
    // Re-use the request already in flight for this open card (grade-before-
    // response must await it instead of issuing a second lookup).
    if (current.liveRefresh && !force) return current.liveRefresh;
    current.liveRefreshState = 'pending';
    const request = runLiveStudyRefresh(current, target, {force}).then(row => {
      if (current.liveRefresh === request) current.liveRefreshState = row ? 'confirmed' : 'unavailable';
      updateStudyReviewAvailability(current);
      return row;
    });
    current.liveRefresh = request;
    return request;
  }

  async function runLiveStudyRefresh(current, target, {force = false} = {}) {
    const backend = liveStateBackend(current || {});
    if (!current || !backend) return null;
    const pair = activeStudyTokenPair(current);
    if (!pair) return null;
    const {wordId, readingIndex} = pair;
    if (target && current.target !== target) current.target = target;
    try {
      if (backend === 'jpdb') {
        // Same account-checked batch path as hydration (lookup-vocabulary).
        const rows = await lookupJpdbLiveStates([[wordId, readingIndex]]);
        const row = rows.find(item => item?.ok && `${Number(item.wordId)}:${Number(item.readingIndex)}` === pair.key) || null;
        if (row && !pendingJitenMutations.has(activeStudyMutationKey(current))) {
          applyJitenStatesToVisible([row], 'jpdb');
          applyLiveRowToActiveCard(current, row, 'jpdb');
          return row;
        }
        return null;
      }
      const api = API();
      if ((api?.study_states || api?.light_novel_study_states) && api?.study_provider_capabilities) {
        // Account capabilities, scope and epoch are validated by lookupLiveStates.
        const rows = await lookupLiveStates([[wordId, readingIndex]], {force, backend:'jiten'});
        const row = rows.find(item => item?.ok && `${Number(item.wordId)}:${Number(item.readingIndex)}` === pair.key) || null;
        if (!row || pendingJitenMutations.has(activeStudyMutationKey(current))) return null;
        applyJitenStatesToVisible([row], backend);
        applyLiveRowToActiveCard(current, row, backend);
        return row;
      }
      // Legacy single-pair endpoint (older WebAppApi builds without study_states).
      const epoch = studyStateEpoch;
      const payload = await apiStudyState({
        backend, word_id:wordId, reading_index:readingIndex, force:Boolean(force),
      });
      if (!payload?.ok || epoch !== studyStateEpoch) return null;
      const row = {...payload, wordId:Number(payload.wordId ?? wordId), readingIndex:Number(payload.readingIndex ?? readingIndex)};
      if (`${row.wordId}:${row.readingIndex}` !== pair.key) return null;
      if (pendingJitenMutations.has(activeStudyMutationKey(current))) return null;
      if (row.account_scope && studyStateScope && row.account_scope === studyStateScope) {
        applyJitenStatesToVisible([row], backend);
      }
      applyLiveRowToActiveCard(current, row, backend);
      return row;
    } catch (_) {
      // Keep the last provider/parse state. Network failure must not demote a
      // known card to New merely because a live refresh was unavailable.
      return null;
    }
  }

  const liveStateHydrationPairs = new Map();
  let liveStateHydrationTimer = null;
  let liveStateHydrationRunning = false;
  let latestOptimalWordLimit = 1000;

  function queueLiveStateHydration(payload, backend = 'jiten') {
    const selected = String(backend || payload?.settings?.study_backend || 'jiten').toLowerCase();
    if (selected !== 'jiten' && selected !== 'jpdb') return;
    for (const item of payload?.vocabulary || []) {
      // jpdb: only native vid/sid; cached parses carry a card_state snapshot.
      if (selected === 'jpdb' && String(item?.idNamespace || '').toLowerCase() !== 'jpdb') continue;
      const wordId = Number(item?.wordId ?? item?.word_id ?? 0);
      const readingIndex = Number(item?.readingIndex ?? item?.reading_index ?? -1);
      if (!Number.isInteger(wordId) || wordId <= 0 || !Number.isInteger(readingIndex) || readingIndex < 0) continue;
      const key = selected === 'jiten' ? `${wordId}:${readingIndex}` : `${selected}|${wordId}:${readingIndex}`;
      liveStateHydrationPairs.set(key, {backend:selected, pair:[wordId, readingIndex]});
    }
    if (!liveStateHydrationPairs.size || liveStateHydrationTimer || liveStateHydrationRunning) return;
    liveStateHydrationTimer = setTimeout(() => {
      liveStateHydrationTimer = null;
      void flushLiveStateHydration();
    }, 60);
  }

  async function lookupLiveStates(pairs, {force = false, backend = 'jiten'} = {}) {
    if (String(backend).toLowerCase() === 'jpdb') return lookupJpdbLiveStates(pairs);
    const words = [];
    const seen = new Set();
    for (const row of Array.isArray(pairs) ? pairs : []) {
      const wordId = Number(Array.isArray(row) ? row[0] : row?.wordId ?? row?.word_id ?? 0);
      const readingIndex = Number(Array.isArray(row) ? row[1] : row?.readingIndex ?? row?.reading_index ?? -1);
      const key = `${wordId}:${readingIndex}`;
      if (!Number.isInteger(wordId) || wordId <= 0 || !Number.isInteger(readingIndex) || readingIndex < 0 || seen.has(key)) continue;
      seen.add(key);
      words.push([wordId, readingIndex]);
      if (words.length >= 500) break;
    }
    if (!words.length) return [];
    const epoch = studyStateEpoch;
    const result = await apiStudyStates({backend:'jiten', words, force:Boolean(force)});
    // The server snapshots the account identity before its network call. Compare
    // against the current account after the call, so slow old-account responses
    // cannot color the new account's book.
    let accountScope = String(result?.account_scope || '');
    const caps = API()?.study_provider_capabilities
      ? await API().study_provider_capabilities('jiten') : null;
    // Compatibility with older WebAppApi builds: the capability endpoint is
    // authoritative for account identity, so a missing top-level scope must not
    // discard an otherwise valid state batch. G16 also adds account_scope to the
    // backend response so mixed-version windows converge after restart.
    if (!accountScope) accountScope = String(caps?.account_key || '');
    if (epoch !== studyStateEpoch || !result?.ok || !accountScope ||
        !caps || accountScope !== String(caps.account_key || '')) return [];
    if (studyStateScope && studyStateScope !== accountScope) {
      for (const node of document.querySelectorAll('[data-pudge-study-verified="1"]')) {
        delete node.dataset.pudgeStudyVerified;
        delete node.dataset.pudgeStudyScope;
      }
      notifyStudyStatesChanged();
    }
    studyStateScope = accountScope;
    const limit = Number(result?.optimalWordLimit ?? result?.optimal_word_limit);
    if (Number.isFinite(limit) && limit > 0) latestOptimalWordLimit = Math.max(1000, Math.floor(limit));
    return Array.isArray(result?.states) ? result.states.map(row => ({...row, account_scope:accountScope})) : [];
  }

  async function lookupJpdbLiveStates(pairs) {
    const words = [];
    const seen = new Set();
    for (const row of Array.isArray(pairs) ? pairs : []) {
      const vid = Number(Array.isArray(row) ? row[0] : row?.wordId ?? 0);
      const sid = Number(Array.isArray(row) ? row[1] : row?.readingIndex ?? -1);
      const key = `${vid}:${sid}`;
      if (!Number.isInteger(vid) || vid <= 0 || !Number.isInteger(sid) || sid < 0 || seen.has(key)) continue;
      seen.add(key);
      words.push([vid, sid]);
      if (words.length >= 500) break;
    }
    if (!words.length) return [];
    const epoch = studyStateEpoch;
    const result = await apiStudyStates({backend:'jpdb', words});
    const accountScope = String(result?.account_scope || '');
    const caps = API()?.study_provider_capabilities ? await API().study_provider_capabilities('jpdb') : null;
    // A response for a previous jpdb token (account switch during the call) is dropped.
    if (epoch !== studyStateEpoch || !result?.ok || !accountScope || !caps ||
        accountScope !== String(caps.account_key || '')) return [];
    if (jpdbStateScope && jpdbStateScope !== accountScope) {
      for (const node of document.querySelectorAll('[data-pudge-study-verified="1"]')) {
        delete node.dataset.pudgeStudyVerified;
        delete node.dataset.pudgeStudyScope;
      }
      notifyStudyStatesChanged();
    }
    jpdbStateScope = accountScope;
    return Array.isArray(result?.states)
      ? result.states.map(row => ({...row, provider:'jpdb', account_scope:accountScope}))
      : [];
  }

  function studyCardStateSet(card = {}) {
    const values = card.states || card.knownState || card.known_state || card.cardState || [];
    const rows = Array.isArray(values) ? values : [values];
    const set = new Set(rows.map(value => String(value || '').toLowerCase().replace(/_/g, '-')).filter(Boolean));
    const normalized = String(card.normalizedState || card.normalized_state || '').toLowerCase();
    if (normalized) set.add(normalized);
    return set;
  }

  function nPlusOneCompanionKnown(card = {}) {
    const states = studyCardStateSet(card);
    // Jiten's Young tier is already a learned/reviewing word, not a fresh +1.
    // Treat it as usable context just like Mature.  The previous Mature-only
    // rule made otherwise-valid N+1 sentences disappear for users with a large
    // Young queue.
    return ['young','mature','learning','known','mastered','never-forget','blacklisted','redundant']
      .some(state => states.has(state));
  }

  function nPlusOneTargetEligible(card = {}) {
    const states = studyCardStateSet(card);
    // A word that is already fully known is not a useful N+1 target even if it
    // happens to live outside a study deck.
    if (nPlusOneCompanionKnown(card)) return false;
    const decks = Array.isArray(card.studyDeckIds) ? card.studyDeckIds : [];
    return decks.length === 0 || states.has('new');
  }

  function tokenContextBounds(token = {}) {
    const start = Number(token.contextStart ?? token.start);
    const end = Number(token.contextEnd ?? token.end ?? (Number.isFinite(start) ? start + String(token.surface || '').length : NaN));
    return {start, end};
  }

  function studyPairKey(token = {}) {
    const card = token.card || {};
    const wordId = Number(token.wordId ?? token.word_id ?? card.wordId ?? card.word_id ?? 0);
    const readingIndex = Number(token.readingIndex ?? token.reading_index ?? card.readingIndex ?? card.reading_index ?? -1);
    return wordId > 0 && readingIndex >= 0 ? `${wordId}:${readingIndex}` : '';
  }

  function suppressOptimalForPair(token = {}, target = null) {
    const key = studyPairKey(token);
    if (!key) return false;
    optimisticStudyPairs.add(key);
    target?.classList?.remove('pudge-optimal-word');
    for (const node of document.querySelectorAll('[data-pudge-study-token]')) {
      const row = registry.get(String(node.dataset.pudgeStudyToken || ''));
      if (row && studyPairKey(row) === key) node.classList?.remove('pudge-optimal-word');
    }
    return true;
  }

  function rollbackOptimisticStudyPair(token = {}) {
    const key = studyPairKey(token);
    if (!key) return;
    optimisticStudyPairs.delete(key);
    refreshOptimalHighlights();
  }

  // One read-only N+1 decision for both the text highlight and the study-card star.
  // The caller supplies token occurrences, not word IDs: eligibility is contextual.
  function evaluateOptimalTargets(entries, {
    frequencyLimit = latestOptimalWordLimit,
    minSentenceTokens = 10,
    fallbackMinSentenceTokens = 5,
  } = {}) {
    const rows = (Array.isArray(entries) ? entries : []).filter(row => row?.token);
    const limit = Math.max(1000, Number(frequencyLimit || latestOptimalWordLimit || 1000));
    const byContext = new Map();
    for (const row of rows) {
      if (row.token.highlightOptimalWords === false || optimisticStudyPairs.has(studyPairKey(row.token))) continue;
      const context = String(row.token.sentence || '');
      if (!context) continue;
      if (!byContext.has(context)) byContext.set(context, []);
      byContext.get(context).push(row);
    }

    const evaluateWithMinimum = minimum => {
      const eligible = new Set();
      const minTokens = Math.max(2, Number(minimum || 0));
      for (const [context, contextRows] of byContext) {
        for (const span of sentenceSpans(context)) {
          const seen = new Set();
          const sentenceRows = contextRows.filter(row => {
            const {start, end} = tokenContextBounds(row.token);
            const point = Number.isFinite(start) ? start : end;
            if (!Number.isFinite(point) || point < span.start || point >= span.end) return false;
            const occurrence = `${start}:${end}:${studyPairKey(row.token)}:${row.token.surface || ''}`;
            if (seen.has(occurrence)) return false;
            seen.add(occurrence);
            return true;
          }).sort((a, b) => tokenContextBounds(a.token).start - tokenContextBounds(b.token).start);
          if (sentenceRows.length < minTokens) continue;
          for (let index = 0; index < sentenceRows.length; index += 1) {
            const row = sentenceRows[index];
            const card = row.token.card || {};
            const rank = Number(card.frequencyRank ?? card.frequency_rank ?? 0);
            if (!Number.isFinite(rank) || rank <= 0 || rank > limit || !nPlusOneTargetEligible(card)) continue;
            if (sentenceRows.some((other, otherIndex) =>
              otherIndex !== index && !nPlusOneCompanionKnown(other.token.card || {}))) continue;
            eligible.add(row.token);
          }
        }
      }
      return eligible;
    };

    // Preserve the old >=10-token preference when it finds useful material.
    // A whole chapter with zero results gets one bounded relaxation to >=5
    // tokens; N+1 strictness (one target, known companions, frequency gate)
    // is unchanged.
    let eligible = evaluateWithMinimum(minSentenceTokens);
    const fallback = Math.max(2, Number(fallbackMinSentenceTokens || 0));
    if (!eligible.size && rows.length >= 40 && fallback < Number(minSentenceTokens || 10)) {
      eligible = evaluateWithMinimum(fallback);
    }
    return eligible;
  }

  function refreshActiveOptimalBadge() {
    const current = activeToken;
    if (!current) return;
    const badge = document.querySelector?.('#pudgeStudyCard .pudge-study-optimal');
    if (!badge) return;
    // LN tokens live in the native LN registry rather than this module's DOM
    // registry. applyOptimalHighlights records eligibility on the actual target
    // element so the shared card can still show the same decision. Manga keeps
    // highlightOptimalWords=false and therefore cannot acquire this marker.
    const allowed = current.backend.toLowerCase() === 'jiten' &&
      current.token.highlightOptimalWords !== false && !current.token.fallback &&
      !current.pendingReview && !current.reviewOutcomeUnknown;
    const targetEligible = current.target?.dataset?.pudgeOptimalEligible === '1';
    const registryEligible = evaluateOptimalTargets(registeredStudyEntries()).has(current.token);
    badge.hidden = !allowed || !(targetEligible || registryEligible);
  }

  function applyOptimalHighlights(entries, {enabled = true, frequencyLimit = latestOptimalWordLimit} = {}) {
    const rows = (Array.isArray(entries) ? entries : []).filter(row => row?.token && row?.node);
    for (const row of rows) {
      row.node.classList?.remove('pudge-optimal-word');
      if (row.node.dataset) delete row.node.dataset.pudgeOptimalEligible;
    }
    const eligible = evaluateOptimalTargets(rows, {frequencyLimit});
    for (const row of rows) {
      if (!eligible.has(row.token)) continue;
      if (row.node.dataset) row.node.dataset.pudgeOptimalEligible = '1';
      if (enabled) row.node.classList?.add('pudge-optimal-word');
    }
    // Disabling the background never disables the independently computed star.
    refreshActiveOptimalBadge();
    return enabled ? eligible.size : 0;
  }

  function registeredStudyEntries() {
    const rows = [];
    for (const node of document.querySelectorAll('[data-pudge-study-token]')) {
      const token = registry.get(String(node.dataset.pudgeStudyToken || ''));
      if (token) rows.push({token, node});
    }
    return rows;
  }

  function refreshOptimalHighlights() {
    return applyOptimalHighlights(registeredStudyEntries(), {enabled:true, frequencyLimit:latestOptimalWordLimit});
  }

  async function flushLiveStateHydration() {
    if (liveStateHydrationRunning || !liveStateHydrationPairs.size) return;
    liveStateHydrationRunning = true;
    // One provider per batch; the other provider's pairs stay queued.
    const batchBackend = [...liveStateHydrationPairs.values()][0]?.backend || 'jiten';
    const rows = [...liveStateHydrationPairs.entries()].filter(([, value]) => value.backend === batchBackend).slice(0, 500);
    for (const [key] of rows) liveStateHydrationPairs.delete(key);
    try {
      const epoch = studyStateEpoch;
      const states = await lookupLiveStates(rows.map(([, value]) => value.pair), {backend:batchBackend});
      if (epoch !== studyStateEpoch) return;
      const byPair = new Map(states.map(row => [`${Number(row?.wordId || 0)}:${Number(row?.readingIndex ?? -1)}`, row]));
      for (const node of document.querySelectorAll('[data-pudge-study-token]')) {
        const token = registry.get(String(node.dataset.pudgeStudyToken || ''));
        if (!token || liveStateBackend(token) !== batchBackend) continue;
        const wordId = Number(token.wordId ?? token.word_id ?? token.card?.wordId ?? token.card?.word_id ?? 0);
        const readingIndex = Number(token.readingIndex ?? token.reading_index ?? token.card?.readingIndex ?? token.card?.reading_index ?? -1);
        const row = byPair.get(`${wordId}:${readingIndex}`);
        if (!row || pendingJitenMutations.has(mutationKey(token))) continue;
        const card = token.card || (token.card = {});
        applyStudyStateToCard(card, node, row);
      }
      if (activeToken && liveStateBackend(activeToken) === batchBackend) {
        const pair = activeStudyTokenPair(activeToken);
        const row = pair ? byPair.get(pair.key) : null;
        if (row) applyLiveRowToActiveCard(activeToken, row, batchBackend);
      }
      refreshOptimalHighlights();
      notifyStudyStatesChanged();
    } catch (_) {
      // Initial coloring is opportunistic. Per-card R15 refresh remains authoritative.
    } finally {
      liveStateHydrationRunning = false;
      if (liveStateHydrationPairs.size && !liveStateHydrationTimer) {
        liveStateHydrationTimer = setTimeout(() => {
          liveStateHydrationTimer = null;
          void flushLiveStateHydration();
        }, 60);
      }
    }
  }

  async function apiTranslate(text, context, targetLanguage, mediaId = null) {
    if (API()?.translate_text) return API().translate_text(text, context, targetLanguage, mediaId);
    return API().light_novel_translate(text, context, targetLanguage, mediaId);
  }

  function studyTokenIdentity(token, backend = 'jiten') {
    if (!token) return '';
    const card = token.card || {};
    return [String(backend || 'jiten'), String(token.wordId ?? token.word_id ?? card.wordId ?? card.word_id ?? ''), String(token.readingIndex ?? token.reading_index ?? card.readingIndex ?? card.reading_index ?? ''), String(token.surface || token.text || card.spelling || card.word || ''), plainStudyReading(card.reading || token.reading || '')].join('\u0001');
  }

  async function openStudyCard({token, target, backend = 'jiten', sentence = '', actions = []}) {
    if (!token || !target) return;
    const identity = studyTokenIdentity(token, backend);
    const {card: pop} = ensureUi();
    if (pop.classList.contains('open') && activeToken?.identity === identity) {
      closeStudyCard();
      return;
    }
    // Manga replaces the OCR region DOM when it becomes active. Keep the
    // original on-screen anchor so the async deck request cannot move the card
    // to (0, 0) after the clicked word has been detached.
    const anchorRect = target.getBoundingClientRect();
    const card = window.PudgeJitenWords?.normalize({...token.card, wordId:token.wordId ?? token.card?.wordId, readingIndex:token.readingIndex ?? token.card?.readingIndex}) || {...(token.card || {})};
    card.rawReading = String(card.reading || token.reading || '');
    card.reading = plainStudyReading(card.rawReading);
    const actionRows = (Array.isArray(actions) ? actions : [])
      .filter(action => action && action.id && typeof action.run === 'function');
    const extraActions = new Map(actionRows.map(action => [String(action.id), action]));
    const headerActions = actionRows.filter(action => action.placement === 'header' || String(action.id) === 'bookmark-here');
    const footerActions = actionRows.filter(action => !headerActions.includes(action));
    const generation = ++studyCardGeneration;
    activeToken = {
      token,
      backend: String(backend || 'jiten'),
      sentence: String(token.minedSentence || '') || studyContext(String(sentence || token.sentence || ''), token),
      extraActions,
      identity,
      generation,
      pendingReview: false,
      reviewOutcomeUnknown: false,
      idNamespace: String(token.idNamespace || card.idNamespace || (String(backend || 'jiten') === 'jiten' ? 'jiten' : '')),
      reviewable: token.reviewable !== false && card.reviewable !== false,
      reviewActions: studyReviewActions(backend),
      target,
      anchorRect,
    };
    activeToken.renderCard = card;
    const meaningsRaw = card.meanings || card.meaningsChunks || [];
    const meanings = Array.isArray(meaningsRaw) ? meaningsRaw.flat?.() || meaningsRaw : [];
    const state = normalizeState(card);
    const status = stateLabel(card);
    const fallbackOnly = Boolean(token.fallback);
    const wordId = Number(token.wordId ?? token.word_id ?? card.wordId ?? card.word_id ?? 0);
    const readingIndex = Number(token.readingIndex ?? token.reading_index ?? card.readingIndex ?? card.reading_index ?? -1);
    const jitenLink = String(activeToken.backend || '').toLowerCase() === 'jiten' && wordId > 0 && readingIndex >= 0
      ? `<button class="pudge-study-jiten-link" data-pudge-study-jiten data-word-id="${wordId}" data-reading-index="${readingIndex}" title="Open in Jiten" aria-label="Open in Jiten">↗</button>`
      : '';
    pop.innerHTML = `
      <div class="pudge-study-head" data-layout="${headerActions.length ? 'inline' : 'compact'}">
        <div class="pudge-study-term-wrap"><div class="pudge-study-term">${studyTerm(card, token)}</div><div class="pudge-study-term-controls">${jitenLink}<span class="pudge-study-optimal" hidden tabindex="0" role="img" aria-label="${ru() ? 'Подходит для изучения' : 'A good new word to learn in this context'}" title="${ru() ? 'Хорошее новое слово для изучения в этом контексте' : 'A good new word to learn in this context'}" data-tooltip="${ru() ? 'Хорошее новое слово для изучения в этом контексте' : 'A good new word to learn in this context'}">★</span></div></div>
        ${headerActions.length ? `<div class="pudge-study-head-actions">
          ${headerActions.map(action =>
            `<button class="pudge-study-header-action" data-pudge-study-action-tone="${String(action.tone || '') === 'listen' || String(action.id) === 'paired-audio-here' ? 'listen' : ''}" data-pudge-study-extra-action="${esc(action.id)}">${esc(action.label || action.id)}</button>`
          ).join('')}
        </div>` : ''}
        <button class="pudge-study-close" data-pudge-study-close aria-label="Close">×</button>
      </div>
      ${fallbackOnly ? '' : `
      <div class="pudge-study-subtle"><span class="pudge-study-state state-${esc(state)}">${esc(status)}</span>${card.frequencyRank ? ` <span aria-hidden="true">•</span> Frequency #${Number(card.frequencyRank)}` : ''}</div>
      <div class="pudge-study-alt-readings">${esc(window.PudgeJitenWords?.readingsLine(card) || "")}</div>
      ${renderPitchAccent(card)}
      <ol class="pudge-study-meanings">
        ${meanings.length
          ? meanings.slice(0, 8).map(x => `<li>${esc(typeof x === 'string' ? x : JSON.stringify(x))}</li>`).join('')
          : `<li class="pudge-study-subtle">${ru() ? 'Нет значений' : 'No meanings returned'}</li>`}
      </ol>
      <div class="pudge-study-controls">
        <select id="pudgeStudyDeck"><option value="">${ru() ? 'Колода…' : 'Study deck…'}</option></select>
        <div class="pudge-study-action-row" data-count="${activeToken.reviewActions.actions.length}">
          <div class="pudge-study-review-actions" role="group" aria-label="Review" data-count="${activeToken.reviewActions.actions.length}">
            ${studyGradeButtonsHtml(activeToken)}
          </div>
          ${activeToken.reviewable ? '' : `<div class="pudge-study-subtle">${ru() ? 'Нет безопасного соответствия ID для выбранного SRS' : 'No safe native-ID mapping for the selected SRS'}</div>`}
          <div class="pudge-study-add-wrap"><button class="pudge-study-add" data-pudge-study-add aria-disabled="false">${ru() ? 'Добавить' : 'Add'}</button></div>
        </div>
        ${footerActions.length ? `<div class="pudge-study-secondary-actions">${footerActions.map(action =>
          `<button class="pudge-study-extra-action" data-pudge-study-extra-action="${esc(action.id)}">${esc(action.label || action.id)}</button>`
        ).join('')}</div>` : ''}
      </div>`}`;
    // Keep the first visible layout off-screen until size + position are known.
    // This prevents the fixed card from flashing at its static top-left position.
    pop.style.left = '-10000px';
    pop.style.top = '-10000px';
    pop.classList.add('open');
    updateStudyReviewAvailability(activeToken);
    refreshActiveOptimalBadge();
    sizeStudyCard(pop);
    position(pop, anchorRect);
    if (fallbackOnly) return;
    if (backend === "jiten" && window.PudgeJitenWords) {
      const enrich = async () => {
        const detail = await window.PudgeJitenWords.enrich(card);
        if (activeToken?.generation !== generation) return;
        const line = pop.querySelector(".pudge-study-alt-readings");
        if (line) line.textContent = window.PudgeJitenWords.readingsLine(detail);
        if (!meanings.length) {
          const definitions = (detail.meanings || detail.meaningsChunks || []).flat().filter(Boolean);
          const list = pop.querySelector(".pudge-study-meanings");
          if (list && definitions.length) list.innerHTML = definitions.map(value => `<li>${esc(value)}</li>`).join("");
        }
      };
      void enrich().catch(() => {});
      setTimeout(() => { if (activeToken?.generation === generation) void enrich().catch(() => {}); }, 750);
    }
    // The lexical chapter cache intentionally outlives SRS state. Refresh only
    // this card from Jiten's lightweight known-state endpoint on open.
    void refreshLiveStudyState(activeToken, target);
    if (!activeToken.reviewable) return;
    try {
      const decks = await apiDecks(activeToken.backend);
      if (activeToken?.token !== token) return;
      const select = document.getElementById('pudgeStudyDeck');
      if (select) {
        select.dataset.pudgeStudyBackend = String(activeToken.backend || 'jiten');
        select.innerHTML = `<option value="">${ru() ? 'Колода…' : 'Study deck…'}</option>` +
          (decks || []).map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
        const remembered = await persistedStudyDeck(activeToken.backend);
        if (activeToken?.token !== token) return;
        if (remembered && [...select.options].some(option => option.value === remembered)) {
          select.value = remembered;
        }
      }
      updateStudyReviewAvailability(activeToken);
      position(pop, anchorRect);
    } catch (error) {
      const select = document.getElementById('pudgeStudyDeck');
      if (select) {
        select.innerHTML = `<option value="">${esc((window.PudgeUiLanguage?.message(error?.message || String(error)) ?? String(error?.message || String(error))))}</option>`;
        select.disabled = true;
      }
    }
  }

  function closeStudyCard() {
    studyCardGeneration += 1;
    activeToken = null;
    const card = document.getElementById('pudgeStudyCard');
    const focusedInside = document.activeElement;
    if (card && focusedInside && card.contains(focusedInside)) focusedInside.blur?.();
    card?.classList.remove('open');
    if (card) {
      card.style.left = '-10000px';
      card.style.top = '-10000px';
    }
  }

  function studyReviewActions(backend) {
    const api = globalThis.PudgeReviewActions;
    return api?.actionSet ? api.actionSet(backend) : {provider: 'jiten', mode: 'native', actions: []};
  }

  // Jiten reviews are shared across study decks: a deck is required only to
  // add a word that is new and not in any deck yet. Everything else reviews
  // without a deck. Returns '' when grading is possible, otherwise why not.
  function studyReviewBlockReason(current, deck = document.getElementById('pudgeStudyDeck')?.value || '') {
    if (!current) return '';
    if (!current.reviewable) return ru() ? 'Нет безопасного соответствия ID для выбранного SRS' : 'No safe native-ID mapping for the selected SRS';
    if (current.pendingReview) return ru() ? 'Review уже отправляется' : 'Review is already being sent';
    if (current.reviewOutcomeUnknown) return ru() ? 'Результат прошлого review неизвестен' : 'The previous review outcome is unknown';
    if (String(current.backend || '').toLowerCase() !== 'jiten' || deck) return '';
    const card = {...(current.renderCard || {}), ...(current.token?.card || {})};
    const memberships = Array.isArray(current.token?.card?.studyDeckIds) ? current.token.card.studyDeckIds : null;
    if (memberships?.length === 0 && normalizeState(card) === 'new' && current.liveRefreshState !== 'pending') {
      return ru() ? 'Новое слово: выберите колоду, в которую его добавить' : 'New word: choose a deck to add it to';
    }
    return '';
  }

  function studyAddBlockReason(current, deck = document.getElementById('pudgeStudyDeck')?.value || '') {
    if (!current) return '';
    if (!current.reviewable) return ru() ? 'Нет безопасного соответствия ID для выбранного SRS' : 'No safe native-ID mapping for the selected SRS';
    return deck ? '' : (ru() ? 'Выберите колоду' : 'Choose a deck');
  }

  function setStudyButtonAvailability(button, reason, hint = '') {
    if (!button) return;
    button.classList?.toggle?.('is-unavailable', Boolean(reason));
    button.setAttribute?.('aria-disabled', reason ? 'true' : 'false');
    const title = reason || hint;
    if (title) button.title = title;
    else if (typeof button.removeAttribute === 'function') button.removeAttribute('title');
    else button.title = '';
  }

  function updateStudyReviewAvailability(current = activeToken) {
    if (!current || activeToken !== current) return;
    const pop = document.getElementById('pudgeStudyCard');
    if (!pop?.querySelectorAll) return;
    const reason = studyReviewBlockReason(current);
    for (const button of pop.querySelectorAll('[data-pudge-study-review]') || []) {
      setStudyButtonAvailability(button, reason, button.dataset?.pudgeStudyHint || '');
    }
    setStudyButtonAvailability(pop.querySelector?.('[data-pudge-study-add]'), studyAddBlockReason(current));
  }

  function studyGradeButtonsHtml(current) {
    const api = globalThis.PudgeReviewActions;
    return (current?.reviewActions?.actions || []).map(action => {
      const text = api?.label ? api.label(action) : action.id;
      const hint = action.shortcut ? ` title="${esc(action.shortcut)}" data-pudge-study-hint="${esc(action.shortcut)}"` : '';
      // aria-disabled (not disabled): WebKit shows no tooltip on a disabled button.
      return `<button class="pudge-study-grade grade-${esc(action.tone)}" data-pudge-study-review="${esc(action.id)}"${hint} aria-disabled="false">${esc(text)}</button>`;
    }).join('');
  }

  function handleReviewKeydown(event) {
    const card = document.getElementById('pudgeStudyCard');
    if (!card?.classList.contains('open') || !activeToken) return false;
    if (event?.isComposing || event?.repeat) return false;
    if (event?.target?.closest?.('input,textarea,select,[contenteditable="true"]')) return false;

    const key = String(event?.key || '');
    const code = String(event?.code || '');
    // A grade shortcut only selects (focuses) its button; Space confirms.
    const action = globalThis.PudgeReviewActions?.matchAction?.(event, activeToken.reviewActions);
    if (action) {
      const button = card.querySelector(`[data-pudge-study-review="${action.id}"]`);
      if (!button || button.disabled) return false;
      event.preventDefault?.();
      event.stopPropagation?.();
      event.stopImmediatePropagation?.();
      button.focus?.({preventScroll:true});
      return true;
    }

    if (event?.metaKey || event?.ctrlKey || event?.altKey || event?.shiftKey) return false;
    if (code === 'Space' || key === ' ') {
      const focused = document.activeElement?.closest?.('[data-pudge-study-review]');
      if (!focused || !card.contains(focused) || focused.disabled) return false;
      event.preventDefault?.();
      event.stopPropagation?.();
      event.stopImmediatePropagation?.();
      focused.click();
      return true;
    }
    return false;
  }

  function textWithoutRuby(fragment) {
    const clone = fragment.cloneNode(true);
    clone.querySelectorAll?.('rt,rp').forEach(node => node.remove());
    return String(clone.textContent || '');
  }

  function selectedContext(root, range) {
    try {
      const before = document.createRange();
      before.selectNodeContents(root);
      before.setEnd(range.startContainer, range.startOffset);
      return textWithoutRuby(before.cloneContents()).replace(/\s+/g, ' ').trim().slice(-200);
    } catch (_) {
      return '';
    }
  }

  function hideTranslation() {
    translationRequest += 1;
    grammarRequest += 1;
    lastTranslationSelection = null;
    const pop = document.getElementById('pudgeTranslationPop');
    if (pop) {
      pop.classList.remove('open', 'loading');
      pop.textContent = '';
    }
  }

  // Two selection handlers (reader + generic) can fire for one mouseup: one
  // LLM translation per identical request while it is in flight.
  const translationInflight = new Map();
  function translateOnce(text, context, language, mediaId) {
    const key = JSON.stringify([text, context, language, mediaId]);
    if (translationInflight.has(key)) return translationInflight.get(key);
    const task = Promise.resolve(apiTranslate(text, context, language, mediaId)).finally(() => translationInflight.delete(key));
    translationInflight.set(key, task);
    return task;
  }

  async function translateSelection(root, {targetLanguage = ''} = {}) {
    if (!root) return false;
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
    const range = selection.getRangeAt(0);
    if (!root.contains(range.commonAncestorContainer)) return false;
    const text = textWithoutRuby(range.cloneContents()).replace(/\s+/g, ' ').trim();
    if (!text || !/[\u3040-\u30ff\u3400-\u9fff]/.test(text)) return false;
    const context = selectedContext(root, range);
    const {translation: pop} = ensureUi();
    const id = ++translationRequest;
    // "Explain grammar" is available immediately; it does not need the translation.
    lastTranslationSelection = {text, context, translationId: id};
    const grammarActions = llmAvailable() && API()?.analyze_grammar ? `<div class="pudge-translation-actions"><button type="button" data-pudge-grammar>${ru() ? 'Разобрать грамматику' : 'Explain grammar'}</button></div><div class="pudge-grammar" hidden></div>` : '';
    pop.innerHTML = `<div class="pudge-translation-text">${esc(ru() ? 'Перевожу…' : 'Translating…')}</div>${grammarActions}`;
    pop.classList.add('open', 'loading');
    position(pop, range.getBoundingClientRect());
    const paint = value => {
      const box = pop.querySelector?.('.pudge-translation-text');
      if (box) box.textContent = String(value || '');
      else pop.innerHTML = `<div class="pudge-translation-text">${esc(value || '')}</div>${grammarActions}`;
      pop.classList.remove('loading');
      position(pop, range.getBoundingClientRect());
    };
    try {
      const language = String(targetLanguage || (ru() ? 'ru' : 'en')).toLowerCase();
      const mediaId = Number(root.dataset.pudgeMediaId || 0) || null;
      const result = await translateOnce(text, context, language, mediaId);
      if (id !== translationRequest) return true;
      paint(result?.translation || '');
    } catch (error) {
      if (id !== translationRequest) return true;
      paint(error?.message || String(error));
    }
    return true;
  }

  const GRAMMAR_REASONS = {
    llm_disabled: ['Включите LLM в настройках, чтобы разбирать грамматику.', 'Enable the LLM in Settings to explain grammar.'],
    selection_too_long: ['Выделение слишком длинное: выделите одно-два предложения.', 'Selection is too long: select one or two sentences.'],
    context_too_long: ['Контекст слишком длинный.', 'Context is too long.'],
    empty_selection: ['Выделите японский текст.', 'Select Japanese text.'],
    invalid_structure: ['LLM вернула ответ в неверном формате.', 'The LLM returned an invalid answer.'],
    llm_no_json: ['LLM не вернула JSON (подробности ниже и в логе).', 'The LLM returned no JSON (details below and in the log).'],
  };

  // Pure renderer (tested in tests/js/grammar_analysis.cjs). Spans are Unicode
  // code point offsets; Array.from() iterates code points, not UTF-16 units.
  function grammarHtml(result) {
    const lang = ru() ? 0 : 1;
    const status = String(result?.status || 'error');
    if (status === 'unavailable' || status === 'error') {
      const reason = String(result?.reason || '');
      const message = GRAMMAR_REASONS[reason]?.[lang] || (ru() ? 'Не удалось разобрать грамматику' : 'Grammar analysis failed');
      return `<div class="pudge-grammar-error">${esc(message)}${result?.detail ? `<small>${esc(result.detail)}</small>` : ''}</div>`;
    }
    const chars = Array.from(String(result?.sentence || ''));
    const cover = chars.map(() => []);
    const points = Array.isArray(result?.points) ? result.points : [];
    for (const point of points) {
      for (const span of Array.isArray(point?.spans) ? point.spans : []) {
        const start = Number(span?.start), end = Number(span?.end);
        if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > chars.length || end <= start) continue;
        if (chars.slice(start, end).join('') !== String(span?.quote ?? chars.slice(start, end).join(''))) continue;
        for (let i = start; i < end; i += 1) cover[i].push(String(point.id));
      }
    }
    let sentence = '';
    for (let i = 0; i < chars.length;) {
      const key = cover[i].join(' ');
      let j = i + 1;
      while (j < chars.length && cover[j].join(' ') === key) j += 1;
      const piece = esc(chars.slice(i, j).join(''));
      sentence += key ? `<span class="pudge-grammar-seg" data-points="${esc(key)}">${piece}</span>` : piece;
      i = j;
    }
    const certaintyLabel = {high: ru() ? 'уверенно' : 'confident', medium: ru() ? 'вероятно' : 'likely', low: ru() ? 'не уверена' : 'unsure'};
    const items = points.map(point => `<li data-grammar-point="${esc(point.id)}"${point.anchored ? '' : ' class="unanchored"'}><button type="button" data-grammar-point-toggle="${esc(point.id)}"><b>${esc(point.pattern)}</b>${point.meaning_in_context ? ` — ${esc(point.meaning_in_context)}` : ''}</button><div class="pudge-grammar-detail">${esc(point.explanation || '')}${point.form ? `<small>${esc(point.form)}</small>` : ''}${certaintyLabel[point.certainty] ? `<small>${esc(certaintyLabel[point.certainty])}</small>` : ''}${point.anchored ? '' : `<small>${ru() ? 'Не удалось точно найти в тексте' : 'Could not be located exactly in the text'}</small>`}</div></li>`).join('');
    const empty = points.length ? '' : `<p class="pudge-grammar-empty">${ru() ? 'Заметных грамматических конструкций нет.' : 'No notable grammar points.'}</p>`;
    return `<div class="pudge-grammar-sentence" lang="ja">${sentence}</div>${result?.translation ? `<div class="pudge-grammar-translation">${esc(result.translation)}</div>` : ''}${empty}<ol class="pudge-grammar-points">${items}</ol><div class="pudge-grammar-note">${ru() ? 'Разбор сделан LLM и может содержать ошибки.' : 'Generated by an LLM; it may contain mistakes.'}</div>`;
  }

  function setGrammarActive(pop, pointId) {
    const root = pop.querySelector?.('.pudge-grammar');
    if (!root) return;
    const next = root.dataset.activePoint === pointId ? '' : pointId;
    root.dataset.activePoint = next;
    root.querySelectorAll?.('.pudge-grammar-seg').forEach(node => {
      const ids = String(node.dataset.points || '').split(' ');
      node.classList.toggle('active', Boolean(next) && ids.includes(next));
    });
    root.querySelectorAll?.('[data-grammar-point]').forEach(node => {
      node.classList.toggle('active', node.dataset.grammarPoint === next);
    });
  }

  async function analyzeGrammarForTranslation() {
    if (!llmAvailable()) return false;
    const pop = document.getElementById('pudgeTranslationPop');
    const selection = lastTranslationSelection;
    // The reading assistant (chat panel on the right) owns grammar explanations;
    // the inline block below stays as a fallback when it is not loaded.
    if (selection && globalThis.PudgeAssistant?.startGrammar) {
      return globalThis.PudgeAssistant.startGrammar({text: selection.text, context: selection.context});
    }
    const root = pop?.querySelector?.('.pudge-grammar');
    if (!pop || !selection || !root || !API()?.analyze_grammar) return false;
    const id = ++grammarRequest;
    const requestId = `g${id}-${Date.now()}`;
    const button = pop.querySelector('[data-pudge-grammar]');
    if (button) button.disabled = true;
    root.hidden = false;
    root.textContent = ru() ? 'Разбираю…' : 'Analysing…';
    let html;
    try {
      const result = await API().analyze_grammar(selection.text, selection.context, requestId);
      // A late answer must not paint another selection or a closed popup. The
      // selection object may be re-created for the same text (clicking the
      // button can re-run selection handling), so compare the text.
      if (id !== grammarRequest || !lastTranslationSelection || lastTranslationSelection.text !== selection.text) return true;
      if (result?.request_id && result.request_id !== requestId) return true;
      html = grammarHtml(result || {});
    } catch (error) {
      if (id !== grammarRequest || !lastTranslationSelection || lastTranslationSelection.text !== selection.text) return true;
      html = `<div class="pudge-grammar-error">${esc((window.PudgeUiLanguage?.message(error?.message || String(error)) ?? String(error?.message || String(error))))}</div>`;
    }
    // Paint into the live popup (the node captured at click time may have
    // been replaced by a re-render of the same translation).
    const liveRoot = document.getElementById('pudgeTranslationPop')?.querySelector?.('.pudge-grammar') || root;
    liveRoot.hidden = false;
    liveRoot.innerHTML = html;
    const liveButton = document.getElementById('pudgeTranslationPop')?.querySelector?.('[data-pudge-grammar]') || button;
    if (liveButton) liveButton.hidden = true;
    return true;
  }

  document.addEventListener('click', event => {
    const pop = document.getElementById('pudgeTranslationPop');
    if (!pop || !event.target.closest?.('#pudgeTranslationPop')) return;
    if (event.target.closest?.('[data-pudge-grammar]')) { void analyzeGrammarForTranslation(); return; }
    const toggle = event.target.closest?.('[data-grammar-point-toggle]');
    if (toggle) { setGrammarActive(pop, String(toggle.dataset.grammarPointToggle || '')); return; }
    const seg = event.target.closest?.('.pudge-grammar-seg');
    if (seg) setGrammarActive(pop, String(seg.dataset.points || '').split(' ')[0] || '');
  });

  function registerToken(token) {
    const id = `study-${++tokenSequence}`;
    registry.set(id, token);
    if (registry.size > 4000) {
      for (const key of [...registry.keys()].slice(0, 1500)) registry.delete(key);
    }
    return id;
  }

  function renderParsedParagraph(payload, paragraphIndex, {backend = 'jiten', contextText = '', contextOffset = 0, mediaContext = null, highlightOptimalWords = true, furigana = false} = {}) {
    if (payload?.settings) applyStudyAppearance(payload.settings);
    const paragraphs = payload?.paragraphs || [];
    const text = String(paragraphs[Number(paragraphIndex)] || '');
    if (!text) return '';
    const vocab = new Map();
    for (const item of payload?.vocabulary || []) {
      vocab.set(`${item.wordId}:${item.readingIndex}`, item);
    }
    const tokenGroups = payload?.tokens || [];
    const ordered = [...(tokenGroups[Number(paragraphIndex)] || [])]
      .sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
    let out = '', pos = 0;
    for (const token of ordered) {
      const rawStart = Number(token.start || 0);
      const rawEnd = Number(token.end ?? (rawStart + Number(token.length || 0)));
      if (!Number.isFinite(rawStart) || !Number.isFinite(rawEnd) ||
          rawEnd <= rawStart || rawStart < pos || rawStart > text.length) continue;
      const start = rawStart, end = Math.min(text.length, rawEnd);
      out += esc(text.slice(pos, start));
      const surface = text.slice(start, end);
      if (!surface) continue;
      const card = token.card || vocab.get(`${token.wordId}:${token.readingIndex}`) || {};
      const state = normalizeState(card);
      const sentence = String(contextText || text);
      const offset = contextText ? Number(contextOffset || 0) : 0;
      const allowOptimalHighlights = highlightOptimalWords !== false && String(mediaContext?.kind || '') !== 'manga';
      const id = registerToken({...token, card, surface, sentence, contextStart:start + offset, contextEnd:end + offset, backend, mediaContext, highlightOptimalWords:allowOptimalHighlights});
      out += `<span class="pudge-study-word state-${esc(state)}" data-pudge-study-token="${id}">${furigana ? surfaceWithRuby(surface, token, start) : esc(surface)}</span>`;
      pos = end;
    }
    out += esc(text.slice(pos));
    return `<p>${out}</p>`;
  }

  // Furigana from the parser's ruby spans (paragraph offsets) for one token.
  function surfaceWithRuby(surface, token, tokenStart) {
    const rubies = (Array.isArray(token?.rubies) ? token.rubies : [])
      .map(ruby => ({start: Number(ruby?.start) - tokenStart, end: Number(ruby?.end ?? (Number(ruby?.start) + Number(ruby?.length || 0))) - tokenStart, text: String(ruby?.text || '')}))
      .filter(ruby => Number.isFinite(ruby.start) && Number.isFinite(ruby.end) && ruby.start >= 0 && ruby.end > ruby.start && ruby.end <= surface.length && ruby.text)
      .sort((a, b) => a.start - b.start);
    if (!rubies.length) return esc(surface);
    let out = '', pos = 0;
    for (const ruby of rubies) {
      if (ruby.start < pos) continue;
      out += esc(surface.slice(pos, ruby.start));
      out += `<ruby class="pudge-study-ruby">${esc(surface.slice(ruby.start, ruby.end))}<rt>${esc(ruby.text)}</rt></ruby>`;
      pos = ruby.end;
    }
    return out + esc(surface.slice(pos));
  }

  function renderParsedText(payload, {backend = 'jiten', contextText = '', contextOffset = 0, mediaContext = null, highlightOptimalWords = true} = {}) {
    if (payload?.settings) applyStudyAppearance(payload.settings);
    const paragraphs = payload?.paragraphs || [];
    const html = paragraphs.map((_, paragraphIndex) =>
      renderParsedParagraph(payload, paragraphIndex, {backend, contextText, contextOffset, mediaContext, highlightOptimalWords})
    ).join('');
    queueLiveStateHydration(payload, backend);
    return html;
  }

  let studyHoverTimer = null;
  let studyHoverKey = '';

  document.addEventListener('pointerover', event => {
    const word = event.target.closest?.('[data-pudge-study-token]');
    if (!word || !word.closest?.('[data-pudge-study-hover]')) return;
    const key = String(word.dataset.pudgeStudyToken || '');
    if (!key || key === studyHoverKey) return;
    const token = registry.get(key);
    if (!token) return;
    studyHoverKey = key;
    if (studyHoverTimer) clearTimeout(studyHoverTimer);
    studyHoverTimer = setTimeout(() => {
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed) return;
      void openStudyCard({
        token,
        target: word,
        backend: token.backend || 'jiten',
        sentence: token.sentence || '',
      });
    }, 180);
  }, true);

  document.addEventListener('pointerout', event => {
    const word = event.target.closest?.('[data-pudge-study-token]');
    if (!word || word.contains(event.relatedTarget)) return;
    if (studyHoverKey === String(word.dataset.pudgeStudyToken || '')) studyHoverKey = '';
    if (studyHoverTimer) {
      clearTimeout(studyHoverTimer);
      studyHoverTimer = null;
    }
  }, true);

  document.addEventListener('change', event => {
    if (event.target?.id !== 'pudgeStudyDeck') return;
    const backend = String(event.target.dataset.pudgeStudyBackend || activeToken?.backend || 'jiten');
    rememberStudyDeck(backend, event.target.value || '');
    updateStudyReviewAvailability(activeToken);
  }, true);

  function firstStudyTokenFromPayload(payload, backend = 'jiten') {
    const paragraphs = payload?.paragraphs || [];
    const vocabulary = new Map();
    for (const item of payload?.vocabulary || []) {
      if (!item || typeof item !== 'object') continue;
      vocabulary.set(`${item.wordId}:${item.readingIndex}`, item);
    }
    let fallback = null;
    for (let paragraphIndex = 0; paragraphIndex < paragraphs.length; paragraphIndex++) {
      const sentence = String(paragraphs[paragraphIndex] || '');
      const ordered = [...((payload?.tokens || [])[paragraphIndex] || [])]
        .sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
      for (const raw of ordered) {
        const start = Number(raw.start || 0);
        const end = Number(raw.end ?? (start + Number(raw.length || 0)));
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
        const surface = sentence.slice(start, Math.min(sentence.length, end));
        if (!surface || !/[\u3040-\u30ff\u3400-\u9fff]/.test(surface)) continue;
        const card = raw.card || vocabulary.get(`${raw.wordId}:${raw.readingIndex}`) || {};
        const token = {...raw, card, surface, sentence, backend};
        if (!fallback) fallback = token;
        if (/[\u3400-\u9fff]/.test(surface) || String(card.spelling || '').trim()) return token;
      }
    }
    return fallback;
  }

  async function openStudyText(text, rect = null, {backend = 'jiten'} = {}) {
    const selected = String(text || '').replace(/\s+/g, '').trim();
    if (!selected || !/[\u3040-\u30ff\u3400-\u9fff]/.test(selected)) return false;
    const parse = API()?.study_parse_text;
    if (typeof parse !== 'function') return false;
    const payload = await parse(selected);
    if (payload?.settings) applyStudyAppearance(payload.settings);
    const token = firstStudyTokenFromPayload(payload, backend);
    if (!token) return false;
    const anchorRect = rect && Number.isFinite(Number(rect.left))
      ? {
          left:Number(rect.left), top:Number(rect.top), right:Number(rect.right), bottom:Number(rect.bottom),
          width:Number(rect.width || Math.max(1, Number(rect.right) - Number(rect.left))),
          height:Number(rect.height || Math.max(1, Number(rect.bottom) - Number(rect.top))),
        }
      : {left:window.innerWidth / 2 - 1, right:window.innerWidth / 2 + 1, top:window.innerHeight / 2 - 1, bottom:window.innerHeight / 2 + 1, width:2, height:2};
    await openStudyCard({
      token,
      target:{getBoundingClientRect:() => anchorRect},
      backend:String(backend || token.backend || 'jiten'),
      sentence:token.sentence || selected,
    });
    return true;
  }

  async function openStudyElement(word) {
    if (!word) return false;
    const token = registry.get(String(word.dataset?.pudgeStudyToken || ''));
    if (!token) return false;
    const selection = window.getSelection?.();
    if (selection && !selection.isCollapsed) return false;
    await openStudyCard({
      token,
      target: word,
      backend: token.backend || 'jiten',
      sentence: token.sentence || '',
    });
    return true;
  }


  document.addEventListener('click', async event => {
    const close = event.target.closest?.('[data-pudge-study-close]');
    if (close) {
      closeStudyCard();
      return;
    }
    const extra = event.target.closest?.('[data-pudge-study-extra-action]');
    if (extra && activeToken) {
      const action = activeToken.extraActions?.get(String(extra.dataset.pudgeStudyExtraAction || ''));
      if (action) {
        extra.disabled = true;
        try {
          await action.run();
          if (action.close !== false) closeStudyCard();
        } finally {
          extra.disabled = false;
        }
      }
      return;
    }
    const word = event.target.closest?.('[data-pudge-study-token]');
    if (word) {
      await openStudyElement(word);
      return;
    }
    const jiten = event.target.closest?.('[data-pudge-study-jiten]');
    if (jiten) {
      const wordId = Number(jiten.dataset.wordId || 0);
      const readingIndex = Number(jiten.dataset.readingIndex ?? -1);
      if (wordId > 0 && readingIndex >= 0) {
        const url = `https://jiten.moe/vocabulary/${wordId}/${readingIndex}`;
        try { await API().open_url(url); } catch (error) { window.toast?.(error?.message || String(error)); }
      }
      return;
    }
    const review = event.target.closest?.('[data-pudge-study-review]');
    if (review && activeToken) {
      if (!activeToken.reviewable || activeToken.pendingReview || activeToken.reviewOutcomeUnknown) return;
      const reviewAction = String(review.dataset.pudgeStudyReview || '');
      if (!(activeToken.reviewActions?.actions || []).some(action => action.id === reviewAction)) return;
      const current = activeToken;
      const token = current.token;
      const attemptId = globalThis.crypto?.randomUUID?.() || `pudge-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let deck = document.getElementById('pudgeStudyDeck')?.value || '';
      const isJiten = String(current.backend || '').toLowerCase() === 'jiten';
      const memberships = () => (Array.isArray(token.card?.studyDeckIds) ? token.card.studyDeckIds : null);
      // Block repeated clicks while the membership decision is pending.
      current.pendingReview = true;
      if (isJiten && !deck && memberships()?.length === 0 && current.liveRefreshState === 'pending' && current.liveRefresh) {
        // A cached parse snapshot of [] does not prove the word is new: wait for
        // the live refresh this card already started (no second lookup).
        try { await current.liveRefresh; } catch (_) {}
        if (activeToken !== current || current.generation !== activeToken?.generation) {
          current.pendingReview = false;
          return;
        }
        deck = document.getElementById('pudgeStudyDeck')?.value || '';
      }
      const blocked = isJiten && !deck ? studyReviewBlockReason({...current, pendingReview:false}, deck) : '';
      if (blocked) {
        current.pendingReview = false;
        updateStudyReviewAvailability(current);
        window.toast?.(blocked);
        return;
      }
      if (liveStateBackend(current)) invalidateJitenStatePair({...token, backend:current.backend, idNamespace:current.idNamespace});
      suppressOptimalForPair(token, current.target);
      closeStudyCard();
      try {
        const result = await apiAction({
          backend: current.backend,
          action: 'review',
          word_id: token.wordId,
          reading_index: token.readingIndex,
          grade: reviewAction,
          deck_id: deck,
          sentence: current.sentence,
          media_context: token.mediaContext || null,
          attempt_id: attemptId,
          id_namespace: current.idNamespace,
        });
        if (result?.outcome === 'unknown') {
          window.toast?.(result?.message || (ru() ? 'Результат review неизвестен; повтор автоматически заблокирован' : 'Review outcome is unknown; automatic retry is blocked'));
          return;
        }
        if (result?.ok === false) {
          rollbackOptimisticStudyPair(token);
          if (liveStateBackend(current)) void refreshJitenPairAfterMutation({...token, backend:current.backend, idNamespace:current.idNamespace});
          window.toast?.(result?.message || (ru() ? 'Не удалось сохранить review' : 'Could not save review'));
          return;
        }
        if (liveStateBackend(current)) {
          void refreshJitenPairAfterMutation({...token, backend:current.backend, idNamespace:current.idNamespace});
        }
        window.dispatchEvent?.(new CustomEvent('pudge-review-completed',{detail:{backend:current.backend,wordId:Number(token.wordId||0),readingIndex:Number(token.readingIndex??-1)}}));
        if (Array.isArray(result?.enrichment_warnings) && result.enrichment_warnings.length) {
          window.toast?.(`${ru() ? 'Review сохранён; дополнение не удалось' : 'Review saved; enrichment failed'}: ${result.enrichment_warnings.join('; ')}`);
        }
      } catch (error) {
        rollbackOptimisticStudyPair(token);
        window.toast?.(error?.message || String(error));
      }
      return;
    }
    const add = event.target.closest?.('[data-pudge-study-add]');
    if (add && activeToken) {
      const current = activeToken;
      const token = current.token;
      const deck = document.getElementById('pudgeStudyDeck')?.value || '';
      const addBlocked = studyAddBlockReason(current, deck);
      if (addBlocked) {
        window.toast?.(addBlocked);
        return;
      }
      const request = {
        backend: current.backend,
        action: 'add',
        word_id: token.wordId,
        reading_index: token.readingIndex,
        deck_id: deck,
        sentence: current.sentence,
        id_namespace: current.idNamespace,
      };
      if (liveStateBackend(current)) invalidateJitenStatePair({...token, backend:current.backend, idNamespace:current.idNamespace});
      suppressOptimalForPair(token, current.target);
      closeStudyCard();
      try {
        const result = await apiAction(request);
        if (result?.ok === false) {
          rollbackOptimisticStudyPair(token);
          if (liveStateBackend(current)) void refreshJitenPairAfterMutation({...token, backend:current.backend, idNamespace:current.idNamespace});
          window.toast?.(result?.message || (ru() ? 'Не удалось добавить слово' : 'Could not add word'));
        } else if (result?.outcome !== 'unknown' && liveStateBackend(current)) {
          void refreshJitenPairAfterMutation({...token, backend:current.backend, idNamespace:current.idNamespace});
        }
      } catch (error) {
        rollbackOptimisticStudyPair(token);
        window.toast?.(error?.message || String(error));
      }
      return;
    }
    if (event.target.closest?.('[data-ln-token]')) return;
    // PudgeSelect renders its menu under <body>, outside the study-card DOM.
    // Choosing a deck must close only that menu, not the Jiten card itself.
    if (event.target.closest?.('.pudge-select-menu')) return;
    const pop = document.getElementById('pudgeStudyCard');
    if (pop?.classList.contains('open') && !event.target.closest?.('#pudgeStudyCard')) closeStudyCard();
  }, true);

  function studyTextClickTarget(target) {
    if (!target?.closest) return false;
    if (target.closest('button,a,input,select,textarea,[contenteditable="true"]')) return false;
    return Boolean(target.closest('[data-pudge-study-token],[data-ln-token]'));
  }

  // Keep ordinary drag selection, but disable the browser's special
  // double/triple-click word/paragraph selection. Clicks remain available to
  // toggle the Jiten card; click+drag still selects arbitrary characters.
  document.addEventListener('mousedown', event => {
    if (event.button !== 0 || Number(event.detail || 0) < 2 || !studyTextClickTarget(event.target)) return;
    event.preventDefault();
  }, true);
  document.addEventListener('dblclick', event => {
    if (!studyTextClickTarget(event.target)) return;
    event.preventDefault();
    const selection = window.getSelection?.();
    if (selection && !selection.isCollapsed) selection.removeAllRanges();
  }, true);

  document.addEventListener('pointerdown', event => {
    if (!event.target.closest?.('#pudgeTranslationPop') &&
        !event.target.closest?.('[data-pudge-translate-root]')) {
      hideTranslation();
    }
  });

  document.addEventListener('mouseup', event => {
    const root = event.target.closest?.('[data-pudge-translate-root]');
    if (!root) return;
    setTimeout(() => void translateSelection(root, {
      targetLanguage: root.dataset.pudgeTranslateLanguage || '',
    }), 0);
  });

  if (typeof window.addEventListener === 'function') {
    // Header layout depends on the available width; re-measure on window resize
    // (coalesced to one frame), never on player/timer ticks.
    let studyResizeFrame = 0;
    window.addEventListener('resize', () => {
      if (studyResizeFrame || typeof requestAnimationFrame !== 'function') return;
      studyResizeFrame = requestAnimationFrame(() => {
        studyResizeFrame = 0;
        const pop = document.getElementById('pudgeStudyCard');
        if (!pop?.classList.contains('open') || !activeToken?.anchorRect) return;
        sizeStudyCard(pop);
        position(pop, activeToken.anchorRect);
      });
    });
    window.addEventListener('pywebviewready', () => {
      const backend = String(window.ui?.lnState?.settings?.study_backend || 'jiten');
      void apiDecks(backend).catch(() => {});
    }, {once:true});
  }

  window.PudgeReadingTools = {
    llmAvailable,
    study: {
      open: openStudyCard,
      openElement: openStudyElement,
      openText: openStudyText,
      close: closeStudyCard,
      isOpen() {
        return Boolean(document.getElementById('pudgeStudyCard')?.classList.contains('open'));
      },
      closeIfOpen() {
        if (!document.getElementById('pudgeStudyCard')?.classList.contains('open')) return false;
        closeStudyCard();
        return true;
      },
      handleReviewKeydown,
      renderParsedText,
      renderParsedParagraph,
      surfaceWithRuby,
      contextForToken: studyContextFromEntries,
      lookupLiveStates,
      queueLiveStateHydration,
      currentAccountScope() { return studyStateScope; },
      verifiedJitenStateForElement(node) {
        if (!studyStateScope || node?.dataset?.pudgeStudyVerified !== '1' ||
            node.dataset.pudgeStudyScope !== studyStateScope) return null;
        const token = registry.get(String(node.dataset.pudgeStudyToken || ''));
        if (!token || String(token.backend || 'jiten').toLowerCase() !== 'jiten' ||
            !jitenTokenPair(token)) return null;
        const states = (token.card?.states || token.card?.knownState || [])
          .map(value => String(value || '').toLowerCase().replace(/_/g, '-'));
        const due = states.some(value => ['due','failed'].includes(value));
        const ignored = states.some(value => ['blacklisted','redundant','ignored'].includes(value));
        if (ignored) return {level:'unknown', due, ignored:true, states, pair:jitenTokenPair(token)};
        // Due is an orthogonal review flag. Preserve the durable knowledge tier
        // instead of painting every due Mature/Known word as Learning.
        let level = 'unknown';
        if (states.some(value => ['known','mastered','never-forget'].includes(value))) level = 'known';
        else if (states.some(value => ['learning','young','mature'].includes(value))) level = 'learning';
        else if (states.includes('new')) level = 'new';
        else {
          const normalized = normalizeState(token.card || {});
          if (['new','learning','known'].includes(normalized)) level = normalized;
        }
        if (!['new','learning','known'].includes(level)) return null;
        return {level, due, ignored:false, states, pair:jitenTokenPair(token)};
      },
      applyOptimalHighlights,
      evaluateOptimalTargets,
      refreshOptimalHighlights,
      optimalWordLimit() { return latestOptimalWordLimit; },
      inlinePitch: renderInlinePitch,
      inlinePitchOnSurface: renderInlinePitchOnSurface,
      inflectedPitchCard,
      applyAppearance: applyStudyAppearance,
    },
    grammar: {
      html: grammarHtml,
      analyze: analyzeGrammarForTranslation,
    },
    translation: {
      translateSelection,
      hide: hideTranslation,
    },
    isOpen() {
      return Boolean(
        document.getElementById('pudgeStudyCard')?.classList.contains('open') ||
        document.getElementById('pudgeTranslationPop')?.classList.contains('open')
      );
    },
    closeIfOpen() {
      const card = document.getElementById('pudgeStudyCard');
      if (card?.classList.contains('open')) { closeStudyCard(); return true; }
      const translation = document.getElementById('pudgeTranslationPop');
      if (translation?.classList.contains('open')) { hideTranslation(); return true; }
      return false;
    },
    closeAll() {
      closeStudyCard();
      hideTranslation();
    },
  };
})();
