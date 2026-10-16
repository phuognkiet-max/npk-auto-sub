// Execute production review, continuation and setup handlers with private
// DOM/API stubs. No browser, provider, credentials or application store access.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const studio = fs.readFileSync(path.join(__dirname, '../static/studio.js'), 'utf8');
const setup = fs.readFileSync(path.join(__dirname, '../static/single-video-setup.js'), 'utf8');
const start = studio.indexOf('async function preRenderReviewDialog(identifier) {');
const end = studio.indexOf('function cleanSlider(', start);
assert.ok(start >= 0 && end > start);
const helper = studio.slice(start, end);
const clone = value => JSON.parse(JSON.stringify(value));
function fixture(options = {}) {
  const project = {id: 'private-fixture', updated: 20, params: {target_language: 'Tiếng Việt',
    voice_gain: 1, original_gain: .35, burn_subtitles: true, use_clean: true},
    artifacts: {tts: {job_id: 'audio-owner'}, clean: {job_id: 'picture-owner'}},
    segments: [{start: 0, end: 2, text: '<img onerror="bad()"> Source', translated: 'Translated dialogue @Account'},
               {start: 2, end: 4, text: 'Credit', translated: 'Credit', speech_enabled: false}]};
  const report = {project_id: project.id, expected_revision: options.staleReport ? 21 : 20,
    signature: 'a'.repeat(64), job_id: 'prepared-job', can_approve: !options.blocked, approved: false,
    reason: 'Need preparation', narration: [{index: 0, text: 'Translated dialogue', enabled: true},
                                          {index: 1, text: '', enabled: false}]};
  if (options.badNarration) report.narration = [{index: 1, text: 'Wrong index', enabled: true}];
  const calls = [], notices = [], elements = {'#preRenderHumanApproval': {checked: false}, '#saved': {}};
  const ctx = {P: project, editVersion: 0, dirty: false, saving: null, conflict: false,
    modal: null, continued: [], selected: new Set(), quality: {}, mixEnabled: false,
    activeProjectPipeline: () => false, save: async () => {},
    api: async (url, body) => {
      calls.push({url, body});
      if (!body) {if (options.editOnGet) {ctx.editVersion++;ctx.dirty = true;} return report;}
      if (options.postConflict) {const error = new Error('HTTP 409 stale version');error.status = 409;throw error;}
      const saved = clone(project); saved.updated = 21;
      if (options.editOnPost) {ctx.P.segments[0].translated = 'Unsaved edited draft';ctx.editVersion++;ctx.dirty = true;}
      if (options.switchOnPost) ctx.P = {id: 'another-project', updated: 80};
      return {project: saved, expected_revision: 21, approved: true, can_continue: !options.blockAfterPost,
              reason: 'Prepared payload changed'};
    },
    esc: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    clock: value => String(value), mediaUrl: url => url + '?token=synthetic',
    selectedSpeechText: row => row.translated || row.text || '',
    $: selector => elements[selector] ||= {},
    openModal: (title, html, buttons) => {ctx.modal = {title, html, buttons};}, closeModal: () => {},
    setArea: area => {ctx.area = area;}, setProgressExpanded: () => {},
    toggleMix: async () => {ctx.mixEnabled = true;},
    notice: (...args) => notices.push(args), renderPanel: () => {}, drawCaption: () => {}, updateUndo: () => {},
    continuePausedPipeline: async id => {ctx.continued.push({id, revision: ctx.P.updated});},
  };
  vm.createContext(ctx);vm.runInContext(helper, ctx);
  const confirm = () => ctx.modal.buttons.find(button => button.label === 'Duyệt và xuất video');
  return {ctx, calls, notices, report, elements, confirm};
}
async function run() {
  let f = fixture();await f.ctx.preRenderReviewDialog('prepared-job');
  assert.equal(f.calls.length, 1);
  assert.ok(f.ctx.modal.html.includes('picture-owner/clean.mp4'));
  assert.ok(f.ctx.modal.html.includes('audio-owner/voice.wav'));
  assert.ok(f.ctx.modal.html.includes('Không lồng tiếng'));
  assert.ok(f.ctx.modal.html.includes('Translated dialogue @Account'));
  assert.ok(f.ctx.modal.html.includes('data-pre-render-narration="0">Translated dialogue</td>'));
  assert.ok(!f.ctx.modal.html.includes('data-pre-render-narration="0">Translated dialogue @Account'));
  assert.ok(f.ctx.modal.html.includes('&lt;img onerror='));
  assert.ok(!f.ctx.modal.html.includes('<img onerror='));
  assert.ok(!/id="preRenderHumanApproval"[^>]*checked/.test(f.ctx.modal.html));
  await assert.rejects(f.confirm().run(), /Chọn xác nhận/);
  assert.equal(f.calls.length, 1);assert.equal(f.ctx.continued.length, 0);
  f.elements['#preRenderHumanApproval'].checked = true;await f.confirm().run();
  assert.equal(f.calls.length, 2);assert.equal(f.calls[1].body.expected_revision, 20);
  assert.equal(f.calls[1].body.approved, true);assert.equal(f.calls[1].body.signature, f.report.signature);
  assert.equal(f.ctx.P.updated, 21);assert.equal(f.ctx.continued[0].revision, 21);

  for (const options of [{staleReport: true}, {editOnGet: true}, {blocked: true}, {badNarration: true}]) {
    f = fixture(options);await assert.rejects(f.ctx.preRenderReviewDialog('prepared-job'));
    assert.equal(f.ctx.modal, null);assert.equal(f.calls.length, 1);
  }
  f = fixture();await assert.rejects(f.ctx.preRenderReviewDialog('old-prepared-job'), /kết quả chuẩn bị khác/);
  assert.equal(f.ctx.modal, null);

  f = fixture();await f.ctx.preRenderReviewDialog('prepared-job');
  f.elements['#preRenderHumanApproval'].checked = true;f.ctx.editVersion++;f.ctx.dirty = true;
  await assert.rejects(f.confirm().run(), /Nội dung đã đổi/);
  assert.equal(f.calls.length, 1);assert.equal(f.ctx.continued.length, 0);

  f = fixture({editOnPost: true});await f.ctx.preRenderReviewDialog('prepared-job');
  f.elements['#preRenderHumanApproval'].checked = true;await f.confirm().run();
  assert.equal(f.ctx.P.segments[0].translated, 'Unsaved edited draft');
  assert.equal(f.ctx.conflict, true);assert.equal(f.ctx.continued.length, 0);

  f = fixture({switchOnPost: true});await f.ctx.preRenderReviewDialog('prepared-job');
  f.elements['#preRenderHumanApproval'].checked = true;await f.confirm().run();
  assert.equal(f.ctx.P.id, 'another-project');assert.equal(f.ctx.continued.length, 0);

  f = fixture({postConflict: true});await f.ctx.preRenderReviewDialog('prepared-job');
  f.elements['#preRenderHumanApproval'].checked = true;
  await assert.rejects(f.confirm().run(), /409/);
  assert.equal(f.ctx.P.updated, 20);assert.equal(f.ctx.continued.length, 0);

  f = fixture({blockAfterPost: true});await f.ctx.preRenderReviewDialog('prepared-job');
  f.elements['#preRenderHumanApproval'].checked = true;
  await assert.rejects(f.confirm().run(), /payload changed/);
  assert.equal(f.ctx.continued.length, 0);

  f = fixture();await f.ctx.preRenderReviewDialog('prepared-job');
  await f.ctx.modal.buttons.find(button => button.label === 'Mở trình biên tập').run();
  assert.equal(f.ctx.area, 'subtitles');assert.equal(f.ctx.subtab, 'content');
  assert.equal(f.calls.length, 1);assert.equal(f.ctx.continued.length, 0);
  await f.ctx.modal.buttons.find(button => button.label === 'Nghe phối âm').run();
  assert.equal(f.ctx.area, 'voice');assert.equal(f.ctx.mixEnabled, true);
  assert.equal(f.calls.length, 1);assert.equal(f.ctx.continued.length, 0);

  // Execute the same presentation/enabling helpers that both setup and job
  // details call; a pending human decision is actionable, not a failed job.
  const setupContext = {window: {}};vm.createContext(setupContext);vm.runInContext(setup, setupContext);
  const ui = setupContext.window.NPKSingleVideoSetup;
  const pending = {review_stage: 'pre_render', can_approve: true, can_continue: false, approved: false};
  assert.equal(ui.continuationPresentation(pending, 381).button, 'Xem và duyệt để xuất');
  assert.equal(ui.continuationCanAct(pending), true);
  assert.equal(ui.continuationCanAct({...pending, can_approve: false}), false);
  assert.equal(ui.continuationPresentation({...pending, approved: true}, 381).button, 'Xuất video đã duyệt');
  assert.equal(ui.continuationCanAct({...pending, approved: true, can_continue: true}), true);
  assert.equal(ui.continuationCanAct({can_continue: false}), false);

  // Execute setup Run itself: pending review opens the explicit dialog without
  // saving workflow overrides, re-running preparation or queuing any work.
  const runStart = setup.indexOf('    find("setupRun").onclick=guard(async()=>{');
  const runEnd = setup.indexOf('    visibility();guard(refresh)();', runStart);
  assert.ok(runStart >= 0 && runEnd > runStart);
  const runControl = {}, counters = {review: 0, continued: 0, refresh: 0};
  const runContext = {reviewBeforeRender: async () => counters.review++,
    continuePipeline: async () => counters.continued++};
  const handlerFactory = new Function('find', 'guard', 'activeJob', 'ctx', 'refresh', 'prepare',
    'notice', 'plan', 'let running=false,restartRecognition=false,continuation=plan;\n' +
    setup.slice(runStart, runEnd) + '\nreturn find("setupRun").onclick;');
  let runHandler = handlerFactory(() => runControl, fn => fn, () => false, runContext,
    async () => counters.refresh++, async () => {throw new Error('Unexpected new preparation');},
    () => {}, {...pending, job_id: 'prepared-job'});
  await runHandler();assert.equal(counters.review, 1);assert.equal(counters.continued, 0);
  runHandler = handlerFactory(() => runControl, fn => fn, () => false, runContext,
    async () => counters.refresh++, async () => {throw new Error('Unexpected new preparation');},
    () => {}, {...pending, job_id: 'prepared-job', approved: true});
  await runHandler();assert.equal(counters.continued, 1);assert.equal(counters.refresh, 2);

  const continueStart = studio.indexOf('async function continuePausedPipeline(identifier) {');
  assert.ok(continueStart >= 0 && continueStart < start);
  const continuedContext = {P: {id: 'private-fixture', updated: 20}, dirty: false, saving: null, editVersion: 0,
    activeProjectPipeline: () => false, save: async () => {}, calls: [], reviews: 0,
    api: async (url, body) => {
      continuedContext.calls.push({url, body});
      return {...pending, project_id: 'private-fixture', expected_revision: 20};
    }, preRenderReviewDialog: async () => {continuedContext.reviews++;}};
  vm.createContext(continuedContext);vm.runInContext(studio.slice(continueStart, start), continuedContext);
  await continuedContext.continuePausedPipeline('prepared-job');
  assert.equal(continuedContext.reviews, 1);assert.equal(continuedContext.calls.length, 1);
  assert.equal(continuedContext.calls[0].body, undefined);

  const againStart = setup.indexOf('    find("setupPrepareAgain").onclick = guard(async () => {');
  const againEnd = setup.indexOf('    find("setupReview").insertAdjacentHTML("afterend", \'<button id="setupAutoSubtitles"', againStart);
  assert.ok(againStart >= 0 && againEnd > againStart);
  const againControls = {}, againCalls = [], againProject = {id: 'edited-private-project', params: {
    pipeline_extract: 'ocr', subtitle_automation: 'local', translation_model: 'chosen-model'},
    segments: [{text: 'Source edited', translated: 'Saved translation'}]};
  const priorRows = clone(againProject.segments);
  const againHandler = new Function('find', 'guard', 'activeJob', 'ctx', 'change', 'prepare', 'mounted',
    'queue', 'notice', 'refresh', 'let running=false;\n' + setup.slice(againStart, againEnd) +
    '\nreturn find("setupPrepareAgain").onclick;')(
      id => againControls[id] ||= {}, fn => fn, () => false, {project: () => againProject},
      fn => fn(againProject), async () => {}, () => true,
      async (...args) => againCalls.push(args), () => {}, async () => {});
  await againHandler();
  assert.equal(againProject.params.pipeline_extract, 'existing');
  assert.equal(againProject.params.subtitle_automation, 'manual');
  assert.equal(againProject.params.translation_model, 'chosen-model');
  assert.deepEqual(againProject.segments, priorRows);assert.equal(againCalls[0][0], 'pipeline');

  // Execute the production original-audio toggle, verifying that mute does
  // not replace the previous mix gain with a hard-coded value on unmute.
  const toggleStart = setup.indexOf('    find("setupKeepOriginal").onchange = guard(async event => {');
  const toggleEnd = setup.indexOf('\n    });', toggleStart) + '\n    });'.length;
  assert.ok(toggleStart >= 0 && toggleEnd > toggleStart);
  const controls = {'setupKeepOriginal': {}, 'setupOriginalGain': {}}, memory = new Map();
  const row = {id: 'audio-toggle-fixture', params: {original_gain: .35}};
  new Function('find', 'ctx', 'change', 'guard', 'originalGains', 'refresh', setup.slice(toggleStart, toggleEnd))(
    id => controls[id], {project: () => row}, fn => fn(row), fn => fn, memory, async () => {});
  await controls.setupKeepOriginal.onchange({target: {checked: false}});assert.equal(row.params.original_gain, 0);
  await controls.setupKeepOriginal.onchange({target: {checked: true}});assert.equal(row.params.original_gain, .35);

  console.log(JSON.stringify({production_review_and_setup_executed: true, scenarios: 22,
    provider_calls: 0, native_interactions: 0, production_store_access: false}));
}
run().catch(error => {console.error(error);process.exitCode = 1;});
