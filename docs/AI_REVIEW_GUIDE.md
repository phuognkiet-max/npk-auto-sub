# Hướng dẫn chuyển giao NPK Auto Sub cho AI rà soát

Mục tiêu của repository là cho phép đọc mã, thảo luận kiến trúc và đề xuất sửa lỗi có thể kiểm chứng. Không cần API key, dữ liệu người dùng hoặc gọi nhà cung cấp để hiểu và kiểm các hợp đồng lõi. Đọc [kiến trúc](ARCHITECTURE.md) trước khi đề xuất tách module hoặc thay luồng xử lý.

## Bối cảnh và kết quả cần giữ

Đây là ứng dụng Windows chạy FastAPI trong cửa sổ pywebview, với worker cục bộ và các engine/media tool được cài riêng. Bài toán chính là đưa một video qua nhận dạng → chọn phụ đề → dịch → tạo giọng → xử lý chữ gốc → ghép/xuất, đồng thời giữ được phần đã hoàn tất khi lỗi.

Các vấn đề thực tế đã thúc đẩy các hợp đồng hiện có gồm: mất dấu checkpoint sau OCR; UI hiển thị model chưa thực sự lưu; job cũ tiếp tục gọi model cũ; retry/chia batch tạo nhiều request; con số tiến độ lẫn giữa editor và checkpoint; giọng dài hơn mốc làm quy trình dừng sớm; và kết quả cũ có nguy cơ bị dùng lại sau sửa nội dung.

Mã hiện có đã thêm checkpoint/journal, lưu model có receipt từ backend, kiểm hợp đồng trước mỗi lần gửi, budget dùng chung qua continuation, nhịp gửi bền qua restart, đo giọng và chính sách tự bố trí tùy chọn. Đây là những cơ chế cần được rà soát chặt chẽ, không phải các khẳng định rằng mọi vấn đề chất lượng đã giải quyết.

Nguồn không kèm video nghiệm thu riêng. Kết quả hoàn tất một video trong môi trường đã cài engine là bằng chứng vận hành cụ thể; một AI chỉ có source không thể tự xác nhận lại chất lượng của video đó. Không nên viết nhận xét “đã kiểm toàn bộ” chỉ dựa vào tên báo cáo hay số lượng test.

## Trình tự đọc được đề nghị

1. `core.py`, `app.py`, `server.py`, `studio_api.py`: cấu trúc dữ liệu, owner, xác thực phiên, revision, hàng đợi.
2. `worker.pipeline()`, `processing_plan.py`, `preflight.py`, `job_progress.py`: lập kế hoạch, thực thi, kết thúc thực và kiểm artifact.
3. `pipeline_resume.py`, `stage_journal.py`, `translation_checkpoint.py`, `ocr_checkpoint.py`, `artifact_resolver.py`: phục hồi và dấu vân tay dữ liệu.
4. `providers.py`, `execution_admission.py`, `provider_usage.py`, `request_pacing.py`: ranh giới cấu hình/API/quota/ngân sách.
5. `speech_delivery.py`, `speech_preparation.py`, `worker.tts()`, `speech_word_alignment.py`: nội dung thực đọc, đo đủ, lập lịch, dựng âm thanh và mốc chữ.
6. Module JavaScript của cấu hình kết nối, pipeline và bảng phụ đề: state đang nhập, state đã lưu, state đang chạy, cách diễn giải số liệu.
7. Các test tương ứng, gồm cả trường hợp sai/hủy/lỗi/tiếp tục thay vì chỉ đường thành công.

## Những điều không được phá khi sửa

