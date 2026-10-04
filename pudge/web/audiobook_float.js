'use strict';
(() => {
  const g = globalThis;
  g.PudgeAudioFloat = true;
  g.ui = {lang:document.documentElement.lang || 'en', state:{settings:{llm_enabled:false}}};
  let toastTimer;
  g.toast = message => {
    const node = document.getElementById('floatError');
    node.textContent = g.PudgeUiLanguage?.message(message) ?? String(message || ''); node.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; },5000);
  };
  const updateEmpty = () => {
    const node = document.getElementById('floatEmpty');
    const text = g.ui.lang === 'ru' ? 'Начните прослушивание в Pudge' : 'Start listening in Pudge';
    if (node.textContent !== text) node.textContent = text;
    node.hidden = Boolean(document.querySelector('.sidebar-audio'));
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
    setTimeout(power,10000);
  }
  window.addEventListener('pywebviewready',async () => {
    try {
      const settings = await g.pywebview.api.audiobook_float_settings();
      g.ui.lang = settings.language === 'ru' ? 'ru' : 'en';document.documentElement.lang = g.ui.lang;updateEmpty();
    } catch (error) { console.debug('floating audiobook settings',error); }
    void power();
  },{once:true});
})();
