'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const counts = {manga: 0, audiobooks: 0};
  let mangaState = {books: []};
  let audioState = {books: []};
  let audioImportBusy = '';
  let audioPollTimer = null;
  let audioControlMutation = 0;
  let audioControlEngagedUntil = 0;
  let activeAudioBookId = null;
  let mangaAniListBookId = null;
  let mangaAniListResults = [];
  let audioLnNyaaRows = [];
  let audioContextBookId = null;
  let audioBookmarkContextId = null;
  let audioBookmarkDrag = null;
  let audioBookmarkDragFrame = 0;
  let audioBookmarkSuppressClickUntil = 0;
  const audioBookmarkUndo = [];
  const audioSelection = new Set();
  const emitAudioSelection = () => window.dispatchEvent(new CustomEvent('pudge-audiobook-selection-changed'));
  const audioSeriesBooks = key => {
    const wanted=String(key||'').trim();
    return (audioState.books||[]).filter(book=>String(book.series_key||'').trim()===wanted);
  };
  const toggleAudioSeriesSelection = books => {
    const ids=(books||[]).map(book=>Number(book.id)).filter(Number.isFinite);
    if(!ids.length)return;
    const allSelected=ids.every(id=>audioSelection.has(id));
    for(const id of ids){if(allSelected)audioSelection.delete(id);else audioSelection.add(id);}
    renderAudio();
    emitAudioSelection();
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
  const updateAudioLiveFields = () => {
    for(const book of audioState.books||[]){
      const id=Number(book.id);
      const card=document.querySelector(`.audiobook-card[data-audiobook-id="${id}"]`);
      if(!card)continue;
      const duration=Math.max(0,Number(book.duration||0));
      const position=Math.max(0,Math.min(Number(book.position||0),duration||Number(book.position||0)));
      const pct=duration?Math.min(100,position/duration*100):0;
      const remaining=Math.max(0,duration-position);
      const summary=card.querySelector(`[data-audio-live-summary="${id}"]`);
      if(summary){
        summary.textContent=`${formatAudioTime(position)} / ${formatAudioTime(duration)} · ${Math.round(pct)}%${remaining>0?` · ${PudgeI18n.t('media.timeLeft',{time:formatAudioTime(remaining)})}`:''}${book.current_chapter?` · ${String(book.current_chapter.title||'')}`:''}${book.multi_file?` · ${PudgeI18n.tn('media.fileCount',Number(book.file_count||0))}`:''}`;
      }
      const scrubber=card.querySelector(`[data-audio-position][data-id="${id}"]`);
      if(scrubber&&document.activeElement!==scrubber){
        scrubber.max=String(Math.max(1,duration||1));
        scrubber.value=String(position);
      }
    }
  };

  const formatBookmarkDate = value => {
    const seconds=Number(value||0);
    if(!Number.isFinite(seconds)||seconds<=0)return '';
    return new Date(seconds*1000).toLocaleString(PudgeI18n.lang(),{
      year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'
    });
  };
  const audioBookmarkById = id => {
    for(const book of audioState.books||[]){
      const mark=(book.bookmarks||[]).find(item=>Number(item.id)===Number(id));
      if(mark)return {book,mark};
    }
    return null;
  };
  const rememberDeletedAudioBookmark = found => {
    if(!found?.book||!found?.mark)return;
    audioBookmarkUndo.push({
      bookId:Number(found.book.id),
      bookmark:{
        id:Number(found.mark.id),
        position:Number(found.mark.position||0),
        title:String(found.mark.title||''),
        sort_order:Number(found.mark.sort_order||0),
        created_at:Number(found.mark.created_at||0),
      },
    });
    if(audioBookmarkUndo.length>20)audioBookmarkUndo.shift();
  };
  const deleteAudioBookmark = async bookmarkId => {
    const found=audioBookmarkById(bookmarkId);
    if(!found)return;
    rememberDeletedAudioBookmark(found);
    try{
      const result=await pywebview.api.audiobook_delete_bookmark(Number(bookmarkId));
      applyAudioActionResult(result);
      renderAudio();
      window.toast?.(PudgeI18n.t('media.bookmarkDeletedZToUndo'));
    }catch(error){
      audioBookmarkUndo.pop();
      throw error;
    }
  };
  const undoDeletedAudioBookmark = async () => {
    const entry=audioBookmarkUndo.pop();if(!entry)return false;
    const mark=entry.bookmark;
    try{
      const result=await pywebview.api.audiobook_restore_bookmark(
        Number(entry.bookId),
        Number(mark.position||0),
        String(mark.title||''),
        Number(mark.sort_order||0),
        Number(mark.created_at||0),
        Number(mark.id||0)
      );
      applyAudioActionResult(result);
      renderAudio();
      window.toast?.(PudgeI18n.t('media.bookmarkRestored'));
      return true;
    }catch(error){
      audioBookmarkUndo.push(entry);
      throw error;
    }
  };
  const audioPreparation = transcription => {
    const status=String(transcription?.status||'');
    if (!status || transcription?.ready) return '';
    if (status==='idle') return `<div class="audiobook-preparation idle"><div><strong>${PudgeI18n.t('media.audioAnalysisHasNotStarted')}</strong></div></div>`;
    if (status==='error') return `<div class="audiobook-preparation danger">${PudgeI18n.t('media.audioAnalysisFailed')}${transcription.error?`: ${esc(PudgeI18n.message(String(transcription.error)))}`:''}</div>`;
    if (status==='cancelled') return `<div class="audiobook-preparation idle"><div><strong>${PudgeI18n.t('media.audioAnalysisCancelled')}</strong></div></div>`;
    const percent=Math.max(0,Math.min(100,Math.round(Number(transcription.progress_percent||0))));
    if(status==='queued'){
      const waitReason=String(transcription.wait_reason||'');
      let queue='';
      if(waitReason==='foreground') queue=PudgeI18n.t('media.waitingForPlaybackToFinish');
      else if(waitReason==='energy_saving') queue=PudgeI18n.t('media.pausedEnergySaving');
      else if(waitReason==='thermal') queue=PudgeI18n.t('media.pausedThermalLimit');
      else if(waitReason==='heavy_work') queue=PudgeI18n.t('media.waitingForAudioWorker');
      else {
        const pos=Math.max(0,Number(transcription.queue_position||0)),size=Math.max(pos,Number(transcription.queue_size||0));
        queue=pos?(size?PudgeI18n.t('media.queuePositionOf',{position:pos,size}):PudgeI18n.t('media.queuePosition',{position:pos})):(PudgeI18n.t('media.queued'));
      }
      return `<div class="audiobook-preparation queued"><div><strong>${PudgeI18n.t('ui.audioAnalysis')}</strong><span>${queue}</span></div></div>`;
    }
    return `<div class="audiobook-preparation"><div><strong>${PudgeI18n.t('ui.audioAnalysis')}</strong><span>${percent}%</span></div><progress max="100" value="${percent}"></progress></div>`;
  };
  const nextPaint = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

  // Mirror of the hovered chapter on the sidebar timeline (visual only, no transport).
  let sidebarChapterHover=null,sidebarChapterHoverSeq=0;
  const clearSidebarChapterHover = chapter => {
    const current=sidebarChapterHover;
    if(!current||(chapter&&current.node!==chapter))return;
    sidebarChapterHover=null;
    try{window.PudgeSidebarCompanion?.clearChapterHoverRange?.(current.source);}catch(error){console.debug('sidebar chapter hover',error);}
  };
  const dropDetachedSidebarChapterHover = () => {
    if(sidebarChapterHover&&!sidebarChapterHover.node?.isConnected)clearSidebarChapterHover();
  };
  const renderAudio = () => {
    const root = $('audiobooksContent');
    if (!root) return;
    const openChapterBooks=new Set(
      [...root.querySelectorAll('.audiobook-chapters[open][data-book-id]')]
        .map(node=>Number(node.dataset.bookId))
        .filter(Number.isFinite)
    );
    const seriesScrollPositions=new Map(
      [...root.querySelectorAll('.audiobook-series-scroll[data-series-key]')]
        .map(node=>[String(node.dataset.seriesKey||''),Number(node.scrollTop||0)])
    );
    const books = audioState.books || [];
    const validIds=new Set(books.map(book=>Number(book.id)));
    [...audioSelection].forEach(id=>{if(!validIds.has(Number(id)))audioSelection.delete(id);});
    const selectedCount=audioSelection.size;
    const seriesCounts=new Map();
    for(const book of books){const key=String(book.series_key||'').trim();if(key)seriesCounts.set(key,(seriesCounts.get(key)||0)+1);}
    const seriesOrder=new Map();let seriesOrderNext=0;
    for(const book of books){const key=String(book.series_key||'').trim();if(!seriesOrder.has(key))seriesOrder.set(key,seriesOrderNext++);}
    const booksForRender=[...books].sort((a,b)=>{
      const ak=String(a.series_key||''),bk=String(b.series_key||'');
      const ao=seriesOrder.get(ak)??999999,bo=seriesOrder.get(bk)??999999;if(ao!==bo)return ao-bo;
      const av=Number(a.volume||0),bv=Number(b.volume||0);
      if(av>0||bv>0)return (av>0?av:Number.MAX_SAFE_INTEGER)-(bv>0?bv:Number.MAX_SAFE_INTEGER);
      return String(a.title||'').localeCompare(String(b.title||''),undefined,{numeric:true,sensitivity:'base'});
    });
    const renderBookCard=(book,grouped=false)=>{
      const pct=book.duration?Math.min(100,book.position/book.duration*100):0;
      const remaining=Math.max(0,Number(book.duration||0)-Number(book.position||0));
      const speed=Number(book.speed??audioSpeed(book.id));
      const phoneSource=book.iphone_progress?.selected?.deviceId && book.iphone_progress.selected.deviceId!=='mac';
      const handoffControl='';
      // A short note at the end of the time line (details on hover): the choice is made on the iPhone.
      // Several iPhones/iPads can differ: one yellow mark per device (its name in the tooltip);
      // clicking a mark takes that device's position and resolves the conflict for every device.
      const conflicted=Boolean(book.iphone_progress?.conflicts?.length);
      const phonePositions=conflicted?(Array.isArray(book.iphone_positions)?book.iphone_positions:(book.iphone_position?[book.iphone_position]:[])).filter(item=>item?.checkpoint_id):[];
      const deviceNames=[...new Set(phonePositions.map(item=>String(item.device_name||'')).filter(Boolean))];
      const conflictDetail=esc(PudgeI18n.t('media.positionsDifferChoose'));
      const conflictShort=deviceNames.length?PudgeI18n.t('media.positionDiffersOn',{devices:deviceNames.join(', ')}):PudgeI18n.t('media.positionDiffersElsewhere');
      const syncConflict=conflicted?`<span class="audiobook-sync-conflict" data-tooltip="${conflictDetail}" aria-label="${conflictDetail}">${esc(conflictShort)}</span>`:'';
      const phoneMarker=phonePositions.map(phonePosition=>{
        const duration=Math.max(1,Number(book.duration||1)),at=Math.max(0,Math.min(duration,Number(phonePosition.position||0)));
        const device=String(phonePosition.device_name||PudgeI18n.t('media.anotherDevice'));
        const tip=esc([PudgeI18n.t('media.devicePositionAt',{device,time:formatAudioTime(at)}),formatBookmarkDate(phonePosition.updated_at)].filter(Boolean).join(', '));
        return `<button type="button" class="audiobook-iphone-marker" data-media-action="use-iphone-audio-position" data-id="${Number(book.id)}" data-checkpoint-id="${esc(String(phonePosition.checkpoint_id))}" data-revision="${Number(phonePosition.revision||0)}" data-device="${esc(device)}" data-position="${at}" style="left:${at/duration*100}%" data-tooltip="${tip}" aria-label="${tip}"></button>`;
      }).join('');
      // One Resume button: while the iPhone is playing this book it first
      // pauses the phone and takes its exact position (handoff).
      const play=book.playing?`<button class="primary" data-media-action="stop-audio" data-id="${book.id}">${PudgeI18n.t('media.stop')}</button>`:`<button class="primary" data-media-action="play-audio" data-id="${book.id}" ${book.iphone_live&&!book.iphone_progress?.conflicts?.length?'data-iphone-live="1"':''} data-device="${esc(String(book.iphone_live_name||''))}" title="${book.iphone_live?esc(PudgeI18n.t('media.playingOnDeviceNow',{device:String(book.iphone_live_name||'iPhone/iPad')})):''}">${book.position>1?(PudgeI18n.t('ui.resume')):(PudgeI18n.t('media.play'))}</button>`;
      const speeds=[0.75,1,1.25,1.5,1.75,2,2.5,3].map(value=>`<option value="${value}" ${value===speed?'selected':''}>${value}×</option>`).join('');
      const bookmarks=(book.bookmarks||[]).map(mark=>{const created=formatBookmarkDate(mark.created_at),tooltip=created?(PudgeI18n.t('media.created',{created:created})):'';return `<div class="audiobook-bookmark" role="button" tabindex="0" data-media-action="seek-audio-to" data-id="${book.id}" data-book-id="${book.id}" data-bookmark-id="${Number(mark.id)}" data-position="${Number(mark.position||0)}" ${tooltip?`data-tooltip="${esc(tooltip)}" aria-label="${esc(tooltip)}"`:''}><span class="audiobook-bookmark-label">${esc(mark.title||formatAudioTime(mark.position))}</span><button class="audiobook-bookmark-delete" type="button" data-media-action="delete-audio-bookmark" data-bookmark-id="${Number(mark.id)}" aria-label="${PudgeI18n.t('media.deleteBookmark')}">×</button></div>`;}).join('');
      const selected=audioSelection.has(Number(book.id));
      const volume=Number(book.volume||0);
      const displayTitle=grouped&&volume>0?(PudgeI18n.t('ui.volume',{volume:volume})):String(book.title||'');
      return `<article class="audiobook-card ${book.playing?'playing':''} ${selected?'selected':''}" data-audiobook-id="${Number(book.id)}"><div class="audiobook-main"><div class="audiobook-title-row"><strong title="${esc(String(book.title||''))}">${esc(displayTitle)}</strong>${book.metadata_pending?`<span class="audiobook-metadata-pending">${PudgeI18n.t('media.metadata')}</span>`:''}${book.tts_generated?`<span class="audiobook-tts-badge" title="${PudgeI18n.t('media.ttsGenerated')}" aria-label="${PudgeI18n.t('media.ttsGenerated')}">🤖</span>`:''}${book.playing?`<span class="audiobook-live"><i></i>${PudgeI18n.t('media.playing')}</span>`:''}</div><span data-audio-live-summary="${Number(book.id)}">${formatAudioTime(book.position)} / ${formatAudioTime(book.duration)} · ${Math.round(pct)}%${remaining>0?` · ${PudgeI18n.t('media.timeLeft',{time:formatAudioTime(remaining)})}`:''}${book.current_chapter?` · ${esc(book.current_chapter.title)}`:''}${book.multi_file?` · ${PudgeI18n.tn('media.fileCount',Number(book.file_count||0))}`:''}${syncConflict?` · ${syncConflict}`:''}</span><div class="audiobook-scrubber-shell" data-audio-timeline data-id="${book.id}" data-duration="${Math.max(1,Number(book.duration||1))}"><input class="audiobook-scrubber" type="range" min="0" max="${Math.max(1,Number(book.duration||1))}" step="1" value="${Number(book.position||0)}" data-audio-position data-id="${book.id}" aria-label="${PudgeI18n.t('media.position')}"><span class="audiobook-chapter-hover" aria-hidden="true"></span>${phoneMarker}</div></div>${audioPreparation(book.transcription||{})}<div class="audiobook-controls">${play}${handoffControl}<button data-media-action="seek-audio" data-id="${book.id}" data-seconds="-15">${PudgeI18n.t('sidebar.seekBackShort')}</button><button data-media-action="seek-audio" data-id="${book.id}" data-seconds="15">${PudgeI18n.t('sidebar.seekForwardShort')}</button><button data-media-action="bookmark-audio" data-id="${book.id}">${PudgeI18n.t('media.bookmark')}</button><label class="audiobook-speed"><span>${PudgeI18n.t('media.speed')}</span><select data-audio-speed data-id="${book.id}">${speeds}</select></label><label class="audiobook-speed"><span>${PudgeI18n.t('media.sleep')}</span><select data-audio-sleep data-id="${book.id}"><option value="off">—</option>${[15,30,45,60].map(minutes=>`<option value="${minutes}">${PudgeI18n.t('library.durationMinutes',{minutes})}</option>`).join('')}<option value="chapter">${PudgeI18n.t('media.endOfChapter')}</option></select></label><button data-media-action="finish-audio" data-id="${book.id}" data-finished="${book.finished?'0':'1'}">${book.finished?(PudgeI18n.t('media.markUnfinished')):(PudgeI18n.t('media.finish'))}</button><button class="danger-action" data-media-action="delete-audio" data-id="${book.id}">${PudgeI18n.t('media.remove')}</button></div>${bookmarks?`<div class="audiobook-bookmarks">${bookmarks}</div>`:''}${(book.chapters||[]).length?`<details class="audiobook-chapters" data-book-id="${Number(book.id)}" ${openChapterBooks.has(Number(book.id))?'open':''}><summary><span class="audiobook-chapters-label"><i>›</i>${PudgeI18n.t('media.chapters')}</span><b>${book.chapters.length}</b><span class="audiobook-chapters-open">${PudgeI18n.t('media.show')}</span><span class="audiobook-chapters-close">${PudgeI18n.t('media.hide')}</span></summary><div class="chapter-list">${book.chapters.map((chapter,index,chapters)=>{const chapterStart=Math.max(0,Number(chapter.start||0)),chapterEnd=Math.max(chapterStart,Number(chapters[index+1]?.start??book.duration??chapterStart));return `<button data-media-action="play-audio" data-id="${book.id}" data-start="${chapterStart}" data-audio-chapter-start="${chapterStart}" data-audio-chapter-end="${chapterEnd}">${esc(chapter.title)}</button>`;}).join('')}</div></details>`:''}</article>`;
    };
    const groups=[];
    for(const book of booksForRender){
      const seriesKey=String(book.series_key||'').trim(),grouped=Boolean(seriesKey&&(seriesCounts.get(seriesKey)||0)>1);
      const groupKey=grouped?`series:${seriesKey}`:`book:${Number(book.id)}`;
      let group=groups[groups.length-1];
      if(!group||group.key!==groupKey){group={key:groupKey,seriesKey,grouped,books:[]};groups.push(group);}
      group.books.push(book);
    }
    const libraryHtml=groups.map(group=>{
      if(!group.grouped)return group.books.map(book=>renderBookCard(book,false)).join('');
      const first=group.books[0],unfinished=group.books.find(book=>!book.finished),fallback=group.books[Math.max(0,group.books.length-2)],target=unfinished||fallback;
      const unread=group.books.filter(book=>!book.finished).length;
      const title=String(first?.series_title||first?.title||PudgeI18n.t('ui.audiobook2'));
      const meta=unread?(PudgeI18n.t('media.unfinished',{unread:unread})):(PudgeI18n.t('media.allFinished'));
      const ids=group.books.map(book=>Number(book.id)).filter(Number.isFinite);
      const selectedInSeries=ids.filter(id=>audioSelection.has(id)).length;
      const selectionClass=ids.length&&selectedInSeries===ids.length?'series-selected':selectedInSeries?'series-partial':'';
      return `<section class="audiobook-series-card ${selectionClass}" data-audiobook-series="${esc(group.seriesKey)}"><div class="audiobook-series-card-head"><div><strong>${esc(title)}</strong><span>${PudgeI18n.tn('media.volumeCount',group.books.length)}</span></div><span>${esc(meta)}</span></div><div class="audiobook-series-scroll" data-series-key="${esc(group.seriesKey)}" data-first-unfinished-id="${Number(target?.id||0)}">${group.books.map(book=>renderBookCard(book,true)).join('')}</div></section>`;
    }).join('');
    root.innerHTML = `<button id="audiobookImport" hidden ${audioImportBusy?'disabled':''}>${PudgeI18n.t('media.import')}</button>${audioImportBusy?`<div class="audiobook-import-status"><span class="audiobook-pulse"></span>${PudgeI18n.t('media.readingAudiobookMetadata')}</div>`:''}<div class="audiobook-list">${libraryHtml}</div>${books.length?'':`<div class="empty">${PudgeI18n.t('media.singleAudioFilesAndFolders')}</div>`}`;
    [...root.querySelectorAll('.audiobook-controls')].forEach(controls=>{const card=controls.closest('.audiobook-card');const book=audiobookById(Number(card?.dataset.audiobookId));if(!book)return;const before=controls.querySelector('.danger-action');if(book.cover_url&&card){card.classList.add('has-cover');const cover=document.createElement(book.anilist_id?'button':'div');cover.className='audiobook-cover';cover.dataset.audioCoverId=String(book.id);if(book.anilist_id){cover.dataset.mediaAction='open-audio-cover';cover.dataset.url=String(book.anilist_site_url||`https://anilist.co/manga/${book.anilist_id}`);cover.title='AniList';}cover.innerHTML=`<img src="${esc(book.cover_url)}" alt="">`;card.prepend(cover);}const identity=document.createElement('button');identity.dataset.mediaAction='identify-audio';identity.dataset.id=String(book.id);identity.dataset.title=String(book.title||'');identity.textContent=book.anilist_id?(PudgeI18n.t('ui.changeAnilist')):'AniList';controls.insertBefore(identity,before);});
    [...root.querySelectorAll('.audiobook-series-scroll[data-series-key]')].forEach(node=>{
      const key=String(node.dataset.seriesKey||'');
      if(seriesScrollPositions.has(key)){node.scrollTop=Number(seriesScrollPositions.get(key)||0);return;}
      const targetId=Number(node.dataset.firstUnfinishedId||0),target=targetId?node.querySelector(`[data-audiobook-id="${targetId}"]`):null;
      if(target)node.scrollTop=Math.max(0,target.offsetTop-node.offsetTop-4);
    });
    dropDetachedSidebarChapterHover();
  };

  const loadManga = async () => {
    mangaState = await pywebview.api.manga_state();
    counts.manga = (mangaState.books || []).length;
    await window.PudgeMangaReaderV2?.renderLibrary?.();
    window.updateCount?.();
  };
  let audioFullRefreshAt = 0;
  const loadAudio = async ({playbackOnly = false} = {}) => {
    if (playbackOnly && pywebview.api.sidebar_audio_state && audioState?.books
      && Date.now() - audioFullRefreshAt < (document.documentElement.classList.contains('energy-saving') ? 30000 : 15000)) {
      const focused = await pywebview.api.sidebar_audio_state();
      const live = new Map((focused.books || []).map(book => [Number(book.id), book]));
      audioState = {...audioState, books:audioState.books.map(book => {
        const update = live.get(Number(book.id));
        return update ? {...book, ...update, transcription:book.transcription} : {...book, playing:false, player_running:false, paused:false};
      })};
    } else {
      try {
        audioState = await pywebview.api.audiobook_state();
      } catch (error) {
        // Without books there is nothing to render: show the error with Retry.
        if (!audioState?.books) {
          const root = $('audiobooksContent');
          const detail = PudgeI18n.message(String(error?.message || error));
          if (root) root.innerHTML = `<div class="empty danger">${esc(PudgeI18n.t('media.couldNotLoadAudiobooks'))}${esc(detail)}<div><button type="button" data-audio-retry>${PudgeI18n.t('media.retry')}</button></div></div>`;
          root?.querySelector('[data-audio-retry]')?.addEventListener('click', () => void loadAudio());
          return;
        }
        throw error;
      }
      audioFullRefreshAt = Date.now();
    }
    window.PudgeSidebarCompanion?.acceptAudioState?.(audioState);
    counts.audiobooks = (audioState.books||[]).length;
    const playingBook=(audioState.books||[]).find(book=>book.playing);
    if(playingBook)activeAudioBookId=Number(playingBook.id);
    else if(activeAudioBookId==null&&(audioState.books||[]).length)activeAudioBookId=Number(audioState.books[0].id);
    const audioRoot=$('audiobooksContent');
    const focusedAudioControl=document.activeElement?.closest?.('[data-audio-speed],[data-audio-position],[data-audio-sleep]');
    // An open custom dropdown (speed, sleep timer) is held like a focused native control:
    // a rebuild would detach its list and the choice would be lost.
    const openAudioMenu=audioRoot?.querySelector?.('.pudge-select.open');
    const livePlayback=(audioState.books||[]).some(book=>book.playing);
    const controlEngaged=Date.now()<audioControlEngagedUntil;
    // Live playback only patches cards already on the page; the first visit (or a book
    // without its card yet) needs the full page.
    const drawnIds=new Set([...(audioRoot?.querySelectorAll?.('.audiobook-card[data-audiobook-id]')||[])].map(card=>Number(card.dataset.audiobookId)));
    const cardsDrawn=drawnIds.size===(audioState.books||[]).length&&(audioState.books||[]).every(book=>drawnIds.has(Number(book.id)));
    if(audioControlMutation===0&&!focusedAudioControl&&!controlEngaged){
      if(livePlayback&&cardsDrawn)updateAudioLiveFields();
      else if(!openAudioMenu)renderAudio();
    }
    window.updateCount?.();
    if (audioPollTimer) clearTimeout(audioPollTimer);
    const active=document.querySelector('.nav button[data-page="audiobooks"]')?.classList.contains('active');
    const playing=(audioState.books||[]).some(book=>book.playing);
    const transcribing=(audioState.books||[]).some(book=>String(book.transcription?.status||'')==='transcribing');
    const covers=(audioState.books||[]).some(book=>book.cover_pending);
    const queued=(audioState.books||[]).some(book=>String(book.transcription?.status||'')==='queued');
    const busy=playing||transcribing||covers||queued;
    if (active && busy && !document.getElementById('lnReaderShell')?.classList.contains('open')) {
      // A 200-book queued library used to rebuild the whole Audiobooks page
      // every 1.2 s forever. Poll active work quickly, but a passive queue only
      // needs occasional position updates.
      const delay=document.documentElement.classList.contains('energy-saving')?5000:playing?1000:transcribing?1400:covers?2200:5000;
      audioPollTimer=setTimeout(()=>void loadAudio({playbackOnly:playing&&!transcribing&&!covers&&!queued}),delay);
    }
  };

  const audiobookById = id =>
    (audioState.books||[]).find(book=>Number(book.id)===Number(id))||null;

  const applyAudioActionResult = result => {
    if(result?.state?.books){audioState=result.state;return;}
    if(result?.book){
      const id=Number(result.book.id),books=audioState.books||[];
      const index=books.findIndex(book=>Number(book.id)===id);
      if(index>=0)books[index]={...books[index],...result.book};
      else books.unshift(result.book);
    }
    if(Array.isArray(result?.removed)&&result.removed.length){
      const removed=new Set(result.removed.map(Number));
      audioState.books=(audioState.books||[]).filter(book=>!removed.has(Number(book.id)));
      removed.forEach(id=>audioSelection.delete(id));
    }else if(result?.book_id&&result?.files_kept!==undefined){
      const id=Number(result.book_id);
      audioState.books=(audioState.books||[]).filter(book=>Number(book.id)!==id);
      audioSelection.delete(id);
    }
  };

  const optimisticAudioPlaying = (id, playing) => {
    activeAudioBookId=Number(id);
    const book=audiobookById(id);
    if(book){book.playing=!!playing;book.player_running=!!playing;}
    if(playing)for(const other of audioState.books||[]){if(Number(other.id)!==Number(id)){other.playing=false;other.player_running=false;}}
    window.PudgeSidebarCompanion?.acceptAudioState?.(audioState,{optimistic:!!playing,stop:!playing});
    renderAudio();
    return book;
  };

  async function continueFromIPhone(id,action){
    const key=`pudge-iphone-handoff:${id}`;
    // Keep the operation through a lost response or a window reload.
    const operation=sessionStorage.getItem(key)||crypto.randomUUID();
    sessionStorage.setItem(key,operation);
    action.disabled=true;
    const device=String(action.dataset.device||'iPhone/iPad');
    window.toast?.(PudgeI18n.t('media.pausingDevice',{device}));
    try{
      let result;
      for(let attempt=0;attempt<25;attempt++){
        result=await pywebview.api.iphone_resume_on_mac(id,operation,audioSpeed(id));
        if(result.status!=='pending')break;
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
      if(result?.status==='pending')await pywebview.api.iphone_cancel_handoff(operation);
      sessionStorage.removeItem(key);
      const started=result?.targetStatus==='started';
      if(started)window.toast?.(PudgeI18n.t('media.devicePausedContinuingOnMac',{device}));
      await loadAudio();
      return started;
    }finally{action.disabled=false;}
  }
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
      if(notify)window.toast?.(PudgeI18n.t('media.playbackStarted'));
    }catch(error){
      if(book){book.playing=previous;book.player_running=previous;}
      window.PudgeSidebarCompanion?.acceptAudioState?.(audioState,{rollback:true});
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
      applyAudioActionResult(result);
      renderAudio();
      if(notify)window.toast?.(
        PudgeI18n.t('media.stoppedPositionSaved')
      );
    }catch(error){
      if(book){book.playing=previous;book.player_running=previous;}
      window.PudgeSidebarCompanion?.acceptAudioState?.(audioState,{rollback:true});
      renderAudio();
      throw error;
    }
  };

  const seekAudiobook = async (id, seconds) => {
    id=Number(id);
    activeAudioBookId=id;
    const result=await pywebview.api.audiobook_seek(id,Number(seconds||0));
    applyAudioActionResult(result);
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
      menu.innerHTML=`<button type="button" class="pudge-select-option" data-audio-import-kind="file">${PudgeI18n.t('media.file')}</button><button type="button" class="pudge-select-option" data-audio-import-kind="folder">${PudgeI18n.t('media.folder')}</button>`;
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
    modalTitle.textContent = PudgeI18n.t('media.findMangaOnAnilist');
    modalBody.innerHTML = `<div class="empty">${PudgeI18n.t('media.searchingAnilist')}</div>`;
    backdrop.classList.add('open');
    let unlink = '';
    try {
      mangaState = await pywebview.api.manga_state() || mangaState;
      const current = (mangaState.books || []).find(book => Number(book.id) === mangaAniListBookId);
      unlink = current?.anilist_id
        ? `<div class="manga-anilist-unlink"><span>${esc(PudgeI18n.t('media.currentLinkTitle', {title: current.series_title || current.title || ''}))}</span><button class="danger-action" data-media-action="unlink-manga-anilist">${PudgeI18n.t('media.unlinkFromAnilist')}</button></div>`
        : '';
      modalBody.innerHTML = unlink + `<div class="empty">${PudgeI18n.t('media.searchingAnilist')}</div>`;
    } catch (_) {}
    try {
      mangaAniListResults = await pywebview.api.manga_search_anilist(title || '') || [];
      const matches = mangaAniListResults.length
        ? mangaAniListResults.map((row,index) => `<div class="ln-nyaa-row"><span class="title" title="${esc(row.title||'')}">${esc(row.title||'')}</span><span>${esc(row.format||'')}</span><span>${row.volumes?PudgeI18n.tn('media.volumesShort',Number(row.volumes)):''}</span><button data-media-action="bind-manga-anilist" data-index="${index}">${PudgeI18n.t('ui.link')}</button></div>`).join('')
        : `<div class="empty">${PudgeI18n.t('media.noMatchingMangaFound')}</div>`;
      modalBody.innerHTML = unlink + matches;
    } catch (error) {
      modalBody.innerHTML = unlink + `<div class="empty danger">${esc(PudgeI18n.message(String(error?.message || error)))}</div>`;
    }
  };

  const closeAudioContextMenu = () => {
    const menu=$('contextMenu');
    if(menu&&(audioContextBookId!=null||audioBookmarkContextId!=null)){
      menu.classList.remove('open');menu.innerHTML='';
    }
    audioContextBookId=null;audioBookmarkContextId=null;
  };
  const positionAudioContextMenu = (menu,x,y) => {
    menu.classList.add('open');
    const rect=menu.getBoundingClientRect();
    menu.style.left=`${Math.max(8,Math.min(x,window.innerWidth-rect.width-8))}px`;
    menu.style.top=`${Math.max(8,Math.min(y,window.innerHeight-rect.height-8))}px`;
  };
  const showAudioCoverContextMenu = (book,x,y) => {
    const menu=$('contextMenu');if(!menu||!book)return;
    audioContextBookId=Number(book.id);audioBookmarkContextId=null;
    const play=book.playing?(PudgeI18n.t('media.stop')):(book.position>1?(PudgeI18n.t('ui.resume')):(PudgeI18n.t('media.play')));
    const anilist=book.anilist_id?`<button data-audio-context-action="open-anilist">${PudgeI18n.t('action.openAniList')}</button>`:'';
    const identify=`<button data-audio-context-action="identify">${book.anilist_id?(PudgeI18n.t('ui.changeAnilist')):(PudgeI18n.t('ui.findOnAnilist'))}</button>`;
    const findLn='';
    const editMetadata=`<button data-audio-context-action="edit-metadata">${PudgeI18n.t('ui.editVolumeAndSeries')}</button>`;
    menu.innerHTML=`<button data-audio-context-action="play">${play}</button>${anilist}${identify}${findLn}${editMetadata}<button data-audio-context-action="reveal">${PudgeI18n.t('media.showInFinder')}</button><button class="danger-action" data-audio-context-action="delete">${PudgeI18n.t('manga.removeFromPudge')}</button>`;
    positionAudioContextMenu(menu,x,y);
  };
  const showAudioBookmarkContextMenu = (bookId,bookmarkId,x,y) => {
    const menu=$('contextMenu');if(!menu)return;
    audioContextBookId=Number(bookId);audioBookmarkContextId=Number(bookmarkId);
    menu.innerHTML=`<button data-audio-context-action="rename-bookmark">${PudgeI18n.t('media.rename')}</button><button class="danger-action" data-audio-context-action="delete-bookmark">${PudgeI18n.t('ui.delete')}</button>`;
    positionAudioContextMenu(menu,x,y);
  };
  const showAudioLnSearch = async bookId => {
    $('modalTitle').textContent=PudgeI18n.t('media.lightNovelOnNyaa');
    $('modalBody').innerHTML=`<div class="empty">${PudgeI18n.t('label.searchingReleases')}</div>`;
    $('modalBackdrop').classList.add('open');
    const result=await pywebview.api.audiobook_search_light_novel_nyaa(Number(bookId));
    audioLnNyaaRows=result.releases||[];
    const rows=audioLnNyaaRows.map((row,index)=>`<div class="ln-nyaa-row"><span class="title" title="${esc(row.title||'')}">${esc(row.title||'')}</span><span>${PudgeI18n.tn('media.seeds',Number(row.seeders||0))} · ${esc(row.size||'')}</span><button data-media-action="download-audio-ln-release" data-index="${index}">${PudgeI18n.t('action.download')}</button></div>`).join('');
    $('modalBody').innerHTML=rows||`<div class="empty">${PudgeI18n.t('media.noReleasesFound')}</div>`;
  };
  const showAudioBookmarkRename = bookmarkId => {
    const found=audioBookmarkById(bookmarkId);if(!found)return;
    const {mark}=found;
    $('modalTitle').textContent=PudgeI18n.t('media.renameBookmark');
    $('modalBody').innerHTML=`<div class="form-grid"><label for="audioBookmarkRenameInput">${PudgeI18n.t('table.title')}</label><div class="setting-inline-actions"><input id="audioBookmarkRenameInput" value="${esc(mark.title||'')}"><button class="primary" data-media-action="save-audio-bookmark-name" data-bookmark-id="${Number(mark.id)}">${PudgeI18n.t('action.save')}</button></div></div>`;
    $('modalBackdrop').classList.add('open');
    requestAnimationFrame(()=>{const input=$('audioBookmarkRenameInput');input?.focus();input?.select();});
  };

  document.addEventListener('contextmenu',event=>{
    const bookmark=event.target.closest?.('.audiobook-bookmark[data-bookmark-id]');
    if(bookmark){event.preventDefault();event.stopImmediatePropagation();showAudioBookmarkContextMenu(Number(bookmark.dataset.bookId),Number(bookmark.dataset.bookmarkId),event.clientX,event.clientY);return;}
    const cover=event.target.closest?.('.audiobook-cover[data-audio-cover-id]');
    if(cover){const book=audiobookById(Number(cover.dataset.audioCoverId));if(!book)return;event.preventDefault();event.stopImmediatePropagation();showAudioCoverContextMenu(book,event.clientX,event.clientY);}
  },true);

  // v206: the large series card is a primary select-all surface, matching
  // light-novel/manga series groups. Inner volume cards keep the existing
  // Cmd+click / active-selection behavior so their playback controls stay safe.
  document.addEventListener('click',event=>{
    const series=event.target.closest?.('#audiobooksContent .audiobook-series-card[data-audiobook-series]');
    if(!series)return;
    if(event.target.closest?.('.audiobook-card[data-audiobook-id]'))return;
    if(event.target.closest?.('button,a,input,select,textarea,label,details,summary,[data-media-action]'))return;
    const books=audioSeriesBooks(series.dataset.audiobookSeries);
    if(!books.length)return;
    event.preventDefault();event.stopImmediatePropagation();
    toggleAudioSeriesSelection(books);
  },true);

  // v207: an audiobook volume card is itself a selection control. Clicking
  // any non-interactive part toggles that one volume, even before a broader
  // selection exists. Real controls and cover art keep their own actions.
  const audioSelectionSurface=(target,card)=>{
    if(!card)return false;
    if(target.closest?.('.audiobook-cover'))return false;
    if(target.closest?.('button,a,input,select,textarea,label,details,summary,[data-media-action],.audiobook-bookmarks,.audiobook-scrubber-shell'))return false;
    return true;
  };
  document.addEventListener('click',event=>{
    const card=event.target.closest?.('#audiobooksContent .audiobook-card[data-audiobook-id]');
    if(!card||!audioSelectionSurface(event.target,card))return;
    event.preventDefault();event.stopImmediatePropagation();
    const id=Number(card.dataset.audiobookId);if(!Number.isFinite(id))return;
    if(audioSelection.has(id))audioSelection.delete(id);else audioSelection.add(id);
    renderAudio();emitAudioSelection();
  },true);

  document.addEventListener('click', async event => {
    const contextAction=event.target.closest?.('[data-audio-context-action]');
    if(contextAction){
      event.preventDefault();event.stopPropagation();
      const type=contextAction.dataset.audioContextAction,bookId=audioContextBookId,bookmarkId=audioBookmarkContextId;
      const book=bookId==null?null:audiobookById(bookId);
      closeAudioContextMenu();
      if(type==='play'&&book){if(book.playing)await stopAudiobook(book.id);else await playAudiobook(book.id);return;}
      if(type==='open-anilist'&&book?.anilist_id){await pywebview.api.open_url(book.anilist_site_url||`https://anilist.co/manga/${book.anilist_id}`);return;}
      if(type==='identify'&&book){await window.showMediaIdentity?.('audiobook',Number(book.id),book.title||'');return;}
      if(type==='find-ln'&&book){await showAudioLnSearch(Number(book.id));return;}
      if(type==='edit-metadata'&&book){const saved=await window.PudgeLibraryMetadata?.edit('audiobook',book);if(saved){await loadAudio();window.toast?.(PudgeI18n.t('ui.volumeAndSeriesSaved'));}return;}
      if(type==='reveal'&&book){await pywebview.api.audiobook_reveal_source(Number(book.id));return;}
      if(type==='delete'&&book){if(!await pudgeConfirm(PudgeI18n.t('media.removeThisAudiobookFromPudge'),{danger:true}))return;const result=await pywebview.api.audiobook_delete(Number(book.id));applyAudioActionResult(result);counts.audiobooks=(audioState.books||[]).length;renderAudio();window.updateCount?.();return;}
      if(type==='rename-bookmark'&&bookmarkId!=null){showAudioBookmarkRename(bookmarkId);return;}
      if(type==='delete-bookmark'&&bookmarkId!=null){await deleteAudioBookmark(Number(bookmarkId));return;}
    }
    if(!event.target.closest?.('#contextMenu'))closeAudioContextMenu();
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
      await showAudioLnSearch(Number(action.dataset.id));
      return;
    }
    if (type === 'download-audio-ln-release') {
      const release=audioLnNyaaRows[Number(action.dataset.index)];
      if(!release)return;
      await pywebview.api.light_novel_download_nyaa(release);
      window.toast?.(PudgeI18n.t('media.lightNovelTorrentAdded'));
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
        window.toast?.(PudgeI18n.t('media.anilistLinked'));
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
      window.toast?.(PudgeI18n.t('media.seriesUnlinkedFromAnilist'));
      return;
    }
    if (type === 'play-audio') {
      const id=Number(action.dataset.id);
      if(action.dataset.iphoneLive==='1'&&action.dataset.start==null){
        if(await continueFromIPhone(id,action))return;
        const fallback=await window.pudgeConfirm(PudgeI18n.t('media.deviceDidNotConfirmPause',{device:String(action.dataset.device||'iPhone/iPad')}));
        if(!fallback)return;
      }
      await playAudiobook(id,action.dataset.start==null?null:Number(action.dataset.start));
    }
    if (type === 'seek-audio') {
      await seekAudiobook(Number(action.dataset.id),Number(action.dataset.seconds||0));
    }
    if (type === 'use-iphone-audio-position') {
      const id=Number(action.dataset.id);
      try{
        const result=await pywebview.api.audiobook_use_iphone_position(id,String(action.dataset.checkpointId||''),Number(action.dataset.revision||0));
        if(result?.book){applyAudioActionResult(result);renderAudio();}
        else await loadAudio();
      }catch(error){
        // The positions changed meanwhile (a newer save from either device): show the current choice.
        console.debug('use iPhone position',error);
        window.toast?.(PudgeI18n.t('media.iphonePositionChanged'));
        await loadAudio();
      }
      return;
    }
    if (type === 'seek-audio-to') {
      const result=await pywebview.api.audiobook_seek_to(Number(action.dataset.id),Number(action.dataset.position||0));applyAudioActionResult(result);renderAudio();
    }
    if (type === 'bookmark-audio') {
      const result=await pywebview.api.audiobook_add_bookmark(Number(action.dataset.id),'');applyAudioActionResult(result);renderAudio();
    }
    if (type === 'delete-audio-bookmark') {
      await deleteAudioBookmark(Number(action.dataset.bookmarkId));
    }
    if (type === 'save-audio-bookmark-name') {
      const title=String($('audioBookmarkRenameInput')?.value||'').trim();
      const result=await pywebview.api.audiobook_rename_bookmark(Number(action.dataset.bookmarkId),title);applyAudioActionResult(result);$('modalBackdrop')?.classList.remove('open');renderAudio();
    }
    if (type === 'finish-audio') {
      if(action.dataset.finished!=='1'&&!await pudgeConfirm(PudgeI18n.t('media.markThisAudiobookUnfinishedThe')))return;
      const result=await pywebview.api.audiobook_mark_finished(Number(action.dataset.id),action.dataset.finished==='1');applyAudioActionResult(result);renderAudio();
    }
    if (type === 'stop-audio') {
      await stopAudiobook(Number(action.dataset.id));
    }
    if (type === 'delete-audio') {
      if(!await pudgeConfirm(PudgeI18n.t('media.removeThisAudiobookFromPudge')))return;
      const result=await pywebview.api.audiobook_delete(Number(action.dataset.id));applyAudioActionResult(result);counts.audiobooks=(audioState.books||[]).length;renderAudio();window.updateCount?.();
    }
  });
  const showSidebarChapterHover = chapter => {
    const card=chapter?.closest?.('.audiobook-card');
    const audiobookId=Number(card?.dataset?.audiobookId||0);
    const start=Number(chapter?.dataset?.audioChapterStart),end=Number(chapter?.dataset?.audioChapterEnd);
    const source=`media-chapter:${++sidebarChapterHoverSeq}`;
    sidebarChapterHover={node:chapter,source};
    try{window.PudgeSidebarCompanion?.showChapterHoverRange?.({audiobookId,start,end,source});}catch(error){console.debug('sidebar chapter hover',error);}
  };
  const showAudioChapterHover = chapter => {
    const card=chapter?.closest?.('.audiobook-card');
    const timeline=card?.querySelector?.('[data-audio-timeline]');
    const marker=timeline?.querySelector?.('.audiobook-chapter-hover');
    showSidebarChapterHover(chapter);
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
    clearSidebarChapterHover(chapter);
  };
  // Closing the chapter list (pointer still inside) or a re-render removing the
  // hovered node never fires pointerout: clear the mirrored range explicitly.
  document.addEventListener('toggle',event=>{
    const details=event.target;
    if(details?.open||!details?.matches?.('.audiobook-chapters'))return;
    const node=sidebarChapterHover?.node;
    if(node&&details.contains(node))hideAudioChapterHover(node);
  },true);
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

  const persistAudioBookmarkOrder = async drag => {
    if(!drag?.changed||!drag.container?.isConnected)return;
    const ids=[...drag.container.querySelectorAll('.audiobook-bookmark[data-bookmark-id]')]
      .map(node=>Number(node.dataset.bookmarkId));
    try{
      const result=await pywebview.api.audiobook_reorder_bookmarks(Number(drag.bookId),ids);
      applyAudioActionResult(result);
    }catch(error){
      window.toast?.(error?.message||String(error));
      await loadAudio();
    }
  };
  const finishAudioBookmarkDrag = event => {
    const drag=audioBookmarkDrag;
    if(!drag||drag.pointerId!==event.pointerId)return;
    audioBookmarkDrag=null;
    if(audioBookmarkDragFrame){cancelAnimationFrame(audioBookmarkDragFrame);audioBookmarkDragFrame=0;}
    drag.node?.classList.remove('dragging');
    drag.node?.releasePointerCapture?.(event.pointerId);
    if(drag.moved)audioBookmarkSuppressClickUntil=performance.now()+350;
    if(drag.changed)void persistAudioBookmarkOrder(drag);
  };
  document.addEventListener('pointerdown',event=>{
    if(event.button!==0||event.target.closest?.('.audiobook-bookmark-delete'))return;
    const bookmark=event.target.closest?.('.audiobook-bookmark[data-bookmark-id]');if(!bookmark)return;
    const container=bookmark.parentElement;if(!container?.classList.contains('audiobook-bookmarks'))return;
    audioBookmarkDrag={
      bookId:Number(bookmark.dataset.bookId),
      bookmarkId:Number(bookmark.dataset.bookmarkId),
      pointerId:event.pointerId,
      node:bookmark,
      container,
      startX:event.clientX,
      startY:event.clientY,
      clientX:event.clientX,
      clientY:event.clientY,
      moved:false,
      changed:false,
    };
    bookmark.classList.add('dragging');
    bookmark.setPointerCapture?.(event.pointerId);
  });
  document.addEventListener('pointermove',event=>{
    const drag=audioBookmarkDrag;if(!drag||drag.pointerId!==event.pointerId)return;
    drag.clientX=event.clientX;drag.clientY=event.clientY;
    if(!drag.moved&&Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)>=4)drag.moved=true;
    if(!drag.moved||audioBookmarkDragFrame)return;
    event.preventDefault();
    audioBookmarkDragFrame=requestAnimationFrame(()=>{
      audioBookmarkDragFrame=0;
      const live=audioBookmarkDrag;if(!live||!live.node?.isConnected)return;
      const target=document.elementFromPoint(live.clientX,live.clientY)?.closest?.('.audiobook-bookmark[data-bookmark-id]');
      if(!target||target===live.node||target.parentElement!==live.container||Number(target.dataset.bookId)!==live.bookId)return;
      const rect=target.getBoundingClientRect(),centerY=rect.top+rect.height/2;
      const differentRow=Math.abs(live.clientY-centerY)>Math.max(6,rect.height*.35);
      const before=differentRow?live.clientY<centerY:live.clientX<rect.left+rect.width/2;
      const reference=before?target:target.nextSibling;
      if(reference===live.node||(!before&&target.nextSibling===live.node))return;
      live.container.insertBefore(live.node,reference);
      live.changed=true;
    });
  },{passive:false});
  document.addEventListener('pointerup',finishAudioBookmarkDrag);
  document.addEventListener('pointercancel',finishAudioBookmarkDrag);
  document.addEventListener('click',event=>{
    if(performance.now()>=audioBookmarkSuppressClickUntil)return;
    if(!event.target.closest?.('.audiobook-bookmark[data-bookmark-id]'))return;
    event.preventDefault();event.stopImmediatePropagation();
  },true);

  document.addEventListener('pointerdown', event => {
    const control=event.target.closest?.('[data-audio-speed],[data-audio-position],[data-audio-sleep]');
    if(control)audioControlEngagedUntil=Date.now()+15000;
  },true);

  document.addEventListener('change', async event => {
    const position=event.target.closest?.('[data-audio-position]');
    if(position){const result=await pywebview.api.audiobook_seek_to(Number(position.dataset.id),Number(position.value||0));applyAudioActionResult(result);renderAudio();return;}
    const sleep=event.target.closest?.('[data-audio-sleep]');
    if(sleep){const result=await pywebview.api.audiobook_sleep_timer(Number(sleep.dataset.id),sleep.value||'off');applyAudioActionResult(result);renderAudio();return;}
    const control=event.target.closest?.('[data-audio-speed]');
    if(!control)return;
    const id=Number(control.dataset.id),speed=Number(control.value||1);
    audioControlMutation+=1;
    audioControlEngagedUntil=0;
    control.blur();
    try{
      const result=await pywebview.api.audiobook_set_speed(id,speed);
      applyAudioActionResult(result);
      renderAudio();
    }finally{
      audioControlMutation=Math.max(0,audioControlMutation-1);
    }
  });

  document.addEventListener('keydown', event => {
    const plain=!event.metaKey&&!event.ctrlKey&&!event.altKey&&!event.shiftKey;
    const editable=!!event.target.closest?.(
      'input,textarea,select,button,[contenteditable="true"]'
    );
    const audioPageActive=document.querySelector(
      '.nav button[data-page="audiobooks"]'
    )?.classList.contains('active');
    const undoDelete=audioPageActive&&!editable&&!event.shiftKey&&!event.altKey
      &&(event.metaKey||event.ctrlKey)&&String(event.key||'').toLowerCase()==='z';
    if(undoDelete&&audioBookmarkUndo.length){
      event.preventDefault();
      event.stopPropagation();
      if(!event.repeat)void undoDeletedAudioBookmark();
      return;
    }
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

  const MANGA_OCR_LABELS = {
    not_installed:'media.ocr.notInstalled', starting:'media.ocr.starting', installing_package:'media.ocr.installingPackage',
    package_installed:'media.ocr.packageInstalled', downloading_model:'media.ocr.downloadingModel',
    ready:'media.ocr.ready', failed:'media.ocr.failed', idle:'media.ocr.notInstalled', updating:'media.ocr.updating',
  };
  const mangaOcrLabels = state => MANGA_OCR_LABELS[state] ? PudgeI18n.t(MANGA_OCR_LABELS[state]) : (state || '—');

  const refreshMangaOcrStatus = async () => {
    const statusNode = $('mangaOcrStatus');
    if (!statusNode || !window.pywebview?.api?.manga_ocr_status) return null;
    try {
      const status = await pywebview.api.manga_ocr_status();
      statusNode.textContent = mangaOcrLabels(status.state);
      const detail = $('mangaOcrDetail');
      if (detail) detail.textContent = status.detail ? PudgeI18n.message(String(status.detail)) : '';
      // Working (or being updated): Update Mokuro; missing or broken: Install (the tested set).
      const usable = status.state === 'ready' || status.state === 'updating';
      const install = $('installMangaOcr');
      if (install) {
        install.textContent = PudgeI18n.t('media.installMokuro');
        install.disabled = !!status.running || usable;
        install.hidden = usable;
      }
      const update = $('updateMokuro');
      if (update) {
        const updating = status.state === 'updating';
        update.textContent = PudgeI18n.t(updating ? 'media.updatingMokuro' : 'media.updateMokuro');
        update.hidden = !(status.updatable || updating);
        update.disabled = !!status.running;
      }
      const version = $('mangaOcrVersion');
      if (version) {
        const parts = [];
        if (status.mokuro_version) parts.push(PudgeI18n.t('media.mokuroVersion', {version: status.mokuro_version}));
        if (status.manga_ocr_version) parts.push(PudgeI18n.t('media.mangaOcrVersion', {version: status.manga_ocr_version}));
        if (status.last_checked_at) parts.push(PudgeI18n.t('settings.toolLastChecked', {date: formatBookmarkDate(status.last_checked_at)}));
        if (status.last_updated_at) parts.push(PudgeI18n.t('settings.toolLastUpdated', {date: formatBookmarkDate(status.last_updated_at)}));
        version.textContent = parts.join(' · ') || '—';
      }
      const block = statusNode.closest('.setting-block');
      if (block) {
        block.dataset.settingsCategory = usable ? 'advanced' : 'essential';
        window.PudgeSettings?.refresh?.();
      }
      if (status.running) setTimeout(() => void refreshMangaOcrStatus(), 1200);
      return status;
    } catch (error) {
      const detail = $('mangaOcrDetail');
      if (detail) detail.textContent = PudgeI18n.message(String(error?.message || error));
      return null;
    }
  };

  document.addEventListener('click', async event => {
    if (event.target.id === 'installMangaOcr' || event.target.id === 'updateMokuro') {
      event.target.disabled = true;
      try {
        await (event.target.id === 'updateMokuro' ? pywebview.api.update_mokuro() : pywebview.api.install_manga_ocr());
        await refreshMangaOcrStatus();
      } catch (error) {
        const detail = $('mangaOcrDetail');
        if (detail) detail.textContent = PudgeI18n.message(String(error?.message || error));
        event.target.disabled = false;
      }
    }
  });

  window.PudgeAudiobookSelection={
    selectedBookIds:()=>[...audioSelection],
    selectAll:()=>{(audioState.books||[]).forEach(book=>audioSelection.add(Number(book.id)));renderAudio();emitAudioSelection();},
    clearSelection:()=>{if(!audioSelection.size)return;audioSelection.clear();renderAudio();emitAudioSelection();},
    deleteSelected:async()=>{
      const ids=[...audioSelection];if(!ids.length)return;
      const removed=new Set(ids.map(Number));
      audioState={...audioState,books:(audioState.books||[]).filter(book=>!removed.has(Number(book.id)))};
      audioSelection.clear();counts.audiobooks=(audioState.books||[]).length;renderAudio();emitAudioSelection();window.updateCount?.();
      try{const result=await pywebview.api.audiobook_delete_many(ids);if(result?.errors?.length)throw new Error(result.errors.map(row=>row.error||row).join(' · '));}
      catch(error){window.toast?.(error?.message||String(error));await loadAudio();}
    }
  };

  window.PudgeMedia = {audioSnapshot:()=>audioState, counts, loadManga, loadAudio, refreshMangaOcrStatus, showMangaAniListSearch};
})();
