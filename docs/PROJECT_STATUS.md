# Trạng thái thực tế của dự án

Đồng bộ mã nguồn công khai **17/10/2026**: cập nhật RC10 với ba đợt làm sạch OCR/credit/lời đọc, ngân sách âm tiết tiếng Việt và chuẩn hóa dữ kiện Trung–Việt, bộ nhớ thuật ngữ xuyên batch, partial retry, model provenance và bảo toàn chỉnh sửa tay. Chi tiết nằm trong [Đợt 1](PHASE1-TEXT-REVIEW-RC10.md) và [Đợt 2–3](PHASE2-3-TRANSLATION-RELIABILITY-RC10.md). Việc đồng bộ này chưa tạo hoặc nghiệm thu thêm video.

Cập nhật **16/10/2026**: đã sửa lỗi gọi lại API vì JSON fence, sửa định dạng JSON của 9Router có giới hạn và ngữ cảnh khi chỉ dịch câu còn trống. Thêm **Kiểm tra lời đọc** cục bộ với preview và chủ động chọn từng gợi ý, để xử lý nhóm lặp/dòng ghi nguồn mà không sửa caption hoặc vùng xóa chữ. Chi tiết và giới hạn tại [API-TRANSLATION-FOCUS-2026-10-16](API-TRANSLATION-FOCUS-2026-10-16.md). Chưa tạo video mới hoặc xác minh lexical toàn bộ trong lượt cập nhật này.

Phiên bản source được chia sẻ là **NPK Auto Sub 0.8.5 RC10**, kèm thay đổi tự bố trí lời đọc ngày **15/10/2026**. Dự án đang phát triển; đây chưa phải sản phẩm đã nghiệm thu chất lượng chung, bộ cài portable hoặc bản phát hành đã kiểm lại trên máy sạch.

## Trạng thái video thực

Theo trạng thái và ảnh người dùng chia sẻ ngày **15/10/2026**, video dài 11 phút 35 giây có 381 câu đã dịch nhưng lượt tiếp tục dừng ở bước kiểm tra timing giọng đọc; ít nhất ba câu chưa thỏa giới hạn tự nhiên và cần xử lý/review. Chưa có thành phẩm video cuối được xác minh cho lượt này. Vì vậy kho này không khẳng định công cụ đã hoàn tất ổn định một video thực.

Các bài test pipeline/browser dùng media tổng hợp và kiểm tra hợp đồng xử lý. Chúng không thay thế việc chạy toàn bộ video thật, nghe/đọc duyệt bản dịch và lời đọc, rồi xác minh MP4 cuối. Không có video, audio hoặc hồ sơ dự án người dùng trong source public.

## Phần đã được đưa vào source

- Snapshot job tách khỏi dự án đang chỉnh sửa; revision giúp ngăn kết quả cũ ghi đè nội dung mới.
- Checkpoint OCR/dịch và journal từng bước, dùng lại phần hoàn tất khi tiếp tục hợp lệ.
- Phân biệt model đang nhập, model backend đã lưu và model của job đang chạy; kiểm hợp đồng trước request tiếp theo sau đổi cấu hình.
- Làm sạch handle/credit dùng chung, ngăn đọc rác OCR, gộp câu lặp và đánh dấu đoạn nguồn không phải lời thoại; xem lại lời đọc trước khi render.
- Ngân sách lời đọc tiếng Việt theo âm tiết, so khớp số/đơn vị Trung–Việt và truyền glossary tên riêng có chứng cứ xuyên các batch.
- Lưu câu hợp lệ ngay trong batch, chỉ retry ID lỗi/thiếu; ghi nhận model đã cấu hình và model response; mặc định không ghi đè câu sửa tay.
- Ledger lượt gửi/usage và budget dùng chung qua continuation; đổi model/key không tự tạo allowance mới.
- Scheduler theo provider/endpoint: một request đang gửi và cách nhau ít nhất 12 giây giữa các lần bắt đầu của NPK; bằng chứng quota bền qua restart.
- Đo raw audio, cache theo nội dung/giọng/tốc độ và kiểm âm thanh sau căn; tự bố trí là tùy chọn riêng, mặc định tắt để giữ hành vi cũ.
- Kiểm giải mã/thời lượng/fingerprint/hash cho artifact và thành phẩm trong các luồng tương ứng; phân biệt video xử lý chữ với video ghép cuối.
- Runner test với DATA riêng và ranh giới mạng, credentials, inference và native process theo profile.

Danh sách này mô tả cơ chế đã có, không phải chứng nhận rằng mọi đường gọi hoặc trường hợp lỗi đều hoàn hảo. Source vẫn cần review và bộ ca đánh giá phong phú hơn.

## Những giới hạn còn quan trọng

**Đồng bộ với hình:** tự bố trí giữ đủ lời và thứ tự trong video nhưng có thể đặt lời đọc sớm/muộn đáng kể. Mục tiêu giảm lệch về khoảng 2 giây trong thuật toán không phải trần cứng. Lệch khoảng 10,81 giây ở ca hiện tại là vấn đề chất lượng cần nghe/xem duyệt, dù file xuất hợp lệ.

**Phát âm và bản dịch:** kiểm tín hiệu/PCM/thời lượng không chứng minh model phát âm đủ mọi chữ, đúng tên riêng hoặc đúng ý. Chưa có kiểm chứng lexical toàn bộ. Dịch, OCR và lựa chọn track cần người dùng xem lại khi confidence/phạm vi có vấn đề.

**Mốc từ:** forced alignment không luôn thành công; 214 câu trong ca trên còn dùng ước lượng. Không thể coi mọi highlight là đã đồng bộ chính xác theo âm thanh.

**Chữ gốc:** làm mờ/che vùng không tái tạo nền. LaMa/Telea cũng có giới hạn với chuyển động, vùng chữ thay đổi hoặc nền phức tạp. Cần đánh giá hình theo từng video.

**Quota và độ tin cậy dịch vụ:** scheduler chỉ giới hạn các request đi qua NPK. Các chương trình khác dùng cùng router/tài khoản không nằm trong kiểm soát này. Quota, số dư và lỗi xác thực là vấn đề riêng; model khác không tự chứng minh kết nối cũ đã phục hồi.

**Cài đặt lại:** runtime/model/binary không có trong Git. Một số công cụ cài đặt có download lớn và môi trường riêng; chưa có kiểm cài mới toàn diện cho bản source public trên máy sạch.

**Bảo trì:** server, API và worker còn lớn; có nhiều thế hệ UI và script thử nghiệm lịch sử. Cần chuẩn hóa kế hoạch thực thi, transaction/checkpoint và phân tách trạng thái frontend/backend tốt hơn. Monitor bảo vệ được ẩn danh trong snapshot, nhưng vẫn cần tổng quát hóa cho triển khai mới.

**Kiểm thử:** test hợp đồng/fixture không thay thế chạy native engine hoặc người nghe/xem. Không sử dụng số bài test của các lượt kiểm cũ như bằng chứng rằng chính checkout public đã chạy đủ toàn bộ suite và mọi engine.

Các câu hỏi rà soát ưu tiên nằm trong [AI_REVIEW_GUIDE](AI_REVIEW_GUIDE.md). Hướng dẫn thiết lập nằm trong [GETTING_STARTED](GETTING_STARTED.md), còn phạm vi loại bỏ/chuẩn hóa của bản chia sẻ nằm trong [PUBLIC_SNAPSHOT](PUBLIC_SNAPSHOT.md).
