/* Home/download status presentation. */
function compactDownloadStatus(download){
  const state=String(download?.effective_state||download?.state||'');
  const progress=Math.max(0,Math.min(100,Math.round(Number(download?.progress||0)*100))),finished=progress>=100||/complete|completed|seeding|uploading/i.test(state);
  if(finished)return t('label.waitingSubs');
  const parts=[`${progress}%`];
  const eta=Number(download?.eta_seconds||0);
  const torrentTrafficActive=ui.torrentToggleDesired===false?false:Boolean(ui.state?.settings?.torrents_enabled&&ui.torrentTraffic?.enabled!==false);
  const offConfirmed=!torrentTrafficActive&&torrentOffConfirmed(ui.torrentTraffic);
  let kind=torrentUiState(download,torrentTrafficActive,offConfirmed?'off_confirmed':'unconfirmed');
  // A title's card shows its download, not the torrent client: while Torrents are off it is
  // paused; whether the client confirmed the stop is the Torrents button's business.
  if(kind==='unconfirmed')kind='paused';
  if(kind==='paused'||kind==='unconfirmed'||kind==='waiting'||kind==='stalled'||kind==='checking'||kind==='error'||kind==='unknown')
    return `${torrentUiLabel(kind,ui.lang)} · ${progress}%`;
  if(torrentTrafficActive&&Number.isFinite(eta)&&eta>0&&(torrentTrafficFresh(ui.torrentTraffic,true)||liveDownloadRecent(download)))parts.push(PudgeI18n.t('home.eta',{time:torrentEta(eta)}));
  return parts.join(' · ');
}
// This download's own live reading is recent (index.html keeps them two minutes): one slow
// traffic poll (a busy torrent client) left the whole status "not fresh" past 15 s and the
// cards dropped "ETA" now and then while the torrent kept going.
function liveDownloadRecent(download,seconds=60){
  const hash=String(download?.torrent_hash||download?.hash||'').trim().toLowerCase();
  const entry=hash&&ui.liveDownloadRows?.get?.(hash);
  return !!entry&&Date.now()/1000-Number(entry.at||0)<seconds;
}
function episodePresentationStatus(a,episode){
  const p=a?.presentation||null;
  if(!p){
    if(a?.local?.state==='couldnt_sync')return PudgeI18n.t('home.couldnTSyncRawSubtitles');
    if(a?.local?.state==='waiting_text_subtitles')return t('label.waitingTextSubs');
    if(a?.local?.state==='waiting_subtitles')return t('label.waitingSubs');
    if(a?.download)return compactDownloadStatus(a.download);
    return episode!==null?t('label.episodeNotReady',{episode}):t('label.notReady');
  }
  if(p.status==='waiting_verification')return PudgeI18n.t('home.waitingForJapaneseSpeechVerification');
  if(p.status==='needs_action')return PudgeI18n.t('section.needsAction');
  if(p.status==='couldnt_sync')return PudgeI18n.t('home.couldnTSyncRawSubtitles');
  if(p.status==='waiting_text_subtitles')return t('label.waitingTextSubs');
  if(p.status==='waiting_subtitles'||p.status==='waiting_preparation')return t('label.waitingSubs');
  if(p.status==='paused_download')return a?.download?compactDownloadStatus({...a.download,state:'paused'}):(PudgeI18n.t('home.downloadPaused'));
  if(p.status==='waiting_download'||p.status==='downloading')return a?.download?compactDownloadStatus(a.download):(PudgeI18n.t('home.waitingForDownload'));
  if(p.status==='queued_torrents_off')return PudgeI18n.t('release.queuedTorrentsOff');
  if(p.status==='download_error')return PudgeI18n.t('home.downloadError');
  if(p.status==='ready'||p.status==='watched')return t('label.readyAll');
  return episode!==null?t('label.episodeNotReady',{episode}):t('label.notReady');
}
// Weekly load in the Anime page header: episodes the watched titles bring in the next
// 7 days (personal schedule items, else the next airing episode). The hover lists them.
function updateWeeklyLoad(){
  const node=document.getElementById('pageWeeklyLoad');if(!node)return;
  const load=ui.state?.weekly_load||null,total=Number(load?.total||0);
  if(ui.page!=='current'||!(total>0)){node.hidden=true;node.textContent='';delete node.dataset.tooltip;node.removeAttribute('title');node.removeAttribute('aria-label');node.removeAttribute('tabindex');return;}
  const rows=(load.items||[]).map(item=>{
    const count=Number(item.count||0);
    return PudgeI18n.t(item.source==='schedule'?'home.weeklyLoadSchedule':'home.weeklyLoadAiring',{title:String(item.title||''),episodes:PudgeI18n.tn('uiB.episodeCount',count)});
  });
  const label=PudgeI18n.tn('home.weeklyLoad',total);
  node.textContent=label;
  // The app's own hover tooltip (data-tooltip): a title attribute showed nothing on hover.
  node.dataset.tooltip=[PudgeI18n.t('home.weeklyLoadHint'),...rows].join('\n');
  node.removeAttribute('title');
  node.setAttribute('aria-label',[label,...rows].join('. '));
  node.setAttribute('tabindex','0');
  node.hidden=false;
}
