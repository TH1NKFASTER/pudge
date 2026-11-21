'use strict';

// More than five titles left the AniList list in one sync. That looks like a glitch more often
// than the user's doing, so Pudge keeps following them and asks here: "Remove from Pudge" does what
// a removal on AniList does, "Keep" keeps them and is never asked again for those titles. The
// notice opens by itself once per held set while nothing else is open; closing it without an
// answer asks again after the next start.
(() => {
  const shown = new Set();
  const tr = (key, params) => PudgeI18n.t(key, params || {});
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  const byId = id => document.getElementById(id);
  const appUi = () => (typeof ui === 'undefined' ? null : ui);
  const noticeOf = settings => {
    const notice = settings?.anilist_removal_notice;
    return notice && notice.id && Array.isArray(notice.titles) && notice.titles.length ? notice : null;
  };
  const busy = () => ['modalBackdrop', 'onboardingBackdrop', 'pudgeConfirmBackdrop'].some(id => byId(id)?.classList.contains('open'));

  const open = notice => {
    const backdrop = byId('modalBackdrop'), title = byId('modalTitle'), body = byId('modalBody');
    if (!backdrop || !title || !body) return false;
    backdrop.hidden = false;
    backdrop.classList.remove('watch-order-open', 'preparing');
    backdrop.classList.add('open');
    title.textContent = tr('anilistRemoval.title');
    const items = notice.titles.map(item => `<li data-media-id="${Number(item.media_id)}">${esc(item.title || `#${Number(item.media_id)}`)}</li>`).join('');
    body.innerHTML = `<div class="anilist-removal-notice" data-anilist-removal-notice="${esc(notice.id)}" style="display:grid;gap:12px;max-width:620px;line-height:1.5">`
      + `<p style="margin:0">${esc(tr('anilistRemoval.why', {count: notice.titles.length}))}</p>`
      + `<ul style="margin:0;padding-left:20px;max-height:240px;overflow:auto">${items}</ul>`
      + `<p class="subtle" style="margin:0">${esc(tr('anilistRemoval.choices'))}</p>`
      + `<div class="setting-inline-actions"><button class="danger-action" data-anilist-removal="remove">${esc(tr('anilistRemoval.remove'))}</button>`
      + `<button class="primary" data-anilist-removal="keep">${esc(tr('anilistRemoval.keep'))}</button></div>`
      + `<p id="anilistRemovalResult" role="status" style="margin:0" hidden></p></div>`;
    return true;
  };

  // From the window's renders: open the notice the first time a held set shows up.
  const maybeShow = settings => {
    const notice = noticeOf(settings);
    if (!notice || shown.has(notice.id) || busy()) return false;
    if (!open(notice)) return false;
    shown.add(notice.id);
    return true;
  };

  const answer = async (action, button) => {
    const root = button.closest('[data-anilist-removal-notice]');
    const noticeId = root?.dataset.anilistRemovalNotice || '';
    root?.querySelectorAll('[data-anilist-removal]').forEach(node => { node.disabled = true; });
    try {
      const result = await window.pywebview.api.resolve_anilist_removal_notice(action, noticeId);
      const app = appUi();
      if (result?.state && app) app.state = result.state;
      if (result?.stale) {
        // The list changed since the notice opened: show what is held now.
        shown.delete(noticeId);
      }
      if (typeof globalThis.closeModal === 'function') globalThis.closeModal();
      if (typeof globalThis.renderDataPages === 'function') globalThis.renderDataPages();
      if (!result?.stale && typeof globalThis.toast === 'function') {
        globalThis.toast(tr(action === 'remove' ? 'anilistRemoval.removed' : 'anilistRemoval.kept', {count: Number(result?.changed || 0)}));
      }
    } catch (error) {
      root?.querySelectorAll('[data-anilist-removal]').forEach(node => { node.disabled = false; });
      const node = byId('anilistRemovalResult');
      if (node) {
        node.textContent = tr('error.generic', {error: PudgeI18n.message?.(error?.message || error) ?? String(error?.message || error)});
        node.hidden = false;
      }
    }
  };

  document.addEventListener('click', event => {
    const button = event.target?.closest?.('[data-anilist-removal]');
    if (!button) return;
    event.preventDefault();
    void answer(button.dataset.anilistRemoval, button);
  });

  window.PudgeAniListRemoval = {maybeShow, open};
})();
