"""UE3 (Unreal Engine 3, 64-bit PS4 builds) profile: finds GNames (TArray<FNameEntry*>) and GObjects
(TArray<UObject*>) on a dump, measures the reflection offsets and exposes them through a ueobj.Engine so the
generators (uegen/uerc/uelive/uecall) work unchanged. ueobj.open_engine falls back to this when no UE4 name pool is
found.

    python ue3find.py ../dumps/CUSA02231/dump.bin
"""
import struct, sys
import ueobj
from ueobj import CF, RF_ClassDefaultObject
from uefind import Dump, data_ranges, find_none_entries


class NameTable:
    """TArray<FNameEntry*>; FNameEntry = {int32 Index<<1|wide; pad; FNameEntry* HashNext; char/wchar Name[]}."""
    STR = 0x10

    def __init__(self, d, addr, data, num):
        self.d, self.addr, self.data, self.num = d, addr, data, num
        self.block_bits = 0
        # Header = Index << 1 | wide (stock UE3) or plain Index without a wide bit (measured: Deadpool CUSA03528).
        self.shift = 1 if d.i32(self.entry(1) or 0) == 2 else 0

    def entry(self, idx):
        return self.d.u64(self.data + 8 * idx) if 0 <= idx < self.num else None

    def name(self, idx, number=0):
        e = self.entry(idx)
        if not e or not self.d.is_ptr(e):
            return None
        hdr = self.d.i32(e)
        raw = self.d.read(e + self.STR, 1024) or b""
        if self.shift and hdr & 1:
            s = raw[:len(raw) & ~1].decode("utf-16-le", "replace").split("\x00")[0]
        else:
            s = raw.split(b"\x00")[0].decode("latin1")
        return f"{s}_{number - 1}" if number > 0 else s

    @staticmethod
    def find(d):
        """Entry 0 is 'None' with Index 0; the pointer table starts with its address and continues with
        'ByteProperty'; the TArray head {Data, Num, Max} references the table."""
        for va in find_none_entries(d):
            key = struct.pack("<Q", va)
            p = d.mm.find(key)
            while p != -1:
                data = d.va(p)
                e1 = d.u64(data + 8) if data % 8 == 0 else 0
                if d.is_ptr(e1) and (d.read(e1 + NameTable.STR, 13) or b"") == b"ByteProperty\x00":
                    q = d.mm.find(struct.pack("<Q", data))
                    while q != -1:
                        arr = d.va(q)
                        num, mx = d.i32(arr + 8), d.i32(arr + 12)
                        if arr % 8 == 0 and num and mx and 1000 < num <= mx < 0x400000:
                            return NameTable(d, arr, data, num)
                        q = d.mm.find(struct.pack("<Q", data), q + 1)
                p = d.mm.find(key, p + 1)


class PoolNames:
    """Mass Effect Legendary Edition (measured ME1 CUSA19515, LTCG-BioGame.elf): no TArray<FNameEntry*>. Entries are
    {u32 Hash|flags (bit30 = wide); FNameEntry* HashNext @+4; char/wchar Name @+0xc}, packed back to back in pools:
    pool 0 = static names in the exe data segment (starts with 'None'), pools 1..7 = 32 MiB heap blocks allocated on
    demand. FName.Index = pool << 29 | byte offset; the pool base table (8 pointers) is the GNames global."""
    STR, NPOOLS = 0xc, 8

    def __init__(self, d, addr):
        self.d, self.addr = d, addr
        self.pools = [d.u64(addr + 8 * i) or 0 for i in range(self.NPOOLS)]
        self.num = self.block_bits = 0        # duck-typing: no index count, no block bits

    def entry(self, idx):
        base = self.pools[(idx >> 29) & 7]
        return base + (idx & 0x1fffffff) if base else None

    def name(self, idx, number=0):
        if idx is None or idx < 0:
            return None
        e = self.entry(idx & 0xffffffff)
        hdr = self.d.i32(e) if e else None
        if hdr is None:
            return None
        raw = self.d.read(e + self.STR, 1024) or b""
        if hdr & 0x40000000:
            s = raw[:len(raw) & ~1].decode("utf-16-le", "replace").split("\x00")[0]
        else:
            s = raw.split(b"\x00")[0].decode("latin1")
        return f"{s}_{number - 1}" if number > 0 else s

    @staticmethod
    def find(d):
        """Static entry 0 = {hash, HashNext 0, 'None'}; the pool table starts with its address, slot 1 points at a
        heap entry (first name allocated at runtime)."""
        for va in d.find_all(rb".{4}\x00{8}None\x00"):
            key = struct.pack("<Q", va)
            p = d.mm.find(key)
            while p != -1:
                tab = d.va(p)
                e1 = d.u64(tab + 8)
                if tab % 8 == 0 and e1 and d.is_ptr(e1) and e1 != va:
                    s = (d.read(e1 + PoolNames.STR, 16) or b"").split(b"\x00")[0]
                    if s and all(0x20 <= c < 0x7f for c in s):
                        return PoolNames(d, tab)
                p = d.mm.find(key, p + 1)


