'use strict';
(() => {
  const g = globalThis;
  g.PudgeAudioFloat = true;
  g.ui = {lang:document.documentElement.lang || 'en', state:{settings:{llm_enabled:false}}};
  let toastTimer;
  g.toast = message => {
    const node = document.getElementById('floatError');
    node.textContent = PudgeI18n.message(String(message ?? '')); node.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; },5000);
  };
  // Static texts of audiobook_float.html: data-i18n (text), data-i18n-title and
  // data-i18n-aria-label (attributes) hold catalog keys; index.html's
  // applyStaticLanguage is not loaded in this window.
  const applyStaticTexts = () => {
    for (const node of document.querySelectorAll('[data-i18n]')) {
      const text = PudgeI18n.t(node.dataset.i18n);
      if (node.textContent !== text) node.textContent = text;
    }
    for (const [attribute, name] of [['title', 'i18nTitle'], ['aria-label', 'i18nAriaLabel']]) {
      for (const node of document.querySelectorAll(`[data-i18n-${attribute}]`)) {
        const text = PudgeI18n.t(node.dataset[name]);
        if (node.getAttribute(attribute) !== text) node.setAttribute(attribute, text);
      }
    }
  };
  const updateEmpty = () => {
    applyStaticTexts();
    document.getElementById('floatEmpty').hidden = Boolean(document.querySelector('.sidebar-audio'));
  };
  new MutationObserver(updateEmpty).observe(document.body,{subtree:true,childList:true});
  updateEmpty();
  async function power() {
    try {
      const state = await g.pywebview?.api?.power_status?.();
      const saving = state?.mode === 'energy_saving';
      if (document.documentElement.classList.contains('energy-saving') !== saving) {
        document.documentElement.classList.toggle('energy-saving',saving);
        g.PudgeSidebarCompanion?.powerChanged?.();
      }
    } catch (error) { console.debug('floating audiobook power',error); }
    // The power policy itself refreshes every 30 s.
    setTimeout(power,30000);
  }
  window.addEventListener('pywebviewready',async () => {
    try {
      const settings = await g.pywebview.api.audiobook_float_settings();
      g.ui.lang = String(settings.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en';document.documentElement.lang = g.ui.lang;updateEmpty();
    } catch (error) { console.debug('floating audiobook settings',error); }
    void power();
  },{once:true});
})();
