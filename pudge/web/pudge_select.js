'use strict';

(() => {
  const enhanced = new WeakMap();
  let openSelect = null;
  // Small ring buffer for "menu did not open/close" reports (exported with the LN sync trace).
  const traceRows = [];
  const trace = (action, select, extra = {}) => {
    traceRows.push({t: Math.round(globalThis.performance?.now?.() ?? Date.now()), action, id: String(select?.id || select?.name || ''), open: Boolean(openSelect), ...extra});
    if (traceRows.length > 60) traceRows.shift();
  };

  const shouldSkip = select => !select || select.multiple || select.id === 'lnChapterSelect' || select.classList.contains('ln-chapter-native-select') || select.dataset.pudgeNativeSelect === 'true';

  function selectedLabel(select) {
    const option = select.options?.[select.selectedIndex];
    return String(option?.textContent || option?.label || select.value || '—').trim() || '—';
  }

  function close(select = openSelect, reason = 'close') {
    if (!select) return;
    const state = enhanced.get(select);
    if (!state) return;
    if (state.menu.classList.contains('open')) trace('close', select, {reason});
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

  function menuSignature(select) {
    return [...select.options].map(option => `${option.value}\u0001${option.textContent}\u0001${option.disabled || option.parentElement?.disabled ? 1 : 0}`).join('\u0002');
  }

  function buildMenu(select) {
    const state = enhanced.get(select);
    if (!state) return;
    state.menuSignature = menuSignature(select);
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
    hideNativeSelect(select);
    state.shell.hidden = Boolean(select.hidden);
    if (select.hidden || select.disabled) close(select, select.hidden ? 'sync-hidden' : 'sync-disabled');
    state.label.textContent = selectedLabel(select);
    state.button.disabled = Boolean(select.disabled);
    state.button.setAttribute('aria-disabled', String(Boolean(select.disabled)));
    if (state.menu.classList.contains('open')) {
      // Rebuild only when the option list changed. Replacing the option buttons
      // under the pointer (e.g. a poll re-setting .value every few hundred ms)
      // swallows the user's click, so the choice "does not take" first time.
      if (state.menuSignature !== menuSignature(select)) {
        buildMenu(select);
      } else {
        state.menu.querySelectorAll('[data-pudge-select-index]').forEach(button => {
          const selected = Number(button.dataset.pudgeSelectIndex) === select.selectedIndex;
          button.classList.toggle('selected', selected);
          button.setAttribute('aria-selected', String(selected));
        });
      }
      position(select);
    }
  }

  function open(select) {
    const state = enhanced.get(select);
    if (!state || select.disabled || select.hidden || state.shell.hidden) {
      trace('open-refused', select, {disabled: Boolean(select.disabled), hidden: Boolean(select.hidden), shellHidden: Boolean(state?.shell?.hidden)});
      return;
    }
    if (openSelect && openSelect !== select) close(openSelect, 'other-opened');
    trace('open', select);
    buildMenu(select);
    state.shell.classList.add('open');
    state.menu.classList.add('open');
    state.button.setAttribute('aria-expanded', 'true');
    openSelect = select;
    position(select);
    state.menu.querySelector('.selected')?.scrollIntoView({block: 'nearest'});
  }

  function hideNativeSelect(select) {
    if (!select?.style) return;
    // WKWebView can briefly keep a native select painted while the settings DOM
    // is being rebuilt. Do not rely only on the stylesheet for hiding the native
    // control: the inline !important contract makes enhancement atomic.
    for (const [name, value] of [
      ['position','absolute'], ['width','1px'], ['height','1px'], ['opacity','0'],
      ['pointer-events','none'], ['margin','0'], ['padding','0'],
    ]) select.style.setProperty(name, value, 'important');
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;
  }

  function enhance(select) {
    if (!(select instanceof HTMLSelectElement) || enhanced.has(select) || shouldSkip(select)) return;
    // A script reload must not wrap an already-enhanced select a second time.
    if (select.dataset.pudgeSelectEnhanced === '1' || select.closest?.('.pudge-select')) {
      hideNativeSelect(select);
      return;
    }
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
    select.dataset.pudgeSelectEnhanced = '1';
    hideNativeSelect(select);
    enhanced.set(select, {shell, button, label, menu});
    sync(select);

    // Toggle on press, not on click: in the LN reader WebKit sometimes never
    // delivers the click after the press (traced: button-down with no click),
    // so the menu "did not open first time". The click that follows a press is
    // ignored; keyboard-generated clicks (no press) still toggle.
    let pressedAt = -Infinity;
    const now = () => globalThis.performance?.now?.() ?? Date.now();
    button.addEventListener('pointerdown', event => {
      trace('button-down', select);
      if (event.button !== 0 || select.disabled) return;
      pressedAt = now();
      if (openSelect === select) close(select, 'toggle-press'); else open(select);
    }, true);
    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      trace('button-click', select);
      if (now() - pressedAt < 800) { pressedAt = -Infinity; return; }
      if (openSelect === select) close(select, 'toggle'); else open(select);
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
      close(select, 'chosen');
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
      if (state && !state.shell.contains(event.target) && !state.menu.contains(event.target)) close(openSelect, 'outside-click:' + String(event.target?.id || event.target?.className || event.target?.tagName || ''));
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
  window.PudgeSelect = {enhance, sync, scan, close, trace: () => traceRows.slice(), isOpen:()=>Boolean(openSelect), closeIfOpen:()=>{if(!openSelect)return false;const select=openSelect;close(select,'escape-or-external');enhanced.get(select)?.button?.blur();return true;}};

  const start = () => {
    scan(document);
    observer.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['disabled', 'selected', 'label', 'hidden']});
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
