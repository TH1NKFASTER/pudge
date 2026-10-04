'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {
  constructor(name,attrs={},children=[],text=''){
    this.nodeName=name;this.nodeType=name==='#text'?3:1;this.nodeValue=text;
    this.attrs=new Map(Object.entries(attrs));this.childNodes=[];this.parentNode=null;this.moves=0;
    for(const child of children){child.parentNode=this;this.childNodes.push(child);}
  }
  get attributes(){return [...this.attrs].map(([name,value])=>({name,value}));}
  getAttribute(name){return this.attrs.get(name)||null;}
  setAttribute(name,value){this.attrs.set(name,value);}
  removeAttribute(name){this.attrs.delete(name);}
  get outerHTML(){return `<${this.nodeName} ${[...this.attrs].map(([k,v])=>`${k}="${v}"`).join(' ')}>${this.childNodes.map(n=>n.nodeType===3?n.nodeValue:n.outerHTML).join('')}</${this.nodeName}>`;}
  remove(){if(this.parentNode){const p=this.parentNode;p.childNodes.splice(p.childNodes.indexOf(this),1);this.parentNode=null;}}
  insertBefore(node,before){assert.notEqual(node,before);node.remove();const at=before?this.childNodes.indexOf(before):this.childNodes.length;assert(at>=0);this.childNodes.splice(at,0,node);node.parentNode=this;node.moves++;}
}
const text=value=>new Node('#text',{},[],value);
const card=(id,title)=>new Node('ARTICLE',{'data-home-key':id,'data-active-index':'0'},[
  new Node('DIV',{class:'cover-shell'},[new Node('IMG',{src:`${id}.png`})]),
  new Node('STRONG',{},[text(title)])
]);
const section=(name,cards)=>new Node('SECTION',{'data-home-section':name},[new Node('H2',{},[text(name)]),new Node('DIV',{class:'airing-grid'},cards)]);
const root=new Node('DIV'),ctx=vm.createContext({});
const source=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(source.slice(source.indexOf('const homeNodeTemplates='),source.indexOf('function renderCurrent(){')),ctx);
const patch=nodes=>ctx.patchHomeChildren(root,nodes);
patch([section('ready',[card('1','First'),card('2','Second')])]);
const oldSection=root.childNodes[0],grid=oldSection.childNodes[1],first=grid.childNodes[0],second=grid.childNodes[1];
const cover=first.childNodes[0],image=cover.childNodes[0];
image.setAttribute('data-pudge-cover-ready','1');cover.setAttribute('class','cover-shell polychrome-wake');
first.setAttribute('data-active-index','2');
const moves=[oldSection.moves,first.moves,second.moves,image.moves];
for(let i=0;i<30;i++)patch([section('ready',[card('1','First'),card('2','Second')])]);
assert.equal(root.childNodes[0],oldSection);assert.equal(grid.childNodes[0],first);
assert.equal(first.getAttribute('data-active-index'),'2');
assert.deepEqual([oldSection.moves,first.moves,second.moves,image.moves],moves);
patch([section('ready',[card('1','Changed'),card('2','Second'),card('3','Third')])]);
assert.equal(grid.childNodes[0],first);assert.equal(first.childNodes[0],cover);assert.equal(cover.childNodes[0],image);
assert.equal(image.getAttribute('data-pudge-cover-ready'),'1');assert.equal(cover.getAttribute('class'),'cover-shell polychrome-wake');
assert.equal(first.childNodes[1].childNodes[0].nodeValue,'Changed');assert.equal(grid.childNodes[1],second);
assert.equal(second.moves,moves[2]);
patch([section('ready',[card('2','Second'),card('1','Changed')])]);
assert.equal(grid.childNodes[0],second);assert.equal(grid.childNodes[1],first);assert.equal(grid.childNodes.length,2);
patch([section('other',[card('4','Fourth')]),section('ready',[card('2','Second'),card('1','Changed')])]);
assert.equal(root.childNodes[1],oldSection);assert.equal(cover.childNodes[0],image);
patch([]);assert.equal(root.childNodes.length,0);
console.log('Home cards: repeated updates, edits, reorder, insertion, deletion and live DOM state passed');
