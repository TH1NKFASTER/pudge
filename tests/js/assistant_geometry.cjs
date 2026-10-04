'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const stored=new Map([['pudge.assistant.geometry.v1',{x:2300,y:900,width:700,height:850}]]);
const nodes=new Map(),listeners={};
function node(){
  const n={hidden:true,isConnected:true,style:{},dataset:{},children:[],listeners:{},parts:{},
    classList:{contains:()=>false,add(){},remove(){}},setAttribute(){},
    set id(value){this._id=value;nodes.set(value,this);},get id(){return this._id;},
    appendChild(child){this.children.push(child);},
    addEventListener(name,fn){this.listeners[name]=fn;},
    querySelector(selector){return this.parts[selector] ||= node();},
    querySelectorAll(){return this.children;},
    getBoundingClientRect(){return {left:parseFloat(this.style.left)||0,top:parseFloat(this.style.top)||0,
      width:parseFloat(this.style.width)||0,height:parseFloat(this.style.height)||0};},focus(){}};
  return n;
}
const context={console,setTimeout:()=>1,clearTimeout(){},
  document:{documentElement:{lang:'en'},body:{appendChild(){}},createElement:()=>node(),getElementById:id=>nodes.get(id),addEventListener(){}},
  innerWidth:1024,innerHeight:720,addEventListener(name,fn){(listeners[name] ||= []).push(fn);},
  PudgeUiStorage:{get:key=>stored.get(key),set:(key,value)=>stored.set(key,value)},
  PudgeReadingTools:{llmAvailable:()=>true}};
context.window=context;context.globalThis=context;vm.createContext(context);
let source=fs.readFileSync(process.argv[2],'utf8');
source=source.replace('  g.PudgeAssistant = {','  g.geometryTest = {clampGeometry};\n  g.PudgeAssistant = {');
vm.runInContext(source,context);
function visible(rect){
  assert(Number.isFinite(rect.width)&&Number.isFinite(rect.height));
  assert(rect.left>=0&&rect.top>=0);
  assert(rect.left+rect.width<=context.innerWidth);
  assert(rect.top+rect.height<=context.innerHeight);
}
context.PudgeAssistant.show();
const panel=nodes.get('pudgeAssistant');visible(panel.getBoundingClientRect());
for(const [width,height] of [[1440,900],[800,420],[240,180],[980,600]]){
  context.innerWidth=width;context.innerHeight=height;
  for(const fn of listeners.resize)fn();
  visible(panel.getBoundingClientRect());
  // Dragging any edge beyond the viewport cannot leave only the title visible.
  const head=panel.querySelector('.pa-head');
  head.listeners.pointerdown({button:0,pointerId:1,clientX:12,clientY:12,target:{closest:()=>null},preventDefault(){}});
  for(const [x,y] of [[-3000,-3000],[3000,3000]]){
    head.listeners.pointermove({pointerId:1,clientX:x,clientY:y});visible(panel.getBoundingClientRect());
  }
  head.listeners.pointerup({pointerId:1});
  const corner=panel.children.find(row=>row.dataset.paResize==='se');
  corner.listeners.pointerdown({button:0,pointerId:2,clientX:0,clientY:0,preventDefault(){},stopPropagation(){}});
  corner.listeners.pointermove({pointerId:2,clientX:5000,clientY:5000});visible(panel.getBoundingClientRect());
  corner.listeners.pointerup({pointerId:2});
}
// A panel collapsed during the window resize is clamped again when reopened.
context.PudgeAssistant.collapse();context.innerWidth=420;context.innerHeight=300;
for(const fn of listeners.resize)fn();
context.PudgeAssistant.show();visible(panel.getBoundingClientRect());
console.log('Assistant viewport, drag, resize and reopen: PASS');
