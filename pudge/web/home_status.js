/* Home/download status presentation. */
function compactDownloadStatus(download){
  const state=String(download?.effective_state||download?.state||'');
  const progress=Math.max(0,Math.min(100,Math.round(Number(download?.progress||0)*100))),finished=progress>=100||/complete|completed|seeding|uploading/i.test(state);
  if(finished)return t('label.waitingSubs');
  const parts=[`${progress}%`];
  const eta=Number(download?.eta_seconds||0);
  const torrentTrafficActive=ui.torrentToggleDesired===false?false:Boolean(ui.state?.settings?.torrents_enabled&&ui.torrentTraffic?.enabled!==false);
  const offConfirmed=!torrentTrafficActive&&torrentTrafficFresh(ui.torrentTraffic,false)&&ui.torrentTraffic?.transition==='off_confirmed';
  const kind=torrentUiState(download,torrentTrafficActive,offConfirmed?'off_confirmed':'unconfirmed');
  if(kind==='paused'||kind==='unconfirmed'||kind==='waiting'||kind==='stalled'||kind==='checking'||kind==='error'||kind==='unknown')
    return `${torrentUiLabel(kind,ui.lang)} · ${progress}%`;
  if(torrentTrafficActive&&Number.isFinite(eta)&&eta>0&&torrentTrafficFresh(ui.torrentTraffic,true))parts.push(PudgeI18n.t('home.eta',{time:torrentEta(eta)}));
  return parts.join(' · ');
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
  if(p.status==='download_error')return PudgeI18n.t('home.downloadError');
  if(p.status==='ready'||p.status==='watched')return t('label.readyAll');
  return episode!==null?t('label.episodeNotReady',{episode}):t('label.notReady');
}
