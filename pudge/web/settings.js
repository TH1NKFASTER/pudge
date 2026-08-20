'use strict';

(() => {
  const text = {
    en: {
      title: 'iPhone companion',
      description: 'Open your Pudge library on an iPhone connected to the same Wi‑Fi.',
      pair: 'Pair iPhone',
      disable: 'Disable',
      copy: 'Copy link',
      copied: 'Copied',
      disabled: 'Not connected',
      enabled: 'Ready on this network',
      pairing: 'Pairing link is valid for 5 minutes.',
    },
    ru: {
      title: 'iPhone',
      description: 'Открывай библиотеку Pudge на iPhone в той же Wi‑Fi сети.',
      pair: 'Подключить iPhone',
      disable: 'Выключить',
      copy: 'Скопировать ссылку',
      copied: 'Скопировано',
      disabled: 'Не подключено',
      enabled: 'Готово в этой сети',
      pairing: 'Ссылка действует 5 минут.',
    },
  };

  const categoryFor = block => {
    const ids = new Set([...block.querySelectorAll('[id]')].map(node => node.id));
    const has = (...values) => values.some(value => ids.has(value));
    if (has('s_language', 's_jimaku', 's_anilist_enabled')) return 'essential';
    if (has('s_library', 's_watched_folders', 's_disk_limit_enabled')) return 'library';
    if (has('s_subtitle_folders', 's_ocr_image_subtitles')) return 'subtitles';
    if (has('s_qbt_enabled', 's_aria2_enabled', 's_nyaa_enabled')) return 'downloads';
    if (has('s_ln_jiten_key')) return 'essential';
    if (has('s_ln_parse_ahead', 'mangaOcrStatus', 'installMangaOcr')) return 'advanced';
    if (has('s_playback_enabled')) return 'advanced';
    if (has('s_agent_enabled')) return 'advanced';
    if (has('companionPair', 'companionDisable')) return 'advanced';
    return 'advanced';
  };

  const labels = {
    en: {essential: 'Essential', library: 'Library', subtitles: 'Subtitles', downloads: 'Downloads', advanced: 'Advanced'},
    ru: {essential: 'Основное', library: 'Библиотека', subtitles: 'Субтитры', downloads: 'Загрузки', advanced: 'Дополнительно'},
  };
  let active = 'essential';

  const bridge = () => window.pywebview?.api || null;

  const refreshCompanion = async (block, strings) => {
    const api = bridge();
    if (!api?.companion_status) return;
    try {
      const status = await api.companion_status();
      const state = block.querySelector('#companionState');
      const base = block.querySelector('#companionBaseUrl');
      const disable = block.querySelector('#companionDisable');
      const live = Boolean(status?.enabled && status?.base_url);
      state.textContent = live ? strings.enabled : strings.disabled;
      base.textContent = live ? `${status.base_url}/companion/` : '';
      base.hidden = !live;
      disable.hidden = !status?.enabled;
    } catch (_) {}
  };

  const copyValue = async input => {
    try {
      await navigator.clipboard.writeText(input.value);
      return true;
    } catch (_) {
      input.hidden = false;
      input.select();
      return document.execCommand('copy');
    }
  };

  const ensureCompanionBlock = (root, language) => {
    const strings = text[language] || text.en;
    let block = root.querySelector('#companionSettingsBlock');
    if (block) {
      refreshCompanion(block, strings);
      return block;
    }

    block = document.createElement('section');
    block.className = 'setting-block';
    block.id = 'companionSettingsBlock';
    block.innerHTML = `
      <h3>${strings.title}</h3>
      <p>${strings.description}</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button type="button" class="primary" id="companionPair">${strings.pair}</button>
        <button type="button" id="companionDisable" hidden>${strings.disable}</button>
      </div>
      <div id="companionState" style="margin-top:8px;color:var(--muted)">${strings.disabled}</div>
      <div id="companionPairResult" hidden style="margin-top:10px;display:grid;gap:8px">
        <input id="companionPairUrl" type="text" readonly>
        <div><button type="button" id="companionCopy">${strings.copy}</button></div>
        <small style="color:var(--muted)">${strings.pairing}</small>
      </div>
      <div id="companionBaseUrl" hidden style="margin-top:8px;color:var(--muted);overflow-wrap:anywhere"></div>
    `;
    root.appendChild(block);

    const pair = block.querySelector('#companionPair');
    const disable = block.querySelector('#companionDisable');
    const result = block.querySelector('#companionPairResult');
    const url = block.querySelector('#companionPairUrl');
    const copy = block.querySelector('#companionCopy');

    pair.addEventListener('click', async () => {
      const api = bridge();
      if (!api?.companion_start_pairing) return;
      pair.disabled = true;
      try {
        const payload = await api.companion_start_pairing();
        url.value = String(payload?.companion_url || '');
        result.hidden = !url.value;
        await refreshCompanion(block, strings);
      } finally {
        pair.disabled = false;
      }
    });

    copy.addEventListener('click', async () => {
      if (!url.value) return;
      if (await copyValue(url)) {
        const previous = copy.textContent;
        copy.textContent = strings.copied;
        setTimeout(() => { copy.textContent = previous; }, 1200);
      }
    });

    disable.addEventListener('click', async () => {
      const api = bridge();
      if (!api?.companion_disable) return;
      disable.disabled = true;
      try {
        await api.companion_disable();
        result.hidden = true;
        await refreshCompanion(block, strings);
      } finally {
        disable.disabled = false;
      }
    });

    refreshCompanion(block, strings);
    return block;
  };

  const show = (root, category) => {
    active = category;
    const searching = Boolean(root.querySelector('#settingsSearch')?.value.trim());
    root.querySelectorAll('.setting-block').forEach(block => {
      block.classList.toggle('settings-category-hidden', !searching && block.dataset.settingsCategory !== active);
    });
    root.querySelectorAll('[data-settings-category]').forEach(button => {
      button.classList.toggle('active', button.dataset.settingsCategory === active);
      button.setAttribute('aria-selected', button.dataset.settingsCategory === active ? 'true' : 'false');
    });
  };

  const enhance = language => {
    const root = document.getElementById('settingsContent');
    if (!root) return;
    ensureCompanionBlock(root, language);
    root.querySelectorAll('.setting-block').forEach(block => {
      block.dataset.settingsCategory = block.dataset.settingsCategory || categoryFor(block);
    });
    if (root.querySelector('.settings-categories')) {
      show(root, active);
      return;
    }
    const strings = labels[language] || labels.en;
    const tabs = document.createElement('div');
    tabs.className = 'settings-categories';
    tabs.setAttribute('role', 'tablist');
    tabs.innerHTML = Object.entries(strings).map(([key, label]) =>
      `<button type="button" role="tab" data-settings-category="${key}">${label}</button>`
    ).join('');
    root.querySelector('.settings-search-wrap')?.after(tabs);
    tabs.addEventListener('click', event => {
      const button = event.target.closest('[data-settings-category]');
      if (button) show(root, button.dataset.settingsCategory);
    });
    root.querySelector('#settingsSearch')?.addEventListener('input', () => show(root, active));
    show(root, active);
  };

  const focusAction = actionCode => {
    const root = document.getElementById('settingsContent');
    if (!root) return;
    const targets = {
      configure_jimaku: 's_jimaku',
      grant_folder_access: 's_watched_folders',
      enable_subtitle_ocr: 's_ocr_image_subtitles',
    };
    const target = document.getElementById(targets[actionCode] || 'settingsSearch');
    const block = target?.closest('.setting-block');
    if (block) show(root, block.dataset.settingsCategory || 'essential');
    target?.scrollIntoView({block: 'center', behavior: 'smooth'});
    target?.focus({preventScroll: true});
  };

  const makeInteractiveAccessible = root => {
    root.querySelectorAll('[data-action]:not(button):not(a), [data-media-id]:not(button):not(a)').forEach(node => {
      if (!node.hasAttribute('tabindex')) node.tabIndex = 0;
      if (!node.hasAttribute('role')) node.setAttribute('role', 'button');
    });
  };
  const observer = new MutationObserver(records => records.forEach(record =>
    record.addedNodes.forEach(node => node.nodeType === 1 && makeInteractiveAccessible(node))
  ));
  observer.observe(document.body, {childList: true, subtree: true});
  makeInteractiveAccessible(document);
  document.addEventListener('keydown', event => {
    if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('[role="button"]:not(button):not(a)')) {
      event.preventDefault();
      event.target.click();
    }
  });

  const refresh = () => { const root = document.getElementById('settingsContent'); if (root) show(root, active); };
  window.PudgeSettings = {enhance, focusAction, refresh};
})();
