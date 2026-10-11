'use strict';

// The Jimaku key notice: the key stopped working (Jimaku refuses the shared trial key or your
// own key, or the 48 hours of the trial ended). It says what happened and why Jimaku subtitles
// are unavailable, and takes a personal key: Save checks it with Jimaku first and says whether it
// worked. It opens by itself once per trial or refused personal key when nothing else is open
// (the backend remembers it was shown); Settings → Jimaku API opens it again. Closing it changes
// nothing else.
(() => {
  const LOGIN_URL = 'https://jimaku.cc/login';
  const ACCOUNT_URL = 'https://jimaku.cc/account';
  const shown = new Set();
  const tr = (key, params) => PudgeI18n.t(key, params || {});
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const byId = id => document.getElementById(id);
  const problemOf = settings => {
    const problem = settings?.jimaku_key_problem;
    return problem && problem.kind ? problem : null;
  };
  const REASONS = {trial_expired: 'jimakuKey.trialExpired', personal_rejected: 'jimakuKey.personalRejected'};
  const reasonKey = problem => REASONS[problem.kind] || 'jimakuKey.trialRejected';
  // Your own key was refused: you have an account already, you need its current key.
  const ownKey = problem => problem?.kind === 'personal_rejected';
  // The window's `ui` is a top-level const of index.html: a global binding, not a window property.
  const appUi = () => (typeof ui === 'undefined' ? null : ui);
  const currentProblem = () => problemOf(appUi()?.state?.settings);

  const style = () => {
    if (byId('jimakuKeyNoticeStyle')) return;
    const node = document.createElement('style');
    node.id = 'jimakuKeyNoticeStyle';
    node.textContent = `
      .jimaku-key-notice{display:grid;gap:12px;max-width:620px;line-height:1.5}
      .jimaku-key-notice p{margin:0}
      .jimaku-key-notice .setup-callout.warning,.jimaku-key-callout.setup-callout{border-color:rgba(255,196,87,.45);background:rgba(255,196,87,.09);color:#fff1d6}
      .jimaku-key-field{display:grid;gap:6px}
      .jimaku-key-field input{width:100%;min-width:0;background:#0f1b2a;color:var(--text);border:1px solid #334862;border-radius:7px;padding:9px 10px;user-select:text;-webkit-user-select:text}
      .jimaku-key-result{margin:0}
      .jimaku-key-result.ok{color:#9be7b8}
      .jimaku-key-result.warning{color:#ffd58a}
      .jimaku-key-result.error{color:#ff9a9a}
      .jimaku-key-callout{display:grid;gap:8px;margin:8px 0}
    `;
    document.head.appendChild(node);
  };

  const busy = () => ['modalBackdrop', 'onboardingBackdrop', 'pudgeConfirmBackdrop'].some(id => byId(id)?.classList.contains('open'));

  const open = (problem = currentProblem()) => {
    const backdrop = byId('modalBackdrop'), title = byId('modalTitle'), body = byId('modalBody');
    if (!backdrop || !title || !body) return false;
    style();
    backdrop.hidden = false;
    backdrop.classList.remove('watch-order-open', 'preparing');
    backdrop.classList.add('open');
    title.textContent = tr('jimakuKey.title');
    const reason = problem ? `<div class="setup-callout warning">${esc(tr(reasonKey(problem)))}</div>` : '';
    body.innerHTML = `<div class="jimaku-key-notice" data-jimaku-key-notice>${reason}`
      + `<p>${esc(tr('jimakuKey.why'))}</p>`
      + `<p class="subtle">${esc(tr(ownKey(problem) ? 'jimakuKey.howToPersonal' : 'jimakuKey.howTo'))}</p>`
      + `<div class="setting-inline-actions"><button data-action="url" data-url="${LOGIN_URL}">${esc(tr('ui.registerSignIn'))}</button>`
      + `<button data-action="url" data-url="${ACCOUNT_URL}">${esc(tr('uiB.jimaku.account'))}</button></div>`
      + `<label class="jimaku-key-field" for="jimakuKeyNoticeInput">${esc(tr('jimakuKey.field'))}`
      + `<input id="jimakuKeyNoticeInput" type="password" autocomplete="off" spellcheck="false"></label>`
      + `<div class="setting-inline-actions"><button class="primary" id="jimakuKeyNoticeSave" data-jimaku-key-save>${esc(tr('jimakuKey.save'))}</button></div>`
      + `<p id="jimakuKeyNoticeResult" class="jimaku-key-result" role="status" hidden></p></div>`;
    return true;
  };

  // From the window's renders: open the notice the first time a problem shows up.
  const maybeShow = settings => {
    const problem = problemOf(settings);
    if (!problem || problem.seen || shown.has(problem.id) || busy()) return false;
    if (!open(problem)) return false;
    shown.add(problem.id);
    try {
      void Promise.resolve(window.pywebview?.api?.mark_jimaku_key_notice_seen?.(problem.id)).catch(error => console.error('Jimaku key notice', error));
    } catch (error) {
      console.error('Jimaku key notice', error);
    }
    return true;
  };

  // The warning in Settings → Jimaku API, with the button that opens the notice again.
  const calloutHtml = (settings, withAction = true) => {
    const problem = problemOf(settings);
    if (!problem) return '';
    style();
    const label = tr(ownKey(problem) ? 'jimakuKey.openNoticePersonal' : 'jimakuKey.openNotice');
    const action = withAction ? `<div class="setting-inline-actions"><button data-jimaku-key-open>${esc(label)}</button></div>` : '';
    return `<div class="setup-callout warning jimaku-key-callout" data-jimaku-key-callout><span>${esc(tr(reasonKey(problem)))}</span>${action}</div>`;
  };

  const report = (key, kind, params) => {
    const node = byId('jimakuKeyNoticeResult');
    if (!node) return;
    node.textContent = tr(key, params);
    node.className = `jimaku-key-result${kind ? ` ${kind}` : ''}`;
    node.hidden = false;
  };

  const save = async () => {
    const input = byId('jimakuKeyNoticeInput'), button = byId('jimakuKeyNoticeSave');
    const key = String(input?.value || '').trim();
    if (!key) {
      report('jimakuKey.enterKey', 'error');
      input?.focus();
      return;
    }
    if (button) button.disabled = true;
    report('jimakuKey.checking', '');
    try {
      const result = await window.pywebview.api.save_jimaku_api_key(key);
      if (!result?.saved) {
        report(result?.status === 'missing' ? 'jimakuKey.enterKey' : 'jimakuKey.invalid', 'error');
        return;
      }
      const app = appUi();
      if (app) {
        if (result.state) app.state = result.state;
        if (result.settings && app.state) app.state.settings = result.settings;
      }
      if (input) input.value = '';
      report(result.verified ? 'jimakuKey.saved' : 'jimakuKey.savedUnchecked', result.verified ? 'ok' : 'warning');
      // Settings keeps its other unsaved edits: only the Jimaku block changes.
      document.querySelectorAll('[data-jimaku-key-callout]').forEach(node => node.remove());
      const field = byId('s_jimaku');
      if (field && result.settings) field.value = result.settings.jimaku_api_key || '';
      if (typeof globalThis.renderDataPages === 'function') globalThis.renderDataPages();
      if (result.recheck_subtitles && app && typeof globalThis.pollForegroundWork === 'function') {
        app.emptyPolls = 0;
        if (app.foregroundPollTimer) clearTimeout(app.foregroundPollTimer);
        app.foregroundPollTimer = setTimeout(globalThis.pollForegroundWork, 0);
      }
    } catch (error) {
      report('error.generic', 'error', {error: PudgeI18n.message?.(error?.message || error) ?? String(error?.message || error)});
    } finally {
      if (button) button.disabled = false;
    }
  };

  document.addEventListener('click', event => {
    const target = event.target;
    if (target?.closest?.('[data-jimaku-key-open]')) {
      event.preventDefault();
      open();
      byId('jimakuKeyNoticeInput')?.focus();
    } else if (target?.closest?.('[data-jimaku-key-save]')) {
      event.preventDefault();
      void save();
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target?.id === 'jimakuKeyNoticeInput') {
      event.preventDefault();
      void save();
    }
  });

  window.PudgeJimakuKey = {maybeShow, open, calloutHtml, save};
})();
