"""ReClass.NET project writer (.rcnet / .rcnetxml), engine neutral.

Used by uerc.py (Unreal) and il2rc.py (Unity IL2CPP). Measured facts about the format are in
Handout_Il2Cpp_ReClass.md: ZIP with one Data.xml, <reclass version="65537" type="x64">, classes carry a uuid,
ClassInstanceNode/PointerNode reference it, EnumNode references the enum by name, 1-byte enums are read signed.
"""
import uuid, zipfile
import xml.etree.ElementTree as ET

HEX = [(8, "Hex64Node"), (4, "Hex32Node"), (2, "Hex16Node"), (1, "Hex8Node")]
ENUM_SIZE = {1: "OneByte", 2: "TwoBytes", 4: "FourBytes", 8: "EightBytes"}


class RCWriter:
    def __init__(self):
        self.uuid = {}            # key -> uuid string
        self.classes = {}         # key -> (name, comment, address, [nodes])
        self.enums = {}           # name -> (size, [(name, value)])

    def uid(self, key):
        return self.uuid.setdefault(key, str(uuid.uuid4()))

    def helper(self, key, name, nodes):
        """Register a shared helper class once (FName, TArray<T>, Il2Cpp String, ...) and return its uuid."""
        if key not in self.classes:
            self.classes[key] = (name, "", "", nodes)
        return self.uid(key)

    def node(self, typ, name, comment="", **attrs):
        return dict(type=typ, name=name, comment=comment, **attrs)

    def pad(self, n, off, nodes):
        if n > 1 << 24:     # ponytail: no real layout is >16 MB; a garbage offset once ate 40 GB of RAM in pad nodes
            nodes.append(self.node("Hex64Node", f"pad_{off:04X}", f"skipped {n:#x} bytes of padding"))
            return nodes
        i = 0
        while n > 0:
            for size, typ in HEX:
                if n >= size and (off % size == 0 or size == 1):
                    nodes.append(self.node(typ, f"pad_{off:04X}" if i == 0 else f"pad_{off:04X}_{i}"))
                    n -= size; off += size; i += 1
                    break
        return nodes

    def instance(self, key, name, tname):
        return self.node("ClassInstanceNode", name, tname, reference=self.uid(key), tname=tname)

    def pointer_to(self, key, name, tname="void"):
        n = self.node("PointerNode", name, tname + "*", tname=tname + "*")
        n["inner"] = self.instance(key, name, tname) if key else self.node("Hex64Node", name)
        return n

    def signed(self, v, size):
        """ReClass reads enum values signed: fold v into the signed range of `size` bytes."""
        bits = 8 * (size if size in (1, 2, 4, 8) else 1)
        return ((v + (1 << (bits - 1))) % (1 << bits)) - (1 << (bits - 1))

    def emit(self, parent, n):
        el = ET.SubElement(parent, "node", type=n["type"], name=n["name"], comment=n.get("comment", ""), hidden="False")
        for k in ("reference", "count", "length"):
            if k in n:
                el.set(k, str(n[k]))
        if "inner" in n:
            self.emit(el, n["inner"])

    def write(self, out):
        root = ET.Element("reclass", version="65537", type="x64")
        ET.SubElement(root, "custom_data")
        ens = ET.SubElement(root, "enums")
        for name, (size, items) in self.enums.items():
            en = ET.SubElement(ens, "enum", name=name, size=size, flags="False")
            for n, v in items:
                ET.SubElement(en, "item", name=n, value=str(v))
        cls = ET.SubElement(root, "classes")
        for key, (name, comment, addr, nodes) in self.classes.items():
            c = ET.SubElement(cls, "class", uuid=self.uid(key), name=name, comment=comment, address=addr)
            for n in nodes:
                self.emit(c, n)
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if out.endswith(".rcnetxml"):
            open(out, "wb").write(data)
        else:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("Data.xml", data)
