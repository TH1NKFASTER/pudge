const fs = require('fs');
function hitSurface(v){return String(v||'').normalize('NFKC').replace(/\s+/g,'');}
function findSeq(h,n,start=0){if(!n.length||n.length>h.length)return -1;for(let i=Math.max(0,Number(start||0));i<=h.length-n.length;i++){let ok=true;for(let j=0;j<n.length;j++){if(h[i+j]!==n[j]){ok=false;break;}}if(ok)return i;}return -1;}
function mapTokenSurfaces(stream,surfaces){let cursor=0;return (surfaces||[]).map(value=>{const surface=[...hitSurface(value)];if(!surface.length)return null;const found=findSeq(stream,surface,cursor);if(found<0)return null;cursor=found+surface.length;return {start:found,end:cursor,surface:surface.join('')};});}
if (process.argv.includes('--self-test')) {
  const got=mapTokenSurfaces([...'の海の'],['の','海','の','の']);
  if(JSON.stringify(got.map(x=>x&&x.start))!==JSON.stringify([0,1,2,null])) throw new Error(JSON.stringify(got));
  const punct=mapTokenSurfaces([...'第6話1人目'],['第','話','1人目']);
  if(JSON.stringify(punct.map(x=>x&&x.start))!==JSON.stringify([0,2,3])) throw new Error(JSON.stringify(punct));
  console.log('PASS no-reuse', JSON.stringify(got));
  console.log('PASS punctuation-gap', JSON.stringify(punct));
  process.exit(0);
}
const input=JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(mapTokenSurfaces([...(input.stream||'')], input.tokens||[]), null, 2)+'\n');
