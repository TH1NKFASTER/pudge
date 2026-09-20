'use strict';

(() => {
  const COVER_SELECTOR = [
    '.cover-shell img',
    'img.ln-card-cover',
    '.ln-inline-image img',
    '.audiobook-cover img',
    '.manga-v2-cover-shell img',
    '.library-cover-shell img',
    '.planned-suggestion-grid img',
    '.list-cover img',
    '.airing-cover-shell img'
  ].join(',');
  const MOUSE_OPEN_DISTANCE = 148;
  const PINCH_OPEN_SCALE = 1.36;
  const MIN_PREVIEW_ZOOM = .5;
  const MAX_PREVIEW_ZOOM = 6;
  let drag = null;
  let pinch = null;
  let previewPinch = null;
  let previewPan = null;
  let previewZoom = 1;
  let previewPanX = 0;
  let previewPanY = 0;
  let suppressClickUntil = 0;
  let previewRestoreFocus = null;
  let previewOpenFrame = 0;
  let previewRequestGeneration = 0;
  let previewRetry = null;
  const previewDecodeInflight = new Map();
  const previewResolveInflight = new Map();
  const PREVIEW_DECODE_TIMEOUT_MS = 8000;

  const coverImage = target => target?.closest?.(COVER_SELECTOR) || null;
  // Anime cards already use primary click to play/open the episode. Physical
  // mouse movement during a click must never turn that gesture into a cover
  // preview. Trackpad pinch remains available because it uses gesture events.
  const allowMouseDragPreview = image => !image?.closest?.('[data-continue-card="1"],.airing-card[data-action="play"]');
  const rectFor = image => {
    const rect=image.getBoundingClientRect();
    return {left:rect.left,top:rect.top,width:rect.width,height:rect.height};
  };
  const imageSource = image => String(image?.currentSrc || image?.src || '').trim();

  function makePeek(image){
    const rect=rectFor(image);if(!rect.width||!rect.height)return null;
    const peek=document.createElement('div');peek.className='pudge-cover-peek';
    Object.assign(peek.style,{left:`${rect.left}px`,top:`${rect.top}px`,width:`${rect.width}px`,height:`${rect.height}px`});
    const clone=document.createElement('img');clone.src=imageSource(image);clone.alt='';peek.appendChild(clone);document.body.appendChild(peek);return peek;
  }
  function discardPeek(peek,{snap=true}={}){
    if(!peek)return;
    if(!snap){peek.remove();return;}
    peek.classList.add('snap-back');setTimeout(()=>peek.remove(),180);
  }
  function previewOverlay(){return document.querySelector('.pudge-cover-preview');}
  function previewImage(){return document.querySelector('.pudge-cover-preview img');}
  function previewAnchor(clientX,clientY){
    return {
      x:Number.isFinite(Number(clientX))?Number(clientX)-window.innerWidth/2:0,
      y:Number.isFinite(Number(clientY))?Number(clientY)-window.innerHeight/2:0,
    };
  }
  function clampPreviewPan(){
    const image=previewImage();
    if(!image||previewZoom<=1){previewPanX=0;previewPanY=0;return;}
    const width=Math.max(1,Number(image.offsetWidth||0))*previewZoom;
    const height=Math.max(1,Number(image.offsetHeight||0))*previewZoom;
    const viewportWidth=Math.max(1,window.innerWidth-24);
    const viewportHeight=Math.max(1,window.innerHeight-24);
    const maxX=Math.max(0,(width-viewportWidth)/2+18);
    const maxY=Math.max(0,(height-viewportHeight)/2+18);
    previewPanX=Math.max(-maxX,Math.min(maxX,previewPanX));
    previewPanY=Math.max(-maxY,Math.min(maxY,previewPanY));
  }
  function applyPreviewTransform({animate=false}={}){
    const image=previewImage();if(!image)return;
    clampPreviewPan();
    image.classList.toggle('zoomed',previewZoom>1.001);
    image.classList.toggle('panning',Boolean(previewPan));
    image.style.transition=animate?'transform .16s cubic-bezier(.2,.8,.2,1)':'none';
    image.style.transform=`translate3d(${previewPanX}px,${previewPanY}px,0) scale(${previewZoom})`;
  }
  function setPreviewZoom(value){
    const clientX=arguments.length>1&&Number.isFinite(Number(arguments[1]))?Number(arguments[1]):window.innerWidth/2;
    const clientY=arguments.length>2&&Number.isFinite(Number(arguments[2]))?Number(arguments[2]):window.innerHeight/2;
    const options=arguments.length>3&&arguments[3]&&typeof arguments[3]==='object'?arguments[3]:{};
    const animate=!!options.animate;
    const oldScale=Math.max(.001,previewZoom);
    const next=Math.max(.5,Math.min(6,Number(value||1)));
    const anchor=previewAnchor(clientX,clientY);
    // Keep the image point under the cursor/fingers stationary while scaling,
    // matching the macOS Preview/Safari feel instead of always zooming centrally.
    previewPanX=anchor.x-(anchor.x-previewPanX)*(next/oldScale);
    previewPanY=anchor.y-(anchor.y-previewPanY)*(next/oldScale);
    previewZoom=next;
    applyPreviewTransform({animate});
  }
  function panPreview(dx,dy){
    if(previewZoom<=1.001)return false;
    previewPanX+=Number(dx||0);previewPanY+=Number(dy||0);applyPreviewTransform();return true;
  }
  function resetPreview({animate=true}={}){
    previewZoom=1;previewPanX=0;previewPanY=0;previewPinch=null;previewPan=null;applyPreviewTransform({animate});
  }
  function fitPreviewImage(image,width=null,height=null){
    if(!image)return;
    const naturalWidth=Number(width||image.naturalWidth||0);
    const naturalHeight=Number(height||image.naturalHeight||0);
    if(!Number.isFinite(naturalWidth)||!Number.isFinite(naturalHeight)||naturalWidth<=0||naturalHeight<=0)return;
    const availableWidth=Math.max(120,window.innerWidth-72);
    const availableHeight=Math.max(120,window.innerHeight-72);
    const fitScale=Math.min(availableWidth/naturalWidth,availableHeight/naturalHeight);
    image.style.width=`${Math.max(1,Math.round(naturalWidth*fitScale))}px`;
    image.style.height=`${Math.max(1,Math.round(naturalHeight*fitScale))}px`;
    image.dataset.pudgeFitScale=String(fitScale);
    clampPreviewPan();
  }
  function coverRefFor(image){
    const kind=String(image?.dataset?.pudgeCoverKind||'').trim();
    const id=Number(image?.dataset?.pudgeCoverId||0);
    return kind&&Number.isFinite(id)&&id>0?{kind,id}:null;
  }
  function setRetryVisible(visible){
    const button=previewOverlay()?.querySelector?.('.pudge-cover-preview-retry');
    if(button)button.hidden=!visible;
  }
  async function preloadPreviewSource(source){
    const src=String(source||'').trim();if(!src)return null;
    if(previewDecodeInflight.has(src))return previewDecodeInflight.get(src);
    const task=(async()=>{
      const candidate=new Image();candidate.decoding='async';candidate.src=src;
      let timer=0;
      const timeout=new Promise(resolve=>{timer=setTimeout(()=>resolve(false),PREVIEW_DECODE_TIMEOUT_MS);});
      const load=(async()=>{
        if(typeof candidate.decode==='function'){
          try{await candidate.decode();}catch(_error){return false;}
        }else if(!candidate.complete){
          await new Promise(resolve=>{candidate.onload=()=>resolve();candidate.onerror=()=>resolve();});
        }
        return Boolean(candidate.naturalWidth&&candidate.naturalHeight);
      })();
      const ok=await Promise.race([load,timeout]);
      if(timer)clearTimeout(timer);
      return ok?candidate:null;
    })().finally(()=>previewDecodeInflight.delete(src));
    previewDecodeInflight.set(src,task);
    return task;
  }
  async function applyResolvedCoverRef(ref,generation){
    if(!ref||generation!==previewRequestGeneration||!previewOverlay())return false;
    const source=String(ref.preview_url||'').trim();
    if(!source)return false;
    const candidate=await preloadPreviewSource(source);
    if(!candidate||generation!==previewRequestGeneration||!previewOverlay())return false;
    const image=previewImage();if(!image)return false;
    image.src=source;
    image.dataset.pudgePreviewSourceKind=String(ref.source_kind||'');
    image.dataset.pudgePreviewRevision=String(ref.source_revision||'');
    fitPreviewImage(image,candidate.naturalWidth,candidate.naturalHeight);
    applyPreviewTransform();
    setRetryVisible(false);
    previewRetry=null;
    return true;
  }
  async function resolveCoverRef(stableRef,generation){
    if(!stableRef||!window.pywebview?.api?.cover_preview_resolve)return false;
    try{
      const key=`${stableRef.kind}:${stableRef.id}:${Math.max(1,Number(window.devicePixelRatio||1)).toFixed(2)}:${Number(window.innerWidth||0)}x${Number(window.innerHeight||0)}`;
      let request=previewResolveInflight.get(key);
      if(!request){
        request=Promise.resolve(window.pywebview.api.cover_preview_resolve(
          stableRef.kind,stableRef.id,Number(window.devicePixelRatio||1),
          Number(window.innerWidth||0),Number(window.innerHeight||0)
        )).finally(()=>previewResolveInflight.delete(key));
        previewResolveInflight.set(key,request);
      }
      const resolved=await request;
      const applied=await applyResolvedCoverRef(resolved,generation);
      if(!applied&&generation===previewRequestGeneration&&previewOverlay()){
        previewRetry=()=>resolveCoverRef(stableRef,generation);setRetryVisible(true);
      }
      return applied;
    }catch(_error){
      if(generation===previewRequestGeneration&&previewOverlay()){
        previewRetry=()=>resolveCoverRef(stableRef,generation);setRetryVisible(true);
      }
      return false;
    }
  }
  function closePreview(options={}){
    const invalidate=options.invalidate!==false;
    const restoreFocus=options.restoreFocus!==false;
    if(invalidate)previewRequestGeneration+=1;
    const overlay=previewOverlay();
    const restore=previewRestoreFocus;
    previewRestoreFocus=null;previewRetry=null;
    if(previewOpenFrame){cancelAnimationFrame(previewOpenFrame);previewOpenFrame=0;}
    const image=previewImage();
    if(previewPan?.pointerId!=null)image?.releasePointerCapture?.(previewPan.pointerId);
    if(drag?.peek)discardPeek(drag.peek,{snap:false});
    if(pinch?.peek&&pinch.peek!==drag?.peek)discardPeek(pinch.peek,{snap:false});
    drag=null;pinch=null;previewPinch=null;previewPan=null;previewZoom=1;previewPanX=0;previewPanY=0;
    // Closing is synchronous/idempotent: pending resolver/decode work is
    // invalidated before the overlay disappears and cannot resurrect it.
    overlay?.querySelector('img')?.style.setProperty('transition','none');
    overlay?.remove();
    if(restoreFocus&&restore?.isConnected&&typeof restore.focus==='function')queueMicrotask(()=>restore.focus({preventScroll:true}));
  }
  function previewFocusable(overlay){
    return [...(overlay?.querySelectorAll?.('button')||[])].filter(button=>!button.hidden);
  }
  function openPreviewSource(source,restoreFocusTo=null){
    const src=String(source||'').trim();if(!src)return 0;
    suppressClickUntil=performance.now()+500;
    closePreview({invalidate:false,restoreFocus:false});
    const generation=++previewRequestGeneration;
    previewRestoreFocus=restoreFocusTo?.isConnected?restoreFocusTo:(document.activeElement&&document.activeElement!==document.body?document.activeElement:null);
    const overlay=document.createElement('div');overlay.className='pudge-cover-preview';overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');overlay.setAttribute('aria-label','Cover preview');overlay.innerHTML=`<div class="pudge-cover-preview-card"><button class="pudge-cover-preview-close" type="button" aria-label="Close">×</button><button class="pudge-cover-preview-retry" type="button" aria-label="Retry high-resolution cover" title="Retry high-resolution cover" hidden>↻</button><img alt="" draggable="false" src="${src.replace(/&/g,'&amp;').replace(/"/g,'&quot;')}"></div>`;
    document.body.appendChild(overlay);previewZoom=1;previewPanX=0;previewPanY=0;
    const openedImage=overlay.querySelector('img');
    openedImage?.addEventListener('load',()=>{if(generation===previewRequestGeneration&&overlay.isConnected){fitPreviewImage(openedImage);applyPreviewTransform();}});
    if(openedImage?.complete)fitPreviewImage(openedImage);
    previewOpenFrame=requestAnimationFrame(()=>{previewOpenFrame=0;if(!overlay.isConnected||generation!==previewRequestGeneration)return;overlay.classList.add('open');applyPreviewTransform({animate:true});overlay.querySelector('.pudge-cover-preview-close')?.focus({preventScroll:true});});
    overlay.addEventListener('keydown',event=>{
      if(event.key!=='Tab')return;
      const buttons=previewFocusable(overlay);if(!buttons.length)return;
      event.preventDefault();const current=Math.max(0,buttons.indexOf(document.activeElement));
      const next=event.shiftKey?(current-1+buttons.length)%buttons.length:(current+1)%buttons.length;
      buttons[next].focus({preventScroll:true});
    });
    overlay.addEventListener('click',event=>{
      if(event.target.closest?.('.pudge-cover-preview-close')){closePreview();return;}
      if(event.target.closest?.('.pudge-cover-preview-retry')){event.preventDefault();void previewRetry?.();return;}
      const current=previewImage();
      if(!current){closePreview();return;}
      const rect=current.getBoundingClientRect();
      const inside=event.clientX>=rect.left&&event.clientX<=rect.right&&event.clientY>=rect.top&&event.clientY<=rect.bottom;
      // The transparent preview card keeps the image's original layout box.
      // After zooming out, clicks in that old box must still count as outside
      // when they are outside the image's *current transformed* rectangle.
      if(!inside)closePreview();
    });
    overlay.addEventListener('dblclick',event=>{if(event.target.closest?.('img')){event.preventDefault();setPreviewZoom(1);}});
    return generation;
  }
  function openPreview(image){
    const src=imageSource(image);if(!src)return;
    const generation=openPreviewSource(src,image);
    const stableRef=coverRefFor(image);
    if(stableRef&&generation)void resolveCoverRef(stableRef,generation);
  }
  function openResolvedRef(ref,thumbnail=''){
    const fallback=String(thumbnail||ref?.thumbnail_url||ref?.preview_url||'').trim();if(!fallback)return;
    const generation=openPreviewSource(fallback);if(generation)void applyResolvedCoverRef(ref,generation);
  }

  // Native image dragging would steal the pointer stream before the preview
  // threshold is reached, so cover drags belong exclusively to this gesture.
  document.addEventListener('dragstart',event=>{
    if(coverImage(event.target)||event.target.closest?.('.pudge-cover-preview img'))event.preventDefault();
  },true);

  document.addEventListener('pointerdown',event=>{
    const pressedCover=coverImage(event.target);
    if(pressedCover&&allowMouseDragPreview(pressedCover))event.preventDefault();
    const openImage=event.target.closest?.('.pudge-cover-preview img');
    if(openImage&&event.button===0&&previewZoom>1.001){
      event.preventDefault();
      previewPan={pointerId:event.pointerId,lastX:event.clientX,lastY:event.clientY,moved:false};
      openImage.setPointerCapture?.(event.pointerId);applyPreviewTransform();return;
    }
    if(event.button!==0||event.pointerType!=='mouse'||previewOverlay())return;
    const image=pressedCover;if(!image||!allowMouseDragPreview(image)||!imageSource(image))return;
    drag={pointerId:event.pointerId,image,startX:event.clientX,startY:event.clientY,peek:null,moved:false,opened:false};
    image.setPointerCapture?.(event.pointerId);
  },true);
  document.addEventListener('pointermove',event=>{
    if(previewPan&&previewPan.pointerId===event.pointerId){
      event.preventDefault();
      const dx=event.clientX-previewPan.lastX,dy=event.clientY-previewPan.lastY;
      if(Math.abs(dx)+Math.abs(dy)>1)previewPan.moved=true;
      previewPan.lastX=event.clientX;previewPan.lastY=event.clientY;panPreview(dx,dy);return;
    }
    if(!drag||drag.pointerId!==event.pointerId||drag.opened)return;
    const distance=Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY);
    if(distance<2)return;drag.moved=true;
    if(!drag.peek)drag.peek=makePeek(drag.image);
    const scale=1+Math.min(.42,distance/360);
    if(drag.peek)drag.peek.style.transform=`scale(${scale})`;
    if(distance>=MOUSE_OPEN_DISTANCE){drag.opened=true;discardPeek(drag.peek,{snap:false});openPreview(drag.image);}
  },{capture:true,passive:false});
  function finishPointer(event){
    if(previewPan&&previewPan.pointerId===event.pointerId){
      const image=previewImage();image?.releasePointerCapture?.(event.pointerId);previewPan=null;applyPreviewTransform();return;
    }
    if(!drag||drag.pointerId!==event.pointerId)return;
    if(drag.moved)suppressClickUntil=performance.now()+350;
    if(!drag.opened&&!drag.moved&&drag.image?.matches?.('.ln-inline-image img')){
      const image=drag.image,figure=image.closest('.ln-inline-image'),reader=image.closest('#lnReader');
      discardPeek(drag.peek,{snap:false});suppressClickUntil=performance.now()+350;drag=null;
      if(reader?.classList.contains('blur-images')&&!figure?.classList.contains('revealed')){figure?.classList.add('revealed');if(figure)figure.dataset.pudgeConsumeRevealClick='1';return;}
      openPreview(image);return;
    }
    if(!drag.opened)discardPeek(drag.peek,{snap:true});
    drag=null;
  }
  document.addEventListener('pointerup',finishPointer,true);
  document.addEventListener('pointercancel',finishPointer,true);

  // Safari/WKWebView trackpad/touch pinch. A short pinch visibly expands then
  // snaps back; crossing the threshold opens the closable image preview. Once
  // open, pinch zoom stays anchored under the fingers and can be panned.
  document.addEventListener('gesturestart',event=>{
    if(event.target.closest?.('.pudge-cover-preview')){
      event.preventDefault();
      const anchor=previewAnchor(event.clientX,event.clientY);
      previewPinch={base:previewZoom,baseScale:previewZoom,baseX:previewPanX,baseY:previewPanY,anchorX:anchor.x,anchorY:anchor.y};return;
    }
    const image=coverImage(event.target);if(!image||!imageSource(image))return;
    event.preventDefault();pinch={image,peek:makePeek(image),opened:false};
  },{capture:true,passive:false});
  document.addEventListener('gesturechange',event=>{
    if(previewPinch){
      event.preventDefault();
      const next=Math.max(MIN_PREVIEW_ZOOM,Math.min(MAX_PREVIEW_ZOOM,previewPinch.base*Math.max(.2,Number(event.scale||1))));
      const ratio=next/Math.max(.001,previewPinch.baseScale);
      previewZoom=next;
      previewPanX=previewPinch.anchorX-(previewPinch.anchorX-previewPinch.baseX)*ratio;
      previewPanY=previewPinch.anchorY-(previewPinch.anchorY-previewPinch.baseY)*ratio;
      applyPreviewTransform();return;
    }
    if(!pinch||pinch.opened)return;event.preventDefault();
    const scale=Math.max(1,Math.min(1.42,Number(event.scale||1)));
    if(pinch.peek)pinch.peek.style.transform=`scale(${scale})`;
    if(scale>=PINCH_OPEN_SCALE){pinch.opened=true;discardPeek(pinch.peek,{snap:false});openPreview(pinch.image);}
  },{capture:true,passive:false});
  document.addEventListener('gestureend',event=>{
    if(previewPinch){event.preventDefault();previewPinch=null;applyPreviewTransform();return;}
    if(!pinch)return;event.preventDefault();if(!pinch.opened)discardPeek(pinch.peek,{snap:true});pinch=null;
  },{capture:true,passive:false});
  document.addEventListener('wheel',event=>{
    if(!event.target.closest?.('.pudge-cover-preview'))return;
    if(event.ctrlKey){
      event.preventDefault();
      setPreviewZoom(previewZoom*Math.exp(-Number(event.deltaY||0)*.01),event.clientX,event.clientY);
      return;
    }
    // Two-finger trackpad scroll pans an enlarged image in both axes.
    if(previewZoom>1.001){event.preventDefault();panPreview(-Number(event.deltaX||0),-Number(event.deltaY||0));}
  },{capture:true,passive:false});

  window.addEventListener('resize',()=>{const image=previewImage();if(image){fitPreviewImage(image);applyPreviewTransform();}});
  document.addEventListener('click',event=>{
    if(performance.now()<suppressClickUntil&&coverImage(event.target)){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  window.PudgeCoverPreview={open:image=>openPreview(image),openSource:source=>openPreviewSource(source),openRef:(ref,thumbnail)=>openResolvedRef(ref,thumbnail),close:closePreview,isOpen:()=>Boolean(previewOverlay()),closeIfOpen:()=>{if(!previewOverlay())return false;closePreview();return true;},zoom:setPreviewZoom,pan:panPreview};
})();
