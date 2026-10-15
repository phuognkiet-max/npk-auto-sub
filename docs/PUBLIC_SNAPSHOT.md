# Phạm vi bản source chia sẻ

Repository này là **snapshot mã nguồn có chọn lọc để đọc, thảo luận và review**, không phải bản sao toàn bộ thư mục vận hành. Ứng dụng và dữ liệu riêng ban đầu được giữ ở môi trường gốc; bước chuẩn bị public tạo một checkout chia sẻ độc lập.

## Có trong bản chia sẻ

- Mã Python first-party để khởi chạy, lưu dự án, phục vụ API, quản lý hàng đợi và xử lý pipeline.
- HTML/CSS/JavaScript và các tài sản giao diện cần thiết được chọn.
- Test hợp đồng/fixture code và công cụ kiểm thử/thiết lập đã chọn.
- README mới, mô tả kiến trúc, hướng dẫn AI review, cách bắt đầu, trạng thái thực tế, ranh giới test và ghi nhận thành phần bên thứ ba.
- `PUBLIC_SOURCE_MANIFEST.json` ghi các tệp đã chọn cùng chuẩn hóa dùng cho snapshot.

Việc có installer script trong Git không nghĩa model/runtime tương ứng đã được phân phối cùng. Nội dung tham khảo một engine cũng không chứng minh engine đã được cài hoặc kiểm lại trên máy đọc source.

## Không đưa lên Git

- API key, Windows Credential Manager, token phiên, cấu hình xác thực hoặc hồ sơ trình duyệt.
- SQLite người dùng, `data/`, snapshot job, checkpoint nội dung thật, log và monitor vận hành thật.
- Video, audio, bản dịch hoặc giọng riêng của người dùng; media nghiệm thu và dữ liệu nhận dạng thật.
- Model weights, runtime Python/native, FFmpeg, môi trường ảo và cache download/inference.
- Binary launcher EXE, các sản phẩm build, báo cáo/kết quả test sinh ra và cache biên dịch.
- Thư mục upstream được tải/chép trong môi trường phát triển.
- Toàn bộ tài liệu chẩn đoán/lịch sử riêng cũ có thể chứa đường dẫn, ID dự án/job hoặc dữ liệu của ca người dùng. Tài liệu public mới thay thế chúng bằng mô tả tổng hợp không định danh.

Các tệp thiếu theo danh sách trên là chủ ý. Không yêu cầu người sở hữu tải cả `data/`, vault, model hoặc runtime lên issue để “hoàn thiện repository”. Khi cần bug report, nên tạo fixture tổng hợp tối thiểu và dữ liệu đã được phép chia sẻ.

## Những chuẩn hóa chỉ áp dụng cho snapshot public

1. Đường dẫn Windows gắn tài khoản máy phát triển được chuyển sang tên mẫu `developer` khi cần giữ hình dạng đường dẫn trong source/test.
2. ID dự án được bảo vệ trong hợp đồng monitor và các tham chiếu liên quan được thay đồng nhất bằng ID tổng hợp **`00000000000000000000000000000001`**. Đây không phải ID của một dự án có dữ liệu trong Git. Cơ chế monitor chưa tự trở thành cấu hình đa dự án chỉ nhờ thay ID.
3. `setup.ps1` tìm Python 3.12 qua cấu hình môi trường/lệnh có sẵn thay vì cố định đường dẫn Python của tài khoản phát triển.
4. README và tài liệu bàn giao được viết cho checkout nguồn độc lập, tránh dẫn người đọc tới hồ sơ nghiệm thu riêng bị loại bỏ.

Những chuẩn hóa này không có nghĩa đã refactor toàn bộ quy tắc vận hành thành sản phẩm đa máy. Cần đọc và kiểm lại cơ chế monitor, owner, vị trí engine và các điều kiện test khi triển khai mới. Không dùng ID mẫu hoặc fixture để sửa hash/metadata nhằm bỏ rào admission trên dữ liệu thật.

## Media fixture tổng hợp

`tools/make_review_fixtures.py` hỗ trợ tạo một số media thử từ âm sin và hình tổng hợp khi đã có FFmpeg. Nó không có giọng người dùng, không tạo TTS và không gọi provider. Kết quả được tạo cục bộ theo nhu cầu và không phải video nghiệm thu thật của dự án.

Fixture đó giúp kiểm đường media cơ bản; không thay thế benchmark OCR, dịch, phát âm, inpainting hoặc đồng bộ ngữ nghĩa. Các script lịch sử đòi dữ liệu/runtime không phát hành có thể không chạy được chỉ từ snapshot.

## Quyền sử dụng và giới hạn

Mã first-party được công khai để chủ dự án chia sẻ nguồn và thảo luận. **Chưa có giấy phép open source riêng được chủ dự án lựa chọn.** Không suy diễn MIT/Apache hoặc quyền tái phân phối thương mại từ việc repository có thể đọc công khai. Xem `LICENSE_STATUS.md`.

Dependencies, model, giọng và native binary có giấy phép riêng. [THIRD_PARTY](THIRD_PARTY.md) ghi nhận các nguồn liên quan, không cấp lại giấy phép cho chúng. Mỗi bản phân phối thực tế vẫn cần kiểm thành phần/phiên bản và nghĩa vụ tương ứng.

Source snapshot không kèm credentials, dữ liệu nghiệm thu riêng hoặc môi trường chạy đã cài, và chưa có bảo đảm cài mới đầy đủ trên máy sạch. Đánh giá code, kết quả test fixture, kiểm chứng media thật và duyệt chất lượng người dùng cần được báo tách bạch.
