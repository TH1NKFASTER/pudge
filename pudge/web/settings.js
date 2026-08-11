'use strict';

(() => {
  const categoryFor = block => {
    const ids = new Set([...block.querySelectorAll('[id]')].map(node => node.id));
    const has = (...values) => values.some(value => ids.has(value));
    if (has('s_language', 's_jimaku', 's_anilist_enabled')) return 'essential';
    if (has('s_library', 's_watched_folders', 's_disk_limit_enabled')) return 'library';
    if (has('s_subtitle_folders', 's_ocr_image_subtitles')) return 'subtitles';
    if (has('s_qbt_enabled', 's_aria2_enabled', 's_nyaa_enabled')) return 'downloads';
    if (has('s_ln_jiten_key', 'mangaOcrStatus', 'installMangaOcr')) return 'reading';
    if (has('s_playback_enabled')) return 'advanced';
    if (has('s_agent_enabled')) return 'advanced';
    return 'advanced';
  };

  const labels = {
    en: {essential: 'Essential', library: 'Library', subtitles: 'Subtitles', downloads: 'Downloads', reading: 'Reading', advanced: 'Advanced'},
    ru: {essential: 'Основное', library: 'Библиотека', subtitles: 'Субтитры', downloads: 'Загрузки', reading: 'Чтение', advanced: 'Дополнительно'},
  };

  let active = 'essential';

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
    if (!root || root.querySelector('.settings-categories')) return;
    root.querySelectorAll('.setting-block').forEach(block => {
      block.dataset.settingsCategory = categoryFor(block);
    });
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

  window.PudgeSettings = {enhance, focusAction};
})();
