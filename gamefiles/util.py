"""Shared helpers for the game-file readers: a binary reader, pure-Python LZ4,
optional-library loading, hex dumps and string extraction."""

import os
import re
import struct
import zlib


class Bin:
    """Little-endian binary reader over bytes."""

    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def seek(self, pos):
        self.p = pos
        return self

    def skip(self, n):
        self.p += n
        return self

    def tell(self):
        return self.p

    def left(self):
        return len(self.d) - self.p

    def read(self, n):
        if n < 0 or self.p + n > len(self.d):
            raise ValueError(f"read past end of data (at {self.p}, wanted {n}, size {len(self.d)})")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def _u(self, fmt, n):
        v = struct.unpack_from("<" + fmt, self.d, self.p)[0] if self.p + n <= len(self.d) else None
        if v is None:
            raise ValueError(f"read past end of data (at {self.p})")
        self.p += n
        return v

    def u8(self): return self._u("B", 1)
    def u16(self): return self._u("H", 2)
    def u32(self): return self._u("I", 4)
    def u64(self): return self._u("Q", 8)
    def i8(self): return self._u("b", 1)
    def i16(self): return self._u("h", 2)
    def i32(self): return self._u("i", 4)
    def i64(self): return self._u("q", 8)
    def f32(self): return self._u("f", 4)
    def f64(self): return self._u("d", 8)

    def cstr(self, size=None, enc="utf-8"):
        """Zero-terminated string; with size, reads a fixed-size field."""
        if size is not None:
            raw = self.read(size)
            return raw.split(b"\0", 1)[0].decode(enc, errors="replace")
        end = self.d.index(b"\0", self.p)
        s = self.d[self.p:end].decode(enc, errors="replace")
        self.p = end + 1
        return s


# ---------- compression ----------

def lz4_block(src, size):
    """Decompress one raw LZ4 block (pure Python; uses the lz4 library when installed)."""
    try:
        import lz4.block
        return lz4.block.decompress(src, uncompressed_size=size)
    except ImportError:
        pass
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if start < 0:
            raise ValueError("corrupt LZ4 data")
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    return bytes(dst[:size]) if size else bytes(dst)


def lz4_frame(src):
    """Decompress an LZ4 frame (as used by Skyrim SE's BSA files)."""
    try:
        import lz4.frame
        return lz4.frame.decompress(src)
    except ImportError:
        pass
    b = Bin(src)
    if b.u32() != 0x184D2204:
        raise ValueError("not an LZ4 frame")
    flg = b.u8()
    b.u8()                                   # BD byte
    has_size, has_dict = flg & 0x08, flg & 0x01
    block_checksum, content_checksum = flg & 0x10, flg & 0x04
    if has_size:
        b.u64()
    if has_dict:
        b.u32()
    b.u8()                                   # header checksum
    out = bytearray()
    while True:
        bs = b.u32()
        if bs == 0:
            break
        raw = bs & 0x80000000
        bs &= 0x7FFFFFFF
        chunk = b.read(bs)
        if raw:
            out += chunk
        else:
            # blocks may refer back into earlier output (linked blocks): decompress with history
            out += _lz4_with_history(chunk, out)
        if block_checksum:
            b.u32()
    if content_checksum and b.left() >= 4:
        b.u32()
    return bytes(out)


def _lz4_with_history(chunk, history):
    window = bytes(history[-65536:])
    marker = len(window)
    try:
        import lz4.block
        res = lz4.block.decompress(chunk, uncompressed_size=4 * 1024 * 1024 + 65536, dict=window)
        return res
    except ImportError:
        pass
    # pure Python: prepend the history window, then cut it off again
    src = chunk
    dst = bytearray(window)
    i, n = 0, len(src)
    while i < n:
        token = src[i]; i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                x = src[i]; i += 1; lit += x
                if x != 255:
                    break
        dst += src[i:i + lit]; i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8); i += 2
        ml = token & 15
        if ml == 15:
            while True:
                x = src[i]; i += 1; ml += x
                if x != 255:
                    break
        ml += 4
        start = len(dst) - off
        for k in range(ml):
            dst.append(dst[start + k])
    return bytes(dst[marker:])


def zstd(src, size=0):
    try:
        import zstandard
    except ImportError:
        raise RuntimeError("This file uses Zstandard compression. Click 'Install helpers' (Apps card → Game files); "
                           "it installs the zstandard library.")
    return zstandard.ZstdDecompressor().decompress(src, max_output_size=size or 512 * 1024 * 1024)


def inflate(src, size=0):
    try:
        return zlib.decompress(src)
    except zlib.error:
        return zlib.decompress(src, -15)       # raw deflate without a header


# ---------- generic views ----------

def hexdump(data, start=0, length=512):
    chunk = data[start:start + length]
    lines = []
    for i in range(0, len(chunk), 16):
        row = chunk[i:i + 16]
        hx = " ".join(f"{b:02x}" for b in row)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{start + i:08x}  {hx:<47}  {asc}")
    return "\n".join(lines)


def strings(data, min_len=5, limit=400):
    """Readable text runs (ASCII and UTF-16) found in a binary file."""
    out, seen = [], set()
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data):
        s = m.group().decode("ascii")
        if s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                return out
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, data):
        s = m.group().decode("utf-16-le")
        if s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
    return out


def size_text(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def safe_name(path):
    """Turn an archive path into a safe relative path for extraction."""
    parts = [p for p in re.split(r"[\\/]+", path) if p not in ("", ".", "..")]
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p) for p in parts]
    return os.path.join(*parts) if parts else "file"


def write_out(out_dir, inner_path, data):
    dest = os.path.join(out_dir, safe_name(inner_path))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def table(rows, headers):
    """Plain text table."""
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    widths = [min(w, 60) for w in widths]
    fmt = lambda r: "  ".join(c[:w].ljust(w) for c, w in zip(r, widths))
    return "\n".join([fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])
