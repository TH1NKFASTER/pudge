'use strict';

(() => {
  let overlay = null;
  let generation = 0;
  let launch = null;
  let busy = false;
  let saving = false;
  let authorizing = false;
  let revealed = false;
  let activeCardKey = '';
  let cardQueue = [];
  let currentStatus = null;
  // Jiten action set of the active review mode (native 4 / binary 2). The
  // server sends it with every gate status; the fallback covers old payloads.
  let gateActions = null;
  let optimisticReviewed = 0;
  let optimisticUndoCount = 0;
  let undoing = false;
  let reviewHistory = [];
  let authorizedCardKeys = new Set();
  let lastGradeStateSignature = '';
  let prefetchTimer = null;
  let warmSyncTimer = null;
  const PREFETCH_INTERVAL_MS = 60_000;
  const WARM_SYNC_INTERVAL_MS = 250;
  const WARM_SYNC_MAX_MS = 15_000;

  const uiRef = () => typeof ui !== 'undefined' ? ui : window.ui;
  const ru = () => document.documentElement.lang === 'ru' || uiRef()?.lang === 'ru';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[ch]));

  function uiLog(event, details = {}) {
    try {
      const fn = window.pywebview?.api?.review_gate_ui_event;
      if (typeof fn !== 'function') return;
      void Promise.resolve(fn({event:String(event || ''), ...details})).catch(() => {});
    } catch (_) {}
  }

  function cardKeyOf(card) {
    return card ? `${card.wordId ?? ''}:${card.readingIndex ?? ''}` : '';
  }

  function ensureUi() {
    if (overlay?.isConnected) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'pudgeReviewGate';
    overlay.className = 'pudge-review-gate';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML = '<div class="pudge-review-gate-card"></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', event => {
      const jiten = event.target.closest?.('[data-review-gate-jiten]');
      if (jiten) {
        event.preventDefault();
        void window.PudgeJitenWords?.openInJiten?.({wordId:jiten.dataset.wordId, readingIndex:jiten.dataset.readingIndex})
          .catch(error => window.toast?.(error?.message || String(error)));
        return;
      }
      if (event.target === overlay || event.target.closest?.('[data-review-gate-close]')) {
        event.preventDefault();
        event.stopPropagation();
        close();
        return;
      }
      if (event.target.closest?.('[data-review-gate-continue]') && currentStatus?.checked && currentStatus?.reason === 'no_due_cards' && currentStatus?.granted) {
        void finishAndLaunch();
        return;
      }
      if (event.target.closest?.('[data-review-gate-show-answer]')) {
        showAnswer();
        return;
      }
      if (event.target.closest?.('[data-review-gate-undo]')) {
        void undoLastReview();
        return;
      }
      const grade = event.target.closest?.('[data-review-gate-grade]');
      if (grade) void submitGrade(String(grade.dataset.reviewGateGrade || ''));
      const retry = event.target.closest?.('[data-review-gate-retry]');
      if (retry) void loadBatch();
    });
    return overlay;
  }

  function close() {
    const onCancel = launch?.onCancel;
    generation += 1;
    gateActions = null;
    busy = false;
    saving = false;
    authorizing = false;
    launch = null;
    revealed = false;
    activeCardKey = '';
    cardQueue = [];
    currentStatus = null;
    optimisticReviewed = 0;
    optimisticUndoCount = 0;
    undoing = false;
    reviewHistory = [];
    authorizedCardKeys.clear();
    lastGradeStateSignature = '';
    overlay?.classList.remove('open', 'pudge-review-gate-busy');
    onCancel?.();
  }

  function closeIfOpen() {
    if (!overlay?.classList.contains('open')) return false;
    close();
    return true;
  }

  function showAnswer() {
    if (!overlay?.classList.contains('open') || revealed || !activeCardKey) return false;
    revealed = true;
    render({}, {replaceCards:false});
    const root = overlay.querySelector('.pudge-review-gate-card');
    const defaultAction = activeGateActions().actions.find(action => action.wire === 'good');
    if (defaultAction) root?.querySelector(`[data-review-gate-grade="${defaultAction.id}"]`)?.focus?.({preventScroll:true});
    uiLog('show_answer', {card:activeCardKey, authorized:authorizedCardKeys.has(activeCardKey), authorizing, saving, queue:cardQueue.length});
    return true;
  }

  // Escape always closes/cancels the review gate. Revealing the answer is a
  // recall action and belongs to Space / the explicit Show answer button.
  function handleEscape() {
    if (!overlay?.classList.contains('open')) return false;
    close();
    return true;
  }

  function handleKeydown(event) {
    if (!overlay?.classList.contains('open')) return false;
    if (event?.isComposing || event?.repeat) return false;
    if (event?.target?.closest?.('input,textarea,select,[contenteditable="true"]')) return false;

    const key = String(event?.key || '');
    const code = String(event?.code || '');
    const undoChord = event?.metaKey && !event?.ctrlKey && !event?.altKey && !event?.shiftKey
      && key.toLowerCase() === 'z';
    if (undoChord && reviewHistory.length && !undoing) {
      event.preventDefault?.();
      event.stopPropagation?.();
      event.stopImmediatePropagation?.();
      void undoLastReview();
      return true;
    }

    if (!activeCardKey) return false;
    if (!revealed) {
      if (event?.metaKey || event?.ctrlKey || event?.altKey || event?.shiftKey) return false;
      if (event?.code !== 'Space' && event?.key !== ' ') return false;
      event.preventDefault?.();
      event.stopPropagation?.();
      event.stopImmediatePropagation?.();
      showAnswer();
      return true;
    }

    // A grade shortcut only selects its button; Space confirms (unchanged).
    const action = globalThis.PudgeReviewActions?.matchAction?.(event, activeGateActions());
    if (action) {
      const button = overlay.querySelector(`[data-review-gate-grade="${action.id}"]`);
      if (!button || button.disabled) return false;
      event.preventDefault?.();
      event.stopPropagation?.();
      event.stopImmediatePropagation?.();
      button.focus?.({preventScroll:true});
      return true;
    }

    if (event?.metaKey || event?.ctrlKey || event?.altKey || event?.shiftKey) return false;
    if (code === 'Space' || key === ' ') {
      const focused = document.activeElement?.closest?.('[data-review-gate-grade]');
      if (!focused || !overlay.contains(focused) || focused.disabled) return false;
      event.preventDefault?.();
      event.stopPropagation?.();
      event.stopImmediatePropagation?.();
      focused.click();
      return true;
    }
    return false;
  }

  function displayedCompleted(status) {
    const required = Math.max(1, Number(status?.required || 1));
    const confirmed = Math.max(0, Number(status?.completed || 0));
    return Math.max(0, Math.min(required, confirmed + Math.max(0, Number(optimisticReviewed || 0)) - Math.max(0, Number(optimisticUndoCount || 0))));
  }

  function progressText(status) {
    if (status?.checked && status?.reason === 'no_due_cards') return ru() ? 'Карточки проверены' : 'Cards checked';
    const completed = displayedCompleted(status);
    const required = Math.max(1, Number(status?.required || 1));
    return ru()
      ? `Повторено ${completed} из ${required}`
      : `Reviewed ${completed} of ${required}`;
  }

  function selectedReading(card) {
    const rows = Array.isArray(card?.readings) ? card.readings : [];
    const wanted = Number(card?.readingIndex ?? -1);
    return rows.find(row => Number(row?.readingIndex ?? -2) === wanted) || rows[0] || null;
  }

  function readingText(card) {
    if (window.PudgeJitenWords) return window.PudgeJitenWords.readingsLine(card);
    const rows = Array.isArray(card?.readings) ? card.readings : [];
    return [...new Set(rows.map(row => String(row?.text || '').trim()).filter(Boolean))].join(' ・ ');
  }

  function confusableReadings(card) {
    return (Array.isArray(card?.confusableReadings) ? card.confusableReadings : [])
      .map(value => String(value || '').trim())
      .filter(Boolean)
      .filter((value, index, values) => values.indexOf(value) === index)
      .slice(0, 5);
  }

  function rubyHtml(value) {
    const text = String(value || '');
    if (!text.includes('[')) return esc(text);
    let out = '';
    let pending = '';
    for (let i = 0; i < text.length; i += 1) {
      if (text[i] !== '[') {
        pending += text[i];
        continue;
      }
      const close = text.indexOf(']', i + 1);
      if (close < 0) {
        pending += text.slice(i);
        break;
      }
      const reading = text.slice(i + 1, close);
      // Jiten bracket-ruby can annotate Arabic/full-width digits too (e.g.
      // 2[に]時[じ]). Treat a trailing digit run as a ruby base so the raw
      // brackets never leak into the rendered answer. Keep digits and kanji as
      // separate alternatives so 第2[に] annotates only 2, not 第2.
      const match = pending.match(/([0-9０-９]+|[\u3400-\u9fff々]+)$/u);
      if (!match || !reading) {
        pending += text.slice(i, close + 1);
        i = close;
        continue;
      }
      const base = match[0];
      const prefix = pending.slice(0, pending.length - base.length);
      out += esc(prefix) + `<ruby>${esc(base)}<rt>${esc(reading)}</rt></ruby>`;
      pending = '';
      i = close;
    }
    return out + esc(pending);
  }

  function meaningRows(card) {
    const definitions = Array.isArray(card?.definitions) ? card.definitions : [];
    const values = [];
    for (const definition of definitions) {
      for (const meaning of Array.isArray(definition?.meanings) ? definition.meanings : []) {
        const text = String(meaning || '').trim();
        if (text && !values.includes(text)) values.push(text);
        if (values.length >= 8) return values;
      }
    }
    return values;
  }

  function secondsText(raw) {
    const seconds = Number(raw || 0);
    if (!Number.isFinite(seconds) || seconds <= 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
    return `${Math.round(seconds / 86400)}d`;
  }

  function activeGateActions() {
    if (gateActions?.actions?.length) return gateActions;
    const api = globalThis.PudgeReviewActions;
    return api?.actionSet ? api.actionSet('jiten') : {provider:'jiten', mode:'native', actions:[]};
  }

  function actionLabel(action) {
    const api = globalThis.PudgeReviewActions;
    return api?.label ? api.label(action) : String(action?.id || '');
  }

  function gradeLabel(card, action) {
    const preview = card?.intervalPreview || {};
    // Interval previews are keyed by the provider-native (wire) grade.
    const interval = secondsText(preview?.[`${action.wire}Seconds`]);
    return `${esc(actionLabel(action))}${interval ? `<small>${esc(interval)}</small>` : ''}`;
  }

  function gradeButtonsHtml(card) {
    const set = activeGateActions();
    return `<div class="pudge-review-gate-grades" data-count="${set.actions.length}">
        ${set.actions.map(action => `<button class="${esc(action.tone)}" data-review-gate-grade="${esc(action.id)}"${action.shortcut ? ` title="${esc(action.shortcut)}"` : ''}>${gradeLabel(card, action)}</button>`).join('')}
      </div>`;
  }

  function backDetails(card) {
    const meanings = meaningRows(card);
    const primary = selectedReading(card);
    const ruby = String(primary?.rubyText || '').trim();
    const plainReading = readingText(card);
    const frontWord = String(card?.wordTextPlain || card?.wordText || primary?.text || '').trim();
    const rubySource = ruby || String(primary?.text || frontWord);
    const pitch = Array.isArray(card?.pitchAccents) ? card.pitchAccents.filter(Number.isFinite) : [];
    const pitchReading = String(primary?.text || '').trim();
    const pitchHtml = pitch.length && pitchReading
      ? (window.PudgeReadingTools?.study?.inlinePitch?.({reading:pitchReading, pitchAccents:pitch}) || '')
      : '';
    const meta = [
      Number(card?.frequencyRank || 0) > 0 ? `#${Number(card.frequencyRank)}` : '',
      ...(Array.isArray(card?.partsOfSpeech) ? card.partsOfSpeech.slice(0, 4) : []),
      Number(card?.lapses || 0) > 0 ? `${ru() ? 'Ошибок' : 'Lapses'}: ${Number(card.lapses)}` : '',
      card?.isLeech ? (ru() ? 'Leech' : 'Leech') : '',
    ].filter(Boolean);
    const source = String(card?.sourceDeckName || '').trim();
    const occurrences = (Array.isArray(card?.deckOccurrences) ? card.deckOccurrences : [])
      .slice(0, 3)
      .map(row => {
        const title = String(row?.originalTitle || row?.englishTitle || row?.romajiTitle || '').trim();
        const count = Number(row?.occurrences || 0);
        return title ? `${title}${count > 0 ? ` ×${count}` : ''}` : '';
      })
      .filter(Boolean);
    const example = String(card?.exampleSentence?.text || card?.pudgeContextSentence || '').replaceAll('**', '').trim();
    const contextImage = String(card?.pudgeContextImage || '').trim();

    return `
      <div class="pudge-review-gate-answer-word">${jitenWordHtml(card, rubyHtml(rubySource))}</div>
      ${plainReading ? `<div class="pudge-review-gate-reading">${esc(plainReading)}</div>` : ''}
      ${pitchHtml ? `<div class="pudge-review-gate-pitch">${pitchHtml}</div>` : ''}
      ${contextImage ? `<div class="pudge-review-gate-image"><img src="${esc(contextImage)}" alt="" loading="lazy"></div>` : ''}
      ${meta.length ? `<div class="pudge-review-gate-meta">${meta.map(value => `<span>${esc(value)}</span>`).join('')}</div>` : ''}
      ${meanings.length ? `<ol class="pudge-review-gate-meanings">${meanings.map(value => `<li>${esc(value)}</li>`).join('')}</ol>` : ''}
      ${example ? `<div class="pudge-review-gate-example"><small>${ru() ? 'Пример' : 'Example'}</small>${esc(example)}</div>` : ''}
      ${(source || occurrences.length) ? `<div class="pudge-review-gate-source"><small>${ru() ? 'Источник' : 'Source'}</small>${esc(source || occurrences.join(' · '))}</div>` : ''}
      ${gradeButtonsHtml(card)}`;
  }

  function jitenWordHtml(card, markup, className = '') {
    const wordId = Number(card?.wordId), readingIndex = Number(card?.readingIndex);
    if (!Number.isSafeInteger(wordId) || wordId <= 0 || !Number.isSafeInteger(readingIndex) || readingIndex < 0) return markup;
    const label = ru() ? 'Открыть слово на Jiten' : 'Open word on Jiten';
    return `<button type="button" class="pudge-review-gate-word-link ${className}" data-review-gate-jiten data-word-id="${wordId}" data-reading-index="${readingIndex}" title="${esc(label)}" aria-label="${esc(label)}">${markup}</button>`;
  }

  function frontWordHtml(card, showReading) {
    const primaryReading = selectedReading(card);
    const frontWord = String(card?.wordTextPlain || card?.wordText || primaryReading?.text || '').trim();
    if (!showReading) return esc(frontWord);
    const primaryText = String(primaryReading?.text || '').trim();
    const frontRuby = String(primaryReading?.rubyText || '').trim()
      || (primaryText.includes('[') ? primaryText : frontWord);
    return rubyHtml(frontRuby);
  }

  function previousReviewHtml() {
    const entry = reviewHistory[reviewHistory.length - 1];
    if (!entry) return '';
    const card = entry.card || {};
    const word = String(card.wordTextPlain || card.wordText || selectedReading(card)?.text || '').trim();
    const grade = String(entry.grade || '').toLowerCase();
    const action = (entry.actions?.actions || activeGateActions().actions).find(row => row.id === grade);
    const tone = String(action?.tone || grade);
    return `
      <div class="pudge-review-gate-previous">
        <span class="pudge-review-gate-previous-label">${ru() ? 'Предыдущее' : 'Previous'}</span>
        <strong>${esc(word)}</strong>
        <span class="pudge-review-gate-previous-grade grade-${esc(tone)}">${esc(action ? actionLabel(action) : grade)}</span>
        <button type="button" data-review-gate-undo ${undoing ? 'disabled' : ''}>${ru() ? 'Отменить' : 'Undo'}</button>
      </div>`;
  }

  function render(status, {replaceCards = true} = {}) {
    const root = ensureUi().querySelector('.pudge-review-gate-card');
    if (replaceCards && Array.isArray(status?.cards)) cardQueue = status.cards.map(card => ({...card}));
    if (status?.review_actions?.actions?.length) gateActions = status.review_actions;
    currentStatus = {...(currentStatus || {}), ...(status || {})};
    const view = currentStatus || {};
    const completed = displayedCompleted(view);
    const required = Math.max(1, Number(view?.required || 1));
    const width = Math.max(0, Math.min(100, completed / required * 100));
    const episode = Number(view?.episode || 0);
    const card = cardQueue[0] || null;
    const cardKey = cardKeyOf(card);
    const cardAuthorized = Boolean(cardKey && authorizedCardKeys.has(cardKey));
    if (cardKey !== activeCardKey) {
      activeCardKey = cardKey;
      revealed = false;
    }
    const unknownCount = Array.isArray(view?.unknown_card_keys) ? view.unknown_card_keys.length : 0;
    let body = '';
    if (card) {
      const frontWord = String(card.wordTextPlain || card.wordText || selectedReading(card)?.text || '').trim();
      const frontMarkup = frontWordHtml(card, revealed);
      const frontConfusable = confusableReadings(card);
      body = `
        <div class="pudge-review-gate-front">
          <div class="pudge-review-gate-word">${jitenWordHtml(card, frontMarkup, 'pudge-review-gate-word-text')}</div>
          ${frontConfusable.length ? `<div class="pudge-review-gate-front-confusable"><small>${ru() ? 'Похожие чтения' : 'Confusable readings'}</small><span>${esc(frontConfusable.join(' ・ '))}</span></div>` : ''}
          <button class="primary pudge-review-gate-show-answer" data-review-gate-show-answer>${ru() ? 'Показать ответ' : 'Show answer'} <kbd>Space</kbd></button>
        </div>
        <div class="pudge-review-gate-back"${revealed ? '' : ' hidden'}>${backDetails(card)}</div>`;
    } else {
      activeCardKey = '';
      revealed = false;
      if (view?.loading || saving) {
        const loadingText = saving
          ? (ru() ? 'Сохраняю предыдущее повторение…' : 'Saving previous review…')
          : (ru() ? 'Загружаю карточку Jiten…' : 'Loading Jiten review card…');
        body = `<div class="pudge-review-gate-loading"><span class="spinner"></span>${esc(loadingText)}</div>`;
      } else {
        const message = String(view?.message || (ru()
          ? 'Нет доступных уже изученных карточек. Новые карточки здесь никогда не создаются.'
          : 'No previously studied cards are available. This gate never creates new cards.'));
        const checkedEmpty = view.checked && view.reason === 'no_due_cards' && view.granted;
        const action = checkedEmpty
          ? `<button class="primary" data-review-gate-continue>${ru() ? 'Продолжить чтение' : 'Continue reading'}</button>`
          : `<button data-review-gate-retry>${ru() ? 'Повторить' : 'Retry'}</button>`;
        body = `<div class="pudge-review-gate-empty">${esc(message)}</div><div class="pudge-review-gate-actions">${action}</div>`;
      }
    }
    const stateNote = undoing && card
      ? `<span class="pudge-review-gate-saving">${esc(ru() ? 'Отменяю предыдущее повторение…' : 'Undoing previous review…')}</span>`
      : (saving && card
        ? `<span class="pudge-review-gate-saving">${esc(ru() ? 'Сохраняю предыдущую…' : 'Saving previous…')}</span>`
        : (card && !cardAuthorized
          ? `<span class="pudge-review-gate-saving">${esc(authorizing ? (ru() ? 'Подтверждаю карточку…' : 'Authorizing card…') : (ru() ? 'Карточка ещё не подтверждена' : 'Card authorization pending'))}</span>`
          : ''));
    // WebKit can retain the focus/hover compositor of a button that is removed
    // by innerHTML. Blur it before replacing the card to avoid transient ghosts.
    const focusedBeforeRender = document.activeElement;
    if (focusedBeforeRender && root.contains(focusedBeforeRender)) {
      focusedBeforeRender.blur?.();
    }
    root.innerHTML = `
      <div class="pudge-review-gate-head">
        <div class="pudge-review-gate-head-copy">
          <div class="pudge-review-gate-title">${launch?.content?(ru()?'Повторение перед чтением':'Review before reading'):(ru() ? `Повторение перед серией${episode ? ` ${episode}` : ''}` : `Review before episode${episode ? ` ${episode}` : ''}`)}</div>

        </div>
        <button class="pudge-review-gate-close" data-review-gate-close aria-label="Close">×</button>
      </div>
      <div class="pudge-review-gate-progress"><span style="width:${width}%"></span></div>
      <div class="pudge-review-gate-status${unknownCount ? ' warning' : ''}">${esc(progressText(view))}${unknownCount ? esc(ru() ? ` • ${unknownCount} неоднозначных попыток не засчитано` : ` • ${unknownCount} ambiguous attempt(s) not counted`) : ''}${stateNote}</div>
      ${previousReviewHtml()}
      ${body}`;
    root.classList.toggle('answer-shown', revealed);
    root.dataset.wordId = card ? String(card.wordId ?? '') : '';
    root.dataset.readingIndex = card ? String(card.readingIndex ?? '') : '';
    if (card) window.PudgeJitenWords?.hydrateReadings?.(card, root, ".pudge-review-gate-back", "pudge-review-gate-reading", () => cardQueue[0]);
    if (card) void window.PudgeJitenWords?.prefetch?.(cardQueue);
    root.querySelectorAll('[data-review-gate-grade]').forEach(button => {
      button.disabled = saving || !cardAuthorized;
      if (undoing) button.disabled = true;
    });
    if (cardKey) {
      const reason = undoing ? 'undoing_previous' : (saving ? 'saving_previous' : (cardAuthorized ? 'ready' : (authorizing ? 'authorizing' : 'not_authorized')));
      const signature = `${cardKey}|${reason}|${revealed ? 1 : 0}`;
      if (signature !== lastGradeStateSignature) {
        lastGradeStateSignature = signature;
        uiLog('grade_state', {card:cardKey, reason, enabled:reason === 'ready', revealed, queue:cardQueue.length});
      }
    }
  }

  async function prefetch(force = false) {
    try {
      if (!window.pywebview?.api?.review_gate_prefetch) return null;
      const snapshot = await window.pywebview.api.review_gate_prefetch(Boolean(force));
      if (snapshot?.reason === 'episode_due_mode') {
        window.__pudgeReviewGateWarmCards = [];
      } else if (Array.isArray(snapshot?.cards) && snapshot.cards.length) {
        window.__pudgeReviewGateWarmCards = snapshot.cards.map(card => ({...card}));
      }
      return snapshot;
    } catch (error) {
      console.debug('review gate prefetch failed', error);
      return null;
    }
  }

  function startPrefetchLoop() {
    if (prefetchTimer) return;
    const deadline = Date.now() + WARM_SYNC_MAX_MS;
    const syncWarm = async () => {
      warmSyncTimer = null;
      const snapshot = await prefetch(false);
      const ready = Array.isArray(snapshot?.cards) && snapshot.cards.length > 0;
      const noTarget = snapshot?.reason === 'no_ready_anime' || Number(snapshot?.target || 0) <= 0;
      if (!ready && !noTarget && Date.now() < deadline) {
        warmSyncTimer = setTimeout(syncWarm, WARM_SYNC_INTERVAL_MS);
      }
    };
    void syncWarm();
    prefetchTimer = setInterval(() => void prefetch(false), PREFETCH_INTERVAL_MS);
  }

  async function finishAndLaunch() {
    const pending = launch;
    generation += 1;
    busy = false;
    saving = false;
    authorizing = false;
    launch = null;
    revealed = false;
    activeCardKey = '';
    cardQueue = [];
    currentStatus = null;
    optimisticReviewed = 0;
    optimisticUndoCount = 0;
    undoing = false;
    reviewHistory = [];
    authorizedCardKeys.clear();
    lastGradeStateSignature = '';
    overlay?.classList.remove('open', 'pudge-review-gate-busy');
    if (!pending) return;
    if(pending.onComplete){pending.onComplete();return;}
    await window.startPlay?.(
      pending.path,
      !!pending.resume,
      !!pending.allowImageSubtitles,
      pending.queueContext || null,
      !!pending.allowUnsyncedSubtitles,
    );
  }

  async function loadBatch() {
    if (!launch || busy) return;
    const myGeneration = generation;
    const startedAt = performance?.now?.() ?? Date.now();
    busy = true;
    authorizing = true;
    // Warm cards remain visible during authorization. The backdrop stays
    // interactive so a loading review can always cancel content entry.
    overlay?.classList.toggle('pudge-review-gate-busy', cardQueue.length === 0);
    render({}, {replaceCards:false});
    uiLog('begin_start', {
      card:cardKeyOf(cardQueue[0]), warm_queue:cardQueue.length,
      completed:Number(currentStatus?.completed || 0), remaining:Number(currentStatus?.remaining || 0),
    });
    try {
      const status = await (launch.content ? window.pywebview.api.content_review_begin(launch.kind,launch.bookId,launch.part) : window.pywebview.api.review_gate_begin(launch.path));
      if (myGeneration !== generation || !launch) return;
      if (!status || (status.granted !== true && status.blocking !== true)) {
        throw new Error(ru() ? 'Ревью ещё не проверено. Повторите запрос.' : 'Review status has not been checked. Retry.');
      }
      if (launch.content && status.checked && status.reason === 'no_due_cards' && status.granted) {
        authorizing = false;
        busy = false;
        overlay?.classList.remove('pudge-review-gate-busy');
        render({...status, loading:false, message:ru() ? 'В этой части нет карточек для повторения.' : 'No cards are due in this part.'}, {replaceCards:true});
        uiLog('begin_done', {granted:true, checked:true, reason:'no_due_cards', awaiting_continue:true,
          duration_ms:Math.round((performance?.now?.() ?? Date.now()) - startedAt)});
        return;
      }
      if (status.granted === true) {
        uiLog('begin_done', {granted:true, reason:String(status.reason || 'granted'), duration_ms:Math.round((performance?.now?.() ?? Date.now()) - startedAt)});
        await finishAndLaunch();
        return;
      }
      if(launch.content)launch.token=status.token;
      const issuedCards = Array.isArray(status?.cards) ? status.cards : [];
      authorizedCardKeys = new Set(issuedCards.map(cardKeyOf).filter(Boolean));
      authorizing = false;
      busy = false;
      overlay?.classList.remove('pudge-review-gate-busy');
      render(status || {}, {replaceCards:true});
      uiLog('begin_done', {
        granted:false, prefetched:Boolean(status?.prefetched), available:Number(status?.available || 0),
        issued:Number(status?.issued || issuedCards.length), authorized:authorizedCardKeys.size,
        candidate_fetch_ms:Number(status?.candidate_fetch_ms || 0),
        duration_ms:Math.round((performance?.now?.() ?? Date.now()) - startedAt),
        card:cardKeyOf(cardQueue[0]),
      });
    } catch (error) {
      if (myGeneration !== generation || !launch) return;
      authorizedCardKeys.clear();
      authorizing = false;
      busy = false;
      overlay?.classList.remove('pudge-review-gate-busy');
      render({required:1, completed:0, cards:[], loading:false, message:error?.message || String(error)}, {replaceCards:true});
      uiLog('begin_error', {duration_ms:Math.round((performance?.now?.() ?? Date.now()) - startedAt), error:String(error?.message || error || '')});
    } finally {
      if (myGeneration === generation) {
        const needsRender = busy || authorizing;
        busy = false;
        authorizing = false;
        overlay?.classList.remove('pudge-review-gate-busy');
        if (needsRender && launch) render({}, {replaceCards:false});
      }
    }
  }

  async function submitGrade(grade) {
    if (!activeGateActions().actions.some(action => action.id === String(grade || ''))) {
      uiLog('grade_blocked', {grade:String(grade || ''), reason:'inactive_action'});
      return;
    }
    const root = overlay?.querySelector('.pudge-review-gate-card');
    const wordId = Number(root?.dataset.wordId || 0);
    const readingIndex = Number(root?.dataset.readingIndex || 0);
    const submittedKey = `${wordId}:${readingIndex}`;
    const blockedReason = !launch ? 'no_launch' : (!revealed ? 'answer_hidden' : (saving ? 'saving_previous' : (!authorizedCardKeys.has(submittedKey) ? (authorizing ? 'authorizing' : 'not_authorized') : '')));
    if (blockedReason) {
      uiLog('grade_blocked', {card:submittedKey, grade:String(grade || ''), reason:blockedReason, busy, authorizing, saving, revealed, queue:cardQueue.length});
      return;
    }
    if (!wordId || !Number.isFinite(readingIndex)) {
      uiLog('grade_blocked', {card:submittedKey, grade:String(grade || ''), reason:'invalid_card_identity'});
      return;
    }
    const myGeneration = generation;
    const submittedCard = cardQueue[0] || null;
    if (!submittedCard || cardKeyOf(submittedCard) !== submittedKey) {
      uiLog('grade_blocked', {card:submittedKey, grade:String(grade || ''), reason:'queue_mismatch', queue_head:cardKeyOf(submittedCard)});
      return;
    }

    const focusedGrade = document.activeElement?.closest?.('[data-review-gate-grade]');
    if (focusedGrade && overlay?.contains(focusedGrade)) focusedGrade.blur?.();

    const attemptId = globalThis.crypto?.randomUUID?.() || `pudge-gate-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const historyEntry = {
      card: {...submittedCard},
      cardKey: submittedKey,
      grade: String(grade || ''),
      actions: activeGateActions(),
      attemptId,
      submitPromise: null,
      result: null,
      settled: false,
      undone: false,
    };

    // Advance visually before network IO. The remaining cards were already
    // prefetched and authorized by review_gate_begin(); the just-submitted card
    // is still counted only after the Jiten mutation is confirmed.
    cardQueue.shift();
    authorizedCardKeys.delete(submittedKey);
    saving = true;
    optimisticReviewed += 1;
    reviewHistory.push(historyEntry);
    revealed = false;
    activeCardKey = '';
    render({loading:false}, {replaceCards:false});

    const submitStartedAt = performance?.now?.() ?? Date.now();
    uiLog('review_optimistic', {
      card:submittedKey, grade:String(grade || 'good'), queue_remaining:cardQueue.length, attempt_id:attemptId,
      displayed_completed:displayedCompleted(currentStatus || {}), confirmed_completed:Number(currentStatus?.completed || 0), optimistic_pending:optimisticReviewed,
    });
    uiLog('review_submit', {card:submittedKey, grade:String(grade || 'good'), queue_remaining:cardQueue.length, attempt_id:attemptId});
    try {
      historyEntry.submitPromise = new Promise(resolve => { historyEntry.resolveSubmit = resolve; });
      const result = await (launch.content?window.pywebview.api.content_review_review:window.pywebview.api.review_gate_review)(
        launch.content?launch.token:launch.path, wordId, readingIndex, String(grade || 'good'), attemptId,
      );
      historyEntry.resolveSubmit?.(result);
      historyEntry.resolveSubmit = null;
      historyEntry.result = result;
      historyEntry.settled = true;
      if (myGeneration !== generation || !launch) return;
      optimisticReviewed = Math.max(0, optimisticReviewed - 1);
      currentStatus = {...(currentStatus || {}), ...(result || {})};
      uiLog('review_result', {card:submittedKey, grade:String(grade || 'good'), outcome:String(result?.outcome || ''), completed:Number(result?.completed || 0), remaining:Number(result?.remaining || 0), granted:Boolean(result?.granted), duration_ms:Math.round((performance?.now?.() ?? Date.now()) - submitStartedAt)});
      if (result?.outcome === 'unknown') {
        uiLog('review_optimistic_rollback', {card:submittedKey, reason:'outcome_unknown', displayed_completed:displayedCompleted(currentStatus), confirmed_completed:Number(currentStatus?.completed || 0), optimistic_pending:optimisticReviewed});
        window.toast?.(result?.message || (ru() ? 'Результат неизвестен; карточка не засчитана и не будет автоматически повторена.' : 'Review outcome is unknown; it was not counted and will not be retried automatically.'));
      } else {
        uiLog('review_optimistic_confirm', {card:submittedKey, displayed_completed:displayedCompleted(currentStatus), confirmed_completed:Number(currentStatus?.completed || 0), optimistic_pending:optimisticReviewed});
        window.dispatchEvent?.(new CustomEvent('pudge-review-completed',{detail:{backend:'jiten',wordId,readingIndex}}));
      }
      if ((result?.granted || !result?.blocking) && !historyEntry.undone) {
        await finishAndLaunch();
        return;
      }
    } catch (error) {
      historyEntry.resolveSubmit?.({
        ok:false,
        outcome:'error',
        message:String(error?.message || error || ''),
      });
      historyEntry.resolveSubmit = null;
      historyEntry.settled = true;
      historyEntry.error = error;
      if (!historyEntry.undone) {
        const historyIndex = reviewHistory.lastIndexOf(historyEntry);
        if (historyIndex >= 0) reviewHistory.splice(historyIndex, 1);
      }
      if (myGeneration !== generation || !launch) return;
      optimisticReviewed = Math.max(0, optimisticReviewed - 1);
      uiLog('review_optimistic_rollback', {card:submittedKey, reason:'exception', displayed_completed:displayedCompleted(currentStatus || {}), confirmed_completed:Number(currentStatus?.completed || 0), optimistic_pending:optimisticReviewed, error:String(error?.message || error || '')});
      uiLog('review_error', {card:submittedKey, grade:String(grade || 'good'), duration_ms:Math.round((performance?.now?.() ?? Date.now()) - submitStartedAt), error:String(error?.message || error || '')});
      window.toast?.(error?.message || String(error));
    } finally {
      if (myGeneration === generation) {
        saving = false;
        render({}, {replaceCards:false});
      }
    }
    if (myGeneration === generation && launch && cardQueue.length === 0) await loadBatch();
  }

  async function undoLastReview() {
    if (!overlay?.classList.contains('open') || !launch || undoing || !reviewHistory.length) return false;

    const entry = reviewHistory.pop();
    entry.undone = true;
    undoing = true;
    optimisticUndoCount += 1;

    cardQueue.unshift({...entry.card});
    authorizedCardKeys.delete(entry.cardKey);
    revealed = false;
    activeCardKey = '';
    render({loading:false}, {replaceCards:false});
    uiLog('review_undo_optimistic', {card:entry.cardKey, grade:entry.grade, history_remaining:reviewHistory.length});

    try {
      const submitted = entry.submitPromise ? await entry.submitPromise : entry.result;
      const outcome = String(submitted?.outcome || '');
      const providerHasReview = Boolean(
        submitted?.ok && (outcome === 'confirmed' || outcome === 'already_counted')
      );
      if (!providerHasReview) {
        throw new Error(
          submitted?.message
          || (ru() ? 'Предыдущее повторение не было подтверждено сервером.' : 'The previous review was not confirmed by the server.')
        );
      }

      const result = await (launch.content?window.pywebview.api.content_review_undo:window.pywebview.api.review_gate_undo)(
        launch.content?launch.token:launch.path,
        Number(entry.card?.wordId || 0),
        Number(entry.card?.readingIndex || 0),
      );
      if (!result?.ok || String(result?.outcome || '') !== 'undone') {
        throw new Error(result?.message || `Undo failed: ${String(result?.outcome || 'unknown')}`);
      }

      currentStatus = {...(currentStatus || {}), ...(result || {})};
      optimisticUndoCount = Math.max(0, optimisticUndoCount - 1);
      authorizedCardKeys.add(entry.cardKey);
      undoing = false;
      render({}, {replaceCards:false});
      uiLog('review_undo_confirm', {card:entry.cardKey, completed:Number(result?.completed || 0), remaining:Number(result?.remaining || 0)});
      return true;
    } catch (error) {
      optimisticUndoCount = Math.max(0, optimisticUndoCount - 1);
      undoing = false;
      authorizedCardKeys.delete(entry.cardKey);
      if (cardKeyOf(cardQueue[0]) === entry.cardKey) cardQueue.shift();
      render({}, {replaceCards:false});
      uiLog('review_undo_error', {card:entry.cardKey, error:String(error?.message || error || '')});
      window.toast?.(
        (ru() ? 'Не удалось отменить предыдущее повторение: ' : 'Could not undo previous review: ')
        + String(error?.message || error || '')
      );
      if (launch && !busy && cardQueue.length === 0) void loadBatch();
      return false;
    }
  }

  async function open(nextLaunch, initialStatus = null) {
    generation += 1;
    launch = {...(nextLaunch || {})};
    busy = false;
    saving = false;
    authorizing = false;
    revealed = false;
    activeCardKey = '';
    cardQueue = [];
    authorizedCardKeys.clear();
    lastGradeStateSignature = '';
    currentStatus = initialStatus ? {...initialStatus} : null;
    optimisticReviewed = 0;
    optimisticUndoCount = 0;
    undoing = false;
    reviewHistory = [];
    const node = ensureUi();
    node.classList.add('open');
    const warm = launch.content || initialStatus?.all_due_episode_words
      ? []
      : (Array.isArray(window.__pudgeReviewGateWarmCards) ? window.__pudgeReviewGateWarmCards : []);
    const excludedWarm = new Set([
      ...(Array.isArray(initialStatus?.confirmed_card_keys) ? initialStatus.confirmed_card_keys : []),
      ...(Array.isArray(initialStatus?.unknown_card_keys) ? initialStatus.unknown_card_keys : []),
    ]);
    const warmEligible = warm
      .filter(card => !excludedWarm.has(`${card?.wordId ?? ''}:${card?.readingIndex ?? ''}`))
      .slice(0, Math.max(1, Number(initialStatus?.remaining || initialStatus?.required || 1)));
    if (initialStatus?.blocking && warmEligible.length) {
      // Zero-latency paint from the minute-refresh snapshot. Use the whole local
      // episode quota, not only card #1. review_gate_begin() authorizes the exact
      // queue in parallel; grades stay disabled during that tiny round-trip.
      render({...initialStatus, cards:warmEligible, loading:false}, {replaceCards:true});
    } else if (initialStatus) {
      render(initialStatus?.blocking ? {...initialStatus, loading:true} : initialStatus, {replaceCards:true});
    }
    uiLog('open', {warm_total:warm.length, warm_eligible:warmEligible.length, blocking:Boolean(initialStatus?.blocking), completed:Number(initialStatus?.completed || 0), remaining:Number(initialStatus?.remaining || initialStatus?.required || 0), card:cardKeyOf(cardQueue[0])});
    await loadBatch();
  }

  window.PudgeReviewGate = {
    open,
    require:async(kind,bookId,part)=>{
      const settings=uiRef()?.state?.settings;
      if(settings?.[`review_gate_${kind}_enabled`] === false)return true;
      if(overlay?.classList.contains('open'))return false;
      return await new Promise(resolve=>{
        void open({content:true,kind,bookId:Number(bookId),part:Number(part),onComplete:()=>resolve(true),onCancel:()=>resolve(false)},{blocking:true,required:1,all_due_episode_words:true})
          .catch(error=>{window.toast?.(error?.message||String(error));close();resolve(false);});
      });
    },
    close,
    closeIfOpen,
    handleEscape,
    handleKeydown,
    showAnswer,
    undoLastReview,
    prefetch,
    startPrefetchLoop,
    isOpen:() => Boolean(overlay?.classList.contains('open')),
    isAnswerShown:() => revealed,
  };

  window.addEventListener('pywebviewready', startPrefetchLoop, {once:true});
  window.addEventListener('focus', () => void prefetch(false));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) void prefetch(false);
  });
})();
