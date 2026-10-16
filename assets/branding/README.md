# Bộ nhận diện NPK Auto Sub

Ngày cập nhật: 2026-09-07. Phạm vi: icon và nhận diện Windows; không thay đổi tác vụ xử lý video, dữ liệu dự án hay API key.

## Thiết kế

Chữ N màu cam, khoảng âm gợi nút phát, hai vạch phụ đề màu sáng trên nền đen. Góc bo nhẹ và hình đơn giản để đọc được khi thu nhỏ trên taskbar.

Hình được tạo bằng **imagegen tích hợp trong Codex**, không dùng API key của người dùng. File master là kết quả gốc, giữ kênh alpha. Chỉ thu nhỏ và mã hóa ICO để đóng gói, không vẽ lại bằng mã.

- Master: `%NPK_WORKSPACE%\assets\branding\npk-auto-sub-master.png` — RGBA 1254 × 1254.
- Icon Windows: `%NPK_WORKSPACE%\static\npk-app-icon.ico` — 16, 20, 24, 32, 40, 48, 64, 96, 128 và 256 px.
- Logo giao diện: `%NPK_WORKSPACE%\static\npk-app-icon.png` — RGBA 256 × 256.
- Master SHA-256: `DC275E1EE626D6FD23D2D3E4B0A45620AD9946135D68344B28C35840B9800174`.

## Tích hợp

1. File `NPK Auto Sub.exe` được build lại với icon nhúng, không chỉ đổi icon shortcut.
2. Cửa sổ pywebview nạp ICO riêng, không lấy icon của Python.
3. AppUserModelID ổn định `NPK.AutoSub.Desktop`; lệnh mở lại trỏ vào NPK Auto Sub.exe có dấu ngoặc kép. Windows property store được dọn khi cửa sổ đóng thật sự.
4. Logo và favicon của giao diện dùng cùng bộ nhận diện.

Không sửa Python dùng chung, registry, danh sách ghim taskbar hay khởi động lại Explorer. Không đóng phiên NPK hiện có của người dùng.

## Sử dụng

Lưu dự án rồi đóng và mở lại `%NPK_WORKSPACE%\NPK Auto Sub.exe` để nhận icon cửa sổ mới. Nếu taskbar vẫn có mục Python đã ghim từ trước, bỏ ghim mục cũ và ghim lại NPK sau khi mở bản mới. Việc ghim và mở lại từ taskbar chưa được kiểm thử trực tiếp; đường dẫn và thuộc tính Windows đã được đọc lại trong cửa sổ thử.

Bản EXE trước thay đổi được giữ tại `%NPK_WORKSPACE%\build\icon-release\backup\NPK Auto Sub-before-icon.exe`. Đây là bản sao EXE, không phải bản sao đầy đủ mã nguồn trước thay đổi.

## Kiểm chứng

- 95 kiểm thử Python đạt, bao gồm kích thước/alpha ICO, đường dẫn mở lại và guard cửa sổ ngoài tiến trình.
- Cửa sổ Windows thử riêng: icon native 40 × 40; đọc lại đúng cả bốn thuộc tính AppUserModel; quan sát được icon mới trên thanh tiêu đề. Cửa sổ thử đã tự đóng.
- 17 nhóm kiểm tra giao diện đạt, không có lỗi JavaScript. Logo tải đúng; không tràn ngang ở chiều rộng 1920, 1280, 1024, 720 và 400 px.
- EXE đã nhúng 10 resource icon và một group icon; SHA-256 bản EXE cài vào: `A98CC17F199D07084FCE95B06F9238DCE43C7FD52696071B982436FF5DE9ADF5`.
- Kiểm thử dùng dữ liệu tạm riêng, không gọi dịch vụ dịch/giọng trả phí. Đây là kiểm tra bản vá icon, không phải nghiệm thu lại toàn bộ chất lượng xử lý video.

Báo cáo: `tests/icon-results/native-report.json`, `tests/studio-results/browser-results.json`.

## Tái tạo tài nguyên / build

Chạy trong `%NPK_WORKSPACE%` bằng PowerShell:

```powershell
& '.\runtime\venv\Scripts\python.exe' '.\tools\build-app-icon.py'
& '.\runtime\venv\Scripts\python.exe' -m PyInstaller --noconfirm --distpath build/icon-release/dist --workpath build/icon-release/work 'NPK Auto Sub.spec'
```

Build vào thư mục staging; kiểm tra và sao lưu bản đích trước khi triển khai. Gói ZIP RC1 cũ được giữ nguyên, không tự ghi đè.

## Prompt nguyên bản

```text
Use case: logo-brand.
Asset type: a single finished Windows desktop application icon for "NPK Auto Sub", a Vietnamese video subtitle and dubbing app, intended to be converted to a multi-resolution .ico.
Primary request: an original, unmistakable app icon replacing the generic Python icon. Create one bold geometric capital N in vivid orange (#FF941F), with an integrated angular right-pointing play-shaped negative-space cut, and two short thick ivory subtitle strokes beneath the N. The N is the dominant recognizable silhouette, not a separate floating play triangle. No wordmark or small lettering.
Composition: straight-on centered square 1024x1024 icon. A near-black (#101216) square tile with subtly rounded corners (about 9% radius) occupies about 92% of the canvas, transparent pixels outside the tile. The large orange N occupies most of the tile interior with balanced margins. The subtitle strokes are simple and sufficiently thick to remain visible at 24 pixels.
Style: precise flat graphic artwork with clean hard edges, high contrast, professional video software identity, minimal and optically balanced, not cute. Flat colors only.
Constraints: only one icon, no presentation sheet, no mockup, no surrounding UI, no glossy highlights, no gradients, no shadows, no 3D, no ornate borders, no AI sparkles, no unrelated symbols, no extra letters, no watermark. Preserve actual alpha transparency outside the tile.
```

## Tham chiếu Windows

- [Property store của cửa sổ và vòng đời](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shgetpropertystoreforwindow).
- [Lệnh mở lại ứng dụng](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-appusermodel-relaunchcommand).
- [Icon dùng để mở lại](https://learn.microsoft.com/en-us/windows/win32/properties/props-system-appusermodel-relaunchiconresource).
