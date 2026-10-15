# Bắt đầu với bản mã nguồn

Đây là hướng dẫn thiết lập môi trường phát triển trên **Windows 11 x64**, không phải bộ cài portable hoặc cam kết chạy đủ mọi engine sau một lệnh. Runtime, model, FFmpeg và dữ liệu nghiệm thu riêng không nằm trong repository. Bản public chưa được kiểm cài mới hoàn chỉnh trên một máy sạch; khả năng chạy từng tính năng phụ thuộc các thành phần đã chuẩn bị.

Nếu mục tiêu chỉ là đưa source cho AI đọc, không cần cài model hoặc nhập API key. Bắt đầu từ [kiến trúc](ARCHITECTURE.md) và [hướng dẫn review](AI_REVIEW_GUIDE.md).

## 1. Yêu cầu nền

| Thành phần | Mục đích |
| --- | --- |
| Windows 11 x64 | Môi trường desktop chính; dùng khóa tiến trình và Windows Credential Manager |
| Python 3.12 x64 | Môi trường Python chính của backend/worker |
| Microsoft Edge WebView2 Runtime | Hiển thị cửa sổ pywebview |
| FFmpeg và ffprobe | Giải mã, kiểm media, căn tốc độ, dựng âm thanh và xuất video |
| Node.js, nếu chạy các test JavaScript | Thực thi một số fixture hợp đồng frontend |
| Model/runtime theo tính năng | Whisper, Piper, LaMa, VieNeu và các công cụ tùy chọn không có trong Git |

GPU CUDA phù hợp có thể tăng tốc các engine được hỗ trợ; mức RAM/VRAM và hiệu năng cần được kiểm trên máy đích. Không suy diễn cấu hình GPU bất kỳ sẽ chạy được toàn bộ model.

## 2. Lấy source và tạo môi trường

```powershell
git clone https://github.com/phuognkiet-max/npk-auto-sub.git
cd npk-auto-sub
py -3.12 -m venv runtime\venv
.\runtime\venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` mô tả dependencies Python chính. Nó không tự cung cấp mọi binary native, model weights hoặc môi trường engine riêng. PyTorch được cài riêng theo hướng dẫn trong file và khả năng máy đích.

Ứng dụng tìm FFmpeg/ffprobe trước trong `runtime/ffmpeg`, sau đó trên PATH. Chuẩn bị bản Windows tin cậy và giấy phép tương ứng; kiểm tra `ffmpeg -version` và `ffprobe -version` trong môi trường sẽ chạy ứng dụng. Không chép runtime hoặc model của người khác mà chưa kiểm nguồn và điều khoản.

## 3. Các tùy chọn cài engine

Đọc script trước khi chạy. `setup.ps1` tạo môi trường dưới thư mục ứng dụng, tìm Python qua `NPK_BASE_PYTHON`, Python launcher hoặc lệnh Python hiện có, rồi kiểm Python 3.12. Script có thể tải **PyTorch CUDA và model lớn**, tốn dung lượng, băng thông và thời gian. Đây không phải bước bắt buộc để review source hoặc chạy các test thuần có mock.

```powershell
# Chỉ chạy khi đã đọc và chủ động muốn cài các thành phần trong script:
.\setup.ps1
```

Script nền không cài đủ mọi tùy chọn VieNeu, tạo giọng riêng hoặc tách nhạc. Các công cụ thiết lập riêng nằm trong `tools/`, chẳng hạn `setup_independent_vieneu.py`, `setup_voice_enrollment.py` và `setup_audio_separation.py`. Chúng có dependencies, download và yêu cầu phần cứng riêng; đừng chạy đồng loạt chỉ để làm một bài test pass.

LaMa/Piper/Whisper/VieNeu có vị trí model và cách tìm runtime tương ứng trong source. Khi thiếu engine, cần đọc chẩn đoán để xác định thành phần cần chuẩn bị. Source snapshot chưa cung cấp một quy trình bootstrap sạch đã kiểm chứng cho mọi tổ hợp engine.

## 4. Mở ứng dụng từ source

Sau khi chuẩn bị các thành phần cần thiết:

