# Kiến trúc NPK Auto Sub

Tài liệu này mô tả mã nguồn hiện có của NPK Auto Sub, ứng dụng Windows để nhận dạng phụ đề, dịch, tạo giọng tiếng Việt, xử lý chữ gốc và xuất video. Tên thư mục/module lịch sử `VietDubStudio` vẫn xuất hiện trong mã để giữ tương thích dữ liệu. Đây là ứng dụng desktop chạy cục bộ, có thể gọi dịch vụ ngôn ngữ do người dùng chọn; không phải dịch vụ đám mây nhiều người dùng.

Đọc [hướng dẫn rà soát cho AI](AI_REVIEW_GUIDE.md) để biết những hợp đồng cần giữ, cách kiểm thử an toàn và các câu hỏi còn mở. Đọc [thành phần bên thứ ba](THIRD_PARTY.md) trước khi phân phối mã, runtime, model hoặc giọng.

## 1. Phạm vi sản phẩm

Người dùng có thể nhập một video, tạo hoặc nhập phụ đề, sửa bảng song ngữ, chọn vùng chữ, dịch, chọn giọng và xuất thành phẩm trong một quy trình. Các thao tác từng bước vẫn có thể chạy riêng. Có trình soạn phụ đề, vùng che/đọc chữ, xem trước, thư viện bản đọc, nhập âm thanh ngoài, phối âm và công cụ lưu/khôi phục dự án.

Các thành phần xử lý chính:

- OCR chữ trên hình bằng RapidOCR/ONNX; nhận dạng lời nói bằng Faster Whisper.
- Phân loại/chọn track chữ từ bằng chứng OCR trên toàn video, với chế độ thủ công, tự động trên máy hoặc hỗ trợ dịch vụ có khả năng nhận ảnh.
- Dịch văn bản bằng Gemini, DeepSeek hoặc API tương thích OpenAI qua 9Router. Model 9Router là ID chính xác do endpoint của người dùng công bố; ứng dụng không tự đoán model thay thế.
- Tạo giọng cục bộ bằng Piper và VieNeu-TTS. Có cache từng đoạn, giọng theo câu, tốc độ, nghỉ và từ điển phát âm.
- Xử lý chữ gốc bằng các phương pháp vùng che, làm mờ, kéo nền, Telea hoặc LaMa tùy cấu hình/model sẵn có. Không có bảo đảm tự động tái tạo nền hoàn hảo.
- Ghép video, phụ đề ASS/SRT, giọng, âm thanh nguồn và nhạc nền bằng FFmpeg; hỗ trợ màu theo từ với căn mốc cục bộ hoặc nhịp ước lượng được báo rõ.

Nguồn được nhập bằng đường dẫn tới tệp có sẵn. Ứng dụng tạo các sản phẩm dẫn xuất; thư mục công việc và cache không thể coi chung là dữ liệu tạm có thể xóa tùy ý.

## 2. Các tiến trình và ranh giới

```text
Launcher Windows
  └─ app.py: khóa một phiên sở hữu + cửa sổ pywebview/WebView2
       ├─ FastAPI/Uvicorn trên địa chỉ loopback
       │    ├─ server.py: xác thực phiên, dự án, credentials, hàng đợi
       │    └─ studio_api.py: API cho giao diện hiện tại và tiếp tục pipeline
       ├─ static/: HTML/CSS/JavaScript giao diện
       └─ hàng đợi tuần tự
            └─ worker.py với input.json riêng cho mỗi job
                 ├─ OCR / ASR / chọn phụ đề / dịch
                 ├─ tổng hợp giọng và dựng timeline
                 ├─ xử lý chữ gốc
                 └─ FFmpeg, kiểm chứng và xuất tệp
```

`app.py` dùng khóa tệp trên Windows để chặn một phiên chủ thứ hai trên cùng DATA. Backend công bố danh tính owner cùng trạng thái sống của hàng đợi qua `owner_connection.py`. Công cụ tiếp tục phải tìm và xác minh owner đang tồn tại, không khởi chạy backend thứ hai để xử lý cùng kho dữ liệu.

