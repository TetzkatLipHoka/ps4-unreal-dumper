"""Live read/write of a property on all instances of a class, using the dump for lookup and ps4debug for access.

    python uelive.py ../dumps/lollipop.bin 192.168.x.y BP_CH_Main_Juliet_C bGodMode        # read
    python uelive.py ../dumps/lollipop.bin 192.168.x.y BP_CH_Main_Juliet_C bGodMode 1      # write
    python uelive.py ../dumps/lollipop.bin 192.168.x.y BP_CH_Main_Juliet_C                 # list properties
"""
import asyncio, math, struct, sys
import ueobj
from ueobj import CF, RF_ClassDefaultObject

FMT = dict(BoolProperty="<B", ByteProperty="<B", Int8Property="<b", Int16Property="<h", UInt16Property="<H",
           IntProperty="<i", UInt32Property="<I", Int64Property="<q", UInt64Property="<Q", FloatProperty="<f",
           DoubleProperty="<d", EnumProperty="<B", ObjectProperty="<Q", ClassProperty="<Q", NameProperty="<i")


def resolve(e, cls, prop):
    """Walk the class chain for a property; returns (field, owning class)."""
    c = cls
    while c:
        m = e.member(c, prop)
        if m:
            return m, c
        c = e.super(c)
    return None, None


async def main(dump, host, cname, prop=None, value=None):
    e = ueobj.open_engine(dump, log=lambda *a: None)
    cls = e.find(cname, CF["Class"])
    if not cls:
        sys.exit(f"class {cname} not found")
    if not prop:
        c = cls
        while c:
            for p in e.properties(c):
                print(f"  +{e.prop_offset(p):#06x} {e.ffield_class_name(p):26} {e.ffield_name(p)}   ({e.name(c)})")
            c = e.super(c)
        return
    f, owner = resolve(e, cls, prop)
    if not f:
        sys.exit(f"property {prop} not found in {cname} or its supers")
    kind, off = e.ffield_class_name(f), e.prop_offset(f)
    mask = 0xFF
    if kind == "BoolProperty":
        mask = e.bool_info(f)[3]
    fmt = FMT.get(kind)
    if not fmt:
        sys.exit(f"{kind} not supported for live editing")
    print(f"{cname}.{prop}: {kind} at +{off:#x} (declared in {e.name(owner)})" + (f" bitmask {mask:#04x}" if mask != 0xFF else ""))

    def is_a(c):
        while c:
            if c == cls:
                return True
            c = e.super(c)
        return False
    subs = {c for c in {e.cls(o) for i, o in e.arr} if is_a(c)}
    inst = [o for i, o in e.arr if e.cls(o) in subs and not (e.flags(o) or 0) & RF_ClassDefaultObject]
    print(f"{len(inst)} instance(s) in dump ({len(subs)} classes incl. subclasses)")
    import ps4debug
    dbg = ps4debug.PS4Debug(host)
    pid = await ueobj.game_pid(dbg)
    for o in inst:
        live_cls = await dbg.read_memory(pid, o + e.O["UObject_Class"], 8)
        if not live_cls or struct.unpack("<Q", live_cls)[0] != e.cls(o):
            print(f"  {o:#x}: object gone (class changed)")
            continue
        raw = await dbg.read_memory(pid, o + off, struct.calcsize(fmt))
        cur = struct.unpack(fmt, raw)[0]
        shown = bool(cur & mask) if mask != 0xFF else cur
        print(f"  {o:#x} {e.fullname(o).split(' ', 1)[1]}: {shown}")
        if value is not None:
            if mask != 0xFF:
                new = (cur | mask) if value not in ("0", "false") else (cur & ~mask & 0xFF)
            else:
                new = float(value) if fmt[-1] in "fd" else int(value, 0)
            await dbg.write_memory(pid, o + off, struct.pack(fmt, new))
            back = struct.unpack(fmt, await dbg.read_memory(pid, o + off, struct.calcsize(fmt)))[0]
            print(f"    -> written, readback {bool(back & mask) if mask != 0xFF else back}")


if __name__ == "__main__":
    a = sys.argv[1:]
    ueobj.run(main(a[0], a[1], a[2], a[3] if len(a) > 3 else None, a[4] if len(a) > 4 else None))
