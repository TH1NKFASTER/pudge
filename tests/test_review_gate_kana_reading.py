"""Reading display contracts shared by the sidebar and review gate."""
import json
from pathlib import Path
import subprocess

SOURCE=Path(__file__).resolve().parents[1]/'pudge/web/jiten_words.js'

def line(card):
    script='global.window=global;'+SOURCE.read_text()+';process.stdout.write(PudgeJitenWords.readingsLine('+json.dumps(card)+'));'
    return subprocess.check_output(['node','-e',script],text=True)

def test_kana_only_review_words_do_not_render_duplicate_reading_line():
    assert line({'wordTextPlain':'やらかす','reading':'やらかす','readings':[{'text':'やらかす'}]})==''

def test_single_bracket_ruby_reading_is_still_suppressed():
    assert line({'wordTextPlain':'伏せる','readingIndex':0,'readings':[{'text':'伏[ふ]せる','readingIndex':0}]})==''

def test_all_alternate_readings_are_normalized_in_both_consumers():
    card={'wordTextPlain':'生','readingIndex':1,'readings':[{'text':'生[せい]','readingIndex':0},{'text':'生[なま]','readingIndex':1},{'text':' セイ '},{'text':'生[しょう]'}]}
    assert line(card)=='せい ・ しょう'
    for name in ('review_gate.js','sidebar_companion.js'):
        assert 'PudgeJitenWords.readingsLine(card)' in SOURCE.with_name(name).read_text()

def test_jiten_ruby_rows_show_only_alternate_pronunciations():
    card={'wordTextPlain':'生','readingIndex':1,'readings':[
        {'text':'生','rubyText':'生[せい]','readingIndex':0},
        {'text':'生','rubyText':'生[なま]','readingIndex':1},
        {'text':'生','rubyText':'生[しょう]','readingIndex':2}]}
    assert line(card)=='せい ・ しょう'

def test_shared_prefetch_moves_eight_ahead_and_is_account_scoped():
    setup="global.window=global;const assert=require('node:assert/strict');let scope='a';const calls=[];window.pywebview={api:{study_provider_capabilities:async()=>({account_key:scope}),study_word:async card=>{calls.push(scope+':'+card.wordId);return card;}}};"
    checks=r'''
(async()=>{
  const cards=Array.from({length:12},(_,i)=>({wordId:i+1,readingIndex:0}));
  const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
  await PudgeJitenWords.prefetch(cards);await flush();
  assert.deepEqual(calls,Array.from({length:9},(_,i)=>'a:'+(i+1)));
  await PudgeJitenWords.prefetch(cards.slice(1));await flush();
  assert.deepEqual(calls,Array.from({length:10},(_,i)=>'a:'+(i+1)));
  await PudgeJitenWords.prefetch(cards.slice(1));await flush();assert.equal(calls.length,10);
  scope='b';await PudgeJitenWords.prefetch(cards);await flush();
  assert.deepEqual(calls.slice(10),Array.from({length:9},(_,i)=>'b:'+(i+1)));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    subprocess.run(['node','-e',setup+SOURCE.read_text()+checks],check=True)
