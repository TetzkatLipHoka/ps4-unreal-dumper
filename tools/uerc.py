"""Export classes from a dump as a ReClass.NET project (.rcnet) for live browsing with the PS4Debug plugin.

    python uerc.py ../dumps/lollipop.bin out.rcnet                    # all /Script/ packages
    python uerc.py ../dumps/lollipop.bin out.rcnet BP_CH_Main_Juliet  # plus packages containing these substrings
    python uerc.py ../dumps/lollipop.bin out.rcnet --roots World,BP_CH_Main_Juliet_C --depth 2
        # small project: only these classes plus what they reach through pointers (depth hops)
"""
import math
import ueobj, uegen
from ueobj import CF
from rcwrite import RCWriter, ENUM_SIZE     # engine neutral .rcnet writer, shared with il2rc.py

NUM = dict(Int8Property="Int8Node", Int16Property="Int16Node", IntProperty="Int32Node", Int64Property="Int64Node",
           UInt16Property="UInt16Node", UInt32Property="UInt32Node", UInt64Property="UInt64Node",
           FloatProperty="FloatNode", DoubleProperty="DoubleNode", LargeWorldCoordinatesRealProperty="DoubleNode")


class RC(RCWriter):
    def __init__(self, e, gen):
        super().__init__()
        self.e, self.g, self.O = e, gen, e.O

    # ---- helper classes (FName, FString, TArray<T>, ...) -------------------------------------------------------
    def fname_cls(self):
        return self.helper("__FName", "FName", [self.node("Int32Node", "ComparisonIndex"), self.node("Int32Node", "Number")])

    def fstring_cls(self):
        return self.helper("__FString", "FString", [self.node("Utf16TextPtrNode", "Data", length=64),
                                                    self.node("Int32Node", "Num"), self.node("Int32Node", "Max")])

    # ---- property -> node -------------------------------------------------------------------------------------
    def prop_node(self, p, name, size):
        e, O, g = self.e, self.O, self.g
        k = e.ffield_class_name(p)
        if k in NUM:
            return self.node(NUM[k], name, k)
        if k == "BoolProperty":
            return self.node("BoolNode", name, k)
        if k in ("ByteProperty", "EnumProperty"):
            en = e.ptr(p + (O["ByteProperty_Enum"] if k == "ByteProperty" else O["EnumProperty_Base"] + 8))
            ename = self.add_enum(en, size) if en in g.enums else None
            if ename:
                return self.node("EnumNode", name, k, reference=ename)
            return self.node({1: "UInt8Node", 2: "UInt16Node", 4: "UInt32Node", 8: "UInt64Node"}.get(size, "UInt8Node"), name, k)
        if k == "NameProperty":
            return self.node("ClassInstanceNode", name, k, reference=self.fname_cls(), tname="FName")
        if k == "StrProperty":
            return self.node("ClassInstanceNode", name, k, reference=self.fstring_cls(), tname="FString")
        if k in ("ObjectProperty", "ObjectPtrProperty", "ClassProperty", "ClassPtrProperty", "InterfaceProperty"):
            c = e.ptr(p + O["ObjectProperty_Class"]) if k != "InterfaceProperty" else None
            if k in ("ClassProperty", "ClassPtrProperty"):
                c = self.g.e.find("Class", CF["Class"])
            return self.pointer_to(c if c in self.wanted else None, name, g.cname(c) if c in g.classes else "UObject")
        if k == "StructProperty":
            s = e.ptr(p + O["StructProperty_Struct"])
            if s in self.wanted:
                return self.node("ClassInstanceNode", name, k, reference=self.uid(s), tname=g.cname(s))
        if k == "ArrayProperty":
            inner = e.ptr(p + O["ArrayProperty_Inner"])
            if inner:
                isize = e.prop_size(inner) or 1
                elem = self.prop_node(inner, "Element", isize)
                if elem is None:                      # unsupported element type: raw bytes of the element size
                    ref = self.helper(("__raw", isize), f"Raw{isize:X}", self.pad(isize, 0, []))
                    elem = self.node("ClassInstanceNode", "Element", e.ffield_class_name(inner), reference=ref, tname=f"Raw{isize:X}")
                key = ("__TArray", self.type_key(elem))
                ptr = self.node("PointerNode", "Data")
                ptr["inner"] = elem
                ref = self.helper(key, f"TArray<{self.type_key(elem)}>", [ptr, self.node("Int32Node", "Num"), self.node("Int32Node", "Max")])
                return self.node("ClassInstanceNode", name, "TArray", reference=ref, tname=f"TArray<{self.type_key(elem)}>")
        if k == "WeakObjectProperty":
            ref = self.helper("__FWeakObjectPtr", "FWeakObjectPtr", [self.node("Int32Node", "ObjectIndex"), self.node("Int32Node", "ObjectSerialNumber")])
            return self.node("ClassInstanceNode", name, k, reference=ref, tname="FWeakObjectPtr")
        return None                                   # -> hex padding of `size`

    def native_node(self, name, kind, cls):
        tname = self.g.cname(cls) if cls in self.g.classes else "UObject"
        key = cls if cls in self.wanted else None
        if kind == "ptr":
            return self.pointer_to(key, name, tname)
        elem = self.pointer_to(key, "Element", tname)
        data = self.node("PointerNode", "Data")
        data["inner"] = elem
        ref = self.helper(("__TArray", self.type_key(elem)), f"TArray<{self.type_key(elem)}>",
                          [data, self.node("Int32Node", "Num"), self.node("Int32Node", "Max")])
        return self.node("ClassInstanceNode", name, "TArray", reference=ref, tname=f"TArray<{self.type_key(elem)}>")

    def type_key(self, n):
        return n.get("tname") or n.get("comment") or n["type"]

    def add_enum(self, en, size):
        name = self.g.cname(en)
        if name not in self.enums:
            e, O = self.e, self.O
            arr, num = e.u64(en + O["UEnum_Names"]), e.i32(en + O["UEnum_Names"] + 8) or 0
            pair = e.enum_stride
            items, seen = [], set()
            for i in range(min(num, 4096)):
                n = (e.fname(arr + i * pair) or f"Value_{i}").split("::")[-1]
                v = e.val(arr + i * pair + (0x10 if e.case_preserving else 8), "<q") if not e.enum_names_only else i
                if n not in seen and v is not None:
                    bits = 8 * (size if size in (1, 2, 4, 8) else 1)
                    v = ((v + (1 << (bits - 1))) % (1 << bits)) - (1 << (bits - 1))   # ReClass reads enums signed
                    seen.add(n); items.append((n, v))
            if not items:
                return None
            self.enums[name] = (ENUM_SIZE.get(size, "OneByte"), items)
        return name

    # ---- struct / class ---------------------------------------------------------------------------------------
    def build(self, s):
        e, O, g = self.e, self.O, self.g
        nodes, pos = [], 0
        sup = e.super(s)
        if sup in self.wanted:
            nodes.append(self.node("ClassInstanceNode", g.cname(sup), "base", reference=self.uid(sup), tname=g.cname(sup)))
            pos = e.struct_size(sup) or 0
        elif s == self.uobject:
            fields = [(0, 8, self.node("Hex64Node", "VTable")),
                      (O["UObject_Flags"], 4, self.node("UInt32Node", "ObjectFlags")),
                      (O["UObject_Index"], 4, self.node("Int32Node", "InternalIndex")),
                      (O["UObject_Class"], 8, self.pointer_to(self.uclass if self.uclass in self.wanted else None, "Class", "UClass")),
                      (O["UObject_Name"], 8, self.node("ClassInstanceNode", "Name", "FName", reference=self.fname_cls(), tname="FName")),
                      (O["UObject_Outer"], 8, self.pointer_to(self.uobject, "Outer", "UObject"))]
            for off, fsize, n in sorted(fields, key=lambda t: t[0]):
                if off > pos:
                    self.pad(off - pos, pos, nodes)
                nodes.append(n)
                pos = off + fsize
        size = e.struct_size(s) or pos
        items = [(e.prop_offset(p) or 0, p) for p in e.properties(s)]
        items += [(off, (name, kind, cls)) for off, name, kind, cls in e.native_members.get(s, [])]
        bits = {}                                     # byte offset -> [names]
        for off, p in sorted(items, key=lambda t: t[0]):
            if isinstance(p, tuple):                  # unreflected native field (UE3 UWorld/ULevel)
                name, kind, cls = p
                if off >= pos:
                    self.flush_bits(bits, pos, off, nodes)
                    nodes.append(self.native_node(name, kind, cls))
                    pos = off + (16 if kind == "tarray" else 8)
                continue
            off, n = e.prop_offset(p), e.ffield_name(p) or "?"
            dim = e.i32(p + O["Property_ArrayDim"]) or 1
            psize = (e.prop_size(p) or 1) * dim
            if off is None:
                continue
            if e.ffield_class_name(p) == "BoolProperty" and g.bool_info(p)[3] != 0xFF:
                bits.setdefault(off, []).append((int(math.log2(g.bool_info(p)[2] or 1)), n))
                continue
            if off < pos:
                continue
            if off > pos:
                self.flush_bits(bits, pos, off, nodes)
                pos = off
            node = self.prop_node(p, n, e.prop_size(p) or 1)
            if node is None:
                self.pad(psize, off, nodes)
                nodes[-1]["name"] = n if psize <= 8 else nodes[-1]["name"]
                nodes[-1]["comment"] = e.ffield_class_name(p)
            elif dim > 1:
                arr = self.node("ArrayNode", n, node.get("comment", ""), count=dim)
                arr["inner"] = node
                nodes.append(arr)
            else:
                nodes.append(node)
            pos = off + psize
        self.flush_bits(bits, pos, size, nodes)
        self.classes[s] = (g.cname(s), e.fullname(s), "", nodes)

    def flush_bits(self, bits, pos, end, nodes):
        """Emit bytes [pos, end): bool-bit bytes as UInt8 with bit names, the rest as hex padding."""
        if end - pos > 1 << 24:     # ponytail: no real UStruct is >16 MB; a garbage Size once produced 70 GB of pad nodes
            nodes.append(self.node("Hex64Node", f"pad_{pos:04X}", f"skipped {end - pos:#x} bytes of padding"))
            return
        while pos < end:
            if pos in bits:
                names = sorted(bits.pop(pos))
                nodes.append(self.node("UInt8Node", "|".join(n for _, n in names), " ".join(f"bit{b}={n}" for b, n in names)))
                pos += 1
            else:
                nxt = min([b for b in bits if pos < b < end] + [end])
                self.pad(nxt - pos, pos, nodes)
                pos = nxt

    # ---- driver -------------------------------------------------------------------------------------------------
    def pointer_targets(self, s):
        e, O, g = self.e, self.O, self.g
        for _, _, _, c in e.native_members.get(s, []):
            if c in g.classes:
                yield c
        for p in e.properties(s):
            k = e.ffield_class_name(p)
            if k in ("ObjectProperty", "ObjectPtrProperty", "WeakObjectProperty"):
                c = e.ptr(p + O["ObjectProperty_Class"])
                if c in g.classes:
                    yield c
            elif k == "ArrayProperty":
                inner = e.ptr(p + O["ArrayProperty_Inner"])
                if inner and e.ffield_class_name(inner) in ("ObjectProperty", "ObjectPtrProperty"):
                    c = e.ptr(inner + O["ObjectProperty_Class"])
                    if c in g.classes:
                        yield c

    def run(self, filters, out, gworld, roots=(), depth=2):
        e, g = self.e, self.g
        g.collect()
        self.uobject = e.find("Object", CF["Class"])
        self.uclass = e.find("Class", CF["Class"])
        want = set()
        if roots:
            frontier = [e.find(r, CF["Class"]) or e.find(r, CF["Struct"]) for r in roots]
            frontier = [r for r in frontier if r]
            want.update(frontier)
            for _ in range(depth):
                nxt = []
                for s in frontier:
                    for c in self.pointer_targets(s):
                        if c not in want:
                            want.add(c); nxt.append(c)
                frontier = nxt
        else:
            for s in list(g.structs) + list(g.classes):
                pn = e.name(g.pkg_of[s]) or ""
                if pn.startswith("/Script/") or "/" not in pn or any(f.lower() in pn.lower() for f in filters):   # UE3 script packages have no path
                    want.add(s)
        # closure over supers and by-value struct members
        todo = list(want)
        while todo:
            s = todo.pop()
            sup = e.super(s)
            if sup and (sup in g.structs or sup in g.classes) and sup not in want:
                want.add(sup); todo.append(sup)
            for p in e.properties(s):
                if e.ffield_class_name(p) == "StructProperty":
                    d = e.ptr(p + self.O["StructProperty_Struct"])
                    if d in g.structs and d not in want:
                        want.add(d); todo.append(d)
        self.wanted = want
        for s in want:
            self.build(s)
        world = e.find("World", CF["Class"])
        if gworld and world in self.classes:
            n, c, _, nodes = self.classes[world]
            self.classes[world] = (n, c, f"[0x{gworld:X}]", nodes)
        self.write(out)
        print(f"{len(self.classes)} classes, {len(self.enums)} enums -> {out}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("dump"); ap.add_argument("out"); ap.add_argument("filters", nargs="*")
    ap.add_argument("--roots", help="comma separated class names; export only these and what they reach")
    ap.add_argument("--depth", type=int, default=2, help="pointer hops from the roots (default 2)")
    a = ap.parse_args()
    e = ueobj.open_engine(a.dump, log=lambda *a: None)
    gw, _ = uegen.find_gworld(e, log=lambda *a: None)
    RC(e, uegen.Gen(e)).run(a.filters, a.out, gw, a.roots.split(",") if a.roots else (), a.depth)
