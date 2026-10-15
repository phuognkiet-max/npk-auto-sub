'use strict';
// The old editor is still available at /editor. Both layouts use the same engine.
if (location.pathname !== '/editor') {
  document.body.classList.add('workflow');
  const get = id => document.getElementById(id);
  const make = (tag, id, text) => { const e=document.createElement(tag); if(id)e.id=id; if(text)e.textContent=text; return e; };
  const css=make('link'); css.rel='stylesheet'; css.href='/static/workflow.css'; document.head.append(css);
  get('topbar').querySelector('strong small').textContent='0.6 · Quy trình video';
  const voices=make('button','voiceSettings','Kho giọng'); voices.hidden=true;get('settings').before(voices);
  get('export').hidden=true;get('pipelineSettings').textContent='Chạy cả quy trình';get('pipelineSettings').classList.add('primary');
  const tools=get('tools'); tools.setAttribute('aria-label','Các bước xử lý');
  for(const [key,label] of [['media','1  Nguồn'],['sub','2  Tách sub'],['translate','3  Dịch'],['clean','4  Che sub'],['voice','5  Lồng tiếng'],['audio','6  Xuất']]) {
    const b=tools.querySelector(`[data-tool="${key}"]`); b.textContent=label; tools.append(b);
  }
  get('topbar').after(tools);
  const side=make('section','stagePanel'); side.setAttribute('aria-label','Công việc hiện tại');
  const viewport=get('workspace'); viewport.append(side);
  const inspector=get('inspector');
  const description=make('p','stageDescription'); description.className='muted';
  inspector.querySelector('.panel-head').after(description);
  const options=make('details','stageOptions');
  options.append(make('summary',null,'Thông số khâu này'),get('parameters'));
  inspector.append(options); get('actions').before(options);
  const objectDetails=make('details','objectDetails');objectDetails.open=true;
  objectDetails.append(make('summary','objectDetailsTitle','Chỉnh câu / vùng được chọn'),get('properties'));
  description.after(get('actions'));get('stageOptions').before(objectDetails);
  const essentials=make('div','essentials');get('actions').after(essentials);
  const content=make('div','stageContent');side.append(content);content.append(inspector);
  const list=get('listPanel');
  const listToolbar=make('div','objectToolbar');
  listToolbar.append(get('selectionLabel'),get('toStart'),get('toEnd'),get('splitObject'),get('duplicateObject'),get('deleteObject'));
  const follow=make('label'); follow.className='inline';
  const followInput=make('input','followVideo'); followInput.type='checkbox'; followInput.checked=true;
  follow.append(followInput,document.createTextNode('Bám theo video'));
  list.querySelector('.panel-head').append(follow);
  get('searchObjects').after(listToolbar);
  const pages=make('div','listPages'); pages.className='row';
  pages.append(make('button','listPrevious','Trang trước'),get('listCount'),make('button','listNext','Trang sau'));
  list.append(pages); content.append(list);
  const next=make('div','stageNavigation');
  next.append(make('button','previousStage','← Quay lại'),make('span','stageSummary'),make('button','nextStage','Tiếp tục →'));
  side.append(next);
  const layers=make('details','previewLayers'); layers.append(make('summary',null,'Lớp xem / nghe thử'),get('trackHeads'));
  get('viewer').append(layers);
  get('resetLayout').textContent='Cân lại bố cục'; get('viewer').querySelector('.viewer-controls').append(get('resetLayout'));
  // Remove unused timeline layout/render work, but retain inert IDs for /editor compatibility.
  get('timelinePanel').hidden=true; get('timelineSplitter').hidden=true; get('leftSplitter').hidden=true;
  get('rightSplitter').setAttribute('aria-label','Chia kích thước video và chức năng');
  get('rightSplitter').tabIndex=0;get('rightSplitter').setAttribute('aria-orientation','vertical');
  get('rightSplitter').setAttribute('aria-valuemin','35');get('rightSplitter').setAttribute('aria-valuemax','60');
  document.querySelector('footer').prepend(get('toast'));
  get('toggleList').hidden=true;
  const external=make('section','externalVoice'); external.className='external-voice';
  external.innerHTML='<h3>SaydiVoice / tệp lồng tiếng ngoài</h3><p class="notice">Dùng qua web · chưa kết nối API tự động. Xuất SRT đã dịch, tạo audio trên SaydiVoice rồi chọn tệp tải về. Video không được tự tải lên dịch vụ.</p><div class="row"><button id="exportVoiceSrt">Xuất SRT lồng tiếng</button><a href="https://voice.saydi.ai/vi/studio/subtitles/" target="_blank" rel="noopener noreferrer">Mở SaydiVoice ↗</a></div><label>Tệp audio hoàn chỉnh<input id="externalAudioPath" placeholder="D:\\Audio\\long-tieng.wav" autocomplete="off"></label><div class="row"><button id="pickExternalAudio">Chọn tệp…</button><button id="inspectExternalAudio">Kiểm tra tệp</button><button id="applyExternalAudio" class="primary">Kiểm tra & dùng tệp</button><button id="clearExternalAudio">Bỏ dùng tệp ngoài</button></div><p id="externalAudioStatus" class="notice" role="status">Chưa kiểm tra tệp. Thời lượng phù hợp không chứng minh từng câu đã khớp.</p><p class="muted">Dùng tệp ngoài sẽ bỏ bước tạo giọng offline trong quy trình. Đường dẫn được giữ nguyên; không sao chép hoặc xóa tệp.</p><audio id="externalAudioPlayer" controls preload="metadata" hidden></audio>';
  inspector.append(external);
}