Frontend và các API cục bộ dùng token phiên ngẫu nhiên. Middleware bảo vệ các đường dẫn API/media và dùng so sánh token an toàn; backend bind loopback. Token phiên ứng dụng khác API key của nhà cung cấp. Cấu trúc này chưa phải thiết kế cho một server công khai hoặc môi trường nhiều tenant.

Worker chạy bằng subprocess, nhận snapshot JSON, phát sự kiện JSON để server cập nhật tiến độ/trạng thái. `process_control.py` quản lý cây tiến trình khi hủy. Khóa chủ hàng đợi và các kiểm tra snapshot giúp tránh chạy trùng, nhưng không thay thế một hệ điều phối phân tán.

## 3. Mô hình dữ liệu và phiên bản

`core.py` xác định ROOT, DATA và MODELS. `VIETDUB_DATA_DIR` cho phép chọn DATA khác trước khi import module. SQLite dùng WAL, bảng đối tượng JSON với khóa `(kind, id)` để lưu dự án, job, cài đặt, lịch sử và trạng thái điều khiển.

Dự án chứa:

- Media: danh tính tệp nguồn, thời lượng, kích thước, FPS và thông tin âm thanh.
- `segments`: nội dung gốc, bản dịch, mốc đầu/cuối và tùy chọn lời đọc/giọng/kiểu phụ đề từng câu.
- `regions` và `extraction_regions`: vùng xử lý chữ gốc và vùng đọc chữ; hai loại có mục đích khác nhau.
- `params`: cấu hình xử lý, nhà cung cấp/model/endpoint, kiểu phụ đề, giọng, âm lượng, tùy chọn xuất.
- `artifacts`: tham chiếu tới sản phẩm đã tạo cùng signature/nguồn công việc.
- Mốc `updated` làm revision cho cập nhật có kiểm tra xung đột; một số luồng còn dùng `source_revision` để phân biệt thay nguồn.

`save_project_version()` lưu dự án và lịch sử giới hạn trong cùng transaction. Những thay đổi nhạy với dữ liệu hiện tại dùng `expected_revision`; kết quả job cũ không được tự ghi đè chỉnh sửa mới hơn.

Mỗi job có thư mục riêng chứa snapshot đầu vào, checkpoint và artifact. Những tên tệp thường gặp:

| Tệp/nhóm | Vai trò |
| --- | --- |
| `input.json` | Snapshot dự án và hợp đồng job lúc nhận việc |
| `translation-checkpoint.json` | Nội dung nhận dạng/dịch có thể khôi phục theo danh tính nguồn/cấu hình |
| `stage-journal.json` | Các bước hoàn tất và bằng chứng artifact để tái sử dụng |
| `api-usage.json` | Lượt gửi HTTP, trạng thái và usage dịch vụ thực sự trả về |
| `api-usage-lineage.sqlite3` | Ngân sách chung qua các lượt tiếp tục cùng quy trình |
| `speech-delivery-plan.json` | Câu đã đo/chưa đo, lỗi thời gian hoặc các vị trí lời đọc đã lập |
| `speech-word-timings.json` | Kết quả căn từ và thông tin mốc ước lượng |
| `voice.wav`, `clean.mp4`, `final.mp4` | Các sản phẩm giọng, xử lý hình và ghép cuối riêng biệt |

Job checkpoint và bảng đang mở có thể có số câu khác nhau sau một lần nhận dạng mới. UI phải gọi tên rõ số câu trong checkpoint, số câu trong editor và số câu có thể khôi phục tương thích; không cộng/trừ chúng thành một số tiến độ giả.

## 4. Pipeline và tiếp tục sau lỗi

`worker.pipeline()` xây danh sách bước từ snapshot và nhu cầu nội dung thực tế:

