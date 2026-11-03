"""ueitems_chain.py - stable pointer chain from the live GWorld to a block of data (the inventory) found in a dump.
    python ueitems_chain.py <dump.bin> <address> [depth]
The chain uses uecheat path syntax: reflected hops by property name (Name, Name[i], Prop:Class downcasts) as long as the
class is known, raw "+0xNN" pointer hops otherwise, and ends with the offset of the target inside the last block.
Heap addresses change every run but the chain from GWorld does not, so it can go into a .TLH trainer."""
import json, os, sys, bisect
import numpy as np

SPAN = 0x1000          # target may sit this far inside the block a pointer points to (struct inside a bigger object)
RAW_BLOCK = 0x400      # bytes scanned in a block that is not a UObject (native C++ object, array storage)
OBJ_CAP = 0x4000


def _regions(d):
    rs = [r for r in d.regions if r["end"] > r["start"]]
    return np.array([r["start"] for r in rs], dtype=np.uint64), np.array([r["end"] for r in rs], dtype=np.uint64)


def class_edges(e, cls, cache={}):
    """{offset: (name, declared class or None, is_array)} of the object-pointer members of `cls` (and its supers)."""
    if cls in cache:
        return cache[cls]
    out, c = {}, cls
    while c:
        for p in e.properties(c):
            kind = e.ffield_class_name(p)
            if kind in ("ObjectProperty", "WeakObjectProperty"):
                out.setdefault(e.prop_offset(p), (e.ffield_name(p), e.ptr(p + e.O["ObjectProperty_Class"]), False))
            elif kind == "ArrayProperty":
                inner = e.ptr(p + e.O["ArrayProperty_Inner"])
                if inner and e.ffield_class_name(inner) == "ObjectProperty":
                    out.setdefault(e.prop_offset(p), (e.ffield_name(p), e.ptr(inner + e.O["ObjectProperty_Class"]), True))
        c = e.super(c)
    cache[cls] = out
    return out


