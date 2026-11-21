/* Home cards for titles that need the person: Action required (with the extra file of a
   download: Delete or Keep) and Dropped / Deleting soon. Rendered by renderCurrent(). */
'use strict';
function droppedHomeCard(a){
  const pending=Boolean(a.delete_at);
  const state=pending?t('label.dropDeletes',{time:formatRemaining(remainingUntil(a.delete_at,a.delete_remaining_seconds))}):(PudgeI18n.t('ui.filesKept'));
  const keepLabel=PudgeI18n.t('ui.keep');
  const keep=pending?`<button type="button" class="needs-action-button" data-action="keep-dropped" data-media-id="${a.media_id}" title="${escapeHtml(PudgeI18n.t('ui.doNotDeleteThisTitle'))}">${keepLabel}</button>`:'';
  return `<article class="airing-card dropped-card" data-media-id="${a.media_id}" data-site-url="${escapeHtml(a.site_url||`https://anilist.co/anime/${a.media_id}`)}" data-title="${escapeHtml(a.title)}" data-action="url" data-url="${escapeHtml(a.site_url||'')}">${cover(a,'airing-cover',false)}<div class="airing-meta"><strong data-full-title="${escapeHtml(a.title)}" title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</strong><span class="airing-state">${state}</span>${keep}</div></article>`;
}
// "2 OP, ED": the kinds of several extras, in the order they came.
function extraFileKinds(files){
  const counts=new Map();
  for(const file of files)counts.set(file.kind||'OVA',(counts.get(file.kind||'OVA')||0)+1);
  return [...counts].map(([kind,count])=>count>1?`${count} ${kind}`:kind).join(', ');
}
function extraFilesHomeCard(a,files){
  // Several extras of one title (Tatami Galaxy: OP1, OP2, ED): one card, a window to answer each.
  const names=files.map(file=>file.name).join('\n');
  return `<article class="airing-card needs-action-card extra-file-card" data-media-id="${a.media_id??''}" data-title="${escapeHtml(a.title)}" title="${escapeHtml(names)}">${cover(a,'airing-cover',false)}<div class="airing-meta"><strong title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</strong><span class="airing-state extra-file-note">${escapeHtml(PudgeI18n.t('ui.extraFilesFromDownload',{kinds:extraFileKinds(files)}))}</span><div class="extra-file-actions"><button type="button" class="needs-action-button" data-action="extra-files-open" data-media-id="${a.media_id??''}">${escapeHtml(PudgeI18n.t('ui.extraFilesChoose'))}</button></div></div></article>`;
}
function extraFileHomeCard(a){
  // An OVA/movie/NCOP that came with a batch: delete it or keep it in the library.
  const files=Array.isArray(a.extra_files)?a.extra_files:[];
  if(files.length>1)return extraFilesHomeCard(a,files);
  const file=a.extra_file||{},path=a.local?.video_path||'',name=file.name||'';
  const hint=PudgeI18n.t('ui.extraFileHint',{name});
  return `<article class="airing-card needs-action-card extra-file-card" data-media-id="${a.media_id??''}" data-title="${escapeHtml(a.title)}" title="${escapeHtml(hint)}">${cover(a,'airing-cover',false)}<div class="airing-meta"><strong title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</strong><span class="airing-state extra-file-note">${escapeHtml(PudgeI18n.t('ui.extraFileFromDownload',{kind:file.kind||'OVA'}))}</span><div class="extra-file-actions"><button type="button" class="needs-action-button" data-action="extra-file-delete" data-path="${escapeHtml(path)}" data-name="${escapeHtml(name)}">${escapeHtml(PudgeI18n.t('action.deleteExtraFile'))}</button><button type="button" class="needs-action-button" data-action="extra-file-keep" data-path="${escapeHtml(path)}">${escapeHtml(PudgeI18n.t('ui.keep'))}</button></div></div></article>`;
}
function retryExhaustedHomeCard(a){
  // The same failure several times in a row (a 20-minute timeout, one error): no more
  // automatic retries, the person retries (or picks a subtitle from "Why not ready?").
  const path=a.local?.video_path||'';
  const error=a.action_error?(window.PudgeUiLanguage?.message(a.action_error)??String(a.action_error)):'';
  return `<article class="airing-card needs-action-card retry-exhausted-card" data-media-id="${a.media_id??''}" data-title="${escapeHtml(a.title)}" title="${escapeHtml(error)}">${cover(a,'airing-cover',false)}<div class="airing-meta"><strong title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</strong><span class="airing-state">${escapeHtml(PudgeI18n.t('ui.subtitleRetryExhausted'))}</span><button type="button" class="needs-action-button" data-action="retry-needs-action" data-path="${escapeHtml(path)}">${escapeHtml(PudgeI18n.t('action.retryPreparation'))}</button></div></article>`;
}
function needsActionHomeCard(a){
  if(a.action_code==='review_extra_file')return extraFileHomeCard(a);
  if(a.action_code==='subtitle_retry_exhausted')return retryExhaustedHomeCard(a);
  const labels={configure_jimaku:PudgeI18n.t('ui.configureJimaku'),grant_folder_access:PudgeI18n.t('ui.grantFolderAccess'),enable_subtitle_ocr:PudgeI18n.t('ui.enableOcr')};
  const label=labels[a.action_code]||(PudgeI18n.t('ui.openSettings'));
  return `<article class="airing-card needs-action-card" data-media-id="${a.media_id??''}" data-title="${escapeHtml(a.title)}">${cover(a,'airing-cover',false)}<div class="airing-meta"><strong title="${escapeHtml(a.title)}">${escapeHtml(a.title)}</strong><button class="needs-action-button" data-action="open-needs-action" data-action-code="${escapeHtml(a.action_code||'')}">${escapeHtml(label)}</button></div></article>`;
}

