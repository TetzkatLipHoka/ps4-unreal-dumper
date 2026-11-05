"""Item catalog + live inventory editor (read / edit / add items) using the dump for lookup and ps4debug for access.

    python ueitems.py ../dumps/X/dump.bin scan [--json]                       # catalog (DataTable of items) + locate the inventory in the dump
    python ueitems.py <dump.bin> give <ip> ITEM=3 [ITEM2=1 ...|$money=999|$expand] [--force] [--rpc]   # give-function games: add items NOW
                                                       (no .TLH; amounts cut to the item's own limit unless --force; $expand = inventory to max;
                                                        ProcessEvent runs on the game thread via a one-shot hook, --rpc: from the debugger's thread)
    python ueitems.py <dump.bin> mark ok|fail|clear [Owner::Name]           # did the last give arrive? remembered per game folder
                                                       (items_tested.json): ok = preferred from now on, fail = last resort
    python ueitems.py <dump.bin> pecheck <ip> ITEM                               # dry-run the exported cheat's steps on the console
    python ueitems.py <dump.bin> catalog [--json]                             # item catalog from the scan cache
    python ueitems.py <dump.bin> <ip> read [--json] [--cand N]                # live inventory: money, backpack, warehouse, refill, key items
    python ueitems.py <dump.bin> <ip> set <bag|ware|refill> <slot> <amount> [--cand N]
    python ueitems.py <dump.bin> <ip> add <bag|ware> <ItemId> <amount> [--force] [--slot N] [--cand N]
    python ueitems.py <dump.bin> <ip> money [value] [--cand N]

How it works (no names at runtime, same idea as uelive/uecheat):
  * item IDs are the row names (FName) of the item DataTable (DT_ItemParam, row struct DMXItemParamTableRow); the
    scan reads the table's RowMap straight from the dump, so the catalog is always the game's own list;
  * the live inventory (FDMXInventoryInfo: money, backpack[60], warehouse[500], refill[3], key items[100]) is not
    reachable through reflection (no UObject holds it), so the scan looks for it by structure in the writable regions
    of the dump: a run of 24-byte slots {Num, ItemId FName, flags, WorkId FName} whose IDs are catalog rows, followed
    by the equip/shortcut tables. Needs >= 2 different items in the backpack when the dump is taken;
  * if the inventory sits in the executable's .data/.bss the address is base-relative and keeps working after a game
    restart (no new dump needed); a heap address is only valid for the session of the dump;
  * every live operation re-validates the structure in the console's memory before reading or writing.

Layout comes from the SDK (DMX_Game_structs.hpp) and is checked against the dump's reflection data on every scan.
Needs numpy for the scan (pip install numpy).
"""
import asyncio, json, os, re, struct, sys, time

import ueobj
from ueobj import CF, RF_ClassDefaultObject

SLOT = 0x18                                  # FDMXOwnItemInfo: Num@0 ID@4 bEnable@c EquipSlotType@d bRegister@e WorkId@10
PROFILE = dict(name="DMX", struct="DMXInventoryInfo", size=0x3854, row_struct="DMXItemParamTableRow",
               money=0x0, bag=0x4, bag_n=0x3c, ware=0x630, ware_n=0x1f4, refill=0x3510, refill_n=3,
               key=0x3534, key_n=0x64, bag_equip=0x5a0, bag_short=0x5ac, short_n=0x10)
AREAS = dict(bag=("bag", "bag_n", SLOT), ware=("ware", "ware_n", SLOT), refill=("refill", "refill_n", 12))
MAX_COUNT = 999999
EQUIP_VALUES = (0, 1, 2, 3, 255)             # EDMXOwnItemEquipSlotType: Primary, Secondary, Consume, Max, None = 255


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---- catalog (item DataTable) ---------------------------------------------------------------------------------------
def enum_names(e, en):
    """{value: short name} of a UEnum (same walk as uegen)."""
    O, out = e.O, {}
    arr, num = e.u64(en + O["UEnum_Names"]), e.i32(en + O["UEnum_Names"] + 8) or 0
    pair = e.enum_stride
    for i in range(min(num, 4096)):
        n = e.fname(arr + i * pair) or f"Value_{i}"
        v = i if getattr(e, "enum_names_only", False) else e.val(arr + i * pair + (0x10 if e.case_preserving else 8), "<q")
        out[v] = n.split("::")[-1]
    return out


def rowmap_off(e, tbl):
    """Offset of UDataTable::RowMap (TMap<FName,uint8*>): the TSparseArray header must be self-consistent
    (Data.Num == AllocationFlags.NumBits)."""
    for off in (0x30, 0x28, 0x38, 0x40, 0x48, 0x50):
        data, num, mx, nbits = e.ptr(tbl + off), e.i32(tbl + off + 8), e.i32(tbl + off + 12), e.i32(tbl + off + 0x28)
        if data and num and 0 < num <= mx < 1_000_000 and nbits == num:
            return off
    return None


