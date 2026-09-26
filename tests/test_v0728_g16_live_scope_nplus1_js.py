from __future__ import annotations
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_g16_live_scope_and_nplus1_js():
    script=ROOT/'tests/js/g16_live_scope_nplus1.cjs'
    tools=ROOT/'pudge/web/reading_tools.js'
    result=subprocess.run(['node',str(script),str(tools)],capture_output=True,text=True,check=False)
    assert result.returncode==0, result.stdout+'\n'+result.stderr
    assert 'PASS' in result.stdout
