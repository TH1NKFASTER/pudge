'use strict';
// Artificial OCR geometry only. No manga pages, actual OCR exports or extracted dialogue.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const begin = source.indexOf('  function mangaRubyDuplicateRegionIndices(regions) {');
const end = source.indexOf('  function normalizedStudyRegionText(region) {', begin);
assert(begin >= 0 && end > begin, 'ruby helper missing from real reader');
const duplicateIndices = vm.runInNewContext(source.slice(begin, end) + '\nmangaRubyDuplicateRegionIndices;');
function row(text, x, y, width, height, orientation = 'vertical') {
  return {text, x, y, width, height, orientation};
}
const main = row('学校生活', .44, .16, .037, .23);
const ruby = row('がっこう', .474, .19, .012, .11);
const ordinaryDialogue = row('いそいで', .60, .16, .043, .21);
const ordinaryOneKanjiRuby = row('ひと', .714, .19, .012, .11);
const ordinaryOneKanjiMain = row('人', .68, .16, .037, .23);
const examples = [
  {name:'ruby next to full text', rows:[main, ruby, ordinaryDialogue], hidden:[1]},
  {name:'two independent ruby/main pairs', rows:[main, ruby, row('先生たち', .22,.4,.037,.23), row('せんせい',.254,.44,.012,.10)], hidden:[1,3]},
  {name:'ruby not present', rows:[main,ordinaryDialogue], hidden:[]},
  {name:'real kana dialogue', rows:[main, ordinaryDialogue], hidden:[]},
  {name:'ruby next to single kanji stays', rows:[ordinaryOneKanjiMain,ordinaryOneKanjiRuby], hidden:[]},
  {name:'separate kana column', rows:[main,row('がっこう',.65,.19,.012,.11)], hidden:[]},
  {name:'ruby same width as main', rows:[main,row('がっこう',.474,.19,.034,.11)], hidden:[]},
  {name:'ruby nearly same height as main', rows:[main,row('がっこう',.474,.19,.012,.21)], hidden:[]},
  {name:'ruby outside vertical range', rows:[main,row('がっこう',.474,.52,.012,.11)], hidden:[]},
  {name:'horizontal print never deduped', rows:[main,row('がっこう',.474,.19,.012,.11,'horizontal')], hidden:[]},
  {name:'mixed kana+kanji is not ruby', rows:[main,row('学こう',.474,.19,.012,.11)], hidden:[]},
  {name:'short single kana', rows:[main,row('が',.474,.19,.012,.11)], hidden:[]},
  {name:'null region guarded', rows:[null, main, ruby], hidden:[2]},
];
for (const example of examples) {
  const before = JSON.stringify(example.rows);
  const actual = [...duplicateIndices(example.rows)].sort((a,b)=>a-b);
  assert.deepEqual(actual,example.hidden,example.name);
  assert.equal(JSON.stringify(example.rows),before,'OCR rows mutated: '+example.name);
}
console.log(JSON.stringify({syntheticCases:examples.length,passed:true}));
