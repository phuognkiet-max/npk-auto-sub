# Thành phần và nguồn tham khảo

Mã giao diện, quản lý dự án, hàng đợi và điều phối xử lý trong VietDub Studio được viết mới. Không dùng source, API key, mã kích hoạt hay runtime của Xtrans. Các thư viện/model dưới đây là thành phần bên thứ ba, không phải tài sản độc quyền của ứng dụng này. Cần tuân thủ giấy phép tương ứng nếu phân phối bản build.

| Thành phần | Nguồn chính thức | Ghi chú |
|---|---|---|
| LaMa | https://github.com/advimman/lama | Apache-2.0; Suvorov et al., Resolution-robust Large Mask Inpainting with Fourier Convolutions |
| TorchScript LaMa | https://github.com/enesmsahin/simple-lama-inpainting | Apache-2.0; dùng model release v0.1.0 big-lama.pt; bộ điều phối ROI viết riêng |
| PyTorch | https://github.com/pytorch/pytorch | BSD-style; bản CUDA có thêm điều khoản thư viện NVIDIA |
| Faster Whisper | https://github.com/SYSTRAN/faster-whisper | MIT; engine CTranslate2, model Whisper chuyển đổi |
| Whisper | https://github.com/openai/whisper | MIT |
| RapidOCR | https://github.com/RapidAI/RapidOCR | Apache-2.0; ONNX OCR đóng kèm package |
| Piper | https://github.com/OHF-Voice/piper1-gpl | GPL-3.0; kèm eSpeak-ng và các điều khoản tương ứng |
| VieNeu-TTS v3 Turbo | https://github.com/pnnbao97/VieNeu-TTS ; https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo | Model/preset công khai Apache-2.0 theo nhà phát hành; bản GGUF Q8_0 dùng trong NPK, giữ LICENSE trong models/vieneu-v3-turbo |
| audio.cpp | https://github.com/0xShug0/audio.cpp/releases/tag/v0.9.1 | Engine native Windows CPU/CUDA v0.9.1; LICENSE lưu trong runtime/audio-cpp |
| CAM++ speaker frontend/model | https://github.com/pnnbao97/VieNeu-TTS ; https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo | Encoder lấy cùng revision model VieNeu `61b85e3d937fbbacb387714180e8182823512523`; frontend tham khảo upstream commit `85344322b7258b4e25479b692e8e3396baf9db34`. Giữ giấy phép/model card upstream; SHA-256 encoder chốt trong installer. |
| kaldi-native-fbank | https://github.com/csukuangfj/kaldi-native-fbank ; https://pypi.org/project/kaldi-native-fbank/1.22.3/ | Apache-2.0; frontend 1.22.3 cài riêng trong runtime/voice-enrollment, giữ metadata và LICENSE của wheel. |
| UVR MDX Inst_HQ_4 | https://github.com/Anjok07/ultimatevocalremovergui ; https://github.com/TRvlvr/model_repo/releases/tag/all_public_uvr_models | Credit Ultimate Vocal Remover (UVR) và các nhà phát triển; kiến trúc MDX-Net của Kuielab / Woosung Choi. Nguồn, giấy phép cho ứng dụng bên thứ ba, model hash và metadata cố định trong audio_separation.py và models/audio-separation/ATTRIBUTION.txt. Chỉ dùng model public, không sao chép giao diện UVR. |
| SEA-G2P | https://pypi.org/project/sea-g2p/ | Bản 0.10.0 trong runtime/vieneu riêng; xem metadata giấy phép package khi phân phối |
| Giọng vi_VN vais1000 medium | https://huggingface.co/rhasspy/piper-voices/blob/main/vi/vi_VN/vais1000/medium/MODEL_CARD | Rhasspy / Michael Hansen; model card dẫn dataset VAIS-1000, CC BY 4.0, một người nói, 22.05 kHz; finetune từ lessac |
| FFmpeg | https://ffmpeg.org/ ; https://www.gyan.dev/ffmpeg/builds/ | Build FFmpeg 8.1 full hiện có trên máy được chép vào runtime riêng. LICENSE đi kèm ở runtime/ffmpeg/LICENSE; kiểm tra cấu hình build khi phân phối |
| pywebview | https://github.com/r0x0r/pywebview | BSD-3-Clause; dùng Microsoft Edge WebView2 |
| FastAPI / Uvicorn | https://github.com/fastapi/fastapi ; https://github.com/encode/uvicorn | MIT / BSD-3-Clause |
| pysubs2 | https://github.com/tkarabela/pysubs2 | MIT |

API dịch được viết theo tài liệu nhà cung cấp:

- Gemini: https://ai.google.dev/api/generate-content
- DeepSeek: https://api-docs.deepseek.com/api/create-chat-completion/ và https://api-docs.deepseek.com/guides/json_mode/

Nghiên cứu kỹ thuật video inpainting:

- ProPainter: https://github.com/sczhou/ProPainter — chưa tích hợp; bảng bộ nhớ chính thức cho thấy cấu hình video lớn có thể vượt xa 4 GB VRAM.
- Video Subtitle Remover: https://github.com/YaoFANGUK/video-subtitle-remover — tham khảo lựa chọn engine, không dùng nó làm ứng dụng bọc hoặc sao chép giao diện.

Quyền sở hữu mã riêng không xóa nghĩa vụ giấy phép của dependencies. Piper hiện dùng GPL; trước khi đóng gói thương mại hoặc phát hành mã đóng, cần xem lại toàn bộ giấy phép và phương án TTS phù hợp. Bản này được triển khai để sử dụng/phát triển trên máy của bạn, chưa phải bộ phân phối thương mại đã rà soát pháp lý.
