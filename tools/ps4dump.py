"""Dump a PS4 game process via ps4debug into <out>.bin + <out>.json.

    python ps4dump.py --host 192.168.x.y            # picks the eboot.bin process
    python ps4dump.py --host 192.168.x.y --pid 123
    python ps4dump.py --selftest

Output: flat .bin with all readable regions back to back, .json index with
{start,end,prot,name,offset} per region. Unreadable chunks are zero-filled and
listed under "holes".
"""
import argparse, asyncio, json, os, sys, time

CHUNK = 1 << 20          # 1 MiB per read
PARALLEL = 4             # concurrent sockets (ps4debug pool default is 10)
PROT_READ = 1
PROBE_ABOVE = 64 << 20   # regions bigger than this are probed chunk-wise before reading
ZERO = bytes(CHUNK)


def load(path_bin):
    """Open a dump. Returns (index, mmap-like bytes). Used by the offline dumper."""
    import mmap
    idx = json.load(open(path_bin[:-4] + ".json"))
    f = open(path_bin, "rb")
    return idx, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)


def read(idx, mm, addr, length):
    """Read `length` bytes at virtual `addr` from a loaded dump; None if unmapped."""
    for r in idx["regions"]:
        if r["start"] <= addr < r["end"]:
            off = r["offset"] + (addr - r["start"])
            avail = r["end"] - addr
            return mm[off:off + min(length, avail)]
    return None


async def dump(host, pid, out, skip_names, max_region):
    import ps4debug
    dbg = ps4debug.PS4Debug(host)
    procs = await dbg.get_processes()
    if pid is None:
        cands = [p for p in procs if p.name.rstrip("\0") == "eboot.bin"]
        if len(cands) != 1:
            print("processes:", [(p.pid, p.name.rstrip("\0")) for p in procs])
            sys.exit("pass --pid, found %d eboot.bin processes" % len(cands))
        pid = cands[0].pid
    info = await dbg.get_process_info(pid)
    maps = await dbg.get_process_maps(pid)
    regions = []
    for m in maps:
        size = m.end - m.start
        if not (m.prot & PROT_READ) or size <= 0:
            continue
        if any(s and s in m.name for s in skip_names):
            continue
        if size > max_region:
            print(f"skip {m.name!r} {m.start:#x} ({size >> 20} MiB > limit)")
            continue
        regions.append(m)
    print(f"pid {pid} {info.name} {info.title_id}: {len(regions)}/{len(maps)} regions, "
          f"{sum(m.end - m.start for m in regions) >> 20} MiB reserved")
    sem = asyncio.Semaphore(PARALLEL)

    async def probe(addr):
        async with sem:
            return await dbg.read_memory(pid, addr, 4) is not None

    # Big regions are mostly unmapped reservations (direct memory): probe each chunk first.
    chunks = []  # (addr, n, mapname, prot)
    for m in regions:
        starts = list(range(m.start, m.end, CHUNK))
        if m.end - m.start > PROBE_ABOVE:
            ok = await asyncio.gather(*(probe(a) for a in starts))
            starts = [a for a, k in zip(starts, ok) if k]
        chunks += [(a, min(CHUNK, m.end - a), m.name, m.prot) for a in starts]
    total = sum(c[1] for c in chunks)
    print(f"{len(chunks)} chunks mapped, {total >> 20} MiB to read")

    holes = []
    index = {"host": host, "pid": pid, "name": info.name, "path": info.path,
             "title_id": info.title_id, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
             "regions": [], "holes": holes}

    async def fetch(addr, n):
        async with sem:
            data = await dbg.read_memory(pid, addr, n)
        if data is None or len(data) != n:
            holes.append([addr, n])
            return b"\0" * n
        return bytes(data)

    t0 = time.time()
    done = 0
    with open(out + ".bin", "wb") as f:
        for i in range(0, len(chunks), PARALLEL * 2):
            batch = chunks[i:i + PARALLEL * 2]
            datas = await asyncio.gather(*(fetch(a, n) for a, n, _, _ in batch))
            for (a, n, name, prot), data in zip(batch, datas):
                if data == ZERO[:n]:
                    continue                  # unmapped reservation or empty page: not stored
                r = index["regions"][-1] if index["regions"] else None
                if r and r["end"] == a and r["name"] == name and r["prot"] == prot:
                    r["end"] = a + n          # merge adjacent chunks
                else:
                    index["regions"].append({"name": name, "start": a, "end": a + n,
                                             "prot": prot, "offset": f.tell()})
                f.write(data)
                done += n
            el = time.time() - t0
            print(f"\r{done >> 20}/{total >> 20} MiB  {done / el / 1e6:.1f} MB/s  holes={len(holes)}", end="")
    print()
    json.dump(index, open(out + ".json", "w"), indent=1)
    print("wrote", out + ".bin", out + ".json", f"({len(index['regions'])} regions)")


def compact(src, dst):
    """Rewrite a dump without all-zero 1 MiB chunks."""
    idx, mm = load(src)
    out = {k: v for k, v in idx.items() if k != "regions"}; out["regions"] = []
    with open(dst + ".bin", "wb") as f:
        for r in idx["regions"]:
            for a in range(r["start"], r["end"], CHUNK):
                n = min(CHUNK, r["end"] - a)
                data = mm[r["offset"] + (a - r["start"]): r["offset"] + (a - r["start"]) + n]
                if data == ZERO[:n]:
                    continue
                last = out["regions"][-1] if out["regions"] else None
                if last and last["end"] == a and last["name"] == r["name"]:
                    last["end"] = a + n
                else:
                    out["regions"].append({"name": r["name"], "start": a, "end": a + n,
                                           "prot": r["prot"], "offset": f.tell()})
                f.write(data)
    json.dump(out, open(dst + ".json", "w"), indent=1)
    print("compacted", len(idx["regions"]), "->", len(out["regions"]), "regions")


def selftest():
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t")
    idx = {"regions": [{"start": 0x1000, "end": 0x1010, "prot": 3, "name": "a", "offset": 0},
                       {"start": 0x5000, "end": 0x5008, "prot": 3, "name": "b", "offset": 16}]}
    open(p + ".bin", "wb").write(bytes(range(16)) + b"ABCDEFGH")
    json.dump(idx, open(p + ".json", "w"))
    i, mm = load(p + ".bin")
    assert read(i, mm, 0x1004, 4) == b"\x04\x05\x06\x07"
    assert read(i, mm, 0x5006, 8) == b"GH"          # clipped at region end
    assert read(i, mm, 0x2000, 1) is None
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host")
    ap.add_argument("--pid", type=int)
    ap.add_argument("-o", "--out", help="output basename (default: <title_id>_<time>)")
    ap.add_argument("--skip", default="", help="comma separated substrings of region names to skip")
    ap.add_argument("--max-region-mb", type=int, default=16384,   # same as ps4ue: the 8 GiB direct-memory regions hold the heap;
                    help="regions above this are skipped; big ones are probed chunk-wise anyway")   # 3072 once dropped them → 129 MB dump
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--compact", metavar="SRC.bin", help="rewrite an existing dump without zero chunks into -o")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        sys.exit()
    if a.compact:
        compact(a.compact, a.out)
        sys.exit()
    out = a.out or time.strftime("dump_%Y%m%d_%H%M%S")
    asyncio.run(dump(a.host, a.pid, out, a.skip.split(","), a.max_region_mb << 20))
