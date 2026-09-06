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

  const coverImage = target => target?.closest?.(COVER_SELECTOR) || null;
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
  function closePreview(){
    const overlay=previewOverlay();if(!overlay)return;
    previewPinch=null;previewPan=null;previewZoom=1;previewPanX=0;previewPanY=0;
    // Closing is deliberately synchronous.  A pending transform/fade used to
    // keep an invisible preview over the UI for ~180 ms and made repeated
    // open/close gestures feel stuck.
    overlay.querySelector('img')?.style.setProperty('transition','none');
    overlay.remove();
  }
  function openPreviewSource(source){
    const src=String(source||'').trim();if(!src)return;
    suppressClickUntil=performance.now()+500;
    closePreview();
    const overlay=document.createElement('div');overlay.className='pudge-cover-preview';overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');overlay.innerHTML=`<div class="pudge-cover-preview-card"><button class="pudge-cover-preview-close" type="button" aria-label="Close">×</button><img alt="" draggable="false" src="${src.replace(/&/g,'&amp;').replace(/"/g,'&quot;')}"></div>`;
    document.body.appendChild(overlay);previewZoom=1;previewPanX=0;previewPanY=0;requestAnimationFrame(()=>{overlay.classList.add('open');applyPreviewTransform({animate:true});});
    overlay.addEventListener('click',event=>{
      if(event.target.closest?.('.pudge-cover-preview-close')){closePreview();return;}
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
  }
  function openPreview(image){const src=imageSource(image);if(!src)return;openPreviewSource(src);}

  // Native image dragging would steal the pointer stream before the preview
  // threshold is reached, so cover drags belong exclusively to this gesture.
  document.addEventListener('dragstart',event=>{
    if(coverImage(event.target)||event.target.closest?.('.pudge-cover-preview img'))event.preventDefault();
  },true);

  document.addEventListener('pointerdown',event=>{
    if(coverImage(event.target))event.preventDefault();
    const openImage=event.target.closest?.('.pudge-cover-preview img');
    if(openImage&&event.button===0&&previewZoom>1.001){
      event.preventDefault();
      previewPan={pointerId:event.pointerId,lastX:event.clientX,lastY:event.clientY,moved:false};
      openImage.setPointerCapture?.(event.pointerId);applyPreviewTransform();return;
    }
    if(event.button!==0||event.pointerType!=='mouse'||previewOverlay())return;
    const image=coverImage(event.target);if(!image||!imageSource(image))return;
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

  window.addEventListener('resize',()=>{if(previewOverlay())applyPreviewTransform();});
  document.addEventListener('click',event=>{
    if(performance.now()<suppressClickUntil&&coverImage(event.target)){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&previewOverlay()){event.preventDefault();closePreview();}},true);

  window.PudgeCoverPreview={open:image=>openPreview(image),openSource:source=>openPreviewSource(source),close:closePreview,zoom:setPreviewZoom,pan:panPreview};
})();
