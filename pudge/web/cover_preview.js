'use strict';

(() => {
  const COVER_SELECTOR = [
    '.cover-shell img',
    'img.ln-card-cover',
    '.audiobook-cover img',
    '.manga-v2-cover-shell img',
    '.library-cover-shell img',
    '.planned-suggestion-grid img',
    '.list-cover img',
    '.airing-cover-shell img'
  ].join(',');
  const MOUSE_OPEN_DISTANCE = 148;
  const PINCH_OPEN_SCALE = 1.36;
  let drag = null;
  let pinch = null;
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
  function closePreview(){
    const overlay=document.querySelector('.pudge-cover-preview');if(!overlay)return;
    overlay.classList.remove('open');setTimeout(()=>overlay.remove(),180);
  }
  function openPreview(image){
    const src=imageSource(image);if(!src)return;
    suppressClickUntil=performance.now()+500;
    closePreview();
    const overlay=document.createElement('div');overlay.className='pudge-cover-preview';overlay.setAttribute('role','dialog');overlay.setAttribute('aria-modal','true');overlay.innerHTML=`<div class="pudge-cover-preview-card"><button class="pudge-cover-preview-close" type="button" aria-label="Close">×</button><img alt="" src="${src.replace(/&/g,'&amp;').replace(/"/g,'&quot;')}"></div>`;
    document.body.appendChild(overlay);requestAnimationFrame(()=>overlay.classList.add('open'));
    overlay.addEventListener('click',event=>{if(event.target===overlay||event.target.closest?.('.pudge-cover-preview-close'))closePreview();});
  }

  // Native image dragging would steal the pointer stream before the preview
  // threshold is reached, so cover drags belong exclusively to this gesture.
  document.addEventListener('dragstart',event=>{
    if(coverImage(event.target))event.preventDefault();
  },true);

  document.addEventListener('pointerdown',event=>{
    if(event.button!==0||event.pointerType!=='mouse'||document.querySelector('.pudge-cover-preview'))return;
    const image=coverImage(event.target);if(!image||!imageSource(image))return;
    drag={pointerId:event.pointerId,image,startX:event.clientX,startY:event.clientY,peek:makePeek(image),moved:false,opened:false};
    image.setPointerCapture?.(event.pointerId);
  },true);
  document.addEventListener('pointermove',event=>{
    if(!drag||drag.pointerId!==event.pointerId||drag.opened)return;
    const distance=Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY);
    if(distance<2)return;drag.moved=true;
    const scale=1+Math.min(.42,distance/360);
    if(drag.peek)drag.peek.style.transform=`scale(${scale})`;
    if(distance>=MOUSE_OPEN_DISTANCE){drag.opened=true;discardPeek(drag.peek,{snap:false});openPreview(drag.image);}
  },true);
  function finishMouseDrag(event){
    if(!drag||drag.pointerId!==event.pointerId)return;
    if(drag.moved)suppressClickUntil=performance.now()+350;
    if(!drag.opened)discardPeek(drag.peek,{snap:true});
    drag=null;
  }
  document.addEventListener('pointerup',finishMouseDrag,true);
  document.addEventListener('pointercancel',finishMouseDrag,true);

  // Safari/WKWebView trackpad/touch pinch. A short pinch visibly expands then
  // snaps back; crossing the threshold opens the closable image preview.
  document.addEventListener('gesturestart',event=>{
    const image=coverImage(event.target);if(!image||!imageSource(image))return;
    event.preventDefault();pinch={image,peek:makePeek(image),opened:false};
  },{capture:true,passive:false});
  document.addEventListener('gesturechange',event=>{
    if(!pinch||pinch.opened)return;event.preventDefault();
    const scale=Math.max(1,Math.min(1.42,Number(event.scale||1)));
    if(pinch.peek)pinch.peek.style.transform=`scale(${scale})`;
    if(scale>=PINCH_OPEN_SCALE){pinch.opened=true;discardPeek(pinch.peek,{snap:false});openPreview(pinch.image);}
  },{capture:true,passive:false});
  document.addEventListener('gestureend',event=>{
    if(!pinch)return;event.preventDefault();if(!pinch.opened)discardPeek(pinch.peek,{snap:true});pinch=null;
  },{capture:true,passive:false});

  document.addEventListener('click',event=>{
    if(performance.now()<suppressClickUntil&&coverImage(event.target)){event.preventDefault();event.stopImmediatePropagation();}
  },true);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&document.querySelector('.pudge-cover-preview')){event.preventDefault();closePreview();}},true);

  window.PudgeCoverPreview={open:image=>openPreview(image),close:closePreview};
})();
