/* Low RAM mode (experimental, Settings → Advanced; pudge/low_ram.py).
   With it on the page renders lighter:
   - off-screen cards skip layout and painting (CSS content-visibility, html.low-ram),
   - Light novels and Statistics drop their DOM a minute after you leave them (Planning too,
     instead of after five minutes) and are built again when opened,
   - AniList covers load at list size, local covers as their static list copy (.list.jpg),
   - no cover animation: the foil covers keep their still look, the wake/hover motion and its
     timers do not run.
   Off: nothing here changes the page. In both modes Python asks unloadVeto() before it lets
   the hidden window's page go (its window process ends 5 min after a close, 30 s in Low RAM). */
(function(g){
  'use strict';
  const TAB_RELEASE_MS=60*1000;
  const RELEASABLE={lightnovels:'lightNovelsContent',statistics:'statisticsContent'};
  const timers=new Map();
  let on=false;
  // Settings is a form saved with its Save button: typed into, it must not go with the page.
  let settingsTouched=false;
  function touched(event){
    const target=event?.target;
    if(target&&typeof target.closest==='function'&&target.closest('#settingsContent'))settingsTouched=true;
  }
  g.document?.addEventListener?.('input',touched,true);
  g.document?.addEventListener?.('change',touched,true);
  // The page's `ui` is a global const of index.html's script (not a window property).
  function state(){try{return typeof ui!=='undefined'?ui:null;}catch(_){return null;}}
  function enabled(){return on;}
  function apply(settings){
    if(!settings||typeof settings!=='object')return on;
    const next=!!settings.low_ram_mode;
    g.document?.documentElement?.classList.toggle('low-ram',next);
    if(next===on)return on;
    on=next;
    if(!on){for(const timer of timers.values())clearTimeout(timer);timers.clear();}
    return on;
  }
  function pageChanged(from,to){
    if(from==='settings'&&to!=='settings')settingsTouched=false;  // Settings is drawn anew next time
    const entered=timers.get(to);
    if(entered){clearTimeout(entered);timers.delete(to);}
    if(!on||from===to||!RELEASABLE[from])return;
    if(timers.has(from))clearTimeout(timers.get(from));
    timers.set(from,setTimeout(()=>release(from),TAB_RELEASE_MS));
  }
  function readerOpen(){
    const doc=g.document;
    return !!(doc?.getElementById('lnReaderShell')?.classList.contains('open')||doc?.querySelector('.manga-v2-reader'));
  }
  function release(page){
    timers.delete(page);
    const current=state();
    if(!on||current?.page===page)return false;
    const box=g.document?.getElementById(RELEASABLE[page]);
    if(!box||!box.firstChild)return false;
    if(page==='statistics'){
      if(box.querySelector('#statsModalHost *'))return false;  // a dialog with input stays
      const stats=g.PudgeStatistics?.state;
      if(stats){stats.payload=null;stats.journalRows=[];}
    }
    box.replaceChildren();
    return true;
  }
  // AniList: cover/large (extraLarge, ~460 px) → cover/medium (~230 px, a card's size).
  // Local: a stored cover's static list copy before the original (an animated GIF/WebP).
  function smallCover(source){
    if(!on)return '';
    const text=String(source||'');
    const anilist=text.replace(/(\/anilistcdn\/media\/(?:anime|manga)\/cover\/)large\//,'$1medium/');
    if(anilist!==text)return anilist;
    if(/(?:^|\/)covers\/[^/?#]+\.(?:gif|webp|png|apng)(?=$|[?#])/i.test(text)&&!/\.list\.jpg(?=$|[?#])/.test(text))return text.replace(/((?:^|\/)covers\/[^/?#]+)(?=$|[?#])/,'$1.list.jpg');
    return '';
  }
  function coverMotion(){return !on;}
  // Why the hidden page must stay ('' when it may go): an episode it follows in mpv (it
  // marks it watched and plays the next one of a queue), UI preferences not saved yet,
  // an open reader, dialog or onboarding, Settings typed into (saved only with its button).
  function unloadVeto(){
    const current=state();
    if(current?.playMonitors?.size)return 'play_monitor';
    if(g.PudgeUiStorage?.pending?.())return 'ui_prefs_saving';
    if(readerOpen())return 'reader';
    if(g.document?.getElementById('modalBackdrop')?.classList.contains('open'))return 'dialog';
    if(g.document?.getElementById('onboardingBackdrop')?.classList.contains('open'))return 'onboarding';
    if(settingsTouched&&current?.page==='settings')return 'settings_unsaved';
    return '';
  }
  g.PudgeLowRam={TAB_RELEASE_MS,enabled,apply,pageChanged,release,smallCover,coverMotion,unloadVeto};
})(window);
