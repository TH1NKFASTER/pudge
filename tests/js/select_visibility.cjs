const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
class Element{
 constructor(){this.children=[];this.dataset={};this.style={setProperty(){}};this.attrs={};this.events={};this.className='';this.hidden=false;this.disabled=false;this.nodeType=1;
 this.classList={contains:x=>this.className.split(' ').includes(x),add:x=>{if(!this.classList.contains(x))this.className+=' '+x;},remove:x=>this.className=this.className.split(' ').filter(v=>v!==x).join(' '),toggle:(x,on)=>on?this.classList.add(x):this.classList.remove(x)};}
 append(...nodes){for(const n of nodes){this.children.push(n);n.parentNode=this;}}
 appendChild(n){this.append(n);return n;} insertBefore(n,old){this.children.splice(this.children.indexOf(old),0,n);n.parentNode=this;}
 setAttribute(k,v){this.attrs[k]=v;} addEventListener(k,v){this.events[k]=v;}
 getBoundingClientRect(){return {width:100,left:20,top:20,bottom:40};}
 querySelector(){return null;}querySelectorAll(q){return this.children.flatMap(c=>[...(q==='select'&&c instanceof Select?[c]:[]),...c.querySelectorAll(q)]);}
 closest(){return null;} contains(n){return n===this||this.children.some(c=>c.contains(n));}blur(){}focus(){}
 click(){this.events.click?.({preventDefault(){},stopPropagation(){}});}
}
class Select extends Element{constructor(){super();this.options=[{textContent:'Local',value:'local'}];this.selectedIndex=0;this.value='local';}}
const body=new Element(),select=new Select();body.append(select);let observer;
const document={body,readyState:'complete',addEventListener(){},querySelectorAll:q=>body.querySelectorAll(q),createElement:()=>new Element()};
const window={innerWidth:1000,innerHeight:800,addEventListener(){}};
const context={window,document,HTMLSelectElement:Select,HTMLOptionElement:class{},Node:{ELEMENT_NODE:1},MutationObserver:class{constructor(fn){observer=fn;}observe(){}},queueMicrotask,Event:class{}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../pudge/web/pudge_select.js'),'utf8'),context);
const shell=select.parentNode,button=shell.children[1];button.click();assert(window.PudgeSelect.isOpen());
select.hidden=true;observer([{target:select,addedNodes:[]}]);assert(shell.hidden&&!window.PudgeSelect.isOpen());button.click();assert(!window.PudgeSelect.isOpen());
select.hidden=false;select.disabled=true;window.PudgeSelect.sync(select);assert(!shell.hidden&&button.disabled);
select.disabled=false;window.PudgeSelect.sync(select);button.click();assert(window.PudgeSelect.isOpen());
console.log('Select hidden/disabled transitions passed');
