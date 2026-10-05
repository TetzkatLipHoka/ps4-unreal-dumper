#!/usr/bin/env python3
"""uediff.py - compares 2+ memory dumps (like Cheat Engine's "Next Scan", but offline) and tells you WHICH game object /
WHICH property is behind the value you are looking for (e.g. hit points). Second mode: finds the code locations that
access a field (disassembly addresses).

Install (once):   pip install numpy capstone

STEP 1 - find the value
  Take dump A while the health bar is FULL, dump B after taking damage (optional C: healed again).

  python uediff.py ..\\dumps\\A\\dump.bin ..\\dumps\\B\\dump.bin --type float --rel dec
  python uediff.py A.bin B.bin C.bin --type float --rel dec,inc          # 3 dumps -> far fewer hits
  python uediff.py A.bin B.bin --type float --v0 100 --v1 75 --rel dec   # if you know the numbers (HUD / table)
  python uediff.py A.bin B.bin --type float,int32 --rel dec --owner Player

  --rel   relation per dump pair: eq (equal) | ne (different) | dec (decreased) | inc (increased)
  --v0..  expected value in the 1st/2nd/3rd dump (float: with tolerance --tol, int32: exact)

STEP 2 - code accessing the field found (the offset is in the output of step 1, e.g. +0x2A8)
  python uediff.py --xref ..\\dumps\\A\\dump.bin --disp 0x2A8 --write

NOTE: a pure dump comparison only knows data, not code. --xref lists every instruction in the game image with exactly
this struct offset as candidates (Blueprint variables are read by the script VM and do NOT show up in code). For ONE
exact location you need a write watchpoint on the data address from step 1 (peek.py trace).
"""
import argparse, json, os, re, struct, sys, time
import numpy as np

np.seterr(invalid="ignore", over="ignore")      # NaN/Inf in memory are normal

DTYPES = {"float": "<f4", "int32": "<i4", "double": "<f8"}
PRIO_NAME = re.compile(r"health|hp\b|^hp|life|vital|hitpoint|hit_point|damage|\bkp\b|^kp|lebens", re.I)
START_KEYS = ("start", "addr", "address", "va", "vaddr", "begin", "base", "lo", "from")
SIZE_KEYS = ("size", "length", "len", "sz", "bytes")
END_KEYS = ("end", "stop", "hi", "to")
PREFIXES = {0x66, 0xF2, 0xF3, 0xF0, 0x2E, 0x36, 0x3E, 0x26, 0x64, 0x65, 0x67}


def hx(v):
    return "None" if v is None else f"{v:#x}"


# --------------------------------------------------------------------------------------------------------------------
# Regions of a dump (the format of uefind.Dump is not hard-wired: we detect it)
# --------------------------------------------------------------------------------------------------------------------
def _num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        try:
            return int(x, 0)
        except ValueError:
            return None
    return None


def _contiguous(d, a, n):
    if n <= 0:
        return False
    o = d.off(a)
    return o is not None and d.off(a + n - 1) == o + n - 1


def _one(d, r):
    """One region entry (dict / tuple / object) -> (start, size) or None."""
    start = size = end = None
    if isinstance(r, dict) or hasattr(r, "__dict__") or hasattr(r, "_fields"):
        get = (lambda k: r.get(k)) if isinstance(r, dict) else (lambda k: getattr(r, k, None))
        low = {k.lower(): k for k in (r.keys() if isinstance(r, dict) else dir(r)) if isinstance(k, str)}
        pick = lambda keys: next((_num(get(low[k])) for k in keys if k in low and _num(get(low[k])) is not None), None)
        start, size, end = pick(START_KEYS), pick(SIZE_KEYS), pick(END_KEYS)
        if start is not None and size is None and end is not None:
            size = end - start
    if start is None and isinstance(r, (list, tuple)):
        nums = [n for n in map(_num, r) if n is not None]
        if len(nums) >= 2:
            start = nums[0]
            for cand in (nums[1], nums[1] - nums[0]):        # second number = size OR end address
                if cand > 0 and _contiguous(d, start, cand):
                    return start, cand
            return None
    if start is None or size is None or size <= 0:
        return None
    return (start, size) if _contiguous(d, start, size) else None


