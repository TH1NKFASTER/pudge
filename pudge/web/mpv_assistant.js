'use strict';
(()=>{
  let request='';
  async function refresh(){
    const data=await pywebview.api.mpv_assistant_snapshot();
    // The interface language comes from the main window's settings (mpv_assistant_snapshot).
    const language=String(data.settings?.language||'en').toLowerCase().startsWith('ru')?'ru':'en';
    const changed=window.ui.lang!==language;
    window.ui.state.settings=data.settings||{};window.ui.lang=language;document.documentElement.lang=language;
    document.title=PudgeI18n.t('mpvAssistant.title');
    if(changed&&document.getElementById('pudgeAssistant'))window.PudgeAssistant?.refreshAvailability?.();
    const snapshot=data.snapshot||{};
    if(!snapshot.request_id||request===snapshot.request_id)return;
    request=snapshot.request_id;
    await window.PudgeAssistant.startGrammar({text:snapshot.text,context:snapshot.context});
  }
  document.addEventListener('click',event=>{if(event.target.closest('[data-pa-close]'))void pywebview.api.mpv_assistant_close();},true);
  window.PudgeMpvAssistant={refresh};
  window.addEventListener('pywebviewready',()=>void refresh(),{once:true});
})();
