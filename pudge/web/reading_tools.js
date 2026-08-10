'use strict';

(() => {
  const API = () => window.pywebview && window.pywebview.api;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[ch]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';

  const registry = new Map();
  let tokenSequence = 0;
  let activeToken = null;
  let translationRequest = 0;
  let hoverStudyTimer = null;
  let hoverStudyWord = null;

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

  async function apiTranslate(text, context, targetLanguage) {
    if (API()?.translate_text) return API().translate_text(text, context, targetLanguage);
    return API().light_novel_translate(text, context, targetLanguage);
  }

  async function openStudyCard({token, target, backend = 'jiten', sentence = ''}) {
    if (!token || !target) return;
    const {card: pop} = ensureUi();
    const card = token.card || {};
    activeToken = {
      token,
      backend: String(backend || 'jiten'),
      sentence: String(sentence || token.sentence || ''),
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
      <div class="pudge-study-meanings">
        ${meanings.length
          ? meanings.slice(0, 8).map(x => `<div>${esc(typeof x === 'string' ? x : JSON.stringify(x))}</div>`).join('')
          : `<div class="pudge-study-subtle">${ru() ? 'Нет значений' : 'No meanings returned'}</div>`}
      </div>
      <select id="pudgeStudyDeck"><option value="">${ru() ? 'Колода…' : 'Study deck…'}</option></select>
      <div class="pudge-study-actions">
        <button data-pudge-study-review="again">${ru() ? 'Снова' : 'Again'}</button>
        <button data-pudge-study-review="hard">${ru() ? 'Трудно' : 'Hard'}</button>
        <button data-pudge-study-review="good">${ru() ? 'Хорошо' : 'Good'}</button>
        <button data-pudge-study-review="easy">${ru() ? 'Легко' : 'Easy'}</button>
        <button data-pudge-study-add>${ru() ? 'Добавить' : 'Add'}</button>
      </div>`;
    pop.classList.add('open');
    position(pop, target.getBoundingClientRect());
    try {
      const decks = await apiDecks(activeToken.backend);
      if (activeToken?.token !== token) return;
      const select = document.getElementById('pudgeStudyDeck');
      if (select) {
        select.innerHTML = `<option value="">${ru() ? 'Колода…' : 'Study deck…'}</option>` +
          (decks || []).map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
      }
      position(pop, target.getBoundingClientRect());
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
      const result = await apiTranslate(text, context, language);
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


  document.addEventListener('pointerover', event => {
    const word = event.target.closest?.('[data-pudge-study-hover="1"] [data-pudge-study-token]');
    if (!word || word === hoverStudyWord) return;
    if (hoverStudyTimer) clearTimeout(hoverStudyTimer);
    hoverStudyWord = word;
    hoverStudyTimer = setTimeout(() => {
      hoverStudyTimer = null;
      if (hoverStudyWord !== word || !word.isConnected) return;
      const token = registry.get(word.dataset.pudgeStudyToken);
      if (token) void openStudyCard({token,target:word,backend:token.backend||'jiten',sentence:token.sentence||''});
    }, 260);
  }, true);

  document.addEventListener('pointerout', event => {
    const word = event.target.closest?.('[data-pudge-study-hover="1"] [data-pudge-study-token]');
    if (!word || word.contains(event.relatedTarget)) return;
    if (hoverStudyWord === word) hoverStudyWord = null;
    if (hoverStudyTimer) { clearTimeout(hoverStudyTimer); hoverStudyTimer = null; }
  }, true);

  document.addEventListener('click', async event => {
    const close = event.target.closest?.('[data-pudge-study-close]');
    if (close) {
      closeStudyCard();
      return;
    }
    const word = event.target.closest?.('[data-pudge-study-token]');
    if (word) {
      const token = registry.get(word.dataset.pudgeStudyToken);
      if (token) {
        const selection = window.getSelection();
        if (!selection || selection.isCollapsed) {
          await openStudyCard({
            token,
            target: word,
            backend: token.backend || 'jiten',
            sentence: token.sentence || '',
          });
        }
      }
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
      close: closeStudyCard,
      renderParsedText,
      renderParsedParagraph,
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
