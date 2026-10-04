/* External Finder files: overlay only, import remains in the existing native handler. */
(() => {
  let depth = 0;
  const surface = () => document.getElementById('lightNovelsContent');
  const ru = () => (typeof ui !== 'undefined' ? ui.lang : window.ui?.lang || document.documentElement?.lang || 'en') === 'ru';
  const active = () => document.getElementById('lightnovels')?.classList.contains('active');
  const external = event => [...(event.dataTransfer?.types || [])].includes('Files');
  const supported = event => {
    const files = [...(event.dataTransfer?.files || [])];
    return !files.length || files.every(file => /\.(epub|txt)$/i.test(file.name || ''));
  };
  const clear = () => { depth = 0; surface()?.classList.remove('ln-file-drag'); };
  function enter(event) {
    if (!active() || !external(event) || !supported(event)) return;
    if (event.type === 'dragenter') depth += 1;
    const box = surface();
    if (box) { box.dataset.dropLabel = ru() ? 'Бросьте файл сюда · EPUB/TXT' : 'Drop EPUB/TXT here'; box.classList.add('ln-file-drag'); }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }
  document.addEventListener('dragenter', enter, true);
  document.addEventListener('dragover', enter, true);
  document.addEventListener('dragleave', event => {
    if (!external(event)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) clear();
  }, true);
  document.addEventListener('drop', event => {
    const reject = active() && external(event) && !supported(event);
    clear();
    if (reject) {
      event.preventDefault(); event.stopImmediatePropagation(); event.stopPropagation();
      window.toast?.(ru() ? 'Поддерживаются EPUB и TXT' : 'EPUB and TXT files are supported');
    }
  }, true);
  for (const name of ['dragend', 'blur', 'pudge-files-imported']) window.addEventListener(name, clear);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') clear(); });
  window.PudgeLnDrop = {clear, supported, external};
})();
