"""Generate a CodeRed-style C++ SDK (per package: _structs.hpp, _classes.hpp, _parameters.hpp) from a dump.

    python uegen.py ../dumps/lollipop.bin [outdir]
"""
import json, math, os, re, sys
from collections import defaultdict
import ueobj
from ueobj import CF, RF_ClassDefaultObject

KEYWORDS = {"class", "struct", "enum", "int", "float", "double", "bool", "char", "short", "long", "void", "new", "delete",
            "default", "template", "this", "operator", "namespace", "union", "public", "private", "protected", "auto",
            "register", "switch", "case", "break", "continue", "return", "if", "else", "for", "while", "do", "const",
            "static", "virtual", "inline", "true", "false", "nullptr", "typedef", "typename", "using", "signed",
            "unsigned", "volatile", "export", "extern", "friend", "goto", "mutable", "sizeof", "throw", "try", "catch"}
NUMERIC = dict(Int8Property="int8_t", Int16Property="int16_t", IntProperty="int32_t", Int64Property="int64_t",
               UInt16Property="uint16_t", UInt32Property="uint32_t", UInt64Property="uint64_t",
               FloatProperty="float", DoubleProperty="double", NameProperty="struct FName", StrProperty="class FString",
               TextProperty="class FText", DelegateProperty="struct FScriptDelegate",
               MulticastInlineDelegateProperty="struct FMulticastScriptDelegate",
               MulticastSparseDelegateProperty="struct FSparseDelegate", MulticastDelegateProperty="struct FMulticastScriptDelegate",
               FieldPathProperty="struct TFieldPath", LargeWorldCoordinatesRealProperty="double",
               Utf8StrProperty="class FUtf8String", AnsiStrProperty="class FAnsiString")


def ident(s):
    s = re.sub(r"[^0-9A-Za-z_]", "_", s or "None")
    if s[0].isdigit():
        s = "_" + s
    return s + "_" if s in KEYWORDS else s


