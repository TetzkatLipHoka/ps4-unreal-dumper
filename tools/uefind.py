"""Locate Unreal globals (GNames / GObjects) in a ps4dump memory dump by structure, not by code patterns.

    python uefind.py ../dumps/lollipop.bin
"""
import json, mmap, re, struct, sys, bisect


class Dump:
    def __init__(self, path_bin):
        self.idx = json.load(open(path_bin[:-4] + ".json"))
        self.f = open(path_bin, "rb")
        self.mm = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        self.regions = sorted(self.idx["regions"], key=lambda r: r["start"])
        self.starts = [r["start"] for r in self.regions]
        exe = [r for r in self.regions if r["name"] == "executable"]
        self.base = exe[0]["start"] if exe else 0
        self.exe_end = exe[-1]["end"] if exe else 0

    def region(self, va):
        i = bisect.bisect_right(self.starts, va) - 1
        if i >= 0 and va < self.regions[i]["end"]:
            return self.regions[i]

    def off(self, va):
        r = self.region(va)
        return None if r is None else r["offset"] + va - r["start"]

    def va(self, off):
        for r in self.regions:
            if r["offset"] <= off < r["offset"] + r["end"] - r["start"]:
                return r["start"] + off - r["offset"]

    def read(self, va, n):
        o = self.off(va)
        return None if o is None else self.mm[o:o + n]

    def u64(self, va):
        o = self.off(va)
        return None if o is None else struct.unpack_from("<Q", self.mm, o)[0]

    def i32(self, va):
        o = self.off(va)
        return None if o is None else struct.unpack_from("<i", self.mm, o)[0]

    def u16(self, va):
        o = self.off(va)
        return None if o is None else struct.unpack_from("<H", self.mm, o)[0]

    def is_ptr(self, v):
        return v is not None and v > 0x10000 and self.region(v) is not None

    def find_all(self, pattern):
        """Yield virtual addresses of every match of a regex over the whole dump."""
        for m in re.finditer(pattern, self.mm, re.S):
            yield self.va(m.start())


class NamePool:
    """UE 4.23+ FNamePool: {Lock 8, CurrentBlock 4, CurrentByteCursor 4, Blocks[8192]}."""

    def __init__(self, d, addr, stride=2, block_bits=16):
        self.d, self.addr, self.stride, self.block_bits = d, addr, stride, block_bits
        self.current_block = d.i32(addr + 8)
        self.cursor = d.i32(addr + 12)

    def entry(self, idx):
        block, off = idx >> self.block_bits, (idx & ((1 << self.block_bits) - 1)) * self.stride
        if block > self.current_block:
            return None
        b = self.d.u64(self.addr + 0x10 + block * 8)
        return None if not b else b + off

    def name(self, idx, number=0):
        e = self.entry(idx)
        if e is None:
            return None
        hdr = self.d.u16(e)
        if hdr is None:
            return None
        n, wide = hdr >> 6, hdr & 1
        if n == 0:                     # outline number entry: {u16 hdr, i32 idx, i32 number}
            i2, num = struct.unpack("<ii", self.d.read(e + 2, 8))
            s = self.name(i2)
            return None if s is None else (f"{s}_{num - 1}" if num > 0 else s)
        raw = self.d.read(e + 2, n * (2 if wide else 1))
        s = raw.decode("utf-16-le", "replace") if wide else raw.decode("latin1")
        return f"{s}_{number - 1}" if number > 0 else s

    @staticmethod
    def find(d):
        """Block 0 starts with 'None' then 'ByteProperty' (headers: len<<6 | 5 hash bits | wide bit)."""
        for va in d.find_all(rb"[\x00-\x3f]\x01None[\x00-\x3f]\x03ByteProperty"):
            key = struct.pack("<Q", va)
            p = d.mm.find(key)
            while p != -1:
                pool = d.va(p) - 0x10
                cur, cursor = d.i32(pool + 8), d.i32(pool + 12)
                blocks = lambda i: d.u64(pool + 0x10 + 8 * i)
                if (cur is not None and 0 <= cur < 8191 and 0 < cursor <= 0x40000 and blocks(cur + 1) == 0
                        and all(d.is_ptr(blocks(i)) for i in range(cur + 1))):
                    return NamePool(d, pool)
                p = d.mm.find(key, p + 1)


def find_none_entries(d):
    """VAs of FNameEntry 0 ('None', Index 0) candidates for the pre-4.23 / UE3 name tables. Every candidate costs a
    whole-dump pointer search (6.6 s on a 5.4 GB dump), so first try the tight form with entry 1 ('ByteProperty')
    allocated right behind it (measured: 1 hit on Days Gone vs 495 loose hits), then fall back to the loose one."""
    for pat in (rb"\x00\x00\x00\x00.{4}.{8}None\x00.{0,40}?ByteProperty\x00", rb"\x00\x00\x00\x00.{4}.{8}None\x00"):
        yield from d.find_all(pat)


