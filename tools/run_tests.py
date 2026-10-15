"""Run unittest in a fresh private store, never the desktop editor's DATA."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern',default='test_*.py')
    parser.add_argument('--verbose',action='store_true')
    parser.add_argument('--profile', choices=('unit', 'media', 'native'), default='unit',
                        help='unit blocks real processes/inference; media opts into fixture FFmpeg/Node; native permits offline runtime inference and loopback tests. Windows secrets/external network/production DATA stay denied.')
    args=parser.parse_args()
    base=(ROOT/'build/test-runs').resolve()
    base.mkdir(parents=True,exist_ok=True)
    private=(base/uuid.uuid4().hex).resolve()
    assert private.parent==base and private!=(ROOT/'data').resolve()
    private.mkdir()
    env=dict(os.environ,VIETDUB_DATA_DIR=str(private),VIETDUB_TOKEN='isolated-unit-tests',
             PYTHONUTF8='1',PYTHONIOENCODING='utf-8',NPK_TEST_PROFILE=args.profile)
    command=[sys.executable,'-X','utf8',str(ROOT/'tools/test_bootstrap.py'),'discover','-s',str(ROOT/'tests'),'-p',args.pattern]
    if args.verbose:command.append('-v')
    print(json.dumps({'test_data':str(private),'production_data_used':False,
                      'profile':args.profile,'host_boundaries':'installed before application imports'}),flush=True)
    result=subprocess.run(command,cwd=ROOT,env=env)
    # Retain diagnostics and fixture files for inspection. No automatic delete
    # or queue worker is started by this runner; release packages exclude build.
    raise SystemExit(result.returncode)

if __name__=='__main__':main()
