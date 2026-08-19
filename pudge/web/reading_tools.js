'use strict';

(() => {
  const API = () => window.pywebview && window.pywebview.api;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[ch]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
  const STUDY_THEMES = new Set(['balanced','jiten','jpdb','focus','underline','none','custom']);
  const STUDY_COLORS = {
    new: '#f3f6fb', learning: '#f4bd63', due: '#ff7d8c',
    known: '#57d38c', blacklisted: '#7d8795',
  };

  const registry = new Map();
  let tokenSequence = 0;
  let activeToken = null;
  let translationRequest = 0;

  const selectedDeckByBackend = new Map();

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
    const state = normalizeState(card);
    const labels = ru()
      ? {new:'Новое',learning:'Изучается',due:'К повторению',known:'Известно',blacklisted:'Игнорируется'}
      : {new:'New',learning:'Learning',due:'Due',known:'Known',blacklisted:'Blacklisted'};
    return labels[state] || labels.new;
  }

  function rubyReading(value) {
    const text = String(value || '');
    let out = '', last = 0;
    const pattern = /([\u3400-\u9fff々〆ヵヶ])\[([^\]\r\n]+)\]/g;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      out += esc(text.slice(last, match.index));
      out += `<ruby>${esc(match[1])}<rt>${esc(match[2])}</rt></ruby>`;
      last = pattern.lastIndex;
    }
    out += esc(text.slice(last));
    return out || esc(text);
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
    return `<div class="pudge-study-pitch"><span class="pudge-study-pitch-label">${ru() ? 'Акцент' : 'Pitch accent'}</span>${rows}</div>`;
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

  function position(el, rect) {
    requestAnimationFrame(() => {
      const r = el.getBoundingClientRect();
      let left = rect.left + rect.width / 2 - r.width / 2;
      left = Math.max(8, Math.min(left, window.innerWidth - r.width - 8));
      let top = rect.bottom + 10;
      if (top + r.height > window.innerHeight - 8) top = Math.max(8, rect.top - r.height - 10);
      el.style.left = `${left}px`;
      el.style.top = `${top}px`;
    });
  }

  async function apiDecks(backend) {
    if (API()?.study_decks) return API().study_decks(backend);
    return API().light_novel_decks(backend);
  }

  async function apiAction(payload) {
    if (API()?.study_action) return API().study_action(payload);
    return API().light_novel_study_action(payload);
  }

  async function apiTranslate(text, context, targetLanguage, mediaId = null) {
    if (API()?.translate_text) return API().translate_text(text, context, targetLanguage, mediaId);
    return API().light_novel_translate(text, context, targetLanguage, mediaId);
  }

  async function openStudyCard({token, target, backend = 'jiten', sentence = '', actions = []}) {
    if (!token || !target) return;
    const {card: pop} = ensureUi();
    // Manga replaces the OCR region DOM when it becomes active. Keep the
    // original on-screen anchor so the async deck request cannot move the card
    // to (0, 0) after the clicked word has been detached.
    const anchorRect = target.getBoundingClientRect();
    const card = token.card || {};
    const extraActions = new Map(
      (Array.isArray(actions) ? actions : [])
        .filter(action => action && action.id && typeof action.run === 'function')
        .map(action => [String(action.id), action])
    );
    activeToken = {
      token,
      backend: String(backend || 'jiten'),
      sentence: String(sentence || token.sentence || ''),
      extraActions,
    };
    const meaningsRaw = card.meanings || card.meaningsChunks || [];
    const meanings = Array.isArray(meaningsRaw) ? meaningsRaw.flat?.() || meaningsRaw : [];
    const status = stateLabel(card);
    pop.innerHTML = `
      <div class="pudge-study-head">
        <div>
          <h3>${esc(card.spelling || token.surface || '')}</h3>
          <div class="pudge-study-reading">${rubyReading(card.reading || '')}</div>
        </div>
        <button data-pudge-study-close aria-label="Close">×</button>
      </div>
      <div class="pudge-study-subtle">${esc(status)}${card.frequencyRank ? ` • Frequency #${Number(card.frequencyRank)}` : ''}</div>
      ${renderPitchAccent(card)}
      <ol class="pudge-study-meanings">
        ${meanings.length
          ? meanings.slice(0, 8).map(x => `<li>${esc(typeof x === 'string' ? x : JSON.stringify(x))}</li>`).join('')
          : `<li class="pudge-study-subtle">${ru() ? 'Нет значений' : 'No meanings returned'}</li>`}
      </ol>
      <select id="pudgeStudyDeck"><option value="">${ru() ? 'Колода…' : 'Study deck…'}</option></select>
      <div class="pudge-study-actions">
        <button data-pudge-study-review="again">${ru() ? 'Снова' : 'Again'}</button>
        <button data-pudge-study-review="hard">${ru() ? 'Трудно' : 'Hard'}</button>
        <button data-pudge-study-review="good">${ru() ? 'Хорошо' : 'Good'}</button>
        <button data-pudge-study-review="easy">${ru() ? 'Легко' : 'Easy'}</button>
        <button data-pudge-study-add>${ru() ? 'Добавить' : 'Add'}</button>
        ${[...extraActions.entries()].map(([id, action]) =>
          `<button class="pudge-study-extra-action" data-pudge-study-extra-action="${esc(id)}">${esc(action.label || id)}</button>`
        ).join('')}
      </div>`;
    pop.classList.add('open');
    position(pop, anchorRect);
    try {
      const decks = await apiDecks(activeToken.backend);
      if (activeToken?.token !== token) return;
      const select = document.getElementById('pudgeStudyDeck');
      if (select) {
        select.innerHTML = `<option value="">${ru() ? 'Колода…' : 'Study deck…'}</option>` +
          (decks || []).map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
        const remembered = rememberedStudyDeck(activeToken.backend);
        if (remembered && [...select.options].some(option => option.value === remembered)) {
          select.value = remembered;
        }
      }
      position(pop, anchorRect);
    } catch (error) {
      const select = document.getElementById('pudgeStudyDeck');
      if (select) {
        select.innerHTML = `<option value="">${esc(error?.message || String(error))}</option>`;
        select.disabled = true;
      }
    }
  }

  function closeStudyCard() {
    activeToken = null;
    document.getElementById('pudgeStudyCard')?.classList.remove('open');
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
    const pop = document.getElementById('pudgeTranslationPop');
    if (pop) {
      pop.classList.remove('open', 'loading');
      pop.textContent = '';
    }
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
    pop.textContent = ru() ? 'Перевожу…' : 'Translating…';
    pop.classList.add('open', 'loading');
    position(pop, range.getBoundingClientRect());
    try {
      const language = String(targetLanguage || (ru() ? 'ru' : 'en')).toLowerCase();
      const mediaId = Number(root.dataset.pudgeMediaId || 0) || null;
      const result = await apiTranslate(text, context, language, mediaId);
      if (id !== translationRequest) return true;
      pop.textContent = result?.translation || '';
      pop.classList.remove('loading');
      position(pop, range.getBoundingClientRect());
    } catch (error) {
      if (id !== translationRequest) return true;
      pop.textContent = error?.message || String(error);
      pop.classList.remove('loading');
      position(pop, range.getBoundingClientRect());
    }
    return true;
  }

  function registerToken(token) {
    const id = `study-${++tokenSequence}`;
    registry.set(id, token);
    if (registry.size > 4000) {
      for (const key of [...registry.keys()].slice(0, 1500)) registry.delete(key);
    }
    return id;
  }

  function renderParsedParagraph(payload, paragraphIndex, {backend = 'jiten'} = {}) {
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
      const id = registerToken({...token, card, surface, sentence:text, backend});
      out += `<span class="pudge-study-word state-${esc(state)}" data-pudge-study-token="${id}">${esc(surface)}</span>`;
      pos = end;
    }
    out += esc(text.slice(pos));
    return `<p>${out}</p>`;
  }

  function renderParsedText(payload, {backend = 'jiten'} = {}) {
    if (payload?.settings) applyStudyAppearance(payload.settings);
    const paragraphs = payload?.paragraphs || [];
    return paragraphs.map((_, paragraphIndex) =>
      renderParsedParagraph(payload, paragraphIndex, {backend})
    ).join('');
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
    if (event.target?.id !== 'pudgeStudyDeck' || !activeToken) return;
    rememberStudyDeck(activeToken.backend, event.target.value || '');
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
    const review = event.target.closest?.('[data-pudge-study-review]');
    if (review && activeToken) {
      const token = activeToken.token;
      await apiAction({
        backend: activeToken.backend,
        action: 'review',
        word_id: token.wordId,
        reading_index: token.readingIndex,
        grade: review.dataset.pudgeStudyReview,
        sentence: activeToken.sentence,
      });
      closeStudyCard();
      return;
    }
    const add = event.target.closest?.('[data-pudge-study-add]');
    if (add && activeToken) {
      const token = activeToken.token;
      const deck = document.getElementById('pudgeStudyDeck')?.value || '';
      await apiAction({
        backend: activeToken.backend,
        action: 'add',
        word_id: token.wordId,
        reading_index: token.readingIndex,
        deck_id: deck,
        sentence: activeToken.sentence,
      });
      closeStudyCard();
      return;
    }
    if (event.target.closest?.('[data-ln-token]')) return;
    const pop = document.getElementById('pudgeStudyCard');
    if (pop?.classList.contains('open') && !event.target.closest?.('#pudgeStudyCard')) closeStudyCard();
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

  window.PudgeReadingTools = {
    study: {
      open: openStudyCard,
      openElement: openStudyElement,
      openText: openStudyText,
      close: closeStudyCard,
      renderParsedText,
      renderParsedParagraph,
      inlinePitch: renderInlinePitch,
      inlinePitchOnSurface: renderInlinePitchOnSurface,
      inflectedPitchCard,
      applyAppearance: applyStudyAppearance,
    },
    translation: {
      translateSelection,
      hide: hideTranslation,
    },
    closeAll() {
      closeStudyCard();
      hideTranslation();
    },
  };
})();
