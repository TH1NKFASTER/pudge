'use strict';
// Behavioural contract for the LN reader entry, Space transport, canonical
// highlight offset, LN context-menu predicates and stale chapter errors.
// Runs the real index.html functions in a Node VM with small DOM fakes.
// Usage: node tests/js/ln_entry_contract.cjs pudge/web
const timeout = setTimeout(() => { console.error('ln_entry_contract timed out'); process.exit(1); }, 10000);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = process.argv[2];
const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
const clockSource = fs.readFileSync(path.join(web, 'paired_audio_clock.js'), 'utf8');

// Top-level functions in index.html start at column 0; their bodies are
// indented and multi-line bodies end with a lone `}`.
function fn(name) {
  const lines = html.split('\n');
  const start = lines.findIndex(line => line.startsWith(`function ${name}(`) || line.startsWith(`async function ${name}(`));
  assert(start >= 0, `missing function ${name}`);
  const out = [lines[start]];
  if (/^\s*$/.test(lines[start + 1] || '') || /^[^\s}]/.test(lines[start + 1] || '')) return out.join('\n');
  for (let i = start + 1; i < lines.length; i++) {
    out.push(lines[i]);
    if (lines[i] === '}') break;
  }
  return out.join('\n');
}
const between = (a, b) => html.slice(html.indexOf(a), html.indexOf(b, html.indexOf(a)));
const deferred = () => { let resolve, reject; const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; }); return {promise, resolve, reject}; };
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

function element(id = '') {
  const classes = new Set();
  return {id, hidden:false, disabled:false, textContent:'', innerHTML:'', value:'', title:'', dataset:{}, options:[], children:[],
    classList:{add:(...x) => x.forEach(c => classes.add(c)), remove:(...x) => x.forEach(c => classes.delete(c)),
      contains:x => classes.has(x), toggle:(x, on) => { if (on === undefined ? !classes.has(x) : on) classes.add(x); else classes.delete(x); }},
    insertAdjacentHTML() {}, replaceChildren() {}, setAttribute() {}, querySelectorAll() { return this.children; }, closest() { return null; }};
}

const TRANSPORT = new Set(['audiobook_seek', 'audiobook_set_paused', 'audiobook_stop', 'audiobook_play', 'light_novel_play_paired',
  'light_novel_play_paired_at_offset', 'audiobook_play_from', 'audiobook_toggle']);

