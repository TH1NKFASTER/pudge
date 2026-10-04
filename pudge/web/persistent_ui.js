/* Preferences survive private WebKit storage and changing HTTP ports. */
(function(g){
  'use strict';
  const allowed=new Set(['pudge.assistant.v1','pudge.assistant.geometry.v1','pudge.readTogether.v1']);
  const values={...(g.__pudgeUiBootstrap?.values||{})},dirty=new Map(),running=new Map(),retries=new Map();
  function get(key){
    if(Object.prototype.hasOwnProperty.call(values,key))return values[key];
    try{return JSON.parse(g.localStorage?.getItem(key)||'null');}catch(_){return null;}
  }
  async function drain(key){
    if(running.has(key))return running.get(key);
    if(!g.pywebview?.api?.ui_preference_save)return;
    if(retries.has(key)){g.clearTimeout?.(retries.get(key));retries.delete(key);}
    const task=(async()=>{
      while(dirty.has(key)){
        const value=dirty.get(key);dirty.delete(key);
        try{await g.pywebview.api.ui_preference_save(key,value);}
        catch(error){if(!dirty.has(key))dirty.set(key,value);console.error('UI preference save failed',error);break;}
      }
    })();running.set(key,task);
    try{await task;}finally{
      running.delete(key);
      if(dirty.has(key)&&g.setTimeout&&!retries.has(key)){
        retries.set(key,g.setTimeout(()=>{retries.delete(key);void drain(key);},1000));
      }
    }
  }
  function set(key,value){
    if(!allowed.has(key))throw new Error('Unknown UI preference');
    // Copy now: mutations during a bridge call must not alter its payload.
    const snapshot=value==null?null:JSON.parse(JSON.stringify(value));
    values[key]=snapshot;dirty.set(key,snapshot);
    try{if(snapshot===null)g.localStorage?.removeItem(key);else g.localStorage?.setItem(key,JSON.stringify(snapshot));}catch(_){}
    void drain(key);
  }
  async function flush(){await Promise.all([...dirty.keys(),...running.keys()].map(drain));}
  g.PudgeUiStorage={get,set,flush};
  g.addEventListener?.('pywebviewready',()=>{void flush();});
  g.addEventListener?.('pagehide',()=>{void flush();});
})(window);
