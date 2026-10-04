'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.argv[2], scenario = process.argv[3];
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise, resolve, reject}; };
const flush = async () => { for(let i=0;i<20;i++) await Promise.resolve(); };
function classes(...initial) {
  const values = new Set(initial);
  return {add:(...xs)=>xs.forEach(x=>values.add(x)),remove:(...xs)=>xs.forEach(x=>values.delete(x)),contains:x=>values.has(x),
    toggle:(x,on)=>{if(on ?? !values.has(x))values.add(x);else values.delete(x);}};
}
function element() {
  return {dataset:{},style:{setProperty(){},removeProperty(){}},classList:classes(),hidden:false,isConnected:true,
    innerHTML:'',textContent:'',querySelector:()=>null,querySelectorAll:()=>[],replaceChildren(){this.innerHTML='';},
    setAttribute(){},toggleAttribute(){},addEventListener(){},getBoundingClientRect:()=>({top:0,bottom:100,width:100,height:100}),
    scrollIntoView(){this.scrolls=(this.scrolls||0)+1;}};
}
function harness() {
  const nodes = new Map(), listeners = new Map(), timers = [], frames = [], observers = [];
  const doc = {documentElement:{lang:'en',classList:classes(),style:{setProperty(){}}},body:element(),hidden:false,activeElement:null,
    getElementById:id=>nodes.get(id)||null,querySelector:()=>null,querySelectorAll:()=>[],createElement:element,
    addEventListener:(name,fn)=>listeners.set(name,[...(listeners.get(name)||[]),fn])};
  const context = {console,document:doc,localStorage:{getItem:()=>null,setItem(){}},
    setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){},requestAnimationFrame:fn=>{frames.push(fn);return frames.length;},cancelAnimationFrame(){},
    queueMicrotask(){},performance:{now:()=>1000},getComputedStyle:()=>({}),CustomEvent:function(){},
    MutationObserver:class{observe(){}},IntersectionObserver:class{constructor(callback,options){this.callback=callback;this.options=options;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}},
    HTMLInputElement:class{},HTMLSelectElement:class{},addEventListener(){},dispatchEvent(){},innerWidth:1000,innerHeight:800};
  context.window = context;
  vm.createContext(context);
  // Match the app: a lexical const does not create window.ui.
  vm.runInContext("const ui={lang:'en',state:{settings:{}},lnState:{settings:{study_backend:'jpdb'}}};globalThis.lexicalUi=ui;",context);
  return {context,nodes,listeners,timers,frames,observers,document:doc};
}
function load(h,name,transform=x=>x) {
  vm.runInContext(transform(fs.readFileSync(path.join(web,name),'utf8')),h.context,{filename:name});
}
function manga(api={}) {
  const h = harness();
  for (const id of ['mangaReaderV2','mangaV2Pages','mangaV2PageLabel','mangaV2Viewport'])h.nodes.set(id,element());
  Object.assign(h.nodes.get('mangaV2Viewport'),{scrollTop:0,clientHeight:500,scrollHeight:20000});
  h.context.pywebview={api};
  h.context.PudgeReviewGate={require:async()=>true};
  load(h,'manga_reader_v2.js',s=>s.replace('  window.PudgeMangaReaderV2 = {',`  window.mangaTest={renderPaged,renderVertical,persistVisiblePage,markReadThrough,openBook,closeReader,movePage,requireMangaPart,currentStudyBackend,openMangaSelectedText,dispatchMangaVirtualStudyHit,
    set(book,page=0,mode='single'){currentBook=book;currentPage=page;approvedPage=page;currentPageCount=Number(book?.page_count||0);settings.mode=mode;pageCache=new Map();},
    state:()=>({book:currentBook,page:currentPage})};\n  window.PudgeMangaReaderV2 = {`));
  h.test=h.context.mangaTest;
  return h;
}
function sidebar(api={}) {
  const h=harness(),host=Object.assign(element(),{offsetParent:{},getClientRects:()=>[{}]}),root=element();
  root.querySelector=s=>s==='[data-sidebar-due]'?host:null;root.contains=()=>false;
  h.context.pywebview={api:{sidebar_due_review_cards:async()=>({cards:[]}),...api}};h.context.toast=()=>{};
  load(h,'sidebar_companion.js',s=>s.replace('  g.PudgeSidebarCompanion = {',`  g.sidebarTest={handleKeydown,audioClick,renderDue,
    set(value){if('root'in value)root=value.root;if('card'in value)dueCard=value.card;if('revealed'in value)dueRevealed=value.revealed;if('audio'in value)audioState=value.audio;},
    state:()=>({revealed:dueRevealed})};\n  g.PudgeSidebarCompanion = {`));
  h.test=h.context.sidebarTest;h.test.set({root,card:{wordId:1,readingIndex:0,wordTextPlain:'猫'},revealed:false});
  return {...h,host,root};
}
const tests = {
  async manga_read_progress() {
    // Loading/prefetching is unread; forward turns count the physical pages left behind.
    for (const [mode, want] of [['single', 4], ['double', 5]]) {
      const marked = [];
      const h = manga({
        manga_page: async (_, index) => ({page_index:index,data_uri:'data:image/png;base64,AA',width:800,height:1200}),
        manga_mark_read: async (_, index) => {marked.push(index);return {read_pages:index+1};},
      });
      h.test.set({id:1,page_count:10,position:4,read_pages:0},4,mode);
      await h.test.renderPaged();await flush();
      assert.deepEqual(marked, [], `${mode}: loading visible pages does not mark them read`);
      await h.test.movePage(1);await flush();
      assert.deepEqual(marked, [want], `${mode}: a forward turn counts exactly the pages left behind`);
      await h.test.movePage(-1);await flush();
      assert.deepEqual(marked, [want], `${mode}: going back never marks more pages read`);
      h.test.set({id:1,page_count:10,position:9,read_pages:want+1},9,mode);
      await h.test.renderPaged();await flush();
      assert.deepEqual(marked, [want, 9], `${mode}: the final page counts without another turn`);
    }
    const marked = [];
    const h = manga({manga_mark_read:async (_, index)=>{marked.push(index);return {read_pages:index+1};}});
    const frame = Object.assign(element(),{dataset:{pageIndex:'5'}});
    h.nodes.get('mangaV2Pages').querySelectorAll = () => [frame];
    h.test.set({id:1,page_count:10,position:4,read_pages:0},4,'vertical');
    h.test.renderVertical();
    assert.deepEqual(marked, [], 'vertical placeholders do not count as reading');
    const visible = h.observers.find(observer=>observer.options?.threshold);
    visible.callback([{target:frame,isIntersecting:true,intersectionRatio:1}]);await flush();
    assert.deepEqual(marked, [4], 'scrolling forward counts only pages above the visible page');
    frame.dataset.pageIndex='3';visible.callback([{target:frame,isIntersecting:true,intersectionRatio:1}]);await flush();
    assert.deepEqual(marked, [4], 'scrolling backward leaves read progress unchanged');
    const viewport = h.nodes.get('mangaV2Viewport');viewport.scrollTop=19500;
    await h.test.persistVisiblePage();
    assert.deepEqual(marked, [4, 9], 'reaching the vertical strip end counts the final page');
  },
  async reader_exit() {
    // Both reader exits close the real assistant panel and its collapsed tab.
    for (const kind of ['ln', 'manga']) {
      const h = kind === 'manga' ? manga() : harness(), c = h.context;
      c.PudgeReadingTools={llmAvailable:()=>true};load(h,'reading_assistant.js');
      const assistant=element(),tab=element();h.nodes.set('pudgeAssistant',assistant);h.nodes.set('pudgeAssistantTab',tab);
      assert.equal(c.PudgeAssistant.isOpen(),true);
      if (kind === 'manga') {
        h.test.set({id:1,page_count:10});h.nodes.get('mangaReaderV2').classList.add('open');
        h.test.closeReader();
        assert.equal(h.nodes.get('mangaReaderV2').classList.contains('open'),false);
        assert.equal(h.test.state().book,null);
      } else {
        c.$=id=>{if(!h.nodes.has(id))h.nodes.set(id,element());return h.nodes.get(id);};
        c.$('lnReaderShell').classList.add('open');
        Object.assign(c.lexicalUi,{lnOpenRequestToken:1,lnChapterLoadToken:0,lnChapterCacheGeneration:0,lnChapterPayloadCache:new Map(),lnTokenMap:new Map()});
        for(const name of ['closeLnFind','closeLnChapterPicker','cancelLnAutoBookmark','stopLnParsePoll','stopLnPairedPoll','hideLnTranslation','persistReadTogetherSession'])c[name]=()=>{};
        c.storedReadTogetherSession=()=>null;c.pywebview={api:{light_novel_cancel_reader_background:async()=>{}}};
        const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
        const close=html.slice(html.indexOf("    if(target.id==='lnReaderClose')"),html.indexOf('    if(target.dataset.lnAudioSeek'));
        vm.runInContext(`globalThis.closeLnReader=async()=>{const target={id:'lnReaderClose'};${close}}`,c);
        await c.closeLnReader();
        assert.equal(c.$('lnReaderShell').classList.contains('open'),false);
        assert.equal(c.lexicalUi.lnOpenRequestToken,2,'closing invalidates pending reader opens');
      }
      assert.equal(c.PudgeAssistant.isOpen(),false,`${kind}: assistant closes with reader`);
      assert.equal(assistant.hidden,true);
      assert.equal(tab.hidden,true,`${kind}: a collapsed assistant cannot reopen after reader exit`);
    }
  },
  async paged() {
    // Removing/using an undefined prefetch step breaks a complete page turn.
    for(const [mode,want] of [['single',[4,3,5]],['double',[4,5,2,6]]]) {
      const loaded=[];
      const h=manga({manga_page:async(id,index)=>{loaded.push(index);return {page_index:index,data_uri:'data:image/png;base64,AA',name:`p${index}`,width:800,height:1200};},manga_mark_read:async()=>({read_pages:5})});
      h.test.set({id:1,page_count:12,position:4,read_pages:0},4,mode);
      await h.test.renderPaged();await flush();
      assert.deepEqual(loaded,want,'prefetch follows the number of physical visible pages');
      assert.match(h.nodes.get('mangaV2Pages').innerHTML,/data-page-index="4"/);
      await h.test.movePage(1);await flush();
      assert.equal(h.test.state().page,mode==='single'?5:6);
    }
  },
  async progress() {
    // A reply for A must not modify B, and reordered replies cannot rewind A.
    const replies=[];
    const h=manga({manga_mark_read:(id,index)=>{const reply=deferred();replies.push({id,index,...reply});return reply.promise;}});
    const a={id:1,page_count:100,read_pages:0},b={id:2,page_count:100,read_pages:0};
    h.test.set(a);const old=h.test.markReadThrough(79);
    h.test.set(b);replies[0].resolve({id:1,read_pages:80});await old;
    assert.equal(b.read_pages,0,'late progress belongs to the requested book');
    h.test.set(a);a.read_pages=0;
    const first=h.test.markReadThrough(9),second=h.test.markReadThrough(19);
    replies[2].resolve({id:1,read_pages:20});await second;
    replies[1].resolve({id:1,read_pages:10});await first;
    assert.equal(a.read_pages,20,'out-of-order saves preserve read progress');
    const closing=h.test.markReadThrough(29);h.test.set(null);replies[3].resolve({id:1,read_pages:30});await closing;
    assert.equal(h.test.state().book,null);
  },
  async backend() {
    // Native selection and virtual OCR hits must retain JPDB identifiers/provider.
    const opened=[];
    const h=manga();
    h.context.PudgeReadingTools={study:{openText:async(...args)=>{opened.push(args);return true;}}};
    const rect={left:0,right:50,top:0,bottom:50,width:50,height:50};
    await h.test.openMangaSelectedText({text:'猫',rect},{clientX:20,clientY:20},'native');
    await h.test.dispatchMangaVirtualStudyHit({virtualText:'犬',core:{x:0,y:0,w:.5,h:.5},x:0,y:0,w:.5,h:.5},
      {querySelector:()=>({getBoundingClientRect:()=>({left:0,top:0,width:100,height:100})})});
    assert.equal(opened[0][2].backend,'jpdb');
    assert.equal(opened[1][2].backend,'jpdb');
    h.context.lexicalUi.lnState.settings.study_backend='jiten';
    await h.test.openMangaSelectedText({text:'猫',rect},{clientX:20,clientY:20},'native');
    assert.equal(opened[2][2].backend,'jiten');
  },
  async vertical() {
    // Granting the next part keeps the scroll strip and observer intact.
    const loaded=[];
    const h=manga({manga_page:async(id,index)=>{loaded.push(index);return {page_index:index,data_uri:'page',name:'page'};}});
    h.test.set({id:1,page_count:60,read_pages:20},19,'vertical');
    const frames=[19,20,21].map(index=>Object.assign(element(),{dataset:{pageIndex:String(index)}}));
    const pages=h.nodes.get('mangaV2Pages');
    pages.querySelectorAll=()=>frames;
    pages.querySelector=selector=>frames.find(x=>selector.includes(`"${x.dataset.pageIndex}"`));
    await h.test.renderVertical();h.frames.shift()();
    const original=pages.innerHTML,observerCount=h.observers.length;
    const observer=h.observers.find(x=>x.options.rootMargin==='0px')||h.observers[0];
    const preload=h.observers.find(x=>x.options.rootMargin==='120% 0px');
    preload?.callback(frames.map(target=>({target,isIntersecting:true,intersectionRatio:1})));
    observer.callback([{target:frames[1],isIntersecting:true,intersectionRatio:.7},{target:frames[2],isIntersecting:true,intersectionRatio:0}]);
    await flush();
    assert.equal(h.observers.length,observerCount,'part approval does not rebuild the vertical strip');
    assert.equal(pages.innerHTML,original);
    assert.equal(h.test.state().page,20);
    assert.equal(frames[1].scrolls||0,0,'approval must not center the next page');
    assert(loaded.includes(21),'nearby page preloads before it appears');
    assert(preload,'the preload observer reaches beyond the viewport');
  },
  async part_grant() {
    // Approval applies to the whole twenty-page part, including empty-card grants.
    const calls=[];const h=manga();
    h.context.PudgeReviewGate.require=async(...args)=>{calls.push(args);return true;};
    h.test.set({id:1,page_count:100});
    assert.equal(await h.test.requireMangaPart(0),true);
    assert.equal(await h.test.requireMangaPart(1),true);
    assert.equal(await h.test.requireMangaPart(19),true);
    assert.equal(await h.test.requireMangaPart(20),true);
    assert.equal(await h.test.requireMangaPart(21),true);
    assert.deepEqual(calls,[['manga',1,0],['manga',1,20]]);
  },
  async gate_disabled() {
    // Lexical state disables the gate without painting or contacting backend.
    const h=harness();let calls=0,created=0;
    h.document.createElement=()=>{created++;return element();};
    h.context.pywebview={api:new Proxy({}, {get:()=>()=>{calls++;throw Error('disabled gate called backend');}})};
    h.context.lexicalUi.state.settings={review_gate_ln_enabled:false,review_gate_manga_enabled:false};
    load(h,'review_gate.js');
    assert.equal(await h.context.PudgeReviewGate.require('ln',1,0),true);
    assert.equal(await h.context.PudgeReviewGate.require('manga',2,20),true);
    assert.equal(created,0);assert.equal(calls,0);
  },
  async ln_generation() {
    // Opening B/closing while A awaits audio context cancels A's final paint.
    const source=fs.readFileSync(path.join(web,'index.html'),'utf8');
    const open=source.slice(source.indexOf('async function openLightNovel('),source.indexOf('async function loadLightNovelChapter('));
    const close=source.slice(source.indexOf("    if(target.id==='lnReaderClose')"),source.indexOf('    if(target.dataset.lnAudioSeek'));
    for(const action of ['newer','close','old_failure']) {
      const h=harness(),audio=deferred(),c=h.context,ui=c.lexicalUi;
      const nodes=new Map();c.$=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
      Object.assign(ui,{lnChapterCacheGeneration:0,lnChapterLoadToken:0,lnChapterPayloadCache:new Map(),lnTokenMap:new Map()});
      for(const name of ['installLnSelectionDiagnostics','persistReadTogetherSession','cancelLnAutoBookmark','populateLnReaderAppearance','applyLnReaderSettings','syncLnFinishButton','ensureLnPairedControls','syncLnChapterPicker','syncLnPairedTray','pollLnPaired','closeLnFind','closeLnChapterPicker','stopLnParsePoll','stopLnPairedPoll','hideLnTranslation'])c[name]=()=>{};
      c.storedReadTogetherSession=()=>null;c.loadLightNovelChapter=async()=>{};c.requestAnimationFrame=fn=>fn();
      c.PudgeReviewGate={require:async()=>true};
      c.pywebview={api:{light_novel_open:async id=>({id,title:`Book ${id}`,current_chapter:0,chapters:[],paired_audio:id===1?{audiobook_id:9}:null}),
        audiobook_review_context:()=>audio.promise,light_novel_cancel_reader_background:async()=>{}}};
      vm.runInContext(open+`\nglobalThis.closeLnReader=async()=>{const target={id:'lnReaderClose'};${close}}`,c);
      const stale=c.openLightNovel(1);await flush();
      if(action==='newer'||action==='old_failure')await c.openLightNovel(2);
      else await c.closeLnReader();
      if(action==='old_failure')audio.reject(Error('old context failure'));else audio.resolve({});
      await stale;
      if(action==='close')assert.equal(c.$('lnReaderShell').classList.contains('open'),false,'close invalidates outstanding opens');
      else assert.equal(ui.lnBook.id,2,'the latest selected book wins after every asynchronous boundary');
    }
  },
  async sidebar_keys() {
    // A hidden/covered sidebar must leave Space and grading shortcuts alone.
    for(const blocked of ['hidden','layout','ln','manga','energy','modal','button','visible']) {
      const h=sidebar();let prevented=0;
      if(blocked==='hidden')h.host.hidden=true;
      if(blocked==='layout'){h.host.offsetParent=null;h.host.getClientRects=()=>[];}
      if(blocked==='energy')h.document.documentElement.classList.add('energy-saving');
      if(blocked==='ln')h.nodes.set('lnReaderShell',Object.assign(element(),{classList:classes('open')}));
      if(blocked==='manga')h.nodes.set('mangaReaderV2',Object.assign(element(),{classList:classes('open')}));
      if(blocked==='modal')h.document.querySelector=s=>s.includes('.modal-backdrop.open')?element():null;
      const button={closest:s=>s.includes('button')?button:null};
      const event={target:blocked==='button'?button:{closest:()=>null},key:' ',code:'Space',preventDefault(){prevented++;},stopPropagation(){},stopImmediatePropagation(){}};
      h.test.handleKeydown(event);
      assert.equal(prevented,blocked==='visible'?1:0,blocked);
      assert.equal(h.test.state().revealed,blocked==='visible',blocked);
    }
  },
  async sidebar_handoff() {
    // The reading handoff fetches fresh book metadata at the user's click.
    let fetched=0,opened;
    const h=sidebar({sidebar_reader_book:async id=>{fetched++;return {id,current_chapter:3,current_offset:123,title:'Fresh'};}});
    h.test.set({audio:{books:[{id:9,player_running:true,linked_light_novel:{id:4}}]}});
    h.context.openLightNovel=async(id,options)=>{opened={id,book:await options.bookPromise};};
    await h.test.audioClick('read',{});
    assert.equal(fetched,1);assert.equal(opened.book.current_chapter,3);assert.equal(opened.book.current_offset,123);
  },
  async sidebar_seek() {
    // A timeline rejection reaches the UI instead of becoming unhandled.
    const messages=[],unhandled=[];
    const h=sidebar({audiobook_seek_to:async()=>{throw Error('seek offline');}});
    h.context.toast=value=>messages.push(value);h.test.set({audio:{books:[{id:9,player_running:true}]}});
    const handler=error=>unhandled.push(error);process.on('unhandledRejection',handler);
    for(const callback of h.listeners.get('change')||[])callback({target:{matches:s=>s==='[data-sc-audio-timeline]',value:'25'}});
    await new Promise(resolve=>setImmediate(resolve));process.removeListener('unhandledRejection',handler);
    assert.equal(unhandled.length,0);assert.deepEqual(messages,['seek offline']);
  },
  async hydration() {
    // Repainting one completed dictionary card must not re-fetch every second.
    const h=harness();let words=0,capabilities=0;
    h.context.pywebview={api:{study_provider_capabilities:async()=>{capabilities++;return {account_key:'jiten:A'};},
      study_word:async card=>{words++;return {...card,dictionary_complete:true,primary_reading:'そせい',all_readings:['そせい','しゅせい']};}}};
    load(h,'jiten_words.js');
    const card={wordId:2,readingIndex:0,wordTextPlain:'素性'},parent=element(),line=element(),root=element();
    parent.querySelector=()=>line;root.querySelector=()=>parent;
    const hydrate=()=>h.context.PudgeJitenWords.hydrateReadings(card,root,'.answer','readings',()=>card);
    hydrate();await flush();
    for(let i=0;i<5;i++){hydrate();await flush();}
    for(const timer of h.timers)timer();await flush();
    assert.equal(words,1,'a completed card does not poll study_word');
    assert.equal(capabilities,2,'a repeated render does not poll account capabilities');
    assert.equal(line.textContent,'しゅせい');
    // An incomplete cold result still updates when its background fetch finishes.
    const cold=harness();let attempt=0;
    cold.context.pywebview={api:{study_provider_capabilities:async()=>({account_key:'jiten:B'}),
      study_word:async value=>++attempt===1?value:{...value,dictionary_complete:true,primary_reading:'そせい',all_readings:['そせい','すぞう']}}};
    load(cold,'jiten_words.js');
    cold.context.PudgeJitenWords.hydrateReadings(card,root,'.answer','readings',()=>card);await flush();
    cold.timers[0]();await flush();
    assert.equal(line.textContent,'すぞう');
  },
  async reader_inputs() {
    // Reader shortcuts leave textarea/contenteditable selection and typing alone.
    const h=manga(),reader=h.nodes.get('mangaReaderV2');reader.classList.add('open');
    for(const field of ['textarea','[contenteditable="true"]']) {
      let prevented=0;const target={closest:s=>s.includes(field)?target:null};
      for(const callback of h.listeners.get('keydown')||[])callback({target,key:'D',code:'KeyD',shiftKey:true,preventDefault(){prevented++;},stopImmediatePropagation(){}});
      assert.equal(prevented,0,'Shift+D belongs to the assistant input');
    }
    const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
    const start=html.indexOf("document.addEventListener('keydown',event=>{if(!$('lnReaderShell')");
    const selectAll=html.slice(start,html.indexOf('\nasync function exportActiveDebugContext',start));
    const c=h.context;c.$=id=>h.nodes.get(id);h.nodes.set('lnReaderShell',Object.assign(element(),{classList:classes('open')}));h.nodes.set('lnReader',element());
    let selections=0;c.document.createRange=()=>({selectNodeContents(){selections++;}});c.getSelection=()=>({removeAllRanges(){},addRange(){}});
    vm.runInContext(selectAll,c);
    const input={closest:s=>s.includes('textarea')?input:null};
    for(const callback of h.listeners.get('keydown')||[])callback({target:input,key:'a',code:'KeyA',metaKey:true,preventDefault(){},stopImmediatePropagation(){}});
    assert.equal(selections,0,'Cmd+A stays within the input');
  },
  async grammar_selector() {
    // Model-supplied point IDs with quotes/brackets still select the right point.
    const h=harness();h.context.PudgeReadingTools={llmAvailable:()=>true};load(h,'reading_assistant.js');
    const id='point"]odd',point=element(),seg={dataset:{points:id},closest:()=>box},box=element();
    point.dataset.grammarPoint=id;box.querySelectorAll=s=>s==='[data-grammar-point]'?[point]:[];
    box.querySelector=()=>{throw new SyntaxError('unescaped point id');};
    const root=element(),target={closest:s=>s==='#pudgeAssistant'?root:s==='.pudge-grammar-seg'?seg:null};
    for(const callback of h.listeners.get('click')||[])callback({target});
    assert.equal(box.dataset.activePoint,id);assert.equal(point.scrolls,1);
  },
  async assistant_ruby() {
    // A quick follow-up replaces the answer DOM while its Japanese parse awaits.
    const h=harness(),pending=deferred();let calls=0;
    h.context.PudgeReadingTools={llmAvailable:()=>true,study:{renderParsedParagraph:()=>'<p><ruby>猫<rt>ねこ</rt></ruby></p>'}};
    h.context.pywebview={api:{study_parse_text:()=>++calls===1?pending.promise:Promise.resolve({paragraphs:['猫'],settings:{study_backend:'jiten'}})}};
    load(h,'reading_assistant.js',s=>s.replace('  g.PudgeAssistant = {',`  g.assistantTest={enhanceMessage,setThread:value=>{thread=value;}};\n  g.PudgeAssistant = {`));
    function bubble() {
      const b=element(),parent={replaceChild(span,node){node.isConnected=false;b.innerHTML=span.innerHTML;}};
      b.innerHTML='猫';b.textNode={nodeValue:'猫',isConnected:true,parentElement:{closest:()=>null},parentNode:parent};return b;
    }
    const old=bubble(),current=bubble(),message={role:'assistant',content:'猫'},list=element(),root=element();
    list.querySelector=()=>current;root.querySelector=()=>list;h.nodes.set('pudgeAssistant',root);
    h.document.createTreeWalker=b=>{let sent=false;return {nextNode:()=>sent?null:(sent=true,b.textNode)};};
    h.context.assistantTest.setThread({messages:[message]});
    const task=h.context.assistantTest.enhanceMessage(message,old);
    old.isConnected=false;old.textNode.isConnected=false;
    // The next render sees the ongoing parse and cannot start a duplicate yet.
    await h.context.assistantTest.enhanceMessage(message,current);
    pending.resolve({paragraphs:['猫'],settings:{study_backend:'jiten'}});await task;await flush();
    assert.match(message.enhancedHtml||'',/<ruby>猫<rt>ねこ<\/rt><\/ruby>/);
    assert.match(current.innerHTML,/<rt>ねこ<\/rt>/,'furigana reaches the replacement bubble');
  },
};
(async()=>{assert(tests[scenario],`Unknown scenario ${scenario}`);await tests[scenario]();console.log(`${scenario}: PASS`);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
