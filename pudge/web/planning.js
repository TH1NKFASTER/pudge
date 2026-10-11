/* Planning page (moved out of index.html, audit 74): planned anime/manga/LN cards,
   relation trees, Planning Jiten stats (also used by LN/manga cards), AniList
   suggestions and separate-episode downloads. Loaded before index.html's inline
   script, whose start-up code can reach these functions: they stay global. */
'use strict';
// AniList's "extraLarge" cover is ~460 px wide; relation and Planning cards
// show ~90 px, so they load the "large" size (the preview opens the big one).
function listCoverUrl(url){return String(url||'').replace(/(anilistcdn\/media\/[a-z]+\/cover\/)large\//,'$1medium/');}
function relationMeta(item){return [item.format,item.season_year].filter(Boolean).map(escapeHtml).join(' • ');}
function relationNode(item,current=false){
  const url=item.site_url||`https://anilist.co/anime/${item.media_id}`;
  const meta=relationMeta(item);
  const listStatus=String(item.list_status||'').toUpperCase();
  const progress=Number(item.progress||0),episodes=Number(item.episodes||0);
  const watched=!!item.watched||['COMPLETED','REPEATING'].includes(listStatus)||!!(episodes&&progress>=episodes);
  const coverUrl=listCoverUrl(item.cover_url||item.cover||'');
  const coverBody=coverUrl
    ?`<img class="relation-node-cover" src="${escapeHtml(coverUrl)}" draggable="false" alt="">`
    :`<span class="relation-node-cover placeholder">${t('label.cover')}</span>`;
  const coverHtml=`<button type="button" class="relation-cover-action" data-action="url" data-url="${escapeHtml(url)}" aria-label="AniList">${coverBody}</button>`;
  return `<div class="relation-node ${current?'current':''} ${watched?'watched':''}" data-relation-node="1" data-media-id="${item.media_id}" data-title="${escapeHtml(item.title||'')}" data-site-url="${escapeHtml(url)}" data-list-status="${escapeHtml(listStatus)}" data-watched="${watched?'1':'0'}" data-media-status="${escapeHtml(item.media_status||'')}" data-cover-url="${escapeHtml(coverUrl)}" data-year="${escapeHtml(item.season_year??'')}" data-episodes="${escapeHtml(item.episodes??'')}" data-studio="${escapeHtml(item.studio||'')}">${coverHtml}<span class="relation-node-copy"><span class="relation-node-title">${escapeHtml(item.title)}</span>${meta?`<span class="relation-node-meta">${meta}</span>`:''}</span></div>`;
}
function relationDiagram(a,full=false){
  const relations=a.relations||{};
  const prequelSource=full?(relations.full_prequel_levels||relations.prequel_levels||[]):(relations.prequel_levels||[]);
  const sequelSource=full?(relations.full_sequel_levels||relations.sequel_levels||[]):(relations.sequel_levels||[]);
  const visibleLevel=level=>(Array.isArray(level)?level:[]).filter(item=>String(item?.format||'').toUpperCase()!=='MUSIC');
  const prequelLevels=prequelSource.map(visibleLevel).filter(level=>level.length).map(level=>full?level:level.slice(0,2));
  const sequelLevels=sequelSource.map(visibleLevel).filter(level=>level.length).map(level=>full?level:level.slice(0,2));
  if(!prequelLevels.length&&!sequelLevels.length)return '';
  const current=relations.current||{media_id:a.media_id,title:a.title,site_url:a.site_url,format:a.format,season_year:a.season_year,episodes:a.episodes,cover_url:a.cover,list_status:a.list_status,media_status:a.media_status};
  const stages=[];
  const releaseOrder=relations.order_mode==='release';
  for(let i=0;i<prequelLevels.length;i++)stages.push({label:t(releaseOrder?'relation.earlierStep':'relation.prequelStep',{step:prequelLevels.length-i}),items:prequelLevels[i],current:false});
  stages.push({label:t('relation.entry'),items:[current],current:true});
  for(let i=0;i<sequelLevels.length;i++)stages.push({label:t(releaseOrder?'relation.laterStep':'relation.sequelStep',{step:i+1}),items:sequelLevels[i],current:false});
  return `<div class="relation-diagram">${stages.map((stage,index)=>`${index?'<div class="relation-arrow"><span>→</span></div>':''}<div class="relation-stage ${stage.current?'current-stage':''}"><div class="relation-stage-label">${stage.label}</div>${stage.items.map(item=>relationNode(item,stage.current)).join('')}</div>`).join('')}</div>`;
}

function relationDateNumber(item){
  const raw=String(item?.start_date||'').trim();
  if(raw){const parts=raw.split('-').map(Number);return (parts[0]||9999)*10000+(parts[1]||7)*100+(parts[2]||1);}
  return Number(item?.season_year||9999)*10000+701;
}
function relationTypeLabel(value){
  const type=String(value||'OTHER').toUpperCase();
  if(type==='SEQUEL')return '';
  const labels={
    SIDE_STORY:'uiA.relation.sideStory',SPIN_OFF:'uiA.relation.spinOff',ALTERNATIVE:'uiA.relation.alternative',SUMMARY:'uiA.relation.summary',PARENT:'uiA.relation.parent',OTHER:'uiA.relation.other',COMPILATION:'uiA.relation.compilation',CONTAINS:'uiA.relation.contains',CHARACTER:'uiA.relation.character',ADAPTATION:'uiA.relation.adaptation',SOURCE:'uiA.relation.source'
  };
  return labels[type]?t(labels[type]):type.toLowerCase().replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
}
function fullRelationGraphDiagram(graph){
  const nodes=(graph?.nodes||[]).filter(item=>item&&item.media_id!=null&&String(item.format||'').toUpperCase()!=='MUSIC');
  const byId=new Map(nodes.map(item=>[Number(item.media_id),item]));
  const rootId=Number(graph?.root_id);
  if(!byId.has(rootId)||!nodes.length)return `<div class="empty">${t('label.noRelatedEntries')}</div>`;
  const edges=(graph?.edges||[]).map(edge=>({source:Number(edge.source),target:Number(edge.target),relation_type:String(edge.relation_type||'OTHER').toUpperCase()})).filter(edge=>byId.has(edge.source)&&byId.has(edge.target)&&edge.source!==edge.target&&!['CHARACTER','SHARED_CHARACTERS','RELATED','OTHER'].includes(edge.relation_type));
  // Compilation/recap movies often clutter the graph by forming several full-size
  // branches around the TV season they summarize. Collapse those movies into a
  // compact Alternative shelf above the adaptation instead.
  const alternativeGroups=new Map(),collapsedAlternativeIds=new Set();
  const alternativeTypes=new Set(['ALTERNATIVE','SUMMARY','COMPILATION']);
  const mediaFormat=id=>String(byId.get(id)?.format||'').toUpperCase();
  const addAlternative=(anchor,movie)=>{if(!alternativeGroups.has(anchor))alternativeGroups.set(anchor,[]);if(!alternativeGroups.get(anchor).includes(movie))alternativeGroups.get(anchor).push(movie);collapsedAlternativeIds.add(movie);};
  for(const edge of edges){
    if(!alternativeTypes.has(edge.relation_type))continue;
    const sourceMovie=mediaFormat(edge.source)==='MOVIE',targetMovie=mediaFormat(edge.target)==='MOVIE';
    if(sourceMovie===targetMovie)continue;
    const movie=sourceMovie?edge.source:edge.target,anchor=sourceMovie?edge.target:edge.source;
    if(movie===rootId||anchor===rootId&&mediaFormat(anchor)==='MOVIE')continue;
    addAlternative(anchor,movie);
  }
  const layoutEdges=edges.filter(edge=>!collapsedAlternativeIds.has(edge.source)&&!collapsedAlternativeIds.has(edge.target));
  const parentLinkedOvas=new Set();
  for(const edge of layoutEdges){
    if(edge.relation_type!=='PARENT')continue;
    if(String(byId.get(edge.source)?.format||'').toUpperCase()==='OVA')parentLinkedOvas.add(edge.source);
    if(String(byId.get(edge.target)?.format||'').toUpperCase()==='OVA')parentLinkedOvas.add(edge.target);
  }
  const sideFormats=new Set(['SPECIAL']);
  const isParentOva=id=>String(byId.get(id)?.format||'').toUpperCase()==='OVA'&&parentLinkedOvas.has(id);
  const isSideNode=id=>sideFormats.has(String(byId.get(id)?.format||'').toUpperCase())||isParentOva(id);
  let layoutRootId=rootId;
  if(isParentOva(rootId)){
    const parentEdge=layoutEdges.find(edge=>edge.relation_type==='PARENT'&&(edge.source===rootId||edge.target===rootId));
    const parentId=parentEdge?(parentEdge.source===rootId?parentEdge.target:parentEdge.source):null;
    if(parentId!=null&&byId.has(parentId))layoutRootId=parentId;
  }
  const eligible=id=>id===layoutRootId||!isSideNode(id);
  const outgoing=new Map(),incoming=new Map(),undirected=new Map();
  const add=(map,key,value)=>{if(!map.has(key))map.set(key,[]);if(!map.get(key).includes(value))map.get(key).push(value);};
  for(const edge of layoutEdges){
    add(undirected,edge.source,edge.target);add(undirected,edge.target,edge.source);
    if(edge.relation_type==='SEQUEL'&&eligible(edge.source)&&eligible(edge.target)){add(outgoing,edge.source,edge.target);add(incoming,edge.target,edge.source);}
  }
  const earlier=(a,b)=>relationDateNumber(byId.get(a))-relationDateNumber(byId.get(b))||a-b;
  const later=(a,b)=>-earlier(a,b);
  function betterPath(candidate,best,preferLater=false){
    if(candidate.length!==best.length)return candidate.length>best.length;
    const left=candidate.map(id=>relationDateNumber(byId.get(id)));
    const right=best.map(id=>relationDateNumber(byId.get(id)));
    for(let i=0;i<Math.min(left.length,right.length);i++)if(left[i]!==right[i])return preferLater?left[i]>right[i]:left[i]<right[i];
    return false;
  }
  const backwardMemo=new Map(),forwardMemo=new Map();
  function backward(id,visiting=new Set()){
    if(backwardMemo.has(id))return [...backwardMemo.get(id)];
    let best=[id];const nextVisiting=new Set(visiting);nextVisiting.add(id);
    for(const previous of [...(incoming.get(id)||[])].sort(earlier)){
      if(nextVisiting.has(previous))continue;
      const candidate=[...backward(previous,nextVisiting),id];
      if(betterPath(candidate,best,false))best=candidate;
    }
    backwardMemo.set(id,[...best]);
    return best;
  }
  function forward(id,visiting=new Set()){
    if(forwardMemo.has(id))return [...forwardMemo.get(id)];
    let best=[id];const nextVisiting=new Set(visiting);nextVisiting.add(id);
    for(const next of [...(outgoing.get(id)||[])].sort(later)){
      if(nextVisiting.has(next))continue;
      const candidate=[id,...forward(next,nextVisiting)];
      if(betterPath(candidate,best,true))best=candidate;
    }
    forwardMemo.set(id,[...best]);
    return best;
  }
  const before=backward(layoutRootId),after=forward(layoutRootId);
  const backbone=[...before.slice(0,-1),...after];
  const backboneSet=new Set(backbone);
  const flatOvas=nodes.map(item=>Number(item.media_id)).filter(id=>!collapsedAlternativeIds.has(id)&&String(byId.get(id)?.format||'').toUpperCase()==='OVA'&&!parentLinkedOvas.has(id)&&!backboneSet.has(id)).sort(earlier);
  for(const id of flatOvas){
    const insertAt=backbone.findIndex(other=>earlier(id,other)<0);
    if(insertAt<0)backbone.push(id);else backbone.splice(insertAt,0,id);
    backboneSet.add(id);
  }
  const assignment=new Map();
  const queue=[];
  for(const id of backbone){assignment.set(id,{anchor:id,depth:0,parent:null});queue.push(id);}
  while(queue.length){
    const current=queue.shift(),base=assignment.get(current);
    const neighbors=[...(undirected.get(current)||[])].sort((a,b)=>relationDateNumber(byId.get(a))-relationDateNumber(byId.get(b))||a-b);
    for(const next of neighbors){if(assignment.has(next))continue;assignment.set(next,{anchor:base.anchor,depth:base.depth+1,parent:current});queue.push(next);}
  }
  for(const id of byId.keys())if(!collapsedAlternativeIds.has(id)&&!assignment.has(id))assignment.set(id,{anchor:layoutRootId,depth:1,parent:layoutRootId});

  const NODE_W=172,NODE_H=84,X_GAP=92,Y_GAP=42,PAD=48;
  const positions=new Map();
  const chronological=[...byId.keys()].filter(id=>!collapsedAlternativeIds.has(id)).sort((a,b)=>earlier(a,b));
  const chronologicalIndex=new Map(chronological.map((id,index)=>[id,index]));
  const chronologicalX=id=>(chronologicalIndex.get(id)||0)*(NODE_W+X_GAP);
  backbone.forEach(id=>positions.set(id,{x:chronologicalX(id),y:0}));
  const branchRoot=id=>{let current=id,info=assignment.get(current);while(info?.parent!=null&&!backboneSet.has(info.parent)){current=info.parent;info=assignment.get(current);}return current;};
  const groups=new Map();
  for(const id of byId.keys()){
    if(collapsedAlternativeIds.has(id)||backboneSet.has(id))continue;
    const info=assignment.get(id),anchor=info.anchor;
    if(!groups.has(anchor))groups.set(anchor,[]);
    groups.get(anchor).push(id);
  }
  for(const [anchor,items] of groups){
    const anchorPos=positions.get(anchor)||positions.get(layoutRootId)||{x:0,y:0};
    items.sort((a,b)=>assignment.get(a).depth-assignment.get(b).depth||relationDateNumber(byId.get(a))-relationDateNumber(byId.get(b))||a-b);
    const branchSlots=new Map(),branchCounts=new Map();let above=0,below=0;
    for(const id of items){
      const root=branchRoot(id),preferBelow=isSideNode(root);
      if(!branchSlots.has(root)){
        if(preferBelow)branchSlots.set(root,{sign:1,slot:++below});
        else branchSlots.set(root,{sign:-1,slot:++above});
      }
      const slot=branchSlots.get(root),member=(branchCounts.get(root)||0);branchCounts.set(root,member+1);
      const depth=Math.max(1,assignment.get(id).depth);
      const outward=slot.slot*(NODE_H+Y_GAP)+member*(NODE_H*.72)+(depth-1)*34;
      positions.set(id,{x:chronologicalX(id),y:anchorPos.y+slot.sign*outward});
    }
  }
  const hasAlternativeShelf=[...alternativeGroups.keys()].some(id=>positions.has(id));
  if(hasAlternativeShelf)for(const pos of positions.values())pos.y+=58;
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  for(const pos of positions.values()){minX=Math.min(minX,pos.x);minY=Math.min(minY,pos.y);maxX=Math.max(maxX,pos.x+NODE_W);maxY=Math.max(maxY,pos.y+NODE_H);}
  if(hasAlternativeShelf)minY=Math.min(minY,0);
  const shiftX=PAD-minX,shiftY=PAD-minY;
  for(const pos of positions.values()){pos.x+=shiftX;pos.y+=shiftY;}
  const width=Math.max(900,Math.ceil(maxX-minX+PAD*2)),height=Math.max(500,Math.ceil(maxY-minY+PAD*2));
  const backboneIndex=new Map(backbone.map((id,index)=>[id,index]));
  const edgePaths=[],edgeLabels=[];
  for(const edge of edges){
    if(collapsedAlternativeIds.has(edge.source)||collapsedAlternativeIds.has(edge.target))continue;
    const source=positions.get(edge.source),target=positions.get(edge.target);if(!source||!target)continue;
    const sourceCenter={x:source.x+NODE_W/2,y:source.y+NODE_H/2};
    const targetCenter={x:target.x+NODE_W/2,y:target.y+NODE_H/2};
    const dx=targetCenter.x-sourceCenter.x,dy=targetCenter.y-sourceCenter.y;
    const boundaryScale=(x,y)=>{const ratios=[];if(Math.abs(x)>0.001)ratios.push((NODE_W/2)/Math.abs(x));if(Math.abs(y)>0.001)ratios.push((NODE_H/2)/Math.abs(y));return ratios.length?Math.min(...ratios):0;};
    const sourceScale=boundaryScale(dx,dy),targetScale=boundaryScale(dx,dy);
    const sx=sourceCenter.x+dx*sourceScale,sy=sourceCenter.y+dy*sourceScale;
    const tx=targetCenter.x-dx*targetScale,ty=targetCenter.y-dy*targetScale;
    const isMain=edge.relation_type==='SEQUEL'&&backboneIndex.has(edge.source)&&backboneIndex.get(edge.target)===backboneIndex.get(edge.source)+1;
    const d=`M ${sx} ${sy} L ${tx} ${ty}`;
    const cls=isMain?'main':edge.relation_type==='SEQUEL'?'':'side';
    edgePaths.push(`<path class="full-relation-edge ${cls}" d="${d}" ${edge.relation_type==='SEQUEL'?'marker-end="url(#fullRelationArrow)"':''}></path>`);
    const label=relationTypeLabel(edge.relation_type);
    if(label&&!isMain){
      const lx=Math.round((sx+tx)/2),ly=Math.round((sy+ty)/2-7),labelWidth=Math.max(44,Math.round(label.length*6.7+16));
      edgeLabels.push(`<g class="full-relation-edge-label-group"><rect class="full-relation-edge-label-bg" x="${lx-labelWidth/2}" y="${ly-10}" width="${labelWidth}" height="20"></rect><text class="full-relation-edge-label" x="${lx}" y="${ly}">${escapeHtml(label)}</text></g>`);
    }
  }
  const nodeHtml=nodes.map(item=>{const id=Number(item.media_id),pos=positions.get(id);if(!pos||collapsedAlternativeIds.has(id))return '';return `<div class="full-relation-node ${backboneSet.has(id)?'backbone':'branch'}" style="left:${Math.round(pos.x)}px;top:${Math.round(pos.y)}px">${relationNode(item,id===rootId)}</div>`;}).join('');
  const alternativeHtml=[...alternativeGroups.entries()].map(([anchor,ids])=>{const pos=positions.get(anchor);if(!pos)return '';const movies=ids.map(id=>byId.get(id)).filter(Boolean).sort((a,b)=>relationDateNumber(a)-relationDateNumber(b)||Number(a.media_id)-Number(b.media_id));const covers=movies.map(item=>{const cover=item.cover_url||item.cover||'',url=item.site_url||`https://anilist.co/anime/${item.media_id}`,title=escapeHtml(item.title||'');const body=cover?`<img src="${escapeHtml(cover)}" draggable="false" alt="">`:`<span class="full-relation-alt-placeholder"></span>`;return `<button type="button" class="full-relation-alt" data-action="url" data-url="${escapeHtml(url)}" data-relation-node="1" data-relation-alternative="1" data-media-id="${item.media_id}" data-title="${title}" data-cover-url="${escapeHtml(cover)}" data-year="${escapeHtml(item.season_year??'')}" data-episodes="${escapeHtml(item.episodes??'')}" data-studio="${escapeHtml(item.studio||'')}" title="${title}">${body}</button>`;}).join('');return `<div class="full-relation-alternatives" style="left:${Math.round(pos.x)}px;top:${Math.round(pos.y-52)}px"><span class="full-relation-alternatives-label">${escapeHtml(relationTypeLabel('ALTERNATIVE'))}</span><div class="full-relation-alternatives-list">${covers}</div></div>`;}).join('');
  const notes=[];if(graph.partial)notes.push(t('label.partialRelatedGraph'));if(graph.truncated)notes.push(t('uiA.relation.truncated'));
  return `${notes.length?`<div class="full-relation-note">${notes.join(' ')}</div>`:''}<div class="full-relation-viewport" data-graph-width="${width}" data-graph-height="${height}"><div class="full-relation-canvas" style="width:${width}px;height:${height}px"><svg class="full-relation-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true"><defs><marker id="fullRelationArrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#7898c7"></path></marker></defs>${edgePaths.join('')}${edgeLabels.join('')}</svg>${nodeHtml}${alternativeHtml}</div></div>`;
}

function planningRelationDiagram(a){return relationDiagram(a,false);}
function hydrateCompactRelationsFromGraph(a,graph){
  if(!a||!graph)return false;
  const nodes=(graph.nodes||[]).filter(item=>item&&item.media_id!=null);
  const byId=new Map(nodes.map(item=>[Number(item.media_id),item]));
  const rootId=Number(a.media_id);if(!byId.has(rootId))return false;
  const edges=(graph.edges||[]).map(edge=>({source:Number(edge.source),target:Number(edge.target),relation_type:String(edge.relation_type||'OTHER').toUpperCase()})).filter(edge=>!['CHARACTER','SHARED_CHARACTERS','RELATED','OTHER'].includes(edge.relation_type));
  const directPre=[],directSeq=[];
  for(const edge of edges){
    if(edge.relation_type!=='SEQUEL')continue;
    if(edge.target===rootId&&byId.has(edge.source))directPre.push(byId.get(edge.source));
    if(edge.source===rootId&&byId.has(edge.target))directSeq.push(byId.get(edge.target));
  }
  const unique=items=>[...new Map(items.map(item=>[Number(item.media_id),item])).values()].sort((x,y)=>relationDateNumber(x)-relationDateNumber(y)||Number(x.media_id)-Number(y.media_id));
  const pre=unique(directPre),seq=unique(directSeq);
  const secondPre=unique(pre.flatMap(item=>edges.filter(edge=>edge.relation_type==='SEQUEL'&&edge.target===Number(item.media_id)&&byId.has(edge.source)).map(edge=>byId.get(edge.source))));
  const secondSeq=unique(seq.flatMap(item=>edges.filter(edge=>edge.relation_type==='SEQUEL'&&edge.source===Number(item.media_id)&&byId.has(edge.target)).map(edge=>byId.get(edge.target))));
  if(!pre.length&&!seq.length&&!secondPre.length&&!secondSeq.length)return false;
  const current=byId.get(rootId)||{media_id:a.media_id,title:a.title,site_url:a.site_url,format:a.format,season_year:a.season_year,episodes:a.episodes,cover_url:a.cover,list_status:a.list_status,media_status:a.media_status};
  a.relations={
    current,
    prequel_levels:[secondPre.slice(-2),pre.slice(-2)].filter(level=>level.length),
    sequel_levels:[seq.slice(0,2),secondSeq.slice(0,2)].filter(level=>level.length),
    full_prequel_levels:[secondPre,pre].filter(level=>level.length),
    full_sequel_levels:[seq,secondSeq].filter(level=>level.length),
    order_mode:'anilist'
  };
  return true;
}
function hydrateAllCompactRelationsFromGraph(graph){
  let changed=false;
  for(const collection of [ui.state?.planned||[],ui.state?.current||[]]){
    for(const anime of collection)changed=hydrateCompactRelationsFromGraph(anime,graph)||changed;
  }
  return changed;
}
function plannedItems(){
  const anime=(ui.state?.planned||[]).map(x=>({...x,media_kind:'anime'}));
  const literature=(ui.lnState?.planning||[]).map(x=>({...x,media_kind:String(x.format||'').toUpperCase()==='NOVEL'?'novel':'manga'}));
  return [...anime,...literature];
}
function plannedKey(a){return `${a.media_kind||'anime'}:${a.media_id}`;}
let plannedJitenObserver=null,plannedJitenRunning=false;
const plannedJitenCache=new Map(),plannedJitenQueue=[];
function planningUsesJiten(){return ['difficulty','characters','comprehension','uniqueWords'].includes($('plannedSort')?.value)||[$('plannedDifficultyFilter')?.value,$('plannedLengthFilter')?.value,$('plannedKnownFilter')?.value].some(value=>value&&value!=='all');}
function jitenDifficultyClass(value){
  const numeric=Number(value);
  if(!Number.isFinite(numeric))return '';
  const index=Math.min(5,Math.max(0,Math.floor(numeric)));
  return `difficulty-${index}`;
}
function updatePlanningJitenLoading(){const node=$('plannedJitenLoading');if(!node)return;const remaining=plannedItems().filter(item=>String(item.media_status||'').trim().toUpperCase()==='FINISHED'&&!plannedJitenCache.has(plannedKey(item))).length;node.hidden=!planningUsesJiten()||remaining===0;node.textContent=PudgeI18n.t('ui.loadingJiten',{remaining:remaining});}
function planningJitenShell(a){
  return String(a.media_status||'').trim().toUpperCase()==='FINISHED'
    ?'<div class="planned-jiten" data-planned-jiten><span class="planned-jiten-loading">Jiten…</span></div>':'';
}
function jitenDuration(milliseconds){
  const minutes=Math.max(1,Math.round(Number(milliseconds||0)/60000)),hours=Math.floor(minutes/60),rest=minutes%60;
  const h=t('remaining.hour'),m=t('remaining.minute');
  return hours?(rest?`${hours}${h} ${rest}${m}`:`${hours}${h}`):`${minutes}${m}`;
}
function renderPlanningJiten(region,stats){
  if(!region?.isConnected)return;
  if(!stats?.available){region.remove();return;}
  const requestedVolume=Number(region.dataset.jitenVolume||0);
  const volumeStats=requestedVolume&&stats?.volumes?.[String(requestedVolume)]?stats.volumes[String(requestedVolume)]:null;
  const shown=volumeStats||stats;
  const names=[0,1,2,3,4,5].map(level=>t(`uiA.jiten.level${level}`));
  const difficulty=Number(shown.difficulty);
  const difficultyText=difficulty>=0&&difficulty<names.length?t('uiA.jiten.difficulty',{level:names[difficulty]}):'';
  // "1.2k words": a compacted number takes the noun form of a large count.
  const wordCount=Number(shown.word_count||0);
  const length=Number(shown.speech_duration_ms||0)>0
    ?t('uiA.jiten.speech',{duration:jitenDuration(shown.speech_duration_ms)})
    :(wordCount>0?tn('uiA.jiten.length',wordCount>=1000?1000:wordCount,{value:compactJitenNumber(wordCount)}):'');
  const known=shown.coverage_available?t('uiA.jiten.known',{percent:Number(shown.known_coverage||0).toFixed(1)}):'';
  const locale=PudgeI18n.t('ui.en');
  // pudge-v0.7.23-hide-series-length-tooltip-v4
  const wholeSeries=region.hasAttribute('data-ln-series-jiten')||region.hasAttribute('data-manga-series-jiten');
  const detail=t('uiA.jiten.detail',{words:Number(shown.word_count||0).toLocaleString(locale),unique:Number(shown.unique_word_count||0).toLocaleString(locale)});
  const displayedLength=wholeSeries?'':length;
  const chips=[
    {text:difficultyText,className:jitenDifficultyClass(difficulty)},
    {text:displayedLength,className:''},
    {text:known,className:'known'},
  ].filter(item=>item.text).map(item=>'<span class="planned-jiten-chip '+item.className+'">'+escapeHtml(item.text)+'</span>').join('');
  if(wholeSeries){
    region.innerHTML='<button data-action="url" data-url="'+escapeHtml(shown.url||'')+'">'+chips+'</button>';
  }else{
    region.innerHTML=(region.hasAttribute('data-ln-card-jiten')||region.hasAttribute('data-library-card-jiten'))?'<div data-tooltip="'+escapeHtml(detail)+'">'+chips+'</div>':'<button data-action="url" data-url="'+escapeHtml(shown.url||'')+'" data-tooltip="'+escapeHtml(detail)+'">'+chips+'</button>';
  }
  // Async Jiten hydration must not repeatedly resize/refocus series scrollers.
  // Initial library rendering focuses the current volume once.
}
function queuePlanningJiten(a,region){
  const key=plannedKey(a),cached=plannedJitenCache.get(key);
  if(cached!==undefined){renderPlanningJiten(region,cached);return;}
  const pending=plannedJitenQueue.find(item=>item.key===key);
  if(pending){pending.regions.push(region);return;}
  plannedJitenQueue.push({key,a,regions:[region]});void runPlanningJitenQueue();
}
window.PudgeLiteratureJiten={hydrate:(region,item)=>{if(region&&item)queuePlanningJiten(item,region);}};

async function runPlanningJitenQueue(){
  if(plannedJitenRunning)return;plannedJitenRunning=true;
  try{
    while(plannedJitenQueue.length){
      const item=plannedJitenQueue.shift(),a=item.a;
      try{
        const titles=[a.title,...(a.titles||[]),...(a.synonyms||[])].filter(Boolean);
        const stats=await pywebview.api.planning_jiten_stats(Number(a.media_id),a.media_kind||'anime',a.format||'',titles);
        plannedJitenCache.set(item.key,stats);
        for(const region of item.regions)renderPlanningJiten(region,stats);
        if(planningUsesJiten())applyPlanned();
      }catch(error){plannedJitenCache.set(item.key,{available:false});console.error('Jiten Planning metadata',error);for(const region of item.regions)region?.remove();}
    }
  }finally{plannedJitenRunning=false;updatePlanningJitenLoading();}
}
function observePlanningJiten(a,node){
  const region=node?.querySelector?.('[data-planned-jiten]');if(!region)return;
  if(!('IntersectionObserver' in window)){queuePlanningJiten(a,region);return;}
  if(!plannedJitenObserver)plannedJitenObserver=new IntersectionObserver(entries=>{
    for(const entry of entries){
      if(!entry.isIntersecting)continue;
      plannedJitenObserver.unobserve(entry.target);
      queuePlanningJiten(entry.target._plannedItem,entry.target);
    }
  },{rootMargin:'220px'});
  region._plannedItem=a;plannedJitenObserver.observe(region);
}
function queueAllPlanningJiten(){
  if(!planningUsesJiten()){updatePlanningJitenLoading();return;}
  for(const item of plannedItems()){
    const region=ui.plannedNodes.get(plannedKey(item))?.querySelector?.('[data-planned-jiten]');
    if(region){plannedJitenObserver?.unobserve(region);queuePlanningJiten(item,region);}
  }
  updatePlanningJitenLoading();
}
function literaturePlannedCard(a){
  const isNovel=a.media_kind==='novel';const type=isNovel?t('uiA.lightNovel'):t('planned.typeManga');
  const progress=isNovel?tn('uiA.planned.volumeProgress',a.volumes??'?',{progress:Number(a.progress_volumes||0)}):tn('uiA.planned.chapterProgress',a.chapters??'?',{progress:Number(a.progress||0)});
  const coverHtml=a.cover?`<div class="list-cover cover-shell"><img src="${escapeHtml(a.cover)}" draggable="false" alt=""></div>`:`<div class="list-cover cover-shell"><div class="cover-placeholder">${t('label.cover')}</div></div>`;
  return `<article class="anime-card planned-literature" data-media-id="${a.media_id}" data-literature="${a.media_kind}" data-action="url" data-url="${escapeHtml(a.site_url||'')}" data-title="${escapeHtml(a.title||'')}"><div class="stripe"></div>${coverHtml}<div class="anime-copy"><div class="anime-title" data-full-title="${escapeHtml(a.title||'')}" title="${escapeHtml(a.title||'')}">${escapeHtml(a.title||'')}</div><div class="meta"><strong>${type}</strong> • ${escapeHtml(progress)}${a.media_status?` • ${escapeHtml(mediaStatusLabel(a.media_status))}`:''}</div>${planningJitenShell(a)}</div></article>`;
}
// Planning is rebuilt only while it is the visible page; hidden, it is marked
// dirty and rebuilt when opened (its cards held MBs of detached DOM).
function buildPlannedOnce(){if(ui.page!=='planned'){ui.plannedDirty=true;ui.plannedVisibleCount=plannedItems().length;return;}ui.plannedDirty=false;buildPlannedNow();}
function buildPlannedNow(){const container=$('plannedContent');if(plannedJitenObserver){plannedJitenObserver.disconnect();plannedJitenObserver=null;}container.innerHTML='';ui.plannedNodes.clear();for(const a of plannedItems()){const wrap=document.createElement('div');wrap.innerHTML=a.media_kind==='anime'?animeCard(a,true):literaturePlannedCard(a);const node=wrap.firstElementChild;ui.plannedNodes.set(plannedKey(a),node);container.appendChild(node);observePlanningJiten(a,node);}applyPlanned();}
function applyPlanned(){
  const filter=$('plannedFilter').value,sort=$('plannedSort').value,typeFilter=$('plannedTypeFilter')?.value||'all';
  const difficultyFilter=$('plannedDifficultyFilter')?.value||'all',lengthFilter=$('plannedLengthFilter')?.value||'all',knownFilter=$('plannedKnownFilter')?.value||'all';
  const query=String($('plannedSearch')?.value||'').trim().toLocaleLowerCase(ui.lang);
  queueAllPlanningJiten();
  let items=plannedItems();
  const isFinished=a=>String(a.media_status||'').trim().toUpperCase()==='FINISHED';
  if(typeFilter!=='all')items=items.filter(a=>String(a.media_kind||'anime')===typeFilter);
  if(filter==='finished')items=items.filter(isFinished);
  if(filter==='unfinished')items=items.filter(a=>!isFinished(a));
  if(difficultyFilter!=='all')items=items.filter(a=>{const stats=plannedJitenCache.get(plannedKey(a));return stats?.available&&Number(stats.difficulty)===Number(difficultyFilter);});
  if(lengthFilter!=='all')items=items.filter(a=>{const stats=plannedJitenCache.get(plannedKey(a)),count=Number(stats?.character_count||0);if(!stats?.available||!count)return false;if(lengthFilter==='short')return count<50000;if(lengthFilter==='medium')return count>=50000&&count<150000;if(lengthFilter==='long')return count>=150000&&count<300000;return count>=300000;});
  if(knownFilter!=='all')items=items.filter(a=>{const stats=plannedJitenCache.get(plannedKey(a));return stats?.coverage_available&&Number(stats.known_coverage)>=Number(knownFilter);});
  if(query){
    const numeric=/^\d+$/.test(query);const exact=numeric?items.find(a=>String(a.media_id)===query):null;
    if(exact)items=[exact];
    else items=items.filter(a=>{if(numeric&&String(a.media_id).includes(query))return true;const names=[a.title,...(a.titles||[]),...(a.synonyms||[])].filter(Boolean).join('\n').toLocaleLowerCase(ui.lang);return names.includes(query);});
  }
  if(sort==='episodes')items.sort((a,b)=>((b.episodes??b.volumes??b.chapters??-1)-(a.episodes??a.volumes??a.chapters??-1))||String(a.title).localeCompare(String(b.title),ui.lang));
  else if(sort==='rating')items.sort((a,b)=>(b.mean_score??-1)-(a.mean_score??-1)||String(a.title).localeCompare(String(b.title),ui.lang));
  else if(sort==='difficulty')items.sort((a,b)=>{const x=plannedJitenCache.get(plannedKey(a)),y=plannedJitenCache.get(plannedKey(b)),xv=x?.available?Number(x.difficulty_raw??x.difficulty):Infinity,yv=y?.available?Number(y.difficulty_raw??y.difficulty):Infinity;return xv-yv||String(a.title).localeCompare(String(b.title),ui.lang);});
  else if(sort==='characters')items.sort((a,b)=>{const x=plannedJitenCache.get(plannedKey(a)),y=plannedJitenCache.get(plannedKey(b)),xv=x?.available?Number(x.character_count||0):Infinity,yv=y?.available?Number(y.character_count||0):Infinity;return xv-yv||String(a.title).localeCompare(String(b.title),ui.lang);});
  else if(sort==='comprehension')items.sort((a,b)=>{const x=plannedJitenCache.get(plannedKey(a)),y=plannedJitenCache.get(plannedKey(b)),xv=x?.coverage_available?Number(x.known_coverage):-1,yv=y?.coverage_available?Number(y.known_coverage):-1;return yv-xv||String(a.title).localeCompare(String(b.title),ui.lang);});
  else if(sort==='uniqueWords')items.sort((a,b)=>{const x=plannedJitenCache.get(plannedKey(a)),y=plannedJitenCache.get(plannedKey(b)),xv=x?.available?Number(x.unique_word_count||0):Infinity,yv=y?.available?Number(y.unique_word_count||0):Infinity;return xv-yv||String(a.title).localeCompare(String(b.title),ui.lang);});
  else items.sort((a,b)=>String(a.title).localeCompare(String(b.title),ui.lang));
  ui.plannedVisibleCount=items.length;
  const fragment=document.createDocumentFragment(),visible=new Set(items.map(plannedKey));
  for(const [id,node] of ui.plannedNodes)node.hidden=!visible.has(id);
  for(const a of items){const node=ui.plannedNodes.get(plannedKey(a));if(node)fragment.appendChild(node);}
  $('plannedContent').appendChild(fragment);
  // Empty states: nothing planned at all, or nothing left after the filters.
  {const container=$('plannedContent');let empty=container.querySelector(':scope > .planned-empty');
   const total=ui.plannedNodes.size;
   const message=!total?(PudgeI18n.t('ui.planningIsEmptyAddTitles'))
     :!items.length?(PudgeI18n.t('ui.nothingMatchesTheseFilters')):'';
   if(message){if(!empty){empty=document.createElement('div');empty.className='empty planned-empty';container.appendChild(empty);}empty.textContent=message;}
   else empty?.remove();}
  updateCount();applyOverflowTitleTooltips($('plannedContent'));
  if(query&&items.length===1&&String(items[0].media_id)===query)requestAnimationFrame(()=>ui.plannedNodes.get(plannedKey(items[0]))?.scrollIntoView({block:'center'}));
}
let plannedSuggestionTimer=null,plannedSuggestionToken=0;
function plannedSuggestionsRoot(){const content=$('plannedContent');let root=$('plannedSuggestions');if(!root){root=document.createElement('section');root.id='plannedSuggestions';root.className='planned-suggestions';}if(content&&root.previousElementSibling!==content)content.after(root);return root;}
function plannedSuggestionFacts(row){
  const facts=[];
  if(row.year)facts.push(String(row.year));
  if(row.episodes)facts.push(tn('uiA.planned.factEpisodes',Number(row.episodes)));
  if(row.chapters)facts.push(tn('uiA.planned.factChapters',Number(row.chapters)));
  if(row.volumes)facts.push(tn('uiA.planned.factVolumes',Number(row.volumes)));
  if(row.studio)facts.push(String(row.studio));
  return facts.join(' · ');
}
function renderPlannedSuggestions(rows,query){
  const root=plannedSuggestionsRoot(),existing=new Set(plannedItems().map(plannedKey)),type=$('plannedTypeFilter')?.value||'all';
  const visible=(rows||[]).filter(row=>!existing.has(plannedKey(row))&&(type==='all'||row.media_kind===type));
  root.innerHTML=visible.length?`<h2>${PudgeI18n.t('ui.anilistSuggestions')} <span>${escapeHtml(query)}</span></h2><div class="planned-suggestion-grid">${visible.map(row=>{const facts=plannedSuggestionFacts(row),score=anilistScoreChip(row.mean_score,'percent'),genres=(row.genres||[]).join(' · '),description=String(row.description||'').trim();return `<article data-action="url" data-url="${escapeHtml(row.site_url||'')}" tabindex="0" title="${PudgeI18n.t('ui.openOnAnilist')}"><img src="${escapeHtml(row.cover||'')}" alt="" loading="lazy"><div class="planned-suggestion-main"><strong>${escapeHtml(row.title||'')}</strong>${row.native_title&&row.native_title!==row.title?`<span>${escapeHtml(row.native_title)}</span>`:''}<span>${escapeHtml(row.format||row.media_kind||'')}${score?` · ${score}`:''}${facts?` · ${escapeHtml(facts)}`:''}</span></div><button data-planning-suggestion="${Number(row.media_id)}" data-media-kind="${escapeHtml(row.media_kind||'anime')}">${PudgeI18n.t('ui.add')}</button><div class="planned-suggestion-info"><strong>${escapeHtml(row.title||'')}</strong>${score}${facts?`<span>${escapeHtml(facts)}</span>`:''}${row.media_status?`<span>${escapeHtml(mediaStatusLabel(row.media_status))}</span>`:''}${genres?`<span>${escapeHtml(genres)}</span>`:''}${description?`<p>${escapeHtml(description)}</p>`:''}</div></article>`;}).join('')}</div>`:'';
}
function schedulePlannedSuggestions(){
  if(plannedSuggestionTimer)clearTimeout(plannedSuggestionTimer);const query=String($('plannedSearch')?.value||'').trim(),token=++plannedSuggestionToken;
  if(query.length<3){renderPlannedSuggestions([],query);return;}
  plannedSuggestionTimer=setTimeout(async()=>{try{const rows=await pywebview.api.planning_search_anilist(query);if(token===plannedSuggestionToken)renderPlannedSuggestions(rows,query);}catch(e){console.error(e);}},350);
}
function planningReleasedEpisodeCountForAnime(anime={}){const finished=String(anime.media_status||'').toUpperCase()==='FINISHED',total=Math.max(0,Number(anime.episodes||0)),released=Math.max(0,Number(anime.released_episodes||0)),beforeNext=Math.max(0,Number(anime.next_airing_episode||1)-1);return Math.floor(finished?(total||released):Math.max(released,beforeNext,Number(anime.progress||0)));}
function planningReleasedEpisodeCount(mediaId){return planningReleasedEpisodeCountForAnime(animeFromId(mediaId)||{});}
function planningEpisodeFallback(context){if(!context.batch||context.episodeCount<1)return '';return `<div class="planning-episode-fallback"><strong>${PudgeI18n.t('ui.noSuitableSeriesPack')}</strong><span class="subtle">${PudgeI18n.t('ui.chooseAReleaseForEach')}</span><div class="setting-inline-actions"><button data-action="planning-episodes-manual">${PudgeI18n.t('ui.chooseSeparateEpisodes')}</button><button class="primary" data-action="planning-episodes-auto">${PudgeI18n.t('ui.downloadReleasedAutomatically')}</button></div></div>`;}
function planningEpisodeContextFromRelease(){const release=ui.releaseContext;if(!release||release.episodeCount<1)return null;if(ui.planningEpisodeContext?.mediaId===release.mediaId&&ui.planningEpisodeContext?.total===release.episodeCount)return ui.planningEpisodeContext;ui.planningEpisodeContext={mediaId:release.mediaId,total:release.episodeCount,title:animeFromId(release.mediaId)?.title||'',states:new Map(),running:false,current:0,added:0};return ui.planningEpisodeContext;}
function planningEpisodeStatusLabel(state){return state==='added'?(PudgeI18n.t('ui.added')):state==='local'?(PudgeI18n.t('ui.alreadyLocal')):state==='missing'?(PudgeI18n.t('ui.notFound')):state==='error'?(PudgeI18n.t('ui.error')):'';}
function renderPlanningEpisodeChooser(context){if(!context)return;ui.planningEpisodeContext=context;$('modalTitle').textContent=`${PudgeI18n.t('ui.separateEpisodes')}${context.title?` — ${context.title}`:''}`;const episodes=Array.from({length:context.total},(_,index)=>index+1),rows=episodes.map(episode=>{const state=context.states.get(episode)||'',status=planningEpisodeStatusLabel(state);return `<button class="${escapeHtml(state)}" data-action="planning-episode-release" data-episode="${episode}" ${state==='local'?'disabled':''}><span>${PudgeI18n.t('table.episode')} ${episode}</span><small>${status}</small></button>`;}).join('');$('modalBody').innerHTML=`<div class="planning-episode-toolbar"><button class="primary" data-action="planning-episodes-auto">${PudgeI18n.t('ui.downloadReleasedAutomatically')}</button></div><div class="planning-episode-grid">${rows}</div>`;}
async function preparePlanningEpisodeChooser(context){if(!context)return;$('modalTitle').textContent=PudgeI18n.t('ui.checkingLocalEpisodes');$('modalBody').innerHTML='<div class="empty"><span class="spinner"></span></div>';const preview=await pywebview.api.planning_episode_download_preview(context.mediaId);context.total=Number(preview.total||context.total);context.title=preview.title||context.title;for(const episode of preview.local_episodes||[])context.states.set(Number(episode),'local');renderPlanningEpisodeChooser(context);}
function planningEpisodeJobSummary(job){const rows=job.episodes||[],counts={added:0,local:0,missing:0,error:0};for(const row of rows)counts[row.status]=(counts[row.status]||0)+1;return PudgeI18n.t('ui.addedAlreadyLocalNotFound',{added:counts.added,local:counts.local,missing:counts.missing,error:counts.error});}
function renderPlanningEpisodeJob(job,finished=false){ui.planningEpisodeJobVisible=true;$('modalBackdrop').hidden=false;$('modalBackdrop').classList.remove('watch-order-open');$('modalBackdrop').classList.add('open');$('modalTitle').textContent=`${finished?(PudgeI18n.t('ui.searchComplete')):(PudgeI18n.t('ui.searchingReleasedEpisodes'))}${job.title?` — ${job.title}`:''}`;if(!finished){$('modalBody').innerHTML=`<div class="planning-episode-progress"><strong>${PudgeI18n.t('ui.checkingLocalFilesAndSearching')} ${Number(job.current||0)} / ${Number(job.total||0)}</strong><progress max="${Math.max(1,Number(job.total||1))}" value="${Number(job.current||0)}"></progress><span class="subtle">${PudgeI18n.t('ui.youCanCloseThisWindow')}</span></div>`;return;}const rows=(job.episodes||[]).map(row=>`<div class="planning-episode-result ${escapeHtml(row.status||'')}"><span>${row.episode==null?(PudgeI18n.t('ui.generalError')):`${PudgeI18n.t('table.episode')} ${Number(row.episode)}`}</span><small>${planningEpisodeStatusLabel(row.status)}</small>${row.error?`<em>${escapeHtml((window.PudgeUiLanguage?.message(row.error) ?? String(row.error)))}</em>`:row.release?`<em>${escapeHtml(row.release)}</em>`:''}</div>`).join('');$('modalBody').innerHTML=`<p>${escapeHtml(planningEpisodeJobSummary(job))}</p><div class="planning-episode-results">${rows}</div>`;}
async function pollPlanningEpisodeDownload(){ui.planningEpisodeJobPollTimer=null;let job;try{job=await pywebview.api.planning_episode_download_status();}catch(error){console.error(error);ui.planningEpisodeJobPollTimer=setTimeout(pollPlanningEpisodeDownload,2500);return;}ui.planningEpisodeJob=job;if(job.running){if(ui.planningEpisodeJobVisible)renderPlanningEpisodeJob(job,false);ui.planningEpisodeJobPollTimer=setTimeout(pollPlanningEpisodeDownload,900);return;}const finishedAt=Number(job.finished_at||0);if(finishedAt&&finishedAt!==ui.planningEpisodeJobFinishedAt){ui.planningEpisodeJobFinishedAt=finishedAt;ui.state=await pywebview.api.get_state();renderDataPages();renderPlanningEpisodeJob(job,true);if((job.episodes||[]).some(row=>row.status==='added')&&!ui.foregroundPollTimer)ui.foregroundPollTimer=setTimeout(pollForegroundWork,1000);}}
async function startPlanningEpisodeDownload(context){if(!context)return;const job=await pywebview.api.start_planning_episode_download(Number(context.mediaId));ui.planningEpisodeJob=job;renderPlanningEpisodeJob(job,false);if(!ui.planningEpisodeJobPollTimer)ui.planningEpisodeJobPollTimer=setTimeout(pollPlanningEpisodeDownload,500);}
async function resumePlanningEpisodeDownload(){try{const job=await pywebview.api.planning_episode_download_status();ui.planningEpisodeJob=job;if(job.running&&!ui.planningEpisodeJobPollTimer)ui.planningEpisodeJobPollTimer=setTimeout(pollPlanningEpisodeDownload,500);}catch(error){console.error(error);}}
