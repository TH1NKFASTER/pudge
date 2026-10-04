'use strict';
// Reading assistant: a ChatGPT-like side panel on the right. "Explain grammar"
// opens it with the structured grammar analysis as the first assistant
// message; the learner can then ask follow-up questions about the sentence.
// The panel can be collapsed to a tab on the right edge and expanded again.
(() => {
  const g = globalThis;
  const API = () => g.window?.pywebview?.api || g.pywebview?.api;
  // eslint-disable-next-line no-undef
  const uiRef = () => (typeof ui !== 'undefined' ? ui : g.ui);
  const ru = () => (uiRef()?.lang || document.documentElement.lang) === 'ru';
  const available = () => Boolean(g.PudgeReadingTools?.llmAvailable?.());
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  // thread: one continuous chat (up to MAX_MESSAGES, scrollable). Each
  // "Explain grammar" starts a new topic inside it; the model only receives
  // the current topic's messages. messages: [{role, content, html, topic}]
  const MAX_MESSAGES = 100;
  const STORE_KEY = 'pudge.assistant.v1';
  const GEOMETRY_KEY = 'pudge.assistant.geometry.v1';
  const DEFAULT_INSET = 12;
  const DEFAULT_WIDTH = 420;
  const MIN_WIDTH = 300;
  const MIN_HEIGHT = 260;
  let thread = loadThread();
  let pending = false;
  let generation = 0;
  let collapsed = false;

  function loadThread() {
    try {
      const raw = g.PudgeUiStorage ? g.PudgeUiStorage.get(STORE_KEY) : JSON.parse(g.localStorage?.getItem?.(STORE_KEY) || 'null');
      if (raw && Array.isArray(raw.messages)) return {topic: Number(raw.topic || 0), sentence: String(raw.sentence || ''), context: String(raw.context || ''), grammar: raw.grammar || null, messages: raw.messages.slice(-MAX_MESSAGES)};
    } catch (_) { /* storage unavailable: start empty */ }
    return null;
  }

  function saveThread() {
    try {
      if (!thread) { if(g.PudgeUiStorage)g.PudgeUiStorage.set(STORE_KEY,null);else g.localStorage?.removeItem?.(STORE_KEY); return; }
      const messages = thread.messages.slice(-MAX_MESSAGES).map(({role, content, html, topic, error, modelContent}) => ({role, content, html, topic, error, modelContent}));
      if(g.PudgeUiStorage)g.PudgeUiStorage.set(STORE_KEY,{...thread,messages});else g.localStorage?.setItem?.(STORE_KEY, JSON.stringify({...thread, messages}));
    } catch (_) { /* quota/private mode: keep in memory only */ }
  }

  function push(message) {
    thread.messages.push({...message, topic: thread.topic});
    if (thread.messages.length > MAX_MESSAGES) thread.messages.splice(0, thread.messages.length - MAX_MESSAGES);
  }

  function modelName() {
    const settings = uiRef()?.state?.settings || {};
    return String(settings.llm_model || '').trim() || (ru() ? 'Помощник' : 'Assistant');
  }

  // Minimal, safe Markdown: everything is escaped first.
  function markdownHtml(text) {
    const blocks = [];
    const source = String(text || '').replace(/\r\n?/g, '\n');
    const fenced = source.split(/```/);
    fenced.forEach((chunk, index) => {
      if (index % 2 === 1) {
        // ```lang\n…``` → a code block with its language and a copy button.
        const match = chunk.match(/^([\w+#.-]{0,20})[ \t]*\n/);
        const lang = match ? match[1] : '';
        const code = (match ? chunk.slice(match[0].length) : chunk).replace(/\n$/, '');
        blocks.push(
          `<div class="pa-code"><div class="pa-code-head"><span>${esc(lang || 'code')}</span>` +
          `<button type="button" class="pa-code-copy" data-pa-copy-code>${ru() ? 'Копировать' : 'Copy'}</button></div>` +
          `<pre><code${lang ? ` data-lang="${esc(lang)}"` : ''}>${g.PudgeAssistantSyntax?.highlight?.(code, lang) ?? esc(code)}</code></pre></div>`,
        );
        return;
      }
      for (const para of chunk.split(/\n{2,}/)) {
        const lines = para.split('\n').filter(line => line.trim());
        if (!lines.length) continue;
        const inline = line => esc(line)
          .replace(/`([^`]+)`/g, '<code>$1</code>')
          .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
          .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
        if (lines.every(line => /^\s*[-*•]\s+/.test(line))) {
          blocks.push(`<ul>${lines.map(line => `<li>${inline(line.replace(/^\s*[-*•]\s+/, ''))}</li>`).join('')}</ul>`);
        } else if (lines.every(line => /^\s*\d+[.)]\s+/.test(line))) {
          blocks.push(`<ol>${lines.map(line => `<li>${inline(line.replace(/^\s*\d+[.)]\s+/, ''))}</li>`).join('')}</ol>`);
        } else if (/^#{1,4}\s+/.test(lines[0]) && lines.length === 1) {
          blocks.push(`<p><strong>${inline(lines[0].replace(/^#{1,4}\s+/, ''))}</strong></p>`);
        } else {
          blocks.push(`<p>${lines.map(inline).join('<br>')}</p>`);
        }
      }
    });
    return blocks.join('');
  }

  function grammarSummary(result) {
    const points = Array.isArray(result?.points) ? result.points : [];
    if (!points.length) return result?.translation ? `Translation: ${result.translation}. No notable grammar points.` : 'No notable grammar points.';
    return [
      result?.translation ? `Translation: ${result.translation}` : '',
      ...points.map(point => `- ${point.pattern}${point.meaning_in_context ? ` — ${point.meaning_in_context}` : ''}`),
    ].filter(Boolean).join('\n');
  }

  function panel() {
    let root = document.getElementById('pudgeAssistant');
    if (root) return root;
    root = document.createElement('aside');
    root.id = 'pudgeAssistant';
    root.className = 'pudge-assistant';
    root.setAttribute('role', 'complementary');
    root.hidden = true;
    root.innerHTML = `
      <header class="pa-head">
        <button type="button" class="pa-avatar" data-pa-reset-geometry aria-label="Reset assistant position">✦</button>
        <div class="pa-window-controls">
          <button type="button" class="pa-window-button pa-window-close" data-pa-close><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3 9 9M9 3 3 9"/></svg></button>
          <button type="button" class="pa-window-button pa-window-collapse" data-pa-collapse><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6h8"/></svg></button>
        </div>
        <button type="button" class="pa-icon pa-clear" data-pa-new><svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 14h8l1-14M10 10v7M14 10v7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg></button>
        <div class="pa-title"><span data-pa-title></span></div>
      </header>
      <div class="pa-messages" data-pa-messages></div>
      <form class="pa-composer" data-pa-form>
        <textarea rows="1" data-pa-input></textarea>
        <button type="submit" class="pa-send" data-pa-send aria-label=""><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
      </form>`;
    for (const edge of ['n', 'e', 's', 'w', 'ne', 'nw', 'se', 'sw']) {
      const resize = document.createElement('div');
      resize.className = `pa-resize-handle pa-resize-${edge}`;
      resize.dataset.paResize = edge;
      resize.setAttribute('aria-label', ru() ? 'Изменить размер' : 'Resize assistant');
      root.appendChild(resize);
    }
    document.body.appendChild(root);
    installGeometry(root);
    // On the textarea itself (target phase), so it runs before document-level
    // bubbling shortcut handlers: typing never reaches reader/review keys.
    root.querySelector('[data-pa-input]')?.addEventListener('keydown', event => {
      if (event.key !== 'Escape') event.stopPropagation();
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        const form = event.target.form;
        if (form?.requestSubmit) form.requestSubmit();
        else form?.dispatchEvent?.(new Event('submit', {bubbles: true, cancelable: true}));
      }
    });
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.id = 'pudgeAssistantTab';
    tab.className = 'pudge-assistant-tab';
    tab.hidden = true;
    document.body.appendChild(tab);
    labels();
    return root;
  }

  function defaultGeometry() {
    const width = Math.max(1, Math.min(DEFAULT_WIDTH, window.innerWidth - DEFAULT_INSET * 2));
    const height = Math.max(1, window.innerHeight - DEFAULT_INSET * 2);
    return {x: Math.max(0, window.innerWidth - width - DEFAULT_INSET), y: Math.min(DEFAULT_INSET, Math.max(0, window.innerHeight - height)), width, height};
  }

  function clampGeometry(value) {
    const fallback = defaultGeometry();
    const maxWidth = Math.max(1, window.innerWidth - 8);
    const maxHeight = Math.max(1, window.innerHeight - 8);
    const width = Math.min(maxWidth, Math.max(Math.min(MIN_WIDTH, maxWidth), Number(value?.width) || fallback.width));
    const height = Math.min(maxHeight, Math.max(Math.min(MIN_HEIGHT, maxHeight), Number(value?.height) || fallback.height));
    const insetX = Math.min(4, Math.max(0, window.innerWidth - width));
    const insetY = Math.min(4, Math.max(0, window.innerHeight - height));
    const x = Math.max(insetX, Math.min(window.innerWidth - width - insetX, Number.isFinite(Number(value?.x)) && value?.x != null ? Number(value.x) : fallback.x));
    const y = Math.max(insetY, Math.min(window.innerHeight - height - insetY, Number.isFinite(Number(value?.y)) && value?.y != null ? Number(value.y) : fallback.y));
    return {x, y, width, height};
  }

  function loadGeometry() {
    try { return clampGeometry(g.PudgeUiStorage ? g.PudgeUiStorage.get(GEOMETRY_KEY) : JSON.parse(g.localStorage?.getItem?.(GEOMETRY_KEY) || 'null')); }
    catch (_) { return defaultGeometry(); }
  }

  function saveGeometry(root) {
    if (!root?.isConnected || root.hidden) return;
    const rect = root.getBoundingClientRect();
    try { const value=clampGeometry({x:rect.left,y:rect.top,width:rect.width,height:rect.height});if(g.PudgeUiStorage)g.PudgeUiStorage.set(GEOMETRY_KEY,value);else g.localStorage?.setItem?.(GEOMETRY_KEY, JSON.stringify(value)); }
    catch (_) {}
  }

  function applyGeometry(root, geometry = loadGeometry()) {
    if (!root) return;
    const next = clampGeometry(geometry);
    root.style.left = `${next.x}px`;
    root.style.top = `${next.y}px`;
    root.style.width = `${next.width}px`;
    root.style.height = `${next.height}px`;
    root.style.right = 'auto';
    root.style.bottom = 'auto';
  }

  function resetGeometry() {
    const root = panel();
    const next = defaultGeometry();
    applyGeometry(root, next);
    try { if(g.PudgeUiStorage)g.PudgeUiStorage.set(GEOMETRY_KEY,next);else g.localStorage?.setItem?.(GEOMETRY_KEY, JSON.stringify(next)); } catch (_) {}
    return next;
  }

  function installGeometry(root) {
    applyGeometry(root);
    const head = root.querySelector('.pa-head');
    if (!head) return;
    if (head.dataset.paGeometryInstalled === '1') return;
    head.dataset.paGeometryInstalled = '1';
    let drag = null;
    head.addEventListener('pointerdown', event => {
      if (event.button !== 0 || event.target.closest('button,input,textarea,select,a')) return;
      const rect = root.getBoundingClientRect();
      drag = {id:event.pointerId, dx:event.clientX-rect.left, dy:event.clientY-rect.top, width:rect.width, height:rect.height};
      head.setPointerCapture?.(event.pointerId);
      root.classList.add('pa-dragging');
      event.preventDefault();
    });
    head.addEventListener('pointermove', event => {
      if (!drag || event.pointerId !== drag.id) return;
      applyGeometry(root, {x:event.clientX-drag.dx,y:event.clientY-drag.dy,width:drag.width,height:drag.height});
    });
    const finish = event => {
      if (!drag || (event?.pointerId != null && event.pointerId !== drag.id)) return;
      drag = null; root.classList.remove('pa-dragging'); saveGeometry(root);
    };
    head.addEventListener('pointerup', finish);
    head.addEventListener('pointercancel', finish);
    let sizing = null;
    for (const resize of root.querySelectorAll('.pa-resize-handle')) {
    resize.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      const rect = root.getBoundingClientRect();
      sizing = {id:event.pointerId, x:rect.left, y:rect.top, width:rect.width, height:rect.height,
        startX:event.clientX, startY:event.clientY, edge:resize.dataset.paResize};
      resize.setPointerCapture?.(event.pointerId);
      event.preventDefault(); event.stopPropagation();
    });
    resize.addEventListener('pointermove', event => {
      if (!sizing || event.pointerId !== sizing.id) return;
      const {edge, x, y, width, height} = sizing;
      const dx = event.clientX - sizing.startX, dy = event.clientY - sizing.startY;
      const next = clampGeometry({x, y,
        width:width + (edge.includes('e') ? dx : edge.includes('w') ? -dx : 0),
        height:height + (edge.includes('s') ? dy : edge.includes('n') ? -dy : 0)});
      if (edge.includes('w')) next.x = x + width - next.width;
      if (edge.includes('n')) next.y = y + height - next.height;
      applyGeometry(root, next);
    });
    const finishSize = event => {
      if (!sizing || event.pointerId !== sizing.id) return;
      sizing = null; saveGeometry(root);
    };
    resize.addEventListener('pointerup', finishSize);
    resize.addEventListener('pointercancel', finishSize);
    }
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
      if (root.hidden || root.classList.contains('pa-dragging')) return;
      clearTimeout(root._paResizeSaveTimer);
      root._paResizeSaveTimer = setTimeout(() => saveGeometry(root), 180);
    }) : null;
    observer?.observe?.(root);
    window.addEventListener?.('resize', () => {
      if (root.hidden) return;
      const rect = root.getBoundingClientRect();
      applyGeometry(root, {x:rect.left,y:rect.top,width:rect.width,height:rect.height});
      saveGeometry(root);
    });
  }

  function labels() {
    const root = document.getElementById('pudgeAssistant');
    const tab = document.getElementById('pudgeAssistantTab');
    if (!root) return;
    root.querySelector('[data-pa-title]').textContent = modelName();
    root.querySelector('[data-pa-title]').title = modelName();
    const set = (sel, title, text) => { const node = root.querySelector(sel); if (!node) return; node.title = title; node.setAttribute('aria-label', title); if (text !== undefined) node.textContent = text; };
    set('[data-pa-new]', ru() ? 'Очистить чат' : 'Clear chat');
    set('[data-pa-reset-geometry]', ru() ? 'Вернуть размер и положение помощника' : 'Reset assistant size and position');
    set('[data-pa-collapse]', ru() ? 'Свернуть' : 'Collapse');
    set('[data-pa-close]', ru() ? 'Закрыть' : 'Close');
    set('[data-pa-send]', ru() ? 'Отправить' : 'Send');
    const input = root.querySelector('[data-pa-input]');
    if (input) input.placeholder = ru() ? 'Спросите о предложении…' : 'Ask about this sentence…';
    if (tab) { tab.textContent = ru() ? '✦ Помощник' : '✦ Assistant'; tab.title = ru() ? 'Развернуть помощника' : 'Expand assistant'; }
  }

  function messageHtml(message, index) {
    const body = message.enhancedHtml || message.html || markdownHtml(message.content);
    const divider = index > 0 && message.role === 'user' && message.topic !== thread.messages[index - 1]?.topic
      ? '<div class="pa-divider" aria-hidden="true"></div>' : '';
    const copy = message.role === 'assistant' && !message.error && message.content
      ? `<button type="button" class="pa-copy" data-pa-copy title="${ru() ? 'Копировать ответ' : 'Copy answer'}" aria-label="${ru() ? 'Копировать ответ' : 'Copy answer'}">⧉</button>` : '';
    return `${divider}<div class="pa-msg pa-${message.role}${message.error ? ' pa-error' : ''}" data-pa-index="${index}"><div class="pa-bubble">${body}</div>${copy}</div>`;
  }

  // Furigana + Jiten cards in the chat: Japanese text nodes of assistant
  // answers are parsed like reader text (one parse call per message) and
  // rendered as study tokens. The annotated sentence of a grammar answer stays
  // as is: its segments select grammar points.
  const JAPANESE = /[\u3040-\u30ff\u3400-\u9fff々〆ヵヶ]/;
  async function enhanceMessage(message, bubble) {
    if (!bubble || message.error || message.role !== 'assistant' || message.enhancedHtml || message.enhancing) return;
    const parse = API()?.study_parse_text;
    const renderer = g.PudgeReadingTools?.study?.renderParsedParagraph;
    if (typeof parse !== 'function' || typeof renderer !== 'function') return;
    const walker = document.createTreeWalker?.(bubble, 4 /* SHOW_TEXT */);
    if (!walker) return;
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!JAPANESE.test(node.nodeValue || '')) continue;
      if (node.parentElement?.closest?.('.pudge-grammar-sentence,code,pre,button,[data-pudge-study-token]')) continue;
      if (/\n/.test(node.nodeValue)) continue;
      nodes.push(node);
    }
    if (!nodes.length) { message.enhancedHtml = bubble.innerHTML; return; }
    message.enhancing = true;
    try {
      const texts = nodes.map(node => node.nodeValue.trim());
      const payload = await parse(texts.join('\n'));
      if (!bubble.isConnected) return;
      const paragraphs = payload?.paragraphs || [];
      if (paragraphs.length !== texts.length) return;
      const backend = String(payload?.settings?.study_backend || 'jiten');
      nodes.forEach((node, i) => {
        if (!node.isConnected || paragraphs[i] !== texts[i]) return;
        const html = String(renderer(payload, i, {backend, furigana: true, highlightOptimalWords: false})).replace(/^<p>|<\/p>$/g, '');
        const lead = node.nodeValue.match(/^\s*/)[0], tail = node.nodeValue.match(/\s*$/)[0];
        const span = document.createElement('span');
        span.className = 'pa-ja';
        span.innerHTML = `${esc(lead)}${html}${esc(tail)}`;
        node.parentNode.replaceChild(span, node);
      });
      message.enhancedHtml = bubble.innerHTML;
    } catch (_) {
      // Parsing is optional: the plain answer stays readable.
    } finally {
      message.enhancing = false;
      if (!bubble.isConnected && thread?.messages.includes(message)) {
        const index = thread.messages.indexOf(message);
        const current = panel().querySelector('[data-pa-messages]')?.querySelector(`[data-pa-index="${index}"] .pa-bubble`);
        if (current && current !== bubble) void enhanceMessage(message, current);
      }
    }
  }

  function enhanceVisible(list) {
    (thread?.messages || []).forEach((message, index) => {
      if (message.role !== 'assistant' || message.enhancedHtml || message.error) return;
      const bubble = list.querySelector?.(`[data-pa-index="${index}"] .pa-bubble`);
      void enhanceMessage(message, bubble);
    });
  }

  function render({scroll = true} = {}) {
    const root = panel();
    labels();
    const list = root.querySelector('[data-pa-messages]');
    const messages = thread?.messages || [];
    list.innerHTML = messages.length
      ? messages.map((message, index) => messageHtml(message, index)).join('') + (pending ? '<div class="pa-msg pa-assistant"><div class="pa-bubble pa-typing"><span></span><span></span><span></span></div></div>' : '')
      : `<div class="pa-empty">${ru() ? 'Выделите предложение и нажмите «Разобрать грамматику», затем задавайте вопросы здесь.' : 'Select a sentence and press “Explain grammar”, then ask questions here.'}</div>`;
    const send = root.querySelector('[data-pa-send]');
    if (send) send.disabled = pending;
    if (scroll) list.scrollTop = list.scrollHeight;
    enhanceVisible(list);
    saveThread();
  }

  function show() {
    if (!available()) { close(); return false; }
    const root = panel();
    applyGeometry(root);
    collapsed = false;
    root.hidden = false;
    document.getElementById('pudgeAssistantTab').hidden = true;
    render();
  }

  function collapse() {
    if (!available()) { close(); return false; }
    const root = document.getElementById('pudgeAssistant');
    if (!root || root.hidden) return false;
    collapsed = true;
    root.hidden = true;
    const tab = document.getElementById('pudgeAssistantTab');
    if (tab) { labels(); tab.hidden = false; }
    return true;
  }

  function close() {
    const root = document.getElementById('pudgeAssistant');
    const tab = document.getElementById('pudgeAssistantTab');
    collapsed = false;
    if (root) root.hidden = true;
    if (tab) tab.hidden = true;
  }

  function refreshAvailability() {
    const ready = available();
    document.querySelectorAll?.('#lnAssistantToggle,[data-pudge-grammar]').forEach(node => { node.hidden = !ready; });
    if (!ready) { generation += 1; pending = false; close(); }
    else if (document.getElementById('pudgeAssistant')) labels();
    return ready;
  }

  function isOpen() {
    const root = document.getElementById('pudgeAssistant');
    return Boolean(root && !root.hidden);
  }

  function historyForModel() {
    return (thread?.messages || [])
      .filter(message => message.topic === thread.topic)
      .filter(message => !message.error && (message.role === 'user' || message.role === 'assistant'))
      .map(message => ({role: message.role, content: message.modelContent || message.content || ''}))
      .filter(message => message.content);
  }

  async function startGrammar({text, context = ''} = {}) {
    const sentence = String(text || '').trim();
    if (!sentence || !available() || !API()?.analyze_grammar) return false;
    const id = ++generation;
    const topic = Number(thread?.topic || 0) + 1;
    thread = {topic, sentence, context: String(context || ''), grammar: null, messages: thread?.messages || []};
    push({role: 'user', content: `${ru() ? 'Разбери грамматику' : 'Explain the grammar of'}: «${sentence}»`});
    pending = true;
    show();
    let reply;
    try {
      const result = await API().analyze_grammar(sentence, thread.context, `pa${id}-${Date.now()}`);
      if (id !== generation) return true;
      const failed = !result || result.status === 'error' || result.status === 'unavailable';
      const html = `<div class="pudge-grammar">${g.PudgeReadingTools?.grammar?.html ? g.PudgeReadingTools.grammar.html(result || {}) : esc(JSON.stringify(result))}</div>`;
      if (!failed) thread.grammar = result;
      reply = {role: 'assistant', html, content: failed ? '' : grammarSummary(result), error: failed};
    } catch (error) {
      if (id !== generation) return true;
      reply = {role: 'assistant', content: String(error?.message || error), error: true};
    }
    pending = false;
    push(reply);
    render();
    return true;
  }

  async function send(text) {
    const content = String(text || '').trim();
    if (!content || pending || !available()) return false;
    if (!thread) thread = {topic: 1, sentence: '', context: '', grammar: null, messages: []};
    push({role: 'user', content});
    const id = ++generation;
    pending = true;
    render();
    let reply;
    try {
      const result = await API().assistant_chat({
        messages: historyForModel(),
        sentence: thread.sentence,
        context: thread.context,
        grammar: thread.grammar,
      });
      if (id !== generation) return true;
      if (result?.ok) {
        reply = {role: 'assistant', content: String(result.reply || '')};
      } else {
        const why = result?.reason === 'llm_disabled'
          ? (ru() ? 'Включите LLM в настройках.' : 'Enable the LLM in Settings.')
          : (ru() ? 'Не удалось получить ответ.' : 'Could not get an answer.');
        reply = {role: 'assistant', content: `${why}${result?.detail ? `\n\n\`${result.detail}\`` : ''}`, error: true};
      }
    } catch (error) {
      if (id !== generation) return true;
      reply = {role: 'assistant', content: String(error?.message || error), error: true};
    }
    pending = false;
    push(reply);
    render();
    return true;
  }

  function newChat() {
    generation += 1;
    pending = false;
    thread = thread?.sentence ? {topic: Number(thread.topic || 0), sentence: thread.sentence, context: thread.context, grammar: thread.grammar, messages: []} : null;
    render();
    panel().querySelector('[data-pa-input]')?.focus?.();
  }

  function autosize(input) {
    input.style.height = 'auto';
    input.style.height = `${Math.min(160, Math.max(38, input.scrollHeight))}px`;
  }

  document.addEventListener('click', event => {
    const target = event.target;
    if (target?.closest?.('#pudgeAssistantTab')) { show(); return; }
    const root = target?.closest?.('#pudgeAssistant');
    if (!root) return;
    if (target.closest('[data-pa-reset-geometry]')) { resetGeometry(); return; }
    if (target.closest('[data-pa-close]')) { close(); return; }
    if (target.closest('[data-pa-collapse]')) { collapse(); return; }
    if (target.closest('[data-pa-new]')) { newChat(); return; }
    const copyCode = target.closest('[data-pa-copy-code]');
    if (copyCode) {
      void copyText(copyCode.closest('.pa-code')?.querySelector('code')?.textContent || '', copyCode);
      return;
    }
    const copyMessage = target.closest('[data-pa-copy]');
    if (copyMessage) {
      const index = Number(copyMessage.closest('[data-pa-index]')?.dataset.paIndex);
      void copyText(String(thread?.messages?.[index]?.content || ''), copyMessage);
      return;
    }
    const seg = target.closest('.pudge-grammar-seg');
    const toggle = target.closest('[data-grammar-point-toggle]');
    if (seg || toggle) {
      const box = (seg || toggle).closest('.pudge-grammar');
      if (!box) return;
      // Clicking part of the sentence selects its grammar point (the first
      // one covering it; clicking again cycles to the next overlapping one).
      let pointId = String(toggle?.dataset.grammarPointToggle || '');
      if (seg) {
        const ids = String(seg.dataset.points || '').split(' ').filter(Boolean);
        const at = ids.indexOf(box.dataset.activePoint || '');
        pointId = ids.length ? ids[(at + 1) % ids.length] : '';
        if (at === ids.length - 1 && ids.length > 1) pointId = ids[0];
      }
      const next = box.dataset.activePoint === pointId ? '' : pointId;
      box.dataset.activePoint = next;
      box.querySelectorAll('.pudge-grammar-seg').forEach(node => node.classList.toggle('active', Boolean(next) && String(node.dataset.points || '').split(' ').includes(next)));
      box.querySelectorAll('[data-grammar-point]').forEach(node => node.classList.toggle('active', node.dataset.grammarPoint === next));
      if (seg && next) [...box.querySelectorAll('[data-grammar-point]')].find(node => node.dataset.grammarPoint === next)?.scrollIntoView?.({block: 'nearest', behavior: 'smooth'});
    }
  });

  document.addEventListener('submit', event => {
    const form = event.target?.closest?.('[data-pa-form]');
    if (!form) return;
    event.preventDefault();
    const input = form.querySelector('[data-pa-input]');
    const text = input.value;
    // While an answer is pending the draft stays in the box (Enter used to
    // wipe it and the question was lost); it is sent once the answer lands.
    if (!String(text || '').trim() || pending) { input.focus?.(); return; }
    input.value = '';
    autosize(input);
    void send(text);
  });

  document.addEventListener('input', event => {
    if (event.target?.matches?.('[data-pa-input]')) autosize(event.target);
  });


  // Clipboard: the async API works on the local asset origin; fall back to a
  // hidden textarea + execCommand for older WebKit.
  async function copyText(text, button) {
    if (!text) return false;
    let ok = false;
    try {
      const clipboard = (g.navigator || g.window?.navigator)?.clipboard;
      if (clipboard?.writeText) { await clipboard.writeText(text); ok = true; }
    } catch (_error) { ok = false; }
    if (!ok && document.execCommand) {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select?.();
      try { ok = Boolean(document.execCommand('copy')); } catch (_error) { ok = false; }
      area.remove?.();
    }
    if (button) {
      const previous = button.textContent;
      button.textContent = ok ? (ru() ? 'Скопировано' : 'Copied') : (ru() ? 'Не вышло' : 'Failed');
      button.classList?.add('pa-copied');
      setTimeout(() => { button.textContent = previous; button.classList?.remove('pa-copied'); }, 1200);
    }
    return ok;
  }

  function toggle() {
    if (!available()) { close(); return false; }
    if (isOpen()) { collapse(); return false; }
    show();
    return true;
  }

  g.PudgeAssistant = {
    startGrammar,
    toggle,
    send,
    show,
    collapse,
    close,
    isOpen,
    // Escape: collapse the assistant (it stays one click away on the right edge).
    collapseIfOpen: () => collapse(),
    newChat,
    markdownHtml,
    copyText,
    resetGeometry,
    refreshAvailability,
    state: () => ({thread, pending, collapsed}),
  };
  refreshAvailability();
  window.addEventListener?.('pywebviewready', refreshAvailability);
})();