class NameArray:
    """UE 4.x < 4.23 TNameEntryArray: {FNameEntry** Chunks[128]; int32 NumElements; int32 NumChunks},
    FNameEntry = {int32 Index (idx<<1 | wide); pad; FNameEntry* HashNext; char/wchar Name[]}."""
    PER_CHUNK = 16384

    def __init__(self, d, addr, str_off):
        self.d, self.addr, self.str_off = d, addr, str_off
        self.num = d.i32(addr + 0x400)
        self.num_chunks = d.i32(addr + 0x404)
        self.current_block = self.num_chunks - 1        # duck-typing with NamePool
        self.block_bits = 14

    def entry(self, idx):
        if not 0 <= idx < self.num:
            return None
        chunk = self.d.u64(self.addr + 8 * (idx // self.PER_CHUNK))
        return self.d.u64(chunk + 8 * (idx % self.PER_CHUNK)) if chunk else None

    def name(self, idx, number=0):
        e = self.entry(idx)
        if not e or not self.d.is_ptr(e):
            return None
        hdr = self.d.i32(e)
        if hdr is None:
            return None
        raw = self.d.read(e + self.str_off, 1024) or b""
        if hdr & 1:
            s = raw[:len(raw) & ~1].decode("utf-16-le", "replace").split("\x00")[0]
        else:
            s = raw.split(b"\x00")[0].decode("latin1")
        return f"{s}_{number - 1}" if number > 0 else s

    @staticmethod
    def find(d):
        """Entry 0 is 'None': {Index 0, pad, HashNext, "None"}; chunk 0 starts with its address;
        the array struct starts with the chunk-0 address and has plausible counters at +0x400."""
        for va in find_none_entries(d):
            key = struct.pack("<Q", va)
            p = d.mm.find(key)
            while p != -1:
                chunk0 = d.va(p)
                e1 = d.u64(chunk0 + 8)
                if d.is_ptr(e1) and (d.read(e1 + 0x10, 12) or b"") == b"ByteProperty":
                    k2 = struct.pack("<Q", chunk0)
                    q = d.mm.find(k2)
                    while q != -1:
                        arr = d.va(q)
                        num, nc = d.i32(arr + 0x400), d.i32(arr + 0x404)
                        if nc and 0 < nc <= 128 and num and (nc - 1) * NameArray.PER_CHUNK < num <= nc * NameArray.PER_CHUNK                                 and all(d.is_ptr(d.u64(arr + 8 * i)) for i in range(nc)) and d.u64(arr + 8 * nc) == 0:
                            return NameArray(d, arr, 0x10)
                        q = d.mm.find(k2, q + 1)
                p = d.mm.find(key, p + 1)


# FChunkedFixedUObjectArray layouts {objects, max, num, maxchunks, numchunks} (Dumper-7 list)
CHUNKED_LAYOUTS = [(0x00, 0x10, 0x14, 0x18, 0x1C), (0x00, 0x0C, 0x08, 0x14, 0x10),
                   (0x10, 0x00, 0x04, 0x08, 0x0C), (0x18, 0x10, 0x00, 0x14, 0x20), (0x18, 0x00, 0x14, 0x10, 0x04)]


class ObjectArray:
    index_off = None     # set by Engine once UObject::Index is measured; then __iter__ drops stale slots

    def __init__(self, d, addr, layout):
        self.d, self.addr, self.layout = d, addr, layout
        o, mx, num, mc, nc = layout
        self.objects, self.max, self.num = d.u64(addr + o), d.i32(addr + mx), d.i32(addr + num)
        self.max_chunks, self.num_chunks = d.i32(addr + mc), d.i32(addr + nc)
        self.per_chunk = self.max // self.max_chunks
        self.chunk0 = d.u64(self.objects)
        self.item_size = self.measure_item_size()

    def measure_item_size(self):
        """FUObjectItem size varies (packing, extra fields): pick the stride where item k -> UObject with Index k."""
        chunk = self.chunk0
        best = (0, 24)
        for stride in (16, 20, 24, 32, 40):
            hits = 0
            for k in range(1, 40):
                o = self.d.u64(chunk + k * stride)
                if self.d.is_ptr(o) and self.d.i32(o + 0xC) == k:
                    hits += 1
            best = max(best, (hits, stride))
        return best[1]

    def get(self, i):
        if not 0 <= i < self.num:
            return None
        chunk = self.d.u64(self.objects + 8 * (i // self.per_chunk))
        return None if not chunk else self.d.u64(chunk + (i % self.per_chunk) * self.item_size)

    def __iter__(self):
        # The dump is not atomic (minutes): objects freed meanwhile still sit in the array but their memory is
        # reused (measured SAO Last Recollection: 369 of 270k slots, two of them read as UEnum and broke uegen).
        for i in range(self.num):
            o = self.get(i)
            if o and (self.index_off is None or self.d.i32(o + self.index_off) == i):
                yield i, o

    @staticmethod
    def valid(d, a, layout):
        o, mx, num, mc, nc = layout
        objects, maxe, nume = d.u64(a + o), d.i32(a + mx), d.i32(a + num)
        maxc, numc = d.i32(a + mc), d.i32(a + nc)
        if None in (objects, maxe, nume, maxc, numc):
            return False
        if not (1 <= numc <= 0x14 and 6 <= maxc <= 0x5FF and nume > 0x800 and maxe > 0x10000):
            return False
        if nume > maxe or numc > maxc or maxe % 0x10:
            return False
        per = maxe // maxc
        if per % 0x10 or not (0x8000 <= per <= 0x80000):
            return False
        if nume // per + 1 != numc or maxe // per != maxc:
            return False
        if not d.is_ptr(objects):
            return False
        return all(d.is_ptr(d.u64(objects + 8 * i)) for i in range(numc))

    @staticmethod
    def find(d, lo, hi):
        """Scan [lo, hi) with 4-byte step for a valid FChunkedFixedUObjectArray."""
        for a in range(lo, hi - 0x30, 4):
            for layout in CHUNKED_LAYOUTS:
                if ObjectArray.valid(d, a, layout):
                    return ObjectArray(d, a, layout)


class FlatObjectArray(ObjectArray):
    """UE 4.8-4.19 FFixedUObjectArray {FUObjectItem* Objects; int32 Max; int32 Num} (measured: Days Gone CUSA09175,
    445k objects, FUObjectItem 16 bytes). layout=None marks it in the engine.json cache."""

    def __init__(self, d, addr):
        self.d, self.addr, self.layout = d, addr, None
        self.objects, self.max, self.num = d.u64(addr), d.i32(addr + 8), d.i32(addr + 12)
        self.max_chunks = self.num_chunks = 1
        self.per_chunk = self.max
        self.chunk0 = self.objects
        self.item_size = self.measure_item_size()

    def get(self, i):
        return self.d.u64(self.objects + i * self.item_size) if 0 <= i < self.num else None

    @staticmethod
    def find(d, lo, hi):
        r = d.region(lo)
        buf = d.mm[r["offset"]:r["offset"] + hi - lo]
        for off in range(0, len(buf) - 16, 8):
            objs, mx, num = struct.unpack_from("<QiI", buf, off)
            if not (d.is_ptr(objs) and 0x800 < num <= mx < 0x800000):
                continue
            for stride in (16, 24, 32):
                if all(d.is_ptr(o := d.u64(objs + k * stride) or 0) and d.i32(o + 0xC) == k for k in (1, 2, 5, 100, 1000)):
                    return FlatObjectArray(d, lo + off)


def data_ranges(d):
    """Writable regions belonging to the main executable (.data/.bss)."""
    return [(r["start"], r["end"]) for r in d.regions
            if r["name"] == "executable" and r["prot"] & 2]


def main(path):
    d = Dump(path)
    print(f"base {d.base:#x}, {len(d.regions)} regions")
    pool = NamePool.find(d)
    if not pool:
        sys.exit("FNamePool not found")
    print(f"GNames (FNamePool) at {pool.addr:#x} = base+{pool.addr - d.base:#x}, "
          f"blocks {pool.current_block + 1}, cursor {pool.cursor:#x}")
    print("  names 0..6:", [pool.name(i) for i in range(7)])
    arr = None
    for lo, hi in data_ranges(d):
        arr = ObjectArray.find(d, lo, hi)
        if arr:
            break
    if not arr:
        sys.exit("GObjects not found")
    print(f"GObjects at {arr.addr:#x} = base+{arr.addr - d.base:#x}, layout {arr.layout}, "
          f"num {arr.num}, max {arr.max}, chunks {arr.num_chunks}/{arr.max_chunks}, per chunk {arr.per_chunk:#x}")
    # Standard UObject: vft 0, flags 8, index 0xC, class 0x10, name 0x18, outer 0x20
    print(f"  FUObjectItem size {arr.item_size}")
    for i in range(8):
        o = arr.get(i)
        if not o:
            continue
        idx, name_i, num = d.i32(o + 0xC), d.i32(o + 0x18), d.i32(o + 0x1C)
        cls = d.u64(o + 0x10)
        if idx is None:
            print(f"  [{i}] {o:#x} not in dump"); continue
        cls_name = pool.name(d.i32(cls + 0x18)) if d.is_ptr(cls) and d.i32(cls + 0x18) is not None else "?"
        print(f"  [{i}] {o:#x} idx={idx} class={cls_name} name={pool.name(name_i, num)}")
    return d, pool, arr


if __name__ == "__main__":
    main(sys.argv[1])
