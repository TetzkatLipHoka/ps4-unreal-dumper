"""Export Il2CppDumper output (dump.cs, optional script.json) as a ReClass.NET project (.rcnet).

    python il2rc.py dump.cs out.rcnet                                 # Assembly-CSharp*.dll classes
    python il2rc.py dump.cs out.rcnet --images Assembly-CSharp.dll,Rewired_Core.dll
    python il2rc.py dump.cs out.rcnet --roots PlayerController,GameManager --depth 2
        # small project: only these classes plus what they reach through pointers (depth hops)
    python il2rc.py dump.cs out.rcnet --module eboot.bin --base 0x400000

Layout facts (measured on Aeterna Noctis / The Forest dumps, see Handout_Il2Cpp_ReClass.md):
class field offsets include the 16 byte object header (klass*, monitor*), struct offsets do not,
static fields have their own offset row (relative to the class' static field block), consts have none.
Classes with static fields get a second class "<Name>_StaticFields" whose address is resolved live from
the <Name>_TypeInfo slot in script.json: [[<module> + slot] + static_fields] (Il2CppClass.static_fields = 0xB8 on x64).
"""
import json, os, re
from collections import defaultdict
from rcwrite import RCWriter, ENUM_SIZE

PRIM = dict(bool=("BoolNode", 1), sbyte=("Int8Node", 1), byte=("UInt8Node", 1), short=("Int16Node", 2), ushort=("UInt16Node", 2),
            int=("Int32Node", 4), uint=("UInt32Node", 4), long=("Int64Node", 8), ulong=("UInt64Node", 8),
            float=("FloatNode", 4), double=("DoubleNode", 8), char=("UInt16Node", 2), IntPtr=("Hex64Node", 8), UIntPtr=("Hex64Node", 8))
MODS = {"public", "private", "internal", "protected", "static", "readonly", "const", "volatile", "abstract", "sealed", "new", "unsafe"}
HEADER = 0x10                                   # Il2CppObject: klass*, monitor*
TYPE_RE = re.compile(r"^(?P<mods>(?:\w+\s+)*?)(?P<kind>class|struct|enum|interface)\s+(?P<name>\S+)(?:\s*:\s*(?P<bases>.*?))?\s*// TypeDefIndex: (?P<idx>\d+)")
FIELD_RE = re.compile(r"^\t(?P<decl>.+?)(?: = (?P<val>.*?))?; // 0x(?P<off>[0-9A-Fa-f]+)$")
CONST_RE = re.compile(r"^\t(?P<decl>.+?) = (?P<val>.*?);$")
IMAGE_RE = re.compile(r"^// Image \d+: (?P<name>\S+) - (?P<start>\d+)")


class Type:
    __slots__ = ("kind", "name", "ns", "idx", "bases", "fields", "statics", "consts", "image", "enum_base")

    def __init__(self, kind, name, ns, idx, bases):
        self.kind, self.name, self.ns, self.idx, self.bases = kind, name, ns, idx, bases
        self.fields, self.statics, self.consts = [], [], []     # (offset, type, name)
        self.image, self.enum_base = "", "int"

    @property
    def full(self):
        return f"{self.ns}.{self.name}" if self.ns else self.name


def split_decl(decl):
    """'private static readonly Dictionary<string, int> map' -> (mods, 'Dictionary<string, int>', 'map')"""
    words = decl.split(" ")
    i = 0
    while i < len(words) and words[i] in MODS:
        i += 1
    return set(words[:i]), " ".join(words[i:-1]), words[-1]


def parse(path):
    types, images, ns = [], [], ""
    cur = None
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.rstrip("\r\n")
        m = IMAGE_RE.match(line)
        if m:
            images.append((int(m["start"]), m["name"]))
            continue
        if line.startswith("// Namespace: "):
            ns = line[14:].strip()
            continue
        m = TYPE_RE.match(line)
        if m:
            bases = [b.strip() for b in m["bases"].split(",")] if m["bases"] else []
            cur = Type(m["kind"], m["name"], ns, int(m["idx"]), bases)
            types.append(cur)
            continue
        if cur is None or not line.startswith("\t") or line.startswith("\t\t") or line.startswith("\t["):
            if line == "}":
                cur = None
            continue
        if line.startswith("\t//"):
            if line.startswith("\t// Methods") or line.startswith("\t// Properties"):
                cur = None                      # fields come first, nothing below is a field
            continue
        m = FIELD_RE.match(line)
        if m:
            mods, typ, name = split_decl(m["decl"])
            off = int(m["off"], 16)
            if off >= 0x1000000:                # thread statics and similar markers (0x80000000, 0xFFFFFFFF) are no layout
                continue
            if cur.kind == "enum" and name == "value__":
                cur.enum_base = typ
            elif "static" in mods:
                cur.statics.append((off, typ, name))
            else:
                cur.fields.append((off, typ, name))
            continue
        m = CONST_RE.match(line)
        if m:
            mods, typ, name = split_decl(m["decl"])
            if "const" in mods:
                cur.consts.append((typ, name, m["val"]))
    images.sort()
    for t in types:
        for start, name in images:
            if t.idx >= start:
                t.image = name
    return types


