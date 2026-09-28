"""Unreal object model on top of a memory dump: finds engine-struct offsets heuristically (port of Dumper-7's
OffsetFinder) and exposes objects, classes, properties and functions.

    python ueobj.py ../dumps/lollipop.bin        # prints offsets, writes <dump>_offsets.json and GObjects.txt
"""
import json, os, random, struct, sys
from uefind import Dump, NamePool, NameArray, ObjectArray, FlatObjectArray, data_ranges

# EClassCastFlags
CF = dict(Field=1 << 0, Enum=1 << 2, Struct=1 << 3, ScriptStruct=1 << 4, Class=1 << 5, ByteProperty=1 << 6,
          IntProperty=1 << 7, FloatProperty=1 << 8, ClassProperty=1 << 10, InterfaceProperty=1 << 12,
          NameProperty=1 << 13, StrProperty=1 << 14, Property=1 << 15, ObjectProperty=1 << 16, BoolProperty=1 << 17,
          Function=1 << 19, StructProperty=1 << 20, ArrayProperty=1 << 21, DelegateProperty=1 << 23,
          NumericProperty=1 << 24, MulticastDelegateProperty=1 << 25, ObjectPropertyBase=1 << 26,
          WeakObjectProperty=1 << 27, LazyObjectProperty=1 << 28, SoftObjectProperty=1 << 29, TextProperty=1 << 30,
          SoftClassProperty=1 << 33, Package=1 << 34, Level=1 << 35, Actor=1 << 36, MapProperty=1 << 46,
          SetProperty=1 << 47, EnumProperty=1 << 48, MulticastInlineDelegateProperty=1 << 50,
          MulticastSparseDelegateProperty=1 << 51, FieldPathProperty=1 << 52, OptionalProperty=1 << 56)
# EFunctionFlags
FF = dict(Final=0x1, RequiredAPI=0x2, Exec=0x200, Native=0x400, Public=0x20000, BlueprintCallable=0x4000000,
          BlueprintPure=0x10000000, Const=0x40000000)
# EPropertyFlags
PF = dict(Edit=0x1, BlueprintVisible=0x4, ZeroConstructor=0x200, SaveGame=0x1000000, IsPlainOldData=0x40000000,
          NoDestructor=0x1000000000, HasGetValueTypeHash=0x8000000000000, NativeAccessSpecifierPublic=0x10000000000000)
RF_ClassDefaultObject = 0x10
NOTFOUND = None


def align(v, a):
    return (v + a - 1) & ~(a - 1)


