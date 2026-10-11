'use strict';
// Screen text panel (screen_text.html): the game window's OCR text, with the
// light novel reader's word cards (a word opens its Jiten card on hover or by the
// LN reader's triggers, as set there; drag-select → Translate / Explain grammar). The lines come from the Visual novels capture (the VN page's
// ScreenCaptureKit path); this page polls the cheap state and parses each new line once.
(() => {
  const g = globalThis;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  g.ui = {lang:document.documentElement.lang || 'en', state:{settings:{llm_enabled:false}}};
  const POLL_MS = 700, HIDDEN_POLL_MS = 3000;
  let settings = {study_backend:'jiten'}, shownId = -1, renderToken = 0, pollTimer = null, lastState = null;
  // The words' card context: the game frame of the shown line (a card made from a word gets it
  // as its picture). One object per line, updated in place when the line's frame arrives.
  let lineContext = {kind:'vn', frame_id:0};
  let toastTimer;
  g.toast = message => {
    const node = $('floatError');
    node.textContent = PudgeI18n.message(String(message ?? '')); node.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; }, 5000);
  };

  // Word cards open as in the light novel reader: on hover, or by its triggers (left /
  // middle / right click, Shift).
  const studyMode = () => (String(settings.study_card_mode || 'button') === 'hover' ? 'hover' : 'button');
  const studyTriggers = () => new Set((Array.isArray(settings.study_card_triggers) && settings.study_card_triggers.length
    ? settings.study_card_triggers : ['MouseLeft']).map(String));
  const studyTrigger = code => studyMode() === 'button' && studyTriggers().has(code);
  const applyStudyMode = () => $('screenTextBody').toggleAttribute('data-pudge-study-hover', studyMode() === 'hover');
  const studyWord = target => target?.closest?.('#screenTextBody [data-pudge-study-token]') || null;
  const openWord = word => {
    const selection = window.getSelection?.();
    if (word && (!selection || selection.isCollapsed)) void g.PudgeReadingTools?.study?.openElement?.(word);
  };
  let hoveredWord = null;

  // The window is small: an open word card or translation that does not fit makes it
  // bigger (up to the screen), and closing it gives the user's size back. Sizes as in
  // reading_tools.css (beyond its height a popup scrolls).
  const POPUP_MARGIN = 24;
  const popupNeeds = () => {
    let width = 0, height = 0;
    const card = $('pudgeStudyCard'), translation = $('pudgeTranslationPop');
    const add = (pop, preferredWidth, maxHeight) => {
      width = Math.max(width, preferredWidth + POPUP_MARGIN);
      height = Math.max(height, Math.min(maxHeight, Number(pop.scrollHeight || 0) + 2) + POPUP_MARGIN);
    };
    if (card?.classList.contains('open')) add(card, Number.parseFloat(card.style.getPropertyValue('--pudge-study-width')) || 440, 560);
    if (translation?.classList.contains('open')) {
      const explained = Boolean(translation.querySelector('.pudge-grammar:not([hidden])'));
      add(translation, explained ? 560 : 460, explained ? 560 : 320);
    }
    return {width:Math.ceil(width), height:Math.ceil(height)};
  };
  let fitTimer = null, fitSent = '', grown = false;
  function fitWindow() {
    fitTimer = null;
    const need = popupNeeds();
    const inner = [Number(window.innerWidth || 0), Number(window.innerHeight || 0)];
    let key;
    if (need.width && need.height) {
      // Fits already and never grown: nothing to ask.
      if (!grown && need.width <= inner[0] && need.height <= inner[1]) return;
      key = `${need.width}x${need.height}`;
    } else {
      if (!grown) return;
      key = 'closed';
    }
    if (key === fitSent) return;
    fitSent = key;
    grown = key !== 'closed';
    const api = g.pywebview?.api;
    if (!api?.screen_text_fit) return;
    const [width, height] = key === 'closed' ? [0, 0] : [need.width, need.height];
    void Promise.resolve(api.screen_text_fit(width, height, inner[0], inner[1])).catch(error => console.debug?.('screen text fit', error));
  }
  const scheduleFit = () => { if (!fitTimer) fitTimer = setTimeout(fitWindow, 0); };
  window.addEventListener('pudge-popup-layout', scheduleFit);
  window.addEventListener('resize', scheduleFit);

  const applyStaticTexts = () => {
    for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = PudgeI18n.t(node.dataset.i18n);
    for (const [attribute, name] of [['title', 'i18nTitle'], ['aria-label', 'i18nAriaLabel']]) {
      for (const node of document.querySelectorAll(`[data-i18n-${attribute}]`)) node.setAttribute(attribute, PudgeI18n.t(node.dataset[name]));
    }
  };

  function problemText(state) {
    const code = String(state.error_code || '');
    if (code === 'permission_required') return PudgeI18n.t('screenText.permission');
    if (code === 'window_unavailable' && state.window_title) return PudgeI18n.t('screenText.windowGone');
    return PudgeI18n.message(String(state.detail || '')) || PudgeI18n.t('vn.captureFailed');
  }

  let hintDismissed = false;
  function renderStatus(state) {
    const status = $('screenTextStatus'), problem = $('screenTextProblem');
    const error = state.status === 'error';
    problem.hidden = !error;
    if (error) {
      const permission = String(state.error_code || '') === 'permission_required';
      problem.innerHTML = `${esc(problemText(state))}${permission ? ` <button id="screenTextPermission" type="button">${esc(PudgeI18n.t('vn.openScreenRecordingSettings'))}</button>` : ''}`;
    }
    let line = String(state.window_title || '');
    if (state.status === 'paused') line = PudgeI18n.t('screenText.paused');
    else if (state.status === 'choosing') line = PudgeI18n.t('screenText.choosing');
    else if (state.status === 'stopped') line = PudgeI18n.t('screenText.stopped');
    else if (!error && !state.text) line = state.ocr_count ? PudgeI18n.t('screenText.noText') : PudgeI18n.t('screenText.starting');
    status.textContent = line;
    status.classList.toggle('paused', state.status === 'paused');
    $('screenTextRefresh').disabled = !state.running;
    $('screenTextHint').hidden = !state.permission_hint || hintDismissed;
  }

  async function renderText(state) {
    const body = $('screenTextBody');
    const id = Number(state.text_id || 0);
    if (id === shownId) { if (Number(state.frame_id || 0)) lineContext.frame_id = Number(state.frame_id); return; }
    shownId = id;
    lineContext = {kind:'vn', frame_id:Number(state.frame_id || 0)};
    const text = String(state.text || '');
    const token = ++renderToken;
    if (!text) { body.textContent = ''; return; }
    body.textContent = text; // shown at once; words become clickable once parsed
    try {
      const payload = await g.pywebview.api.visual_novel_parse(text);
      if (token !== renderToken) return;
      const html = g.PudgeReadingTools?.study?.renderParsedText(payload, {backend:String(settings.study_backend || 'jiten'), contextText:text, mediaContext:lineContext});
      if (html) body.innerHTML = html;
    } catch (error) { console.debug?.('screen text parse', error); }
  }

  async function poll() {
    pollTimer = null;
    try {
      const state = await g.pywebview.api.screen_text_state();
      lastState = state;
      renderStatus(state);
      await renderText(state);
    } catch (error) { console.debug?.('screen text state', error); }
    pollTimer = setTimeout(poll, document.hidden ? HIDDEN_POLL_MS : POLL_MS);
  }

  document.addEventListener('click', event => {
    if (event.target.closest?.('#screenTextRefresh')) void g.pywebview.api.screen_text_refresh().catch(error => g.toast(error?.message || error));
    else if (event.target.closest?.('#screenTextPermission')) void g.pywebview.api.open_screen_recording_settings();
    else if (event.target.closest?.('#screenTextAllow')) void g.pywebview.api.screen_recording_request_access();
    else if (event.target.closest?.('#screenTextHintClose')) {
      hintDismissed = true;
      $('screenTextHint').hidden = true;
      void g.pywebview.api.screen_text_dismiss_hint?.();
    }
  });
  const body = $('screenTextBody');
  // A left click opens the card only when it is one of the LN reader's triggers (this
  // listener comes before reading_tools.js's, which opens the card on a click).
  document.addEventListener('click', event => {
    if (studyWord(event.target) && studyMode() === 'button' && !studyTriggers().has('MouseLeft')) event.stopImmediatePropagation();
  }, true);
  body.addEventListener('contextmenu', event => {
    const word = studyWord(event.target);
    if (!word || !studyTrigger('MouseRight')) return;
    event.preventDefault();
    openWord(word);
  });
  body.addEventListener('pointerup', event => {
    const word = studyWord(event.target);
    if (!word || Number(event.button) !== 1 || !studyTrigger('MouseMiddle')) return;
    event.preventDefault();
    openWord(word);
  });
  body.addEventListener('pointerover', event => { hoveredWord = studyWord(event.target); });
  body.addEventListener('pointerleave', () => { hoveredWord = null; });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && g.PudgeReadingTools?.study?.closeIfOpen?.()) { event.preventDefault(); return; }
    if (hoveredWord?.isConnected && studyTrigger(String(event.code || '')) && !event.repeat) { openWord(hoveredWord); return; }
    g.PudgeReadingTools?.study?.handleReviewKeydown?.(event);
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && pollTimer) { clearTimeout(pollTimer); void poll(); }
  });
  applyStaticTexts();
  applyStudyMode();
  // At start and whenever the LN reader's (or the app's) settings are saved.
  async function reloadSettings() {
    try {
      settings = {...settings, ...(await g.pywebview.api.screen_text_settings())};
      g.ui.lang = String(settings.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en';
      document.documentElement.lang = g.ui.lang;
      g.ui.state.settings = settings;
      applyStaticTexts();
      applyStudyMode();
      if (lastState) renderStatus(lastState);
    } catch (error) { console.debug?.('screen text settings', error); }
  }
  window.addEventListener('pywebviewready', async () => {
    await reloadSettings();
    void poll();
  }, {once:true});
  g.PudgeScreenText = {poll, reloadSettings};
})();