class Il2RC(RCWriter):
    def __init__(self, types, typeinfo, module, base, static_off):
        super().__init__()
        self.types = types
        self.by_name = defaultdict(list)          # simple name (generic args stripped) -> [Type]
        for t in types:
            self.by_name[strip_generic(t.name)].append(t)
        self.typeinfo, self.module, self.base, self.static_off = typeinfo, module, base, static_off
        self.wanted = set()

    # ---- type resolution -----------------------------------------------------------------------------------
    def resolve(self, tname, ns=""):
        """Field type string -> Type or None (arrays, pointers, generic params stay unresolved)."""
        if tname.endswith("[]") or tname.endswith("*") or tname.endswith("&"):
            return None
        cands = self.by_name.get(strip_generic(tname))
        if not cands:
            return None
        same = [c for c in cands if c.ns == ns]
        return (same or cands)[0]

    def size_of(self, tname, ns="", depth=0):
        if tname in PRIM:
            return PRIM[tname][1]
        t = self.resolve(tname, ns)
        if t is None or t.kind in ("class", "interface") or depth > 8:
            return 8                              # reference or unknown: pointer
        if t.kind == "enum":
            return PRIM.get(t.enum_base, ("", 4))[1]
        if not t.fields:
            return 8
        off, ftype, _ = max(t.fields)
        return off + self.size_of(ftype, t.ns, depth + 1)

    # ---- helpers (String, Array<T>, List<T>) ---------------------------------------------------------------
    def string_cls(self):
        return self.helper("__String", "String", [self.node("Hex64Node", "klass"), self.node("Hex64Node", "monitor"),
                                                  self.node("Int32Node", "length"), self.node("Utf16TextNode", "chars", length=64)])

    def array_cls(self, elem_type, ns):
        key = ("__Array", elem_type)
        if key not in self.classes:
            elem = self.field_node(elem_type, "Element", ns)
            arr = self.node("ArrayNode", "vector", elem_type, count=16)
            arr["inner"] = elem
            self.helper(key, f"Array<{elem_type}>", [self.node("Hex64Node", "klass"), self.node("Hex64Node", "monitor"),
                                                     self.node("Hex64Node", "bounds"), self.node("Int64Node", "max_length"), arr])
        return key

    def list_cls(self, elem_type, ns):
        key = ("__List", elem_type)
        if key not in self.classes:
            items = self.pointer_to(self.array_cls(elem_type, ns), "_items", f"Array<{elem_type}>")
            self.helper(key, f"List<{elem_type}>", [self.node("Hex64Node", "klass"), self.node("Hex64Node", "monitor"), items,
                                                    self.node("Int32Node", "_size"), self.node("Int32Node", "_version")])
        return key

    # ---- field -> node ---------------------------------------------------------------------------------------
    def field_node(self, tname, name, ns):
        if tname in PRIM:
            return self.node(PRIM[tname][0], name, tname)
        if tname == "string":
            return self.pointer_to("__String", name, "String") if self.string_cls() else None
        if tname.endswith("[]") or tname.endswith("[,]"):
            elem = tname[:tname.index("[")]
            return self.pointer_to(self.array_cls(elem, ns), name, f"Array<{elem}>")
        if tname.startswith("List<") and tname.endswith(">"):
            elem = tname[5:-1]
            return self.pointer_to(self.list_cls(elem, ns), name, f"List<{elem}>")
        t = self.resolve(tname, ns)
        if t is None:
            return self.node("Hex64Node", name, tname)          # generic parameter, pointer, delegate of unknown shape
        if t.kind == "enum":
            return self.node("EnumNode", name, tname, reference=self.add_enum(t))
        if t.kind == "struct":
            if t in self.wanted:
                return self.instance(t, name, t.name)
            size = self.size_of(tname, ns)              # struct outside the export: opaque bytes of its size
            key = ("__raw", size)
            self.helper(key, f"Raw{size:X}", self.pad(size, 0, []))
            return self.instance(key, name, f"Raw{size:X}")
        return self.pointer_to(t if t in self.wanted else None, name, tname)

    def add_enum(self, t):
        if t.full not in self.enums:
            size = PRIM.get(t.enum_base, ("", 4))[1]
            items = []
            for _, name, val in t.consts:
                try:
                    items.append((name, self.signed(int(val), size)))
                except ValueError:
                    pass
            self.enums[t.full] = (ENUM_SIZE.get(size, "FourBytes"), items)
        return t.full

    # ---- class layout ----------------------------------------------------------------------------------------
    def chain(self, t):
        """Base classes first (MonoBehaviour -> Behaviour -> Component -> Object), then t."""
        out, seen = [], set()
        while t is not None and t not in seen:
            seen.add(t); out.append(t)
            t = self.resolve(t.bases[0], t.ns) if t.bases else None
            if t is not None and t.kind != "class":
                t = None
        return out[::-1]

    def build(self, t):
        nodes, pos = [], 0
        if t.kind != "struct":
            nodes += [self.node("Hex64Node", "klass", "Il2CppClass*"), self.node("Hex64Node", "monitor")]
            pos = HEADER
        fields = []
        for owner in self.chain(t):
            fields += [(off, ftype, name, owner) for off, ftype, name in owner.fields]
        for off, ftype, name, owner in sorted(fields, key=lambda f: f[0]):
            if off < pos:
                continue
            if off > pos:
                self.pad(off - pos, pos, nodes)
            n = self.field_node(ftype, name, owner.ns)
            size = self.size_of(ftype, owner.ns)
            if n is None:
                self.pad(size, off, nodes)
                nodes[-1]["comment"] = ftype
            else:
                nodes.append(n)
            if owner is not t:
                nodes[-1]["comment"] = f"{owner.name}: {nodes[-1].get('comment', '')}".rstrip(": ")
            pos = off + size
        self.classes[t] = (t.name, t.full if t.ns else "", "", nodes)
        if t.statics:
            self.build_statics(t)

    def build_statics(self, t):
        nodes, pos = [], 0
        for off, ftype, name in sorted(t.statics):
            if off < pos:
                continue
            if off > pos:
                self.pad(off - pos, pos, nodes)
            n = self.field_node(ftype, name, t.ns)
            size = self.size_of(ftype, t.ns)
            if n is None:
                self.pad(size, off, nodes)
                nodes[-1]["comment"] = ftype
            else:
                nodes.append(n)
            pos = off + size
        slot = self.typeinfo.get(t.full)
        addr = ""
        if slot is not None:
            origin = f"0x{self.base + slot:X}" if self.base is not None else f"<{self.module}> + 0x{slot:X}"
            addr = f"[[{origin}] + 0x{self.static_off:X}]"
        self.classes[(t, "static")] = (f"{t.name}_StaticFields", f"static fields of {t.full}", addr, nodes)

    # ---- driver -----------------------------------------------------------------------------------------------
    def pointer_targets(self, t):
        for owner in self.chain(t):
            for _, ftype, _ in owner.fields + owner.statics:
                inner = ftype[5:-1] if ftype.startswith("List<") else ftype.rstrip("[]")
                d = self.resolve(inner, owner.ns)
                if d is not None and d.kind == "class":
                    yield d

    def run(self, out, images=(), roots=(), depth=2):
        want = set()
        if roots:
            frontier = [t for r in roots for t in self.by_name.get(strip_generic(r), []) if t.kind in ("class", "struct")]
            want.update(frontier)
            for _ in range(depth):
                nxt = [d for t in frontier for d in self.pointer_targets(t) if d not in want]
                want.update(nxt); frontier = nxt
        else:
            want.update(t for t in self.types if t.kind in ("class", "struct") and any(i.lower() in t.image.lower() for i in images))
        todo = list(want)                         # closure over base classes and by-value structs
        while todo:
            t = todo.pop()
            for b in self.chain(t):
                if b not in want:
                    want.add(b); todo.append(b)
            for _, ftype, _ in t.fields + t.statics:
                d = self.resolve(ftype, t.ns)
                if d is not None and d.kind == "struct" and d not in want:
                    want.add(d); todo.append(d)
        self.wanted = want
        for t in sorted(want, key=lambda t: t.idx):
            self.build(t)
        self.write(out)
        print(f"{len(self.classes)} classes, {len(self.enums)} enums -> {out}")


