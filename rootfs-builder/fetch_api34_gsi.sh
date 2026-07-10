#!/bin/bash
# ==============================================================================
# PHISHADOW DROID: ANDROID 14 (API 34) GSI CORE FETCHER
# ==============================================================================
# Tải và trích xuất AOSP 14 Vanilla x86_64 (TrebleDroid GSI)
# Cung cấp hệ thống API hiện đại nhất để chạy 100% ứng dụng APK mới.

set -e

WORK_DIR="$(pwd)"
OUTPUT_DIR="${WORK_DIR}/aosp14_core"
# Sử dụng bản TrebleDroid AOSP 14 v416.0 (Vanilla, x86_64, không Root/Superuser mặc định để bảo mật container)
GSI_URL="https://github.com/TrebleDroid/treble_experimentations/releases/download/v416.0/system-td-based-x86_64-vanilla.img.xz"
IMG_FILE="system.img"

if [ "$EUID" -ne 0 ]; then
  echo "[!] Yêu cầu chạy script bằng quyền root (sudo)."
  exit 1
fi

echo "[*] Khởi tạo quy trình nạp Android 14 (API 34) GSI..."

# 1. Cài đặt dependency xử lý ảnh GSI
echo "[*] Cài đặt công cụ giải nén (xz-utils, e2fsprogs)..."
apt-get update -qq
apt-get install -y wget xz-utils e2fsprogs

# 2. Dọn dẹp môi trường
rm -rf "${OUTPUT_DIR}" system.img.xz system.img mount_temp
mkdir -p "${OUTPUT_DIR}"
mkdir -p mount_temp

# 3. Kéo GSI từ upstream (Băng thông cao)
echo "[*] Đang tải AOSP 14 GSI x86_64..."
wget --show-progress -O system.img.xz "${GSI_URL}"

# 4. Giải nén ảnh hệ thống (.img.xz -> .img)
echo "[*] Đang giải nén file .xz (Sẽ tốn chút thời gian tùy thuộc vào CPU)..."
unxz -k system.img.xz

# 5. Phân tích và trích xuất dữ liệu thô (EXT4)
echo "[*] Kiểm tra tính toàn vẹn của phân vùng EXT4..."
e2fsck -y -f system.img || true
resize2fs system.img || true

echo "[*] Đang mount Image vào thư mục tạm..."
mount -o loop,ro system.img mount_temp/

echo "[*] Đang sao chép toàn bộ cấu trúc file system sang không gian làm việc..."
cp -a mount_temp/* "${OUTPUT_DIR}/"

# 6. Dọn dẹp rác hệ thống
echo "[*] Hủy mount và dọn dẹp file tạm..."
umount mount_temp/
rm -rf mount_temp system.img system.img.xz

# 7. Khởi tạo các điểm gắn kết (Mount points) cho Container Daemon
echo "[*] Khởi tạo các Node giao tiếp hệ thống (dev, proc, sys, vendor, data)..."
mkdir -p "${OUTPUT_DIR}/dev"
mkdir -p "${OUTPUT_DIR}/proc"
mkdir -p "${OUTPUT_DIR}/sys"
mkdir -p "${OUTPUT_DIR}/vendor"
mkdir -p "${OUTPUT_DIR}/data"

echo "======================================================="
echo "[+] XONG! Bản AOSP 14 (API 34) GSI gốc đã sẵn sàng tại: ${OUTPUT_DIR}"
echo "[+] Môi trường này là nguyên bản 100%, chưa qua cắt gọt."
echo "======================================================="