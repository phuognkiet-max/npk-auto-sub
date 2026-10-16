# Đợt 1: Làm sạch OCR, gộp câu và duyệt trước render — RC10

Ngày triển khai: 16/10/2026. Phạm vi là tầng văn bản từ nhận dạng tới dịch/TTS và cổng duyệt trước xuất. Không thay thuật toán xóa watermark trên hình, vùng che, lựa chọn provider/model, giọng hay giới hạn nhịp request hiện có.

## Luồng hiện tại

```text
Video / SRT / bảng hiện có
  → OCR hoặc ASR nếu người dùng chọn nhận dạng
  → chọn lớp phụ đề nếu dùng OCR tự động
  → chuẩn hóa văn bản + gộp nguồn + báo cáo trước/sau
  → API dịch theo ngôn ngữ đích đã chọn
  → làm sạch lời đọc theo đúng nguồn source/translated/edited
  → tắt đọc credit/câu dịch lặp, tạo hoặc dùng lại raw-audio cache
  → xử lý vùng chữ theo lựa chọn đã lưu
  → DỪNG: xem phụ đề, hình và nghe phối âm
  → người dùng duyệt đúng phiên bản
  → render-only + kiểm giải mã/âm thanh/thời lượng/hash + lưu nơi xuất
```

Đây là quy tắc chung theo văn bản, thời gian và metadata. Không có nhánh nhận diện tên video, project ID, một đoạn hội thoại hay một ngôn ngữ đích cụ thể. Các bộ nhận dạng/model dịch/giọng vẫn có phạm vi ngôn ngữ riêng; làm sạch Unicode không tự bổ sung giọng TTS cho mọi ngôn ngữ.

## Bộ làm sạch dùng chung

`subtitle_text.py` là module thuần không tải model hoặc gọi dịch vụ. `cleaner.py` tái xuất `sanitize_subtitle_text`; `speech_delivery.spoken_text` luôn dùng cùng hàm trước khi tạo lời đọc, kể cả dự án cũ chưa bật tùy chọn làm sạch. Điều này ngăn dấu `@` và handle lọt vào giọng hoặc cache.

- Xóa handle theo mẫu yêu cầu `@[A-Za-z0-9_\u4e00-\u9fff.\-]+`, mở rộng cho ký tự Unicode có dấu và các chữ viết khác; xóa mọi ký tự `@` còn lại. NFKC, bỏ ký tự điều khiển/nhiễu, giữ dấu và ký tự kết nối cần thiết của chữ viết.
- Nhận các nhãn `来源:`, `视频来源:`, `文字来源:`, `图片来源:`, `公文编号:`, `Nguồn video:` và một số dạng credit tương đương. Cả dấu hai chấm ASCII/full-width được hỗ trợ.
- Credit-only giữ văn bản để dịch/hiển thị nhưng đặt `speech_enabled=False` và không được đọc TTS. Credit dính vào câu chính bị cắt khỏi phần thoại; dòng credit riêng đi kèm câu chính bị bỏ, giữ phần thoại.
- Dòng watermark-only không còn nội dung, không có bản dịch/lời biên tập/metadata được bảo vệ, được bỏ khỏi bảng nguồn với bản gốc lưu trong audit; không gửi chuỗi rỗng cho dịch vụ. Nếu đã có nội dung biên tập cần giữ thì giữ dòng và yêu cầu kiểm tra.

## Gộp nguồn và khử đọc lặp

`clean_timing.prepare_subtitle_segments` trả bảng mới, ánh xạ `source_indices`, `removed_indices`, toàn bộ thay đổi trước/sau và lý do cần review. Đầu vào không bị sửa tại chỗ.

| Quy tắc | Điều kiện |
|---|---|
| So sánh | Case-fold Unicode, bỏ dấu câu/khoảng trắng/ký hiệu nhiễu, giữ chữ/dấu phụ/số |
| Gộp trùng nguồn | Hai câu liền kề, gap ≤1,5 giây, span nhóm ≤12 giây; `SequenceMatcher(autojunk=False)` ≥0,80 hoặc chuỗi ngắn nằm trọn chuỗi dài |
| Kết quả trùng nguồn | Một dòng, min(start), max(end), chọn bản chữ sạch nhất; lặp tới điểm ổn định |
| Mảnh câu | Thời lượng <1 giây hoặc <3 từ, hoặc ≤3 ký tự Hán; với chữ Nhật/Thái không có khoảng trắng không coi cả câu dài là một từ |
| Hàn mảnh | Hàng liền kề, gap ≤0,35 giây, tổng span ≤6 giây; ưu tiên hàng gần nhất, giữ thứ tự nội dung |
| Sau dịch / trước TTS | So sánh đúng nguồn lời đọc đã chọn; giữ ID/caption/mốc/thứ tự và đặt `speech_enabled=False` cho bản lặp thay vì đổi bảng caption |

Giới hạn 12 giây tính cả nhóm giữ lại, không kéo chuỗi lặp vô hạn qua video. Ngoài cửa sổ, gap lớn, hàng không được chọn hoặc khác vai đọc là ranh giới. Không gộp qua hàng ngoài scope. Gộp fuzzy, hàn mảnh và xung đột có đánh dấu cần đối chiếu.

Lời đã biên tập, giọng/nhân vật/tốc độ/ngắt nghỉ/style/vai khác nhau, bản dịch đã có không tương thích được bảo vệ. Hàn mảnh không tự gán một bản dịch cũ cho câu nguồn mới ghép. Các nhóm bị bảo vệ được ghi rõ để người dùng xử lý thay vì làm mất chỉnh sửa. Vì ngưỡng 0,80 và containment có thể gộp câu lặp có chủ đích hoặc khác một từ quan trọng, bước review và audit là cần thiết; không coi phép so chuỗi là hiểu nghĩa.