def strip_generic(name):
    return name.split("<")[0]


def load_typeinfo(path):
    """script.json ScriptMetadata '<Namespace.Class>_TypeInfo' -> address of the Il2CppClass* slot."""
    if not path or not os.path.exists(path):
        return {}
    d = json.load(open(path, encoding="utf-8"))
    return {e["Name"][:-9]: e["Address"] for e in d.get("ScriptMetadata", []) if e["Name"].endswith("_TypeInfo")}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="dump.cs from Il2CppDumper")
    ap.add_argument("out", help="output .rcnet (or .rcnetxml for plain XML)")
    ap.add_argument("--images", default="Assembly-CSharp", help="comma separated image name substrings (default Assembly-CSharp)")
    ap.add_argument("--roots", help="comma separated class names; export only these and what they reach")
    ap.add_argument("--depth", type=int, default=2, help="pointer hops from the roots (default 2)")
    ap.add_argument("--script", help="script.json for the _TypeInfo addresses (default: next to dump.cs)")
    ap.add_argument("--module", default="Il2CppUserAssemblies.prx", help="module name for live addresses (default Il2CppUserAssemblies.prx)")
    ap.add_argument("--base", type=lambda x: int(x, 0), help="fixed image base instead of the module name, e.g. 0x400000 for eboot.bin")
    ap.add_argument("--static-fields", type=lambda x: int(x, 0), default=0xB8, help="Il2CppClass.static_fields offset (default 0xB8)")
    a = ap.parse_args()
    script = a.script or os.path.join(os.path.dirname(os.path.abspath(a.dump)), "script.json")
    rc = Il2RC(parse(a.dump), load_typeinfo(script), a.module, a.base, a.static_fields)
    rc.run(a.out, a.images.split(","), a.roots.split(",") if a.roots else (), a.depth)
