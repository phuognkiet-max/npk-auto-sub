# Đợt 2–3: Chất lượng dịch, lời đọc và hợp đồng gọi API — RC10

Ngày triển khai: 16/10/2026. Nối tiếp [Đợt 1](PHASE1-TEXT-REVIEW-RC10.md). Mục tiêu là xử lý bảng phụ đề tổng quát theo ngôn ngữ đích, giữ phần biên tập và giới hạn chi phí khi phản hồi dịch vụ không hoàn chỉnh. Không hardcode tên nhân vật hay nội dung của một video.

## Đợt 2 — Dịch và lời đọc

- `translation_quality.effective_words_per_second` dùng tối thiểu **4,8 âm tiết cách bằng khoảng trắng/giây** cho `vi`, `vi-VN`, `Vietnamese`, `Tiếng Việt`; giữ giá trị người dùng đặt cao hơn. Ngôn ngữ khác giữ cấu hình hiện có. Đây là ngân sách dự kiến, không thay tốc độ tổng hợp giọng hoặc bảo đảm audio vừa khung.
- `speech_rewrite` dùng cùng phép tính khi tạo đề xuất và đọc lại checkpoint. Câu hợp lệ về dữ kiện, nằm trong ngân sách được nhận. Câu vượt ngân sách nhưng ngắn hơn lời hiện tại được nhận kèm cảnh báo phải đo audio/duyệt; không ép bỏ dữ kiện để đạt số âm tiết. Câu thay số/đơn vị vẫn bị từ chối.
- `facts` chuẩn hóa một số biểu thức số Trung–Việt trước so sánh: `1万比索` ↔ `10.000 peso`/`10 nghìn peso`, `百米` ↔ `100 mét`, `两` ↔ `2`/`hai`, `半个月` ↔ `nửa tháng`/`15 ngày`. Phép quy ước nửa tháng = 15 ngày chỉ phục vụ nhận diện cách nói trong phụ đề; không phải phép tính lịch. Không quy đổi tỷ giá hay tự bổ sung đơn vị.
- Khử tiếng Việt lặp liền kề thận trọng ở các hư từ như `Nhưng nhưng`. Không xóa mù quáng từ láy/cách nói hợp lệ như `đều đều`, `xanh xanh`, `ngày ngày`, hoặc lặp có dấu câu. Không áp quy tắc này lên ngôn ngữ khác.
- Prompt tiếng Việt yêu cầu kể/thuyết minh tự nhiên, liên kết ngữ cảnh, đảo trật tự diễn đạt cho thuận tiếng Việt, giữ nghĩa và dữ kiện. Thời lượng là gợi ý chọn cách diễn đạt. Lời đọc tối ưu là đề xuất riêng, không ghi đè bản dịch hiển thị.

### Bộ nhớ thực thể xuyên batch

`translation_entities.py` giữ từ điển tích lũy có nguồn chứng cứ. Model có thể trả `entities:[{source,target}]` ngay trong phản hồi dịch hiện tại; không có lần gọi API riêng để trích tên. Cặp tên chỉ được nhận khi có mặt trong câu gốc và bản dịch đã được nhận. Fallback cục bộ chỉ ghi nhận tên nhiều từ giữ nguyên cách viết, không đoán phiên âm tên Trung Quốc.

Từ điển đi cùng checkpoint, gồm vị trí xác nhận và nguồn tạo. Batch tiếp theo nhận các mục liên quan trong Context. Từ điển do người dùng nhập có ưu tiên cao hơn. Câu trả về trái mục đã xác nhận được tách ra khỏi phần thành công để xử lý lại trong ngân sách hiện có. Không thay tên bằng phép replace trên mọi câu. Nếu dịch vụ không cung cấp cặp tên đủ chứng cứ, hệ thống không tuyên bố đã nhận diện được mọi tên; cần duyệt thuật ngữ trong bảng.

## Đợt 3 — Partial recovery, model và sửa tay

### Lưu từng phần, chỉ gọi lại phần thiếu

Translation và spoken rewrite kiểm độc lập từng ID: đúng phạm vi, ID nguyên, không trùng, đúng kiểu/nội dung. Các ID hợp lệ được ghi checkpoint ngay trước request kế tiếp. Với phản hồi 19/20 câu, lần sau chỉ gửi câu còn thiếu. Các ID không thuộc yêu cầu không được áp dụng; ID trùng không được đoán chọn một bản.

