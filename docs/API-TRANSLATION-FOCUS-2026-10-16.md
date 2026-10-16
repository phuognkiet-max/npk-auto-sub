# Ưu tiên API, dịch và nội dung lồng tiếng — 16/10/2026

## Phạm vi được người dùng chọn

Giữ phần xóa vùng chọn hiện có. Chưa cải tiến xóa watermark trên hình hoặc tái cấu trúc toàn bộ thư mục. Ưu tiên API/dịch, đặc biệt lỗi lặp câu và đọc chữ không thuộc lời thoại. Không áp dụng mù đề xuất fuzzy dedup 80%, bỏ mọi chuỗi @, cắt lời tràn hoặc tự tắt lời cuối video.

## Các lỗi đã sửa

1. **JSON có code fence thường bị phá ở worker.** Parser đã hỗ trợ JSON thuần và cả hai kiểu fence. Worker từng xóa dấu đóng thêm một lần, làm phản hồi hợp lệ thành lỗi rồi gọi lại. Nay chỉ parser xử lý định dạng, mỗi phản hồi hợp lệ dùng một request.
2. **9Router xử lý lỗi JSON khác các kết nối còn lại.** Adapter nay phân loại rõ `output_reason=invalid_json`; worker cho cùng cơ chế sửa định dạng tối đa ba lượt. Auth/quota/service/blocked không bị biến thành lỗi định dạng. Mọi lượt vẫn dùng ledger/budget chung qua continuation; adapter không tự sinh thêm vòng retry cho HTTP 200 lỗi JSON.
3. **Ngữ cảnh thiếu khi dịch scope không liên tục.** Context nay lấy quanh từng ID cần dịch, kèm các bản dịch đã có gần đó và ID/thứ tự. Ví dụ chỉ dịch câu 2,4,6 thì vẫn có câu 1,3,5,7 làm ngữ cảnh. Context giữ JSON hợp lệ trong giới hạn 2.800 ký tự; không gửi các câu context thành mục cần dịch lại.

## Kiểm tra lời đọc cục bộ

Nút **Kiểm tra lời đọc** có ở **Phụ đề** và **Giọng & âm thanh**. Dùng nội dung OCR/bản dịch đã lưu, không gọi dịch vụ hoặc tạo giọng.

- Preview liệt kê các nhóm nguồn và lời đọc trùng sau chuẩn hóa hẹp, liền nhau, và các dòng có nhãn nguồn video/hình ảnh ở đầu câu.
- Nhóm lặp cần cùng nguồn, cùng lời đọc và cùng giọng/tốc độ/nhân vật/ngắt nghỉ; không có lời đọc riêng đã biên tập. Chỉ xét coverage OCR, không đánh đồng ASR với phụ đề in trên hình.
- Suffix ` @handle` chỉ được bỏ trong phép so sánh nhận nhóm. Nội dung thực tế không bị sửa; nhóm phải có một câu sạch để giữ đọc.
- Không chọn sẵn gợi ý. Mọi câu của nhóm có đủ lời gốc, lời đang đọc và nhãn giữ/tắt để người dùng đối chiếu.
- Áp dụng chỉ đặt `speech_enabled=false` cho các câu thuộc gợi ý được chọn; giữ đầy đủ caption, bản dịch, mốc gốc, vùng xóa chữ và giọng. Lịch sử phiên bản và hoàn tác giữ lại bản trước.
- Signature gắn revision, nguồn, toàn bộ bảng và cấu hình. Preview cũ, ID lạ, dự án đang chạy/queued bị từ chối; frontend cũng kiểm revision của report trước khi dùng bảng đang hiển thị.
- Câu chứa @ nhưng chưa đủ bằng chứng được đưa thành mục cần xem riêng. Không tự xóa email, username, phủ định, số hay câu lặp có chủ đích.

Trên snapshot 381 câu của ca đã kiểm, preview tìm đúng bốn gợi ý: hai nhóm ba câu có bản giữa dính tài khoản, và hai dòng ghi nguồn cuối. Đây là kết quả kiểm read-only; chưa áp dụng vào dự án người dùng trong lượt sửa này. Các nhóm khác chỉ giống nhau gần đúng không bị tự đề xuất tắt.

## Kiểm chứng

194 kiểm thử tập trung đạt, không đếm trùng:

| Nhóm | Số test |
| --- | ---: |
| 9Router adapter | 18 |
| Provider resilience | 51 |
| Request pacing | 14 |
| Translation/checkpoint/lineage/request budget, gồm prompt contract mới | 43 |
| Adaptive translation | 16 |
| Video upgrade contracts | 19 |
| Dialogue preview/apply/API/UI | 24 |
| Saved model execution contract | 9 |

UI test chạy hàm production trong Node VM với DOM/API giả, gồm bảy tình huống: không chọn sẵn, xem đủ và escape nội dung, chỉ gửi ID đã chọn, từ chối revision report khác bảng đang xem, thay đổi khi GET, preview cũ khi apply, giữ bản nháp đổi trong khi POST và trạng thái không có gợi ý. Đây không phải nghiệm thu giao diện bằng trình duyệt/native screenshot.

Runner dùng DATA riêng, chặn credentials/mạng thật; mọi provider request trong test là mock. Kiểm tra cú pháp các tệp Python thay đổi và JavaScript đạt. Không dùng số lượng test để chứng nhận chất lượng dịch ngôn ngữ hay phát âm của model thật.

## Giới hạn còn lại

- JSON đúng không chứng minh bản dịch đúng nghĩa, xưng hô hoặc mọi dữ kiện. Report và test lần này không thay cho đánh giá bản dịch với nguồn.
- Chưa áp đặt cổng chặn mọi cảnh báo số/đơn vị vì kiểm heuristic còn có false positive với cách viết số hoặc đơn vị khác nhau.
- Lặp do glyph OCR khác nhau, câu đọc tự lặp trong raw TTS và đồng bộ lời với cảnh cần kiểm riêng; không suy từ chuỗi giống nhau để tự xóa.
- Chưa thay thuật toán lịch đọc hoặc cam kết lệch <=0,25 giây. Chưa tạo video mới trong lượt này.
- Các probe/catalog vẫn tách khỏi budget dịch của pipeline. Nhịp gửi được kiểm soát, nhưng cần ledger diagnostic bền vững riêng để kiểm toán đầy đủ việc bấm thử kết nối.
- App desktop đang mở cần được đóng/lưu rồi mở lại để nạp endpoint mới; mã nguồn được kiểm không tự thay tiến trình đã nạp.

## Tệp liên quan

- `providers.py`, `worker.py`, `translation_quality.py`: adapter, retry và ngữ cảnh.
- `dialogue_review.py`: preview/apply thuần cục bộ.
- `studio_api.py`, `static/studio.js`: API và giao diện kiểm tra lời đọc.
- `tests/test_translation_prompt_contract.py`, `tests/test_9router_provider.py`, `tests/test_dialogue_review*.py`, `tests/dialogue_review_ui.cjs`: hồi quy tập trung.

Kinh nghiệm đối chiếu: [I Replaced My TTS Engine. The API Call Was the Easy Part. — Anthony Papoti](https://dev.to/anthony_builds/i-replaced-my-tts-engine-the-api-call-was-the-easy-part-49cm) mô tả giữ mốc từng cụm lời và đo WAV sau tổng hợp; không thể bảo đảm thời lượng chỉ bằng prompt dịch. Đây là kinh nghiệm của tác giả, không phải bằng chứng nghiệm thu NPK.