class Engine:
    def __init__(self, d, pool, arr, log=print):
        self.d, self.pool, self.arr, self.log = d, pool, arr, log
        self.O = dict(UObject_Vft=0, UObject_Flags=8, UObject_Index=0xC, UObject_Class=0x10, UObject_Name=0x18,
                      UObject_Outer=0x20, FName_Number=4, FName_Size=8,
                      FField_Class=8, FField_Owner=0x10, FField_Next=0x20, FField_Name=0x28, FField_Flags=0x30,
                      FFieldClass_CastFlags=0x10)
        self.use_fproperty = False
        self.case_preserving = False
        self.outline_number = False
        self.by_name = {}

    # ---- raw accessors --------------------------------------------------------------------------------------
    def u64(self, a): return self.d.u64(a)
    def i32(self, a): return self.d.i32(a)
    def ptr(self, a):
        v = self.d.u64(a)
        return v if self.d.is_ptr(v) else None

    def val(self, a, fmt):
        o = self.d.off(a)
        return None if o is None else struct.unpack_from(fmt, self.d.mm, o)[0]

    # ---- UObject ---------------------------------------------------------------------------------------------
    def fname(self, a):
        idx = self.i32(a)
        if idx is None:
            return None
        num = 0 if self.outline_number else self.i32(a + self.O["FName_Number"])
        return self.pool.name(idx, num or 0)

    def name(self, o):
        return self.fname(o + self.O["UObject_Name"]) if o else None

    def cls(self, o): return self.ptr(o + self.O["UObject_Class"])
    def outer(self, o): return self.ptr(o + self.O["UObject_Outer"])
    def flags(self, o): return self.i32(o + self.O["UObject_Flags"])
    def index(self, o): return self.i32(o + self.O["UObject_Index"])

    def fullname(self, o):
        parts = []
        x = self.outer(o)
        while x and len(parts) < 64:      # bounded: cyclic Outer chains exist in live dumps (Days Gone)
            parts.append(self.name(x) or "?")
            x = self.outer(x)
        c = self.cls(o)
        return f"{self.name(c) if c else '?'} {'.'.join(parts[::-1] + [self.name(o) or '?'])}"

    def cast_flags(self, cls_obj):
        return self.val(cls_obj + self.O["UClass_CastFlags"], "<Q") if "UClass_CastFlags" in self.O else None

    def isa(self, o, flag):
        c = self.cls(o)
        cf = self.cast_flags(c) if c else None
        return cf is not None and (cf & flag) == flag

    def build_index(self):
        for i, o in self.arr:
            n = self.name(o)
            if n is not None:
                self.by_name.setdefault(n.lower(), []).append(o)

    def find(self, name, flag=0):
        for o in self.by_name.get(name.lower(), ()):
            if not flag or self.isa(o, flag):
                return o

    def find_in_outer(self, name, outer_name):
        for o in self.by_name.get(name.lower(), ()):
            x = self.outer(o)
            if x and (self.name(x) or "").lower() == outer_name.lower():
                return o

    def find_full(self, fullname, flag=0):
        for o in self.by_name.get(fullname.split(".")[-1].lower(), ()):
            if (not flag or self.isa(o, flag)) and self.fullname(o).lower() == fullname.lower():
                return o

    # ---- UStruct / fields ------------------------------------------------------------------------------------
    def super(self, s): return self.ptr(s + self.O["UStruct_SuperStruct"])
    def struct_size(self, s): return self.i32(s + self.O["UStruct_Size"])
    def children(self, s): return self.ptr(s + self.O["UStruct_Children"])
    def child_properties(self, s): return self.ptr(s + self.O["UStruct_ChildProperties"])

    # Fields are FField (4.25+) or UField/UProperty objects (older). fields() remembers which; the ffield_*
    # accessors dispatch on that so generators can treat both the same way.
    _ff = {}
    native_members = {}      # class addr -> [(offset, name, "ptr"|"tarray", target class addr)] for unreflected fields (UE3)

    def is_ff(self, f): return self._ff.get(f, self.use_fproperty)

    def ffield_class(self, f): return self.ptr(f + self.O["FField_Class"]) if self.is_ff(f) else self.cls(f)
    def ffield_next(self, f):
        return self.ptr(f + self.O["FField_Next"]) if self.is_ff(f) else self.ptr(f + self.O["UField_Next"])
    def ffield_name(self, f): return self.fname(f + self.O["FField_Name"]) if self.is_ff(f) else self.name(f)

    def ffield_isa(self, f, flag):
        if not self.is_ff(f):
            return self.isa(f, flag)
        c = self.ffield_class(f)
        cf = self.val(c + self.O["FFieldClass_CastFlags"], "<Q") if c else None
        return cf is not None and (cf & flag) == flag

    def ffield_class_name(self, f):
        c = self.ffield_class(f)
        if not c:
            return None
        return self.fname(c) if self.is_ff(f) else self.name(c)     # FFieldClass::Name at 0

    def fields(self, s):
        """Yield (field_addr, is_ffield) for all members of a struct: FField chain and/or UField chain."""
        seen = set()        # cyclic Next chains exist in live dumps (Days Gone open world: generator hung here)
        if self.use_fproperty and "UStruct_ChildProperties" in self.O:
            f = self.child_properties(s)
            while f and f not in seen:
                seen.add(f)
                self._ff[f] = True
                yield f, True
                f = self.ptr(f + self.O["FField_Next"])
        f = self.children(s)
        while f and f not in seen:
            seen.add(f)
            self._ff[f] = False
            yield f, False
            f = self.ptr(f + self.O["UField_Next"])

    def field_isa(self, f, is_ff, flag):
        return self.ffield_isa(f, flag) if is_ff else self.isa(f, flag)

    def field_name(self, f, is_ff):
        return self.ffield_name(f) if is_ff else self.name(f)

    def properties(self, s):
        return [f for f, ff in self.fields(s) if self.field_isa(f, ff, CF["Property"])]

    def member(self, s, name, flag=CF["Property"]):
        for f, ff in self.fields(s):
            if self.field_isa(f, ff, flag) and (self.field_name(f, ff) or "").lower() == name.lower():
                return f

    def bool_info(self, p):
        """FBoolProperty bit layout: (FieldSize, ByteOffset, ByteMask, FieldMask); FieldMask 0xFF = plain bool."""
        b = self.d.read(p + self.O["BoolProperty_Base"], 4) or bytes((1, 0, 255, 255))
        return b[0], b[1], b[2], b[3]

    @property
    def enum_stride(self):
        """UEnum::Names element size: TPair<FName, int64> (FName is 0x10 when case-preserving)."""
        return (0x10 if self.case_preserving else 8) + 8

    def prop_size(self, p): return self.i32(p + self.O["Property_ElementSize"])
    def prop_offset(self, p): return self.i32(p + self.O["Property_Offset_Internal"])

    # ---- offset finder primitives ------------------------------------------------------------------------------
    def find_offset(self, pairs, fmt, lo=0x28, hi=0x1A0, step=4):
        pairs = [(a, v) for a, v in pairs if a]
        if not pairs:
            return NOTFOUND
        best, found, i = lo, False, 0
        while i < len(pairs):
            a, v = pairs[i]
            i += 1
            for j in range(best, hi, step):
                if self.val(a + j, fmt) == v:
                    found = True
                    if j > best:
                        best, i = j, 0
                    break
        return best if found else NOTFOUND

    def valid_ptr_offset(self, a, b, start, hi, in_exe=False):
        if not a or not b:
            return NOTFOUND
        for j in range(start, hi, 8):
            pa, pb = self.ptr(a + j), self.ptr(b + j)
            if pa and pb and (not in_exe or (self.d.base <= pa < self.d.exe_end and self.d.base <= pb < self.d.exe_end)):
                return j
        return NOTFOUND

    # ---- UObject offsets -----------------------------------------------------------------------------------------
    def find_uobject_offsets(self):
        O = self.O
        objs = [self.arr.get(i) for i in range(0x100)]
        objs = [o for o in objs if o]
        # Flags: 0x43 is a very common flag value
        for off in range(4, 0x40, 4):
            if sum(1 for o in objs if self.i32(o + off) == 0x43) > 0xA0:
                O["UObject_Flags"] = off
                break
        # Index
        r = self.find_offset([(self.arr.get(0x55), 0x55), (self.arr.get(0x123), 0x123)], "<i", 8, 0x40)
        O["UObject_Index"] = r if r is not None else O["UObject_Flags"] + 4
        self.arr.index_off = O["UObject_Index"]     # from here on the array iterator drops stale slots
        # Class: pointer chain that ends in a self-reference (Class CoreUObject.Class)
        a, b = self.arr.get(0x55), self.arr.get(0x123)
        for off in range(8, 0x50, 8):
            def cyclic(x):
                for _ in range(16):
                    n = self.ptr(x + off)
                    if not n:
                        return False
                    if n == x:
                        return True
                    x = n
                return False
            if cyclic(a) and cyclic(b):
                O["UObject_Class"] = off
                break
        # Outer: lowest valid pointer offset (not Class) over random object pairs
        rng = random.Random(a)
        lowest = None
        n = min(self.arr.num - 1, 0x3FF)
        for _ in range(16):
            ia, ib = rng.randint(0, n), rng.randint(0, n)
            oa, ob = self.arr.get(ia), self.arr.get(ib if ib != ia else (ia + 1) % (n + 1))
            off = 0
            while off is not None:
                off = self.valid_ptr_offset(oa, ob, off + 8, 0x50)
                if off not in (O["UObject_Class"], O["UObject_Index"]):
                    break
            if off is not None and (lowest is None or off < lowest):
                lowest = off
        if lowest is not None:
            O["UObject_Outer"] = lowest
        # Name: statistics over all objects (average comparison index in a plausible range, few tiny indices)
        cands = [off for off in range(8, 0x44, 4)
                 if off not in (O["UObject_Class"], O["UObject_Class"] + 4, O["UObject_Outer"], O["UObject_Outer"] + 4,
                                O["UObject_Flags"], O["UObject_Index"], 0, 4)]
        stats = {c: [0, 0, True] for c in cands}       # total, low count, in range
        count = 0
        for i, o in self.arr:
            if (o & 0xFFF) > 0x1000 - 0x44:
                continue
            count += 1
            for c in cands:
                v = self.val(o + c, "<I")
                if v is None:
                    continue
                st = stats[c]
                st[0] += v
                st[1] += v <= 0x10
                st[2] = st[2] and v < 0x4000000
        for c in cands:
            tot, low, ok = stats[c]
            avg = tot // max(count, 1)
            if ok and low <= 0x40 and 0x280 <= avg <= 0x2000000:
                O["UObject_Name"] = c
                break
        if O["UObject_Outer"] <= O["UObject_Name"]:
            O["UObject_Outer"] = O["UObject_Name"] + 8
        self.name_before_class = O["UObject_Name"] < O["UObject_Class"]

    def init_fname_settings(self):
        O = self.O
        first = self.arr.get(0)
        na = first + O["UObject_Name"]
        i1, i2 = self.i32(na), self.i32(na + 4)
        size = (O["UObject_Class"] - O["UObject_Name"]) if self.name_before_class else (O["UObject_Outer"] - O["UObject_Name"])
        O["FName_Number"] = 4
        if size == 8 and i1 == i2:
            self.case_preserving = self.outline_number = True
            O["FName_Number"], O["FName_Size"] = -1, 8
        elif size == 0x10:
            self.case_preserving = True
            O["FName_Number"], O["FName_Size"] = (8 if i1 == i2 else 4), 0xC
        else:
            small = sum(1 for i, o in self.arr if 0 < (self.i32(o + O["UObject_Name"] + 4) or 0) < 5)
            if small < self.arr.num * 0.03:
                self.outline_number = True
                O["FName_Number"], O["FName_Size"] = -1, 4
            else:
                O["FName_Number"], O["FName_Size"] = 4, 8

    def post_init_fname_settings(self):
        ps = self.find("PlayerStart", CF["Class"])
        m = self.member(ps, "PlayerStartTag") if ps else None
        if not m:
            return
        size = self.prop_size(m)
        if size == self.O["FName_Size"]:
            return
        self.O["FField_Flags"] += size - self.O["FName_Size"]
        self.O["FName_Size"] = size
        self.outline_number = size == 4
        self.case_preserving = size > 8
        self.O["FName_Number"] = -1 if size == 4 else 4

    # ---- everything else, in Dumper-7 order ----------------------------------------------------------------------
    def init(self):
        O, log = self.O, self.log
        self.find_uobject_offsets()
        self.init_fname_settings()
        self.build_index()
        log(f"UObject: flags {O['UObject_Flags']:#x} index {O['UObject_Index']:#x} class {O['UObject_Class']:#x} "
            f"name {O['UObject_Name']:#x} outer {O['UObject_Outer']:#x}; FName size {O['FName_Size']} "
            f"casepreserving={self.case_preserving} outline={self.outline_number}")
        # block offset bits for the name pool: check high name indices resolve (Dumper-7 PostInit)
        self.fix_block_bits()

        actor, klass = self.find("Actor"), self.find("Class")
        O["UClass_CastFlags"] = self.find_offset([(actor, CF["Actor"]), (klass, CF["Field"] | CF["Struct"] | CF["Class"])], "<Q")
        log(f"UClass::CastFlags {O['UClass_CastFlags']:#x}")

        # Children / property system
        tc = self.find_in_outer("TransformComponent", "Controller")     # a UObjectProperty object => UProperty system
        if tc and self.name(self.cls(tc)) == "ObjectProperty":
            pairs = [(self.find("Vector"), self.find_in_outer("X", "Vector")), (self.find("Vector4"), self.find_in_outer("X", "Vector4")),
                     (self.find("Vector2D"), self.find_in_outer("X", "Vector2D")), (self.find("Guid"), self.find_in_outer("A", "Guid"))]
            O["UStruct_Children"] = self.find_offset(pairs, "<Q", 0x14)
        else:
            self.use_fproperty = True
            pairs = [(self.find("PlayerController"), self.find_in_outer("WasInputKeyJustReleased", "PlayerController")),
                     (self.find("Controller"), self.find_in_outer("UnPossess", "Controller"))]
            O["UStruct_Children"] = self.find_offset(pairs, "<Q")
        log(f"UStruct::Children {O['UStruct_Children']:#x}  FProperty={self.use_fproperty}")

        hi = max(O["UObject_Index"], O["UObject_Name"], O["UObject_Flags"], O["UObject_Outer"], O["UObject_Class"])
        ksl, kstr = self.find("KismetSystemLibrary"), self.find("KismetStringLibrary")
        O["UField_Next"] = self.valid_ptr_offset(self.children(ksl) if ksl else 0, self.children(kstr) if kstr else 0, align(hi + 4, 8), 0x60)
        log(f"UField::Next {O['UField_Next']:#x}")

        st, fi, cl = self.find("Struct") or self.find("struct"), self.find("Field"), self.find("Class")
        O["UStruct_SuperStruct"] = self.find_offset([(st, fi), (cl, st)], "<Q")
        color, guid = self.find("Color", CF["Struct"]), self.find("Guid", CF["Struct"])
        O["UStruct_Size"] = self.find_offset([(color, 4), (guid, 0x10)], "<i")
        O["UStruct_MinAlignment"] = O["UStruct_Size"] + 4        # follows PropertiesSize in every engine version
        log(f"UStruct::SuperStruct {O['UStruct_SuperStruct']:#x} Size {O['UStruct_Size']:#x} MinAlignment {O['UStruct_MinAlignment']:#x}")

        if self.use_fproperty:
            O["UStruct_ChildProperties"] = self.valid_ptr_offset(color, guid, O["UStruct_Children"] + 8, 0x80)
            log(f"UStruct::ChildProperties {O['UStruct_ChildProperties']:#x}")
            self.fixup_hardcoded()
            gch, vch = self.child_properties(guid), self.child_properties(self.find("Vector", CF["Struct"]))
            O["FField_Next"] = self.valid_ptr_offset(gch, vch, O["FField_Owner"] + 8, 0x48)
            O["FField_Class"] = self.valid_ptr_offset(gch, vch, 8, 0x30)
            O["FField_Name"] = self.find_ffield_name(gch, vch)
            O["FField_Flags"] = O["FField_Name"] + O["FName_Size"]
            colch = self.child_properties(color)
            O["FFieldClass_CastFlags"] = self.find_offset(
                [(self.ffield_class(gch), CF["Field"] | CF["Property"] | CF["NumericProperty"] | CF["IntProperty"]),
                 (self.ffield_class(colch), CF["Field"] | CF["Property"] | CF["NumericProperty"] | CF["ByteProperty"])], "<Q", 8, 0x30) or 0x10
            log(f"FField: class {O['FField_Class']:#x} next {O['FField_Next']:#x} name {O['FField_Name']:#x} flags {O['FField_Flags']:#x}; "
                f"FFieldClass::CastFlags {O['FFieldClass_CastFlags']:#x}")

        O["UClass_ClassDefaultObject"] = self.find_offset([(self.find("Object", CF["Class"]), self.find("Default__Object")),
                                                           (self.find("Field", CF["Class"]), self.find("Default__Field"))], "<Q", 0x28, 0x200)
        log(f"UClass::ClassDefaultObject {O['UClass_ClassDefaultObject']:#x}")

        pairs = [(self.find("ENetRole", CF["Enum"]), 5), (self.find("ETraceTypeQuery", CF["Enum"]), 0x22)]
        nv = self.find_offset(pairs, "<i")
        if nv is None:
            pairs = [(self.find("EAlphaBlendOption", CF["Enum"]), 0x10), (self.find("EUpdateRateShiftBucket", CF["Enum"]), 8)]
            nv = self.find_offset(pairs, "<i")
        O["UEnum_Names"] = nv - 8 if nv is not None else None
        self.enum_names_only = False
        if nv is not None:
            arr = self.ptr(pairs[0][0] + O["UEnum_Names"])
            pair_size = (0x10 if self.case_preserving else 8) + 8
            if arr and self.val(arr + pair_size + (0x10 if self.case_preserving else 8), "<q") != 1:
                self.enum_names_only = True
        log(f"UEnum::Names {O['UEnum_Names']:#x} names_only={self.enum_names_only}")

        f1, f2 = self.find("WasInputKeyJustPressed", CF["Function"]), self.find("ToggleSpeaking", CF["Function"])
        f3 = self.find("SwitchLevel", CF["Function"]) or self.find("FOV", CF["Function"])
        fl1 = FF["Final"] | FF["Native"] | FF["Public"] | FF["BlueprintCallable"] | FF["BlueprintPure"] | FF["Const"]
        fl2 = FF["Exec"] | FF["Native"] | FF["Public"]
        r = self.find_offset([(f1, fl1), (f2, fl2), (f3, fl2)], "<I")
        if r is None:
            r = self.find_offset([(f1, fl1 | FF["RequiredAPI"]), (f2, fl2 | FF["RequiredAPI"]), (f3, fl2 | FF["RequiredAPI"])], "<I")
        O["UFunction_FunctionFlags"] = r
        O["UFunction_ExecFunction"] = 0
        for i in range(0x30, 0x140, 8):
            if all(f and self.d.base <= (self.u64(f + i) or 0) < self.d.exe_end for f in (f1, f2, f3)):
                O["UFunction_ExecFunction"] = i
                break
        log(f"UFunction::FunctionFlags {O['UFunction_FunctionFlags']:#x} ExecFunction {O['UFunction_ExecFunction']:#x}")

        ga, gc, gd = (self.member(guid, n) for n in "ACD")
        O["Property_ElementSize"] = es = self.find_offset([(ga, 4), (gc, 4), (gd, 4)], "<i")
        O["Property_ArrayDim"] = self.find_offset([(ga, 1), (gc, 1), (gd, 1)], "<i", es - 0x10, es + 0x10)
        cb, cg = self.member(color, "B"), self.member(color, "G")
        O["Property_Offset_Internal"] = self.find_offset([(cb, 0), (cg, 1), (gc, 8)], "<i")
        gf = PF["Edit"] | PF["ZeroConstructor"] | PF["SaveGame"] | PF["IsPlainOldData"] | PF["NoDestructor"] | PF["HasGetValueTypeHash"]
        cr = self.member(color, "R") or self.member(color, "r")
        r = self.find_offset([(ga, gf), (cr, gf | PF["BlueprintVisible"])], "<Q")
        if r is None:
            p = PF["NativeAccessSpecifierPublic"]
            r = self.find_offset([(ga, gf | p), (cr, gf | PF["BlueprintVisible"] | p)], "<Q")
        O["Property_PropertyFlags"] = r
        log(f"Property: ElementSize {es:#x} ArrayDim {O['Property_ArrayDim']:#x} Offset {O['Property_Offset_Internal']:#x} Flags {O['Property_PropertyFlags']:#x}")

        oi = O["Property_Offset_Internal"]
        eng, pc = self.find("Engine", CF["Class"]), self.find("PlayerController", CF["Class"])
        r = self.find_offset([(self.member(eng, "bIsOverridingSelectedColor"), 0xFF), (self.member(eng, "bEnableOnScreenDebugMessagesDisplay"), 2),
                              (self.member(pc, "bAutoManageActiveCameraTarget"), 0xFF)], "<B", oi, 0x1A0, 1)
        O["BoolProperty_Base"] = r - 3 if r is not None else None
        ac, pawn = self.find("ActorComponent", CF["Class"]), self.find("Pawn", CF["Class"])
        r = self.find_offset([(self.member(ac, "CreationMethod", CF["EnumProperty"]), self.find("EComponentCreationMethod", CF["Enum"])),
                              (self.member(pawn, "AutoPossessAI", CF["EnumProperty"]), self.find("EAutoPossessAI", CF["Enum"]))], "<Q", oi)
        O["EnumProperty_Base"] = r - 8 if r is not None else O["BoolProperty_Base"]
        O["Property_Size"] = ps = O["EnumProperty_Base"]
        log(f"BoolProperty::Base {O['BoolProperty_Base']:#x} EnumProperty::Base {O['EnumProperty_Base']:#x} => sizeof(Property) {ps:#x}")

        ctrl, world = self.find("Controller", CF["Class"]), self.find("World", CF["Class"])
        O["ObjectProperty_Class"] = self.find_offset([(self.member(ctrl, "PlayerState"), self.find("PlayerState", CF["Class"])),
                                                      (self.member(ctrl, "Pawn"), self.find("Pawn", CF["Class"])),
                                                      (self.member(world, "PersistentLevel"), self.find("Level", CF["Class"]))], "<Q", oi) or ps
        crc = self.find("CollisionResponseContainer", CF["Struct"])
        O["ByteProperty_Enum"] = self.find_offset([(self.member(crc, "GameTraceChannel1", CF["ByteProperty"]), self.find("ECollisionResponse", CF["Enum"])),
                                                   (self.member(crc, "GameTraceChannel2", CF["ByteProperty"]), self.find("ECollisionResponse", CF["Enum"]))], "<Q", oi) or ps
        tv, vec = self.find("TwoVectors", CF["Struct"]), self.find("Vector", CF["Struct"])
        O["StructProperty_Struct"] = self.find_offset([(self.member(tv, "v1", CF["StructProperty"]), vec), (self.member(tv, "v2", CF["StructProperty"]), vec)], "<Q", oi) or ps
        sig = self.find("TimerDynamicDelegate__DelegateSignature", CF["Function"])
        d1, d2 = self.find("K2_GetTimerElapsedTimeDelegate", CF["Function"]), self.find("K2_GetTimerRemainingTimeDelegate", CF["Function"])
        O["DelegateProperty_SignatureFunction"] = self.find_offset([(self.member(d1, "Delegate", CF["DelegateProperty"]) if d1 else None, sig),
                                                                    (self.member(d2, "Delegate", CF["DelegateProperty"]) if d2 else None, sig)], "<Q", oi) or ps
        O["ArrayProperty_Inner"] = O["SetProperty_ElementProp"] = O["MapProperty_Base"] = ps
        if self.use_fproperty:
            for key, cname, mname, flag in (("ArrayProperty_Inner", "GameViewportClient", "DebugProperties", CF["ArrayProperty"]),
                                            ("SetProperty_ElementProp", "LevelCollection", "Levels", CF["SetProperty"]),
                                            ("MapProperty_Base", "UserDefinedEnum", "DisplayNameMap", CF["MapProperty"])):
                c = self.find(cname)
                m = self.member(c, mname, flag) if c else None
                if m and not self.ptr(m + ps):
                    O[key] = ps + 8
        O["ClassProperty_MetaClass"] = O["ObjectProperty_Class"] + 8
        O["FieldPathProperty_FieldClass"] = O["OptionalProperty_ValueProperty"] = ps
        log(f"ObjectProperty::Class {O['ObjectProperty_Class']:#x} StructProperty::Struct {O['StructProperty_Struct']:#x} "
            f"ArrayProperty::Inner {O['ArrayProperty_Inner']:#x} MapProperty::Base {O['MapProperty_Base']:#x}")
        self.post_init_fname_settings()
        O["ULevel_Actors"] = self.find_level_actors()
        log(f"ULevel::Actors {O['ULevel_Actors']}")
        return O

    def fix_block_bits(self):
        """FNamePool block offset bits: usually 16; if object names have block indices beyond the pool, raise."""
        if not isinstance(self.pool, NamePool):
            return
        self.pool.block_bits = 14
        i = self.arr.num - 1
        while i >= 0:
            o = self.arr.get(i)
            i -= 1
            if not o:
                continue
            blk = (self.i32(o + self.O["UObject_Name"]) or 0) >> self.pool.block_bits
            if blk == self.pool.current_block:
                break
            if blk > self.pool.current_block:
                self.pool.block_bits += 1
                i = self.arr.num - 1
        self.by_name = {}
        self.build_index()

    def fixup_hardcoded(self):
        O = self.O
        if self.case_preserving:
            O["FField_Flags"] += 8
            O["FFieldClass_CastFlags"] += 8
        # FFieldVariant lost its bool in 5.1.1: then Owner+8 is already the Next pointer
        ok = 0
        for n in ("Actor", "ActorComponent", "Pawn"):
            c = self.find(n, CF["Class"])
            cp = self.child_properties(c) if c else None
            v = self.u64(cp + O["FField_Owner"] + 8) if cp else None
            ok += bool(v and self.d.is_ptr(v) and not (v & 1))
        if ok == 3:
            self.log("FFieldVariant without bool (UE 5.1.1+): shifting FField offsets by -8")
            O["FField_Next"] -= 8
            O["FField_Name"] -= 8
            O["FField_Flags"] -= 8

    def find_ffield_name(self, gch, vch):
        def ok(off):
            self.O["FField_Name"] = off
            g, v = (self.ffield_name(gch) or "").upper(), (self.ffield_name(vch) or "").upper()
            return g in ("A", "D") and v in ("X", "Z")
        if ok(self.O["FField_Name"]):
            return self.O["FField_Name"]
        for off in range(self.O["FField_Owner"], 0x40, 4):
            if ok(off):
                return off
        return NOTFOUND

    def find_level_actors(self):
        lvl = None
        for i, o in self.arr:
            if not (self.flags(o) or 0) & RF_ClassDefaultObject and self.isa(o, CF["Level"]):
                lvl = o
                break
        uobj = self.find("Object", CF["Class"]) or self.find("object", CF["Class"])
        url = self.find("URL", CF["Struct"])
        lc = self.cls(lvl) if lvl else None
        ow = self.member(lc, "OwningWorld") if lc else None
        if not (lvl and uobj and url and ow):
            return None
        for i in range(self.struct_size(uobj) + self.struct_size(url), self.prop_offset(ow) - 0x10 + 1, 8):
            data, num, mx = self.u64(lvl + i), self.i32(lvl + i + 8), self.i32(lvl + i + 12)
            if self.d.is_ptr(data) and 0 <= num <= mx and mx < 0x100000:
                return i
        return None