def find_chain(e, target, roots, depth=4, cap=400000, say=print, top=5, span=SPAN):
    """roots = [(address, "World[.path]", class or None)]; target = one address or a list (all searched in one walk; span=8
    for "the object itself"). Returns [(tokens, leaf offset, hops, why)] shortest first."""
    d = e.d
    ts = sorted(target) if isinstance(target, (list, tuple, set)) else [target]
    def hit(v):
        """the target inside [v, v + span), or None"""
        i = bisect.bisect_left(ts, v)
        return ts[i] if i < len(ts) and ts[i] < v + span else None
    from ueobj import CF
    def real_cls(v):
        """Class of the UObject at v, or None when v is not one (a garbage "class" made a 2.9 GB read of a random block)."""
        c = e.cls(v)
        if c and e.isa(c, CF["Class"]) and e.name(c) and 0 < (e.struct_size(c) or 0) < 0x1000000:
            return c
    starts, ends = _regions(d)
    def valid(q):
        i = np.searchsorted(starts, q, side="right") - 1
        ok = (i >= 0) & (q > 0x10000)
        ii = np.clip(i, 0, len(ends) - 1)
        return ok & (q < ends[ii])
    seen = {a for a, _, _ in roots}
    frontier = [(a, lab, c, lab.split("."), True) for a, lab, c in roots]      # addr, label, class, tokens, named-hops-still-possible
    found, nodes = [], 0
    for lvl in range(depth + 1):
        nxt = []
        for addr, lab, cls, toks, named in frontier:
            nodes += 1
            if nodes > cap:
                say(f"node cap {cap} reached")
                return sorted(found, key=lambda f: (len(f[0]), f[1]))[:top]
            size = min(max(e.struct_size(cls) or RAW_BLOCK, 8), OBJ_CAP) if cls else RAW_BLOCK
            raw = d.read(addr, size)
            if not raw or len(raw) < 8:
                continue
            q = np.frombuffer(raw[:len(raw) // 8 * 8], dtype="<u8")
            m = valid(q)
            edges = class_edges(e, cls) if (cls and named) else {}
            for k in np.flatnonzero(m).tolist():
                v, off = int(q[k]), k * 8
                if off in edges and edges[off][2]:                       # TArray<UObject*>: expand elements as Name[i]
                    continue
                t = hit(v)
                if t is not None:
                    found.append((toks + [edges[off][0] if off in edges else f"+{off:#x}"], t - v, lvl + 1,
                                  f"block {addr:#x} class {e.name(cls) if cls else None} read {len(raw):#x} bytes, "
                                  f"pointer {v:#x} at +{off:#x}"))
                    continue
                if v in seen or lvl >= depth:
                    continue
                seen.add(v)
                c2 = real_cls(v)
                if off in edges:
                    name, decl, _ = edges[off]
                    tok = name + (f":{e.name(c2)}" if c2 and decl and c2 != decl else "")
                    nxt.append((v, tok, c2, toks + [tok], True))
                else:
                    nxt.append((v, f"+{off:#x}", c2, toks + [f"+{off:#x}"], False))
            if cls and named:
                for off, (name, decl, is_arr) in edges.items():
                    if not is_arr:
                        continue
                    data, n = e.ptr(addr + off), e.i32(addr + off + 8) or 0
                    if not data or not 0 < n < 4096:
                        continue
                    for i in range(n):
                        v = e.ptr(data + 8 * i)
                        if not v:
                            continue
                        c2 = real_cls(v)
                        tok = f"{name}[{i}]" + (f":{e.name(c2)}" if c2 and decl and c2 != decl else "")
                        t = hit(v)
                        if t is not None:
                            found.append((toks + [tok], t - v, lvl + 1, f"array element {tok}"))
                        elif v not in seen and lvl < depth:
                            seen.add(v)
                            nxt.append((v, tok, c2, toks + [tok], True))
        if found:
            break
        frontier = nxt
        say(f"depth {lvl + 1}: {len(nxt)} blocks")
    found.sort(key=lambda f: (len(f[0]), f[1]))
    return found[:top]


def world_root(e, dump):
    meta = json.load(open(os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "offsets.json")))
    w = e.ptr(e.d.base + meta["gworld"]) if meta.get("gworld") else None
    if not w:
        sys.exit("GWorld is null in this dump (dump taken in-game, with offsets.json from uegen)")
    roots = [(w, "World", e.cls(w))]
    # the local PlayerController first: its components are then reached through it, not through GameState.PlayerArray[0]
    # (in co-op that can be another player)
    o, path, cls = w, "World", e.cls(w)
    for tok in ("OwningGameInstance", "LocalPlayers[0]", "PlayerController"):
        name, _, idx = tok.rstrip("]").partition("[")
        p = _member(e, cls, name)
        if not p:
            return roots, meta
        a = o + e.prop_offset(p)
        o = e.ptr(e.ptr(a) + 8 * int(idx)) if idx and e.ptr(a) else e.ptr(a)
        if not o or not e.cls(o):
            return roots, meta
        decl = e.ptr((e.ptr(p + e.O["ArrayProperty_Inner"]) if idx else p) + e.O["ObjectProperty_Class"])
        path += f".{tok}" + (f":{e.name(e.cls(o))}" if e.cls(o) != decl else "")
        cls = e.cls(o)
    return [(o, path, cls)] + roots, meta


def _member(e, cls, name):
    while cls:
        m = e.member(cls, name)
        if m:
            return m
        cls = e.super(cls)


def main(dump, addr, depth=4):
    import ueobj
    e = ueobj.open_engine(dump, log=lambda *a: None)
    roots, meta = world_root(e, dump)
    res = find_chain(e, int(addr, 0), roots, int(depth))
    if not res:
        print(f"no chain of {depth} pointer hops from GWorld reaches {addr}")
    for toks, leaf, hops, why in res:
        print(f"{'.'.join(toks)}    leaf +{leaf:#x}    ({hops} hops)\n      {why}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
