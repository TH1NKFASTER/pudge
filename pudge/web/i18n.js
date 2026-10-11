/* Interface strings (audit 68Б): one catalog per language in locales.<lang>.js.
 *
 *   PudgeI18n.t('key', {name: value})      text in the current language
 *   PudgeI18n.tn('key', count, {...})       plural entry ({count} is filled in)
 *   PudgeI18n.message(text)                 a backend error (English) in the current language
 *
 * Missing keys fall back to English, then to the key itself. Python reads the
 * same catalogs (pudge/i18n.py), so backend errors are English in logs and
 * shown in the interface language here.
 */
(() => {
  'use strict';
  const g = globalThis;
  const catalogs = () => g.PudgeLocales || {};

  function lang() {
    let current;
    try { current = typeof ui !== 'undefined' ? ui.lang : null; } catch (_) { /* before ui exists */ }
    const value = String(
      current || g.ui?.lang || g.__pudgeUiBootstrap?.language || g.document?.documentElement?.lang || 'en'
    ).toLowerCase();
    return value.startsWith('ru') ? 'ru' : 'en';
  }

  const pluralRules = new Map();
  function pluralForm(language, count) {
    const number = Math.abs(Number(count) || 0);
    try {
      if (!pluralRules.has(language)) pluralRules.set(language, new Intl.PluralRules(language === 'ru' ? 'ru-RU' : 'en-US'));
      return pluralRules.get(language).select(number);
    } catch (_) {
      if (language !== 'ru') return number === 1 ? 'one' : 'other';
      if (!Number.isInteger(number)) return 'other';
      const ten = number % 10, hundred = number % 100;
      if (ten === 1 && hundred !== 11) return 'one';
      if (ten >= 2 && ten <= 4 && (hundred < 12 || hundred > 14)) return 'few';
      return 'many';
    }
  }

  function raw(key, language) {
    const own = catalogs()[language];
    if (own && Object.prototype.hasOwnProperty.call(own, key)) return own[key];
    const english = catalogs().en;
    if (english && Object.prototype.hasOwnProperty.call(english, key)) return english[key];
    return undefined;
  }

  function has(key) { return raw(key, 'en') !== undefined; }

  // A passed null/undefined becomes empty text; an unknown field stays as is.
  function format(text, params) {
    return String(text).replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => {
      if (!params || !Object.prototype.hasOwnProperty.call(params, name)) return match;
      const value = params[name];
      return value === undefined || value === null ? '' : String(value);
    });
  }

  // Values that are themselves backend messages are shown in the interface language too.
  const MESSAGE_PARAMS = new Set(['error', 'reason', 'message', 'detail']);
  function localizedParams(params, language) {
    if (!params || typeof params !== 'object') return params;
    let copy = null;
    for (const name of MESSAGE_PARAMS) {
      if (typeof params[name] === 'string' && params[name]) {
        const translated = message(params[name], language);
        if (translated !== params[name]) {
          copy = copy || {...params};
          copy[name] = translated;
        }
      }
    }
    return copy || params;
  }

  function pick(value, language, params) {
    if (value && typeof value === 'object') {
      const form = pluralForm(language, params?.count ?? 0);
      return value[form] ?? value.other ?? value.many ?? value.one ?? '';
    }
    return value;
  }

  // Optional template hook (the main window puts the app's brand name in place of "Pudge");
  // it runs on the catalog text before parameters are filled in, never on user data.
  let templateTransform = null;
  function setTemplateTransform(fn) { templateTransform = typeof fn === 'function' ? fn : null; }

  function t(key, params = {}, language = lang()) {
    const value = raw(String(key), language);
    if (value === undefined) return String(key);
    let template = String(pick(value, language, params));
    if (templateTransform) { try { template = String(templateTransform(template)); } catch (_) { /* keep the catalog text */ } }
    return format(template, localizedParams(params, language));
  }

  function tn(key, count, params = {}, language = lang()) {
    return t(key, {...(params || {}), count}, language);
  }

  // Backend errors arrive as English text rendered from "error.*" entries;
  // older stored ones may still be Russian. Either is shown in the current language.
  let errorPatterns = null;
  function compileErrors() {
    const patterns = [];
    for (const language of ['en', 'ru']) {
      for (const [key, template] of Object.entries(catalogs()[language] || {})) {
        if (!key.startsWith('error.') || typeof template !== 'string') continue;
        const names = [];
        const source = template.split(/(\{[A-Za-z0-9_]+\})/).map(part => {
          const field = part.match(/^\{([A-Za-z0-9_]+)\}$/);
          if (field) { names.push(field[1]); return '([\\s\\S]*?)'; }
          return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        }).join('');
        const weight = template.replace(/\{[A-Za-z0-9_]+\}/g, '').length;
        if (weight > 0) patterns.push({key, names, regex: new RegExp(`^${source}$`), weight});
      }
    }
    patterns.sort((a, b) => b.weight - a.weight);
    return patterns;
  }

  function message(text, language = lang(), depth = 0) {
    const value = String(text ?? '');
    if (!value || depth > 3) return value;
    if (!errorPatterns) errorPatterns = compileErrors();
    const prefix = value.startsWith('Error: ') ? 'Error: ' : '';
    let body = value.slice(prefix.length);
    // Python's KeyError quotes its message.
    if (body.length >= 2 && body[0] === body[body.length - 1] && (body[0] === "'" || body[0] === '"')) body = body.slice(1, -1);
    for (const pattern of errorPatterns) {
      const match = pattern.regex.exec(body);
      if (!match) continue;
      const params = {};
      pattern.names.forEach((name, index) => { params[name] = message(match[index + 1], language, depth + 1); });
      const translated = format(pick(raw(pattern.key, language), language, params), params);
      return prefix ? t('error.generic', {error: translated}, language) : translated;
    }
    return value;
  }

  function catalog(language = lang()) { return {...(catalogs().en || {}), ...(catalogs()[language] || {})}; }

  // Numbers in the interface language (not the Mac's region): 12,345 / 12 345.
  const numberFormats = new Map();
  function number(value, language = lang()) {
    if (!numberFormats.has(language)) numberFormats.set(language, new Intl.NumberFormat(language === 'ru' ? 'ru-RU' : 'en-US'));
    return numberFormats.get(language).format(Number(value || 0));
  }

  g.PudgeI18n = {t, tn, lang, has, format, message, pluralForm, catalog, setTemplateTransform, number};
  // Older callers.
  g.PudgeUiLanguage = {...(g.PudgeUiLanguage || {}), message: (text) => message(text), language: lang};
})();