| Hợp đồng | Câu hỏi kiểm tra khi review |
| --- | --- |
| Dự án đang sửa và snapshot job độc lập | Job cũ hoàn tất có ghi đè revision mới không? Apply/continue có kiểm `expected_revision` không? |
| Một owner cho DATA | Công cụ có vô tình tạo backend/hàng đợi thứ hai không? Owner sống có được xác minh lại sau restart không? |
| Khôi phục theo danh tính nguồn/cấu hình | Đổi video, vùng đọc hoặc lựa chọn track có dùng nhầm checkpoint cũ không? |
| Giữ sửa tay và phần đã dịch | Chỉ điền chỗ thiếu tương thích; không chạy OCR/dịch lại để tránh xử lý trạng thái khó. |
| Model đã lưu có xác nhận | Thao tác UI có receipt chính xác hay chỉ sửa biến frontend? Job cũ có dừng trước lần gửi tiếp theo sau đổi kết nối/model? |
| Ngân sách không reset khi đổi model/key | Mọi retry, lần chia batch và continuation có reserve vào cùng lịch sử phù hợp không? |
| Quota và audit hold khác nhau | Kết nối nào thực sự cần dùng? Thiếu metadata có fail closed thay vì bỏ chặn không? |
| Lượt gửi không bằng token/câu mới | Timeout, phản hồi không có usage và catalog request được phản ánh đúng không? |
| Không cắt lời trong chế độ tự nhiên/tự bố trí | Có kiểm PCM sau atempo/resample, tránh overlap và giữ raw cache khi lỗi không? |
| Giọng theo nội dung thực đọc | Đổi lời, giọng, model, phát âm hoặc tốc độ có làm signature/cache cũ vô hiệu đúng mức không? |
| Thành phẩm khớp chỉnh sửa | Resolver có chọn `final.mp4` đúng revision/fingerprint, thay vì video clean hoặc final cũ không? |
| Lưu/xóa dữ liệu có phạm vi | Có kế hoạch dọn, kiểm đường dẫn sở hữu và bảo toàn tệp còn được tham chiếu không? |

## Các hướng rà soát có giá trị cao

### 1. Tách trạng thái và quyền quyết định

`server.py`, `studio_api.py` và `worker.py` vẫn lớn. Hãy xác định một đối tượng kế hoạch thực thi có kiểu/ràng buộc rõ cho các bước còn thiếu, kết nối cần dùng, nguồn checkpoint, revision và budget. Tránh có nhiều phép suy ra “cần gọi provider” không đồng nhất giữa preflight, enqueue, dispatch và worker.

Không sửa bằng cách bỏ admission, xóa checkpoint, ép review thành đã duyệt hoặc sửa signature. Hãy chỉ ra sự trùng lặp bằng các đường gọi và đề xuất migration giữ tương thích dữ liệu cũ.

### 2. Mô hình dữ liệu và transaction

Kho JSON trong SQLite thuận tiện nhưng nhiều invariant nằm ngoài schema. Rà soát cập nhật đồng thời, snapshot bất biến, revision dạng timestamp, lịch sử continuation và commit giữa file checkpoint/SQLite. Đề xuất hợp đồng ghi nguyên tử và phục hồi sau mất điện/kill mà không biến một artifact chưa kiểm thành artifact hợp lệ.

### 3. Nhịp request và quota

Kiểm xem catalog, probe, generate, hỗ trợ chọn track và adaptive split có cùng đường điều phối không. Cần phân biệt trần cục bộ với quota tài khoản thực; không nên hứa 5 request/phút là an toàn cho mọi nhà cung cấp. Client khác gọi cùng 9Router chưa được ứng dụng điều phối.

Audit/monitor vận hành có quy tắc phục vụ môi trường triển khai. Rà soát khả năng chuyển thành cấu hình chung có schema và migration rõ thay vì ràng buộc ngầm với một máy hoặc dự án. Không cần đọc credentials để làm việc này.

### 4. Chất lượng tự bố trí lời đọc

Hiện `schedule_speech()` đo đủ thời lượng, xếp đúng thứ tự, giữ khoảng nghỉ, không overlap và có giới hạn tempo 1,25×. Thuật toán tối thiểu hóa độ lệch cực đại của vị trí với khả năng nén đồng đều; không biết ngữ nghĩa cảnh, người nói hoặc điểm nhấn câu.

Cần thảo luận thêm: giới hạn lệch theo câu/nhóm cảnh; phát hiện đoạn dày lời; chính sách ưu tiên câu nhạy với hình; minh bạch khoảng dịch chuyển trước khi chạy; và sự khác nhau giữa mốc phụ đề gốc với mốc phát giọng mới. Trần 2 giây trong lựa chọn mục tiêu hiện tại không phải cam kết mọi câu lệch dưới 2 giây. Không giải quyết bằng cách âm thầm tóm tắt bản dịch hoặc tăng tempo vô hạn.

### 5. Phát âm và mốc từ