// The window for several extras: Delete or Keep for each file, then Apply (one confirmation
// for all deletions). Answered files leave the list; the window closes with the last one.
function openExtraFilesModal(mediaId){
  const card=(ui.state?.home?.needs_action||[]).find(a=>Number(a.media_id)===Number(mediaId)&&a.action_code==='review_extra_file');
  const files=Array.isArray(card?.extra_files)?card.extra_files.filter(file=>file?.video_path):[];
  if(!card||!files.length)return false;
  ui.extraFiles={mediaId:Number(mediaId),title:String(card.title||''),files:[...files],choices:{}};
  $('modalBackdrop').hidden=false;$('modalBackdrop').classList.remove('watch-order-open');$('modalBackdrop').classList.add('open');
  $('modalTitle').textContent=PudgeI18n.t('ui.extraFilesTitle',{title:card.title||''});
  renderExtraFilesModal();
  return true;
}
function renderExtraFilesModal(){
  const st=ui.extraFiles;if(!st)return;
  const label=choice=>choice==='delete'?PudgeI18n.t('action.deleteExtraFile'):PudgeI18n.t('ui.keep');
  const rows=st.files.map(file=>{
    const chosen=st.choices[file.video_path]||'';
    const buttons=['delete','keep'].map(choice=>`<button type="button" class="extra-files-choice ${choice}${chosen===choice?' selected':''}" data-action="extra-files-choice" data-choice="${choice}" data-path="${escapeHtml(file.video_path)}" aria-pressed="${chosen===choice}">${escapeHtml(label(choice))}</button>`).join('');
    return `<div class="extra-files-row" data-extra-path="${escapeHtml(file.video_path)}"><div class="extra-files-name"><strong>${escapeHtml(file.kind||'OVA')}</strong><span title="${escapeHtml(file.name||'')}">${escapeHtml(file.name||'')}</span></div><div class="extra-files-buttons">${buttons}</div></div>`;
  }).join('');
  const count=Object.keys(st.choices).length;
  $('modalBody').innerHTML=`<p class="extra-files-hint">${escapeHtml(PudgeI18n.t('ui.extraFilesHint'))}</p><div class="extra-files-list">${rows}</div>`
    +`<div class="extra-files-footer"><button type="button" data-action="extra-files-all" data-choice="delete">${escapeHtml(PudgeI18n.t('ui.extraFilesDeleteAll'))}</button><button type="button" data-action="extra-files-all" data-choice="keep">${escapeHtml(PudgeI18n.t('ui.extraFilesKeepAll'))}</button><span class="spacer"></span>`
    +`<button type="button" class="primary" data-action="extra-files-apply"${count?'':' disabled'}>${escapeHtml(PudgeI18n.t('ui.extraFilesApply'))}</button></div>`;
}
async function applyExtraFiles(button){
  const st=ui.extraFiles;if(!st)return;
  const chosen=st.files.filter(file=>st.choices[file.video_path]);
  if(!chosen.length)return;
  const deletes=chosen.filter(file=>st.choices[file.video_path]==='delete').length;
  if(deletes&&!await pudgeConfirm(PudgeI18n.t('confirm.deleteExtraFiles',{count:deletes}),{danger:true,confirmText:PudgeI18n.t('action.deleteExtraFile')}))return;
  if(button)button.disabled=true;
  let state=null,failed=0,deleted=0,kept=0;
  for(const file of chosen){
    const keep=st.choices[file.video_path]==='keep';
    try{
      const result=keep?await pywebview.api.extra_file_keep(file.video_path):await pywebview.api.extra_file_delete(file.video_path);
      if(!result?.ok)throw new Error(result?.error||'');
      if(result.state)state=result.state;
      if(keep)kept+=1;else deleted+=1;
      st.files=st.files.filter(item=>item.video_path!==file.video_path);delete st.choices[file.video_path];
    }catch(error){failed+=1;}
  }
  if(state){ui.state=state;renderDataPages(true);}
  toast(failed?PudgeI18n.t('ui.extraFilesFailed',{count:failed}):PudgeI18n.t('toast.extraFilesDone',{deleted,kept}));
  if(ui.extraFiles!==st)return;
  if(!st.files.length){closeModal();return;}
  renderExtraFilesModal();
}
async function handleExtraFilesAction(action,target){
  if(action==='extra-files-open'){openExtraFilesModal(target.dataset.mediaId);return;}
  const st=ui.extraFiles;if(!st)return;
  if(action==='extra-files-choice'){const path=target.dataset.path||'',choice=target.dataset.choice||'';if(st.choices[path]===choice)delete st.choices[path];else st.choices[path]=choice;renderExtraFilesModal();return;}
  if(action==='extra-files-all'){for(const file of st.files)st.choices[file.video_path]=target.dataset.choice;renderExtraFilesModal();return;}
  if(action==='extra-files-apply')await applyExtraFiles(target);
}
