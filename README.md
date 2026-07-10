# AeroAPK (AeroDroid) 🚀

![AeroAPK Banner](https://img.shields.io/badge/AeroAPK-Android_Container-success?style=for-the-badge&logo=android)

**AeroAPK** (hay còn gọi là AeroDroid) là một giải pháp môi trường chạy (container runtime) siêu nhẹ giúp bạn cài đặt và khởi chạy trực tiếp các tệp tin ứng dụng Android (`.apk`) trên hệ điều hành Linux. 

Dự án sử dụng lõi `shadow-droid-core` tận dụng công nghệ namespace trên Linux để giả lập môi trường Android một cách nguyên bản, hiệu năng cao và ít tốn tài nguyên nhất.

## ✨ Tính năng nổi bật

* ⚡ **Hiệu năng gốc (Native Performance):** Chạy ứng dụng Android gần như không có độ trễ nhờ vào công nghệ Linux Namespace thay vì máy ảo nặng nề.
* 📦 **Đóng gói siêu nhỏ (Quantum Compression):** Trình đóng gói `.deb` được tối ưu hóa với thuật toán nén `xz` mức độ cao nhất, giảm thiểu tối đa dung lượng phân phối.
* 🔒 **Bảo mật mã nguồn (Obfuscation):** Mã nguồn Python tự động được biên dịch sang dạng mã byte (Bytecode) khi đóng gói, giúp bảo vệ logic cốt lõi của ứng dụng.
* 🎨 **Hỗ trợ Wayland/Weston:** Tích hợp mượt mà với giao diện đồ họa Linux hiện đại thông qua compositor Weston.
* 🤖 **Tương thích Android 14:** Hỗ trợ lõi `aosp14_google_core` cho khả năng tương thích app cực tốt.

## 🛠 Hướng dẫn cài đặt và Build

### Yêu cầu hệ thống
- Hệ điều hành Linux (khuyên dùng Ubuntu/Debian).
- Các gói phụ thuộc: `python3 (>= 3.10)`, `python3-pyqt6`, `util-linux`, `policykit-1`, `weston`, `libcap2-bin`.

### Xây dựng (Build) gói `.deb`

Để đóng gói ứng dụng từ mã nguồn, hãy chạy script build đi kèm:

```bash
# Cấp quyền thực thi nếu cần thiết
chmod +x ./scripts/build-deb.sh

# Tiến hành đóng gói ứng dụng
./scripts/build-deb.sh
```

Sau khi quá trình hoàn tất, tệp tin cài đặt sẽ được tạo ra tại đường dẫn: `dist/aerodroid_<version>_<arch>.deb`.

### Cài đặt ứng dụng

Sử dụng lệnh `apt` hoặc `dpkg` để cài đặt file `.deb` vừa được tạo ra:

```bash
sudo apt install ./dist/aerodroid_*.deb
```

## 🚀 Hướng dẫn đẩy lên GitHub

Nếu bạn vừa tạo repo này trên GitHub, hãy dùng các lệnh sau để tải source code của bạn lên:

```bash
git remote add origin https://github.com/<USERNAME>/AeroAPK.git
git branch -M main
git push -u origin main
```

---
*Phát triển bởi [Tên của bạn]* - *Mã nguồn mở và mạnh mẽ!*
