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
  let optimisticReviewed = 0;
  let authorizedCardKeys = new Set();
  let lastGradeStateSignature = '';
  let prefetchTimer = null;
  let warmSyncTimer = null;
  const PREFETCH_INTERVAL_MS = 60_000;
  const WARM_SYNC_INTERVAL_MS = 250;
  const WARM_SYNC_MAX_MS = 15_000;

  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
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
      if (event.target === overlay || event.target.closest?.('[data-review-gate-close]')) {
        close();
        return;
      }
      if (event.target.closest?.('[data-review-gate-show-answer]')) {
        showAnswer();
        return;
      }
      const grade = event.target.closest?.('[data-review-gate-grade]');
      if (grade) void submitGrade(String(grade.dataset.reviewGateGrade || 'good'));
      const retry = event.target.closest?.('[data-review-gate-retry]');
      if (retry) void loadBatch();
    });
    return overlay;
  }

  function close() {
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
    authorizedCardKeys.clear();
    lastGradeStateSignature = '';
    overlay?.classList.remove('open', 'pudge-review-gate-busy');
  }

  function closeIfOpen() {
    if (!overlay?.classList.contains('open')) return false;
    close();
    return true;
  }

  function showAnswer() {
    if (!overlay?.classList.contains('open') || revealed || !activeCardKey) return false;
    revealed = true;
    const root = overlay.querySelector('.pudge-review-gate-card');
    root?.classList.add('answer-shown');
    root?.querySelector('[data-review-gate-show-answer]')?.setAttribute('hidden', '');
    root?.querySelector('.pudge-review-gate-back')?.removeAttribute('hidden');
    root?.querySelector('[data-review-gate-grade="good"]')?.focus?.({preventScroll:true});
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
    if (!overlay?.classList.contains('open') || revealed || !activeCardKey) return false;
    if (event?.isComposing || event?.repeat || event?.metaKey || event?.ctrlKey || event?.altKey || event?.shiftKey) return false;
    if (event?.code !== 'Space' && event?.key !== ' ') return false;
    event.preventDefault?.();
    event.stopPropagation?.();
    event.stopImmediatePropagation?.();
    showAnswer();
    return true;
  }

  function displayedCompleted(status) {
    const required = Math.max(1, Number(status?.required || 1));
    const confirmed = Math.max(0, Number(status?.completed || 0));
    return Math.min(required, confirmed + Math.max(0, Number(optimisticReviewed || 0)));
  }

  function progressText(status) {
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
    const rows = Array.isArray(card?.readings) ? card.readings : [];
    return rows.map(row => String(row?.text || '').trim()).filter(Boolean).slice(0, 3).join(' ・ ');
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

  function gradeLabel(card, grade, en, ruText) {
    const preview = card?.intervalPreview || {};
    const field = `${grade}Seconds`;
    const interval = secondsText(preview?.[field]);
    return `${ru() ? ruText : en}${interval ? `<small>${esc(interval)}</small>` : ''}`;
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
    const example = String(card?.exampleSentence?.text || '').replaceAll('**', '').trim();

    return `
      <div class="pudge-review-gate-answer-word">${rubyHtml(rubySource)}</div>
      ${plainReading ? `<div class="pudge-review-gate-reading">${esc(plainReading)}</div>` : ''}
      ${pitchHtml ? `<div class="pudge-review-gate-pitch">${pitchHtml}</div>` : ''}
      ${meta.length ? `<div class="pudge-review-gate-meta">${meta.map(value => `<span>${esc(value)}</span>`).join('')}</div>` : ''}
      ${meanings.length ? `<ol class="pudge-review-gate-meanings">${meanings.map(value => `<li>${esc(value)}</li>`).join('')}</ol>` : ''}
      ${example ? `<div class="pudge-review-gate-example"><small>${ru() ? 'Пример' : 'Example'}</small>${esc(example)}</div>` : ''}
      ${(source || occurrences.length) ? `<div class="pudge-review-gate-source"><small>${ru() ? 'Источник' : 'Source'}</small>${esc(source || occurrences.join(' · '))}</div>` : ''}
      <div class="pudge-review-gate-grades">
        <button class="again" data-review-gate-grade="again">${gradeLabel(card, 'again', 'Again', 'Снова')}</button>
        <button class="hard" data-review-gate-grade="hard">${gradeLabel(card, 'hard', 'Hard', 'Трудно')}</button>
        <button class="good" data-review-gate-grade="good">${gradeLabel(card, 'good', 'Good', 'Хорошо')}</button>
        <button class="easy" data-review-gate-grade="easy">${gradeLabel(card, 'easy', 'Easy', 'Легко')}</button>
      </div>`;
  }

  function render(status, {replaceCards = true} = {}) {
    const root = ensureUi().querySelector('.pudge-review-gate-card');
    if (replaceCards && Array.isArray(status?.cards)) cardQueue = status.cards.map(card => ({...card}));
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
      const frontConfusable = confusableReadings(card);
      body = `
        <div class="pudge-review-gate-front">
          <div class="pudge-review-gate-word">${esc(frontWord)}</div>
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
        body = `<div class="pudge-review-gate-empty">${esc(message)}</div><div class="pudge-review-gate-actions"><button data-review-gate-retry>${ru() ? 'Повторить' : 'Retry'}</button></div>`;
      }
    }
    const stateNote = saving && card
      ? `<span class="pudge-review-gate-saving">${esc(ru() ? 'Сохраняю предыдущую…' : 'Saving previous…')}</span>`
      : (card && !cardAuthorized
        ? `<span class="pudge-review-gate-saving">${esc(authorizing ? (ru() ? 'Подтверждаю карточку…' : 'Authorizing card…') : (ru() ? 'Карточка ещё не подтверждена' : 'Card authorization pending'))}</span>`
        : '');
    root.innerHTML = `
      <div class="pudge-review-gate-head">
        <div class="pudge-review-gate-head-copy">
          <div class="pudge-review-gate-title">${ru() ? `Повторение перед серией${episode ? ` ${episode}` : ''}` : `Review before episode${episode ? ` ${episode}` : ''}`}</div>
          <div class="pudge-review-gate-subtitle">${ru() ? 'Только уже изученные карточки Jiten; новых слов не будет.' : 'Previously studied Jiten cards only; no new words are introduced.'}</div>
        </div>
        <button class="pudge-review-gate-close" data-review-gate-close aria-label="Close">×</button>
      </div>
      <div class="pudge-review-gate-progress"><span style="width:${width}%"></span></div>
      <div class="pudge-review-gate-status${unknownCount ? ' warning' : ''}">${esc(progressText(view))}${unknownCount ? esc(ru() ? ` • ${unknownCount} неоднозначных попыток не засчитано` : ` • ${unknownCount} ambiguous attempt(s) not counted`) : ''}${stateNote}</div>
      ${body}`;
    root.classList.toggle('answer-shown', revealed);
    root.dataset.wordId = card ? String(card.wordId ?? '') : '';
    root.dataset.readingIndex = card ? String(card.readingIndex ?? '') : '';
    root.querySelectorAll('[data-review-gate-grade]').forEach(button => { button.disabled = saving || !cardAuthorized; });
    if (cardKey) {
      const reason = saving ? 'saving_previous' : (cardAuthorized ? 'ready' : (authorizing ? 'authorizing' : 'not_authorized'));
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
      if (Array.isArray(snapshot?.cards) && snapshot.cards.length) {
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
    authorizedCardKeys.clear();
    lastGradeStateSignature = '';
    overlay?.classList.remove('open', 'pudge-review-gate-busy');
    if (!pending) return;
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
    // A warm front card is already useful while backend authorization catches up.
    // Never blanket-disable pointer events in that state; only cold/no-card loading
    // uses the old busy overlay treatment.
    overlay?.classList.toggle('pudge-review-gate-busy', cardQueue.length === 0);
    render({}, {replaceCards:false});
    uiLog('begin_start', {
      card:cardKeyOf(cardQueue[0]), warm_queue:cardQueue.length,
      completed:Number(currentStatus?.completed || 0), remaining:Number(currentStatus?.remaining || 0),
    });
    try {
      const status = await window.pywebview.api.review_gate_begin(launch.path);
      if (myGeneration !== generation || !launch) return;
      if (status?.granted || !status?.blocking) {
        uiLog('begin_done', {granted:true, duration_ms:Math.round((performance?.now?.() ?? Date.now()) - startedAt)});
        await finishAndLaunch();
        return;
      }
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
      render({required:1, completed:0, cards:[], message:error?.message || String(error)}, {replaceCards:true});
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

    // Advance visually before network IO. The remaining cards were already
    // prefetched and authorized by review_gate_begin(); the just-submitted card
    // is still counted only after the Jiten mutation is confirmed.
    cardQueue.shift();
    authorizedCardKeys.delete(submittedKey);
    saving = true;
    optimisticReviewed += 1;
    revealed = false;
    activeCardKey = '';
    render({loading:false}, {replaceCards:false});

    const attemptId = globalThis.crypto?.randomUUID?.() || `pudge-gate-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const submitStartedAt = performance?.now?.() ?? Date.now();
    uiLog('review_optimistic', {
      card:submittedKey, grade:String(grade || 'good'), queue_remaining:cardQueue.length, attempt_id:attemptId,
      displayed_completed:displayedCompleted(currentStatus || {}), confirmed_completed:Number(currentStatus?.completed || 0), optimistic_pending:optimisticReviewed,
    });
    uiLog('review_submit', {card:submittedKey, grade:String(grade || 'good'), queue_remaining:cardQueue.length, attempt_id:attemptId});
    try {
      const result = await window.pywebview.api.review_gate_review(
        launch.path, wordId, readingIndex, String(grade || 'good'), attemptId,
      );
      if (myGeneration !== generation || !launch) return;
      optimisticReviewed = Math.max(0, optimisticReviewed - 1);
      currentStatus = {...(currentStatus || {}), ...(result || {})};
      uiLog('review_result', {card:submittedKey, grade:String(grade || 'good'), outcome:String(result?.outcome || ''), completed:Number(result?.completed || 0), remaining:Number(result?.remaining || 0), granted:Boolean(result?.granted), duration_ms:Math.round((performance?.now?.() ?? Date.now()) - submitStartedAt)});
      if (result?.outcome === 'unknown') {
        uiLog('review_optimistic_rollback', {card:submittedKey, reason:'outcome_unknown', displayed_completed:displayedCompleted(currentStatus), confirmed_completed:Number(currentStatus?.completed || 0), optimistic_pending:optimisticReviewed});
        window.toast?.(result?.message || (ru() ? 'Результат неизвестен; карточка не засчитана и не будет автоматически повторена.' : 'Review outcome is unknown; it was not counted and will not be retried automatically.'));
      } else {
        uiLog('review_optimistic_confirm', {card:submittedKey, displayed_completed:displayedCompleted(currentStatus), confirmed_completed:Number(currentStatus?.completed || 0), optimistic_pending:optimisticReviewed});
      }
      if (result?.granted || !result?.blocking) {
        await finishAndLaunch();
        return;
      }
    } catch (error) {
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
    const node = ensureUi();
    node.classList.add('open');
    const warm = Array.isArray(window.__pudgeReviewGateWarmCards) ? window.__pudgeReviewGateWarmCards : [];
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
    close,
    closeIfOpen,
    handleEscape,
    handleKeydown,
    showAnswer,
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
