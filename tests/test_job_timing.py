"""Job timing is frozen at ended, never extended by later browser viewing."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
NODE=shutil.which('node') or 'C:/Users/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
SCRIPT=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const helpers=source.slice(source.indexOf('function jobTimingInfo('),source.indexOf('function safeMessage('));
const context={Date,Number,Math,P:null};vm.createContext(context);vm.runInContext(helpers,context);
const error={state:'error',created:999,started:1000,ended:1133.43,progress:.98};
assert.ok(Math.abs(context.jobTimingInfo(error,4600).elapsed_seconds-133.43)<.00001);
assert.equal(context.jobTimingLabel(error,4600),'Thời gian xử lý 2 phút 13 giây');
assert.equal(context.jobTimingLabel(error,99999),context.jobTimingLabel(error,4600));
assert.ok(context.jobEndedLabel(error).includes('Dừng do lỗi lúc'));
assert.equal(context.jobTimingInfo({state:'error',started:1000},4600).elapsed_seconds,null);
assert.equal(context.jobTimingInfo({state:'error',started:2000,ended:1000},4600).elapsed_seconds,null);
assert.equal(context.jobTimingLabel({state:'running',started:1000},1061),'Đã xử lý 1 phút 1 giây');
assert.equal(context.jobTimingLabel({state:'queued',created:1000},1061),'Chờ 1 phút 1 giây');
assert.equal(context.processingDuration(3600),'1 giờ 0 phút 0 giây');
console.log('PASS stopped duration, missing/reversed timestamps, running and queue timing');
'''


class JobTimingTests(unittest.TestCase):
    @unittest.skipUnless(Path(NODE).is_file(),'Node unavailable')
    def test_error_duration_does_not_keep_growing_and_active_duration_does(self):
        result=subprocess.run([NODE,'-e',SCRIPT,str(ROOT/'static/studio.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr or result.stdout)


if __name__=='__main__':unittest.main()
