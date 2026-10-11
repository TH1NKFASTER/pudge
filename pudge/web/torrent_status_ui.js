/* Shared torrent status presentation; never infer live rates from cached DB rows. */
function torrentUiState(download,enabled,transition='enabled'){
  const raw=String(download?.state||'').trim().toLowerCase();
  const progress=Number(download?.progress||0);
  if(/error|missingfiles/.test(raw))return 'error';
  if(progress>=1||/complete|completed|seeding|uploading|forcedup|pausedup|stoppedup|queuedup|stalledup/.test(raw))return 'finished';
  if(!enabled)return transition==='off_confirmed'?'paused':'unconfirmed';
  if(/paused|stopped/.test(raw))return 'paused';
  if(/queued|waiting/.test(raw))return 'waiting';
  if(/stalled/.test(raw))return 'stalled';
  if(/checking|moving|allocating/.test(raw))return 'checking';
  if(/downloading|active|forceddl|metadl/.test(raw))return 'downloading';
  return 'unknown';
}
function torrentUiLabel(state,lang){
  const keys={finished:'torrent.state.finished',error:'ui.error',paused:'ui.paused',unconfirmed:'ui.shutdownNotConfirmed',waiting:'torrent.state.waiting',stalled:'torrent.state.stalled',checking:'torrent.state.checking',downloading:'torrent.state.downloading',unknown:'torrent.state.unknown'};
  const language=lang?(String(lang).toLowerCase().startsWith('ru')?'ru':'en'):undefined;
  return PudgeI18n.t(keys[state]||keys.unknown,{},language);
}
function torrentTrafficFresh(live,enabled,now=Date.now()/1000){
  const observed=Number(live?.updated_at||0);
  return !!live&&live.stale===false&&live.enabled===enabled&&observed>0&&now>=observed&&now-observed<15;
}
function torrentSnapshotFresh(snapshot,live,enabled,now=Date.now()/1000){
  const observed=Number(snapshot?.observed_at||0);
  return snapshot?.enabled===enabled&&torrentTrafficFresh(live,enabled,now)&&observed>0&&now>=observed&&now-observed<15&&!snapshot?.warning;
}
