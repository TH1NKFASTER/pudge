'use strict';
// Small, offline lexer. Work on source text and escape every token exactly once.
// Unknown languages remain plain text; long blocks avoid costly highlighting.
(() => {
  const esc = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch]));
  const aliases = {js:'javascript', ts:'typescript', jsx:'javascript', tsx:'typescript', py:'python', sh:'bash', shell:'bash', zsh:'bash', yml:'yaml', xml:'html', svg:'html', 'c++':'cpp', 'c#':'csharp'};
  const supported = new Set(['javascript','typescript','python','json','bash','html','css','sql','yaml','toml','c','cpp','csharp','java','go','rust','ruby','swift','kotlin']);
  const keywords = new Set(('as async await break case catch class const continue def del do elif else enum except export extends finally fn for from func function if implements import in interface is lambda let match namespace new of package pass private protected public raise return select static struct super switch this throw try type typeof use var void while with yield').split(' '));
  const literals = new Set(['true','false','null','undefined','None','True','False','nil']);
  function highlight(source, language) {
    const text = String(source ?? ''), raw = String(language || '').toLowerCase();
    const lang = aliases[raw] || raw;
    if (!supported.has(lang) || text.length > 100_000) return esc(text);
    const hashComments = ['python','bash','yaml','toml','ruby'].includes(lang);
    const slashComments = !['json','html','sql','yaml','toml','python','bash','ruby'].includes(lang);
    let out = '', i = 0;
    const emit = (kind, token) => { out += kind ? `<span class="pa-syntax-${kind}">${esc(token)}</span>` : esc(token); i += token.length; };
    while (i < text.length) {
      const rest = text.slice(i), ch = text[i];
      if ((hashComments && ch === '#') || (slashComments && rest.startsWith('//')) || (lang === 'sql' && rest.startsWith('--'))) {
        emit('comment', rest.split('\n',1)[0]); continue;
      }
      if ((slashComments || lang === 'css' || lang === 'sql') && rest.startsWith('/*')) {
        const end = text.indexOf('*/', i + 2); emit('comment', text.slice(i, end < 0 ? text.length : end + 2)); continue;
      }
      if (lang === 'html' && rest.startsWith('<!--')) {
        const end = text.indexOf('-->', i + 4); emit('comment', text.slice(i, end < 0 ? text.length : end + 3)); continue;
      }
      if (ch === '"' || ch === "'" || (['javascript','typescript','go'].includes(lang) && ch === '`')) {
        const triple = lang === 'python' && rest.startsWith(ch.repeat(3));
        const quote = triple ? ch.repeat(3) : ch;
        let end = i + quote.length;
        while (end < text.length) {
          if (text[end] === '\\') { end += 2; continue; }
          if (text.startsWith(quote,end)) { end += quote.length; break; }
          end++;
        }
        const token = text.slice(i,end);
        const kind = lang === 'json' && /^\s*:/.test(text.slice(end)) ? 'property' : 'string';
        emit(kind,token); continue;
      }
      if (lang === 'html') {
        const tag = rest.match(/^<\/?[\w:-]+|^\/?>/);
        if (tag) { emit('keyword',tag[0]); continue; }
      }
      const number = rest.match(/^(?:0[xX][\da-fA-F]+|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)/);
      if (number) { emit('number',number[0]); continue; }
      const word = rest.match(/^[A-Za-z_$][\w$]*/);
      if (word) {
        const token = word[0], key = lang === 'sql' ? token.toLowerCase() : token;
        const sqlWord = lang === 'sql' && /^(select|insert|into|update|delete|where|join|left|right|inner|outer|on|group|order|by|and|or|not|values|create|table|limit|desc|asc|set|distinct|having|union|all|drop)$/.test(key);
        const kind = literals.has(token) ? 'literal' : keywords.has(key) || sqlWord ? 'keyword' : /^\s*\(/.test(text.slice(i+token.length)) ? 'function' : null;
        emit(kind,token); continue;
      }
      emit(/[{}()[\];,:=+*/!<>|&?-]/.test(ch) ? 'operator' : null, ch);
    }
    return out;
  }
  globalThis.PudgeAssistantSyntax = {highlight};
})();
