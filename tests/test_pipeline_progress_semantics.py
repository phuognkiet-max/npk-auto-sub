"""Raw OCR completion and semantic selection failures remain distinct in the UI."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node') or 'C:/Users/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
SCRIPT = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
const context={window:{}, Date, Number, Math, P:null, esc:value=>String(value),
    stateNames:{error:'Đã dừng do lỗi'}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const setup=context.window.NPKSingleVideoSetup;
const pending={completed_steps:['ocr'],remaining_steps:['auto_subtitles','translate','tts','clean','render'],
    next_action:'continue_auto_subtitles',recovery_job_id:'failed',reused_translation_count:0,
    missing_translation_count:0,recognition_complete:true,recognition_cue_count:375};
let view=setup.continuationPresentation(pending,0);
assert.equal(view.selectionPending,true);
assert.match(view.savedText,/375 câu nhận dạng/);
assert.doesNotMatch(view.savedText,/0 câu phụ đề/);
assert.equal(view.translationText,'');
assert.equal(view.button,'Tiếp tục tự xử lý phụ đề');
assert.match(view.reason,/kết quả nhận dạng đã lưu/);
assert.equal(setup.stepNames.auto_subtitles,'Tự xử lý phụ đề');
view=setup.continuationPresentation({...pending,recognition_cue_count:undefined},0);
assert.match(view.savedText,/Đã lưu kết quả nhận dạng/);
assert.equal(view.translationText,'');
view=setup.continuationPresentation({...pending,recognition_cue_count:undefined,recognition_count:375},0);
assert.match(view.savedText,/375 câu nhận dạng/);
view=setup.continuationPresentation({completed_steps:['ocr','auto_subtitles'],
    remaining_steps:['translate','tts','clean','render'],recovery_job_id:'failed',
    reused_translation_count:20,missing_translation_count:355},375);
assert.equal(view.selectionPending,false);
assert.equal(view.savedText,'Đã lưu 375 câu phụ đề.');
assert.match(view.translationText,/Giữ 20 câu dịch đã lưu; còn 355 câu cần dịch/);
assert.equal(view.button,'Tiếp tục các bước còn lại');
view=setup.continuationPresentation({completed_steps:['ocr','auto_subtitles','translate'],
    reused_translation_count:381,missing_translation_count:0,recognition_count:381},375);
assert.match(view.savedText,/Đã lưu 381 câu phụ đề/);
assert.match(view.savedText,/Bảng biên tập hiện có 375 câu/);
assert.equal(setup.continuationPresentation({recovery_job_id:'failed'},0).translationText,'');
assert.equal(setup.continuationSubtitleCount({job_id:'failed',reused_translation_count:0,
    missing_translation_count:375},0),'375 câu trong checkpoint · 0 đã dịch, 375 cần dịch');
assert.equal(setup.continuationSubtitleCount({job_id:'failed',reused_translation_count:2,
    missing_translation_count:373},10),null);
const studio=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(studio.slice(studio.indexOf('function connectionContractText('), studio.indexOf('function refreshConnectionReceipt(')), context);
vm.runInContext(studio.slice(studio.indexOf('function jobTimingInfo('),studio.indexOf('function safeMessage(')),context);
const failed={kind:'pipeline',state:'error',stage:'error',failure_stage:'auto_subtitles',
    started:1000,ended:1045,progress:.2};
assert.equal(context.jobFailureStage(failed).name,'Tự xử lý phụ đề');
assert.equal(context.jobFailureStage({...failed,stage:'ocr'}).id,'auto_subtitles');
const html=context.jobDiagnostics(failed);
assert.match(html,/data-job-failure-stage="auto_subtitles"/);
assert.match(html,/Dừng ở bước: Tự xử lý phụ đề/);
assert.doesNotMatch(html,/Nhận dạng chưa hoàn tất|0 câu dịch/);
assert.doesNotMatch(html,/data-job-api-usage/,'Old jobs must not invent a zero request count.');
const metered=context.jobDiagnostics({...failed,api_usage:{request_attempts:7,request_limit:24}});
assert.match(metered,/data-job-api-usage="true"/);
assert.match(metered,/7\/24 lượt API/);
assert.match(metered,/chưa xác nhận chi phí/);
assert.doesNotMatch(context.jobDiagnostics({...failed,api_usage:{request_attempts:'bad',request_limit:24}}),/data-job-api-usage/);
const continued=context.jobDiagnostics({...failed,api_usage:{request_attempts:2,request_limit:24,
    lineage_request_attempts:8,lineage_request_limit:24,history_unknown_jobs:0,
    reused_cues:5,newly_completed_cues:0,remaining_cues:370}});
assert.match(continued,/8\/24 lượt API của quy trình/);
assert.match(continued,/5 câu dùng lại · 0 câu dịch mới · 370 câu còn thiếu/);
const unknown=context.jobDiagnostics({...failed,api_usage:{lineage_request_attempts:2,lineage_request_limit:24,history_unknown_jobs:3}});
assert.match(unknown,/Lịch sử các lượt cũ chưa đầy đủ/);
assert.doesNotMatch(context.jobDiagnostics({...failed,api_usage:{reused_cues:'bad',newly_completed_cues:0,remaining_cues:370}}),/data-job-translation-counts/);
assert.equal(context.jobFailureStage({...failed,failure_stage:undefined}),null);
assert.equal(context.jobFailureStage({...failed,failure_stage:undefined,stage:'2/4:tts'}).id,'tts');
const historical = context.jobDiagnostics({...failed,id:'old-deepseek-job',created:1000,
    configuration:{provider:'deepseek',translation_model:'deepseek-flash'}});
assert.match(historical,/deepseek · deepseek-flash/);
assert.match(historical,/old-deepseek-job/);
assert.match(historical,/Tạo lúc/);
console.log('PASS semantic selection failure status, saved OCR counts, continuation action and explicit failure stage');
'''


class PipelineProgressSemanticsTests(unittest.TestCase):
    @unittest.skipUnless(Path(NODE).is_file(), 'Node unavailable')
    def test_saved_ocr_remains_complete_when_gemini_selection_fails(self):
        result = subprocess.run([NODE, '-e', SCRIPT,
            str(ROOT/'static/single-video-setup.js'), str(ROOT/'static/studio.js')],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == '__main__':
    unittest.main()
