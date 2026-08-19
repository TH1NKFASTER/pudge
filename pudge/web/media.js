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
  let activeAudioBookId = null;
  let mangaAniListBookId = null;
  let mangaAniListResults = [];
  let audioLnNyaaRows = [];

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
    const fileBusy=audioImportBusy==='file';
    root.innerHTML = `<button id="audiobookImport" hidden ${audioImportBusy?'disabled':''}>Import</button>${audioImportBusy?`<div class="audiobook-import-status"><span class="audiobook-pulse"></span>${ru()?'Читаю метаданные аудиокниги…':'Reading audiobook metadata…'}</div>`:''}<div class="audiobook-list">${books.map(book => {
      const pct=book.duration?Math.min(100,book.position/book.duration*100):0;
      const remaining=Math.max(0,Number(book.duration||0)-Number(book.position||0));
      const speed=Number(book.speed??audioSpeed(book.id));
      const play=book.playing?`<button class="primary" data-media-action="stop-audio" data-id="${book.id}">${ru()?'Стоп':'Stop'}</button>`:`<button class="primary" data-media-action="play-audio" data-id="${book.id}">${book.position>1?(ru()?'Продолжить':'Resume'):(ru()?'Слушать':'Play')}</button>`;
      const speeds=[0.75,1,1.25,1.5,1.75,2,2.5,3].map(value=>`<option value="${value}" ${value===speed?'selected':''}>${value}×</option>`).join('');
      const bookmarks=(book.bookmarks||[]).map(mark=>`<button class="audiobook-bookmark" data-media-action="seek-audio-to" data-id="${book.id}" data-position="${Number(mark.position||0)}">${esc(mark.title||formatAudioTime(mark.position))}<span data-media-action="delete-audio-bookmark" data-bookmark-id="${Number(mark.id)}">×</span></button>`).join('');
      return `<article class="audiobook-card ${book.playing?'playing':''}" data-audiobook-id="${Number(book.id)}"><div class="audiobook-main"><div class="audiobook-title-row"><strong>${esc(book.title)}</strong>${book.playing?`<span class="audiobook-live"><i></i>${ru()?'Играет':'Playing'}</span>`:''}</div><span>${formatAudioTime(book.position)} / ${formatAudioTime(book.duration)} · ${Math.round(pct)}%${remaining>0?` · ${ru()?'осталось':'left'} ${formatAudioTime(remaining)}`:''}${book.current_chapter?` · ${esc(book.current_chapter.title)}`:''}${book.multi_file?` · ${book.file_count} ${ru()?'файлов':'files'}`:''}</span><div class="audiobook-scrubber-shell" data-audio-timeline data-id="${book.id}" data-duration="${Math.max(1,Number(book.duration||1))}"><input class="audiobook-scrubber" type="range" min="0" max="${Math.max(1,Number(book.duration||1))}" step="1" value="${Number(book.position||0)}" data-audio-position data-id="${book.id}" aria-label="${ru()?'Позиция':'Position'}"><span class="audiobook-chapter-hover" aria-hidden="true"></span></div></div>${audioPreparation(book.transcription||{})}<div class="audiobook-controls">${play}<button data-media-action="seek-audio" data-id="${book.id}" data-seconds="-15">−15s</button><button data-media-action="seek-audio" data-id="${book.id}" data-seconds="15">+15s</button><button data-media-action="bookmark-audio" data-id="${book.id}">${ru()?'Закладка':'Bookmark'}</button><label class="audiobook-speed"><span>${ru()?'Скорость':'Speed'}</span><select data-audio-speed data-id="${book.id}">${speeds}</select></label><label class="audiobook-speed"><span>${ru()?'Таймер':'Sleep'}</span><select data-audio-sleep data-id="${book.id}"><option value="off">—</option><option value="15">15m</option><option value="30">30m</option><option value="45">45m</option><option value="60">60m</option><option value="chapter">${ru()?'До конца главы':'End of chapter'}</option></select></label><button data-media-action="finish-audio" data-id="${book.id}" data-finished="${book.finished?'0':'1'}">${book.finished?(ru()?'Сбросить':'Reset'):(ru()?'Завершить':'Finish')}</button><button class="danger-action" data-media-action="delete-audio" data-id="${book.id}">${ru()?'Удалить':'Remove'}</button></div>${bookmarks?`<div class="audiobook-bookmarks">${bookmarks}</div>`:''}${(book.chapters||[]).length?`<details class="audiobook-chapters" data-book-id="${Number(book.id)}" ${openChapterBooks.has(Number(book.id))?'open':''}><summary><span class="audiobook-chapters-label"><i>›</i>${ru()?'Главы':'Chapters'}</span><b>${book.chapters.length}</b><span class="audiobook-chapters-open">${ru()?'Показать':'Show'}</span><span class="audiobook-chapters-close">${ru()?'Скрыть':'Hide'}</span></summary><div class="chapter-list">${book.chapters.map((chapter,index,chapters)=>{const chapterStart=Math.max(0,Number(chapter.start||0)),chapterEnd=Math.max(chapterStart,Number(chapters[index+1]?.start??book.duration??chapterStart));return `<button data-media-action="play-audio" data-id="${book.id}" data-start="${chapterStart}" data-audio-chapter-start="${chapterStart}" data-audio-chapter-end="${chapterEnd}">${esc(chapter.title)}</button>`;}).join('')}</div></details>`:''}</article>`
    }).join('')}</div>${books.length?'':`<div class="empty">${ru()?'Поддерживаются отдельные аудиофайлы и папки, где каждый файл — отдельная глава.':'Single audio files and folders where each file is a chapter are supported.'}</div>`}`;
    [...root.querySelectorAll('.audiobook-controls')].forEach((controls,index)=>{const book=books[index];if(!book)return;const card=controls.closest('.audiobook-card'),before=controls.querySelector('.danger-action');if(book.cover_url&&card){card.classList.add('has-cover');const cover=document.createElement(book.anilist_id?'button':'div');cover.className='audiobook-cover';if(book.anilist_id){cover.dataset.mediaAction='open-audio-cover';cover.dataset.url=String(book.anilist_site_url||`https://anilist.co/manga/${book.anilist_id}`);cover.title='AniList';}cover.innerHTML=`<img src="${esc(book.cover_url)}" alt="">`;card.prepend(cover);}const identity=document.createElement('button');identity.dataset.mediaAction='identify-audio';identity.dataset.id=String(book.id);identity.dataset.title=String(book.title||'');identity.textContent=book.anilist_id?(ru()?'Изменить AniList':'Change AniList'):'AniList';controls.insertBefore(identity,before);if(!book.linked_light_novel){const search=document.createElement('button');search.dataset.mediaAction='find-audio-ln';search.dataset.id=String(book.id);search.textContent=ru()?'Найти LN на Nyaa':'Find LN on Nyaa';controls.insertBefore(search,before);}});
  };

  const loadManga = async () => {
    mangaState = await pywebview.api.manga_state();
    counts.manga = (mangaState.books || []).length;
    await window.PudgeMangaReaderV2?.renderLibrary?.();
    window.updateCount?.();
  };
  const loadAudio = async () => {
    audioState = await pywebview.api.audiobook_state();
    counts.audiobooks = (audioState.books||[]).length;
    const playingBook=(audioState.books||[]).find(book=>book.playing);
    if(playingBook)activeAudioBookId=Number(playingBook.id);
    else if(activeAudioBookId==null&&(audioState.books||[]).length)activeAudioBookId=Number(audioState.books[0].id);
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

  const audiobookById = id =>
    (audioState.books||[]).find(book=>Number(book.id)===Number(id))||null;

  const optimisticAudioPlaying = (id, playing) => {
    activeAudioBookId=Number(id);
    const book=audiobookById(id);
    if(book)book.playing=!!playing;
    renderAudio();
    return book;
  };

  const playAudiobook = async (id, start=null, notify=true) => {
    id=Number(id);
    const book=audiobookById(id),previous=!!book?.playing;
    optimisticAudioPlaying(id,true);
    try{
      await pywebview.api.audiobook_play(
        id,
        start==null?null:Number(start),
        audioSpeed(id)
      );
      await loadAudio();
      if(notify)window.toast?.(ru()?'Воспроизведение запущено':'Playback started');
    }catch(error){
      if(book)book.playing=previous;
      renderAudio();
      throw error;
    }
  };

  const stopAudiobook = async (id, notify=true) => {
    id=Number(id);
    const book=audiobookById(id),previous=!!book?.playing;
    optimisticAudioPlaying(id,false);
    try{
      const result=await pywebview.api.audiobook_stop(id);
      audioState=result.state||await pywebview.api.audiobook_state();
      renderAudio();
      if(notify)window.toast?.(
        ru()?'Остановлено; позиция сохранена':'Stopped; position saved'
      );
    }catch(error){
      if(book)book.playing=previous;
      renderAudio();
      throw error;
    }
  };

  const seekAudiobook = async (id, seconds) => {
    id=Number(id);
    activeAudioBookId=id;
    const result=await pywebview.api.audiobook_seek(id,Number(seconds||0));
    audioState=result.state||await pywebview.api.audiobook_state();
    renderAudio();
  };

  const activeAudiobook = () => {
    const books=audioState.books||[];
    return books.find(book=>book.playing)
      ||books.find(book=>Number(book.id)===Number(activeAudioBookId))
      ||books[0]
      ||null;
  };

  const toggleActiveAudiobook = async () => {
    const book=activeAudiobook();
    if(!book)return;
    if(book.playing)await stopAudiobook(book.id,false);
    else await playAudiobook(book.id,null,false);
  };

  const closeAudioImportMenu = () => {
    const menu=$('audiobookImportMenu');
    if(menu)menu.classList.remove('open');
  };

  const showAudioImportMenu = () => {
    let menu=$('audiobookImportMenu');
    if(!menu){
      menu=document.createElement('div');
      menu.id='audiobookImportMenu';
      menu.className='pudge-select-menu audiobook-import-menu';
      menu.innerHTML=`<button type="button" class="pudge-select-option" data-audio-import-kind="file">${ru()?'Файл':'File'}</button><button type="button" class="pudge-select-option" data-audio-import-kind="folder">${ru()?'Папка':'Folder'}</button>`;
      document.body.appendChild(menu);
    }
    const anchor=$('pageImportButton');
    const rect=anchor?.getBoundingClientRect();
    menu.classList.add('open');
    if(rect){
      const width=Math.max(160,rect.width);
      menu.style.width=`${width}px`;
      menu.style.left=`${Math.max(8,Math.min(window.innerWidth-width-8,rect.right-width))}px`;
      menu.style.top=`${Math.min(window.innerHeight-110,rect.bottom+5)}px`;
    }
  };

  const importAudiobook = async kind => {
    closeAudioImportMenu();
    audioImportBusy=kind;renderAudio();await nextPaint();
    try{
      const result=kind==='folder'
        ? await pywebview.api.choose_audiobook_folder()
        : await pywebview.api.choose_audiobook_file();
      if(result.error)window.toast?.(result.error);
      if(result.errors?.length)window.toast?.(result.errors.join(' • '));
      if(!result.cancelled){audioState=result.state;counts.audiobooks=audioState.books.length;}
    }finally{audioImportBusy='';renderAudio();}
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
    if (event.target.id === 'audiobookImport') {
      showAudioImportMenu();
      return;
    }
    const importChoice=event.target.closest?.('[data-audio-import-kind]');
    if(importChoice){await importAudiobook(importChoice.dataset.audioImportKind||'file');return;}
    if(!event.target.closest?.('#audiobookImportMenu')&&!event.target.closest?.('#pageImportButton'))closeAudioImportMenu();
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
        await window.PudgeMangaReaderV2?.renderLibrary?.();
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
      await window.PudgeMangaReaderV2?.renderLibrary?.();
      window.updateCount?.();
      $('modalBackdrop')?.classList.remove('open');
      window.toast?.(ru()?'Серия отвязана от AniList':'Series unlinked from AniList');
      return;
    }
    if (type === 'play-audio') {
      await playAudiobook(Number(action.dataset.id),action.dataset.start==null?null:Number(action.dataset.start));
    }
    if (type === 'seek-audio') {
      await seekAudiobook(Number(action.dataset.id),Number(action.dataset.seconds||0));
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
      await stopAudiobook(Number(action.dataset.id));
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
    control.blur();
    const result=await pywebview.api.audiobook_set_speed(id,speed);audioState=result.state||await pywebview.api.audiobook_state();renderAudio();
  });

  document.addEventListener('keydown', event => {
    const plain=!event.metaKey&&!event.ctrlKey&&!event.altKey&&!event.shiftKey;
    const editable=!!event.target.closest?.(
      'input,textarea,select,button,[contenteditable="true"]'
    );
    const audioPageActive=document.querySelector(
      '.nav button[data-page="audiobooks"]'
    )?.classList.contains('active');
    if(audioPageActive&&plain&&!editable){
      const audioShortcuts={ArrowLeft:-5,ArrowRight:5,ArrowUp:-15,ArrowDown:15};
      if(event.code==='Space'){
        event.preventDefault();
        event.stopPropagation();
        if(!event.repeat)void toggleActiveAudiobook();
        return;
      }
      if(Object.prototype.hasOwnProperty.call(audioShortcuts,event.key)){
        const book=activeAudiobook();
        if(!book)return;
        event.preventDefault();
        event.stopPropagation();
        if(!event.repeat)void seekAudiobook(book.id,audioShortcuts[event.key]);
        return;
      }
    }
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
      const block = statusNode.closest('.setting-block');
      if (block) {
        block.dataset.settingsCategory = status.state === 'ready' ? 'advanced' : 'essential';
        window.PudgeSettings?.refresh?.();
      }
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
