'use strict';
// Interface strings (audit 68Б): load locales.en.js, locales.ru.js and i18n.js
// into a vm context (or get their source) before the module under test.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const FILES = ['locales.en.js', 'locales.ru.js', 'i18n.js'];

function i18nSource(webDir) {
  return FILES.map(name => fs.readFileSync(path.join(webDir, name), 'utf8')).join('\n;\n');
}

function loadI18n(context, webDir) {
  if (!context.globalThis) context.globalThis = context;
  for (const name of FILES) vm.runInContext(fs.readFileSync(path.join(webDir, name), 'utf8'), context, {filename: name});
  return context.PudgeI18n;
}

module.exports = {FILES, i18nSource, loadI18n};
