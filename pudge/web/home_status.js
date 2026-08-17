/* Home/download status presentation. */
function compactDownloadStatus(download){
  const progress=Math.max(0,Math.min(100,Math.round(Number(download?.progress||0)*100))),finished=progress>=100||/complete|completed|seeding|uploading/i.test(String(download?.state||''));
  if(finished)return t('label.waitingSubs');
  const parts=[ui.lang==='ru'?`Загрузка ${progress}%`:`Downloading ${progress}%`];
  const eta=Number(download?.eta_seconds||0);
  if(Number.isFinite(eta)&&eta>0)parts.push(`ETA ${torrentEta(eta)}`);
  return parts.join(' · ');
}
function episodePresentationStatus(a,episode){
  const p=a?.presentation||null;
  if(!p){
    if(a?.local?.state==='waiting_text_subtitles')return t('label.waitingTextSubs');
    if(a?.local?.state==='waiting_subtitles')return t('label.waitingSubs');
    if(a?.download)return compactDownloadStatus(a.download);
    return episode!==null?t('label.episodeNotReady',{episode}):t('label.notReady');
  }
  if(p.status==='needs_action')return ui.lang==='ru'?'Требуется действие':'Action required';
  if(p.status==='waiting_text_subtitles')return t('label.waitingTextSubs');
  if(p.status==='waiting_subtitles'||p.status==='waiting_preparation')return t('label.waitingSubs');
  if(p.status==='waiting_download'||p.status==='downloading')return a?.download?compactDownloadStatus(a.download):(ui.lang==='ru'?'Ожидание загрузки':'Waiting for download');
  if(p.status==='download_error')return ui.lang==='ru'?'Ошибка загрузки':'Download error';
  if(p.status==='ready'||p.status==='watched')return t('label.readyAll');
  return episode!==null?t('label.episodeNotReady',{episode}):t('label.notReady');
}