1. OCR/ASR nếu được chọn, hoặc dùng các câu hiện có.
2. Tự chọn/xử lý track phụ đề khi cần.
3. Dịch phần nội dung còn thiếu.
4. Tạo hoặc dùng lại giọng hợp lệ.
5. Xử lý hoặc dùng lại video đã xử lý chữ gốc.
6. Ghép và xuất video cuối.

Sau mỗi bước, `job_progress.verify_outputs()` kiểm artifact rồi `stage_journal.py` ghi bằng chứng. Cảnh báo cần người xem có thể đưa job sang trạng thái review thay vì âm thầm tiếp tục. Bước OCR đã xong được giữ ngay cả khi bước chọn track hoặc dịch về sau lỗi.

`pipeline_resume.py` kiểm snapshot, revision, phạm vi nhận dạng, cấu hình và các checkpoint/journal trong dòng tiếp tục. Nó tạo snapshot khôi phục riêng, giữ sửa tay và chỉ điền bản dịch tương thích vào chỗ thiếu. Không được tiếp tục bằng cách sửa hash cho qua, tạo job trực tiếp hoặc coi các câu editor hiện tại là toàn bộ checkpoint mới.

`processing_plan.py`, `preflight.py` và `subtitle_quality.py` quyết định tính sẵn sàng của nội dung/artifact. `execution_admission.py` quyết định công việc đó có được chạy lúc này hay không. Vì vậy `content_ready`, `can_continue` và `execution_allowed` là các thông tin khác nhau. Một kết nối được phép không làm một bản lời đọc quá dài trở nên sẵn sàng.

`artifact_resolver.py` và các signature kiểm kết quả có khớp cấu hình hiện tại. `final.mp4` là thành phẩm ghép; `clean.mp4` chỉ là đầu vào hình đã xử lý. Xuất tới thư mục đích có kiểm tra và có thể giữ bản đã xử lý nếu chép tới đích thất bại.

## 5. Hợp đồng lưu model và kiểm soát API

Luồng cấu hình phân biệt khóa đã lưu, model đang nhập, model đã được backend xác nhận và snapshot model của job đang chạy. Lưu model không được báo thành công khi backend chưa xác nhận đúng provider/model/endpoint/revision.

Job có snapshot bất biến. Nếu người dùng lưu provider/model/endpoint khác, job cũ phải dừng trước lượt API tiếp theo thay vì tiếp tục gọi model cũ; phần đã xong được giữ để tiếp tục theo cấu hình mới. `require_execution_contract()` kiểm việc này ở các ranh giới thực thi và trước gửi.

Ba lớp kiểm soát có vai trò riêng:

| Thành phần | Kiểm soát |
| --- | --- |
| `execution_admission.py` | Audit hold, đúng kết nối cần dùng, lịch sử lỗi/cooldown và điều kiện chạy tự động |
| `provider_usage.py` | Đếm/reserve lượt gửi, lịch sử ngân sách qua continuation, usage biết/chưa biết |
| `request_pacing.py` | Một request đang gửi cho mỗi provider/endpoint, cách nhau ít nhất 12 giây giữa các lần bắt đầu, bằng chứng quota bền qua restart |

Model hoặc key mới không tạo ngân sách mới. Bucket nhịp gửi theo kết nối, không theo model/key. Nhịp 12 giây là trần cục bộ bảo thủ, không phải cam kết quota từ upstream; client khác dùng cùng router/tài khoản nằm ngoài kiểm soát này.

Lỗi 429 không tự kích hoạt vòng thử lại liên tục. Với quota unknown/daily, thời gian trôi qua hoặc tải được catalog không chứng minh phục hồi. Phép thử sinh nội dung chủ động có điều kiện riêng và chỉ xác nhận phục hồi sau khi phản hồi dùng được đã được kiểm tra. Lỗi tạm thời có retry giới hạn, hủy được và vẫn đi qua nhịp gửi/ngân sách chung. Dịch có thể chia nhỏ batch thích ứng khi timeout/giới hạn đầu ra, vẫn giữ phạm vi và checkpoint.

