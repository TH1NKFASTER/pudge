'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {};vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const highlight = context.PudgeAssistantSyntax.highlight;
const plain = html => html.replace(/<\/?span\b[^>]*>/g,'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
const samples = {
  py:'def hello(name):\n    # return 5\n    return "<script>日本語</script>" + name',
  js:'const x = 3; // hello\nconsole.log("return", true);',
  json:'{"key":42,"value":null}',
  bash:'#!/bin/sh\necho "<hello>" # comment',
  html:'<!-- comment --><p title="text">Hi & bye</p>',
  sql:'SELECT name FROM users WHERE id = 5; -- comment',
  unknown:'<img src=x onerror="alert(1)">',
};
for (const [lang,source] of Object.entries(samples)) {
  const html = highlight(source,lang);
  assert.equal(plain(html),source,lang);
  assert(!/<(?:script|img|p)\b/.test(html));
}
assert.match(highlight(samples.py,'python'),/pa-syntax-keyword">def/);
assert.match(highlight(samples.py,'python'),/pa-syntax-comment"># return 5/);
assert.match(highlight(samples.json,'json'),/pa-syntax-property">&quot;key&quot;/);
assert(!highlight(samples.unknown,'unknown').includes('<span'));
const long = '<'.repeat(100001);assert.equal(highlight(long,'python'),'&lt;'.repeat(100001));
assert.equal(plain(highlight('"unterminated \\" + 5', 'js')),'"unterminated \\" + 5');
console.log('assistant syntax: PASS');
