#!/usr/bin/env python3
"""
Minimal ext4 reader — extracts files from an ext4 image without root/FUSE.
Handles: extents, filetype, flex_bg, 256B inodes, 64bit (basic).
"""

import struct, os, sys, stat as sm

EXT4_MAGIC = 0xEF53
EXT4_EXTENT_CSUM = 0x02000000  # CRC32 in extents

O_DIR = 2
O_FILE = 1
O_SYMLINK = 7


def read_at(f, off, size):
    f.seek(off)
    return f.read(size)


class Ext4:
    def __init__(self, path):
        self.f = open(path, "rb")
        self._load_sb()

    def _load_sb(self):
        sb = read_at(self.f, 1024, 1024)
        self.inodes_count = struct.unpack_from("<I", sb, 0)[0]
        self.blocks_count_lo = struct.unpack_from("<I", sb, 4)[0]
        self.blocks_per_group = struct.unpack_from("<I", sb, 32)[0]
        self.inodes_per_group = struct.unpack_from("<I", sb, 40)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0] or 256
        self.feature_incompat = struct.unpack_from("<I", sb, 96)[0]
        self.feature_ro_compat = struct.unpack_from("<I", sb, 100)[0]
        log_bsize = struct.unpack_from("<I", sb, 24)[0]
        self.block_size = 1024 << log_bsize
        self.num_groups = (self.blocks_count_lo + self.blocks_per_group - 1) // self.blocks_per_group
        self.blocks_count_hi = struct.unpack_from("<I", sb, 152)[0]
        self.has_64bit = bool(self.feature_ro_compat & 0x80)
        self.has_flex_bg = bool(self.feature_incompat & 0x200)
        self.flex_bg_size = 16  # default
        self._gdt_offset = 2 * self.block_size if self.block_size == 1024 else self.block_size
        self.gdt_entry_size = 64 if self.has_64bit else 32

    def _gdt_entry(self, group):
        off = self._gdt_offset + group * self.gdt_entry_size
        d = read_at(self.f, off, self.gdt_entry_size)
        bg_inode_table_lo = struct.unpack_from("<I", d, 8)[0]
        bg_inode_table_hi = struct.unpack_from("<I", d, 40)[0] if self.has_64bit and len(d) >= 44 else 0
        return bg_inode_table_lo | (bg_inode_table_hi << 32)

    def _empty_inode(self):
        return {"mode": 0, "uid": 0, "gid": 0, "size": 0, "atime": 0,
                "ctime": 0, "mtime": 0, "dtime": 0, "links": 0,
                "flags": 0, "blocks": 0, "i_block": b"\0" * 60, "gen": 0}

    def _read_inode(self, inode_num):
        group = (inode_num - 1) // self.inodes_per_group
        idx = (inode_num - 1) % self.inodes_per_group
        bg_itable_block = self._gdt_entry(group)
        offset = bg_itable_block * self.block_size + idx * self.inode_size
        if offset >= os.fstat(self.f.fileno()).st_size:
            return self._empty_inode()
        raw = read_at(self.f, offset, self.inode_size)
        mode = struct.unpack_from("<H", raw, 0)[0]
        uid = struct.unpack_from("<H", raw, 2)[0]
        size_lo = struct.unpack_from("<I", raw, 4)[0]
        atime = struct.unpack_from("<I", raw, 8)[0]
        ctime = struct.unpack_from("<I", raw, 12)[0]
        mtime = struct.unpack_from("<I", raw, 16)[0]
        dtime = struct.unpack_from("<I", raw, 20)[0]
        gid = struct.unpack_from("<H", raw, 24)[0]
        links = struct.unpack_from("<H", raw, 26)[0]
        blocks_count = struct.unpack_from("<I", raw, 28)[0]
        flags = struct.unpack_from("<I", raw, 32)[0]
        os_spec = read_at(self.f, offset + 64, 12)  # osd1
        # i_block (extent tree root or block pointers) — 60 bytes at offset 40
        i_block = raw[40:100]
        # Extended attributes
        gen = struct.unpack_from("<I", raw, 100)[0]
        ea_blocks_lo = struct.unpack_from("<H", raw, 106)[0]
        size_hi = struct.unpack_from("<I", raw, 108)[0] if self.inode_size > 108 else 0
        size = size_lo | (size_hi << 32)
        frag = raw[104] if len(raw) > 104 else 0
        fsize = raw[105] if len(raw) > 105 else 0
        return {
            "mode": mode,
            "uid": uid,
            "gid": gid,
            "size": size,
            "atime": atime,
            "ctime": ctime,
            "mtime": mtime,
            "dtime": dtime,
            "links": links,
            "flags": flags,
            "blocks": blocks_count,
            "i_block": i_block,
            "gen": gen,
        }

    def _extent_read(self, i_block):
        """Read file data using extents tree."""
        # i_block is 60 bytes: header (12) + up to 4 extent entries
        eh_magic = struct.unpack_from("<H", i_block, 0)[0]
        if eh_magic != 0xF30A:
            # Fallback: might be inline data or direct blocks
            return b""
        eh_entries = struct.unpack_from("<H", i_block, 2)[0]
        eh_max = struct.unpack_from("<H", i_block, 4)[0]
        eh_depth = struct.unpack_from("<H", i_block, 6)[0]
        eh_generation = struct.unpack_from("<I", i_block, 8)[0]

        entries = []
        pos = 12
        for _ in range(eh_entries):
            if pos + 12 > len(i_block):
                break
            ee_block = struct.unpack_from("<I", i_block, pos)[0]
            ee_len = struct.unpack_from("<H", i_block, pos + 4)[0]
            ee_start_hi = struct.unpack_from("<H", i_block, pos + 6)[0]
            ee_start_lo = struct.unpack_from("<I", i_block, pos + 8)[0]
            entries.append((ee_block, ee_len, ee_start_hi, ee_start_lo))
            pos += 12

        if eh_depth > 0:
            # Index block — need to recurse
            data = b""
            for _, _, hi, lo in entries:
                idx_blk = (hi << 32) | lo
                self.f.seek(idx_blk * self.block_size)
                idx_data = self.f.read(self.block_size)
                data += self._extent_read(idx_data)
            return data
        else:
            # Leaf extents
            data = b""
            for ee_block, ee_len, ee_start_hi, ee_start_lo in entries:
                phys_blk = (ee_start_hi << 32) | ee_start_lo
                length = min(ee_len, 32768)  # max extent length
                self.f.seek(phys_blk * self.block_size)
                # Uninit extents have ee_len as negative; we handle length
                for i in range(length):
                    data += self.f.read(self.block_size)
            return data

    def _read_inode_data(self, inode):
        size = inode["size"]
        if size == 0:
            return b""
        i_block = inode["i_block"]
        eh_magic = struct.unpack_from("<H", i_block, 0)[0]
        is_extent = bool(inode["flags"] & 0x80000) and eh_magic == 0xF30A
        if not is_extent:
            data = i_block[:min(size, 60)]
            return data
        data = self._extent_read(i_block)
        if data:
            return data[:size]
        return b""

    def read_dir(self, inode_num):
        """Read directory entries — returns list of (inode, name, filetype)."""
        inode = self._read_inode(inode_num)
        if not sm.S_ISDIR(inode["mode"]):
            return []
        raw = self._read_inode_data(inode)
        entries = []
        pos = 0
        while pos + 8 <= len(raw):
            ino = struct.unpack_from("<I", raw, pos)[0]
            rec_len = struct.unpack_from("<H", raw, pos + 4)[0]
            name_len = raw[pos + 6]
            file_type = raw[pos + 7]
            if rec_len == 0 or ino == 0:
                pos += 1
                continue
            name = raw[pos + 8:pos + 8 + name_len].decode("utf-8", errors="replace")
            filetype = file_type
            entries.append((ino, name, filetype))
            pos += rec_len
        return entries

    def extract(self, out_dir):
        """Extract entire filesystem."""
        os.makedirs(out_dir, exist_ok=True)
        self._extract_inode(2, out_dir, "/")

    def _extract_inode(self, inode_num, phys_path, virt_path, depth=0):
        if depth > 50:
            return
        try:
            inode = self._read_inode(inode_num)
            mode = inode["mode"]
        except Exception as e:
            sys.stdout.write(f"\n[!] Error reading inode {inode_num}: {e}\n")
            return
        
        if not mode:
            return
        
        if sm.S_ISDIR(mode):
            os.makedirs(phys_path, exist_ok=True)
            try:
                entries = self.read_dir(inode_num)
            except Exception as e:
                sys.stdout.write(f"\n[!] Error reading dir {inode_num}: {e}\n")
                return
            for child_ino, name, ft in entries:
                if name in (".", ".."):
                    continue
                child_path = os.path.join(phys_path, name)
                self._extract_inode(child_ino, child_path, virt_path + "/" + name, depth + 1)
        elif sm.S_ISREG(mode):
            data = self._read_inode_data(inode)
            os.makedirs(os.path.dirname(phys_path), exist_ok=True)
            try:
                with open(phys_path, "wb") as f:
                    f.write(data)
            except OSError:
                pass
            sys.stdout.write(f"\r  {virt_path} ({inode['size']} bytes)      ")
            sys.stdout.flush()
        elif sm.S_ISLNK(mode):
            target = self._read_inode_data(inode).decode("utf-8", errors="replace")
            if not target:
                target = os.path.basename(phys_path)
            os.makedirs(os.path.dirname(phys_path), exist_ok=True)
            tmp_path = phys_path + ".tmp_symlink"
            try:
                os.symlink(target, tmp_path)
                os.rename(tmp_path, phys_path)
            except (FileExistsError, OSError):
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    def close(self):
        self.f.close()