`request_attempts`, token được báo cáo và chi phí thực thu là ba khái niệm khác nhau. Timeout hoặc 0 câu dịch mới không chứng minh không có request hay không có chi phí. Các ledger giữ trạng thái usage chưa biết và không suy diễn giá tiền từ số token.

## 6. Giọng đọc và bố trí thời gian

`speech_delivery.py` là lớp hàm thuần: chọn nội dung thực đọc, kiểm tốc độ/nghỉ, tạo cửa sổ và signature. Nguồn lời đọc được chọn rõ: bản dịch, lời biên tập hoặc nội dung gốc; dự án mới không âm thầm đọc nguồn khi bản dịch đang thiếu.

Cache raw audio gắn với nội dung thực đọc, danh tính giọng/model và tốc độ tổng hợp. `speech_preparation.py` xác minh PCM và đo thời lượng; phân biệt câu chưa đo với câu đã đo nhưng không vừa. `worker.tts()` dùng lại cache hợp lệ, tạo phần thiếu và chỉ công bố timeline khi âm thanh sau lọc thực sự nằm trong giới hạn.

Hai chính sách mới/cũ cần được phân biệt:

- Chế độ tự nhiên trong từng mốc phụ đề có mức nén thời gian sau tổng hợp tối đa 1,15×. Không vừa thì giữ cache và báo điều kiện cần xử lý.
- `tts_auto_schedule=true` là lựa chọn tự bố trí lời đọc: đo đủ mọi đoạn trước, giữ thứ tự và khoảng nghỉ, phân phối các đoạn trong thời lượng video, rồi tăng tempo đồng đều trong giới hạn 1,25× khi cần. Thuật toán có thể mượn khoảng trống ở trước và sau mốc gốc để giảm độ lệch lớn nhất. Mốc phụ đề không tự thay theo vị trí giọng.

Tự bố trí không rút gọn bản dịch, không cắt đuôi lời, không cho các đoạn nói chồng nhau. Nếu tổng lời vẫn không vừa ở giới hạn thì dừng có bằng chứng. Các lượt FFmpeg/atempo có sai số so với phép tính thời lượng nên worker đo lại kết quả và chỉ điều chỉnh trong giới hạn.

Giới hạn quan trọng: thuật toán hiện tại ưu tiên khả năng xếp đủ lời và giảm lệch, chưa hiểu cảnh hoặc quan hệ lời nói với hành động. Không có trần cứng nhỏ cho độ lệch từng câu; đoạn dày lời có thể đọc sớm/muộn đáng kể so với phụ đề. Giới hạn tempo sau tổng hợp cũng khác tốc độ tổng hợp do người dùng đặt. `lexical_verification` của giọng hiện là `unverified`: có tín hiệu âm thanh và đủ tệp không chứng minh model phát âm đúng toàn bộ chữ. Highlight từ có thể dùng mốc ước lượng khi không căn được tin cậy.

## 7. Bản đồ module cho người đọc mã