class Gen:
    def __init__(self, e):
        self.e, self.O = e, e.O
        self.prefix = {}         # struct/class addr -> C++ name
        self.pkg_of = {}
        self.enums, self.structs, self.classes, self.funcs = {}, {}, {}, defaultdict(list)
        self.enum_type = {}      # enum addr -> underlying c type
        self.taken = {}          # C++ name -> addr, to keep generated names unique

    # ---- classification -------------------------------------------------------------------------------------------
    def collect(self):
        e = self.e
        for i, o in e.arr:
            # Every UStruct/UEnum/UClass has an Outer (package or class) and a resolvable UClass; slots that pass the
            # index check but fail this are dead objects (Days Gone: 144, some with Size 1.8 GB -> uerc ate 70 GB RAM).
            if not e.outer(o) or e.name(e.cls(o)) in (None, "None"):
                continue
            if e.isa(o, CF["Enum"]):
                self.enums[o] = 1
            elif e.isa(o, CF["Class"]):
                self.classes[o] = 1
            elif e.isa(o, CF["Function"]):
                self.funcs[e.outer(o)].append(o)
            elif e.isa(o, CF["Struct"]):
                self.structs[o] = 1
        for o in list(self.enums) + list(self.structs) + list(self.classes):
            self.pkg_of[o] = self.package(o)

    def package(self, o):
        x = o
        for _ in range(64):          # bounded: a Days Gone open-world dump had a cyclic Outer chain (measured hang)
            nxt = self.e.outer(x)
            if not nxt:
                break
            x = nxt
        return x

    def pkg_name(self, p):
        n = self.e.name(p) or "None"
        return ident(n.rsplit("/", 1)[-1])

    def is_actor(self, c):
        while c:
            if self.e.name(c) == "Actor" and self.e.name(self.package(c)) in ("/Script/Engine", "Engine"):
                return True
            c = self.e.super(c)
        return False

    def cname(self, s):
        """C++ name of a UStruct/UClass/UEnum object, with prefix."""
        if s in self.prefix:
            return self.prefix[s]
        n = ident(self.e.name(s))
        if s in self.enums:
            r = n if n.startswith("E") else "E" + n
        elif s in self.classes:
            r = ("A" if self.is_actor(s) else "U") + n
        else:
            r = "F" + n
        if self.taken.get(r, s) != s:                      # same name elsewhere (UE3 per-class structs, per-level blueprints)
            outer = self.e.outer(s)
            tag = self.pkg_name(self.package(s)) if s in self.classes or outer == self.package(s) else ident(self.e.name(outer))
            r2, i = f"{r}_{tag}", 1
            while self.taken.get(r2, s) != s:
                i += 1
                r2 = f"{r}_{tag}_{i}"
            r = r2
        self.taken[r] = s
        self.prefix[s] = r
        return r

    # ---- property typing -------------------------------------------------------------------------------------------
    def ptype(self, p):
        """(c type string, dependency struct addr or None)"""
        e, O = self.e, self.O
        k = e.ffield_class_name(p)
        if k in NUMERIC:
            return NUMERIC[k], None
        if k == "ByteProperty":
            en = e.ptr(p + O["ByteProperty_Enum"])
            if en and en in self.enums:
                self.enum_type.setdefault(en, "uint8_t")
                return self.cname(en), en
            return "uint8_t", None
        if k == "BoolProperty":
            return "bool", None
        if k == "EnumProperty":
            under = e.ptr(p + O["EnumProperty_Base"])
            en = e.ptr(p + O["EnumProperty_Base"] + 8)
            size = e.prop_size(under) if under else e.prop_size(p)
            if en and en in self.enums:
                self.enum_type[en] = {1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t"}.get(size, "uint8_t")
                return self.cname(en), en
            return {1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t"}.get(size, "uint8_t"), None
        if k in ("ObjectProperty", "ObjectPtrProperty", "ComponentProperty"):
            c = e.ptr(p + O["ObjectProperty_Class"])
            return (f"class {self.cname(c)}*" if c in self.classes else "class UObject*"), None
        if k == "ClassProperty" or k == "ClassPtrProperty":
            c = e.ptr(p + O["ClassProperty_MetaClass"])
            return "class UClass*", None
        if k == "WeakObjectProperty":
            c = e.ptr(p + O["ObjectProperty_Class"])
            return f"TWeakObjectPtr<class {self.cname(c) if c in self.classes else 'UObject'}>", None
        if k == "LazyObjectProperty":
            c = e.ptr(p + O["ObjectProperty_Class"])
            return f"TLazyObjectPtr<class {self.cname(c) if c in self.classes else 'UObject'}>", None
        if k == "SoftObjectProperty":
            c = e.ptr(p + O["ObjectProperty_Class"])
            return f"TSoftObjectPtr<class {self.cname(c) if c in self.classes else 'UObject'}>", None
        if k == "SoftClassProperty":
            return "TSoftClassPtr<class UObject>", None
        if k == "InterfaceProperty":
            c = e.ptr(p + O["ObjectProperty_Class"])
            return f"TScriptInterface<class {self.cname(c) if c in self.classes else 'UObject'}>", None
        if k == "StructProperty":
            s = e.ptr(p + O["StructProperty_Struct"])
            return (f"struct {self.cname(s)}", s) if s in self.structs else ("uint8_t", None)
        if k == "ArrayProperty":
            inner = e.ptr(p + O["ArrayProperty_Inner"])
            t, _ = self.ptype(inner) if inner else ("uint8_t", None)
            return f"TArray<{t}>", None
        if k == "SetProperty":
            inner = e.ptr(p + O["SetProperty_ElementProp"])
            t, _ = self.ptype(inner) if inner else ("uint8_t", None)
            return f"TSet<{t}>", None
        if k == "MapProperty":
            kp, vp = e.ptr(p + O["MapProperty_Base"]), e.ptr(p + O["MapProperty_Base"] + 8)
            kt, _ = self.ptype(kp) if kp else ("uint8_t", None)
            vt, _ = self.ptype(vp) if vp else ("uint8_t", None)
            return f"TMap<{kt}, {vt}>", None
        if k == "OptionalProperty":
            inner = e.ptr(p + O["OptionalProperty_ValueProperty"])
            t, dep = self.ptype(inner) if inner else ("uint8_t", None)
            return f"TOptional<{t}>", dep
        return f"uint8_t /* {k} */", None

    def bool_info(self, p):
        return self.e.bool_info(p)

    # ---- struct/class body ----------------------------------------------------------------------------------------
    def members(self, s, start):
        """Emit member lines from `start` to struct size, with padding. Returns (lines, deps)."""
        e, O = self.e, self.O
        lines, deps, pad = [], [], 0
        size = e.struct_size(s) or start
        props = e.properties(s)
        props.sort(key=lambda p: (e.prop_offset(p), self.bool_info(p)[2] if e.ffield_class_name(p) == "BoolProperty" else 0))
        pos, bitpos, seen = start, 0, defaultdict(int)
        for p in props:
            off, n, dim = e.prop_offset(p), ident(e.ffield_name(p)), e.i32(p + O["Property_ArrayDim"]) or 1
            if off is None or off < pos and not (off == pos - 1 and bitpos):
                continue
            seen[n] += 1
            if seen[n] > 1:
                n = f"{n}_{seen[n] - 1}"
            flags = e.val(p + O["Property_PropertyFlags"], "<Q") or 0
            k = e.ffield_class_name(p)
            if k == "BoolProperty" and self.bool_info(p)[3] != 0xFF:
                fs, bo, bm, fm = self.bool_info(p)
                bit = int(math.log2(bm)) if bm else 0
                if off > pos or (off == pos and bitpos == 0 and off != pos - 1):
                    if bitpos:
                        pos += 1; bitpos = 0
                    if off > pos:
                        lines.append(f"\tuint8_t                                            Pad_{pad:04X}[{off - pos:#x}];{'':<16}// {pos:#06x} ({off - pos:#06x}) MISSED OFFSET"); pad += 1
                        pos = off
                elif off == pos - 1 and bitpos:
                    pass
                if off == pos and bitpos == 0:
                    pos += 1
                if bit > bitpos:
                    lines.append(f"\tuint8_t                                            BitPad_{pad:04X} : {bit - bitpos};{'':<24}// {off:#06x}"); pad += 1
                    bitpos = bit
                lines.append(f"\tuint8_t                                            {n} : 1;{'':<34}// {off:#06x} (0x0001) [{flags:#018x}] [{fm:#04x}]")
                bitpos = bit + 1
                if bitpos >= 8:
                    bitpos = 0
                continue
            if bitpos:
                bitpos = 0
            if off > pos:
                lines.append(f"\tuint8_t                                            Pad_{pad:04X}[{off - pos:#x}];{'':<16}// {pos:#06x} ({off - pos:#06x}) MISSED OFFSET"); pad += 1
            pos = off
            t, dep = self.ptype(p)
            if dep and dep != s:
                deps.append(dep)
            esz = e.prop_size(p) or 0
            arr = f"[{dim:#x}]" if dim > 1 else ""
            lines.append(f"\t{t:<50} {n}{arr};{'':<{max(1, 30 - len(n) - len(arr))}}// {off:#06x} ({esz * dim:#06x}) [{flags:#018x}] {k}")
            pos = off + esz * dim
        if bitpos:
            pos += 1
        if size > pos:
            lines.append(f"\tuint8_t                                            Pad_{pad:04X}[{size - pos:#x}];{'':<16}// {pos:#06x} ({size - pos:#06x}) MISSED OFFSET")
        return lines, deps

    def enum_ctype(self, en):
        vals = [v for _, v in self.enum_items(en)]
        t = self.enum_type.get(en)
        if t is None and vals and (max(vals) > 255 or min(vals) < 0):
            t = "int32_t" if min(vals) < 0 else "uint32_t"
        return t or "uint8_t"

    def enum_items(self, en):
        e, O = self.e, self.O
        arr, num = e.u64(en + O["UEnum_Names"]), e.i32(en + O["UEnum_Names"] + 8) or 0
        pair, out = e.enum_stride, []
        for i in range(min(num, 4096)):
            n = e.fname(arr + i * pair) or f"Value_{i}"
            v = e.val(arr + i * pair + (0x10 if e.case_preserving else 8), "<q") if not e.enum_names_only else i
            out.append((n, v))
        return out

    def enum_lines(self, en):
        e, O = self.e, self.O
        arr, num = e.u64(en + O["UEnum_Names"]), e.i32(en + O["UEnum_Names"] + 8) or 0
        pair = e.enum_stride
        out, seen = [], defaultdict(int)
        for i in range(min(num, 4096)):
            n = e.fname(arr + i * pair) or f"Value_{i}"
            v = e.val(arr + i * pair + (0x10 if e.case_preserving else 8), "<q") if not e.enum_names_only else i
            n = ident(n.split("::")[-1])
            seen[n] += 1
            if seen[n] > 1:
                n = f"{n}_{seen[n] - 1}"
            out.append(f"\t{n:<50} = {v}")
        return out

    # ---- ordering --------------------------------------------------------------------------------------------------
    def topo(self, items, deps):
        """Order items so that dependencies come first (cycles broken in encounter order)."""
        out, state = [], {}
        def visit(x):
            if state.get(x) == 2:
                return
            if state.get(x) == 1:
                return          # cycle
            state[x] = 1
            for d in deps.get(x, ()):
                if d in deps:
                    visit(d)
            state[x] = 2
            out.append(x)
        for x in items:
            visit(x)
        return out

    # ---- emit -------------------------------------------------------------------------------------------------------
    def run(self, outdir, title):
        e, O = self.e, self.O
        self.collect()
        sdk = os.path.join(outdir, "SDK_HEADERS")
        os.makedirs(sdk, exist_ok=True)
        by_pkg = defaultdict(lambda: dict(enums=[], structs=[], classes=[]))
        for o in self.enums: by_pkg[self.pkg_of[o]]["enums"].append(o)
        for o in self.structs: by_pkg[self.pkg_of[o]]["structs"].append(o)
        for o in self.classes: by_pkg[self.pkg_of[o]]["classes"].append(o)

        # bodies first (collect deps), then order
        body, deps = {}, {}
        for s in list(self.structs) + list(self.classes):
            sup = e.super(s)
            start = e.struct_size(sup) if sup and (sup in self.structs or sup in self.classes) else 0
            lines, d = self.members(s, start)
            body[s] = (lines, sup, start)
            deps[s] = ([sup] if sup and (sup in self.structs or sup in self.classes) else []) + d
        struct_pkg_deps, class_pkg_deps = defaultdict(set), defaultdict(set)
        for s, dl in deps.items():
            target = class_pkg_deps if s in self.classes else struct_pkg_deps
            for d in dl:
                if self.pkg_of.get(d) != self.pkg_of[s]:
                    target[self.pkg_of[s]].add(self.pkg_of[d])
        pkgs = list(by_pkg)
        struct_order = self.topo(pkgs, {p: struct_pkg_deps.get(p, set()) for p in by_pkg})
        class_order = self.topo(pkgs, {p: class_pkg_deps.get(p, set()) for p in by_pkg})

        used_names, includes, file_of = defaultdict(int), [], {}
        head = lambda f: (f"/*\n# {title} SDK\n# Generated by PS4Unreal uegen (CodeRed-compatible layout)\n# File: {f}\n*/\n#pragma once\n\n"
                          "#ifdef _MSC_VER\n\t#pragma pack(push, 0x8)\n#endif\n\n")
        tail = "\n#ifdef _MSC_VER\n\t#pragma pack(pop)\n#endif\n"
        for p in pkgs:
            pn = self.pkg_name(p)
            used_names[pn] += 1
            if used_names[pn] > 1:
                pn = f"{pn}_{used_names[pn] - 1}"
            file_of[p] = pn
            g = by_pkg[p]
            # structs file: enums + structs
            with open(os.path.join(sdk, f"{pn}_structs.hpp"), "w", encoding="utf-8") as f:
                f.write(head(f"{pn}_structs.hpp"))
                f.write("// Enums\n\n")
                for en in g["enums"]:
                    f.write(f"// Enum {e.fullname(en).split(' ', 1)[1]}\nenum class {self.cname(en)} : {self.enum_ctype(en)}\n{{\n")
                    f.write(",\n".join(self.enum_lines(en)) + "\n};\n\n")
                f.write("// Structs\n\n")
                for s in self.topo(g["structs"], {s: [d for d in deps[s] if self.pkg_of.get(d) == p] for s in g["structs"]}):
                    lines, sup, start = body[s]
                    base = f" : public {self.cname(sup)}" if start else ""
                    f.write(f"// ScriptStruct {e.fullname(s).split(' ', 1)[1]}\n// {e.struct_size(s) - start:#06x} ({start:#06x} - {e.struct_size(s):#06x})\n"
                            f"struct {self.cname(s)}{base}\n{{\n" + "\n".join(lines) + "\n};\n\n")
                f.write(tail)
            # classes file
            with open(os.path.join(sdk, f"{pn}_classes.hpp"), "w", encoding="utf-8") as f:
                f.write(head(f"{pn}_classes.hpp"))
                for c in self.topo(g["classes"], {c: [d for d in deps[c] if self.pkg_of.get(d) == p] for c in g["classes"]}):
                    lines, sup, start = body[c]
                    base = f" : public {self.cname(sup)}" if sup and sup in self.classes else ""
                    cf = e.cast_flags(c) or 0
                    f.write(f"// Class {e.fullname(c).split(' ', 1)[1]}\n// {e.struct_size(c) - start:#06x} ({start:#06x} - {e.struct_size(c):#06x}) CastFlags {cf:#x}\n"
                            f"class {self.cname(c)}{base}\n{{\npublic:\n" + "\n".join(lines) + "\n")
                    if self.funcs.get(c):
                        f.write("\npublic:\n")
                        for fn in self.funcs[c]:
                            fl = e.val(fn + O["UFunction_FunctionFlags"], "<I") or 0
                            ex = (e.u64(fn + O["UFunction_ExecFunction"]) or 0) if O.get("UFunction_ExecFunction") is not None else 0
                            params = [(pp, e.val(pp + O["Property_PropertyFlags"], "<Q") or 0) for pp in e.properties(fn)]
                            ret = next((self.ptype(pp)[0] for pp, fl2 in params if fl2 & 0x400), "void")
                            args = ", ".join(f"{self.ptype(pp)[0]}{'*' if fl2 & 0x100 and not fl2 & 0x400 else ''} {ident(e.ffield_name(pp))}"
                                             for pp, fl2 in params if not fl2 & 0x400)
                            f.write(f"\t// {ret} {ident(e.name(fn))}({args});  // flags {fl:#010x}  native {ex - e.d.base:#x}\n" if ex else
                                    f"\t// {ret} {ident(e.name(fn))}({args});  // flags {fl:#010x}\n")
                    f.write("};\n\n")
                f.write(tail)
            # parameters file
            with open(os.path.join(sdk, f"{pn}_parameters.hpp"), "w", encoding="utf-8") as f:
                f.write(head(f"{pn}_parameters.hpp"))
                for c in g["classes"]:
                    for fn in self.funcs.get(c, ()):
                        lines, _ = self.members(fn, 0)
                        if not lines:
                            continue
                        f.write(f"// Function {e.fullname(fn).split(' ', 1)[1]}\n// {e.struct_size(fn):#06x}\nstruct {self.cname(c)}_{ident(e.name(fn))}_Params\n{{\n"
                                + "\n".join(lines) + "\n};\n\n")
                f.write(tail)
        includes = ([f'#include "SDK_HEADERS/{file_of[p]}_structs.hpp"' for p in struct_order]
                    + [f'#include "SDK_HEADERS/{file_of[p]}_classes.hpp"' for p in class_order]
                    + [f'#include "SDK_HEADERS/{file_of[p]}_parameters.hpp"' for p in pkgs])
        with open(os.path.join(outdir, "SdkHeaders.hpp"), "w", encoding="utf-8") as f:
            f.write(f"/*\n# {title} SDK\n*/\n#pragma once\n#include \"GameDefines.hpp\"\n\n" + "\n".join(includes) + "\n")
        print(f"{len(pkgs)} packages, {len(self.enums)} enums, {len(self.structs)} structs, {len(self.classes)} classes")


def find_process_event(e):
    """UObject::ProcessEvent vtable index: the virtual that tests UFunction::FunctionFlags & FUNC_Native (0x400).
    Clang emits `test byte [reg+Flags+1], 4`, MSVC `test dword [reg+Flags], 0x400`; both accepted. Each entry is
    scanned only up to the next vtable function so a hit inside a neighbour is not credited to a short thunk.
    Returns (index, address) or (None, None)."""
    import struct
    ff = e.O["UFunction_FunctionFlags"]
    d32 = re.escape(struct.pack("<I", ff))
    mem = rb"(?:[\x80-\x87]|\x84\x24)"      # [reg+disp32]; r12/rsp need the SIB byte (measured: Days Gone uses r12)
    pat = re.compile(rb"(?:\x41)?\xF7" + mem + d32 + rb"\x00\x04\x00\x00"
                     rb"|(?:\x41)?\xF6" + mem + re.escape(struct.pack("<I", ff + 1)) + rb"\x04"
                     # UE3: movzx eax, word [reg+Flags]; test ax, FUNC_Native|FUNC_Defined (0x402)
                     rb"|(?:\x41)?\x0F\xB7" + mem + d32 + rb".{0,12}?(?:\x66\xA9|\xA9)\x02\x04"
                     rb"|(?:\x41)?\xF7" + mem + d32 + rb"\x02\x04\x00\x00", re.S)
    uobj = e.find("Object", CF["Class"]) or e.find("object", CF["Class"])
    vt = e.u64(e.ptr(uobj + e.O["UClass_ClassDefaultObject"])) if uobj else 0
    fns = []
    while vt:
        fn = e.u64(vt + len(fns) * 8)
        if not (e.d.base <= fn < e.d.exe_end):
            break
        fns.append(fn)
    ends = sorted(fns)
    hits = []
    for i, fn in enumerate(fns):
        nxt = next((a for a in ends if a > fn), fn + 0x1000)
        if pat.search(e.d.read(fn, min(nxt - fn, 0x1000)) or b""):
            hits.append((i, fn))
    return hits[0] if len(hits) == 1 else (None, None)


def find_gworld(e, log=print):
    """UWorld** GWorld: pointers in the executable's data segment to a non-CDO UWorld instance.
    Several globals can point at the world (GWorld, GActiveLogWorld, ...); if the console that produced the
    dump is reachable, poll them and keep the one that stays constant. Returns (chosen, all_candidates)."""
    import struct as st
    world, pc = e.find("World", CF["Class"]), e.find("PlayerController", CF["Class"])
    def is_pc(c):
        while c:
            if c == pc:
                return True
            c = e.super(c)
    def owner_world(o):
        while o:
            if e.cls(o) == world:
                return o
            o = e.outer(o)
    # UE3 streams one UWorld per level package; the game world is the one that owns the PlayerController
    game = next((owner_world(o) for i, o in e.arr if pc and not (e.flags(o) or 0) & RF_ClassDefaultObject and is_pc(e.cls(o))), None)
    if game:
        log(f"game world: {e.fullname(game)}")
    cands = []
    for i, o in e.arr:
        if e.cls(o) == world and not (e.flags(o) or 0) & RF_ClassDefaultObject and (not game or o == game):
            key = st.pack("<Q", o)
            for lo, hi in ueobj.data_ranges(e.d):
                r = e.d.region(lo)
                seg = e.d.mm[r["offset"]:r["offset"] + hi - lo]
                p = seg.find(key)
                while p != -1:
                    if p % 8 == 0:
                        cands.append(lo + p)
                    p = seg.find(key, p + 1)
    cands = sorted(set(cands))
    if len(cands) > 1 and e.d.idx.get("host"):
        stable = poll_stable(e.d.idx["host"], cands, log)
        if stable:
            return stable, cands
    return (cands[-1] if cands else None), cands


def poll_stable(host, addrs, log):
    """Read each address ~40 times over a second; return the one whose value never changes (and is non-zero)."""
    import asyncio, struct as st
    try:
        import ps4debug
    except ImportError:
        return None

    async def go():
        dbg = ps4debug.PS4Debug(host)
        pid = await asyncio.wait_for(ueobj.game_pid(dbg, required=False), 5)
        if pid is None:
            return None
        seen = {a: set() for a in addrs}
        for _ in range(40):
            for a in addrs:
                raw = await dbg.read_memory(pid, a, 8)
                seen[a].add(st.unpack("<Q", raw)[0] if raw else 0)
            await asyncio.sleep(0.025)
        good = [a for a in addrs if len(seen[a]) == 1 and 0 not in seen[a]]
        log("GWorld candidates polled live: " + ", ".join(f"{a:#x}={'stable' if a in good else 'flickers'}" for a in addrs))
        return good[0] if len(good) == 1 else None
    try:
        return asyncio.run(go())
    except Exception as ex:
        log(f"live GWorld check skipped ({ex.__class__.__name__})")
        return None


if __name__ == "__main__":
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else path[:-4] + "_out"
    e = ueobj.open_engine(path)
    gw, gw_all = find_gworld(e)
    print(f"GWorld {gw:#x} (base+{gw - e.d.base:#x}), candidates {[hex(a - e.d.base) for a in gw_all]}" if gw else "GWorld not found")
    pe_idx, pe_addr = find_process_event(e)
    print(f"ProcessEvent vtable index {pe_idx} at base+{pe_addr - e.d.base:#x}" if pe_idx is not None else "ProcessEvent not found")
    title = e.d.idx.get("title_id", "game")
    g = Gen(e)
    g.run(out, title)
    # FNamePool is itself the global; TNameEntryArray lives on the heap and a global pointer (GNames) refers to it
    gnames = e.pool.addr
    if not (e.d.base <= gnames < e.d.exe_end):
        import struct as st
        for lo, hi in ueobj.data_ranges(e.d):
            r = e.d.region(lo)
            p = e.d.mm[r["offset"]:r["offset"] + hi - lo].find(st.pack("<Q", gnames))
            if p != -1:
                gnames = lo + p
                break
    meta = dict(title=title, base=e.d.base, gnames=gnames - e.d.base, gobjects=e.arr.addr - e.d.base,
                gworld=(gw - e.d.base) if gw else None, gobjects_layout=e.arr.layout, fuobjectitem_size=e.arr.item_size,
                name_block_bits=e.pool.block_bits, use_fproperty=e.use_fproperty, case_preserving=e.case_preserving,
                outline_number=e.outline_number, process_event_index=pe_idx,
                process_event=(pe_addr - e.d.base) if pe_idx is not None else None, offsets=e.O)
    with open(os.path.join(out, "GameDefines.hpp"), "w") as f:
        f.write(f"// {title}: engine globals and reflection offsets (relative to eboot base {e.d.base:#x})\n#pragma once\n#include <cstdint>\n\n")
        kind = {"NamePool": "FNamePool (struct itself)", "PoolNames": "Mass Effect LE pool base table (8 pointers)"}.get(
            type(e.pool).__name__, "TNameEntryArray* (pointer to the chunk table)")
        f.write(f"#define GNames_Offset    0x{meta['gnames']:X}   // {kind}\n#define GObjects_Offset  0x{meta['gobjects']:X}   // FChunkedFixedUObjectArray\n")
        if gw:
            f.write(f"#define GWorld_Offset    0x{meta['gworld']:X}   // UWorld**  candidates: "
                    + ", ".join(f"0x{a - e.d.base:X}" for a in gw_all) + "\n")
        if pe_idx is not None:
            f.write(f"#define ProcessEvent_Index {pe_idx}   // UObject vtable slot\n#define ProcessEvent_Offset 0x{meta['process_event']:X}\n")
        f.write(f"#define FUObjectItem_Size {e.arr.item_size}\n#define FNamePool_BlockBits {e.pool.block_bits}\n\n")
        for k, v in e.O.items():
            if v is not None:
                f.write(f"#define OFF_{k:<40} 0x{v:X}\n")
        # engine kind, container sizes (measured from properties) and object-array layout for GameDefines_core.hpp
        ue3 = type(e).__name__ == "Engine3"
        pool_kind = type(e.pool).__name__
        names_kind = (6 if pool_kind == "PoolNames" else 3) if ue3 else (5 if pool_kind == "NamePool" else 4)
        f.write(f"\n#define UE_ENGINE {3 if ue3 else 4}\n#define UE_NAMES_KIND {names_kind}   // 3 TArray<FNameEntry*>, 4 TNameEntryArray, 5 FNamePool, 6 Mass Effect LE pools\n"
                f"#define NameEntry_StrOffset {dict({5: 2, 6: 0xc}).get(names_kind, 0x10):#x}\n")
        sizes = {}
        for o in list(g.structs) + list(g.classes):
            for pr in e.properties(o):
                sizes.setdefault(e.ffield_class_name(pr), e.prop_size(pr))
        defaults = dict(StrProperty=16, DelegateProperty=16, MulticastDelegate=16, SparseDelegate=1, InterfaceProperty=16,
                        MapProperty=80, SetProperty=80, TextProperty=24, LazyObjectProperty=28, SoftObjectProperty=40, FieldPathProperty=32)
        sizes["MulticastDelegate"] = sizes.get("MulticastInlineDelegateProperty") or sizes.get("MulticastDelegateProperty")
        sizes["SparseDelegate"] = sizes.get("MulticastSparseDelegateProperty")
        for k, dv in defaults.items():
            f.write(f"#define SIZEOF_{k:<26} {sizes.get(k) or dv}{'' if sizes.get(k) else '   // default, not measured'}\n")
        if e.arr.layout:
            for k, v in zip(("Objects", "Max", "Num", "MaxChunks", "NumChunks"), e.arr.layout):
                f.write(f"#define GObjects_Layout_{k:<12} 0x{v:X}\n")
        elif hasattr(e.arr, "objects"):
            f.write("#define GObjects_Flat 1   // FFixedUObjectArray {FUObjectItem* Objects; int32 Max; int32 Num} (UE 4.8-4.19)\n")
        f.write("\n" + open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "GameDefines_core.hpp")).read())
    json.dump(meta, open(os.path.join(out, "offsets.json"), "w"), indent=1)
    with open(os.path.join(out, "GObjects.txt"), "w", encoding="utf-8") as f:
        for i, o in e.arr:
            f.write(f"[{i:08X}] {{{o:#x}}} {e.fullname(o)}\n")
    print("wrote", out)