def run(coro):
    """asyncio.run that exits cleanly on sys.exit() inside the coroutine (ps4debug's socket pool would otherwise
    complain about a closed event loop at interpreter shutdown and bury the real message)."""
    import asyncio
    try:
        asyncio.run(coro)
    except SystemExit as x:
        if x.code not in (None, 0):
            print(x.code, file=sys.stderr)
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(1 if x.code else 0)


async def game_pid(dbg, required=True):
    """PID of the running game: eboot.bin, else the single process whose title id starts with CUSA
    (Mass Effect LE runs as LTCG-BioGame.elf). System processes carry unparsable info blocks -> skipped."""
    procs = await dbg.get_processes()
    c = [p.pid for p in procs if p.name.rstrip("\0") == "eboot.bin"]
    if not c:
        for p in procs:
            if p.pid > 100:
                try:
                    if (await dbg.get_process_info(p.pid)).title_id.startswith("CUSA"):
                        c.append(p.pid)
                except Exception:
                    pass
    if len(c) == 1:
        return c[0]
    if not required:
        return None
    sys.exit(f"expected one game process, found {len(c)}: {[(p.pid, p.name.rstrip(chr(0))) for p in procs if p.pid > 100]}")


def open_engine(path, log=print):
    """Engine on a dump. The finder results (which engine, where the name/object tables are) are cached next to
    the dump in <dump>.engine.json, so the second open skips the whole-file scans (~30 s on a 2 GB dump)."""
    d = Dump(path)
    cache, meta = path + ".engine.json", None
    try:
        if os.path.getmtime(cache) >= os.path.getmtime(path):
            meta = json.load(open(cache))
    except (OSError, ValueError):
        meta = None
    if meta and meta.get("kind") == "ue3" or not meta and not (NamePool.find(d) or NameArray.find(d)):
        import ue3find
        e = ue3find.open_engine(d, log, meta)
        if not e:
            sys.exit("neither FNamePool (UE 4.23+), TNameEntryArray (UE4 < 4.23) nor a UE3 name table found")
        json.dump(e.cache_meta, open(cache, "w"))
        return e
    if meta:
        pool = (NamePool(d, meta["pool"], meta["stride"], meta["block_bits"]) if meta["pool_kind"] == "NamePool"
                else NameArray(d, meta["pool"], meta["str_off"]))
        arr = (ObjectArray(d, meta["arr"], tuple(meta["layout"])) if meta.get("layout") is not None
               else FlatObjectArray(d, meta["arr"]))
    else:
        pool = NamePool.find(d) or NameArray.find(d)
        arr = None
        for finder in (ObjectArray.find, FlatObjectArray.find):     # chunked (4.20+) first, then flat (4.8-4.19)
            for lo, hi in data_ranges(d):
                arr = finder(d, lo, hi)
                if arr:
                    break
            if arr:
                break
        if not arr:
            sys.exit("GObjects not found")
    log(f"names: {type(pool).__name__}")
    log(f"GNames {pool.addr:#x} (base+{pool.addr - d.base:#x})  GObjects {arr.addr:#x} (base+{arr.addr - d.base:#x})  "
        f"objects {arr.num}  item size {arr.item_size}")
    e = Engine(d, pool, arr, log)
    e.init()
    json.dump(dict(kind="ue4", pool_kind=type(pool).__name__, pool=pool.addr, stride=getattr(pool, "stride", None),
                   block_bits=pool.block_bits, str_off=getattr(pool, "str_off", None), arr=arr.addr, layout=arr.layout and list(arr.layout)),
              open(cache, "w"))
    return e


if __name__ == "__main__":
    path = sys.argv[1]
    e = open_engine(path)
    out = path[:-4] + "_out"
    os.makedirs(out, exist_ok=True)
    meta = dict(base=e.d.base, gnames=e.pool.addr, gobjects=e.arr.addr, gobjects_layout=e.arr.layout,
                fuobjectitem_size=e.arr.item_size, name_block_bits=e.pool.block_bits, use_fproperty=e.use_fproperty,
                case_preserving=e.case_preserving, outline_number=e.outline_number, offsets=e.O)
    json.dump(meta, open(os.path.join(out, "offsets.json"), "w"), indent=1)
    with open(os.path.join(out, "GObjects.txt"), "w", encoding="utf-8") as f:
        for i, o in e.arr:
            f.write(f"[{i:08X}] {{{o:#x}}} {e.fullname(o)}\n")
    print("wrote", out)