def regions_of(d, path):
    sources = [getattr(d, "regions", None)]
    for jp in (os.path.splitext(path)[0] + ".json", path + ".json"):
        if os.path.exists(jp):
            try:
                j = json.load(open(jp))
                if isinstance(j, dict):
                    j = j.get("regions") or next((v for v in j.values() if isinstance(v, list)), None)
                sources.append(j)
            except (OSError, ValueError):
                pass
    for src in sources:
        if not src:
            continue
        items = list(src.values()) if isinstance(src, dict) else list(src)
        regs = [x for x in (_one(d, r) for r in items) if x]
        if len(regs) >= max(1, len(items) // 2):
            return sorted(set(regs))
    first = next((s for s in sources if s), None)
    sample = (list(first)[:2] if not isinstance(first, dict) else list(first.items())[:2]) if first else None
    sys.exit("Region format of the dump not recognised. Please send this line to the developer:\n"
             f"  type={type(first).__name__} sample={sample!r}")


# --------------------------------------------------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------------------------------------------------
def pieces(dumps, va, n, sz):
    """Parts of [va, va+n) that are contiguous in ALL dumps -> (va, [file offsets])."""
    offs = [d.off(va) for d in dumps]
    if all(o is not None for o in offs) and all(_contiguous(d, va, n) for d in dumps):
        yield va, offs, n
        return
    page = 0x1000                     # region missing/shifted in one dump -> check page by page
    for p in range(va, va + n, page):
        m = min(page, va + n - p)
        if all(_contiguous(d, p, m) for d in dumps):
            yield p, [d.off(p) for d in dumps], m


def scan(dumps, regs, dtype, rels, vals, tol, chunk, max_hits):
    dt = np.dtype(dtype)
    sz = dt.itemsize
    isf = dt.kind == "f"
    total = sum(s for _, s in regs)
    done, t0, nextp = 0, time.time(), 0.05
    addrs, values, nhits, capped = [], [], 0, False
    for start, size in regs:
        a0 = (start + sz - 1) // sz * sz
        end = start + size
        va = a0
        while va < end and not capped:
            n = min(chunk, end - va) // sz * sz
            if n <= 0:
                break
            for pva, offs, pn in pieces(dumps, va, n, sz):
                pn = pn // sz * sz
                if pn <= 0:
                    continue
                arrs = [np.frombuffer(d.mm, dtype=dt, count=pn // sz, offset=o) for d, o in zip(dumps, offs)]
                mask = np.ones(pn // sz, dtype=bool)
                if isf:
                    for a in arrs:
                        mask &= np.isfinite(a)
                for i, v in enumerate(vals):
                    if v is not None and i < len(arrs):
                        mask &= (np.abs(arrs[i] - v) <= tol) if isf else (arrs[i] == v)
                    if not mask.any():
                        break
                if mask.any():
                    for i, rel in enumerate(rels):
                        a, b = arrs[i], arrs[i + 1]
                        mask &= {"eq": a == b, "ne": a != b, "dec": b < a, "inc": b > a}[rel]
                        if not mask.any():
                            break
                if mask.any():
                    idx = np.flatnonzero(mask)
                    addrs.append(pva + idx.astype(np.uint64) * np.uint64(sz))
                    values.append(np.stack([a[idx] for a in arrs], axis=1))
                    nhits += len(idx)
                    if nhits >= max_hits:
                        capped = True
                        break
            va += n
            done += n
            if done / total >= nextp:
                el = time.time() - t0
                print(f"  {done / total:5.0%}  {nhits:>9,} hits  ({el:4.0f}s, ~{el / (done / total) - el:4.0f}s left)",
                      flush=True)
                nextp += 0.05
        if capped:
            break
    if not addrs:
        return np.zeros(0, np.uint64), np.zeros((0, len(dumps)), dt), capped
    return np.concatenate(addrs), np.concatenate(values), capped


# --------------------------------------------------------------------------------------------------------------------
# Hit -> UObject / property
# --------------------------------------------------------------------------------------------------------------------
def build_owner_index(e):
    csz, items = {}, []
    for _, o in e.arr:
        c = e.cls(o)
        if not c:
            continue
        if c not in csz:
            csz[c] = e.struct_size(c) or 0
        if 0x28 <= csz[c] < (1 << 28):
            items.append((o, csz[c]))
    items.sort()
    return (np.array([x[0] for x in items], dtype=np.uint64), np.array([x[1] for x in items], dtype=np.uint64))


def prop_table(e, cls, cache):
    """All leaf properties of a class incl. super classes and embedded structs: (start, end, "path", "type")."""
    t = cache.get(cls)
    if t is not None:
        return t
    t = []

    def add_struct(s, base, prefix, depth):
        c, guard = s, 0
        while c and guard < 64:
            for p in e.properties(c):
                off, es = e.prop_offset(p), e.prop_size(p)
                if off is None or es is None or es <= 0:
                    continue
                dim = max(e.i32(p + e.O["Property_ArrayDim"]) or 1, 1)
                name, typ = e.ffield_name(p) or "?", e.ffield_class_name(p) or "?"
                if typ == "StructProperty" and dim == 1 and depth < 3:
                    inner = e.ptr(p + e.O["StructProperty_Struct"])
                    if inner:
                        add_struct(inner, base + off, f"{prefix}{name}.", depth + 1)
                        continue
                t.append((base + off, base + off + es * dim, prefix + name, typ))
            c, guard = e.super(c), guard + 1

    add_struct(cls, 0, "", 0)
    cache[cls] = t
    return t


def resolve_hits(e, addrs, values, dtype, owner_re):
    oa, osz = build_owner_index(e)
    cache, out, raw = {}, [], 0
    idx = np.searchsorted(oa, addrs, side="right").astype(np.int64) - 1
    for k in range(len(addrs)):
        i = idx[k]
        h = int(addrs[k])
        if i < 0 or h >= int(oa[i]) + int(osz[i]):
            raw += 1
            continue
        owner = int(oa[i])
        cls = e.cls(owner)
        off = h - owner
        full = e.fullname(owner)
        if owner_re and not owner_re.search(full):
            continue
        hit = next((x for x in prop_table(e, cls, cache) if x[0] <= off < x[1]), None)
        pname, ptype = (hit[2], hit[3]) if hit else ("?", "?")
        poff = f"+{off - hit[0]:#x}" if hit and off != hit[0] else ""
        score = 0
        score += 6 if PRIO_NAME.search(pname) else 0
        score += 3 if re.search(r"player|character|pawn|simon", full, re.I) else 0
        score += 2 if ptype in ("FloatProperty", "DoubleProperty", "IntProperty") else 0
        score -= 4 if "Default__" in full else 0
        score -= 2 if pname == "?" else 0
        out.append(dict(score=score, addr=h, owner=owner, off=off, full=full, prop=pname + poff, typ=ptype,
                        vals=values[k]))
    out.sort(key=lambda r: (-r["score"], r["addr"]))
    return out, raw


def fmt_val(v):
    if isinstance(v, (np.floating, float)):
        return f"{float(v):.6g}"
    return str(int(v))


def short(s, n=78):
    return s if len(s) <= n else "..." + s[-(n - 3):]


# --------------------------------------------------------------------------------------------------------------------
# Code xref
# --------------------------------------------------------------------------------------------------------------------
def xref(d, regs, disp, write_only, lo, hi, limit, mnems):
    try:
        import capstone as cs
    except ImportError:
        sys.exit("capstone missing:  pip install capstone")
    if -0x80 <= disp < 0x80:
        sys.exit("Offset too small (<0x80): not uniquely searchable as an 8-bit displacement. Use a larger offset.")
    md = cs.Cs(cs.CS_ARCH_X86, cs.CS_MODE_64)
    md.detail = True
    pat = struct.pack("<i", disp)
    found, seen = [], set()
    scanned = 0
    for start, size in regs:
        a, b = max(start, lo), min(start + size, hi)
        if a >= b:
            continue
        o = d.off(a)
        data = bytes(d.mm[o:o + (b - a)])
        scanned += len(data)
        pos = data.find(pat)
        while pos != -1:
            p = a + pos
            cands = []
            for back in range(2, 10):                     # the instruction starts 2..9 bytes before the disp32
                s = pos - back
                if s < 0:
                    continue
                ins = next(md.disasm(data[s:s + 16], a + s, 1), None)
                if not ins or ins.disp != disp or ins.address + ins.disp_offset != p or ins.disp_offset + 4 > ins.size:
                    continue
                mem = [op for op in ins.operands if op.type == cs.x86.X86_OP_MEM]
                if not mem or any(op.mem.base == cs.x86.X86_REG_RIP for op in mem):
                    continue
                cands.append((s, ins, mem))
            # Ambiguity: decoding from a byte in the middle of an instruction nearly always yields "something" (e.g.
            # F3 0F 11 83 .. -> from 11: adc instead of movss). A start right behind 0F / a legacy prefix / REX is
            # therefore implausible.
            good = [c for c in cands if c[0] == 0 or not (data[c[0] - 1] in PREFIXES or data[c[0] - 1] == 0x0F
                                                           or 0x40 <= data[c[0] - 1] <= 0x4F)]
            pick = (good or cands)[:1]
            for s, ins, mem in pick:
                wr = any(op.access & cs.CS_AC_WRITE for op in mem)
                if write_only and not wr:
                    continue
                if mnems and not any(ins.mnemonic.startswith(m) for m in mnems):
                    continue
                if ins.address not in seen:
                    seen.add(ins.address)
                    found.append((ins.address, bytes(ins.bytes).hex(" "), f"{ins.mnemonic} {ins.op_str}", wr))
            pos = data.find(pat, pos + 1)
    found.sort()
    print(f"{scanned / 1048576:.0f} MiB of image searched, {len(found)} instructions with [reg{disp:+#x}]"
          f"{' (writing only)' if write_only else ''}:")
    for addr, bts, txt, wr in found[:limit]:
        print(f"  {addr:#012x}  {bts:<26} {txt}{'   <-- writes' if wr and not write_only else ''}")
    if len(found) > limit:
        print(f"  ... {len(found) - limit} more (raise --limit or filter with --mnem movss,vmovss,mov)")
    return found


# --------------------------------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare dumps and find the address of a value / the code accessing it.")
    ap.add_argument("dumps", nargs="*", help="2+ dump.bin (in chronological order)")
    ap.add_argument("--type", default="float", help="float, int32, double (comma separated allowed). Default float")
    ap.add_argument("--rel", default=None, help="per dump pair: eq|ne|dec|inc (default: dec). e.g. dec,inc")
    ap.add_argument("--v0", type=float), ap.add_argument("--v1", type=float), ap.add_argument("--v2", type=float)
    ap.add_argument("--tol", type=float, default=0.01, help="tolerance for float values (default 0.01)")
    ap.add_argument("--owner", default=None, help="regex on the object name (e.g. Player|Character)")
    ap.add_argument("--top", type=int, default=40, help="number of resolved hits to print")
    ap.add_argument("--max-hits", type=int, default=3_000_000)
    ap.add_argument("--chunk", type=lambda x: int(x, 0), default=0x400000, help="scan block size in bytes")
    ap.add_argument("--raw", type=int, default=15, help="number of hits without a UObject that are shown in addition")
    ap.add_argument("--out", default=None, help="write all resolved hits to this text file")
    ap.add_argument("--xref", default=None, metavar="DUMP", help="search code accessing --disp in this dump")
    ap.add_argument("--disp", type=lambda x: int(x, 0), help="struct offset for --xref (e.g. 0x2A8)")
    ap.add_argument("--write", action="store_true", help="--xref: only instructions that write to memory")
    ap.add_argument("--mnem", default=None, help="--xref: only these mnemonics (prefix), e.g. movss,vmovss,mov,subss")
    ap.add_argument("--range", nargs=2, type=lambda x: int(x, 0), metavar=("LO", "HI"), help="--xref: address range")
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args(argv)

    from uefind import Dump

    if args.xref:
        if args.disp is None:
            sys.exit("--xref needs --disp <offset>")
        d = Dump(args.xref)
        regs = regions_of(d, args.xref)
        lo, hi = args.range or (d.base, d.exe_end)
        xref(d, regs, args.disp, args.write, lo, hi, args.limit, [m.strip() for m in args.mnem.split(",")] if args.mnem else [])
        return

    if len(args.dumps) < 2:
        ap.error("pass at least 2 dumps (or --xref)")
    rels = (args.rel or "dec").split(",")
    if len(rels) != len(args.dumps) - 1 or any(r not in ("eq", "ne", "dec", "inc") for r in rels):
        ap.error(f"--rel needs {len(args.dumps) - 1} values out of eq|ne|dec|inc (comma separated), got: {args.rel or 'dec'}")
    vals = [args.v0, args.v1, args.v2][:len(args.dumps)]

    dumps = [Dump(p) for p in args.dumps]
    regs = regions_of(dumps[0], args.dumps[0])
    print(f"{len(regs)} regions, {sum(s for _, s in regs) / 2 ** 30:.2f} GiB in dump 1; comparing: "
          f"{' -> '.join(os.path.basename(os.path.dirname(os.path.abspath(p))) or p for p in args.dumps)}  rel={','.join(rels)}")

    from ueobj import open_engine
    e = open_engine(args.dumps[0], log=lambda *a: None)
    owner_re = re.compile(args.owner, re.I) if args.owner else None

    all_rows = []
    for tname in [t.strip() for t in args.type.split(",")]:
        if tname not in DTYPES:
            ap.error(f"--type {tname}? allowed: {', '.join(DTYPES)}")
        print(f"\n== scan as {tname} ==")
        addrs, values, capped = scan(dumps, regs, DTYPES[tname], rels, vals, args.tol, args.chunk, args.max_hits)
        print(f"{len(addrs):,} hits{' (ABORTED: --max-hits reached, tighten the conditions: --v0/--v1, 3rd dump)' if capped else ''}")
        if not len(addrs):
            continue
        rows, raw = resolve_hits(e, addrs, values, DTYPES[tname], owner_re)
        print(f"{len(rows):,} of them are inside a UObject, {raw:,} are not (heap/engine-internal data)\n")
        for r in rows:
            r["type"] = tname
        all_rows += rows
        print(f"{'Address':<14} {'Values (dump1 -> ...)':<28} Object  [+offset in object]  Property (type)")
        for r in rows[:args.top]:
            print(f"{r['addr']:#014x} {' -> '.join(fmt_val(v) for v in r['vals']):<28} "
                  f"{short(r['full'])}  [{r['off']:#x}]  {r['prop']} ({r['typ']})")
        if args.raw and raw:
            print(f"\nExamples without a UObject (first {args.raw}):")
            oa, osz = build_owner_index(e)
            idx = np.searchsorted(oa, addrs, side="right").astype(np.int64) - 1
            shown = 0
            for k in range(len(addrs)):
                i = idx[k]
                if i < 0 or int(addrs[k]) >= int(oa[i]) + int(osz[i]):
                    print(f"  {int(addrs[k]):#014x}  {' -> '.join(fmt_val(v) for v in values[k])}")
                    shown += 1
                    if shown >= args.raw:
                        break
    if args.out and all_rows:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in all_rows:
                f.write(f"{r['addr']:#014x}\t{r['type']}\t{' -> '.join(fmt_val(v) for v in r['vals'])}\t"
                        f"{r['full']}\t[{r['off']:#x}]\t{r['prop']}\t{r['typ']}\n")
        print(f"\n{len(all_rows)} hits written to {args.out}")
    if all_rows:
        b = all_rows[0]
        print(f"\nBest candidate: {b['addr']:#x} = {b['full']} + {b['off']:#x}  ({b['prop']})")
        print(f"Code for it:  python uediff.py --xref {args.dumps[0]} --disp {b['off']:#x} --write")


if __name__ == "__main__":
    main()