Lỗi JSON có số lần thử hữu hạn; retry HTTP, format repair và chia batch vẫn chịu ngân sách request/pacing chung. Auth/quota không bị biến thành vòng sửa JSON. Không tăng ngân sách khi resume. Dòng chỉ còn punctuation/handle sau sanitizer không được gửi lên provider và bị tắt đọc; bản gốc cùng dấu vết bỏ qua vẫn được giữ trong checkpoint. Credit-only vẫn có thể được dịch để hiện trên màn hình, nhưng `speech_enabled=False` ngăn TTS đọc nó. Nếu hàng rác đã có nội dung dịch/lời đọc cần giữ, bước dịch dừng để người dùng kiểm tra thay vì âm thầm xóa hoặc gửi lại. Câu có lời đã biên tập không được coi là rác chỉ vì nguồn trống.

### Model đã chọn và model dịch vụ báo

`providers` phát sự kiện trước mỗi lần gửi với `configured_model`, rồi ghi `response_model` từ JSON HTTP nếu dịch vụ có cung cấp. Gemini dùng `modelVersion`, endpoint tương thích OpenAI dùng `model`. Nhật ký không lấy model đã chọn để điền vào chỗ response bị thiếu.

`provider_usage` ràng buộc model đã cấu hình vào journal request. Đổi model giữa tác vụ bị chặn trước lần gửi kế tiếp. Summary có model đã cấu hình, tập model phản hồi, lượt thiếu thông tin và lượt tên không khớp. UI chẩn đoán hiển thị hai bên và cảnh báo tên khác nhau. Router có thể trả tên upstream khác alias đã chọn: điều này là thông tin phải đối chiếu, không đủ để tự kết luận router gọi sai model.

Checkpoint dịch gắn provider/model/endpoint/ngôn ngữ. Resume không khớp dừng với lý do cụ thể; người dùng có thể giữ phần đã dịch và chủ động chọn lượt mới/phạm vi còn thiếu. Không tự đổi model hoặc xóa checkpoint. Request đã gửi và token dịch vụ báo được đếm riêng; thiếu token/model phản hồi không có nghĩa request miễn phí.

### Câu người dùng biên tập

`cue_editor` đánh dấu `user_edited=True` khi nội dung `translated` hoặc `spoken_text` thực sự đổi. Marker được validation và lưu revision giữ lại. Dịch/rewrite bỏ qua câu này mặc định, vẫn dùng làm ngữ cảnh. Xóa trống một trường có chủ đích cũng là sửa tay, không phải lời mời tự khôi phục bản cũ.

Ghi đè phải được yêu cầu rõ bằng job flag boolean `overwrite_user_edits=True`, không phải setting toàn cục. Flag nằm trong snapshot và điều kiện kiểm checkpoint/idempotency. Tiếp tục tác vụ cũ không được dùng làm cách âm thầm nâng quyền ghi đè. Phần đã dịch và các câu sửa tay được giữ qua cả phục hồi checkpoint.

## Kiểm chứng

Các test dùng phản hồi/API giả lập, dữ liệu riêng và không đọc kho khóa:

- Việt 2 giây / 9 âm tiết được nhận; ngân sách cao hơn do người dùng đặt vẫn giữ.
- Số/đơn vị tương đương được nhận; số/currency bị đổi thật vẫn bị phát hiện.
- Lời ngắn hơn vượt budget có cảnh báo và vẫn resume được.
- Dịch/rewrite partial 19/20 → request sau chỉ 1 ID; checkpoint còn dùng được sau gián đoạn.
- Sửa tay được giữ, còn explicit overwrite được kiểm riêng.
- Entity glossary truyền xuyên batch/checkpoint, có kiểm chứng và không âm thầm đổi mục đã nhận.
- Model response có/không có/khác configured được phân biệt và đưa tới UI; drift configured bị chặn trước HTTP.

Kiểm thử xác nhận hợp đồng và điều kiện biên, không chứng minh bản dịch mọi video đều đúng nghĩa hoặc giọng phát âm mọi từ đúng. Cổng xem/nghe duyệt trước render của Đợt 1 vẫn cần cho chất lượng đầu ra.