class ObjTable:
    """TArray<UObject*>: obj[i]->Index == i."""
    item_size, layout = 8, None

    def __init__(self, d, addr, data, num, index_off):
        self.d, self.addr, self.data, self.num, self.index_off = d, addr, data, num, index_off

    def __iter__(self):
        for i in range(self.num):
            o = self.d.u64(self.data + 8 * i)
            if o and self.d.is_ptr(o) and self.d.i32(o + self.index_off) == i:   # skip slots freed during the dump
                yield i, o

    @staticmethod
    def find(d):
        for lo, hi in data_ranges(d):
            r = d.region(lo)
            buf = d.mm[r["offset"]:r["offset"] + hi - lo]
            for off in range(0, len(buf) - 16, 8):
                data, num, mx = struct.unpack_from("<QiI", buf, off)
                if not (d.is_ptr(data) and 10000 < num <= mx < 0x1000000):
                    continue
                for io in range(4, 0x44, 4):
                    if all(d.is_ptr(o := d.u64(data + 8 * i) or 0) and d.i32(o + io) == i for i in (1, 2, 5, 100, 1000)):
                        return ObjTable(d, lo + off, data, num, io)


class Engine3(ueobj.Engine):
    """UE3 has no cast flags, CDOs are 'Default__*', bools are DWORD bitmasks, enums are TArray<FName>."""
    enum_stride = 8
    size_mask = 0xffffffff

    def __init__(self, d, pool, arr, log=print):
        super().__init__(d, pool, arr, log)
        self.enum_names_only = True
        self._cf = {}

    def struct_size(self, s):
        v = self.i32(s + self.O["UStruct_Size"])
        return None if v is None else v & self.size_mask

    def cast_flags(self, c):
        if c not in self._cf:
            cf, x = 0, c
            while x:
                n = self.name(x) or ""
                cf |= CF.get(n, 0)
                x = self.super(x) if "UStruct_SuperStruct" in self.O else None
            self._cf[c] = cf
        return self._cf[c]

    def flags(self, o):
        return RF_ClassDefaultObject if (self.name(o) or "").startswith("Default__") else 0

    def bool_info(self, p):
        """(FieldSize, ByteOffset, ByteMask, FieldMask) like UE4, derived from the UE3 DWORD BitMask."""
        m = self.val(p + self.O["BoolProperty_Base"], "<I") or 1
        bit = m.bit_length() - 1
        return 1, 0, 1 << (bit & 7), 1 << (bit & 7)

    def prop_offset(self, p):
        off = self.i32(p + self.O["Property_Offset_Internal"])
        if off is not None and self.name(self.cls(p)) == "BoolProperty":
            off += ((self.val(p + self.O["BoolProperty_Base"], "<I") or 1).bit_length() - 1) >> 3
        return off

    # ---- measurement -------------------------------------------------------------------------------------------
    def measure(self):
        O, d, log = self.O, self.d, self.log
        O["UObject_Index"] = self.arr.index_off
        objs = [o for _, (_, o) in zip(range(200), self.arr)]
        # Name: int32 name index + int32 number resolving for every sample; Class: pointer whose own slot is itself
        for k in range(0x10, 0x100, 8):
            if all(self.pool.name(d.i32(o + k) or -1) for o in objs) and all(0 <= (d.i32(o + k + 4) or 0) < 0x10000 for o in objs):
                O["UObject_Name"] = k
                break
        for k in range(0x10, 0x100, 8):
            if all(d.is_ptr(c := d.u64(o + k) or 0) and d.u64(c + k) == c for o in objs):
                O["UObject_Class"] = k
                break
        self.build_index()
        vec, guid = self.find("Vector"), self.find("Guid")
        # Outer: Vector's outer is the class 'Object'
        obj_cls = next(o for o in self.by_name["object"] if self.cls(o) == self.cls(self.cls(o)))
        for k in range(0x10, 0x100, 8):
            if d.u64(vec + k) == obj_cls:
                O["UObject_Outer"] = k
                break
        actor = self.find("Actor")
        x, y, z = (self.find_in_outer(n, "Vector") for n in "XYZ")
        a = self.find_in_outer("A", "Guid")
        log(f"UObject: index {O['UObject_Index']:#x} name {O['UObject_Name']:#x} class {O['UObject_Class']:#x} outer {O['UObject_Outer']:#x}")
        O["UField_Next"] = next(k for k in range(0x40, 0x100, 8) if d.u64(x + k) == y)
        O["UStruct_SuperStruct"] = next(k for k in range(0x40, 0x100, 8) if d.u64(actor + k) == obj_cls)
        O["UStruct_Children"] = next(k for k in range(0x40, 0x100, 8) if d.u64(vec + k) == x and d.u64(guid + k) == a)
        # Borderlands TPS packs flags into the top byte of PropertiesSize (Vector 0x0400000c) -> compare the low 24 bits
        O["UStruct_Size"] = next(k for k in range(0x40, 0x100, 4)
                                 if ((d.i32(vec + k) or 0) & 0xffffff, (d.i32(guid + k) or 0) & 0xffffff) == (12, 16))
        self.size_mask = 0xffffff if (d.i32(vec + O["UStruct_Size"]) or 0) >> 24 else 0xffffffff
        O["UStruct_MinAlignment"] = None
        O["UStruct_Script"] = O["UStruct_Size"] + 8            # TArray<BYTE> Script {Data, Num, Max}
        O["Property_Offset_Internal"] = next(k for k in range(0x40, 0x100, 4) if (d.i32(x + k), d.i32(y + k), d.i32(z + k)) == (0, 4, 8))
        O["Property_ArrayDim"] = next(k for k in range(0x40, O["Property_Offset_Internal"], 4)
                                      if all(d.i32(p + k) == 1 for p in (x, y, z, a)) and d.i32(x + k + 4) == 4)
        O["Property_ElementSize"] = O["Property_ArrayDim"] + 4
        O["Property_PropertyFlags"] = O["Property_ArrayDim"] + 8
        loc = self.member(actor, "Location")
        base = next(k for k in range(O["Property_Offset_Internal"] + 4, 0x140, 8) if d.u64(loc + k) == vec)
        for key in ("StructProperty_Struct", "ObjectProperty_Class", "ByteProperty_Enum", "ArrayProperty_Inner", "BoolProperty_Base",
                    "DelegateProperty_SignatureFunction", "EnumProperty_Base", "SetProperty_ElementProp", "MapProperty_Base",
                    "FieldPathProperty_FieldClass", "OptionalProperty_ValueProperty"):
            O[key] = base
        O["Property_Size"] = base
        O["ClassProperty_MetaClass"] = base + 8
        log(f"UStruct: super {O['UStruct_SuperStruct']:#x} children {O['UStruct_Children']:#x} size {O['UStruct_Size']:#x}; "
            f"Property: dim {O['Property_ArrayDim']:#x} offset {O['Property_Offset_Internal']:#x} sub-field {base:#x}")
        # UEnum::Names: TArray<FName> whose first name is the enum's first value
        en = self.find("EAxis") or next(o for _, o in self.arr if self.isa(o, CF["Enum"]))
        O["UEnum_Names"] = next(k for k in range(0x40, 0x100, 8)
                                if d.is_ptr(d.u64(en + k) or 0) and 0 < (d.i32(en + k + 8) or 0) < 4096
                                and (self.fname(d.u64(en + k)) or "").upper().startswith(("AXIS", "E", "A")))
        # UFunction: flags with FUNC_Native (0x400) on natives, clear on a script event; native Func pointer into the executable
        # samples: Object's functions are (almost all) native and have tiny NativeParm scripts; long scripts are UnrealScript
        funcs = [o for _, o in self.arr if self.isa(o, CF["Function"])]
        natives = [f for f in funcs if self.outer(f) == obj_cls and (d.i32(f + O["UStruct_Script"] + 8) or 0) < 64]
        scripts = [f for f in funcs if (d.i32(f + O["UStruct_Script"] + 8) or 0) >= 100][:200]
        def native_bit_ratio(k, fs):
            return sum(1 for f in fs if (d.i32(f + k) or 0) & 0x400) / max(len(fs), 1)
        O["UFunction_FunctionFlags"] = next(k for k in range(O["UStruct_Size"] + 4, 0x140, 4)
                                            if native_bit_ratio(k, natives) > 0.9 and native_bit_ratio(k, scripts) < 0.05)
        O["UFunction_iNative"] = O["UFunction_FunctionFlags"] + 4       # WORD; ProcessEvent returns early when != 0
        O["UFunction_ExecFunction"] = next((k for k in range(O["UFunction_FunctionFlags"], 0x140, 8)
                                            if d.base <= (d.u64(natives[0] + k) or 0) < d.exe_end), None)
        cdo = next(o for o in self.by_name.get("default__actor", ()) if self.cls(o) == actor)
        O["UClass_ClassDefaultObject"] = next(k for k in range(O["UFunction_FunctionFlags"], 0x300, 8) if d.u64(actor + k) == cdo)
        log(f"UEnum::Names {O['UEnum_Names']:#x}  UFunction: flags {O['UFunction_FunctionFlags']:#x} func {O['UFunction_ExecFunction']}  "
            f"UClass::ClassDefaultObject {O['UClass_ClassDefaultObject']:#x}")
        O["ULevel_Actors"] = self.find_level_actors_ue3()
        log(f"ULevel::Actors {O['ULevel_Actors']}")
        if O["UObject_Index"] + 8 not in (O["UObject_Outer"], O["UObject_Name"], O["UObject_Class"]):
            O["UObject_Flags"] = O["UObject_Index"] + 8             # QWORD ObjectFlags follows the index (stock UE3)
        # UWorld/ULevel are native-only in UE3: measure the pointer fields the generators need
        world, level, wi = self.find("World", CF["Class"]), self.find("Level", CF["Class"]), self.find("WorldInfo", CF["Class"])
        winst = next((o for _, o in self.arr if self.cls(o) == world and not self.flags(o)), None)
        nm = {}
        if winst and level and wi:
            def field(target):
                for k in range(0x60, self.struct_size(world) or 0x400, 8):
                    p = d.u64(winst + k)
                    if p and d.is_ptr(p) and self.cls(p) == target:
                        return k
            nm[world] = [(k, n, "ptr", c) for k, n, c in ((field(level), "PersistentLevel", level), (field(wi), "WorldInfo", wi)) if k is not None]
        if level and O["ULevel_Actors"]:
            owner = level                                            # the field may live in a base class (ULevelBase)
            while (sup := self.super(owner)) and (self.struct_size(sup) or 0) > O["ULevel_Actors"]:
                owner = sup
            nm[owner] = [(O["ULevel_Actors"], "Actors", "tarray", actor)]
        self.native_members = nm
        log("native members: " + "; ".join(f"{self.name(c)}: " + ", ".join(f"{n}@{k:#x}" for k, n, _, _ in v) for c, v in nm.items()))

    def find_level_actors_ue3(self):
        d = self.d
        lvl = next((o for _, o in self.arr if self.isa(o, CF["Level"]) and not self.flags(o)), None)
        if not lvl:
            return None
        for k in range(0x60, 0x400, 8):
            data, num, mx = d.u64(lvl + k), d.i32(lvl + k + 8), d.i32(lvl + k + 12)
            if d.is_ptr(data) and num and 0 < num <= mx < 0x100000:
                first = next((d.u64(data + 8 * i) for i in range(min(num, 16)) if d.u64(data + 8 * i)), 0)
                if first and d.is_ptr(first) and self.isa(first, CF["Actor"]):
                    return k
        return None


