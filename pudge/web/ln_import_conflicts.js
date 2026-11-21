'use strict';

// A light novel whose title (and volume) is already in the library with other contents: an
// updated copy, or another book that only shares the title. Pudge asks for each one: Keep both
// (imported as a separate book, its title numbered), Replace (the library's copy, its reading
// place carried over) or Cancel (nothing is imported). The backend holds each file under a token.
(() => {
  const tr = (key, params) => PudgeI18n.t(key, params || {});
  const appUi = () => (typeof ui === 'undefined' ? null : ui);

  const resolve = async conflicts => {
    let imported = 0;
    for (const conflict of Array.isArray(conflicts) ? conflicts : []) {
      const choice = await window.pudgeChoose(tr('lnImportConflict.message', {name: conflict.name || '', title: conflict.title || ''}), {
        title: tr('lnImportConflict.title'),
        actions: [
          {value: 'keep_both', label: tr('lnImportConflict.keepBoth'), primary: true},
          {value: 'replace', label: tr('lnImportConflict.replace'), danger: true},
        ],
      });
      try {
        const result = await window.pywebview.api.light_novel_resolve_import_conflict(String(conflict.token || ''), choice || 'cancel');
        if (result?.ok === false) {
          globalThis.toast?.(PudgeI18n.message?.(result.error) ?? String(result.error || ''));
          continue;
        }
        if (result?.book) imported += 1;
        const app = appUi();
        if (result?.state && app) {
          app.lnState = result.state;
          if (typeof globalThis.renderLightNovels === 'function') globalThis.renderLightNovels();
          if (typeof globalThis.updateCount === 'function') globalThis.updateCount();
        }
      } catch (error) {
        globalThis.toast?.(tr('error.generic', {error: PudgeI18n.message?.(error?.message || error) ?? String(error?.message || error)}));
      }
    }
    return imported;
  };

  window.PudgeLnImportConflicts = {resolve};
})();
