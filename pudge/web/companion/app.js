(() => {
  'use strict';

  const PUDGE_COMPANION_PWA_V1 = true;
  const PUDGE_COMPANION_PWA_V2 = true;
  const PUDGE_COMPANION_LIBRARY_UI_V8 = true;
  const PUDGE_COMPANION_LIBRARY_GROUPS_V11 = true;
  const PUDGE_COMPANION_ANIME_STREAMING_V12 = true;
  const PUDGE_COMPANION_INTERACTIVE_SUBTITLES_V13 = true;
  const TOKEN_KEY = 'pudge.companion.token.v1';
  const DEVICE_KEY = 'pudge.companion.device.v1';
  const DB_NAME = 'pudge-companion-v1';
  const MEDIA_STORE = 'offline-media';
  const MEDIA_CACHE = 'pudge-companion-media-v1';
  const EVICT_AT = 0.82;
  const EVICT_TO = 0.68;

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || '',
    deviceId: localStorage.getItem(DEVICE_KEY) || '',
    entities: [],
    filter: 'all',
    search: '',
    reader: null,
    readerBlobUrl: '',
    series: null,
    coverBlobUrls: new Map(),
    animePlayer: null,
    subtitleCues: [],
    subtitleVisible: true,
    subtitleOffsetSeconds: 0,
    subtitleScale: Number(localStorage.getItem('pudge.companion.subtitle.scale.v1') || 1),
    subtitleActiveCueKey: '',
    subtitleActiveCue: null,
    subtitleParseCache: new Map(),
    subtitleParseRequest: 0,
    subtitleResumeAfterSheet: false,
    wakeLock: null,
    lastAnimeSyncAt: 0,
  };
  const $ = selector => document.querySelector(selector);

  const openDb = () => new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) return resolve(null);
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(MEDIA_STORE)) {
        const store = db.createObjectStore(MEDIA_STORE, {keyPath: 'entityId'});
        store.createIndex('lastUsed', 'lastUsed');
        store.createIndex('pinned', 'pinned');
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });

  const listOffline = async () => {
    const db = await openDb();
    if (!db) return [];
    try {
      return await new Promise((resolve, reject) => {
        const request = db.transaction(MEDIA_STORE, 'readonly').objectStore(MEDIA_STORE).getAll();
        request.onsuccess = () => resolve(request.result || []);
        request.onerror = () => reject(request.error);
      });
    } finally {
      db.close();
    }
  };

  const removeOffline = async record => {
    if (!record?.entityId) return;
    if ('caches' in window) {
      const cache = await caches.open(MEDIA_CACHE);
      for (const key of record.cacheKeys || []) await cache.delete(key);
    }
    const db = await openDb();
    if (!db) return;
    try {
      await new Promise((resolve, reject) => {
        const tx = db.transaction(MEDIA_STORE, 'readwrite');
        tx.objectStore(MEDIA_STORE).delete(record.entityId);
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  };

  const storageEstimate = async () => {
    if (!navigator.storage?.estimate) return {usage: 0, quota: 0};
    const value = await navigator.storage.estimate();
    return {usage: Number(value.usage || 0), quota: Number(value.quota || 0)};
  };

  const evictUnusedMedia = async () => {
    const estimate = await storageEstimate();
    if (!estimate.quota || estimate.usage / estimate.quota < EVICT_AT) return 0;
    const records = (await listOffline()).filter(item => !item.pinned).sort((a, b) => Number(a.lastUsed || 0) - Number(b.lastUsed || 0));
    let usage = estimate.usage;
    let removed = 0;
    for (const record of records) {
      if (usage / estimate.quota <= EVICT_TO) break;
      await removeOffline(record);
      usage = Math.max(0, usage - Number(record.size || 0));
      removed += 1;
    }
    return removed;
  };

  window.PudgeOfflineStore = {list: listOffline, remove: removeOffline, evictUnusedMedia, cacheName: MEDIA_CACHE};

  const api = async (path, options = {}, authenticated = true) => {
    const headers = new Headers(options.headers || {});
    if (options.body) headers.set('Content-Type', 'application/json');
    if (authenticated && state.token) headers.set('Authorization', `Bearer ${state.token}`);
    const response = await fetch(path, {...options, headers, cache: 'no-store'});
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (response.status === 401 && authenticated) {
      forgetLocal();
      throw new Error('Pairing expired');
    }
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  };

  const fetchBlob = async path => {
    const response = await fetch(path, {headers: {'Authorization': `Bearer ${state.token}`}, cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.blob();
  };

  const forgetLocal = () => {
    state.token = '';
    state.deviceId = '';
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(DEVICE_KEY);
  };

  const platformName = () => /iPad/i.test(navigator.userAgent) ? 'ipad-web' : 'iphone-web';

  const pair = async pairingToken => {
    const payload = await api('/api/v1/pair/complete', {
      method: 'POST',
      body: JSON.stringify({
        pairing_token: String(pairingToken || '').trim(),
        name: platformName() === 'ipad-web' ? 'Pudge on iPad' : 'Pudge on iPhone',
        platform: platformName(),
      }),
    }, false);
    state.token = String(payload.access_token || '');
    state.deviceId = String(payload.device_id || '');
    localStorage.setItem(TOKEN_KEY, state.token);
    localStorage.setItem(DEVICE_KEY, state.deviceId);
    history.replaceState({}, '', '/companion/');
  };

  const progress = entity => {
    const p = entity.position || {};
    if (entity.kind === 'anime_episode') {
      const total = Number(p.duration_ms || 0);
      return [`Episode ${p.episode || entity.metadata?.episode || ''}`.trim(), total ? Number(p.position_ms || 0) / total : 0];
    }
    if (entity.kind === 'manga') {
      const total = Number(p.page_count || entity.metadata?.page_count || 0);
      const page = Number(p.page_index || 0);
      return [total ? `Page ${page + 1} / ${total}` : `Page ${page + 1}`, total > 1 ? page / (total - 1) : 0];
    }
    if (entity.kind === 'light_novel') return [`Chapter ${Number(p.chapter_index || 0) + 1}`, Number(p.fraction || 0)];
    if (entity.kind === 'audiobook') {
      const total = Number(p.duration_ms || 0);
      return [total ? `${Math.floor(Number(p.position_ms || 0) / 60000)} / ${Math.ceil(total / 60000)} min` : 'Audiobook', total ? Number(p.position_ms || 0) / total : 0];
    }
    return ['', entity.status === 'completed' ? 1 : 0];
  };

  const naturalTitle = value => String(value || '').normalize('NFKC').replace(/\s+/g, ' ').trim();

  const fallbackLnSeriesTitle = value => {
    let text = naturalTitle(value);
    text = text.replace(/[（(][^()（）]{0,80}[)）]\s*$/u, ' ');
    text = text.replace(/\b(?:light[ ._-]*novel|novel|vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}(?:\.\d+)?\b/giu, ' ');
    text = text.replace(/第\s*0*\d{1,3}(?:\.\d+)?\s*巻/gu, ' ');
    text = text.replace(/\s*0*\d{1,3}(?:\.\d+)?\s*巻\s*$/gu, ' ');
    text = text.replace(/\s+/g, ' ').trim();
    return text || naturalTitle(value);
  };

  const lnSeriesKey = entity => {
    const metadata = entity?.metadata || {};
    const explicit = String(metadata.series_key || '').trim();
    if (explicit) return explicit;
    const anilist = Number(metadata.anilist_id || 0);
    if (anilist > 0) return `anilist:${anilist}`;
    return `title:${fallbackLnSeriesTitle(entity?.title).toLocaleLowerCase()}`;
  };

  const lnSeriesTitle = entity => String(entity?.metadata?.series_title || fallbackLnSeriesTitle(entity?.title) || entity?.title || 'Light novel');

  const volumeNumber = entity => {
    const explicit = Number(entity?.metadata?.volume);
    if (Number.isFinite(explicit) && explicit > 0) return explicit;
    const match = naturalTitle(entity?.title).match(/(?:\b(?:vol(?:ume)?|v)\s*|第\s*)(\d{1,3})(?:\s*巻)?/iu);
    return match ? Number(match[1]) : 0;
  };

  const sortVolumes = entities => [...entities].sort((a, b) => {
    const av = volumeNumber(a), bv = volumeNumber(b);
    if (av && bv && av !== bv) return av - bv;
    if (av !== bv) return av ? -1 : 1;
    return naturalTitle(a.title).localeCompare(naturalTitle(b.title), undefined, {numeric: true});
  });

  const groupLightNovels = entities => {
    const groups = new Map();
    for (const entity of entities.filter(item => item.kind === 'light_novel')) {
      const key = lnSeriesKey(entity);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entity);
    }
    return [...groups.entries()].map(([key, volumes]) => {
      const ordered = sortVolumes(volumes);
      const inProgress = ordered.filter(item => item.status === 'in_progress');
      const completed = ordered.filter(item => item.status === 'completed');
      const continueEntity = [...inProgress].sort((a, b) => Number(b.occurred_at || 0) - Number(a.occurred_at || 0))[0]
        || ordered.find(item => item.status !== 'completed') || ordered[ordered.length - 1];
      const coverEntity = ordered.find(item => String(item.metadata?.cover_url || '').trim()) || continueEntity || ordered[0];
      return {
        type:'ln-series',
        mediaKind:'light_novel',
        key,
        title:lnSeriesTitle(ordered[0]),
        items:ordered,
        volumes:ordered,
        continueEntity,
        coverEntity,
        completedCount:completed.length,
        updatedAt:Math.max(...ordered.map(item => Number(item.occurred_at || 0)), 0),
      };
    });
  };

  const mangaSeriesKey = entity => String(
    entity?.metadata?.series_key ||
    (Number(entity?.metadata?.anilist_id || 0) > 0
      ? `anilist:${Number(entity.metadata.anilist_id)}`
      : `title:${fallbackLnSeriesTitle(entity?.title).toLocaleLowerCase()}`)
  );

  const mangaSeriesTitle = entity => String(
    entity?.metadata?.series_title ||
    fallbackLnSeriesTitle(entity?.title) ||
    entity?.title ||
    'Manga'
  );

  const groupManga = entities => {
    const groups = new Map();
    for (const entity of entities.filter(item => item.kind === 'manga')) {
      const key = mangaSeriesKey(entity);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entity);
    }
    return [...groups.entries()].map(([key, volumes]) => {
      const ordered = sortVolumes(volumes);
      const inProgress = ordered.filter(item => item.status === 'in_progress');
      const completed = ordered.filter(item => item.status === 'completed');
      const continueEntity = [...inProgress].sort((a, b) => Number(b.occurred_at || 0) - Number(a.occurred_at || 0))[0]
        || ordered.find(item => item.status !== 'completed') || ordered[ordered.length - 1];
      const coverEntity = ordered.find(item => String(item.metadata?.cover_url || '').trim()) || continueEntity || ordered[0];
      return {
        type:'manga-series',
        mediaKind:'manga',
        key,
        title:mangaSeriesTitle(ordered[0]),
        items:ordered,
        volumes:ordered,
        continueEntity,
        coverEntity,
        completedCount:completed.length,
        updatedAt:Math.max(...ordered.map(item => Number(item.occurred_at || 0)), 0),
      };
    });
  };

  const groupAnime = entities => {
    const groups = new Map();
    for (const entity of entities.filter(item => item.kind === 'anime_episode')) {
      const mediaId = Number(entity.metadata?.media_id || 0);
      const key = mediaId > 0
        ? `anime:${mediaId}`
        : `anime-title:${String(entity.metadata?.anime_title || entity.title || '').toLocaleLowerCase()}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entity);
    }
    return [...groups.entries()].map(([key, episodes]) => {
      const ordered = [...episodes].sort(
        (a, b) => Number(a.metadata?.episode || a.position?.episode || 0)
          - Number(b.metadata?.episode || b.position?.episode || 0)
      );
      const inProgress = ordered.filter(item => item.status === 'in_progress');
      const completed = ordered.filter(item => item.status === 'completed');
      const continueEntity = [...inProgress].sort(
        (a, b) => Number(b.occurred_at || 0) - Number(a.occurred_at || 0)
      )[0] || ordered.find(item => item.status !== 'completed') || ordered[ordered.length - 1];
      const coverEntity = ordered.find(item => String(item.metadata?.cover_url || '').trim()) || ordered[0];
      return {
        type:'anime-series',
        mediaKind:'anime_episode',
        key,
        title:String(ordered[0]?.metadata?.anime_title || ordered[0]?.title || 'Anime'),
        items:ordered,
        episodes:ordered,
        continueEntity,
        coverEntity,
        completedCount:completed.length,
        updatedAt:Math.max(...ordered.map(item => Number(item.occurred_at || 0)), 0),
      };
    });
  };

  const allSeries = () => [
    ...groupAnime(state.entities),
    ...groupManga(state.entities),
    ...groupLightNovels(state.entities),
  ];

  const seriesMatchesFilter = series => {
    if (state.filter === 'all') return true;
    if (state.filter === 'continue') return series.items.some(item => item.status === 'in_progress');
    return series.mediaKind === state.filter;
  };

  const seriesMatchesSearch = (series, search) => {
    if (!search) return true;
    return `${series.title} ${series.items.map(item => item.title).join(' ')}`.toLocaleLowerCase().includes(search);
  };

  const libraryItems = () => {
    const search = state.search.toLocaleLowerCase().trim();
    const groupedKinds = new Set(['light_novel','manga','anime_episode']);
    const regularItems = state.entities
      .filter(entity => !groupedKinds.has(entity.kind))
      .filter(entity => {
        if (state.filter === 'continue' && entity.status !== 'in_progress') return false;
        if (!['all','continue'].includes(state.filter) && entity.kind !== state.filter) return false;
        if (!search) return true;
        return `${entity.title || ''} ${JSON.stringify(entity.metadata || {})}`.toLocaleLowerCase().includes(search);
      })
      .map(entity => ({type:'entity', entity, updatedAt:Number(entity.occurred_at || 0)}));

    const seriesItems = allSeries().filter(
      series => seriesMatchesFilter(series) && seriesMatchesSearch(series, search)
    );

    return [...seriesItems, ...regularItems].sort((a,b) => {
      const aContinue = a.type === 'entity'
        ? a.entity.status === 'in_progress'
        : a.items.some(item => item.status === 'in_progress');
      const bContinue = b.type === 'entity'
        ? b.entity.status === 'in_progress'
        : b.items.some(item => item.status === 'in_progress');
      if (aContinue !== bContinue) return bContinue ? 1 : -1;
      return Number(b.updatedAt || 0) - Number(a.updatedAt || 0);
    });
  };

  const coverEndpoint = entity => `/api/v1/content/${encodeURIComponent(entity.entity_id)}/cover`;
  const attachCover = (img, entity, placeholder) => {
    const raw = String(entity?.metadata?.cover_url || '').trim();
    if (/^https?:\/\//i.test(raw)) { img.src = raw; img.hidden = false; if (placeholder) placeholder.hidden = true; return; }
    const cached = state.coverBlobUrls.get(entity?.entity_id);
    if (cached) { img.src = cached; img.hidden = false; if (placeholder) placeholder.hidden = true; return; }
    fetchBlob(coverEndpoint(entity)).then(blob => {
      if (!blob?.size) return;
      const url = URL.createObjectURL(blob);
      state.coverBlobUrls.set(entity.entity_id, url);
      img.src = url; img.hidden = false; if (placeholder) placeholder.hidden = true;
    }).catch(() => {});
  };

  const volumeLabel = entity => volumeNumber(entity) ? `Vol. ${volumeNumber(entity)}` : naturalTitle(entity?.title || 'Volume');

  const filtered = () => {
    const search = state.search.toLocaleLowerCase().trim();
    return state.entities.filter(entity => {
      if (state.filter === 'continue' && entity.status !== 'in_progress') return false;
      if (!['all', 'continue'].includes(state.filter) && entity.kind !== state.filter) return false;
      if (!search) return true;
      return `${entity.title || ''} ${JSON.stringify(entity.metadata || {})}`.toLocaleLowerCase().includes(search);
    });
  };

  const entityCard = entity => {
    const [label, fraction] = progress(entity);
    const card = document.createElement('article'); card.className = 'card media-card'; card.dataset.entityId = entity.entity_id; card.tabIndex = 0; card.setAttribute('role','button');
    const media = document.createElement('div'); media.className = 'card-media';
    const img = document.createElement('img'); img.className='cover'; img.loading='lazy'; img.alt=''; img.hidden=true;
    const placeholder = document.createElement('div'); placeholder.className='placeholder'; placeholder.textContent=String(entity.title || 'P').charAt(0).toUpperCase(); media.append(img,placeholder);
    const body=document.createElement('div'); body.className='body'; body.innerHTML=`<div class="eyebrow"></div><div class="title"></div><div class="meta"></div><div class="progress"><span></span></div>`;
    body.querySelector('.eyebrow').textContent=({anime_episode:'ANIME',manga:'MANGA',audiobook:'AUDIO'}[entity.kind]||entity.kind||'').toUpperCase(); body.querySelector('.title').textContent=String(entity.title||'Untitled'); body.querySelector('.meta').textContent=label; body.querySelector('.progress span').style.width=`${Math.max(0,Math.min(1,Number(fraction||0)))*100}%`;
    if(entity.status==='completed'){const done=document.createElement('div');done.className='done';done.textContent='Completed';body.appendChild(done)}
    card.append(media,body); const cover=String(entity.metadata?.cover_url||''); if(cover){img.src=cover;img.hidden=false;placeholder.hidden=true} return card;
  };

  const seriesItemNumber = (series, entity) => {
    if (series.type === 'anime-series') {
      return Number(entity.metadata?.episode || entity.position?.episode || 0);
    }
    return volumeNumber(entity);
  };

  const seriesItemLabel = (series, entity) => {
    const number = seriesItemNumber(series, entity);
    if (series.type === 'anime-series') return number ? `Ep. ${number}` : 'Episode';
    return number ? `Vol. ${number}` : volumeLabel(entity);
  };

  const seriesCard = series => {
    const card=document.createElement('article');
    card.className=`card media-card series-card ${series.type}`;
    card.dataset.seriesKey=series.key;
    card.dataset.seriesKind=series.type;
    card.tabIndex=0;
    card.setAttribute('role','button');

    const media=document.createElement('div');
    media.className='card-media';
    const img=document.createElement('img');
    img.className='cover';
    img.loading='lazy';
    img.alt='';
    img.hidden=true;
    const placeholder=document.createElement('div');
    placeholder.className='placeholder';
    placeholder.textContent=String(series.title||'P').charAt(0).toUpperCase();
    media.append(img,placeholder);
    if(series.coverEntity)attachCover(img,series.coverEntity,placeholder);

    const total=series.items.length;
    const current=series.continueEntity?seriesItemLabel(series,series.continueEntity):'';
    const noun=series.type==='anime-series'?'episode':'volume';
    const eyebrow=series.type==='anime-series'
      ?'ANIME'
      :series.type==='manga-series'
        ?'MANGA'
        :'LIGHT NOVEL';

    const body=document.createElement('div');
    body.className='body';
    body.innerHTML=`<div class="eyebrow"></div><div class="title"></div><div class="meta"></div><div class="series-volume-strip"></div><div class="progress"><span></span></div><div class="card-actions"></div>`;
    body.querySelector('.eyebrow').textContent=eyebrow;
    body.querySelector('.title').textContent=series.title;
    body.querySelector('.meta').textContent=`${total} ${noun}${total===1?'':'s'}${current?` · ${current}`:''}`;
    body.querySelector('.progress span').style.width=`${total ? series.completedCount/total*100 : 0}%`;

    const strip=body.querySelector('.series-volume-strip');
    for(const entity of series.items.slice(0,7)){
      const chip=document.createElement('span');
      chip.className=`volume-chip ${entity.status==='completed'?'complete':entity.status==='in_progress'?'active':''}`;
      chip.textContent=seriesItemNumber(series,entity)||'•';
      strip.appendChild(chip);
    }
    if(series.items.length>7){
      const more=document.createElement('span');
      more.className='volume-chip more';
      more.textContent=`+${series.items.length-7}`;
      strip.appendChild(more);
    }

    if(series.continueEntity&&series.continueEntity.status==='in_progress'){
      const button=document.createElement('button');
      button.type='button';
      button.className='continue-button';
      button.dataset.continueEntityId=series.continueEntity.entity_id;
      button.textContent=`Continue ${seriesItemLabel(series,series.continueEntity)}`;
      body.querySelector('.card-actions').appendChild(button);
    }

    card.append(media,body);
    return card;
  };

  const render = () => {
    const grid=$('#libraryGrid');
    const rows=libraryItems();
    grid.textContent='';
    $('#libraryEmpty').hidden=Boolean(rows.length);
    const visibleCount=allSeries().length
      +state.entities.filter(item=>!['light_novel','manga','anime_episode'].includes(item.kind)).length;
    $('#librarySummary').textContent=`${visibleCount} titles · ${state.entities.length} items`;
    for(const item of rows){
      grid.appendChild(item.type==='entity'?entityCard(item.entity):seriesCard(item));
    }
  };

  const showPair = message => {
    $('#pairView').hidden = false;
    $('#libraryView').hidden = true;
    $('#readerView').hidden = true;
    $('#animePlayerView').hidden = true;
    $('#refreshButton').hidden = true;
    $('#forgetDevice').hidden = true;
    $('#connectionState').textContent = message || 'Not paired';
  };

  const showReader = () => {
    $('#readerView').hidden = false;
    $('#mainTopbar').hidden = true;
    $('#mainFooter').hidden = true;
  };

  const hideReader = () => {
    if (state.readerBlobUrl) {
      URL.revokeObjectURL(state.readerBlobUrl);
      state.readerBlobUrl = '';
    }
    $('#readerView').hidden = true;
    $('#mainTopbar').hidden = false;
    $('#mainFooter').hidden = false;
    state.reader = null;
  };

  const syncReaderProgress = async () => {
    const reader = state.reader;
    if (!reader?.entity || !reader.content) return;
    const entity = reader.entity;
    let position;

    if (entity.kind === 'light_novel') {
      const scroll = $('#readerScroll');
      const maxScroll = Math.max(1, scroll.scrollHeight - scroll.clientHeight);
      const fraction = Math.max(0, Math.min(1, scroll.scrollTop / maxScroll));
      const length = Number(reader.content.chapter_length || 0);
      position = {
        chapter_index: reader.index,
        character_offset: Math.round(fraction * length),
        chapter_length: length,
        chapter_hash: String(reader.content.chapter_hash || ''),
        fraction,
      };
    } else if (entity.kind === 'manga') {
      position = {page_index: reader.index, page_count: Number(reader.content.page_count || 0)};
    } else {
      return;
    }

    try {
      await api('/api/v1/sync/events', {
        method: 'POST',
        body: JSON.stringify({
          events: [{
            event_id: crypto.randomUUID ? crypto.randomUUID() : `iphone-${Date.now()}-${Math.random()}`,
            entity_id: entity.entity_id,
            type: 'progress.updated',
            payload: {position, status: 'in_progress'},
            occurred_at: Date.now() / 1000,
          }],
        }),
      });
      entity.position = position;
      entity.status = 'in_progress';
    } catch (_) {}
  };

  const openReaderIndex = async index => {
    const reader = state.reader;
    if (!reader?.entity) return;
    const entity = reader.entity;
    const safeIndex = Math.max(0, Number(index || 0));
    $('#readerPrev').disabled = true;
    $('#readerNext').disabled = true;
    $('#lnReaderText').hidden = true;
    $('#mangaReaderPage').hidden = true;
    $('#readerUnsupported').hidden = true;

    const content = await api(`/api/v1/content/${encodeURIComponent(entity.entity_id)}?index=${safeIndex}`);
    reader.index = Number(content.index || 0);
    reader.content = content;
    $('#readerTitle').textContent = entity.title || 'Pudge';

    if (!content.supported) {
      $('#readerSubtitle').textContent = '';
      const unsupported = $('#readerUnsupported');
      unsupported.textContent = entity.kind === 'anime_episode'
        ? 'Anime streaming to iPhone is the next companion step.'
        : 'Audiobook streaming and shadowing on iPhone are the next companion step.';
      unsupported.hidden = false;
      return;
    }

    if (entity.kind === 'light_novel') {
      $('#readerSubtitle').textContent = content.chapter_title || `Chapter ${reader.index + 1}`;
      const text = $('#lnReaderText');
      text.textContent = String(content.text || '');
      text.hidden = false;
      requestAnimationFrame(() => {
        const scroll = $('#readerScroll');
        scroll.scrollTop = Math.max(0, Math.min(1, Number(content.fraction || 0))) * Math.max(0, scroll.scrollHeight - scroll.clientHeight);
      });
    } else if (entity.kind === 'manga') {
      $('#readerSubtitle').textContent = `Page ${reader.index + 1} / ${content.page_count}`;
      const blob = await fetchBlob(`/api/v1/content/${encodeURIComponent(entity.entity_id)}/page?index=${reader.index}`);
      if (state.readerBlobUrl) URL.revokeObjectURL(state.readerBlobUrl);
      state.readerBlobUrl = URL.createObjectURL(blob);
      $('#mangaReaderImage').src = state.readerBlobUrl;
      $('#mangaReaderPage').hidden = false;
      $('#readerScroll').scrollTop = 0;
    }

    $('#readerPrev').disabled = reader.index <= 0;
    $('#readerNext').disabled = reader.index >= Number(content.total_items || content.page_count || 1) - 1;
  };

  const animeSiblings = entity => {
    const mediaId = Number(entity?.metadata?.media_id || 0);
    return state.entities
      .filter(item => item.kind === 'anime_episode' && Number(item.metadata?.media_id || 0) === mediaId)
      .sort((a, b) => Number(a.metadata?.episode || a.position?.episode || 0) - Number(b.metadata?.episode || b.position?.episode || 0));
  };

  const parseVttTime = raw => {
    const parts = String(raw || '').trim().split(':').map(Number);
    if (parts.some(value => !Number.isFinite(value))) return 0;
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return parts[0] || 0;
  };

  const decodeSubtitleEntities = value => {
    const area = document.createElement('textarea');
    area.innerHTML = String(value || '');
    return area.value;
  };

  const cleanSubtitleText = value => decodeSubtitleEntities(
    String(value || '')
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<[^>]+>/g, '')
      .replace(/\u200b/g, '')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n[ \t]+/g, '\n')
      .trim()
  );

  const parseWebVtt = text => {
    const blocks = String(text || '').replace(/\r/g, '').split(/\n\n+/);
    const cues = [];
    for (const block of blocks) {
      const lines = block.split('\n').filter(Boolean);
      const timingIndex = lines.findIndex(line => line.includes('-->'));
      if (timingIndex < 0) continue;
      const timing = lines[timingIndex].split('-->');
      const start = parseVttTime(timing[0]);
      const end = parseVttTime(String(timing[1] || '').split(/\s+/)[0]);
      const cueText = cleanSubtitleText(lines.slice(timingIndex + 1).join('\n'));
      if (cueText && end >= start) cues.push({start, end, text: cueText});
    }
    return cues;
  };

  const subtitleOffsetKey = entity => `pudge.companion.subtitle.offset.${entity?.entity_id || 'global'}`;
  const clampSubtitleOffset = value => Math.max(-30, Math.min(30, Number(value || 0)));

  const loadSubtitlePreferences = entity => {
    const raw = localStorage.getItem(subtitleOffsetKey(entity));
    state.subtitleOffsetSeconds = clampSubtitleOffset(raw === null ? 0 : Number(raw));
    state.subtitleScale = Math.max(.72, Math.min(1.65, Number(state.subtitleScale || 1)));
    applySubtitlePreferences();
  };

  const saveSubtitleOffset = () => {
    if (!state.animePlayer?.entity) return;
    localStorage.setItem(
      subtitleOffsetKey(state.animePlayer.entity),
      String(Number(state.subtitleOffsetSeconds || 0).toFixed(2))
    );
  };

  const applySubtitlePreferences = () => {
    const overlay = $('#animeSubtitleOverlay');
    overlay.style.setProperty('--subtitle-scale', String(state.subtitleScale));
    const offset = Number(state.subtitleOffsetSeconds || 0);
    const label = $('#animeSubtitleOffsetValue');
    if (label) label.textContent = `${offset > 0 ? '+' : ''}${offset.toFixed(1)}s`;
    const size = $('#animeSubtitleSizeValue');
    if (size) size.textContent = `${Math.round(state.subtitleScale * 100)}%`;
  };

  const normalizeStudyState = card => {
    const explicit = String(card?.normalizedState || '').toLowerCase();
    if (explicit) return explicit;
    const raw = card?.knownState ?? card?.cardState ?? card?.states ?? [];
    const states = (Array.isArray(raw) ? raw : [raw]).map(item => String(item || '').toLowerCase());
    if (states.some(item => ['due', 'failed'].includes(item))) return 'due';
    if (states.some(item => ['known', 'mastered', 'never-forget'].includes(item))) return 'known';
    if (states.some(item => ['learning', 'young', 'mature'].includes(item))) return 'learning';
    if (states.includes('blacklisted')) return 'blacklisted';
    return 'new';
  };

  const applyStudyColors = settings => {
    const overlay = $('#animeSubtitleOverlay');
    const defaults = {
      new: '#f3f6fb',
      learning: '#f4bd63',
      due: '#ff7d8c',
      known: '#57d38c',
      blacklisted: '#7d8795',
    };
    for (const [key, fallback] of Object.entries(defaults)) {
      overlay.style.setProperty(`--study-${key}`, String(settings?.[`word_color_${key}`] || fallback));
    }
  };

  const fallbackSubtitleSegments = text => {
    try {
      if (Intl?.Segmenter) {
        const segmenter = new Intl.Segmenter('ja', {granularity: 'word'});
        return [...segmenter.segment(text)]
          .filter(part => String(part.segment || ''))
          .map(part => ({
            start: Number(part.index || 0),
            end: Number(part.index || 0) + String(part.segment || '').length,
            surface: String(part.segment || ''),
            wordLike: part.isWordLike !== false,
            card: null,
          }));
      }
    } catch (_) {}
    const result = [];
    const pattern = /[\u3400-\u9fff々〆ヵヶぁ-ゟ゠-ヿー]+|[A-Za-z0-9]+|./gu;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      result.push({
        start: match.index,
        end: match.index + match[0].length,
        surface: match[0],
        wordLike: /[\u3400-\u9fff々〆ヵヶぁ-ゟ゠-ヿーA-Za-z0-9]/u.test(match[0]),
        card: null,
      });
    }
    return result;
  };

  const studySegments = (text, payload) => {
    const study = payload?.study || payload || {};
    const tokenGroups = Array.isArray(study.tokens) ? study.tokens : [];
    const tokens = Array.isArray(tokenGroups[0]) ? tokenGroups[0] : tokenGroups;
    const vocabulary = Array.isArray(study.vocabulary) ? study.vocabulary : [];
    if (!Array.isArray(tokens) || !tokens.length) return fallbackSubtitleSegments(text);

    const vocab = new Map();
    for (const card of vocabulary) {
      const wordId = String(card?.wordId ?? card?.word_id ?? '');
      const readingIndex = String(card?.readingIndex ?? card?.reading_index ?? 0);
      if (wordId) vocab.set(`${wordId}:${readingIndex}`, card);
    }

    const result = [];
    for (const token of tokens) {
      const start = Number(token?.start);
      const end = Number(token?.end);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || start < 0 || start >= text.length) continue;
      const safeEnd = Math.min(text.length, end);
      const surface = text.slice(start, safeEnd);
      const wordId = String(token?.wordId ?? token?.word_id ?? '');
      const readingIndex = String(token?.readingIndex ?? token?.reading_index ?? 0);
      result.push({
        start,
        end: safeEnd,
        surface,
        wordLike: Boolean(wordId) || /[\u3400-\u9fff々〆ヵヶぁ-ゟ゠-ヿーA-Za-z0-9]/u.test(surface),
        card: vocab.get(`${wordId}:${readingIndex}`) || null,
      });
    }
    return result.length ? result : fallbackSubtitleSegments(text);
  };

  const parseSubtitleStudy = async text => {
    const key = String(text || '').trim();
    if (!key) return null;
    if (state.subtitleParseCache.has(key)) return state.subtitleParseCache.get(key);
    try {
      const payload = await api('/api/v1/study/parse', {
        method: 'POST',
        body: JSON.stringify({text: key}),
      });
      state.subtitleParseCache.set(key, payload);
      return payload;
    } catch (_) {
      state.subtitleParseCache.set(key, null);
      return null;
    }
  };

  const appendSubtitleText = (overlay, text) => {
    if (text) overlay.appendChild(document.createTextNode(text));
  };

  const renderSubtitleCue = async cue => {
    const overlay = $('#animeSubtitleOverlay');
    const cueKey = `${cue.start.toFixed(3)}:${cue.end.toFixed(3)}:${cue.text}`;
    if (state.subtitleActiveCueKey === cueKey && overlay.childNodes.length) return;

    state.subtitleActiveCueKey = cueKey;
    state.subtitleActiveCue = cue;
    const requestId = ++state.subtitleParseRequest;
    overlay.textContent = cue.text;
    overlay.hidden = false;
    overlay.classList.add('subtitle-unparsed');

    const payload = await parseSubtitleStudy(cue.text);
    if (requestId !== state.subtitleParseRequest || state.subtitleActiveCueKey !== cueKey || !state.animePlayer) return;

    const study = payload?.study || payload || {};
    applyStudyColors(study.settings || {});
    const segments = studySegments(cue.text, payload);

    overlay.textContent = '';
    overlay.classList.remove('subtitle-unparsed');
    let cursor = 0;
    for (const segment of segments) {
      if (segment.start > cursor) appendSubtitleText(overlay, cue.text.slice(cursor, segment.start));
      const surface = String(segment.surface || '');
      if (!surface) continue;
      if (segment.wordLike) {
        const token = document.createElement('span');
        token.className = `subtitle-token state-${normalizeStudyState(segment.card)}`;
        token.dataset.subtitleToken = surface;
        token.tabIndex = 0;
        token.setAttribute('role', 'button');
        token.textContent = surface;
        token._pudgeCard = segment.card || null;
        overlay.appendChild(token);
      } else {
        appendSubtitleText(overlay, surface);
      }
      cursor = Math.max(cursor, segment.end);
    }
    if (cursor < cue.text.length) appendSubtitleText(overlay, cue.text.slice(cursor));
  };

  const clearSubtitleCue = () => {
    state.subtitleActiveCueKey = '';
    state.subtitleActiveCue = null;
    state.subtitleParseRequest += 1;
    const overlay = $('#animeSubtitleOverlay');
    overlay.hidden = true;
    overlay.textContent = '';
  };

  const updateAnimeSubtitle = () => {
    const video = $('#animeVideo');
    if (!state.subtitleVisible || !state.subtitleCues.length || !state.animePlayer) {
      clearSubtitleCue();
      return;
    }
    const now = Number(video.currentTime || 0) + Number(state.subtitleOffsetSeconds || 0);
    const cue = state.subtitleCues.find(item => item.start <= now && now <= item.end);
    if (!cue) {
      clearSubtitleCue();
      return;
    }
    renderSubtitleCue(cue);
  };

  const subtitleMeanings = card => {
    const raw = card?.meanings ?? card?.meaningsChunks ?? [];
    const items = Array.isArray(raw) ? raw.flat?.(2) || raw : [];
    return items
      .map(item => typeof item === 'string' ? item : item?.meaning || item?.text || '')
      .map(item => String(item || '').trim())
      .filter(Boolean)
      .slice(0, 8);
  };

  const showSubtitleStudySheet = (surface, card, cue) => {
    const video = $('#animeVideo');
    state.subtitleResumeAfterSheet = !video.paused;
    video.pause();

    $('#animeStudyWord').textContent = String(card?.spelling || surface || '');
    $('#animeStudyReading').textContent = String(card?.reading || '');
    $('#animeStudyState').textContent = ({
      new: 'New',
      learning: 'Learning',
      due: 'Due',
      known: 'Known',
      blacklisted: 'Ignored',
    }[normalizeStudyState(card)] || 'New');

    const accents = (card?.pitchAccents || card?.pitch_accents || [])
      .map(Number)
      .filter(value => Number.isInteger(value) && value >= 0);
    $('#animeStudyPitch').textContent = accents.length ? `Pitch: ${accents.join(', ')}` : '';

    const meanings = subtitleMeanings(card);
    const list = $('#animeStudyMeanings');
    list.textContent = '';
    if (meanings.length) {
      for (const meaning of meanings) {
        const item = document.createElement('li');
        item.textContent = meaning;
        list.appendChild(item);
      }
    } else {
      const item = document.createElement('li');
      item.className = 'study-muted';
      item.textContent = card ? 'No meanings returned' : 'Jiten parsing unavailable — copy the word or line.';
      list.appendChild(item);
    }

    const sheet = $('#animeSubtitleStudySheet');
    sheet.dataset.surface = String(surface || '');
    sheet.dataset.line = String(cue?.text || '');
    sheet.hidden = false;
  };

  const closeSubtitleStudySheet = ({resume = false} = {}) => {
    $('#animeSubtitleStudySheet').hidden = true;
    if (resume && state.subtitleResumeAfterSheet) $('#animeVideo').play().catch(() => {});
    state.subtitleResumeAfterSheet = false;
  };

  const releaseWakeLock = async () => {
    const lock = state.wakeLock;
    state.wakeLock = null;
    if (lock) {
      try { await lock.release(); } catch (_) {}
    }
  };

  const requestWakeLock = async () => {
    if (!navigator.wakeLock?.request || state.wakeLock) return;
    try { state.wakeLock = await navigator.wakeLock.request('screen'); } catch (_) {}
  };

  const syncAnimeProgress = async forcedStatus => {
    const player = state.animePlayer;
    if (!player?.entity) return;
    const video = $('#animeVideo');
    const durationSeconds = Number.isFinite(video.duration) && video.duration > 0
      ? video.duration
      : Number(player.stream?.duration_ms || player.entity.position?.duration_ms || 0) / 1000;
    const positionSeconds = Math.max(0, Number(video.currentTime || 0));
    const completed = forcedStatus === 'completed' || (durationSeconds > 0 && positionSeconds / durationSeconds >= 0.92);
    const position = {
      episode: Number(player.entity.metadata?.episode || player.entity.position?.episode || 0),
      position_ms: Math.round(positionSeconds * 1000),
      duration_ms: Math.round(Math.max(0, durationSeconds) * 1000),
    };
    try {
      await api('/api/v1/sync/events', {
        method: 'POST',
        body: JSON.stringify({events: [{
          event_id: crypto.randomUUID ? crypto.randomUUID() : `iphone-anime-${Date.now()}-${Math.random()}`,
          entity_id: player.entity.entity_id,
          type: 'progress.updated',
          payload: {position, status: completed ? 'completed' : 'in_progress'},
          occurred_at: Date.now() / 1000,
        }]}),
      });
      player.entity.position = position;
      player.entity.status = completed ? 'completed' : 'in_progress';
      state.lastAnimeSyncAt = Date.now();
    } catch (_) {}
  };

  const showAnimePlayer = () => {
    $('#animePlayerView').hidden = false;
    $('#mainTopbar').hidden = true;
    $('#mainFooter').hidden = true;
  };

  const hideAnimePlayer = async () => {
    const video = $('#animeVideo');
    video.pause();
    await releaseWakeLock();
    video.removeAttribute('src');
    video.load();
    $('#animePlayerView').hidden = true;
    $('#mainTopbar').hidden = false;
    $('#mainFooter').hidden = false;
    state.animePlayer = null;
    state.subtitleCues = [];
    state.subtitleActiveCueKey = '';
    state.subtitleActiveCue = null;
    $('#animeSubtitleOverlay').hidden = true;
    $('#animeSubtitleStudySheet').hidden = true;
  };

  const prepareAnimeStream = async entity => {
    const status = $('#animeStreamStatus');
    const transcode = $('#animeTranscodeInfo');
    status.hidden = false;
    status.textContent = 'Preparing stream on Mac…';
    transcode.textContent = '';
    let payload = null;
    for (let attempt = 0; attempt < 150; attempt += 1) {
      payload = await api(`/api/v1/content/${encodeURIComponent(entity.entity_id)}/stream`);
      if (!state.animePlayer || state.animePlayer.entity.entity_id !== entity.entity_id) return null;
      if (payload.state === 'failed') throw new Error(payload.error || 'Unable to prepare stream');
      const details = [];
      if (payload.encoder) details.push(payload.encoder === 'h264_videotoolbox' ? 'VideoToolbox' : payload.encoder);
      if (Number(payload.segment_count || 0)) details.push(`${payload.segment_count} segments`);
      transcode.textContent = details.join(' · ');
      if (payload.playlist_url) return payload;
      status.textContent = `Preparing stream on Mac${'.'.repeat((attempt % 3) + 1)}`;
      await new Promise(resolve => setTimeout(resolve, 800));
    }
    throw new Error('Stream preparation timed out');
  };

  const loadAnimeSubtitles = async stream => {
    state.subtitleCues = [];
    state.subtitleActiveCueKey = '';
    state.subtitleActiveCue = null;
    $('#animeSubtitleOverlay').hidden = true;
    const diagnostic = $('#animeSubtitleDiagnostic');
    if (!stream?.subtitles_url) {
      $('#animeSubtitleToggle').disabled = true;
      $('#animeSubtitleToggle').classList.remove('active');
      diagnostic.textContent = 'No Japanese text subtitles for this episode';
      return;
    }
    $('#animeSubtitleToggle').disabled = false;
    $('#animeSubtitleToggle').classList.toggle('active', state.subtitleVisible);
    try {
      const response = await fetch(stream.subtitles_url, {cache: 'no-store'});
      if (!response.ok) {
        diagnostic.textContent = `Subtitle load failed · HTTP ${response.status}`;
        return;
      }
      state.subtitleCues = parseWebVtt(await response.text());
      diagnostic.textContent = state.subtitleCues.length
        ? `${state.subtitleCues.length} subtitle cues · tap a word to pause + inspect`
        : 'Subtitle track is empty';
      updateAnimeSubtitle();
    } catch (error) {
      diagnostic.textContent = `Subtitle load failed · ${String(error?.message || error)}`;
    }
  };

  const openAnimePlayer = async entity => {
    const video = $('#animeVideo');
    state.animePlayer = {entity, stream: null, resumeApplied: false};
    state.lastAnimeSyncAt = 0;
    state.subtitleCues = [];
    showAnimePlayer();
    $('#animePlayerTitle').textContent = String(entity.metadata?.anime_title || entity.title || 'Anime');
    const ep = Number(entity.metadata?.episode || entity.position?.episode || 0);
    const episodeTitle = String(entity.metadata?.episode_title || '').trim();
    $('#animePlayerSubtitle').textContent = `Episode ${ep}${episodeTitle ? ` · ${episodeTitle}` : ''}`;
    loadSubtitlePreferences(entity);
    $('#animeSpeed').value = '1';
    video.playbackRate = 1;
    video.removeAttribute('src');
    video.load();

    const siblings = animeSiblings(entity);
    const index = siblings.findIndex(item => item.entity_id === entity.entity_id);
    $('#animePrevEpisode').disabled = index <= 0;
    $('#animeNextEpisode').disabled = index < 0 || index >= siblings.length - 1;
    $('#animePrevEpisode').dataset.entityId = index > 0 ? siblings[index - 1].entity_id : '';
    $('#animeNextEpisode').dataset.entityId = index >= 0 && index < siblings.length - 1 ? siblings[index + 1].entity_id : '';

    try {
      const stream = await prepareAnimeStream(entity);
      if (!stream || !state.animePlayer || state.animePlayer.entity.entity_id !== entity.entity_id) return;
      state.animePlayer.stream = stream;
      await loadAnimeSubtitles(stream);
      $('#animeStreamStatus').textContent = 'Ready — press play';
      video.src = stream.playlist_url;
      video.load();
      const resumeSeconds = Number(stream.position_ms || entity.position?.position_ms || 0) / 1000;
      const applyResume = () => {
        if (!state.animePlayer || state.animePlayer.resumeApplied) return;
        state.animePlayer.resumeApplied = true;
        if (resumeSeconds > 1 && Number.isFinite(video.duration)) {
          video.currentTime = Math.min(resumeSeconds, Math.max(0, video.duration - 1));
        }
      };
      video.addEventListener('loadedmetadata', applyResume, {once: true});
      video.addEventListener('canplay', () => { $('#animeStreamStatus').hidden = true; }, {once: true});
    } catch (error) {
      $('#animeStreamStatus').hidden = false;
      $('#animeStreamStatus').textContent = String(error.message || error);
    }
  };

  const openEntity = async entity => {
    if (entity.kind === 'anime_episode') {
      await openAnimePlayer(entity);
      return;
    }
    state.reader = {
      entity,
      index: Number(entity.kind === 'manga' ? entity.position?.page_index || 0 : entity.kind === 'light_novel' ? entity.position?.chapter_index || 0 : 0),
      content: null,
    };
    showReader();
    try {
      await openReaderIndex(state.reader.index);
    } catch (error) {
      $('#readerTitle').textContent = entity.title || 'Pudge';
      $('#readerSubtitle').textContent = '';
      $('#readerUnsupported').textContent = String(error.message || error);
      $('#readerUnsupported').hidden = false;
    }
  };

  const showSeries = () => { $('#seriesView').hidden=false; $('#mainTopbar').hidden=true; $('#mainFooter').hidden=true; };
  const hideSeries = () => { $('#seriesView').hidden=true; $('#mainTopbar').hidden=false; $('#mainFooter').hidden=false; state.series=null; };
  const openSeries = series => {
    if(!series)return;
    if(series.items.length===1){
      openEntity(series.items[0]);
      return;
    }
    state.series=series;
    showSeries();

    const isAnime=series.type==='anime-series';
    const isManga=series.type==='manga-series';
    $('#seriesKind').textContent=isAnime?'Anime':isManga?'Manga':'Light novel';
    $('#seriesItemsHeading').textContent=isAnime?'Episodes':'Volumes';
    $('#seriesTitle').textContent=series.title;
    $('#seriesMeta').textContent=`${series.items.length} ${isAnime?'episodes':'volumes'} · ${series.completedCount} completed`;

    const heroImg=$('#seriesCover');
    const heroPlaceholder=$('#seriesCoverPlaceholder');
    heroImg.hidden=true;
    heroPlaceholder.hidden=false;
    heroPlaceholder.textContent=String(series.title||'P').charAt(0).toUpperCase();
    if(series.coverEntity)attachCover(heroImg,series.coverEntity,heroPlaceholder);

    const grid=$('#volumeGrid');
    grid.textContent='';
    for(const entity of series.items){
      const [label,fraction]=progress(entity);
      const button=document.createElement('button');
      button.type='button';
      button.className=`volume-card ${isAnime?'episode-card':''} ${entity.status==='in_progress'?'active':''} ${entity.status==='completed'?'complete':''}`;
      button.dataset.volumeEntityId=entity.entity_id;

      const number=document.createElement('strong');
      number.textContent=seriesItemLabel(series,entity);
      const subtitle=document.createElement('span');
      subtitle.textContent=isAnime
        ?String(entity.metadata?.episode_title||'')
        :(label||naturalTitle(entity.title));

      const bar=document.createElement('div');
      bar.className='mini-progress';
      const fill=document.createElement('span');
      fill.style.width=`${Math.max(0,Math.min(1,Number(fraction||0)))*100}%`;
      bar.appendChild(fill);
      button.append(number,subtitle,bar);
      grid.appendChild(button);
    }

    const c=$('#seriesContinue');
    if(series.continueEntity){
      c.hidden=false;
      c.dataset.entityId=series.continueEntity.entity_id;
      c.textContent=series.continueEntity.status==='in_progress'
        ?`Continue ${seriesItemLabel(series,series.continueEntity)}`
        :`Open ${seriesItemLabel(series,series.continueEntity)}`;
    }else{
      c.hidden=true;
    }
  };

  const openLnSeries = series => openSeries(series);

  const loadLibrary = async () => {
    const payload = await api('/api/v1/library');
    state.entities = Array.isArray(payload.entities) ? payload.entities : [];
    $('#pairView').hidden = true;
    $('#libraryView').hidden = false;
    $('#refreshButton').hidden = false;
    $('#forgetDevice').hidden = false;
    $('#connectionState').textContent = 'Connected';
    render();
    await evictUnusedMedia();
    const estimate = await storageEstimate();
    if (estimate.quota) {
      $('#storageSummary').textContent = `Storage ${(estimate.usage / 1024 / 1024).toFixed(0)} MB / ${(estimate.quota / 1024 / 1024 / 1024).toFixed(1)} GB · media only on request`;
    }
  };

  $('#pairForm').addEventListener('submit', async event => {
    event.preventDefault();
    const error = $('#pairError');
    error.hidden = true;
    try {
      await pair($('#pairToken').value);
      await loadLibrary();
    } catch (reason) {
      error.textContent = String(reason.message || reason);
      error.hidden = false;
    }
  });

  $('#refreshButton').addEventListener('click', () => loadLibrary().catch(() => {}));
  $('#forgetDevice').addEventListener('click', () => { forgetLocal(); state.entities = []; showPair(); });
  $('#librarySearch').addEventListener('input', event => { state.search = event.target.value || ''; render(); });
  $('#libraryTabs').addEventListener('click', event => {
    const button = event.target.closest('[data-kind]');
    if (!button) return;
    state.filter = button.dataset.kind;
    document.querySelectorAll('#libraryTabs [data-kind]').forEach(item => item.classList.toggle('active', item === button));
    render();
  });

  $('#libraryGrid').addEventListener('click', event => {
    const continueButton=event.target.closest('[data-continue-entity-id]');if(continueButton){event.stopPropagation();const entity=state.entities.find(item=>item.entity_id===continueButton.dataset.continueEntityId);if(entity)openEntity(entity);return}
    const seriesNode=event.target.closest('.card[data-series-key]');if(seriesNode){const series=allSeries().find(item=>item.key===seriesNode.dataset.seriesKey&&item.type===seriesNode.dataset.seriesKind);if(series)openSeries(series);return}
    const card=event.target.closest('.card[data-entity-id]');if(!card)return;const entity=state.entities.find(item=>item.entity_id===card.dataset.entityId);if(entity)openEntity(entity);
  });
  $('#libraryGrid').addEventListener('keydown', event => {
    if (!['Enter', ' '].includes(event.key)) return;
    const card = event.target.closest('.card[data-entity-id], .card[data-series-key]');
    if (!card) return;
    event.preventDefault();
    card.click();
  });

  $('#seriesBack').addEventListener('click',()=>{hideSeries();render()});
  $('#seriesContinue').addEventListener('click',event=>{const entity=state.entities.find(item=>item.entity_id===event.currentTarget.dataset.entityId);if(!entity)return;hideSeries();openEntity(entity)});
  $('#volumeGrid').addEventListener('click',event=>{const card=event.target.closest('[data-volume-entity-id]');if(!card)return;const entity=state.entities.find(item=>item.entity_id===card.dataset.volumeEntityId);if(!entity)return;hideSeries();openEntity(entity)});

  $('#animePlayerBack').addEventListener('click', async () => {
    await syncAnimeProgress();
    await hideAnimePlayer();
    render();
  });
  const switchAnimeEpisode = async targetId => {
    const entity = state.entities.find(item => item.entity_id === targetId);
    if (!entity) return;
    await syncAnimeProgress();
    $('#animeVideo').pause();
    await releaseWakeLock();
    await openAnimePlayer(entity);
  };
  $('#animePrevEpisode').addEventListener('click', event => switchAnimeEpisode(event.currentTarget.dataset.entityId));
  $('#animeNextEpisode').addEventListener('click', event => switchAnimeEpisode(event.currentTarget.dataset.entityId));
  $('#animeBack10').addEventListener('click', () => { const video=$('#animeVideo'); video.currentTime=Math.max(0,Number(video.currentTime||0)-10); });
  $('#animeForward10').addEventListener('click', () => { const video=$('#animeVideo'); const end=Number.isFinite(video.duration)?video.duration:Number(video.currentTime||0)+10; video.currentTime=Math.min(end,Number(video.currentTime||0)+10); });
  $('#animeSpeed').addEventListener('change', event => { $('#animeVideo').playbackRate=Math.max(.5,Math.min(3,Number(event.target.value||1))); });
  $('#animeSubtitleToggle').addEventListener('click', event => {
    if (event.currentTarget.disabled) return;
    state.subtitleVisible=!state.subtitleVisible;
    event.currentTarget.classList.toggle('active',state.subtitleVisible);
    updateAnimeSubtitle();
  });

  const changeSubtitleOffset = delta => {
    state.subtitleOffsetSeconds = clampSubtitleOffset(Number(state.subtitleOffsetSeconds || 0) + Number(delta || 0));
    saveSubtitleOffset();
    applySubtitlePreferences();
    state.subtitleActiveCueKey = '';
    updateAnimeSubtitle();
  };
  $('#animeSubtitleOffsetMinus').addEventListener('click', () => changeSubtitleOffset(-0.5));
  $('#animeSubtitleOffsetPlus').addEventListener('click', () => changeSubtitleOffset(0.5));
  $('#animeSubtitleOffsetValue').addEventListener('click', () => {
    state.subtitleOffsetSeconds = 0;
    saveSubtitleOffset();
    applySubtitlePreferences();
    state.subtitleActiveCueKey = '';
    updateAnimeSubtitle();
  });

  const changeSubtitleScale = delta => {
    state.subtitleScale = Math.max(.72, Math.min(1.65, Number(state.subtitleScale || 1) + Number(delta || 0)));
    localStorage.setItem('pudge.companion.subtitle.scale.v1', String(state.subtitleScale));
    applySubtitlePreferences();
  };
  $('#animeSubtitleSizeMinus').addEventListener('click', () => changeSubtitleScale(-0.1));
  $('#animeSubtitleSizePlus').addEventListener('click', () => changeSubtitleScale(0.1));

  $('#animeSubtitleOverlay').addEventListener('click', event => {
    const token = event.target.closest('[data-subtitle-token]');
    if (!token) return;
    event.preventDefault();
    event.stopPropagation();
    showSubtitleStudySheet(token.dataset.subtitleToken, token._pudgeCard || null, state.subtitleActiveCue);
  });

  $('#animeSubtitleStudyClose').addEventListener('click', () => closeSubtitleStudySheet());
  $('#animeStudyResume').addEventListener('click', () => closeSubtitleStudySheet({resume:true}));
  $('#animeStudyReplayLine').addEventListener('click', () => {
    const cue = state.subtitleActiveCue;
    if (!cue) return;
    const video = $('#animeVideo');
    video.currentTime = Math.max(0, Number(cue.start || 0) - Number(state.subtitleOffsetSeconds || 0));
    closeSubtitleStudySheet({resume:true});
  });
  $('#animeStudyCopyWord').addEventListener('click', async () => {
    const text = String($('#animeSubtitleStudySheet').dataset.surface || '').trim();
    if (!text) return;
    try { await navigator.clipboard.writeText(text); } catch (_) { return; }
    $('#animeStudyCopyWord').textContent = 'Copied';
    setTimeout(() => { $('#animeStudyCopyWord').textContent = 'Copy word'; }, 700);
  });
  $('#animeStudyCopyLine').addEventListener('click', async () => {
    const text = String($('#animeSubtitleStudySheet').dataset.line || '').trim();
    if (!text) return;
    try { await navigator.clipboard.writeText(text); } catch (_) { return; }
    $('#animeStudyCopyLine').textContent = 'Copied';
    setTimeout(() => { $('#animeStudyCopyLine').textContent = 'Copy line'; }, 700);
  });
  $('#animeVideo').addEventListener('play', () => requestWakeLock());
  $('#animeVideo').addEventListener('pause', () => { releaseWakeLock(); syncAnimeProgress(); });
  $('#animeVideo').addEventListener('ended', () => { releaseWakeLock(); syncAnimeProgress('completed'); });
  $('#animeVideo').addEventListener('timeupdate', () => {
    updateAnimeSubtitle();
    if (state.animePlayer && Date.now() - state.lastAnimeSyncAt >= 12000) syncAnimeProgress();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden' && state.animePlayer) syncAnimeProgress();
  });

  $('#readerBack').addEventListener('click', async () => {
    await syncReaderProgress();
    hideReader();
    render();
  });
  $('#readerPrev').addEventListener('click', async () => {
    if (!state.reader) return;
    await syncReaderProgress();
    await openReaderIndex(state.reader.index - 1);
  });
  $('#readerNext').addEventListener('click', async () => {
    if (!state.reader) return;
    await syncReaderProgress();
    await openReaderIndex(state.reader.index + 1);
  });

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('./sw.js').catch(() => {});
  }

  (async () => {
    const pairToken = new URLSearchParams(location.search).get('pair');
    if (pairToken) {
      try {
        $('#connectionState').textContent = 'Pairing…';
        await pair(pairToken);
      } catch (error) {
        showPair();
        $('#pairToken').value = pairToken;
        $('#pairError').textContent = String(error.message || error);
        $('#pairError').hidden = false;
        return;
      }
    }
    if (!state.token) return showPair();
    try { await loadLibrary(); } catch (error) { showPair(String(error.message || 'Offline')); }
  })();
})();