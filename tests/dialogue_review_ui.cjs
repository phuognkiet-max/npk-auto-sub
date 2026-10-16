// Exercise the production dialog with isolated DOM/API stubs, without a browser
// or any access to the application store, provider, credentials or voice engine.
const fs = require('node:fs'), path = require('node:path');
const vm = require('node:vm'), assert = require('node:assert/strict');
const script = fs.readFileSync(path.join(__dirname, '../static/studio.js'), 'utf8');
const start = script.indexOf('async function dialogueReviewDialog() {');
const end = script.indexOf('function speechSourceName(', start);
assert.ok(start >= 0 && end > start);
const helper = script.slice(start, end);
function fixture(options = {}) {
  const project = {id: 'synthetic', updated: 20, params: {}, segments: Array.from({length: 5}, (_, i) => ({
    text: `Nguồn ${i + 1} <img onerror="bad()">`, translated: `Lời ${i + 1}`, start: i, end: i + 1}))};
  const proposal = {id: 'a'.repeat(64), kind: 'duplicate_group', indices: [0, 1, 2, 3, 4], keep_index: 0,
    message: 'Gợi ý <script>bad()</script>', changes: [1,2,3,4].map(index => ({index, speech_enabled: false}))};
  const report = {expected_revision: options.staleReport ? 21 : 20, signature: 'b'.repeat(64),
    proposals: options.empty ? [] : [proposal], issues: []};
  const calls = [], notices = [], checked = [], elements = {};
  const ctx = {P: project, editVersion: 0, dirty: false, saving: null, conflict: false,
    quality: {}, selected: new Set(), modal: null, remembered: 0,
    save: async () => {}, selectedSpeechText: row => row.translated,
    esc: value => String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'),
    $$: () => checked, $: selector => elements[selector] ||= {},
    openModal: (title, html, buttons) => { ctx.modal = {title, html, buttons}; }, closeModal: () => {},
    remember: () => ctx.remembered++, renderPanel: () => {}, drawCaption: () => {}, updateUndo: () => {},
    notice: (...args) => notices.push(args),
    api: async (url, body) => {
      calls.push({url, body});
      if (!body) { if (options.editOnGet) {ctx.editVersion++;ctx.dirty = true;} return report; }
      const result = JSON.parse(JSON.stringify(project));
      result.updated = 21; result.segments[1].speech_enabled = false;
      if (options.editOnApply) {ctx.P.segments[0].translated = 'Bản đang nhập';ctx.editVersion++;ctx.dirty = true;}
      return result;
    }};
  vm.createContext(ctx); vm.runInContext(helper, ctx);
  return {ctx, calls, checked, notices, report, proposal};
}
async function run() {
  let f = fixture(); await f.ctx.dialogueReviewDialog();
  assert.equal(f.calls.length, 1); assert.equal(f.ctx.modal.buttons.length, 2);
  assert.ok(!/data-dialogue-proposal="[^"]+" checked/.test(f.ctx.modal.html));
  assert.ok(f.ctx.modal.html.includes('Nguồn 5 &lt;img'));
  assert.ok(f.ctx.modal.html.includes('Lời 5'));
  assert.ok(f.ctx.modal.html.includes('đề xuất tắt đọc'));
  assert.ok(f.ctx.modal.html.includes('giữ đọc'));
  assert.ok(!f.ctx.modal.html.includes('<script>bad()'));
  assert.ok(!f.ctx.modal.html.includes('<img onerror'));
  await assert.rejects(f.ctx.modal.buttons[1].run(), /Chọn ít nhất/);
  assert.equal(f.calls.length, 1);
  f.checked.push({dataset: {dialogueProposal: f.proposal.id}});
  await f.ctx.modal.buttons[1].run();
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[1].url, '/api/studio/projects/synthetic/dialogue-review/apply');
  assert.equal(f.calls[1].body.expected_revision, 20);
  assert.equal(f.calls[1].body.signature, f.report.signature);
  assert.deepEqual(Array.from(f.calls[1].body.proposal_ids), [f.proposal.id]);
  assert.equal(f.ctx.P.updated, 21); assert.equal(f.ctx.remembered, 1);
  assert.equal(f.ctx.P.segments[1].speech_enabled, false);

  f = fixture({staleReport: true});
  await assert.rejects(f.ctx.dialogueReviewDialog(), /Nội dung đã đổi/);
  assert.equal(f.ctx.modal, null); assert.equal(f.calls.length, 1);

  f = fixture({editOnGet: true});
  await assert.rejects(f.ctx.dialogueReviewDialog(), /Nội dung đã đổi/);
  assert.equal(f.ctx.modal, null);

  f = fixture(); await f.ctx.dialogueReviewDialog();
  f.checked.push({dataset: {dialogueProposal: f.proposal.id}}); f.ctx.P.updated = 22;
  await assert.rejects(f.ctx.modal.buttons[1].run(), /Nội dung đã đổi/);
  assert.equal(f.calls.length, 1);

  f = fixture({editOnApply: true}); await f.ctx.dialogueReviewDialog();
  f.checked.push({dataset: {dialogueProposal: f.proposal.id}});
  await f.ctx.modal.buttons[1].run();
  assert.equal(f.ctx.P.segments[0].translated, 'Bản đang nhập');
  assert.equal(f.ctx.conflict, true); assert.equal(f.ctx.remembered, 0);
  assert.equal(f.notices.length, 1);

  f = fixture({empty: true}); await f.ctx.dialogueReviewDialog();
  assert.equal(f.ctx.modal.buttons.length, 1); assert.equal(f.calls.length, 1);
  console.log(JSON.stringify({production_dialog_executed: true, scenarios: 7,
    provider_calls: 0, production_store_access: false}));
}
run().catch(error => {console.error(error);process.exitCode = 1;});
