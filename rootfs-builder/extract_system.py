#!/usr/bin/env python3
"""
extract_system.py — Extract ext4 filesystem from Android super partition (gDla format).

Parses the LP metadata headers inside the "gDla4" wrapped super partition,
finds the "system_a" (or "system") logical volume, and extracts its ext4 image.

Usage:
    python3 extract_system.py system.img output_dir
"""

import struct
import sys
import os
import errno

SUPER_PARTITION_START_SECTOR = 4096  # sector 4096 of the GPT disk
SECTOR_SIZE = 512


class LpMetadata:
    """Minimal parser for Android Logical Partition metadata."""

    HEADER_MAGIC = b'0PLA'  # LP_METADATA_HEADER_MAGIC_V0

    def __init__(self, data: bytes, base_offset: int):
        self.base_offset = base_offset
        self.data = data
        self.parse_header()

    def parse_header(self):
        h = self.data
        self.magic = h[0:4]
        self.major_version = struct.unpack_from('<H', h, 4)[0]
        self.minor_version = struct.unpack_from('<H', h, 6)[0]
        self.header_size = struct.unpack_from('<I', h, 8)[0]
        self.tables_size = struct.unpack_from('<I', h, 44)[0]
        self.partitions_offset, self.num_partitions, self.partition_entry_size = struct.unpack_from('<III', h, 80)
        self.extents_offset, self.num_extents, self.extent_entry_size = struct.unpack_from('<III', h, 92)
        self.groups_offset, self.num_groups, self.group_entry_size = struct.unpack_from('<III', h, 104)
        self.block_devices_offset, self.num_block_devices, self.block_device_entry_size = struct.unpack_from('<III', h, 116)

    def partitions(self):
        """Yield (name, num_extents, extent_table_index) for each partition."""
        pos = self.header_size + self.partitions_offset
        for i in range(self.num_partitions):
            name_bytes = self.data[pos:pos + 36]
            name = name_bytes.split(b'\0')[0].decode('ascii', errors='replace')
            first_extent_index = struct.unpack_from('<I', self.data, pos + 40)[0]
            num_extents = struct.unpack_from('<I', self.data, pos + 44)[0]
            yield name, num_extents, first_extent_index
            pos += self.partition_entry_size

    def extents_for_partition(self, first_extent_index: int, count: int):
        """Yield (num_sectors, target_sector, flags) for each extent."""
        pos = self.header_size + self.extents_offset + first_extent_index * self.extent_entry_size
        for i in range(count):
            num_sectors = struct.unpack_from('<Q', self.data, pos)[0]
            target_type = struct.unpack_from('<I', self.data, pos + 8)[0]
            target_sector = struct.unpack_from('<Q', self.data, pos + 12)[0]
            if target_type != 0:  # LP_TARGET_TYPE_LINEAR
                raise ValueError(f"Unsupported LP extent target type: {target_type}")
            yield num_sectors, target_sector
            pos += self.extent_entry_size


