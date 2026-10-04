"""Exercise actual DOM event handlers with synthetic drag events."""
from pathlib import Path
import subprocess

def test_ln_external_drag_nested_children_and_unsupported_file():
    source=Path('pudge/web/ln_drop.js').read_text()
    setup=r'''
const assert=require('assert'); const listeners={}; const classes=new Set();
const box={dataset:{},classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}};
const page={classList:{contains:x=>x==='active'}};
global.window=global;window.ui={lang:'ru'};window.addEventListener=(k,f)=>listeners[k]=f;
global.document={getElementById:id=>id==='lightnovels'?page:box,addEventListener:(k,f)=>listeners[k]=f};
let notices=0;window.toast=()=>notices++;
function event(type,names=[],types=['Files']){return {type,dataTransfer:{types,files:names.map(name=>({name}))},preventDefault(){this.prevented=true},stopImmediatePropagation(){this.stopped=true},stopPropagation(){}}}
'''
    checks=r'''
listeners.dragenter(event('dragenter',['a.epub']));listeners.dragenter(event('dragenter',['a.epub']));
assert(classes.has('ln-file-drag'));listeners.dragleave(event('dragleave'));
assert(classes.has('ln-file-drag'));listeners.dragleave(event('dragleave'));assert(!classes.has('ln-file-drag'));
listeners.dragenter(event('dragenter',[],['text/plain']));assert(!classes.has('ln-file-drag'));
listeners.dragenter(event('dragenter',['a.pdf']));assert(!classes.has('ln-file-drag'));
const bad=event('drop',['a.pdf']);listeners.drop(bad);assert(bad.stopped&&bad.prevented);assert.equal(notices,1);
listeners.dragenter(event('dragenter',['a.txt']));const good=event('drop',['a.txt']);listeners.drop(good);
assert(!good.stopped);assert(!classes.has('ln-file-drag'));
listeners.dragenter(event('dragenter',['a.epub']));listeners.keydown({key:'Escape'});assert(!classes.has('ln-file-drag'));
'''
    subprocess.run(['node','-e',setup+source+checks],check=True)