## Tích hợp và phục hồi

- `core.prepare_project_text` chạy trên bản sao, không lưu dữ liệu hoặc gọi dịch vụ.
- Lượt translate/pipeline bảng hiện có: queue làm sạch bản draft trước preflight, kiểm admission/credential/checkpoint/evidence, rồi mới lưu revision canonical và input.json. Queue validation không làm mất ID/custom metadata. Scope được remap theo ánh xạ nguồn.
- OCR thủ công, ASR và lựa chọn OCR tự động đã đủ điều kiện: worker chuẩn hóa bảng sau nhận dạng/chọn, trước khi tạo identity dịch. OCR manifest, nhận dạng checkpoint và SRT gốc vẫn giữ nguyên.
- Resume/retry không làm sạch lại thành một corpus mới để vượt danh tính checkpoint. Các phần đã dịch được giữ. Một câu trong scope không còn lời gốc bị chặn trước credential/provider.
- `speech_cleanup_report`, preflight, worker, signature và cache dùng cùng danh sách lời đọc hiệu lực. Dòng không đọc không resolve voice hoặc tạo cache key. Chạy lại có thể dùng raw-audio cache đã xác minh.
- Worker trả lại role dẫn xuất và `text-preparation-speech.json`, nên bản cần duyệt hiển thị đúng các câu không đọc. Recovery dùng TTS đã có cũng tái dựng role để reviewer thấy đúng lời của audio cache.
- Báo cáo `text-preparation-source.json`, `text-preparation-ocr.json`, `text-preparation-asr.json`, `text-preparation-auto_subtitles.json` được ghi theo bước; chỉ tệp phù hợp với lượt đang chạy xuất hiện. Báo cáo có thể chứa lời thoại và bản gốc riêng tư, không đưa lên GitHub.

## Review trước render

Dự án mới bật `subtitle_text_cleanup=True` và `review_before_render=True`. Snapshot cũ thiếu hai cờ giữ `False` để không âm thầm thay nội dung, chữ ký cache hay chính sách xuất. Có thể bật từng cờ ở Thiết lập & chạy. Có toggle **Giữ âm thanh gốc**, giữ mức gain dương trước đó khi tắt/bật lại trong phiên UI.

Sau các bước chuẩn bị, pipeline chuyển sang `review`, `review_stage=pre_render`, chỉ còn `remaining_steps=['render']`, có `pipeline-review.json` và prepared project. Chưa có final.mp4 và không báo đã xuất.

Nút **Xem và duyệt để xuất** mở hình nguồn/hình đã xử lý, giọng đã chuẩn bị, bảng gốc/bản dịch/lời đọc thực tế từ backend, liên kết tới trình biên tập và nghe phối âm. Hình xem trong modal là hình trước ghép; editor dùng để xem phụ đề và phối âm theo cấu hình. Checkbox xác nhận không được chọn sẵn. Đây là cổng review, không phải một bộ dựng timeline đầy đủ tương đương CapCut/Premiere/AE.

GET `/api/studio/projects/{id}/pre-render-review` chỉ kiểm tra. POST với `approved:true`, `job_id`, `expected_revision`, `signature` lưu quyết định, không enqueue. Sau khi UI nhận đúng revision mới, nó gọi endpoint continue-pipeline hiện có để chỉ render.

Chữ ký gắn nguồn, bảng đầy đủ, timing, vai đọc, lựa chọn giọng/phối âm/style/vùng xử lý và byte artifact TTS/clean đang dùng. Đổi nội dung hay artifact làm approval cũ không hợp lệ. Worker kiểm lại trước render, kể cả gọi tác vụ render riêng. Sửa trong lúc GET/POST không làm mất draft hoặc tự xuất phiên bản mới. **Chuẩn bị lại sau chỉnh sửa** dùng bảng hiện tại và phần còn thiếu, không tự chạy OCR lại. Tắt review là lựa chọn cấu hình của người dùng, không được dùng ngầm như một cách vượt lỗi.

## Kiểm chứng và giới hạn

Các test mới kiểm sanitizer Unicode, cả credit-only/inline, chuỗi ba câu bắt buộc thành một câu, các ranh giới gap/span, orphan, metadata/scope, điểm ổn định, đúng nguồn lời đọc, cache, admission/queue revision, human review và stale approval. Provider là fake, giọng fixture là PCM tổng hợp; không API key, provider hoặc inference thật trong bộ regression.

Kiểm thử trình duyệt riêng dùng video mẫu đi qua queue/worker thật: chuẩn bị → chưa có final → checkbox chưa chọn → duyệt → render-only → FFmpeg kiểm giải mã và công bố tệp. Dữ liệu fixture nằm riêng trong build, không sửa video của người dùng.

Kết quả chạy cụ thể được ghi trong báo cáo kiểm chứng của lượt triển khai. Những test này xác nhận hợp đồng luồng xử lý; không chứng minh mọi bản dịch đúng nghĩa, mọi TTS phát âm đúng hoặc mọi loại video đều đã được nghiệm thu. Không thay đổi chất lượng phục hồi nền/watermark trên hình ở đợt này.

Tham khảo thiết kế review: [kinh nghiệm giữ văn bản đã duyệt và đưa điểm không chắc ra review của người làm TimedSubs](https://dev.to/woshiliyana/asr-generated-subtitles-vs-forced-alignment-why-script-first-captions-fail-less-342i). Đây là kinh nghiệm triển khai cá nhân, không phải benchmark cho NPK.