def open_engine(d, log=print, meta=None):
    if meta:
        pool = (PoolNames(d, meta["pool"]) if meta.get("pool_kind") == "PoolNames"
                else NameTable(d, meta["pool"], meta["pool_data"], meta["pool_num"]))
        arr = ObjTable(d, meta["arr"], meta["arr_data"], meta["arr_num"], meta["index_off"])
    else:
        pool = NameTable.find(d) or PoolNames.find(d)
        if not pool:
            return None
        arr = ObjTable.find(d)
        if not arr:
            sys.exit("UE3 names found but no TArray<UObject*> with obj[i]->Index == i in the exe data segment "
                     "(custom UObject layout, e.g. Deadpool CUSA03528: 0x40-byte objects, no Class/Outer pointers -> needs a profile)")
    log(f"names: {'UE3 TArray<FNameEntry*>' if isinstance(pool, NameTable) else 'Mass Effect LE pool table (pool<<29 | offset)'}\n"
        f"GNames {pool.addr:#x} (base+{pool.addr - d.base:#x})  GObjects {arr.addr:#x} "
        f"(base+{arr.addr - d.base:#x})  objects {arr.num}  names {pool.num}")
    e = Engine3(d, pool, arr, log)
    e.measure()
    e.cache_meta = dict(kind="ue3", pool_kind=type(pool).__name__, pool=pool.addr, pool_data=getattr(pool, "data", None),
                        pool_num=pool.num, arr=arr.addr, arr_data=arr.data, arr_num=arr.num, index_off=arr.index_off)
    return e


if __name__ == "__main__":
    e = open_engine(Dump(sys.argv[1]))
    print(e.O)
