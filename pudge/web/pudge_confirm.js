'use strict';

(() => {
  let pendingResolve = null;

  const finish = value => {
    const backdrop = document.getElementById('pudgeConfirmBackdrop');
    if (backdrop) backdrop.classList.remove('open');
    const resolve = pendingResolve;
    pendingResolve = null;
    if (resolve) resolve(Boolean(value));
  };

  const ensureDialog = () => {
    let backdrop = document.getElementById('pudgeConfirmBackdrop');
    if (backdrop) return backdrop;

    const style = document.createElement('style');
    style.textContent = `
      .pudge-confirm-backdrop{position:fixed;z-index:12050;inset:0;display:none;place-items:center;padding:24px;background:rgba(2,7,13,.72);backdrop-filter:blur(3px)}
      .pudge-confirm-backdrop.open{display:grid}
      .pudge-confirm-dialog{width:min(470px,calc(100vw - 32px));display:grid;gap:16px;padding:18px;border:1px solid #405675;border-radius:14px;background:#101b2a;color:#edf4ff;box-shadow:0 24px 70px rgba(0,0,0,.58)}
      .pudge-confirm-head{display:flex;align-items:center;gap:12px}
      .pudge-confirm-head img{width:42px;height:42px;object-fit:contain;flex:0 0 auto;filter:drop-shadow(0 0 8px rgba(118,219,255,.28))}
      .pudge-confirm-head strong{font-size:16px}
      .pudge-confirm-message{margin:0;color:#dbe7f6;font-size:14px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}
      .pudge-confirm-actions{display:flex;justify-content:flex-end;gap:8px}
      .pudge-confirm-actions button{min-width:92px}
      .pudge-confirm-actions .confirm{background:#6f8cff;border-color:#6f8cff;color:white}
    `;
    document.head.appendChild(style);

    backdrop = document.createElement('div');
    backdrop.id = 'pudgeConfirmBackdrop';
    backdrop.className = 'pudge-confirm-backdrop';
    backdrop.innerHTML = `
      <div class="pudge-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="pudgeConfirmTitle" aria-describedby="pudgeConfirmMessage">
        <div class="pudge-confirm-head">
          <img src="app-logo.png" alt="">
          <strong id="pudgeConfirmTitle">Pudge</strong>
        </div>
        <p id="pudgeConfirmMessage" class="pudge-confirm-message"></p>
        <div class="pudge-confirm-actions">
          <button type="button" data-pudge-confirm="cancel">Cancel</button>
          <button type="button" class="confirm" data-pudge-confirm="ok">Confirm</button>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);

    backdrop.addEventListener('click', event => {
      const action = event.target.closest?.('[data-pudge-confirm]')?.dataset.pudgeConfirm;
      if (action === 'ok') finish(true);
      else if (action === 'cancel' || event.target === backdrop) finish(false);
    });

    return backdrop;
  };

  document.addEventListener('keydown', event => {
    const backdrop = document.getElementById('pudgeConfirmBackdrop');
    if (!backdrop?.classList.contains('open')) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    }
  }, true);

  window.pudgeConfirm = message => new Promise(resolve => {
    if (pendingResolve) {
      const previous = pendingResolve;
      pendingResolve = null;
      previous(false);
    }

    const backdrop = ensureDialog();
    const lang = String(window.ui?.lang || document.documentElement.lang || 'en').toLowerCase();
    document.getElementById('pudgeConfirmMessage').textContent = String(message ?? '');

    const cancel = backdrop.querySelector('[data-pudge-confirm="cancel"]');
    const ok = backdrop.querySelector('[data-pudge-confirm="ok"]');
    cancel.textContent = lang.startsWith('ru') ? 'Отмена' : 'Cancel';
    ok.textContent = lang.startsWith('ru') ? 'Подтвердить' : 'Confirm';

    pendingResolve = resolve;
    backdrop.classList.add('open');
    requestAnimationFrame(() => ok.focus());
  });
})();
