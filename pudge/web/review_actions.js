(() => {
  'use strict';

  // Mirror of pudge/review_actions.py (kept in sync by tests). Used only until
  // the backend settings payload (review_action_sets) has been received.
  const DEFAULT_SETS = {
    'jiten:native': [
      ['again', 'again', 'Again', 'Снова', 'again', '1'],
      ['hard', 'hard', 'Hard', 'Трудно', 'hard', '2'],
      ['good', 'good', 'Good', 'Хорошо', 'good', '3'],
      ['easy', 'easy', 'Easy', 'Легко', 'easy', '4'],
    ],
    'jiten:binary': [
      ['fail', 'again', 'Fail', 'Не помню', 'again', '1'],
      ['pass', 'good', 'Pass', 'Помню', 'good', '2'],
    ],
    'jpdb:native': [
      ['nothing', 'nothing', 'Nothing', 'Ничего', 'again', '1'],
      ['something', 'something', 'Something', 'Что-то', 'something', '2'],
      ['hard', 'hard', 'Hard', 'Трудно', 'hard', '3'],
      ['okay', 'okay', 'Okay', 'Хорошо', 'good', '4'],
      ['easy', 'easy', 'Easy', 'Легко', 'easy', '5'],
    ],
    'jpdb:binary': [
      ['fail', 'fail', 'Fail', 'Не помню', 'again', '1'],
      ['pass', 'pass', 'Pass', 'Помню', 'good', '2'],
    ],
  };
  const MODIFIERS = ['Meta', 'Ctrl', 'Alt', 'Shift'];
  const KEY_ALIASES = {' ': 'Space', Escape: 'Esc', Spacebar: 'Space'};

  let latest = null;

  const ru = () => document?.documentElement?.lang === 'ru' || globalThis.ui?.lang === 'ru';
  const provider = value => String(value || '').toLowerCase() === 'jpdb' ? 'jpdb' : 'jiten';
  const mode = value => String(value || '').toLowerCase() === 'binary' ? 'binary' : 'native';

  function update(settings) {
    if (settings && typeof settings === 'object' && (settings.review_action_sets || settings.review_mode)) {
      latest = settings;
    }
    return latest;
  }

  function defaultSet(providerName, modeName) {
    const key = `${provider(providerName)}:${mode(modeName)}`;
    return {
      provider: provider(providerName),
      mode: mode(modeName),
      actions: DEFAULT_SETS[key].map(([id, wire, label_en, label_ru, tone, default_key]) =>
        ({id, wire, label_en, label_ru, tone, default_key, shortcut: default_key})),
    };
  }

  function actionSet(providerName, settings = latest) {
    const modeName = mode(settings?.review_mode);
    const key = `${provider(providerName)}:${modeName}`;
    const fromSettings = settings?.review_action_sets?.[key];
    if (fromSettings && Array.isArray(fromSettings.actions) && fromSettings.actions.length) {
      return {provider: fromSettings.provider, mode: fromSettings.mode, actions: fromSettings.actions.map(row => ({...row}))};
    }
    return defaultSet(providerName, modeName);
  }

  function label(action) {
    return String((ru() ? action?.label_ru : action?.label_en) || action?.id || '');
  }

  function parseShortcut(raw) {
    const parts = String(raw || '').split('+').map(part => part.trim()).filter(Boolean);
    if (!parts.length) return null;
    const key = parts.pop();
    const mods = new Set(parts.map(part => ({cmd: 'Meta', command: 'Meta', control: 'Ctrl', option: 'Alt', opt: 'Alt'}[part.toLowerCase()] || part)));
    return {key: key.length === 1 ? key.toLowerCase() : key, mods};
  }

  function eventKeys(event) {
    const out = new Set();
    let key = String(event?.key || '');
    key = KEY_ALIASES[key] || key;
    if (key) out.add(key.length === 1 ? key.toLowerCase() : key);
    // Layout-independent fallbacks: physical digit / letter / numpad keys.
    const code = String(event?.code || '');
    let match = /^(?:Digit|Numpad)(\d)$/.exec(code);
    if (match) out.add(match[1]);
    match = /^Key([A-Z])$/.exec(code);
    if (match) out.add(match[1].toLowerCase());
    if (code === 'Space') out.add('Space');
    return out;
  }

  function eventMods(event) {
    const mods = new Set();
    if (event?.metaKey) mods.add('Meta');
    if (event?.ctrlKey) mods.add('Ctrl');
    if (event?.altKey) mods.add('Alt');
    if (event?.shiftKey) mods.add('Shift');
    return mods;
  }

  function shortcutMatches(raw, event) {
    const parsed = parseShortcut(raw);
    if (!parsed) return false;
    const mods = eventMods(event);
    if (MODIFIERS.some(mod => parsed.mods.has(mod) !== mods.has(mod))) return false;
    return eventKeys(event).has(parsed.key);
  }

  // Returns the action of the ACTIVE set bound to this key event, or null.
  // Hidden profiles are never consulted, so an old binding cannot fire.
  function matchAction(event, set) {
    if (!event || event.isComposing || event.repeat) return null;
    if (event.target?.closest?.('input,textarea,select,[contenteditable="true"]')) return null;
    for (const action of set?.actions || []) {
      if (action.shortcut && shortcutMatches(action.shortcut, event)) return action;
    }
    return null;
  }

  function conflicts(set) {
    const seen = new Map();
    const rows = [];
    for (const action of set?.actions || []) {
      const value = String(action.shortcut || '').toLowerCase();
      if (!value) continue;
      if (seen.has(value)) rows.push([seen.get(value), action.id, action.shortcut]);
      else seen.set(value, action.id);
    }
    return rows;
  }

  globalThis.PudgeReviewActions = {update, actionSet, defaultSet, label, matchAction, shortcutMatches, conflicts, DEFAULT_SETS};
})();
