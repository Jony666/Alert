"""Run every frozen unit test plus the deployed persistence/spec-review safety tests."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import yaml


def run(command, cwd, log):
    with log.open('w') as stream:
        result=subprocess.run(command,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT)
    lines=log.read_text().splitlines()
    print('\n'.join(lines[-8:]),flush=True)
    if result.returncode:raise SystemExit(result.returncode)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--model-root',required=True);p.add_argument('--control',required=True);p.add_argument('--candidate',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out);out.mkdir(parents=True,exist_ok=False)
    path='.github/workflows/okx-v09-forward-scheduled.yml'
    reviewed=(Path(a.control)/path).read_bytes()
    assert reviewed==(Path(a.candidate)/path).read_bytes(),'production workflow must not change in phase 7'
    workflow=yaml.load(reviewed,Loader=yaml.BaseLoader)
    body=next(s['run'] for s in workflow['jobs']['forward']['steps'] if s.get('name','').startswith('Materialize operational handoff'))
    # Approved control emits test helpers in backtest/, outside the frozen v09 source manifest.
    subprocess.run(['bash','-e','-c',body],cwd=a.model_root,check=True)
    run([sys.executable,'-m','unittest','discover','-s','.','-p','test*.py','-v'],a.model_root,out/'frozen-all-tests.log')
    run([sys.executable,'-m','unittest','test_spec_review','-v'],a.model_root,out/'spec-review-tests.log')
    run([sys.executable,'scheduled_handoff.py','--self-test'],a.model_root,out/'handoff-tests.log')
