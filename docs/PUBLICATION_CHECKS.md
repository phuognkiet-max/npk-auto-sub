# Kiểm tra bản source công khai — 15/10/2026

Cập nhật 16/10/2026: phần API/dịch/kiểm tra lời đọc có 194 kiểm thử tập trung đạt trong workspace phát triển; xem [báo cáo phạm vi](API-TRANSLATION-FOCUS-2026-10-16.md). Bản public cũng được kiểm lại với runner/DATA riêng. Test API mới dùng model/config identity tổng hợp và chặn inference, không phụ thuộc weights trên máy phát triển.

Các kiểm tra này áp dụng cho snapshot mã nguồn trước khi đưa lên GitHub. Chúng
không thay thế việc cài trên máy sạch hoặc duyệt nội dung của video thực tế.

- Python source được parse bằng AST trước khi commit; không khởi chạy app để kiểm cú pháp.
- 12 file JavaScript trong `static/` vượt qua `node --check`.
- Các liên kết Markdown tương đối trong tài liệu hiện có đều trỏ tới file tồn tại.
- `tools/run_tests.py --profile media --pattern test_speech*.py`: **88 bài đạt**.
- `tools/run_tests.py --profile media --pattern test_pipeline_resume.py`: **25 bài đạt**.
- Test được chạy từ checkout public, sử dụng DATA riêng do runner tạo;
  `production_data_used=false`. Dùng Python/dependencies và FFmpeg đã cài trên
  máy phát triển; chưa thử cài mới toàn bộ dependencies từ Internet trên máy sạch.
- Schema test giọng dùng model/config identity giả trong DATA thử, giữ bộ kiểm
  tra giọng thật và chặn inference. Không sao chép model weights vào repository.
- Fixture hình/tiếng là test pattern và âm sin tạo cục bộ bằng
  `tools/make_review_fixtures.py`; không phải lời đọc người dùng, cũng không đủ
  để kiểm ASR hoặc độ đúng của phát âm. Media fixture được Git bỏ qua.
- Rà chính danh sách staged: không có operational DATA, model/runtime, hồ sơ
  trình duyệt, database, media thật, log, hội thoại hoặc binary launcher.
- Quét mẫu secret/private-key và đường dẫn riêng chỉ thấy các key-shaped
  sentinel đã xác định là dữ liệu test tổng hợp. Không đọc Windows vault hay
  provider key để thực hiện bước công bố.

Giới hạn: đây là kiểm tra mẫu và allowlist, không phải cam kết tuyệt đối rằng
mọi dữ liệu nhạy cảm có thể tồn tại trong tương lai đều được phát hiện. Phải
rà lại diff và manifest mỗi lần thêm nguồn/tài liệu mới; `.gitignore` không
thay thế việc rà nội dung đã staged.

Phần giọng của ca video thực tế vẫn có `lexical_verification=unverified`,
và hạn chế timing được ghi trong [PROJECT_STATUS](PROJECT_STATUS.md).