if __name__ == "__main__":
    img = sys.argv[1] if len(sys.argv) > 1 else "/home/dang-nhat-phi/AeroAPK/rootfs-builder/waydroid_system.ext4"
    out = sys.argv[2] if len(sys.argv) > 2 else "/home/dang-nhat-phi/AeroAPK/rootfs-builder/aosp14_google_core"
    
    print(f"[*] Reading ext4 image: {img}")
    print(f"[*] Output directory: {out}")
    
    ext4 = Ext4(img)
    
    try:
        print(f"[*] Block size: {ext4.block_size}")
        print(f"[*] Inode size: {ext4.inode_size}")
        print(f"[*] Groups: {ext4.num_groups}")
        print(f"[*] 64bit: {ext4.has_64bit}, FlexBG: {ext4.has_flex_bg}")
        
        # Test: read root directory
        print("\n[*] Reading root directory...")
        entries = ext4.read_dir(2)
        print(f"[+] Root directory entries ({len(entries)}):")
        for ino, name, ft in entries[:30]:
            print(f"    [{ft}] ino={ino}: {name}")
        if len(entries) > 30:
            print(f"    ... and {len(entries)-30} more")
        
        # Start extraction
        print(f"\n[*] Extracting filesystem...")
        ext4.extract(out)
        print(f"\n[+] Extraction complete!")
        
        # Count extracted
        count = sum(len(files) for _, _, files in os.walk(out))
        print(f"[+] Total files: {count}")
        
    finally:
        ext4.close()