def table_rows(e, tbl):
    """[(FName idx, FName number, name, row address)] for the live rows of a DataTable."""
    off = rowmap_off(e, tbl)
    if off is None:
        return []
    data, num = e.ptr(tbl + off), e.i32(tbl + off + 8)
    bits_at = tbl + off + 0x10 if num <= 128 else e.ptr(tbl + off + 0x20)   # TBitArray<InlineAllocator<4>>
    bits = e.d.read(bits_at, ((num + 31) // 32) * 4) if bits_at else None
    fn = e.O["FName_Size"]
    stride = ((fn + 7) & ~7) + 8 + 8                                         # TPair<FName,ptr> + HashNextId/HashIndex
    out = []
    for i in range(num):
        if bits is not None and not bits[i >> 3] >> (i & 7) & 1:
            continue
        el = data + i * stride
        idx, nb, row = e.i32(el), e.i32(el + 4), e.ptr(el + ((fn + 7) & ~7))
        name = e.fname(el)
        if idx is not None and row and name:
            out.append((idx, nb or 0, name, row))
    return out


def find_item_tables(e, row_struct):
    dts = {c for n in ("DataTable", "CompositeDataTable") if (c := e.find(n, CF["Class"]))}
    if not dts:
        sys.exit("class DataTable not found in the dump")
    cache = {}

    def is_dt(c):
        if c not in cache:
            x = c
            while x and x not in dts:
                x = e.super(x)
            cache[c] = bool(x)
        return cache[c]
    m = e.member(e.find("DataTable", CF["Class"]), "RowStruct")
    rs_off = e.prop_offset(m) if m else 0x28
    return [o for i, o in e.arr if not (e.flags(o) or 0) & RF_ClassDefaultObject and (c := e.cls(o)) and is_dt(c)
            and (rs := e.ptr(o + rs_off)) and e.name(rs) == row_struct]


def build_catalog(e, prof):
    tables = find_item_tables(e, prof["row_struct"])
    if not tables:
        sys.exit(f"no DataTable with row struct {prof['row_struct']} in the dump (item table not loaded yet? dump in-game)")
    rs = e.find(prof["row_struct"], CF["ScriptStruct"])
    fld = {n: (lambda p: e.prop_offset(p) if p else None)(e.member(rs, n))
           for n in ("MainCategory", "SalePrice", "OwnLimitByBackPack", "OwnLimitByWarehouse", "SortNo")}
    cat_names = {}
    p = e.member(rs, "MainCategory")
    if p:
        en = e.ptr(p + e.O["EnumProperty_Base"] + 8) if e.ffield_class_name(p) == "EnumProperty" else e.ptr(p + e.O["ByteProperty_Enum"])
        cat_names = enum_names(e, en) if en else {}
    items, seen = [], set()
    for t in tables:
        for idx, nb, name, row in table_rows(e, t):
            if (idx, nb) in seen:
                continue
            seen.add((idx, nb))
            g = lambda n, f="<i": (e.val(row + fld[n], f) if fld[n] is not None else None)
            cat = g("MainCategory", "<B")
            items.append(dict(id=name, idx=idx, num=nb, cat=cat_names.get(cat, str(cat)) if cat is not None else "",
                              price=g("SalePrice"), lim_bag=g("OwnLimitByBackPack"), lim_ware=g("OwnLimitByWarehouse"),
                              sort=g("SortNo"), table=e.name(t)))
    items.sort(key=lambda x: (x["cat"], x["sort"] if x["sort"] is not None else 0, x["id"]))
    return items


# ---- generic catalog (games without a built-in profile): every item-like DataTable with a numeric ID column ----------------------
SKIP_TABLE = re.compile(r"(?i)icon|dialog|talk|text|guide|brush|style|category|window|layout|preload|key|input|option|tag")


def all_data_tables(e):
    dts = {c for n in ("DataTable", "CompositeDataTable") if (c := e.find(n, CF["Class"]))}
    m = e.member(e.find("DataTable", CF["Class"]), "RowStruct")
    rs_off = e.prop_offset(m) if m else 0x28
    cache = {}

    def is_dt(c):
        if c not in cache:
            x = c
            while x and x not in dts:
                x = e.super(x)
            cache[c] = bool(x)
        return cache[c]
    return [(o, e.ptr(o + rs_off)) for i, o in e.arr if not (e.flags(o) or 0) & RF_ClassDefaultObject and (c := e.cls(o)) and is_dt(c)]


def row_id_offset(e, rs, rows):
    """Offset of the numeric item id inside a row: an IntProperty called ID / ItemID (own or inherited), else +8 when it holds distinct
    positive numbers in most rows."""
    c = rs
    while c:
        for p in e.properties(c):
            if e.ffield_class_name(p) == "IntProperty" and re.fullmatch(r"(?i)(item_?)?id", e.ffield_name(p) or ""):
                return e.prop_offset(p)
        c = e.super(c)
    vals = [e.i32(r[3] + 8) for r in rows[:200]]
    vals = [v for v in vals if v is not None]
    return 8 if len(vals) >= 3 and len(set(vals)) >= 0.9 * len(vals) and sum(1 for v in vals if v > 0) >= 0.9 * len(vals) else None


LIMIT_COL = re.compile(r"(?i)(own|have|hold|carry|max).{0,12}(limit|max|count|num|stack)|(limit|max).{0,6}(backpack|bag|ware|stor|stack|own)"
                       r"|stack.?(max|limit|size)")
WARE_COL = re.compile(r"(?i)ware|stor|stash")
CAT_COL = re.compile(r"(?i)categor|item.?type|item.?kind|item.?class|item.?group|inventory.?type|bag.?type|^(type|kind|group|class|family)$")
MAX_CATS = 60


def struct_fields(e, st):
    """[(name, kind, offset, property)] of a struct / class and its supers."""
    out, c, n = [], st, 0
    while c and n < 40:
        out += [(e.ffield_name(p) or "", e.ffield_class_name(p), e.prop_offset(p), p) for p in e.properties(c)]
        c, n = e.super(c), n + 1
    return out


def limit_fields(e, st):
    """(bag, warehouse, stack) offsets of the int members with the game's own amount limits: OwnLimitByBackPack / MaxNumHold (how many
    you may own: amounts are cut to it), OwnLimitByWarehouse, MaxStackSize (size of one stack: only shown, more just makes more stacks)."""
    bag, ware, stack = [], [], []
    for n, k, off, _ in struct_fields(e, st):
        if k == "IntProperty" and LIMIT_COL.search(n):
            (stack if re.search(r"(?i)stack", n) else ware if WARE_COL.search(n) else bag).append(off)
    return bag, ware, stack


def read_limits(e, addr, offs):
    """(bag limit, warehouse limit, stack size) of one row / object, None where the game has none."""
    def low(lst):
        v = [x for o in lst if (x := e.i32(addr + o)) and 0 < x < 10_000_000]
        return min(v) if v else None
    return tuple(low(x) for x in offs)


def category_field(e, st, rows):
    """-> function(row address) -> category name, for the member that splits the rows into 2..MAX_CATS groups (category / item type
    enum, FName or int), or None."""
    best = None
    for n, k, off, p in struct_fields(e, st):
        if not CAT_COL.search(n) or k not in ("EnumProperty", "ByteProperty", "NameProperty", "IntProperty"):
            continue
        size = min(e.prop_size(p) or 1, 8)
        vals = [e.d.read(r + off, size) for r in rows[:4000]]
        distinct = len(set(vals))
        if 2 <= distinct <= MAX_CATS and (best is None or distinct > best[0]):
            best = (distinct, k, off, p, size)
    if not best:
        return None
    _, k, off, p, size = best
    if k == "NameProperty":
        return lambda r: e.fname(r + off) or ""
    en = e.ptr(p + e.O["EnumProperty_Base"] + 8) if k == "EnumProperty" else e.ptr(p + e.O["ByteProperty_Enum"]) if k == "ByteProperty" else 0
    names = enum_names(e, en) if en else {}
    return lambda r: (lambda v: names.get(v, str(v)))(int.from_bytes(e.d.read(r + off, size) or b"\0", "little", signed=size > 1))


SKIP_WORDS = {"item", "items", "dt", "row", "data", "it", "itm", "id", "none", "default", "new", "the", "bp", "da"}


def name_prefix(name):
    """'Item_Fish_Trout' -> 'Fish', 'FishTrout' -> 'Fish', else None."""
    toks = [t for t in re.split(r"[_.\-\s]+", str(name)) if t]
    while toks and toks[0].lower() in SKIP_WORDS:
        toks.pop(0)
    if len(toks) < 2:
        toks = [w for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", toks[0] if toks else "") if w.lower() not in SKIP_WORDS]
    return toks[0] if len(toks) >= 2 and not toks[0].isdigit() and len(toks[0]) >= 2 else None


def prefix_categories(items):
    """Items without a category column (cat = table name, one table): group by name prefix when that gives 2..MAX_CATS groups."""
    pre = {id(i): name_prefix(i["id"]) for i in items}
    groups = {p for p in pre.values() if p}
    if 2 <= len(groups) <= MAX_CATS and sum(1 for p in pre.values() if p) >= 0.8 * len(items):
        for i in items:
            i["cat"] = pre[id(i)] or "Other"


def build_catalog_generic(e, need_numid=True):
    """Items of games whose item list is spread over several DataTables identified by a number (rows: name FName + numeric id).
    Category: a category / item-type column, else the table, else the name prefix; limits: the row's own limit columns."""
    items, used, tables = [], {}, set()
    for t, rs in all_data_tables(e):
        tn = e.name(t)
        if not rs or not re.search(r"(?i)item", tn) or SKIP_TABLE.search(tn):
            continue
        rows = table_rows(e, t)
        if len(rows) < 2:
            continue
        off = row_id_offset(e, rs, rows)
        if off is None and need_numid:
            continue
        cat = re.sub(r"(?i)^(c?dt_)|item(data)?$", "", tn).strip("_") or tn
        catf = category_field(e, rs, [r[3] for r in rows])
        lims = limit_fields(e, rs)
        tables.add(tn)
        for idx, nb, name, row in rows:
            numid = e.i32(row + off) if off is not None else 0
            if numid is None or (need_numid and numid <= 0) or (idx, nb, numid) in used:
                continue
            used[(idx, nb, numid)] = True
            lb, lw, ls = read_limits(e, row, lims)
            items.append(dict(id=name, idx=idx, num=nb, cat=(catf(row) if catf else "") or cat, price=None, lim_bag=lb, lim_ware=lw,
                              lim_stack=ls, sort=numid, table=tn, numid=numid))
    if len(tables) == 1 and len({i["cat"] for i in items}) == 1:
        prefix_categories(items)
    seen = {}
    for it in items:                                           # rule names must be unique: keep the row name, add #id on a clash
        seen[it["id"].lower()] = seen.get(it["id"].lower(), 0) + 1
    for it in items:
        if seen[it["id"].lower()] > 1:
            it["id"] = f"{it['id']}#{it['numid']}"
    items.sort(key=lambda x: (x["cat"], x["sort"], x["id"]))
    return items


def check_profile(e, prof):
    """The SDK layout must still match this build (game update / other game): fail loudly instead of writing blindly."""
    s = e.find(prof["struct"], CF["ScriptStruct"])
    if not s:
        sys.exit(f"struct {prof['struct']} not found: this game has no inventory profile in ueitems.py "
                 "(add one from its SDK, see PROFILE)")
    if e.struct_size(s) != prof["size"]:
        sys.exit(f"{prof['struct']} is {e.struct_size(s):#x} bytes, profile expects {prof['size']:#x}: game updated? "
                 "adjust PROFILE from the new SDK")
    want = dict(MoneyInfo=prof["money"], BackPackItemInfo=prof["bag"], WarehouseItemInfo=prof["ware"],
                RefillItemInfo=prof["refill"], ImportantItemInfo=prof["key"])
    for n, off in want.items():
        m = e.member(s, n)
        if not m or e.prop_offset(m) != off:
            sys.exit(f"{prof['struct']}.{n} is at {e.prop_offset(m) if m else None}, profile expects {off:#x}")
    slot = e.find("DMXOwnItemInfo", CF["ScriptStruct"])
    if not slot or e.struct_size(slot) != SLOT:
        sys.exit("DMXOwnItemInfo is not 0x18 bytes: profile outdated")


# ---- inventory structure (parse / validate) -------------------------------------------------------------------------
def parse_slots(buf, off, n, relaxed=False):
    """Raw 24-byte slots -> (list of tuples (slot, num, idx, nb, enable, equip, reg, work_idx, work_nb), "") or
    (None, reason) if a slot is structurally impossible (flag bytes out of range ...). `relaxed` accepts ANY Num
    (games that keep counts obfuscated in memory)."""
    out = []
    for i in range(n):
        num, idx, nb, en, eq, reg, _p, widx, wnb = struct.unpack_from("<iiiBBBBii", buf, off + i * SLOT)
        empty = idx == 0 if relaxed else (num == 0 and idx == 0)
        why = ("bEnable" if en > 1 else "bRegister" if reg > 1 else "EquipSlotType" if eq not in EQUIP_VALUES else
               "WorkId" if not 0 <= widx < 0x4000000 else "Num" if not (relaxed or 0 <= num <= MAX_COUNT) else
               "ItemId" if not (empty or (idx > 0 and 0 <= nb < 0x100000)) else "")
        if why:
            return None, f"slot {i}: {why} out of range"
        out.append((i, num, idx, nb, en, eq, reg, widx, wnb))
    return out, ""


def parse_inventory(buf, keyset, prof=PROFILE, tolerate_bag=3, tolerate_ware=25, relaxed=False):
    """(inventory dict, "") or (None, reason). Checks every table of FDMXInventoryInfo against the item catalog."""
    if len(buf) < prof["size"]:
        return None, "short read"
    bag, why = parse_slots(buf, prof["bag"], prof["bag_n"], relaxed)
    if bag is None:
        return None, "backpack " + why
    unknown = [s for s in bag if s[2] and (s[2], s[3]) not in keyset]
    if len(unknown) > tolerate_bag:
        return None, f"{len(unknown)} backpack slots with IDs outside the item catalog"
    eqs = struct.unpack_from("<3i", buf, prof["bag_equip"])
    if any(not -1 <= v < prof["bag_n"] for v in eqs):               # anchors: kept even when counts are relaxed
        return None, "equip slot indexes out of range"
    bad_short = 0                                          # shortcuts may hold things that are not catalog items (skills, ...)
    for i in range(prof["short_n"]):
        idx, nb = struct.unpack_from("<ii", buf, prof["bag_short"] + 8 * i)
        if idx > 0 and (idx, nb) not in keyset:           # -1 = nothing in this shortcut
            bad_short += 1
        elif idx < -1 or idx > 0x7fffff:
            return None, "shortcut IDs out of range"
    if bad_short > 8:
        return None, "shortcut IDs outside the item catalog"
    ware, why = parse_slots(buf, prof["ware"], prof["ware_n"], relaxed)
    if ware is None:
        return None, "warehouse " + why
    if sum(1 for s in ware if s[2] and (s[2], s[3]) not in keyset) > tolerate_ware:
        return None, "warehouse IDs outside the item catalog"
    refill = [(i,) + struct.unpack_from("<iii", buf, prof["refill"] + 12 * i) for i in range(prof["refill_n"])]   # slot, num, ID
    key = [struct.unpack_from("<ii", buf, prof["key"] + 8 * i) for i in range(prof["key_n"])]
    money = struct.unpack_from("<i", buf, prof["money"])[0]
    if not relaxed and not 0 <= money <= 2_000_000_000:
        return None, f"money {money} out of range"
    return dict(money=money, bag=bag, ware=ware, refill=refill, key=[k for k in key if k[0]], equip=eqs), ""


def populated(slots, keyset, relaxed=False):
    return sum(1 for s in slots if (s[1] > 0 or relaxed) and (s[2], s[3]) in keyset)


# ---- scan (dump) -----------------------------------------------------------------------------------------------------
def view(d, va, n):
    """n bytes at va if they lie inside ONE region of the dump, else None."""
    r = d.region(va)
    if r is None or va + n > r["end"]:
        return None
    o = r["offset"] + va - r["start"]
    return d.mm[o:o + n]


def scan_hits(d, keys, say=log):
    """Slot-like places in the writable regions: an item-catalog FName (index AND number of a catalog row) at +4 of a
    24-byte slot. Returns (all, flag_ok): addresses of the slot start; flag_ok also has plausible bEnable/EquipSlotType/
    bRegister bytes at +0xc. The count is NOT looked at (some games keep it obfuscated)."""
    import numpy as np
    idxs = np.unique(np.array([k[0] for k in keys], dtype=np.int32))
    pairs = np.array(sorted({(k[0] << 32) | k[1] for k in keys}), dtype=np.int64)
    CH = 1 << 24
    every, ok = [], []
    total = sum((r["end"] - r["start"]) for r in d.regions if r["prot"] & 2)
    done, t0 = 0, time.time()
    for r in d.regions:
        if not r["prot"] & 2:
            continue
        n = (r["end"] - r["start"]) // 4
        c0 = 0
        while c0 + 4 <= n:
            cnt = min(CH + 3, n - c0)
            a = np.frombuffer(d.mm, dtype="<i4", count=cnt, offset=r["offset"] + c0 * 4)
            idx, nb, fl = a[1:-2], a[2:-1], a[3:]                      # slot at k: Num a[k], ID (a[k+1], a[k+2]), flags a[k+3]
            ks = np.flatnonzero((nb >= 0) & (idx > 0))
            ks = ks[ks < CH]
            if ks.size:
                pos = np.minimum(np.searchsorted(idxs, idx[ks]), len(idxs) - 1)
                ks = ks[idxs[pos] == idx[ks]]                          # same FName base index as a catalog row
            if ks.size:                                                # ... and the same number (ITEM_MTL_1010 = "ITEM_MTL" + 1011)
                ks = ks[np.isin((idx[ks].astype(np.int64) << 32) | nb[ks].astype(np.int64), pairs)]
            if ks.size:
                f = fl[ks].astype(np.uint32)
                en, eq, rg = f & 0xFF, (f >> 8) & 0xFF, (f >> 16) & 0xFF
                good = (en <= 1) & (rg <= 1) & ((eq <= 3) | (eq == 255))
                addr = r["start"] + (c0 + ks.astype(np.int64)) * 4
                every.extend(addr.tolist())
                ok.extend(addr[good].tolist())
            c0 += CH
        done += r["end"] - r["start"]
        say(f"scanning writable memory {done >> 20}/{total >> 20} MiB, {len(every)} item-ID hits ({len(ok)} with slot-like flags), {time.time() - t0:.0f}s")
    return every, ok


def array_starts(hits, prof):
    """Slot-0 addresses of every 60-slot array that would hold >= 2 of the hits (slots are 24 bytes apart)."""
    import bisect
    by_res, starts = {}, set()
    for a in sorted(set(hits)):
        by_res.setdefault(a % SLOT, []).append(a)
    span = (prof["bag_n"] - 1) * SLOT
    for lst in by_res.values():
        for i, a in enumerate(lst):
            j = bisect.bisect_right(lst, a + span)
            if j - i >= 2:
                starts.update(range(lst[j - 1] - span, a + 1, SLOT))
    return sorted(s for s in starts if s > 0)


def describe_clusters(d, hits, idx_name, prof, say, top=8):
    """When nothing validates: show the best groups of item IDs 24 bytes apart with their raw bytes, so the real slot
    layout can be read off (obfuscated counts, other flag bytes, ...). Rows followed by a pointer (the rows of the game's
    own DataTables: key, number, value pointer, hash links) are left out, the inventory is a small group."""
    import bisect
    by_res, wins, tables = {}, [], 0
    for a in sorted(set(hits)):
        p = d.u64(a + 0xc)
        if d.is_ptr(p):
            tables += 1
            continue
        by_res.setdefault(a % SLOT, []).append(a)
    span = (prof["bag_n"] - 1) * SLOT
    for lst in by_res.values():
        i = 0
        while i < len(lst):
            j = bisect.bisect_right(lst, lst[i] + span)
            ids = {d.i32(a + 4) for a in lst[i:j]}
            if len(ids) >= 2:
                wins.append((len(ids), lst[i], lst[i:j]))
            i = j if j > i else i + 1
    wins.sort(key=lambda w: (-w[0], w[1]))
    say(f"{tables} hits are DataTable rows (pointer after the ID) and were left out. Groups of >= 2 different item IDs "
        f"{SLOT} bytes apart: {len(wins)}")
    for n, a0, lst in wins[:top]:
        reg = d.region(a0)
        say(f"  {n} IDs from {a0:#x} [{reg['name'] if reg else '?'}]")
        for a in lst[:6]:
            raw = view(d, a, SLOT) or b""
            say(f"     {a:#x}: {raw.hex(' ', 4)}   {idx_name.get(d.i32(a + 4), '?')}")


def scan_candidates(d, keys, prof, say=log, names=None):
    """Find FDMXInventoryInfo copies in the writable regions of the dump."""
    keyset = set(keys)
    every, ok = scan_hits(d, keys, say)
    starts = array_starts(ok, prof)
    found, fails = {}, {}
    for relaxed in (False, True):                    # strict first; then accept any count (obfuscated amounts)
        for s in starts:
            ib = s - prof["bag"]
            if ib in found:
                continue
            buf = view(d, ib, prof["size"])
            if buf is None:
                continue
            inv, why = parse_inventory(bytes(buf), keyset, prof, relaxed=relaxed)
            if inv and populated(inv["bag"], keyset, relaxed) < 2:
                inv, why = None, "fewer than 2 catalog items in the backpack"
            if inv:
                found[ib] = (inv, relaxed)
            elif not relaxed:
                why = re.sub(r"slot \d+: ", "", why)                 # group the reasons, not the slot numbers
                fails[why] = fails.get(why, 0) + 1
        if found:
            break
    if not found or all(r for _, r in found.values()):
        if found:
            say("WARNING: only matched with the amounts ignored (obfuscated counts, or not the real inventory). Strict pass failed because:")
        else:
            say(f"{len(every)} item-ID hits, {len(ok)} with slot-like flags, {len(starts)} array positions tested, none is an inventory. Why they failed:")
        for why, n in sorted(fails.items(), key=lambda kv: -kv[1])[:6]:
            say(f"   {n:6d} x {why}")
        describe_clusters(d, every, {k[0]: (names or {}).get(f"{k[0]},{k[1]}", "?") for k in keys}, prof, say)
    kept = []                                          # copies shifted by whole slots overlap: keep the fullest one
    def rank(x):                                       # fullest first; then the copy whose equip anchors point at items; then the one
        inv, rel = found[x]                            # whose first item is in the lowest slot (a shifted copy has empty slots first)
        full = populated(inv["bag"], keyset, rel) + populated(inv["ware"], keyset, rel)
        used = [s[0] for s in inv["bag"] if s[2]]
        bad_eq = sum(1 for v in inv["equip"] if v >= 0 and v not in used)
        return (-full, bad_eq, used[0] if used else 99, x)
    for ib in sorted(found, key=rank):
        if all(abs(ib - k) >= prof["size"] for k in kept):
            kept.append(ib)
    found = {ib: found[ib] for ib in kept}
    cands = []
    for ib, (inv, relaxed) in found.items():
        reg = d.region(ib)
        cands.append(dict(addr=ib, off=ib - d.base, static=reg["name"] == "executable", region=reg["name"], money=inv["money"],
                          bag_items=populated(inv["bag"], keyset, relaxed), ware_items=populated(inv["ware"], keyset, relaxed),
                          key_items=len(inv["key"]), num_odd=relaxed))
    cands.sort(key=lambda c: (not c["static"], -c["bag_items"] - c["ware_items"], c["addr"]))
    return cands


def cache_path(dump):
    return dump + ".items.json"


def load_cache(dump):
    try:
        return json.load(open(cache_path(dump), encoding="utf-8"))
    except (OSError, ValueError):
        sys.exit(f"no item scan for {dump}: run the scan first (Items window -> Scan, or: python ueitems.py {dump} scan)")


def tested_path(dump):
    """Console test results of give functions, one file per game folder: {"Owner::Name": "ok" | "fail"} (ueitems.py <dump> mark ok|fail)."""
    return os.path.join(os.path.dirname(os.path.abspath(dump)), "items_tested.json")


def load_tested(dump):
    try:
        return json.load(open(tested_path(dump), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fkey(fn):
    return f"{fn['owner']}::{fn['name']}"


def rank_give(give, tested):
    """Functions that worked on the console first, failed ones last (they stay as a last resort)."""
    order = lambda f: ({"ok": 0, "fail": 2}.get(tested.get(fkey(f)), 1), -(f.get("score") or 0))
    for k in ("item", "money", "expand"):
        fns = sorted([f for f in [give.get(k)] + list(give.get(k + "_alts") or []) if f], key=order)
        if fns:
            give[k], give[k + "_alts"] = fns[0], fns[1:]


def do_scan(dump):
    """1st choice: the game's own give-item function (works for any UE game with an SDK); 2nd: the built-in inventory profile (DEADCRAFT)."""
    e = ueobj.open_engine(dump, log=log)
    prof = PROFILE
    tested = load_tested(dump)
    import ueitems_give
    give = None
    try:
        give = ueitems_give.resolve(e, dump, say=log)
    except Exception as ex:                                   # e.g. no capstone: fall back to the profile
        log(f"give-function search failed: {ex}")
    if not give or tested.get(fkey(give["item"])) == "fail":  # no static native entry point (or it failed on the console): ProcessEvent
        try:
            import ueitems_pe
            pe = ueitems_pe.resolve(e, say=log, tested=tested)
            if pe and give:
                pe["item_alts"] = pe["item_alts"] + [give["item"]]
                pe["money"] = pe.get("money") or give.get("money")
            give = pe or give
        except Exception as ex:
            log(f"process-event search failed: {ex}")
    if give:
        try:
            import ueitems_pe
            ex_fns = ueitems_pe.find_expand(e, say=log)
            give["expand"], give["expand_alts"] = (ex_fns[0], ex_fns[1:]) if ex_fns else (None, [])
        except Exception as ex:
            log(f"inventory expansion search failed: {ex}")
        rank_give(give, tested)
        idp = next((p for p in give["item"]["params"] if p["role"] == "id"), None)
        idkind = idp["type"] if idp else "int32_t"
        if idkind in ("UClass", "UObject"):
            import ueitems_pe
            cat = ueitems_pe.class_catalog if idkind == "UClass" else ueitems_pe.object_catalog
            items = cat(e, idp["meta_addr"]) if idp.get("meta_addr") else []
        elif idp and idp.get("sname") == "GameplayTag":
            import ueitems_pe
            items = ueitems_pe.tagmap_catalog(e) or ueitems_pe.tag_catalog(e)      # the game's own tag -> asset map first, else the tag names
        else:
            items = build_catalog_generic(e, need_numid=idkind != "FName")
        log(f"{len(items)} items in the generic catalog")
        if items:
            names = {f"{i['idx']},{i['num']}": i["id"] for i in items}
            cache = dict(version=2, mode="give", dump=os.path.basename(dump), time=time.strftime("%Y-%m-%d %H:%M:%S"), base=e.d.base,
                         profile="generic", title=e.d.idx.get("title_id"), catalog=items, names=names, candidates=[], give=give)
            json.dump(cache, open(cache_path(dump), "w", encoding="utf-8"))
            return cache
        log("the give function was found but no item table with ids: trying the built-in profile")
    try:
        check_profile(e, prof)
    except SystemExit as ex:
        log(f"no built-in inventory profile for this game either ({ex})")
        items = build_catalog_generic(e, need_numid=False)
        cache = dict(version=2, mode="give", dump=os.path.basename(dump), time=time.strftime("%Y-%m-%d %H:%M:%S"), base=e.d.base,
                     profile="generic", title=e.d.idx.get("title_id"), catalog=items, names={}, candidates=[], give=None)
        json.dump(cache, open(cache_path(dump), "w", encoding="utf-8"))
        return cache
    log("reading the item DataTable ...")
    items = build_catalog(e, prof)
    log(f"{len(items)} items in the catalog")
    keys = [(i["idx"], i["num"]) for i in items]
    names = {f"{i['idx']},{i['num']}": i["id"] for i in items}
    cands = scan_candidates(e.d, keys, prof, names=names)
    keyset = set(keys)
    for c in cands:                                          # names of the IDs found outside the catalog (key items, ...)
        buf = bytes(view(e.d, c["addr"], prof["size"]))
        inv, _ = parse_inventory(buf, keyset, prof, relaxed=c["num_odd"])
        for grp in ("bag", "ware"):
            for s in inv[grp]:
                if s[2]:
                    names.setdefault(f"{s[2]},{s[3]}", e.pool.name(s[2], s[3]) or f"#{s[2]}")
        for idx, nb in inv["key"]:
            names.setdefault(f"{idx},{nb}", e.pool.name(idx, nb) or f"#{idx}")
        c["snapshot"] = dict(money=inv["money"], bag=[(s[0], names.get(f"{s[2]},{s[3]}"), s[1]) for s in inv["bag"] if s[2] and s[1]])
    cache = dict(version=1, dump=os.path.basename(dump), time=time.strftime("%Y-%m-%d %H:%M:%S"), base=e.d.base,
                 profile=prof["name"], title=e.d.idx.get("title_id"), catalog=items, names=names, candidates=cands)
    json.dump(cache, open(cache_path(dump), "w", encoding="utf-8"))
    return cache


# ---- live access ------------------------------------------------------------------------------------------------------
def cand_addr(cache, c):
    return cache["base"] + c["off"] if c["static"] else c["addr"]


class Live:
    def __init__(self, cache, dbg, pid, ib, cand):
        self.cache, self.dbg, self.pid, self.ib, self.cand = cache, dbg, pid, ib, cand
        self.keyset = {(i["idx"], i["num"]) for i in cache["catalog"]}
        self.relaxed = bool(cand.get("num_odd"))
        self.names = cache["names"]
        self.catalog = {i["id"].lower(): i for i in cache["catalog"]}

    def name(self, idx, nb):
        return self.names.get(f"{idx},{nb}") or f"#{idx}"

    async def fetch(self):
        raw = await self.dbg.read_memory(self.pid, self.ib, PROFILE["size"])
        if not raw or len(raw) < PROFILE["size"]:
            sys.exit(f"cannot read the inventory at {self.ib:#x} (game closed?)")
        inv, why = parse_inventory(bytes(raw), self.keyset, relaxed=self.relaxed)
        if inv and not (populated(inv["bag"], self.keyset, self.relaxed) or populated(inv["ware"], self.keyset, self.relaxed) or inv["key"]):
            inv, why = None, "it holds no items at all (zeroed or reused memory)"       # all-zero memory is "valid" too
        if not inv:
            sys.exit(f"the console memory at {self.ib:#x} is not the inventory any more ({why}): "
                     + ("is the game running?" if self.cand["static"] else "new session? take a new dump and scan again"))
        return inv

    async def write(self, off, data):
        addr = self.ib + off
        await self.dbg.write_memory(self.pid, addr, data)
        back = await self.dbg.read_memory(self.pid, addr, len(data))
        if bytes(back or b"") != data:
            sys.exit(f"write to {addr:#x} did not stick (readback {bytes(back or b'').hex()})")


async def connect(dump, host, cand_i):
    cache = load_cache(dump)
    cands = cache["candidates"]
    if not cands:
        sys.exit("the scan found no inventory in this dump (needs >= 2 different items in the backpack when dumping)")
    if not 0 <= cand_i < len(cands):
        sys.exit(f"candidate {cand_i} does not exist (0..{len(cands) - 1})")
    import ps4debug
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    c = cands[cand_i]
    ib = cand_addr(cache, c)
    if c["static"] and not await dbg.read_memory(pid, cache["base"], 4):
        sys.exit(f"module base {cache['base']:#x} of the dump is not readable in this session")
    return Live(cache, dbg, pid, ib, c)


def inv_json(live, inv):
    nm = live.name
    cat = live.catalog

    def slot(s):
        n = nm(s[2], s[3]) if s[2] else ""
        lim = cat.get(n.lower(), {})
        return dict(slot=s[0], id=n, num=s[1], enable=s[4], equip=s[5], reg=s[6], work=bool(s[7]),
                    lim_bag=lim.get("lim_bag"), lim_ware=lim.get("lim_ware"))
    return dict(addr=live.ib, static=live.cand["static"], money=inv["money"],
                bag=[slot(s) for s in inv["bag"]], ware=[slot(s) for s in inv["ware"] if s[2] or s[1]],
                refill=[dict(slot=s[0], id=nm(s[2], s[3]) if s[2] else "", num=s[1]) for s in inv["refill"]],
                key=[nm(i, n) for i, n in inv["key"]], equip=list(inv["equip"]))


def plan_add(live, inv, area, item, qty, force, slot=None):
    """Writes needed to add `qty` of `item` to bag/ware: (list of (offset, bytes), message). Stacks into a slot that
    already holds the item (no WorkId), else takes the first free usable slot."""
    base = PROFILE[AREAS[area][0]]
    slots = inv[area]
    lim = item["lim_bag"] if area == "bag" else item["lim_ware"]
    cap = lim if lim and lim > 0 else 9999
    if not force and qty > cap:
        sys.exit(f"{item['id']}: {qty} is above the game's limit {cap} for the {area} (use --force to ignore)")
    key = (item["idx"], item["num"])
    if slot is None:
        for s in slots:                                               # stack into an existing plain stack
            if (s[2], s[3]) == key and s[7] == 0 and s[1] > 0:
                new = s[1] + qty if force else min(s[1] + qty, cap)
                return [(base + s[0] * SLOT, struct.pack("<i", new))], f"{item['id']}: slot {s[0]} {s[1]} -> {new}"
    # New stack. bEnable = slot unlocked (the SDK has DevExpansionEnableSlotByMaxLevel: bag expansions enable slots),
    # so only enabled empty slots are used; a locked slot needs an explicit --slot together with --force.
    if slot is not None:
        free = next((s for s in slots if s[0] == slot), None) or sys.exit(f"{area} has no slot {slot}")
        if free[1] and not force:
            sys.exit(f"{area}[{slot}] already holds {free[1]} items (use --force to overwrite)")
        if not free[4] and not force:
            sys.exit(f"{area}[{slot}] is locked (not unlocked by a bag expansion); --force enables it")
    else:
        free = next((s for s in slots if s[1] == 0 and s[4]), None) or sys.exit(f"no free {area} slot (full or locked)")
    off = base + free[0] * SLOT
    amt = qty if force else min(qty, cap)
    writes = [(off, struct.pack("<iii", amt, item["idx"], item["num"]))]
    if not free[4]:
        writes.append((off + 0xc, b"\x01"))
    writes.append((off + 0xe, b"\x00"))                                 # bRegister (equipped / shortcut) = no
    writes.append((off + 0x10, struct.pack("<ii", 0, 0)))              # WorkId = None
    return writes, f"{item['id']}: new stack in slot {free[0]} x{amt}"


def give_args(fn, it, count):
    """Integer arguments of the game's give function in declaration order (System V: rdi rsi rdx rcx r8 r9), like the cheat's asm."""
    out = []
    for p in fn["params"][:6]:
        if p["role"] == "id":
            out.append((it["idx"] | (it["num"] << 32)) if p["type"] == "FName" else it["numid"])
        elif p["role"] == "count":
            out.append(count)
        else:
            out.append(0)
    return [a & (2**64 - 1) for a in out] + [0] * (6 - len(out))


async def give_live(dump, host, pairs, force=False, rpc=False):
    """Call the game's own add-item function on the console right now (no .TLH): pairs = [(item id, $money or $expand, count)].
    Native static functions are called directly; instance / Blueprint / UClass* functions go through ProcessEvent. The amount is cut to
    the item's own limit (unless force); a function that answers false is reported (the game did not take it)."""
    try:
        cache = json.load(open(cache_path(dump), encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    g = cache.get("give") if cache.get("mode") == "give" else None
    if not g:                                           # no scan yet / scan without a give function: resolve it now, like the cheat would
        log("resolving the give function from the dump ...")
        cache = do_scan(dump)
        g = cache.get("give") if cache.get("mode") == "give" else None
    if not g:
        sys.exit("no give-item function found for this game (run: python ueitems_give.py <dump.bin> --grep item,inventory,gold to look at the candidates)")
    cat = {i["id"].lower(): i for i in cache["catalog"]}
    jobs = []
    for iid, n in pairs:
        if iid.lower() in ("$money", "$expand"):
            k = iid.lower()[1:]
            fns = [x for x in [g.get(k)] + list(g.get(k + "_alts") or []) if x] or sys.exit(f"this game has no {k} function")
            jobs.append((iid, None, fns, n))
        else:
            it = cat.get(iid.lower()) or sys.exit(f"{iid} is not in the item catalog (scan again?)")
            lim = it.get("lim_bag") or it.get("lim_ware")
            if lim and n > lim and not force:
                log(f"{iid}: {n} is above the game's limit {lim}: giving {lim} (--force gives {n})")
                n = lim
            jobs.append((iid, it, [g["item"]] + list(g.get("item_alts") or []), n))
    import ps4debug, ueitems_pe
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    if not await dbg.read_memory(pid, cache["base"], 4):
        sys.exit(f"module base {cache['base']:#x} of the dump is not readable in this session (other binary/base?)")
    stub = await ueitems_pe.get_stub(dbg, pid, os.path.join(os.path.dirname(os.path.abspath(dump)), "rpc_stub.txt"))
    pe_ctx, refused, used = None, [], None

    async def context():                               # the dump is only opened when needed (class layouts, names)
        e = ueobj.open_engine(dump, log=log)
        meta = json.load(open(os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "offsets.json")))
        gw = struct.unpack("<Q", await dbg.read_memory(pid, cache["base"] + meta["gworld"], 8) or bytes(8))[0]
        gw or sys.exit("GWorld is null (loading? start the game first)")
        return e, ueitems_pe.LiveGraph(e, dbg, pid, gw, say=log), meta["process_event_index"]
    for iid, it, fns, n in jobs:
        errs, done = [], False
        for fn in fns:
            if fn.get("via") != "pe":                  # native static function: direct call
                if it and any(p["role"] == "id" and p["type"] == "FName" for p in fn["params"]):
                    pe_ctx = pe_ctx or await context()
                    it = dict(it, idx=await pe_ctx[1].live_name(it["idx"]))
                a = give_args(fn, it or {"idx": 0, "num": 0, "numid": 0}, n)
                rax = await ueitems_pe.rpc_call(dbg, pid, cache["base"] + fn["impl_rva"], a[:len(fn["params"])], stub)
                no = (fn.get("ret") or "").split()[-1:] == ["bool"] and rax is not None and not int(rax) & 0xFF
                print(f"{iid}: {fn['owner']}::{fn['name']}({', '.join(hex(x) for x in a[:len(fn['params'])])}) -> rax={rax}"
                      + ("  REFUSED (returned false)" if no else ""))
                refused += [iid] if no else []
                used = used or (None if iid.startswith("$") else fkey(fn))
                done = True
                break
            pe_ctx = pe_ctx or await context()
            e, graph, pe_idx = pe_ctx
            try:
                rax, no = await ueitems_pe.call_live(e, dbg, pid, graph, fn, it, n, pe_idx, stub, say=log, rpc=rpc)
                print(f"{iid}: {fn['owner']}::{fn['name']} via ProcessEvent x{n}" + (f" -> rax={rax}" if rpc else "") + ("  REFUSED (returned false)" if no else ""))
                refused += [iid] if no else []
                used = used or (None if iid.startswith("$") else fkey(fn))
                if fn.get("chain_live") and fn["chain_live"] != fn.get("chain_saved"):        # keep the chain that worked for the cheat export
                    fn["chain_saved"] = fn["chain_live"]
                    json.dump(cache, open(cache_path(dump), "w", encoding="utf-8"))
                done = True
                break
            except ueitems_pe.PEError as ex:
                errs.append(f"{fn['owner']}::{fn['name']}: {ex}")
                log(f"   {errs[-1]}")
        if not done:
            sys.exit(f"{iid}: could not call any give function:\n  " + "\n  ".join(errs))
    if used:
        cache["last_give"] = used                     # what "mark ok|fail" refers to
        json.dump(cache, open(cache_path(dump), "w", encoding="utf-8"))
        log(f"did it arrive? remember it for this game: python ueitems.py {dump} mark ok   (or: mark fail)")
    if refused:
        sys.exit(f"the game refused {', '.join(refused)} (the function returned false: inventory full, limit reached or not an item of this "
                 "function)")


def mark(dump, verdict, key=None):
    """Remember a console test of a give function for this game: ok = preferred from now on, fail = only as a last resort."""
    verdict in ("ok", "fail", "clear") or sys.exit("mark: ok | fail | clear")
    cache = load_cache(dump)
    key = key or cache.get("last_give") or sys.exit("nothing given yet: give an item first, or name the function (Owner::Name)")
    t = load_tested(dump)
    if verdict == "clear":
        t.pop(key, None)
    else:
        t[key] = verdict
    json.dump(t, open(tested_path(dump), "w", encoding="utf-8"), indent=1)
    if cache.get("give"):
        rank_give(cache["give"], t)
        json.dump(cache, open(cache_path(dump), "w", encoding="utf-8"))
    print(f"{key}: {verdict}" + (f"; give function now {fkey(cache['give']['item'])}" if cache.get("give") else "")
          + ("  (scan again to look at the other functions too)" if verdict == "fail" else ""))


async def pe_check(dump, host, iid):
    """Runs the same steps the exported cheat does, against the live console, and says where it would stop (python ueitems.py <dump> pecheck <ip> <ItemId>)."""
    import re as _re, ps4debug, ueitems_pe, ueitems_tlh
    P = ueitems_pe
    cache = json.load(open(cache_path(dump), encoding="utf-8"))
    g = cache.get("give") or sys.exit("no scan for this dump")
    fn = g["item"]
    fn.get("via") == "pe" or sys.exit("this game's give function is a native static one (no ProcessEvent): nothing to check")
    it = {i["id"].lower(): i for i in cache["catalog"]}.get(iid.lower()) or sys.exit(f"{iid} is not in the catalog")
    e = ueobj.open_engine(dump, log=log)
    meta = json.load(open(os.path.join(os.path.dirname(os.path.abspath(dump)), "SDK", "offsets.json")))
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    gw = struct.unpack("<Q", await dbg.read_memory(pid, e.d.base + meta["gworld"], 8))[0]
    graph = P.LiveGraph(e, dbg, pid, gw, say=log)
    O = e.O
    print(f"function {fn['owner']}::{fn['name']}  static={fn['static']}  GWorld={gw:#x}")
    ok = True
    # 1. the instance as the live walk finds it
    free = fn["static"] or fn.get("this_unused")              # no instance needed: called on GWorld with the class found by name
    got = await graph.find({fn["owner"]}) if not free else {}
    bfs = got.get(fn["owner"].lower())
    print(f"1. live walk from GWorld: instance {bfs and hex(bfs)}  hops {graph.paths.get(bfs)}")
    # 2. the chain the cheat uses
    obj = bfs
    if not free:
        ch = ueitems_tlh.pe_instance_chain(e, dump, cache, fn)
        print(f"2. exported chain ({'learned live' if fn.get('chain_live') else 'from the dump'}): {ch and ch['text']}")
        if ch:
            lines = ueitems_tlh.pe_block(e, dump, cache, [(1, iid, "1")], meta)[0]
            offs = []
            for l in lines[lines.index("peg0:") + 1:]:
                if l == "mov rbx, rcx":
                    break
                m = _re.match(r"mov rcx, \[rcx\+(0x[0-9a-f]+)\]$", l)
                if m:
                    offs.append(int(m.group(1), 16))
            cur = gw
            for off in offs:
                raw = await graph.rd(cur + off, 8)
                nxt = struct.unpack("<Q", raw)[0] if raw else 0
                head = await graph.rd(nxt, 0x20) if P._ok_ptr(nxt) else None
                cn = ""
                if head:
                    ki = await graph.kinfo(struct.unpack_from("<Q", head, graph.o_cls)[0])
                    cn = ki["names"][0] if ki else "?"
                print(f"     [+{off:#x}] -> {nxt:#x} {cn}")
                if not nxt:
                    break
                cur = nxt
            else:
                print(f"   chain ends at {cur:#x}; live walk instance {bfs and hex(bfs)}: {'SAME object' if cur == bfs else 'DIFFERENT object - the chain is wrong'}")
                obj = cur
                ok = ok and cur == bfs
    # 3. the UFunction by FName index
    ufn = e.find_in_outer(fn["name"], fn["owner"])
    idx = await graph.live_name(e.i32(ufn + O["UObject_Name"]))
    if obj:
        head = await graph.rd(obj, 0x20)
        func = await graph.live_function(struct.unpack_from("<Q", head, graph.o_cls)[0], idx) if head else None
        print(f"3. UFunction with FName index {idx:#x} in the object's class chain: {func and hex(func)}")
        ok = ok and bool(func)
    # 4. the item class
    ca = it.get("class_addr")
    if ca:
        want = await graph.live_name(e.i32(ca + O["UObject_Name"]))
        raw = await graph.rd(ca, 0x20)
        live_idx = struct.unpack_from("<i", raw, graph.o_name)[0] if raw else None
        print(f"4. item class {it['id']}: dump address {ca:#x}, name index in dump {want:#x}, live at that address: {live_idx and hex(live_idx)}  -> {'OK' if live_idx == want else 'NOT THE SAME in this session'}")
        ok = ok and live_idx == want
        # 5. the GObjects array arithmetic the cheat uses: find the item class by scanning the chunks
        lay = meta["gobjects_layout"]
        base = e.d.base + meta["gobjects"]
        hd = await graph.rd(base, 0x20)
        objs, num = struct.unpack_from("<Q", hd, lay[0])[0], struct.unpack_from("<i", hd, lay[2])[0]
        per, isz, ooff = e.arr.per_chunk, meta["fuobjectitem_size"], meta.get("fuobjectitem_object_offset", 0)
        hit = None
        for c in range(num // per + 1):
            cp = struct.unpack("<Q", (await graph.rd(objs + 8 * c, 8)) or bytes(8))[0]
            if not cp:
                continue
            n = min(per, num - c * per)
            blob = await graph.rd(cp, n * isz) if n > 0 else None
            if not blob:
                continue
            for j in range(n):
                if struct.unpack_from("<Q", blob, j * isz + ooff)[0] == ca:
                    hit = c * per + j
                    break
            if hit is not None:
                break
        print(f"5. GObjects: {num} objects, item class found at index {hit}")
        ok = ok and hit is not None
    print("RESULT:", "every step of the cheat would succeed" if ok else "the cheat would stop at a step marked above")


async def live_main(dump, host, cmd, rest, flags):
    live = await connect(dump, host, int(flags.get("cand", 0)))
    inv = await live.fetch()
    as_json = "json" in flags
    if cmd == "read":
        j = inv_json(live, inv)
        if as_json:
            print(json.dumps(j))
        else:
            print(f"inventory at {live.ib:#x} ({'static' if j['static'] else 'heap'})  money {j['money']}")
            for g in ("bag", "ware"):
                for s in j[g]:
                    if s["num"] or s["id"]:
                        print(f"  {g}[{s['slot']}] {s['id']} x{s['num']}")
            print("  key items:", ", ".join(j["key"]))
        return
    if live.relaxed and cmd in ("money", "set", "add"):
        sys.exit("this game keeps the amounts obfuscated in memory (the scan only matched the item IDs): "
                 "reading works, editing amounts is disabled so nothing gets corrupted")
    if cmd == "money":
        if rest:
            v = int(rest[0], 0)
            await live.write(PROFILE["money"], struct.pack("<i", v))
            print(f"money {inv['money']} -> {v}")
        else:
            print(inv["money"])
        return
    if cmd == "set":
        area, slot, num = rest[0], int(rest[1], 0), int(rest[2], 0)
        if area not in AREAS:
            sys.exit(f"area must be one of {', '.join(AREAS)}")
        _, ncount, stride = AREAS[area]
        if not 0 <= slot < PROFILE[ncount]:
            sys.exit(f"slot {slot} out of range 0..{PROFILE[ncount] - 1}")
        if not 0 <= num <= MAX_COUNT:
            sys.exit(f"amount must be 0..{MAX_COUNT}")
        off = PROFILE[AREAS[area][0]] + slot * stride
        old = struct.unpack_from("<i", await live.dbg.read_memory(live.pid, live.ib + off, 4))[0]
        await live.write(off, struct.pack("<i", num))
        print(f"{area}[{slot}] {old} -> {num}")
        return
    if cmd == "add":
        area, name, qty = rest[0], rest[1], int(rest[2], 0)
        if area not in ("bag", "ware"):
            sys.exit("area must be bag or ware")
        item = live.catalog.get(name.lower()) or sys.exit(f"item {name} is not in the catalog")
        if qty <= 0:
            sys.exit("amount must be > 0")
        writes, msg = plan_add(live, inv, area, item, qty, "force" in flags, int(flags["slot"]) if "slot" in flags else None)
        for off, data in writes:
            await live.write(off, data)
        print(msg)
        return
    sys.exit(f"unknown command {cmd}")


def parse_argv(argv):
    flags, pos, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--json", "--force", "--rpc"):
            flags[a[2:]] = True
        elif a in ("--cand", "--slot"):
            flags[a[2:]] = argv[i + 1]
            i += 1
        else:
            pos.append(a)
        i += 1
    return pos, flags


def main(argv):
    pos, flags = parse_argv(argv)
    if len(pos) < 2:
        sys.exit(__doc__)
    dump, cmd = pos[0], pos[1]
    if not os.path.isfile(dump) or not os.path.isfile(dump[:-4] + ".json"):
        sys.exit(f"dump not found: {dump} (and its .json index)")
    if cmd in ("scan", "catalog"):
        cache = do_scan(dump) if cmd == "scan" else load_cache(dump)
        if "json" in flags:
            print(json.dumps(cache))
        else:
            if cache.get("mode") == "give":
                g = cache.get("give")
                print(f"{len(cache['catalog'])} items; give-function mode: " + (f"{g['item']['owner']}::{g['item']['name']} " + (f"impl base+{g['item']['impl_rva']:#x}" if "impl_rva" in g['item'] else "via ProcessEvent") if g else "NO give function found"))
                return
            print(f"{len(cache['catalog'])} items, {len(cache['candidates'])} inventory candidate(s)")
            for n, c in enumerate(cache["candidates"]):
                print(f"  [{n}] {c['addr']:#x} {'static' if c['static'] else 'heap'} {c['region']}: money {c['money']}, "
                      f"{c['bag_items']} backpack / {c['ware_items']} warehouse items")
        return
    if cmd == "pecheck":                                # ueitems.py <dump> pecheck <ip> <ItemId>
        len(pos) >= 4 or sys.exit(__doc__)
        ueobj.run(pe_check(dump, pos[2], pos[3]))
        return
    if cmd == "give":                                   # ueitems.py <dump> give <ip> <ItemId=count|$money=count> ...
        if len(pos) < 4:
            sys.exit(__doc__)
        pairs = []
        for a in pos[3:]:
            k, _, v = a.rpartition("=")
            pairs.append((k, int(v, 0)) if k else (a, 1))
        ueobj.run(give_live(dump, pos[2], pairs, "force" in flags, "rpc" in flags))
        return
    if cmd == "mark":                                   # ueitems.py <dump> mark ok|fail|clear [Owner::Name]
        len(pos) >= 3 or sys.exit(__doc__)
        mark(dump, pos[2].lower(), pos[3] if len(pos) > 3 else None)
        return
    if len(pos) < 3:
        sys.exit(__doc__)
    ueobj.run(live_main(dump, pos[1], pos[2], pos[3:], flags))


if __name__ == "__main__":
    main(sys.argv[1:])