function readerHarness(api = {}) {
  const nodes = new Map(), calls = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id, element(id)); return nodes.get(id); };
  const tray = $('lnPairedTray');
  tray.children = ['lnPairedSeekBack', 'lnPairedSeekForward', 'lnPairedScrubberShell', 'lnPairedTime', 'lnPairedSpeed', 'lnPairedAutoScrollLabel'].map(id => $(id));
  const ui = {lang:'en', lnOpenRequestToken:0, lnChapterLoadToken:0, lnChapterCacheGeneration:0, lnChapterPayloadCache:new Map(), lnTokenMap:new Map(), lnState:{settings:{}}};
  const gate = [];
  const apiCalls = [];
  const apiImpl = Object.assign({
    light_novel_open: async id => ({id, title:`Book ${id}`, chapters:[], current_chapter:0, current_offset:0, paired_audio:null}),
    audiobook_review_context: async () => ({ok:true}),
    audiobook_reader_context: async () => ({ok:true}),
    light_novel_cancel_reader_background: async () => ({}),
  }, api);
  const proxy = new Proxy({}, {get:(_o, name) => {
    if (!(name in apiImpl)) return undefined;
    return (...args) => { apiCalls.push({name, args}); if (TRANSPORT.has(name)) throw new Error(`transport call ${name}`); return apiImpl[name](...args); };
  }});
  const context = {ui, $, console, performance:{now:() => 1000}, Promise, Number, String, Math, Date, Object, Array, Map, Set, JSON,
    escapeHtml:String, pywebview:{api:proxy}, requestAnimationFrame:cb => { cb(); return 1; },
    PudgeReviewGate:{require:async (...args) => { gate.push(args); return true; }}};
  context.window = context; context.globalThis = context;
  for (const name of ['installLnSelectionDiagnostics', 'cancelLnAutoBookmark', 'setPage', 'populateLnReaderAppearance', 'applyLnReaderSettings',
    'syncLnFinishButton', 'syncLnChapterPicker', 'syncLnFuriganaReadingSetting', 'cancelLnPairedInterpolation', 'closeLnFind',
    'closeLnChapterPicker', 'stopLnParsePoll', 'hideLnTranslation'])
    context[name] = (...args) => calls.push({name, args});
  context.persistReadTogetherSession = value => { ui.lnReadTogether = value || null; };
  context.storedReadTogetherSession = () => null;
  context.ensureLnPairedControls = () => $('lnPairedAudio');
  context.stopLnPairedPoll = () => { calls.push({name:'stopLnPairedPoll'}); ui.lnPairedState = null; ui.lnPairedTransportClockPosition = null; };
  context.pollLnPaired = () => calls.push({name:'poll'});
  context.loadLightNovelChapter = async (...args) => { calls.push({name:'load', args}); return true; };
  context.applyLnPairedPosition = async (...args) => calls.push({name:'apply', args});
  context.PudgeSidebarCompanion = {acceptPairedState:(...args) => calls.push({name:'sidebarAccept', args})};
  vm.createContext(context);
  vm.runInContext(clockSource, context);
  const source = [between('async function openLightNovel(', 'async function loadLightNovelChapter('),
    fn('syncLnPairedTray'), fn('lnPairedSnapshotActive'), fn('lnPairedPreparationText'), fn('lnPairedClock'),
    fn('lnPairedTransportClockDesired'), fn('lnPairedTransportClockNow'), fn('lnPairedTransportClockReset'),
    fn('lnSpaceOwnedTarget'), fn('lnPairedSpaceTransportEvent')].join('\n');
  const close = between("    if(target.id==='lnReaderClose')", '    if(target.dataset.lnAudioSeek');
  vm.runInContext(`${source}\nglobalThis.closeLnReader=async()=>{const target={id:'lnReaderClose'};${close}}`, context);
  const controlsVisible = () => !$('lnPairedTray').hidden && !$('lnPairedTime').hidden && !$('lnPairedSeekBack').hidden;
  const transportCalls = () => apiCalls.filter(row => TRANSPORT.has(row.name));
  return {context, ui, $, calls, gate, apiCalls, controlsVisible, transportCalls};
}

const nestedLink = (book, extra = {}) => ({ln_book_id:178, alignment_mode:'stt', book:{id:281, title:'Audio', duration:900, position:120, speed:1, ...book}, ...extra});