`lexical_verification: unverified` là giới hạn thật: xác minh WAV có tín hiệu không chứng minh đủ chữ/đúng phát âm. Có thể nghiên cứu kiểm nhận dạng ngược cục bộ, confidence và điểm cần người nghe duyệt, nhưng cần tránh dùng ASR như chân lý tuyệt đối.

Căn mốc từ có fallback ước lượng. Cần giữ nhãn provenance, invalidation cache khi lời/giọng đổi và báo mức tin cậy. Khi lời đọc được tự bố trí, phải kiểm cách căn từ tương tác với mốc phụ đề đang giữ nguyên.

### 6. Khả năng cài đặt và bảo trì

Source không bao gồm runtime/model. Kiểm installer, các môi trường Python phụ trợ, native binaries, phiên bản dependency, khả năng CPU/GPU và thông báo thiếu engine. Không tuyên bố portable chỉ vì có launcher EXE.

Giấy phép model/giọng/FFmpeg/Piper và quyền nội dung cần được xem ở tài liệu upstream khi chuẩn bị phân phối; tài liệu ghi nhận thành phần không tự cấp quyền phân phối lại mọi artifact.

## Cách kiểm thử an toàn

Đọc [ranh giới kiểm thử](TEST-BOUNDARIES.md). Dùng runner của repository để DATA thử nghiệm, chặn vault/mạng và inference được cài trước import ứng dụng. Không chạy discovery trực tiếp trên DATA desktop, không đưa dữ liệu thật vào fixture và không chạy script acceptance lịch sử tùy ý.

Ví dụ từ thư mục source trên Windows:

```powershell
python tools\run_tests.py --pattern test_provider_resilience.py --verbose
python tools\run_tests.py --profile media --verbose
```

Lệnh cần môi trường Python/dependencies đã chuẩn bị. Nếu dùng môi trường runtime đặt cạnh source thì thay `python` bằng Python tương ứng. Profile mặc định chặn native process và inference; `media` chỉ cho phép media/Node fixture đã giới hạn; `native` là opt-in có thể tốn CPU/GPU. Các guard Python không phải sandbox cấp hệ điều hành.

Ưu tiên test có khả năng bác bỏ lỗi thật: đổi model lúc worker đang chờ lịch; kill giữa ghi checkpoint và commit; timeout đã gửi nhưng chưa biết usage; stale revision; nguồn/vùng OCR đổi; cùng cache nhiều câu; lời cuối video vượt thời lượng; sai số sau resample; lịch giọng đủ thời lượng nhưng lệch cảnh. Mock phải ghi rõ phạm vi. Không gọi provider chỉ để “thử lại xem” nếu chưa có sự cho phép và ngân sách rõ ràng.

## Dạng kết quả review mong muốn

Mỗi phát hiện cần có:

1. Mức ảnh hưởng và tình huống cụ thể tái hiện được.
2. Tệp/hàm và chuỗi dữ liệu liên quan.
3. Invariant bị vi phạm hoặc rủi ro còn chưa chứng minh.
4. Cách sửa và kế hoạch giữ/migrate dữ liệu cũ.
5. Test hoặc thực nghiệm có thể chứng minh sửa đúng, phân biệt fixture với chạy engine/media thật.

Không coi cảnh báo đã được ghi rõ là lỗi cần xóa khỏi UI. Không tuyên bố đã có chất lượng dịch/giọng/xóa chữ phổ quát chỉ vì test pass. Nếu chưa có video/runtime để tái hiện, hãy nêu giới hạn đó và cung cấp đánh giá mã có căn cứ.

## Prompt có thể gửi cho một AI khác

> Hãy đọc README, docs/ARCHITECTURE.md và docs/AI_REVIEW_GUIDE.md của NPK Auto Sub, rồi rà soát mã theo luồng snapshot → admission → provider/worker → checkpoint → artifact → apply/export. Tập trung lưu model đúng, giới hạn request qua continuation, không mất nội dung khi phục hồi và tự bố trí giọng đọc mà không cắt lời. Đưa các lỗi có bằng chứng, ưu tiên mức nghiêm trọng, kèm tệp/hàm, cách tái hiện, hướng sửa và test. Phân biệt tính hợp lệ media với chất lượng dịch/phát âm/đồng bộ cảnh. Không yêu cầu API key, không gọi dịch vụ hoặc xóa dữ liệu và không tự bỏ các rào admission/review để làm pipeline chạy qua.
