'use strict';
(()=>{
  let request='';
  async function refresh(){
    const data=await pywebview.api.mpv_assistant_snapshot();
    window.ui.state.settings=data.settings||{};window.ui.lang=data.settings?.language||'en';document.documentElement.lang=window.ui.lang;
    const snapshot=data.snapshot||{};
    if(!snapshot.request_id||request===snapshot.request_id)return;
    request=snapshot.request_id;
    await window.PudgeAssistant.startGrammar({text:snapshot.text,context:snapshot.context});
  }
  document.addEventListener('click',event=>{if(event.target.closest('[data-pa-close]'))void pywebview.api.mpv_assistant_close();},true);
  window.PudgeMpvAssistant={refresh};
  window.addEventListener('pywebviewready',()=>void refresh(),{once:true});
})();
