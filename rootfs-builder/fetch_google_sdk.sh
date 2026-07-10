#!/bin/bash
# ==============================================================================
# PHISHADOW DROID: GOOGLE SDK AOSP 14 (API 34) x86_64 FETCHER
# ==============================================================================
set -e

WORK_DIR="$(pwd)"
OUTPUT_DIR="${WORK_DIR}/aosp14_google_core"

# URL trực tiếp từ repository của Google Android SDK (x86_64, API 34)
GOOGLE_SYS_IMG="https://dl.google.com/android/repository/sys-img/google_apis/x86_64-34_r14.zip"

if [ "$EUID" -ne 0 ]; then
  echo "[!] Yêu cầu chạy bằng quyền root (sudo)."
  exit 1
fi

echo "[*] Bắt đầu nạp Android 14 x86_64 từ Google SDK..."
apt-get update -qq && apt-get install -y wget unzip e2fsprogs

rm -rf "${OUTPUT_DIR}" sys-img.zip mount_temp extracted_img
mkdir -p "${OUTPUT_DIR}" mount_temp extracted_img

echo "[*] Kéo file ZIP từ server Google..."
wget --show-progress -O sys-img.zip "${GOOGLE_SYS_IMG}"

echo "[*] Đang giải nén file ZIP..."
unzip -q sys-img.zip -d extracted_img/

# Định vị file system.img bên trong thư mục giải nén
IMG_PATH=$(find extracted_img/ -name "system.img" | head -n 1)

echo "[*] Đang mount file ${IMG_PATH} vào thư mục tạm..."
e2fsck -y -f "${IMG_PATH}" || true
resize2fs "${IMG_PATH}" || true
mount -o loop,ro "${IMG_PATH}" mount_temp/

echo "[*] Sao chép toàn bộ cấu trúc file system sang không gian làm việc..."
cp -a mount_temp/* "${OUTPUT_DIR}/"

echo "[*] Dọn dẹp rác..."
umount mount_temp/
rm -rf mount_temp sys-img.zip extracted_img/

# Khởi tạo các điểm gắn kết (Mount points) bắt buộc cho LXC/Namespaces
mkdir -p "${OUTPUT_DIR}/dev" "${OUTPUT_DIR}/proc" "${OUTPUT_DIR}/sys" "${OUTPUT_DIR}/vendor" "${OUTPUT_DIR}/data"

echo "======================================================="
echo "[+] XONG! Bản Android 14 x86_64 gốc đã sẵn sàng tại: ${OUTPUT_DIR}"
echo "======================================================="