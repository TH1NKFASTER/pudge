'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const ru = () => document.documentElement.lang === 'ru' || window.ui?.lang === 'ru';
  const counts = {manga: 0, audiobooks: 0};
  let mangaState = {books: []};
  let audioState = {books: []};
  let audioImportBusy = '';
  let audioPollTimer = null;
  let openManga = null;
  let openPage = 0;
  let mangaAniListBookId = null;
  let mangaAniListResults = [];
  let audioLnNyaaRows = [];
  let pageLoadToken = 0;
  let ocrRequestToken = 0;
  const mangaOcrPollers = new Map();

  const renderManga = () => {
    const root = $('mangaContent');
    if (!root) return;
    const books = mangaState.books || [];
    root.innerHTML = `<div class="media-head"><div><h2>${ru()?'Манга':'Manga'}</h2><p>${ru()?'CBZ/ZIP, чтение справа налево. OCR можно сделать заранее для всей книги.':'CBZ/ZIP, right-to-left reading. OCR can be precomputed for the whole book.'}</p></div><button id="mangaImport" class="primary">${ru()?'Добавить CBZ':'Add CBZ'}</button></div><div class="media-grid">${books.map(book => {const cached=Number(book.ocr_cached_pages||0),total=Number(book.page_count||0),ready=!!book.ocr_complete;const cover=book.cover_url?`<img class="media-card-art" src="${esc(book.cover_url)}" alt="" style="object-fit:cover">`:`<div class="media-card-art">漫</div>`;const anilist=book.anilist_id?`<button data-media-action="open-manga-anilist" data-url="${esc(book.site_url||`https://anilist.co/manga/${book.anilist_id}`)}">AniList</button><button data-media-action="find-manga-anilist" data-id="${book.id}" data-title="${esc(book.title)}">${ru()?'Изменить связь':'Change link'}</button>`:`<button data-media-action="find-manga-anilist" data-id="${book.id}" data-title="${esc(book.title)}">${ru()?'Найти в AniList':'Find on AniList'}</button>`;return `<article class="media-card">${cover}<strong>${esc(book.title)}</strong><span>${Number(book.position||0)+1} / ${book.page_count}</span><button data-media-action="read-manga" data-id="${book.id}">${ru()?'Читать':'Read'}</button>${anilist}<button data-media-action="ocr-all-manga" data-id="${book.id}" ${ready?'disabled':''}>${ready?(ru()?'OCR готов':'OCR ready'):(cached?`OCR ${cached}/${total}`:(ru()?'OCR всей манги':'OCR whole manga'))}</button></article>`}).join('')}</div>${books.length?'':`<div class="empty">${ru()?'Добавьте CBZ или ZIP с изображениями страниц.':'Add a CBZ or ZIP containing page images.'}</div>`}`;
  };

  const savedAudioSpeed = bookId => {
    const key=`pudge.audiobook.speed.${Number(bookId)}`;
    const value=Number(localStorage.getItem(key)||1);
    return [0.75,1,1.25,1.5,1.75,2,2.5,3].includes(value)?value:1;
  };
  const audioSpeed = bookId => {
    const book=(audioState.books||[]).find(item=>Number(item.id)===Number(bookId));
    const value=Number(book?.speed??savedAudioSpeed(bookId));
    return [0.75,1,1.25,1.5,1.75,2,2.5,3].includes(value)?value:1;
  };
  const formatAudioTime = value => {
    const total = Math.max(0, Math.floor(Number(value || 0)));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours > 0
      ? `${hours}:${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}`
      : `${minutes}:${String(seconds).padStart(2,'0')}`;
  };
  const audioPreparation = transcription => {
    const status=String(transcription?.status||'');
    if (!status || transcription?.ready) return '';
    if (status==='error') return `<div class="audiobook-preparation danger">${ru()?'Разбор аудио завершился ошибкой':'Audio analysis failed'}${transcription.error?`: ${esc(transcription.error)}`:''}</div>`;
    const percent=Math.max(0,Math.min(100,Math.round(Number(transcription.progress_percent||0))));
    const label=status==='queued'?(ru()?'Разбор аудио ожидает запуска':'Audio analysis is queued'):(ru()?'Разбор аудио':'Audio analysis');
    return `<div class="audiobook-preparation"><div><strong>${label}</strong><span>${percent}%</span></div><progress max="100" value="${percent}"></progress></div>`;
  };
  const nextPaint = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

  const renderAudio = () => {
    const root = $('audiobooksContent');
    if (!root) return;
    const openChapterBooks=new Set(
      [...root.querySelectorAll('.audiobook-chapters[open][data-book-id]')]
        .map(node=>Number(node.dataset.bookId))
        .filter(Number.isFinite)
    );
    const books = audioState.books || [];
    const fileBusy=audioImportBusy==='file',folderBusy=audioImportBusy==='folder';
    root.innerHTML = `<div class="media-head"><div><h2>${ru()?'Аудиокниги':'Audiobooks'}</h2><p>${ru()?'Продолжайте с сохранённого места, меняйте скорость и переходите по главам.':'Resume where you left off, change speed, and jump between chapters.'}</p></div><div class="setting-inline-actions"><button id="audiobookImport" class="primary" ${audioImportBusy?'disabled':''}>${fileBusy?(ru()?'Добавляю…':'Adding…'):(ru()?'Добавить файл':'Add file')}</button><button id="audiobookImportFolder" ${audioImportBusy?'disabled':''}>${folderBusy?(ru()?'Сканирую папку…':'Scanning folder…'):(ru()?'Добавить папку':'Add folder')}</button></div></div>${audioImportBusy?`<div class="audiobook-import-status"><span class="audiobook-pulse"></span>${folderBusy?(ru()?'Читаю файлы и главы. Большие папки могут занять немного времени.':'Reading files and chapters. Large folders can take a little while.'):(ru()?'Читаю метаданные аудиокниги…':'Reading audiobook metadata…')}</div>`:''}<div class="audiobook-list">${books.map(book => {
      const pct=book.duration?Math.min(100,book.position/book.duration*100):0;
      const remaining=Math.max(0,Number(book.duration||0)-Number(book.position||0));
      const speed=Number(book.speed??audioSpeed(book.id));
      const play=book.playing?`<button class="primary" data-media-action="stop-audio" data-id="${book.id}">${ru()?'Стоп':'Stop'}</button>`:`<button class="primary" data-media-action="play-audio" data-id="${book.id}">${book.position>1?(ru()?'Продолжить':'Resume'):(ru()?'Слушать':'Play')}</button>`;
      const speeds=[0.75,1,1.25,1.5,1.75,2,2.5,3].map(value=>`<option value="${value}" ${value===speed?'selected':''}>${value}×</option>`).join('');
      const bookmarks=(book.bookmarks||[]).map(mark=>`<button class="audiobook-bookmark" data-media-action="seek-audio-to" data-id="${book.id}" data-position="${Number(mark.position||0)}">${esc(mark.title||formatAudioTime(mark.position))}<span data-media-action="delete-audio-bookmark" data-bookmark-id="${Number(mark.id)}">×</span></button>`).join('');
      return `<article class="audiobook-card ${book.playing?'playing':''}"><div class="audiobook-main"><div class="audiobook-title-row"><strong>${esc(book.title)}</strong>${book.playing?`<span class="audiobook-live"><i></i>${ru()?'Играет':'Playing'}</span>`:''}</div><span>${formatAudioTime(book.position)} / ${formatAudioTime(book.duration)} · ${Math.round(pct)}%${remaining>0?` · ${ru()?'осталось':'left'} ${formatAudioTime(remaining)}`:''}${book.current_chapter?` · ${esc(book.current_chapter.title)}`:''}${book.multi_file?` · ${book.file_count} ${ru()?'файлов':'files'}`:''}</span><div class="audiobook-scrubber-shell" data-audio-timeline data-id="${book.id}" data-duration="${Math.max(1,Number(book.duration||1))}"><input class="audiobook-scrubber" type="range" min="0" max="${Math.max(1,Number(book.duration||1))}" step="1" value="${Number(book.position||0)}" data-audio-position data-id="${book.id}" aria-label="${ru()?'Позиция':'Position'}"><span class="audiobook-chapter-hover" aria-hidden="true"></span></div></div>${audioPreparation(book.transcription||{})}<div class="audiobook-controls">${play}<button data-media-action="seek-audio" data-id="${book.id}" data-seconds="-15">−15s</button><button data-media-action="seek-audio" data-id="${book.id}" data-seconds="15">+15s</button><button data-media-action="bookmark-audio" data-id="${book.id}">${ru()?'Закладка':'Bookmark'}</button><label class="audiobook-speed"><span>${ru()?'Скорость':'Speed'}</span><select data-audio-speed data-id="${book.id}">${speeds}</select></label><label class="audiobook-speed"><span>${ru()?'Таймер':'Sleep'}</span><select data-audio-sleep data-id="${book.id}"><option value="off">—</option><option value="15">15m</option><option value="30">30m</option><option value="45">45m</option><option value="60">60m</option><option value="chapter">${ru()?'До конца главы':'End of chapter'}</option></select></label><button data-media-action="finish-audio" data-id="${book.id}" data-finished="${book.finished?'0':'1'}">${book.finished?(ru()?'Сбросить':'Reset'):(ru()?'Завершить':'Finish')}</button><button class="danger-action" data-media-action="delete-audio" data-id="${book.id}">${ru()?'Удалить':'Remove'}</button></div>${bookmarks?`<div class="audiobook-bookmarks">${bookmarks}</div>`:''}${(book.chapters||[]).length?`<details class="audiobook-chapters" data-book-id="${Number(book.id)}" ${openChapterBooks.has(Number(book.id))?'open':''}><summary><span class="audiobook-chapters-label"><i>›</i>${ru()?'Главы':'Chapters'}</span><b>${book.chapters.length}</b><span class="audiobook-chapters-open">${ru()?'Показать':'Show'}</span><span class="audiobook-chapters-close">${ru()?'Скрыть':'Hide'}</span></summary><div class="chapter-list">${book.chapters.map((chapter,index,chapters)=>{const chapterStart=Math.max(0,Number(chapter.start||0)),chapterEnd=Math.max(chapterStart,Number(chapters[index+1]?.start??book.duration??chapterStart));return `<button data-media-action="play-audio" data-id="${book.id}" data-start="${chapterStart}" data-audio-chapter-start="${chapterStart}" data-audio-chapter-end="${chapterEnd}">${esc(chapter.title)}</button>`;}).join('')}</div></details>`:''}</article>`
    }).join('')}</div>${books.length?'':`<div class="empty">${ru()?'Поддерживаются отдельные аудиофайлы и папки, где каждый файл — отдельная глава.':'Single audio files and folders where each file is a chapter are supported.'}</div>`}`;
    [...root.querySelectorAll('.audiobook-controls')].forEach((controls,index)=>{const book=books[index];if(!book)return;const card=controls.closest('.audiobook-card'),before=controls.querySelector('.danger-action');if(book.cover_url&&card){card.classList.add('has-cover');const cover=document.createElement(book.anilist_id?'button':'div');cover.className='audiobook-cover';if(book.anilist_id){cover.dataset.mediaAction='open-audio-cover';cover.dataset.url=String(book.anilist_site_url||`https://anilist.co/manga/${book.anilist_id}`);cover.title='AniList';}cover.innerHTML=`<img src="${esc(book.cover_url)}" alt="">`;card.prepend(cover);}const identity=document.createElement('button');identity.dataset.mediaAction='identify-audio';identity.dataset.id=String(book.id);identity.dataset.title=String(book.title||'');identity.textContent=book.anilist_id?(ru()?'Изменить AniList':'Change AniList'):'AniList';controls.insertBefore(identity,before);if(!book.linked_light_novel){const search=document.createElement('button');search.dataset.mediaAction='find-audio-ln';search.dataset.id=String(book.id);search.textContent=ru()?'Найти LN на Nyaa':'Find LN on Nyaa';controls.insertBefore(search,before);}});
  };

  const loadManga = async () => {
    mangaState = await pywebview.api.manga_state();
    counts.manga = (mangaState.books||[]).length;
    // Manga Reader v2 owns #mangaContent. Rendering the legacy card grid first
    // caused a one-frame flash of its much larger artwork on every tab open.
    if (!window.PudgeMangaReaderV2?.renderLibrary) renderManga();
    window.updateCount?.();
  };
  const loadAudio = async () => {
    audioState = await pywebview.api.audiobook_state();
    counts.audiobooks = (audioState.books||[]).length;
    renderAudio();
    window.updateCount?.();
    if (audioPollTimer) clearTimeout(audioPollTimer);
    const active=document.querySelector('.nav button[data-page="audiobooks"]')?.classList.contains('active');
    const playing=(audioState.books||[]).some(book=>book.playing);
    const busy=(audioState.books||[]).some(book=>book.playing||['queued','transcribing'].includes(String(book.transcription?.status||'')));
    if (active && busy) {
      audioPollTimer=setTimeout(()=>void loadAudio(),playing?750:1200);
    }
  };

  const ensureReader = () => {
    if ($('mangaReader')) return $('mangaReader');
    const reader = document.createElement('div');
    reader.id = 'mangaReader';
    reader.className = 'manga-reader';
    reader.innerHTML = `<div class="manga-toolbar"><button data-media-action="close-manga">← ${ru()?'Библиотека':'Library'}</button><strong id="mangaReaderTitle"></strong><span id="mangaPageLabel"></span><span class="spacer"></span><button data-media-action="ocr-all-current-manga">${ru()?'OCR всё':'OCR all'}</button><button data-media-action="ocr-manga">OCR</button></div><div class="manga-page"><button class="manga-turn left" data-media-action="next-page" aria-label="Next page">‹</button><img id="mangaPageImage" alt=""><button class="manga-turn right" data-media-action="previous-page" aria-label="Previous page">›</button></div><aside id="mangaOcrText" class="manga-ocr-text"></aside>`;
    document.body.append(reader);
    return reader;
  };

  const clearMangaOcrPanel = () => {
    const panel = $('mangaOcrText');
    if (!panel) return;
    panel.classList.remove('open');
    panel.textContent = '';
    delete panel.dataset.pageIndex;
  };

  const showPage = async index => {
    if (!openManga) return;
    const total = Math.max(1, Number(openManga.page_count||1));
    const requested = Math.max(0, Math.min(Number(index)||0, total-1));
    const token = ++pageLoadToken;
    ++ocrRequestToken;
    openPage = requested;
    clearMangaOcrPanel();
    const image = $('mangaPageImage');
    if (image) { image.removeAttribute('src'); delete image.dataset.pageIndex; }
    $('mangaPageLabel').textContent = `${requested+1} / ${total}`;
    const page = await pywebview.api.manga_page(openManga.id, requested);
    if (token !== pageLoadToken || !openManga || Number(page.book_id)!==Number(openManga.id)) return;
    openPage = Number(page.page_index);
    if (image) { image.src = page.data_uri; image.dataset.pageIndex = String(openPage); }
    $('mangaPageLabel').textContent = `${openPage+1} / ${page.page_count}`;
  };

  const pollWholeMangaOcr = async bookId => {
    try {
      const status = await pywebview.api.manga_ocr_book_status(Number(bookId));
      const button = document.querySelector(`[data-media-action="ocr-all-manga"][data-id="${Number(bookId)}"]`);
      if (button) {
        const done=Number(status.cached_pages||0), total=Number(status.total_pages||0);
        button.textContent=status.complete?(ru()?'OCR готов':'OCR ready'):`OCR ${done}/${total}`;
        button.disabled=!!status.running||!!status.complete;
      }
      if (status.running) {
        const timer=setTimeout(()=>void pollWholeMangaOcr(bookId),700);
        mangaOcrPollers.set(Number(bookId),timer);
      } else {
        mangaOcrPollers.delete(Number(bookId));
        await loadManga();
        if (status.errors?.length) window.toast?.(status.errors.slice(0,2).join(' • '));
      }
    } catch (error) {
      mangaOcrPollers.delete(Number(bookId));
      window.toast?.(String(error?.message||error));
    }
  };

  const startWholeMangaOcr = async bookId => {
    const id=Number(bookId);
    if (mangaOcrPollers.has(id)) return;
    await pywebview.api.start_manga_ocr_book(id);
    void pollWholeMangaOcr(id);
  };

  const showMangaAniListSearch = async (bookId, title) => {
    if (window.showMediaIdentity) return window.showMediaIdentity('manga', Number(bookId), title || '');
    mangaAniListBookId = Number(bookId);
    mangaAniListResults = [];
    const modalTitle = $('modalTitle'), modalBody = $('modalBody'), backdrop = $('modalBackdrop');
    if (!modalTitle || !modalBody || !backdrop) return;
    modalTitle.textContent = ru() ? 'Найти мангу в AniList' : 'Find Manga on AniList';
    modalBody.innerHTML = `<div class="empty">${ru()?'Ищу AniList…':'Searching AniList…'}</div>`;
    backdrop.classList.add('open');
    let unlink = '';
    try {
      mangaState = await pywebview.api.manga_state() || mangaState;
      const current = (mangaState.books || []).find(book => Number(book.id) === mangaAniListBookId);
      unlink = current?.anilist_id
        ? `<div class="manga-anilist-unlink"><span>${ru()?'Текущая связь':'Current link'}: ${esc(current.series_title || current.title || '')}</span><button class="danger-action" data-media-action="unlink-manga-anilist">${ru()?'Отвязать от AniList':'Unlink from AniList'}</button></div>`
        : '';
      modalBody.innerHTML = unlink + `<div class="empty">${ru()?'Ищу AniList…':'Searching AniList…'}</div>`;
    } catch (_) {}
    try {
      mangaAniListResults = await pywebview.api.manga_search_anilist(title || '') || [];
      const matches = mangaAniListResults.length
        ? mangaAniListResults.map((row,index) => `<div class="ln-nyaa-row"><span class="title" title="${esc(row.title||'')}">${esc(row.title||'')}</span><span>${esc(row.format||'')}</span><span>${row.volumes?`${row.volumes} vol`:''}</span><button data-media-action="bind-manga-anilist" data-index="${index}">${ru()?'Связать':'Link'}</button></div>`).join('')
        : `<div class="empty">${ru()?'Ничего не найдено.':'No matching manga found.'}</div>`;
      modalBody.innerHTML = unlink + matches;
    } catch (error) {
      modalBody.innerHTML = unlink + `<div class="empty danger">${esc(error?.message || error)}</div>`;
    }
  };

  document.addEventListener('click', async event => {
    if (event.target.id === 'mangaImport') {
      const result = await pywebview.api.choose_manga_file();
      if (result.errors?.length) window.toast?.(result.errors.join(' • '));
      if (!result.cancelled) {
        mangaState = result.state; counts.manga = mangaState.books.length; renderManga();
        const first=(result.books||[]).find(book=>!book.anilist_id);
        if(first) await showMangaAniListSearch(first.id, first.title);
      }
      return;
    }
    if (event.target.id === 'audiobookImport') {
      audioImportBusy='file';renderAudio();await nextPaint();
      try {
        const result = await pywebview.api.choose_audiobook_file();
        if (result.errors?.length) window.toast?.(result.errors.join(' • '));
        if (!result.cancelled) { audioState = result.state; counts.audiobooks = audioState.books.length; }
      } finally { audioImportBusy='';renderAudio(); }
      return;
    }
    if (event.target.id === 'audiobookImportFolder') {
      audioImportBusy='folder';renderAudio();await nextPaint();
      try {
        const result = await pywebview.api.choose_audiobook_folder();
        if (result.error) window.toast?.(result.error);
        if (!result.cancelled) { audioState = result.state; counts.audiobooks = audioState.books.length; }
      } finally { audioImportBusy='';renderAudio(); }
      return;
    }
    const action = event.target.closest('[data-media-action]');
    if (!action) return;
    const type = action.dataset.mediaAction;
    if (type === 'open-audio-cover') {
      if (action.dataset.url) await pywebview.api.open_url(action.dataset.url);
      return;
    }
    if (type === 'find-manga-anilist') {
      await showMangaAniListSearch(Number(action.dataset.id), action.dataset.title || '');
      return;
    }
    if (type === 'identify-audio') {
      await window.showMediaIdentity?.('audiobook', Number(action.dataset.id), action.dataset.title || '');
      return;
    }
    if (type === 'find-audio-ln') {
      $('modalTitle').textContent=ru()?'Ранобэ на Nyaa':'Light Novel on Nyaa';
      $('modalBody').innerHTML=`<div class="empty">${ru()?'Ищу релизы…':'Searching releases…'}</div>`;
      $('modalBackdrop').classList.add('open');
      const result=await pywebview.api.audiobook_search_light_novel_nyaa(Number(action.dataset.id));
      audioLnNyaaRows=result.releases||[];
      const rows=audioLnNyaaRows.map((row,index)=>`<div class="ln-nyaa-row"><span class="title" title="${esc(row.title||'')}">${esc(row.title||'')}</span><span>${Number(row.seeders||0)} seeds · ${esc(row.size||'')}</span><button data-media-action="download-audio-ln-release" data-index="${index}">${ru()?'Скачать':'Download'}</button></div>`).join('');
      $('modalBody').innerHTML=rows||`<div class="empty">${ru()?'Ничего не найдено':'No releases found'}</div>`;
      return;
    }
    if (type === 'download-audio-ln-release') {
      const release=audioLnNyaaRows[Number(action.dataset.index)];
      if(!release)return;
      await pywebview.api.light_novel_download_nyaa(release);
      window.toast?.(ru()?'Торрент ранобэ добавлен':'Light Novel torrent added');
      return;
    }
    if (type === 'open-manga-anilist') {
      if (action.dataset.url) await pywebview.api.open_url(action.dataset.url);
      return;
    }
    if (type === 'bind-manga-anilist') {
      const row=mangaAniListResults[Number(action.dataset.index)];
      if(row&&mangaAniListBookId){
        const result=await pywebview.api.manga_bind_anilist(mangaAniListBookId,Number(row.media_id),row);
        mangaState=result.state||await pywebview.api.manga_state();
        counts.manga=(mangaState.books||[]).length;
        if (window.PudgeMangaReaderV2?.renderLibrary) await window.PudgeMangaReaderV2.renderLibrary();
        else renderManga();
        window.updateCount?.();
        $('modalBackdrop')?.classList.remove('open');
        window.toast?.(ru()?'AniList связан':'AniList linked');
      }
      return;
    }
    if (type === 'unlink-manga-anilist' && mangaAniListBookId) {
      const result=await pywebview.api.manga_unbind_anilist(mangaAniListBookId);
      mangaState=result.state||await pywebview.api.manga_state();
      counts.manga=(mangaState.books||[]).length;
      if (window.PudgeMangaReaderV2?.renderLibrary) await window.PudgeMangaReaderV2.renderLibrary();
      else renderManga();
      window.updateCount?.();
      $('modalBackdrop')?.classList.remove('open');
      window.toast?.(ru()?'Серия отвязана от AniList':'Series unlinked from AniList');
      return;
    }
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
    if (type === 'ocr-all-manga') { await startWholeMangaOcr(Number(action.dataset.id)); }
    if (type === 'ocr-all-current-manga' && openManga) { await startWholeMangaOcr(Number(openManga.id)); }
    if (type === 'ocr-manga') {
      const image = $('mangaPageImage');
      const requestedPage = Number(openPage);
      if (!openManga || Number(image?.dataset.pageIndex) !== requestedPage) {
        window.toast?.(ru()?'Страница ещё загружается':'Page is still loading');
        return;
      }
      const requestId = ++ocrRequestToken;
      const requestedBook = Number(openManga.id);
      const panel = $('mangaOcrText');
      panel.textContent = ru()?'Распознаю…':'Recognizing…';
      panel.dataset.pageIndex = String(requestedPage);
      panel.classList.add('open');
      const result = await pywebview.api.manga_ocr_page(requestedBook, requestedPage);
      if (requestId !== ocrRequestToken || !openManga || Number(openManga.id)!==requestedBook || Number(openPage)!==requestedPage || Number(result.page_index)!==requestedPage) return;
      panel.dataset.pageIndex = String(requestedPage);
      panel.textContent = result.text || result.error || (ru()?'Текст не найден':'No text found');
    }
    if (type === 'play-audio') {
      const id=Number(action.dataset.id);await pywebview.api.audiobook_play(id, action.dataset.start==null?null:Number(action.dataset.start), audioSpeed(id));
      await loadAudio(); window.toast?.(ru()?'Воспроизведение запущено':'Playback started');
    }
    if (type === 'seek-audio') {
      const result=await pywebview.api.audiobook_seek(Number(action.dataset.id),Number(action.dataset.seconds||0));audioState=result.state||await pywebview.api.audiobook_state();renderAudio();
    }
    if (type === 'seek-audio-to') {
      const result=await pywebview.api.audiobook_seek_to(Number(action.dataset.id),Number(action.dataset.position||0));audioState=result.state||await pywebview.api.audiobook_state();renderAudio();
    }
    if (type === 'bookmark-audio') {
      const result=await pywebview.api.audiobook_add_bookmark(Number(action.dataset.id),'');audioState=result.state||await pywebview.api.audiobook_state();renderAudio();
    }
    if (type === 'delete-audio-bookmark') {
      const result=await pywebview.api.audiobook_delete_bookmark(Number(action.dataset.bookmarkId));audioState=result.state||await pywebview.api.audiobook_state();renderAudio();
    }
    if (type === 'finish-audio') {
      const result=await pywebview.api.audiobook_mark_finished(Number(action.dataset.id),action.dataset.finished==='1');audioState=result.state||await pywebview.api.audiobook_state();renderAudio();
    }
    if (type === 'stop-audio') {
      const result=await pywebview.api.audiobook_stop(Number(action.dataset.id));audioState=result.state||await pywebview.api.audiobook_state();renderAudio();window.toast?.(ru()?'Остановлено; позиция сохранена':'Stopped; position saved');
    }
    if (type === 'delete-audio') {
      if(!await pudgeConfirm(ru()?'Удалить аудиокнигу из Pudge? Исходные файлы останутся на диске.':'Remove this audiobook from Pudge? Source files will stay on disk.'))return;
      const result=await pywebview.api.audiobook_delete(Number(action.dataset.id));audioState=result.state||{books:[]};counts.audiobooks=(audioState.books||[]).length;renderAudio();window.updateCount?.();
    }
  });
  const showAudioChapterHover = chapter => {
    const card=chapter?.closest?.('.audiobook-card');
    const timeline=card?.querySelector?.('[data-audio-timeline]');
    const marker=timeline?.querySelector?.('.audiobook-chapter-hover');
    if(!timeline||!marker)return;
    const duration=Math.max(0,Number(timeline.dataset.duration||0));
    if(!duration)return;
    const start=Math.max(0,Math.min(duration,Number(chapter.dataset.audioChapterStart||0)));
    const end=Math.max(start,Math.min(duration,Number(chapter.dataset.audioChapterEnd||start)));
    if(end<=start){marker.classList.remove('show');return;}
    marker.style.left=`${start/duration*100}%`;
    marker.style.width=`${(end-start)/duration*100}%`;
    marker.classList.add('show');
  };
  const hideAudioChapterHover = chapter => {
    const marker=chapter?.closest?.('.audiobook-card')?.querySelector?.('.audiobook-chapter-hover');
    if(marker)marker.classList.remove('show');
  };
  document.addEventListener('pointerover',event=>{
    const chapter=event.target.closest?.('[data-audio-chapter-start]');
    if(!chapter||chapter.contains(event.relatedTarget))return;
    showAudioChapterHover(chapter);
  });
  document.addEventListener('pointerout',event=>{
    const chapter=event.target.closest?.('[data-audio-chapter-start]');
    if(!chapter||chapter.contains(event.relatedTarget))return;
    hideAudioChapterHover(chapter);
  });

  document.addEventListener('change', async event => {
    const position=event.target.closest?.('[data-audio-position]');
    if(position){const result=await pywebview.api.audiobook_seek_to(Number(position.dataset.id),Number(position.value||0));audioState=result.state||await pywebview.api.audiobook_state();renderAudio();return;}
    const sleep=event.target.closest?.('[data-audio-sleep]');
    if(sleep){const result=await pywebview.api.audiobook_sleep_timer(Number(sleep.dataset.id),sleep.value||'off');audioState=result.state||await pywebview.api.audiobook_state();renderAudio();return;}
    const control=event.target.closest?.('[data-audio-speed]');
    if(!control)return;
    const id=Number(control.dataset.id),speed=Number(control.value||1);
    const result=await pywebview.api.audiobook_set_speed(id,speed);audioState=result.state||await pywebview.api.audiobook_state();renderAudio();
  });

  document.addEventListener('keydown', event => {
    if (!$('mangaReader')?.classList.contains('open')) return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); void showPage(openPage + 1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); void showPage(openPage - 1); }
    if (event.key === 'Escape') $('mangaReader').classList.remove('open');
  });

  const mangaOcrLabels = state => {
    const language = ru();
    const labels = language ? {
      not_installed:'Не установлен', starting:'Запуск…', installing_package:'Устанавливаю пакет…',
      package_installed:'Пакет установлен; модель не загружена', downloading_model:'Скачиваю модель…',
      ready:'Готов', failed:'Ошибка', idle:'Не установлен',
    } : {
      not_installed:'Not installed', starting:'Starting…', installing_package:'Installing package…',
      package_installed:'Package installed; model not downloaded', downloading_model:'Downloading model…',
      ready:'Ready', failed:'Failed', idle:'Not installed',
    };
    return labels[state] || state || '—';
  };

  const refreshMangaOcrStatus = async () => {
    const statusNode = $('mangaOcrStatus');
    if (!statusNode || !window.pywebview?.api?.manga_ocr_status) return null;
    try {
      const status = await pywebview.api.manga_ocr_status();
      statusNode.textContent = mangaOcrLabels(status.state);
      const detail = $('mangaOcrDetail');
      if (detail) detail.textContent = status.detail || '';
      const install = $('installMangaOcr');
      if (install) {
        install.disabled = !!status.running || status.state === 'ready';
        install.hidden = status.state === 'ready';
      }
      const log = $('openMangaOcrLog');
      if (log) log.hidden = !status.log_path || status.state === 'not_installed';
      if (status.running) setTimeout(() => void refreshMangaOcrStatus(), 1200);
      return status;
    } catch (error) {
      const detail = $('mangaOcrDetail');
      if (detail) detail.textContent = String(error?.message || error);
      return null;
    }
  };

  document.addEventListener('click', async event => {
    if (event.target.id === 'installMangaOcr') {
      event.target.disabled = true;
      try {
        await pywebview.api.install_manga_ocr();
        await refreshMangaOcrStatus();
      } catch (error) {
        const detail = $('mangaOcrDetail');
        if (detail) detail.textContent = String(error?.message || error);
        event.target.disabled = false;
      }
    }
    if (event.target.id === 'openMangaOcrLog') {
      await pywebview.api.reveal_manga_ocr_install_log();
    }
  });

  window.PudgeMedia = {counts, loadManga, loadAudio, refreshMangaOcrStatus, showMangaAniListSearch};
})();
