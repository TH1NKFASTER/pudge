'use strict';

(() => {
  const enhanced = new WeakMap();
  let openSelect = null;

  const shouldSkip = select => !select || select.multiple || select.id === 'lnChapterSelect' || select.classList.contains('ln-chapter-native-select') || select.dataset.pudgeNativeSelect === 'true';

  function selectedLabel(select) {
    const option = select.options?.[select.selectedIndex];
    return String(option?.textContent || option?.label || select.value || '—').trim() || '—';
  }

  function close(select = openSelect) {
    if (!select) return;
    const state = enhanced.get(select);
    if (!state) return;
    state.shell.classList.remove('open');
    state.menu.classList.remove('open');
    state.button.setAttribute('aria-expanded', 'false');
    if (openSelect === select) openSelect = null;
  }

  function position(select) {
    const state = enhanced.get(select);
    if (!state || !state.menu.classList.contains('open')) return;
    const rect = state.button.getBoundingClientRect();
    const width = Math.max(rect.width, Math.min(360, Math.max(170, rect.width * 1.2)));
    const maxLeft = Math.max(8, window.innerWidth - width - 8);
    const below = window.innerHeight - rect.bottom;
    const menuHeight = Math.min(state.menu.scrollHeight, Math.min(360, window.innerHeight * .55));
    const top = below >= Math.min(menuHeight, 180) ? rect.bottom + 5 : Math.max(8, rect.top - menuHeight - 5);
    state.menu.style.left = `${Math.max(8, Math.min(maxLeft, rect.left))}px`;
    state.menu.style.top = `${top}px`;
    state.menu.style.width = `${width}px`;
  }

  function buildMenu(select) {
    const state = enhanced.get(select);
    if (!state) return;
    state.menu.innerHTML = '';
    [...select.options].forEach((option, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'pudge-select-option';
      button.dataset.pudgeSelectIndex = String(index);
      button.textContent = String(option.textContent || option.label || option.value || '—').trim();
      button.disabled = Boolean(option.disabled || option.parentElement?.disabled);
      button.classList.toggle('selected', index === select.selectedIndex);
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(index === select.selectedIndex));
      state.menu.appendChild(button);
    });
  }

  function sync(select) {
    const state = enhanced.get(select);
    if (!state) return;
    state.label.textContent = selectedLabel(select);
    state.button.disabled = Boolean(select.disabled);
    state.button.setAttribute('aria-disabled', String(Boolean(select.disabled)));
    if (state.menu.classList.contains('open')) {
      buildMenu(select);
      position(select);
    }
  }

  function open(select) {
    const state = enhanced.get(select);
    if (!state || select.disabled) return;
    if (openSelect && openSelect !== select) close(openSelect);
    buildMenu(select);
    state.shell.classList.add('open');
    state.menu.classList.add('open');
    state.button.setAttribute('aria-expanded', 'true');
    openSelect = select;
    position(select);
    state.menu.querySelector('.selected')?.scrollIntoView({block: 'nearest'});
  }

  function enhance(select) {
    if (!(select instanceof HTMLSelectElement) || enhanced.has(select) || shouldSkip(select)) return;
    const shell = document.createElement('span');
    shell.className = 'pudge-select';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pudge-select-button';
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    const label = document.createElement('span');
    label.className = 'pudge-select-label';
    const chevron = document.createElement('span');
    chevron.className = 'pudge-select-chevron';
    chevron.textContent = '⌄';
    button.append(label, chevron);
    const menu = document.createElement('div');
    menu.className = 'pudge-select-menu';
    menu.setAttribute('role', 'listbox');

    select.parentNode?.insertBefore(shell, select);
    shell.append(select, button);
    document.body.appendChild(menu);
    select.classList.add('pudge-select-native');
    enhanced.set(select, {shell, button, label, menu});
    sync(select);

    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      if (openSelect === select) close(select); else open(select);
    });
    button.addEventListener('keydown', event => {
      if (['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        open(select);
      }
    });
    menu.addEventListener('click', event => {
      const optionButton = event.target.closest?.('[data-pudge-select-index]');
      if (!optionButton || optionButton.disabled) return;
      const index = Number(optionButton.dataset.pudgeSelectIndex);
      const option = select.options[index];
      if (!option) return;
      select.selectedIndex = index;
      select.dispatchEvent(new Event('input', {bubbles: true}));
      select.dispatchEvent(new Event('change', {bubbles: true}));
      sync(select);
      close(select);
      enhanced.get(select)?.button?.blur();
    });
  }

  function stateFocus(select) {
    const state = enhanced.get(select);
    state?.button?.focus({preventScroll: true});
  }

  function scan(root = document) {
    if (root instanceof HTMLSelectElement) enhance(root);
    root.querySelectorAll?.('select').forEach(enhance);
  }

  document.addEventListener('click', event => {
    if (openSelect) {
      const state = enhanced.get(openSelect);
      if (state && !state.shell.contains(event.target) && !state.menu.contains(event.target)) close(openSelect);
    }
    const label = event.target.closest?.('label[for]');
    if (!label) return;
    const target = document.getElementById(label.htmlFor);
    if (!(target instanceof HTMLSelectElement) || !enhanced.has(target)) return;
    event.preventDefault();
    enhanced.get(target)?.button.click();
  }, true);

  document.addEventListener('change', event => {
    if (event.target instanceof HTMLSelectElement) sync(event.target);
  }, true);
  document.addEventListener('input', event => {
    if (event.target instanceof HTMLSelectElement) sync(event.target);
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && openSelect) {
      event.preventDefault();
      const select = openSelect;
      close(select);
      enhanced.get(select)?.button?.blur();
    }
  }, true);
  window.addEventListener('resize', () => openSelect && position(openSelect));
  window.addEventListener('scroll', () => openSelect && position(openSelect), true);

  const observer = new MutationObserver(records => {
    for (const record of records) {
      record.addedNodes.forEach(node => {
        if (node.nodeType === Node.ELEMENT_NODE) scan(node);
      });
      const target = record.target;
      if (target instanceof HTMLSelectElement) sync(target);
      else if (target instanceof HTMLOptionElement) {
        const select = target.closest('select');
        if (select) sync(select);
      }
    }
  });

  const installValueHook = name => {
    const descriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, name);
    if (!descriptor?.get || !descriptor?.set || descriptor.set.__pudgeWrapped) return;
    const wrapped = function(value) {
      descriptor.set.call(this, value);
      queueMicrotask(() => sync(this));
    };
    wrapped.__pudgeWrapped = true;
    Object.defineProperty(HTMLSelectElement.prototype, name, {...descriptor, set: wrapped});
  };

  installValueHook('value');
  installValueHook('selectedIndex');
  window.PudgeSelect = {enhance, sync, scan, close};

  const start = () => {
    scan(document);
    observer.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['disabled', 'selected', 'label']});
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