| Nhóm | Các tệp bắt đầu đọc |
| --- | --- |
| Khởi chạy/cửa sổ | `launcher.py`, `app.py`, `windows_identity.py` |
| API/lưu dự án/hàng đợi | `core.py`, `server.py`, `studio_api.py`, `owner_connection.py`, `job_admission.py` |
| Điều phối/kiểm chứng | `worker.py`, `job_progress.py`, `processing_plan.py`, `preflight.py`, `hardware_admission.py`, `resource_budget.py` |
| OCR/ASR/track | `ocr_checkpoint.py`, `ocr_tracks.py`, `asr_checkpoint.py`, `video_sampling.py`, `auto_subtitle_director.py` |
| Dịch/API | `providers.py`, `translation_checkpoint.py`, `translation_quality.py`, `provider_usage.py`, `request_pacing.py`, `execution_admission.py` |
| Tiếp tục/artifact | `pipeline_resume.py`, `stage_journal.py`, `artifact_resolver.py`, `exports.py` |
| Phụ đề/biên tập | `cue_editor.py`, `subtitle_quality.py`, `subtitle_layout.py`, `subtitle_flow.py`, `subtitle_fonts.py`, `subtitle_presets.py`, `karaoke.py` |
| Giọng/âm thanh | `voices.py`, `vieneu_voice.py`, `speech_delivery.py`, `speech_preparation.py`, `speech_word_alignment.py`, `audio_verification.py`, `narration.py` |
| Công cụ âm thanh bổ sung | `audio_inputs.py`, `audio_insights.py`, `audio_separation.py`, `speaker_groups.py`, `voice_enrollment.py`, `speech_library.py` |
| Chữ gốc/hình | `cleaner.py`, `fast_cover.py`, `tracking.py`, `clean_timing.py`, `live_clean_preview.py`, `encoding.py` |
| Credentials/dữ liệu | `credentials.py`, `storage_manager.py`, `project_transfer.py`, `project_bundle.py`, `studio_preferences.py` |
| Giao diện | `static/studio.html`, `static/studio.js` và các module JavaScript trong `static/` |
| Kiểm thử/công cụ | `tools/run_tests.py`, `tools/test_boundary.py`, `tests/test_*.py`, `tools/continue_pipeline_job.py` |

## 8. Bảo mật, đóng gói và giới hạn kiểm chứng

Khóa dịch vụ được lưu bằng Windows Credential Manager qua `keyring`; dự án và gói backup chỉnh sửa không chứa khóa. Có API để kiểm tra trạng thái khóa/cấu hình mà không trả bí mật thô. Lỗi provider được chuyển thành thông tin an toàn thay vì dump header/body. Endpoint router không nhận tài khoản trong URL, query hoặc fragment; HTTP chỉ được cho loopback, mạng ngoài yêu cầu HTTPS.

Source chia sẻ không cần và không nên chứa dữ liệu thật, credentials, session token, database người dùng, snapshot job, video, âm thanh, model weights hoặc runtime đã cài. Gói nguồn không phải bộ cài portable. Python, WebView2, FFmpeg, các model và môi trường phụ trợ cần được chuẩn bị riêng.

Trong lần kiểm chứng vận hành gần nhất, quy trình đã xuất được một video khoảng 11 phút 35 giây, 381 câu, H.264 1280×720 và AAC stereo 48 kHz; kiểm giải mã toàn bộ 20.859 khung hình. Lượt ghép cuối dùng lại đủ cache giọng mà không dịch lại. Tuy nhiên tự bố trí đã khiến một số lời đọc ở đoạn đuôi sớm hơn mốc phụ đề tối đa khoảng 10,81 giây; 167 câu có mốc từ qua alignment và 214 câu dùng nhịp ước lượng. Đây là ví dụ rõ về sự khác nhau giữa **hoàn tất kỹ thuật** và **đồng bộ/chất lượng đã được người xem duyệt**. Media, danh tính dự án, đường dẫn riêng và các báo cáo chứa dữ liệu của ca đó không thuộc gói source chia sẻ.

Các bài test hợp đồng chỉ chứng minh những điều đã assert trên fixture. Kiểm artifact/giải mã chứng minh tệp và luồng media dùng được trong phạm vi kiểm; không chứng nhận bản dịch, phát âm, đồng bộ ngữ nghĩa hoặc mức sạch chữ gốc. Một pipeline hoàn tất trên video cụ thể không chứng minh tất cả video đầu vào sẽ hoàn tất hoặc đạt chất lượng như nhau.

Repository nguồn dành cho review không tự cấp một giấy phép mã nguồn mở cụ thể. Không suy diễn MIT/Apache cho mã ứng dụng từ giấy phép của một dependency; quyền dùng/phân phối mã riêng cần theo thông tin giấy phép được chủ dự án công bố.