const scenarios = {
  async nested_playing() {
    // Real backend link shape: transport flags only exist on paired_audio.book.
    const h = readerHarness({light_novel_open:async () => ({id:178, title:'LN', chapters:[], current_chapter:3, current_offset:.2,
      paired_audio:nestedLink({playing:true, player_running:true, paused:false})})});
    await h.context.openLightNovel(178);
    assert.equal(h.gate.length, 0, 'audio-first entry does not wedge the review gate in front of a live session');
    assert.equal(h.ui.lnPairedExpanded, true);
    assert.equal(h.ui.lnPairedStarted, true);
    assert.equal(h.controlsVisible(), true, 'controls visible on ordinary Read');
    assert.equal(h.$('lnPairedAudio').textContent, 'Pause');
    assert.deepEqual(h.apiCalls.find(row => row.name === 'audiobook_review_context').args, [281, 'ln'], 'consumer switches to ln with the real id');
    const reader = h.apiCalls.find(row => row.name === 'audiobook_reader_context').args[0];
    assert.deepEqual({...reader, generation:undefined}, {open:true, ln_book_id:178, audiobook_id:281, generation:undefined});
    assert(reader.generation > 0);
    assert.deepEqual(h.transportCalls(), [], 'plain open: no seek/play/stop');
    assert(h.calls.some(row => row.name === 'poll'));
    assert.deepEqual(JSON.parse(JSON.stringify(h.calls.find(row => row.name === 'load').args)), [3, .2], 'reader opens at its bookmark; audio-follow comes from polls');
    assert(!h.calls.some(row => row.name === 'sidebarAccept'), 'link metadata is not pushed to the sidebar as a transport snapshot');
    assert.equal(h.context.lnPairedSpaceTransportEvent({code:'Space', key:' ', target:{closest:() => null}}), true);
    // A later runtime poll keeps the controls and Space available.
    h.context.syncLnPairedTray({audiobook_id:281, playing:true, player_running:true, position:121, alignment:{ready:false, status:'aligning'}});
    assert.equal(h.controlsVisible(), true, 'basic controls do not wait for alignment');
    assert.equal(h.$('lnPairedAutoScrollLabel').hidden, true, 'alignment-only control stays hidden');
    assert.equal(h.$('lnPairedAudio').disabled, false);
  },
  async paused_player() {
    const h = readerHarness({light_novel_open:async () => ({id:178, chapters:[], current_chapter:0,
      paired_audio:nestedLink({playing:false, player_running:true, paused:true})})});
    await h.context.openLightNovel(178);
    assert.equal(h.gate.length, 0);
    assert.equal(h.controlsVisible(), true);
    assert.equal(h.ui.lnPairedStarted, true);
    assert.equal(h.$('lnPairedAudio').textContent, 'Resume');
    assert.equal(h.ui.lnPairedState.position, 120, 'paused position is not advanced');
    assert.equal(h.context.lnPairedTransportClockNow(h.ui.lnPairedState), 120);
    assert.equal(h.context.lnPairedSpaceTransportEvent({code:'Space', key:' ', target:{closest:() => null}}), true, 'Space can resume a paused session');
    assert.deepEqual(h.transportCalls(), []);
  },
  async linked_idle() {
    const h = readerHarness({light_novel_open:async () => ({id:178, chapters:[], current_chapter:0,
      paired_audio:nestedLink({playing:false, player_running:false, paused:false})})});
    await h.context.openLightNovel(178);
    assert.equal(h.gate.length, 1, 'ordinary reading keeps the entry gate');
    assert.equal(h.ui.lnPairedExpanded, false);
    assert.equal(h.ui.lnPairedStarted, false);
    assert.equal(h.$('lnPairedTray').hidden, false, 'Listen button remains available');
    assert.equal(h.$('lnPairedTime').hidden, true, 'transport controls stay collapsed');
    assert.equal(h.context.lnPairedSpaceTransportEvent({code:'Space', key:' ', target:{closest:() => null}}), false, 'idle link: Space scrolls');
    assert.deepEqual(h.transportCalls(), [], 'no new playback session');
    assert.deepEqual(h.apiCalls.find(row => row.name === 'audiobook_review_context').args, [281, 'ln']);
  },
  async unlinked() {
    const h = readerHarness();
    await h.context.openLightNovel(5);
    assert.equal(h.gate.length, 1);
    assert.equal(h.$('lnPairedTray').hidden, true);
    assert.equal(h.ui.lnPairedState, null);
    assert(!h.apiCalls.some(row => row.name === 'audiobook_review_context'));
    assert.equal(h.context.lnPairedSpaceTransportEvent({code:'Space', key:' ', target:{closest:() => null}}), false);
  },
  async explicit_read_together_idle() {
    const h = readerHarness({light_novel_open:async () => ({id:178, chapters:[], current_chapter:0, paired_audio:nestedLink({})})});
    await h.context.openLightNovel(178, {readTogether:true, audiobookId:281});
    assert.equal(h.ui.lnPairedExpanded, true, 'Read and listen opens transport UI');
    assert.equal(h.ui.lnPairedStarted, false, 'but does not invent a running session');
    assert.equal(h.ui.lnReadTogether.returnPage, 'audiobooks');
  },
  async sidebar_handoff_validation() {
    // A handoff snapshot for another audiobook is ignored.
    const h = readerHarness({sidebar_reader_book:async () => ({})});
    await h.context.openLightNovel(7, {readTogether:true, audiobookId:5, bookPromise:Promise.resolve({id:7, chapters:[], paired_audio:nestedLink({}, {}), current_chapter:0}),
      audioBook:{id:5}, pairedState:{audiobook_id:5, playing:true, player_running:true, position:3}});
    assert.equal(h.ui.lnBook.paired_audio.book.id, 281, 'sidebar metadata never replaces the backend link to a different audiobook');
    assert(!h.calls.some(row => row.name === 'apply'), 'mismatched snapshot is not applied');
    assert.equal(h.ui.lnPairedStarted, false);
    // Matching handoff without a backend link (sidebar_reader_book shape).
    const g = readerHarness();
    await g.context.openLightNovel(7, {readTogether:true, audiobookId:5, bookPromise:Promise.resolve({id:7, chapters:[], current_chapter:0}),
      audioBook:{id:5, title:'A'}, pairedState:{audiobook_id:5, ln_chapter_index:1, playing:false, player_running:true, paused:true, position:3}});
    assert.equal(g.ui.lnBook.paired_audio.book.id, 5);
    assert.equal(g.ui.lnPairedStarted, true);
    assert(g.calls.some(row => row.name === 'apply'));
    assert.deepEqual(g.apiCalls.find(row => row.name === 'audiobook_review_context').args, [5, 'ln']);
    assert.deepEqual(g.transportCalls(), []);
  },
  async open_race() {
    // A's late metadata/audio-context response must not replace B.
    const aBook = deferred(), aContext = deferred();
    const h = readerHarness({
      light_novel_open:id => id === 1 ? aBook.promise : Promise.resolve({id:2, title:'B', chapters:[], current_chapter:0, paired_audio:null}),
      audiobook_review_context:() => aContext.promise});
    const a = h.context.openLightNovel(1);
    await h.context.openLightNovel(2);
    aBook.resolve({id:1, chapters:[], current_chapter:0, paired_audio:nestedLink({playing:true, player_running:true})});
    await a;
    assert.equal(h.ui.lnBook.id, 2);
    // A reaches its audio-context await, then B wins.
    const h2 = readerHarness({
      light_novel_open:async id => ({id, chapters:[], current_chapter:0, paired_audio:id === 1 ? nestedLink({playing:true, player_running:true}) : null}),
      audiobook_review_context:() => aContext.promise});
    const a2 = h2.context.openLightNovel(1); await flush();
    await h2.context.openLightNovel(2);
    aContext.resolve({});
    await a2;
    assert.equal(h2.ui.lnBook.id, 2, 'latest selection wins after every await');
    assert.equal(h2.calls.filter(row => row.name === 'load').length, 1, 'stale entry does not load its chapter');
    const readerCtx = h2.apiCalls.filter(row => row.name === 'audiobook_reader_context').map(row => row.args[0]);
    assert.deepEqual(readerCtx.map(row => row.ln_book_id), [2], 'stale A never announces itself to the backend');
  },
  async close_during_entry() {
    const aContext = deferred();
    const h = readerHarness({light_novel_open:async id => ({id, chapters:[], current_chapter:0, paired_audio:nestedLink({playing:true, player_running:true})}),
      audiobook_review_context:(id, consumer) => consumer === 'ln' ? aContext.promise : Promise.resolve({})});
    const pending = h.context.openLightNovel(178); await flush();
    assert.equal(h.$('lnReaderShell').classList.contains('open'), true);
    await h.context.closeLnReader();
    aContext.resolve({});
    await pending;
    assert.equal(h.$('lnReaderShell').classList.contains('open'), false, 'late response does not reopen the reader');
    assert(!h.calls.some(row => row.name === 'load'), 'no chapter load after close');
    assert(!h.calls.some(row => row.name === 'poll'), 'no poll after close');
    const ctx = h.apiCalls.filter(row => row.name === 'audiobook_reader_context').map(row => row.args[0]);
    assert.deepEqual(ctx.map(row => row.open), [false], 'close reports open:false; stale entry stays silent');
    assert.equal(ctx[0].audiobook_id, 281);
    // Generations increase monotonically across entry/close.
    const g = readerHarness({light_novel_open:async id => ({id, chapters:[], current_chapter:0, paired_audio:nestedLink({playing:true, player_running:true})})});
    for (let i = 0; i < 3; i++) { await g.context.openLightNovel(178); await g.context.closeLnReader(); }
    const generations = g.apiCalls.filter(row => row.name === 'audiobook_reader_context').map(row => row.args[0].generation);
    assert.equal(generations.length, 6);
    assert(generations.every((value, index) => index === 0 || value > generations[index - 1]));
    assert.deepEqual(g.transportCalls(), [], 'repeated exit/return never seeks/plays/stops');
  },
  async space_keydown() {
    // Run the real keydown listener: priorities, filters and one toggle per press.
    const start = html.indexOf("document.addEventListener('keydown',event=>{\n  if(event.target?.id==='lnFindInput')");
    assert(start >= 0);
    const listener = html.slice(start, html.indexOf('\n},true);', start) + 9);
    const make = ({state, reviewGate = false, study = false, capture = false} = {}) => {
      const h = readerHarness();
      const c = h.context, toggles = [];
      h.$('lnReaderShell').classList.add('open');
      h.ui.lnBook = {id:178, paired_audio:nestedLink({})};
      h.ui.lnPairedState = state;
      h.ui.shortcutCaptureTarget = capture ? element('rec') : null;
      c.toggleLnPairedPlayback = async () => toggles.push(1);
      c.seekLnPairedRelative = async () => {};
      c.PudgeReviewGate = {handleKeydown:() => reviewGate};
      c.PudgeReadingTools = {study:{handleReviewKeydown:() => study}};
      c.lnStudyMode = () => 'click'; c.lnStudyHoveredWord = null; c.lnStudyTriggerEnabled = () => false;
      c.editableShortcutTarget = target => !!target?.closest?.('input,textarea,select,[contenteditable="true"]');
      c.capturedShortcut = () => 'Space'; c.setShortcutRecorder = () => {};
      let handler; c.document = {addEventListener:(_type, fn) => { handler = fn; }};
      vm.runInContext(listener, c);
      const press = (extra = {}) => {
        let prevented = 0;
        const event = {code:'Space', key:' ', target:{closest:() => null}, repeat:false, defaultPrevented:false, isComposing:false,
          metaKey:false, ctrlKey:false, altKey:false, shiftKey:false, preventDefault() { prevented++; }, stopPropagation() {}, stopImmediatePropagation() {}, ...extra};
        handler(event);
        return {prevented, toggles:toggles.length};
      };
      return {press, toggles, h};
    };
    const playing = {audiobook_id:281, playing:true, player_running:true, alignment:{ready:false, status:'aligning'}};
    const paused = {audiobook_id:281, playing:false, paused:true, player_running:true};
    const idle = {audiobook_id:281, playing:false, player_running:false};
    // Tray collapsed + alignment not ready: Space still controls a live player.
    let t = make({state:playing}); t.h.ui.lnPairedExpanded = false;
    assert.deepEqual(t.press(), {prevented:1, toggles:1}, 'word click → Space toggles');
    assert.deepEqual(t.press({repeat:true}), {prevented:1, toggles:1}, 'held Space: scroll prevented, no extra toggle');
    t = make({state:paused}); assert.deepEqual(t.press(), {prevented:1, toggles:1}, 'paused session resumes');
    t = make({state:idle}); assert.deepEqual(t.press(), {prevented:0, toggles:0}, 'linked idle: page keeps Space');
    t = make({state:{...playing, audiobook_id:999}}); assert.deepEqual(t.press(), {prevented:0, toggles:0}, 'snapshot of another audiobook is ignored');
    t = make({state:playing, study:true}); assert.deepEqual(t.press(), {prevented:0, toggles:0}, 'focused grade + Space grades, never pauses');
    t = make({state:playing, reviewGate:true}); assert.deepEqual(t.press(), {prevented:0, toggles:0}, 'review gate keeps priority');
    t = make({state:playing, capture:true}); assert.equal(t.press().toggles, 0, 'shortcut capture keeps priority');
    for (const [label, extra] of [['defaultPrevented', {defaultPrevented:true}], ['composition', {isComposing:true}], ['meta', {metaKey:true}],
      ['shift', {shiftKey:true}], ['ctrl', {ctrlKey:true}], ['alt', {altKey:true}]]) {
      t = make({state:playing}); assert.deepEqual(t.press(extra), {prevented:0, toggles:0}, label);
    }
    for (const selector of ['input', 'textarea', 'select', 'button', 'a[href]', '[contenteditable]:not([contenteditable="false"])', '[role="button"]']) {
      t = make({state:playing});
      const target = {closest:query => query.split(',').includes(selector) ? target : null};
      assert.deepEqual(t.press({target}), {prevented:0, toggles:0}, `Space owned by ${selector}`);
    }
    t = make({state:playing}); assert.deepEqual(t.press({target:{isContentEditable:true, closest:() => null}}), {prevented:0, toggles:0});
    t = make({state:playing}); t.h.$('lnReaderShell').classList.remove('open');
    assert.deepEqual(t.press(), {prevented:0, toggles:0}, 'reader closed');
  },
  async offset_parity() {
    // LN and sidebar use the same canonical offset (1e-6), independent of DOM.
    const c = {console, performance:{now:() => 0}};
    c.globalThis = c; c.window = c;
    vm.createContext(c);
    vm.runInContext(clockSource, c);
    c.ui = {lnPairedState:null};
    vm.runInContext(`${fn('lnPairedAnchorPath')}\n${fn('lnPairedOffsetAtTime')}`, c);
    const sidebar = (state, time) => c.PudgePairedAudioClock.offsetAtTime(state, time);
    const states = [
      {anchor_window:{activity_clock:true, path:[{time:0, offset:0}, {time:8, offset:8}], activity:[{start:0, end:2}, {start:6, end:8}]}},
      {anchor_window:{activity_clock:true, path:[{time:0, offset:0}, {time:4, offset:3, wall_clock_from_previous:true}, {time:8, offset:9}], activity:[{start:5, end:6}]}},
      {anchor_window:{path:[{time:1, offset:0}, {time:1, offset:4}, {time:1, offset:4}, {time:3, offset:10}]}},
      {anchor_window:{left_time:2770, left_offset:11770, right_time:2780, right_offset:11800}},
      {anchor_window:{points:[{time:10, offset:5}, {time:12, offset:5}, {time:13, offset:9}]}},
      {anchor_window:null, chapter_char_offset_exact:42.5},
      {anchor_window:{path:[{time:0, offset:3}, {time:2, offset:1}]}, chapter_char_offset:7},
    ];
    for (const state of states) for (let time = -1; time <= 2790; time += time < 20 ? .037 : (time < 2760 ? 500 : .13)) {
      const a = c.lnPairedOffsetAtTime(state, time), b = sidebar(state, time);
      if (Number.isNaN(b)) assert(Number.isNaN(a)); else assert(Math.abs(a - b) <= 1e-6, `parity ${time}: ${a} vs ${b}`);
    }
    // Pre-fix LN re-timed by DOM mora weights; those helpers must be gone.
    for (const removed of ['lnPairedWeightMap', 'lnPairedWeightedPosition', 'lnPairedSourcePosition', 'lnPairedSmoothOffset'])
      assert(!html.includes(`function ${removed}(`), `${removed} no longer participates in timing`);
  },
  async no_forward_clamp() {
    // Two consecutive frames: rendered offset follows the canonical offset.
    const nodes = new Map(), $ = id => { if (!nodes.has(id)) nodes.set(id, element(id)); return nodes.get(id); };
    const para = element('p'); para.dataset = {lnParagraph:'0', lnAudioStart:'0', lnAudioEnd:'100'};
    const reader = $('lnReader'); reader.querySelectorAll = selector => selector.includes('data-ln-paragraph') ? [para] : [];
    let now = 1000;
    const traces = [];
    const c = {ui:{lnChapter:{chapter_index:0}, lnChapterLoadToken:1}, $, console, performance:{now:() => now}, Number, Math, String};
    c.globalThis = c;
    c.lnPairedTrace = (event, _state, extra) => traces.push({event, ...extra});
    for (const name of ['lnPairedResetWordProgress', 'lingerLnPairedFurigana', 'resetLnPairedFuriganaExpiry', 'scheduleLnPairedFuriganaExpiry', 'lnPairedSurface', 'lnPairedWordWeights'])
      c[name] = () => {};
    c.lnPairedManualNavigationActive = () => false;
    vm.createContext(c);
    vm.runInContext([fn('lnPairedDomIndex'), fn('lnPairedFindRange'), fn('renderLnPairedPosition')].join('\n'), c);
    const state = {playing:true, alignment_mode:'stt', ln_chapter_index:0, position:10};
    c.renderLnPairedPosition(state, 10, {speechActive:true});
    now += 50;
    c.renderLnPairedPosition({...state, position:10.05}, 13.5, {speechActive:true});
    assert.equal(c.ui.lnPairedLastDisplayOffset, 13.5, 'no LN-only forward clamp (old cap was ≤.65 chars/frame)');
    assert(!traces.some(row => row.event === 'clamp_forward'));
    // Manual scrolling still defers painting (presentation), not the source.
    $('lnReaderScroll').classList.add('ln-reader-scrolling');
    c.renderLnPairedPosition({...state, position:10.3}, 14, {speechActive:true});
    assert.equal(c.ui.lnPairedDeferredRender.offset, 14);
    assert.equal(c.ui.lnPairedLastDisplayOffset, 13.5);
  },
  async menu_predicates() {
    const nodes = new Map(), $ = id => { if (!nodes.has(id)) nodes.set(id, element(id)); return nodes.get(id); };
    const c = {ui:{lang:'en', lnSelection:new Set(), lnIrodoriStarting:new Set(), lnState:{settings:{}}}, $, positionContextMenu() {}, Number, String};
    vm.createContext(c);
    vm.runInContext([fn('lnValidAniListId'), fn('lnPairedAudiobookId'), fn('hasResettableLnPosition'), fn('showLnBookMenu'), fn('showLnSeriesMenu')].join('\n'), c);
    const actions = () => [...$('contextMenu').innerHTML.matchAll(/data-ln-context-action="([^"]+)"/g)].map(m => m[1]);
    const book = extra => ({id:1, title:'B', current_chapter:0, current_offset:0, bookmark_source:null, bookmark_updated_at:null, ...extra});
    const rows = [
      // [label, book, jiten, nyaa, reset]
      ['anilist positive', book({anilist_id:123}), false, true, false],
      ['anilist null', book({anilist_id:null}), true, true, false],
      ['anilist zero', book({anilist_id:0}), true, true, false],
      ['anilist "0"', book({anilist_id:'0'}), true, true, false],
      ['nested paired audio', book({paired_audio:{book:{id:281}}}), true, false, false],
      ['link without book id', book({paired_audio:{book:{}}}), true, true, false],
      ['nonzero offset', book({current_offset:.3}), true, true, true],
      ['later chapter', book({current_chapter:2}), true, true, true],
      ['bookmark at zero', book({bookmark_source:'reader', bookmark_updated_at:'2026-10-01'}), true, true, true],
      ['bookmark timestamp only', book({bookmark_updated_at:'2026-10-01'}), true, true, true],
      ['finished only', book({finished:true, reading_progress:1, updated_at:'2026-10-01'}), true, true, false],
      ['after reset, finished kept', book({finished:true, reading_progress:100, current_chapter:0, current_offset:0}), true, true, false],
    ];
    for (const [label, row, jiten, nyaa, reset] of rows) {
      c.showLnBookMenu(row, 0, 0);
      const list = actions();
      assert.equal(list.includes('jiten-search'), jiten, `${label}: jiten`);
      assert.equal(list.includes('find-audiobook-nyaa'), nyaa, `${label}: nyaa`);
      assert.equal(list.includes('reset-position'), reset, `${label}: reset`);
      assert(list.includes('finish') !== list.includes('unfinish'), `${label}: finished action unchanged`);
    }
    c.showLnBookMenu(book({paired_audio:{book:{id:281}}}), 0, 0);
    assert.match($('contextMenu').innerHTML, /data-ln-context-action="read-together">Read and listen</);
    c.ui.lang = 'ru';
    c.showLnBookMenu(book({paired_audio:{book:{id:281}}}), 0, 0);
    assert.match($('contextMenu').innerHTML, /data-ln-context-action="read-together">Читать и слушать</);
    assert(!/Read together|Читать вместе/.test($('contextMenu').innerHTML));
    c.ui.lang = 'en';
    c.showLnSeriesMenu([book({id:1, anilist_id:0}), book({id:2, anilist_id:55})], 0, 0);
    assert(!actions().includes('jiten-search'), 'series resolved by a volume AniList link');
    assert.equal(c.ui.lnContextBook.id, 2, 'series context uses the volume with the valid link');
    c.showLnSeriesMenu([book({id:1, anilist_id:0}), book({id:2, anilist_id:null})], 0, 0);
    assert(actions().includes('jiten-search'));
  },
  async stale_chapter_error() {
    // COMPLIANCE X02: an old failing chapter request cannot overwrite a newer one.
    const nodes = new Map(), $ = id => { if (!nodes.has(id)) nodes.set(id, element(id)); return nodes.get(id); };
    const requests = new Map();
    const c = {ui:{lnBook:{id:7}, lnChapterLoadToken:0, lnApprovedInitial:null}, $, escapeHtml:String, Number, String,
      PudgeReviewGate:{require:async () => true}};
    c.window = c;
    for (const name of ['cancelLnAutoBookmark', 'stopLnParsePoll', 'syncLnChapterPicker', 'restoreLnReaderOffset', 'scheduleLnAutoBookmark', 'pollLnParse']) c[name] = () => {};
    c.prefetchLnChapter = (_book, index) => { if (!requests.has(index)) requests.set(index, deferred()); return requests.get(index).promise; };
    c.renderLnChapterPayload = payload => { $('lnReader').innerHTML = `chapter ${payload.chapter_index}`; };
    vm.createContext(c);
    vm.runInContext(fn('loadLightNovelChapter'), c);
    const old = c.loadLightNovelChapter(0, 0, {audioFollow:true});
    const fresh = c.loadLightNovelChapter(1, 0, {audioFollow:true});
    requests.get(1).resolve({chapter_index:1});
    assert.equal(await fresh, true);
    requests.get(0).reject(new Error('old chapter failed'));
    assert.equal(await old, false, 'stale failure resolves quietly');
    assert.equal($('lnReader').innerHTML, 'chapter 1');
    // The current request's failure is still shown and rethrown.
    const failing = c.loadLightNovelChapter(3, 0, {audioFollow:true});
    requests.get(3).reject(new Error('boom'));
    await assert.rejects(failing, /boom/);
    assert.match($('lnReader').innerHTML, /boom/);
  },
};

(async () => {
  const only = process.argv[3];
  for (const [name, run] of Object.entries(scenarios)) {
    if (only && only !== name) continue;
    await run();
    console.log(`${name}: PASS`);
  }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(timeout));
