'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
  const counts = {manga: 0, audiobooks: 0};
  let mangaState = {books: []};
  let audioState = {books: []};
  let openManga = null;
  let openPage = 0;

  const renderManga = () => {
    const root = $('mangaContent');
    if (!root) return;
    const books = mangaState.books || [];
    root.innerHTML = `<div class="media-head"><div><h2>${ru()?'Манга':'Manga'}</h2><p>${ru()?'CBZ/ZIP, чтение справа налево. MangaOCR запускается только по кнопке.':'CBZ/ZIP, right-to-left reading. MangaOCR runs only when requested.'}</p></div><button id="mangaImport" class="primary">${ru()?'Добавить CBZ':'Add CBZ'}</button></div><div class="media-grid">${books.map(book => `<article class="media-card"><div class="media-card-art">漫</div><strong>${esc(book.title)}</strong><span>${Number(book.position||0)+1} / ${book.page_count}</span><button data-media-action="read-manga" data-id="${book.id}">${ru()?'Читать':'Read'}</button></article>`).join('')}</div>${books.length?'':`<div class="empty">${ru()?'Добавьте CBZ или ZIP с изображениями страниц.':'Add a CBZ or ZIP containing page images.'}</div>`}`;
  };

  const renderAudio = () => {
    const root = $('audiobooksContent');
    if (!root) return;
    const books = audioState.books || [];
    root.innerHTML = `<div class="media-head"><div><h2>${ru()?'Аудиокниги':'Audiobooks'}</h2><p>${ru()?'mpv запоминает позицию; главы M4B читаются из файла.':'mpv playback with saved position and embedded M4B chapters.'}</p></div><button id="audiobookImport" class="primary">${ru()?'Добавить аудиокнигу':'Add audiobook'}</button></div><div class="audiobook-list">${books.map(book => {const pct=book.duration?Math.min(100,book.position/book.duration*100):0;return `<article class="audiobook-card"><div><strong>${esc(book.title)}</strong><span>${formatTime(book.position)} / ${formatTime(book.duration)}</span><div class="media-progress"><i style="width:${pct}%"></i></div></div><button data-media-action="play-audio" data-id="${book.id}">${book.position>1?(ru()?'Продолжить':'Resume'):(ru()?'Слушать':'Play')}</button>${(book.chapters||[]).length?`<details><summary>${ru()?'Главы':'Chapters'} · ${book.chapters.length}</summary><div class="chapter-list">${book.chapters.map(chapter=>`<button data-media-action="play-audio" data-id="${book.id}" data-start="${chapter.start}">${esc(chapter.title)}</button>`).join('')}</div></details>`:''}</article>`}).join('')}</div>${books.length?'':`<div class="empty">${ru()?'Поддерживаются M4B, M4A, MP3, Opus и FLAC.':'M4B, M4A, MP3, Opus and FLAC are supported.'}</div>`}`;
  };

  const formatTime = value => {
    const total = Math.max(0, Math.floor(Number(value)||0));
    const h = Math.floor(total/3600), m = Math.floor(total%3600/60), s = total%60;
    return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
  };

  const loadManga = async () => {
    mangaState = await pywebview.api.manga_state();
    counts.manga = (mangaState.books||[]).length;
    renderManga();
    window.updateCount?.();
  };
  const loadAudio = async () => {
    audioState = await pywebview.api.audiobook_state();
    counts.audiobooks = (audioState.books||[]).length;
    renderAudio();
    window.updateCount?.();
  };

  const ensureReader = () => {
    if ($('mangaReader')) return $('mangaReader');
    const reader = document.createElement('div');
    reader.id = 'mangaReader';
    reader.className = 'manga-reader';
    reader.innerHTML = `<div class="manga-toolbar"><button data-media-action="close-manga">← ${ru()?'Библиотека':'Library'}</button><strong id="mangaReaderTitle"></strong><span id="mangaPageLabel"></span><span class="spacer"></span><button data-media-action="ocr-manga">OCR</button></div><div class="manga-page"><button class="manga-turn left" data-media-action="next-page" aria-label="Next page">‹</button><img id="mangaPageImage" alt=""><button class="manga-turn right" data-media-action="previous-page" aria-label="Previous page">›</button></div><aside id="mangaOcrText" class="manga-ocr-text"></aside>`;
    document.body.append(reader);
    return reader;
  };

  const showPage = async index => {
    if (!openManga) return;
    const page = await pywebview.api.manga_page(openManga.id, index);
    openPage = Number(page.page_index);
    $('mangaPageImage').src = page.data_uri;
    $('mangaPageLabel').textContent = `${openPage+1} / ${page.page_count}`;
    $('mangaOcrText').classList.remove('open');
    $('mangaOcrText').textContent = '';
  };

  document.addEventListener('click', async event => {
    const nav = event.target.closest('.nav button[data-page]');
    if (nav?.dataset.page === 'manga') await loadManga();
    if (nav?.dataset.page === 'audiobooks') await loadAudio();
    if (event.target.id === 'mangaImport') {
      const result = await pywebview.api.choose_manga_file();
      if (result.errors?.length) window.toast?.(result.errors.join(' • '));
      if (!result.cancelled) { mangaState = result.state; counts.manga = mangaState.books.length; renderManga(); }
      return;
    }
    if (event.target.id === 'audiobookImport') {
      const result = await pywebview.api.choose_audiobook_file();
      if (result.errors?.length) window.toast?.(result.errors.join(' • '));
      if (!result.cancelled) { audioState = result.state; counts.audiobooks = audioState.books.length; renderAudio(); }
      return;
    }
    const action = event.target.closest('[data-media-action]');
    if (!action) return;
    const type = action.dataset.mediaAction;
    if (type === 'read-manga') {
      openManga = (mangaState.books||[]).find(book => Number(book.id)===Number(action.dataset.id));
      if (!openManga) return;
      const reader = ensureReader();
      $('mangaReaderTitle').textContent = openManga.title;
      reader.classList.add('open');
      await showPage(Number(openManga.position||0));
    }
    if (type === 'close-manga') { $('mangaReader')?.classList.remove('open'); await loadManga(); }
    if (type === 'next-page') await showPage(openPage + 1);
    if (type === 'previous-page') await showPage(openPage - 1);
    if (type === 'ocr-manga') {
      const panel = $('mangaOcrText'); panel.textContent = ru()?'Распознаю…':'Recognizing…'; panel.classList.add('open');
      const result = await pywebview.api.manga_ocr_page(openManga.id, openPage);
      panel.textContent = result.text || result.error || (ru()?'Текст не найден':'No text found');
    }
    if (type === 'play-audio') {
      await pywebview.api.audiobook_play(Number(action.dataset.id), action.dataset.start==null?null:Number(action.dataset.start));
      window.toast?.(ru()?'Аудиокнига открыта в mpv':'Audiobook opened in mpv');
    }
  });
  document.addEventListener('keydown', event => {
    if (!$('mangaReader')?.classList.contains('open')) return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); void showPage(openPage + 1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); void showPage(openPage - 1); }
    if (event.key === 'Escape') $('mangaReader').classList.remove('open');
  });

  window.PudgeMedia = {counts, loadManga, loadAudio};
})();
