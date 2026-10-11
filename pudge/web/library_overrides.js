'use strict';

// Hand-made volume / series corrections for light novels, manga and audiobooks.
// Empty fields mean "as Pudge detected it"; both empty removes the correction.
(() => {
  let pending = null;

  const backdropEl = () => document.getElementById('pudgeLibraryMetaBackdrop');
  const isOpen = () => Boolean(backdropEl()?.classList.contains('open'));

  const close = value => {
    backdropEl()?.classList.remove('open');
    const current = pending;
    pending = null;
    if (current) current.resolve(value || null);
  };

  const ensureDialog = () => {
    let backdrop = backdropEl();
    if (backdrop) return backdrop;
    const style = document.createElement('style');
    style.textContent = `
      .pudge-meta-backdrop{position:fixed;z-index:12050;inset:0;display:none;place-items:center;padding:24px;background:rgba(2,7,13,.8)}
      .pudge-meta-backdrop.open{display:grid}
      .pudge-meta-dialog{width:min(470px,calc(100vw - 32px));display:grid;gap:14px;padding:18px;border:1px solid #405675;border-radius:14px;background:#101b2a;color:#edf4ff;box-shadow:0 24px 70px rgba(0,0,0,.58)}
      .pudge-meta-dialog strong{font-size:16px}
      .pudge-meta-book{margin:0;color:#9fb2c9;font-size:13px;overflow-wrap:anywhere}
      .pudge-meta-grid{display:grid;grid-template-columns:auto 1fr;gap:10px 12px;align-items:center}
      .pudge-meta-grid input{width:100%;box-sizing:border-box}
      .pudge-meta-hint{margin:0;color:#9fb2c9;font-size:12px;line-height:1.45}
      .pudge-meta-error{margin:0;color:#ff8a98;font-size:13px;min-height:1em}
      .pudge-meta-actions{display:flex;justify-content:flex-end;gap:8px}
      .pudge-meta-actions .reset{margin-right:auto}
      .pudge-meta-actions .save{background:#4a68e0;border-color:#4a68e0;color:white}
    `;
    document.head.appendChild(style);
    backdrop = document.createElement('div');
    backdrop.id = 'pudgeLibraryMetaBackdrop';
    backdrop.className = 'pudge-meta-backdrop';
    backdrop.innerHTML = `
      <form class="pudge-meta-dialog" novalidate role="dialog" aria-modal="true" aria-labelledby="pudgeMetaTitle">
        <strong id="pudgeMetaTitle"></strong>
        <p id="pudgeMetaBook" class="pudge-meta-book"></p>
        <div class="pudge-meta-grid">
          <label for="pudgeMetaVolume" id="pudgeMetaVolumeLabel"></label>
          <input id="pudgeMetaVolume" type="number" min="1" max="999" step="1" inputmode="numeric">
          <label for="pudgeMetaSeries" id="pudgeMetaSeriesLabel"></label>
          <input id="pudgeMetaSeries" type="text" maxlength="200" autocomplete="off">
        </div>
        <p id="pudgeMetaHint" class="pudge-meta-hint"></p>
        <p id="pudgeMetaError" class="pudge-meta-error" role="alert"></p>
        <div class="pudge-meta-actions">
          <button type="button" class="reset" data-pudge-meta="reset"></button>
          <button type="button" data-pudge-meta="cancel"></button>
          <button type="submit" class="save" data-pudge-meta="save"></button>
        </div>
      </form>`;
    document.body.appendChild(backdrop);
    backdrop.addEventListener('click', event => {
      const action = event.target.closest?.('[data-pudge-meta]')?.dataset.pudgeMeta;
      if (action === 'cancel' || event.target === backdrop) close(null);
      else if (action === 'reset') void save(true);
    });
    backdrop.querySelector('form').addEventListener('submit', event => {
      event.preventDefault();
      void save(false);
    });
    return backdrop;
  };

  const save = async reset => {
    if (!pending || pending.saving) return;
    const volume = reset ? '' : String(document.getElementById('pudgeMetaVolume').value || '').trim();
    const series = reset ? '' : String(document.getElementById('pudgeMetaSeries').value || '').trim();
    const error = document.getElementById('pudgeMetaError');
    error.textContent = '';
    pending.saving = true;
    try {
      const result = await window.pywebview.api.library_set_metadata(pending.kind, Number(pending.item.id), volume, series);
      if (!result?.ok) {
        error.textContent = result?.error ? PudgeI18n.message(String(result.error)) : PudgeI18n.t('libraryUi.couldNotSave');
        return;
      }
      close(result.item || {id: pending.item.id});
    } catch (exc) {
      error.textContent = PudgeI18n.message(String(exc?.message || exc));
    } finally {
      if (pending) pending.saving = false;
    }
  };

  // Escape closes only this dialog, never the reader or the library behind it.
  document.addEventListener('keydown', event => {
    if (!isOpen() || event.key !== 'Escape') return;
    event.preventDefault();
    event.stopImmediatePropagation();
    close(null);
  }, true);

  const edit = (kind, item) => new Promise(resolve => {
    if (!item) { resolve(null); return; }
    if (pending) close(null);
    const backdrop = ensureDialog();
    const manual = Boolean(item.manual_metadata);
    document.getElementById('pudgeMetaTitle').textContent = PudgeI18n.t('libraryUi.editVolumeAndSeries');
    document.getElementById('pudgeMetaBook').textContent = String(item.title || '');
    document.getElementById('pudgeMetaVolumeLabel').textContent = PudgeI18n.t('ui.volume3');
    document.getElementById('pudgeMetaSeriesLabel').textContent = PudgeI18n.t('ln.series');
    document.getElementById('pudgeMetaHint').textContent = PudgeI18n.t('libraryUi.metadataHint');
    document.getElementById('pudgeMetaError').textContent = '';
    const volume = document.getElementById('pudgeMetaVolume');
    const series = document.getElementById('pudgeMetaSeries');
    volume.value = item.manual_volume != null ? String(item.manual_volume) : '';
    volume.placeholder = item.volume ? String(item.volume) : '';
    series.value = String(item.manual_series_title || '');
    series.placeholder = String(item.series_title || '');
    const reset = backdrop.querySelector('[data-pudge-meta="reset"]');
    reset.textContent = PudgeI18n.t('libraryUi.resetToDetected');
    reset.hidden = !manual;
    backdrop.querySelector('[data-pudge-meta="cancel"]').textContent = PudgeI18n.t('action.cancel');
    backdrop.querySelector('[data-pudge-meta="save"]').textContent = PudgeI18n.t('action.save');
    pending = {kind, item, resolve, saving: false};
    backdrop.classList.add('open');
    requestAnimationFrame(() => volume.focus());
  });

  window.PudgeLibraryMetadata = {edit, isOpen, closeIfOpen: () => (isOpen() ? (close(null), true) : false)};
})();