```powershell
.\runtime\venv\Scripts\python.exe app.py
```

Không có launcher EXE trong bản public. Nếu một script lịch sử nhắc EXE, đó không phải tệp đã đi kèm repository này.

`app.py` mở cửa sổ desktop và backend loopback, khóa một owner cho DATA. Dữ liệu mặc định được tạo dưới `data/` trong checkout. Không dùng đồng thời hai phiên trên cùng DATA và không khởi chạy thêm backend để né một phiên đang sở hữu hàng đợi.

Nếu muốn môi trường thử riêng, chọn thư mục DATA trước khi chạy, ví dụ:

```powershell
$env:VIETDUB_DATA_DIR = Join-Path $PWD 'build\manual-dev-data'
.\runtime\venv\Scripts\python.exe app.py
```

Biến môi trường này áp dụng cho shell hiện tại. Dùng media thử riêng. Test tự động nên dùng runner ở phần tiếp theo để có thêm ranh giới vault/mạng/inference.

## 5. Kiểm thử có ranh giới

Khi đã có môi trường Python/dependencies phù hợp, dùng runner của repository. Nếu môi trường Python hiện có nằm ngoài checkout, thay đường dẫn Python trong lệnh; không cần sao chép cả môi trường vào Git.

```powershell
.\runtime\venv\Scripts\python.exe tools\run_tests.py --pattern test_speech_auto_schedule.py --verbose
.\runtime\venv\Scripts\python.exe tools\run_tests.py --profile media --pattern test_speech*.py --verbose
```

Profile `unit` mặc định tạo DATA riêng và chặn socket/vault/inference/native process theo các guard Python. Profile `media` cho phép FFmpeg/ffprobe/Node fixture giới hạn, vẫn chặn inference và dịch vụ thật. Profile `native` là opt-in riêng, có thể tải engine đã cài vào RAM/VRAM và tốn tài nguyên. Guard Python không phải sandbox hệ điều hành. Xem [TEST-BOUNDARIES](TEST-BOUNDARIES.md).

Một số bài media cần fixture không đưa vào Git. Khi đã có FFmpeg, có thể tạo fixture tổng hợp bằng:

```powershell
.\runtime\venv\Scripts\python.exe tools\make_review_fixtures.py
```

Công cụ này tạo âm sin và video tổng hợp để kiểm media cơ bản, **không tạo giọng, không gọi provider và không thay thế dữ liệu nghe/xem nghiệm thu thật**. Fixture tổng hợp không chứng minh OCR/dịch/TTS đạt chất lượng với nội dung thực. Các script nghiệm thu lịch sử có thể yêu cầu runtime hoặc dữ liệu riêng không được phát hành; không phải mọi script cũ đều là bài test tái hiện được chỉ từ checkout sạch.

Không chạy `python -m unittest discover` trực tiếp khi chưa thiết lập DATA và boundaries trước import. Không dùng API key thật trong fixture. Thất bại do thiếu engine/media/tool cần được báo là thiếu điều kiện thử, không sửa bằng cách bỏ các rào kiểm.

## 6. Kết nối dịch vụ khi thực sự muốn dịch

Chỉ nhập khóa trực tiếp trong ứng dụng, tại phần kết nối dịch vụ. Không commit khóa, dán vào issue hoặc đưa cho AI review. Khóa được lưu qua Windows Credential Manager; token phiên local là loại thông tin khác.

Chọn đúng provider, endpoint và model từ catalog của kết nối. Lưu khóa, tải catalog và sinh nội dung là các mức kiểm khác nhau. Tải model catalog không chứng minh tài khoản có số dư/quota sinh nội dung. Dịch có thể phát sinh phí; nhịp request cục bộ không bảo đảm quota từ nhà cung cấp.

Muốn chạy video thật, hãy bắt đầu với đoạn thử ngắn có quyền sử dụng, kiểm chất lượng phụ đề/giọng/vùng chữ, rồi mới chạy toàn bộ. Một pipeline có thể hoàn tất kỹ thuật mà vẫn cần chỉnh đồng bộ hoặc phát âm. Đọc [trạng thái thực tế](PROJECT_STATUS.md) trước khi đánh giá kết quả.
