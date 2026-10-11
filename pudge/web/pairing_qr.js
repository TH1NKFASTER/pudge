'use strict';

(() => {
  let timer = null;
  let invitation = null;
  let busy = false;
  let closed = true;
  const api = () => window.pywebview?.api;
  const cancel = async () => {
    if (!closed && invitation) await api()?.iphone_cancel_pairing?.();
    closed = true;
    invitation = null;
  };
  const when = ms => new Date(Number(ms)).toLocaleString(PudgeI18n.lang(), {dateStyle: 'medium', timeStyle: 'short'});
  const enhance = (root, language) => {
    if (root.querySelector('#iphoneNativeSettings')) return;
    const block = document.createElement('section');
    block.id = 'iphoneNativeSettings';
    block.className = 'setting-block';
    block.dataset.settingsCategory = 'advanced';
    block.innerHTML = `<h3>${PudgeI18n.t('pairing.heading')}</h3>
      <p>${PudgeI18n.t('pairing.devicesIntro')}</p>
      <button id="iphoneEnable" class="primary">${PudgeI18n.t('pairing.enableDeviceConnection')}</button>
      <button id="iphoneShow" class="primary" hidden>${PudgeI18n.t('pairing.showCode')}</button>
      <button id="iphoneOff" hidden>${PudgeI18n.t('pairing.disable')}</button>
      <div id="iphonePairArea" hidden style="margin-top:12px;display:grid;gap:10px;justify-items:start">
        <img id="iphoneQR" alt="${PudgeI18n.t('pairing.qrAlt')}" width="320" height="320" style="max-width:100%;height:auto;background:white;padding:8px;border-radius:12px">
        <small id="iphoneExpiry"></small>
        <button id="iphoneCopy">${PudgeI18n.t('pairing.copyInvitation')}</button>
      </div><div id="iphoneApprovals"></div><p id="iphoneMessage"></p><div id="iphoneDevices"></div>`;
    root.appendChild(block);
    const node = id => block.querySelector('#' + id);
    const say = text => { node('iphoneMessage').textContent = text; };
    const showError = error => { say(PudgeI18n.message(String(error?.message || error))); };
    const visible = () => block.isConnected && !block.classList.contains('settings-category-hidden') && Boolean(block.getClientRects().length);
    const hideCode = () => { node('iphonePairArea').hidden = true; node('iphoneQR').removeAttribute('src'); };
    const live = () => Boolean(invitation && !invitation.completed && Date.now() < invitation.expiresAt);
    // A pairing code exists only after a click, for 10 minutes, while this section is open.
    const pair = async () => {
      if (busy || !visible()) return;
      busy = true;
      try {
        await cancel();
        invitation = await api().iphone_start_pairing();
        closed = false;
        node('iphoneQR').src = invitation.qrDataURL;
        node('iphonePairArea').hidden = false;
        node('iphoneShow').hidden = true;
        say(PudgeI18n.t('pairing.scanInPudgeOnDevice'));
      } catch (error) { showError(error); } finally { busy = false; }
    };
    const button = (label, action, primary = false) => {
      const item = document.createElement('button');
      item.textContent = label;
      if (primary) item.className = 'primary';
      item.onclick = async () => { try { await action(); } catch (error) { showError(error); } };
      return item;
    };
    let pendingKey = '';
    const renderPending = items => {
      const key = JSON.stringify(items || []);
      if (key === pendingKey) return;
      pendingKey = key;
      node('iphoneApprovals').replaceChildren();
      for (const item of items || []) {
        const row = document.createElement('p');
        row.className = 'iphone-approval';
        const text = document.createElement('strong');
        const code = String(item.code || '').replace(/^(\d{3})(\d{3})$/, '$1 $2');
        text.textContent = PudgeI18n.t('pairing.approvePrompt', {device: item.name, code}) + ' ';
        row.append(
          text,
          button(PudgeI18n.t('pairing.allow'), async () => { await api().iphone_approve_pairing(item.deviceId); await refresh(); }, true),
          button(PudgeI18n.t('pairing.deny'), async () => {
            await api().iphone_deny_pairing(item.deviceId);
            closed = true; invitation = null; hideCode();
            say(PudgeI18n.t('pairing.denied'));
            await refresh();
          }),
        );
        node('iphoneApprovals').appendChild(row);
      }
    };
    let devicesKey = '';
    const renderDevices = devices => {
      const shown = (devices || []).filter(device => !device.revoked);
      const key = JSON.stringify(shown);
      if (key === devicesKey) return;
      devicesKey = key;
      node('iphoneDevices').replaceChildren();
      for (const device of shown) {
        const row = document.createElement('p');
        const details = [device.name];
        if (device.paired_at) details.push(PudgeI18n.t('pairing.pairedAt', {date: when(device.paired_at)}));
        details.push(device.last_seen ? PudgeI18n.t('pairing.lastSeen', {date: when(device.last_seen)}) : PudgeI18n.t('pairing.notSeenYet'));
        row.textContent = details.join(' · ') + ' ';
        row.appendChild(button(PudgeI18n.t('pairing.revokeAccess'), async () => { await api().iphone_revoke_device(device.id); await refresh(); }));
        node('iphoneDevices').appendChild(row);
      }
    };
    const refresh = async () => {
      if (!visible()) { await cancel(); hideCode(); return; }
      if (busy || !api()?.iphone_status) return;
      try {
        const status = await api().iphone_status();
        node('iphoneEnable').hidden = status.enabled;
        node('iphoneOff').hidden = !status.enabled;
        if (status.enabled && status.paired && invitation && !invitation.completed) {
          hideCode();
          say(PudgeI18n.t('pairing.deviceConnected', {device: status.pairedName || 'iPhone/iPad'}));
          await cancel();
          // A completed code is not replaced: the next one comes from Show pairing code.
          invitation = {completed: true};
        } else if (invitation && !invitation.completed && (!status.enabled || !status.expiresAt || !live())) {
          hideCode();
          await cancel();
          if (status.enabled) say(PudgeI18n.t('pairing.codeExpired'));
        }
        node('iphoneShow').hidden = !status.enabled || live();
        if (live()) {
          const left = Math.max(0, Math.ceil((invitation.expiresAt - Date.now()) / 1000));
          node('iphoneExpiry').textContent = PudgeI18n.t('pairing.expiresInTime', {time: `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`});
        }
        renderPending(status.enabled ? status.pending : []);
        renderDevices(status.devices);
      } catch (error) { showError(error); }
    };
    node('iphoneEnable').onclick = async () => { try { await api().iphone_enable(); await pair(); await refresh(); } catch (error) { showError(error); } };
    node('iphoneShow').onclick = async () => { await pair(); await refresh(); };
    node('iphoneOff').onclick = async () => { try { await cancel(); await api().iphone_disable(); hideCode(); await refresh(); } catch (error) { showError(error); } };
    // Copied only on request (the clipboard can reach other Apple devices).
    node('iphoneCopy').onclick = () => {
      if (!live()) return;
      navigator.clipboard.writeText(invitation.invitation).then(() => say(PudgeI18n.t('pairing.copied'))).catch(showError);
    };
    if (timer) clearInterval(timer);
    timer = setInterval(() => { refresh().catch(showError); }, 2000);
    refresh().catch(showError);
  };
  window.PudgeIPhoneSettings = {enhance};
})();