def find_lp_headers(disk_path: str):
    """Scan the super partition for LP metadata headers."""
    with open(disk_path, 'rb') as f:
        f.seek(SUPER_PARTITION_START_SECTOR * SECTOR_SIZE)
        data = f.read(100 * 1024 * 1024)  # First 100MB of super partition

        results = []
        offset = 0
        while True:
            pos = data.find(LpMetadata.HEADER_MAGIC, offset)
            if pos < 0:
                break
            if pos + 128 <= len(data):
                results.append(pos)
            offset = pos + 1
        return results


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} system.img output_dir")
        print(f"  Extract ext4 system from Android 14 super partition image")
        sys.exit(1)

    disk_path = sys.argv[1]
    output_dir = sys.argv[2]

    if not os.path.exists(disk_path):
        print(f"Error: {disk_path} not found")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    print(f"[*] Scanning {disk_path} for LP metadata...")
    offsets = find_lp_headers(disk_path)
    
    if not offsets:
        print("[!] No LP metadata found. Is this a valid Android super partition?")
        sys.exit(1)
    
    print(f"[+] Found LP headers at offsets: {offsets[:5]}")

    # Use the first metadata slot
    lp_offset = offsets[0]
    
    with open(disk_path, 'rb') as f:
        # Read LP metadata block (typically up to 1MB)
        f.seek(SUPER_PARTITION_START_SECTOR * SECTOR_SIZE + lp_offset)
        meta_data = f.read(1024 * 1024)  # 1MB should contain tables

        lp = LpMetadata(meta_data, lp_offset)
        print(f"\n[*] LP metadata at partition2 offset {lp_offset}:")
        print(f"    Version: {lp.major_version}.{lp.minor_version}")
        print(f"    Partitions: {lp.num_partitions}")
        print(f"    Extents: {lp.num_extents}")
        print(f"    Tables size: {lp.tables_size}")

        # Find system partition
        target_names = ['system_a', 'system', 'system_b']
        system_info = None
        
        print("\n[*] Available partitions:")
        for name, n_ext, first_ext in lp.partitions():
            print(f"    - {name}: {n_ext} extents")
            if name in target_names:
                system_info = (name, n_ext, first_ext)

        if not system_info:
            print("\n[!] System partition not found!")
            return

        sys_name, n_ext, first_ext = system_info
        print(f"\n[+] Extracting '{sys_name}' ({n_ext} extents)...")

        # Calculate total size
        total_sectors = 0
        extents = []
        for num_sectors, target_sector in lp.extents_for_partition(first_ext, n_ext):
            total_sectors += num_sectors
            extents.append((num_sectors, target_sector))
            print(f"    Extent: {num_sectors} sectors @ offset {target_sector}")

        total_bytes = total_sectors * SECTOR_SIZE
        print(f"\n[+] Total size: {total_bytes} bytes ({total_bytes / (1024*1024):.1f} MB)")

        # System partition output file (ext4 image)
        ext4_path = os.path.join(output_dir, "system_ext4.img")
        print(f"\n[*] Writing ext4 image to {ext4_path}...")
        
        with open(ext4_path, 'wb') as out:
            for num_sectors, target_sector in extents:
                # The target_sector is relative to the SUPER partition
                seek_pos = SUPER_PARTITION_START_SECTOR * SECTOR_SIZE + target_sector * SECTOR_SIZE
                f.seek(seek_pos)
                bytes_to_read = num_sectors * SECTOR_SIZE
                
                # Read in chunks to handle large extents
                chunk = 32 * 1024 * 1024  # 32MB chunks
                remaining = bytes_to_read
                while remaining > 0:
                    read_size = min(chunk, remaining)
                    data = f.read(read_size)
                    if not data:
                        break
                    out.write(data)
                    remaining -= len(data)
                    pct = ((bytes_to_read - remaining) / bytes_to_read) * 100
                    sys.stdout.write(f"\r    Extent progress: {pct:.1f}%")
                    sys.stdout.flush()
                print()

        # Mount the ext4 image
        mount_point = os.path.join(output_dir, "rootfs")
        os.makedirs(mount_point, exist_ok=True)
        
        # Try using debugfs to verify
        print(f"\n[*] Verifying ext4 image with debugfs...")
        import subprocess
        result = subprocess.run(
            ['debugfs', '-R', 'ls /', ext4_path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print(f"[+] Image is valid ext4. Contents:")
            print(f"    {result.stdout[:200]}")
            print(f"    ...")
        else:
            print(f"[!] Verification error: {result.stderr[:200]}")

        print(f"\n[*] To mount:")
        print(f"    sudo mount -o loop {ext4_path} {mount_point}")
        print(f"[*] Then run debloat:")
        print(f"    ./scripts/debloat_aosp.sh --target-dir {mount_point}")

        # Also try to directly extract into rootfs using debugfs
        print(f"\n[*] Attempting direct extraction to {mount_point}...")
        result = subprocess.run(
            ['debugfs', '-R', f'rdump / {mount_point}', '-w', ext4_path],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print(f"[+] Extraction complete!")
            ls_out = subprocess.run(
                ['ls', mount_point],
                capture_output=True, text=True, timeout=5
            )
            print(f"    Contents: {ls_out.stdout[:200]}")
        else:
            print(f"[!] 'rdump' failed, try manual mount instead:")
            print(f"    sudo mount -o loop {ext4_path} {mount_point}")
            if result.stderr:
                print(f"    Error: {result.stderr[:200]}")


if __name__ == '__main__':
    main()
